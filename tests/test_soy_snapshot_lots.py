# tests/test_soy_snapshot_lots.py
"""
Lot-level SOY bootstrap: seeding FIFO lots from a lot-level IBKR positions
snapshot (positions query with the "Lot" level of detail).

When the trade-history reconstruction cannot cover the reported SOY
position, the ledger seeds per-lot FifoLots with REAL acquisition dates
from the snapshot instead of one estimated 31 Dec fallback lot. The
snapshot is used only when complete — any inconsistency falls back to the
old single-lot behaviour.
"""
import uuid
from decimal import Decimal

import pytest

import src.config as global_config
from src.domain.assets import Asset, SoyPositionLot
from src.domain.enums import AssetCategory
from src.engine.fifo_manager import FifoLedger
from src.parsers.parsing_orchestrator import ParsingOrchestrator
from src.parsers.raw_models import RawPositionRecord
from src.utils.currency_converter import CurrencyConverter
from tests.support.mock_providers import MockECBExchangeRateProvider


def _make_ledger(asset_category=AssetCategory.STOCK):
    provider = MockECBExchangeRateProvider(foreign_to_eur_init_value=Decimal("0.5"))
    converter = CurrencyConverter(rate_provider=provider)
    return FifoLedger(
        asset_internal_id=uuid.uuid4(),
        asset_category=asset_category,
        asset_multiplier_from_asset=None,
        currency_converter=converter,
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
        tax_classifier=None,
    )


def _asset(soy_qty, soy_lots, cost=Decimal("1500"), currency="USD"):
    return Asset(
        asset_category=AssetCategory.STOCK,
        ibkr_isin="US0000000001",
        ibkr_symbol="SNAP",
        soy_quantity=soy_qty,
        soy_cost_basis_amount=cost,
        soy_cost_basis_currency=currency,
        soy_lots=soy_lots,
    )


def _lot(open_date, qty, cost, currency="USD"):
    return SoyPositionLot(
        open_date=open_date,
        quantity=Decimal(qty),
        cost_basis_amount=Decimal(cost) if cost is not None else None,
        cost_basis_currency=currency if cost is not None else None,
    )


class TestSnapshotSeeding:
    def test_long_lots_seeded_with_real_dates(self):
        ledger = _make_ledger()
        asset = _asset(Decimal("10"), [
            _lot("2021-11-25", "5", "787.19"),
            _lot("2023-12-24", "5", "752.55"),
        ])
        ledger.initialize_lots_from_soy(asset, [], tax_year=2025)

        assert [l.acquisition_date for l in ledger.lots] == ["2021-11-25", "2023-12-24"]
        assert [l.quantity for l in ledger.lots] == [Decimal("5"), Decimal("5")]
        # USD→EUR at mock rate 0.5; NOT flagged as estimated (no SOY_FALLBACK)
        assert ledger.lots[0].total_cost_basis_eur == Decimal("787.19") * Decimal("0.5")
        assert all(not l.source_transaction_id.startswith("SOY_FALLBACK")
                   for l in ledger.lots)
        assert all(l.source_transaction_id.startswith("SOY_SNAPSHOT")
                   for l in ledger.lots)

    def test_quantity_mismatch_falls_back_to_single_lot(self):
        ledger = _make_ledger()
        asset = _asset(Decimal("10"), [_lot("2021-11-25", "5", "787.19")])
        ledger.initialize_lots_from_soy(asset, [], tax_year=2025)

        [lot] = ledger.lots
        assert lot.acquisition_date == "2024-12-31"
        assert lot.quantity == Decimal("10")
        assert lot.source_transaction_id.startswith("SOY_FALLBACK")

    def test_missing_cost_basis_falls_back(self):
        ledger = _make_ledger()
        asset = _asset(Decimal("10"), [
            _lot("2021-11-25", "5", "787.19"),
            _lot("2023-12-24", "5", None),
        ])
        ledger.initialize_lots_from_soy(asset, [], tax_year=2025)
        [lot] = ledger.lots
        assert lot.source_transaction_id.startswith("SOY_FALLBACK")

    def test_lot_dated_inside_tax_year_falls_back(self):
        ledger = _make_ledger()
        asset = _asset(Decimal("10"), [
            _lot("2025-01-02", "5", "787.19"),
            _lot("2023-12-24", "5", "752.55"),
        ])
        ledger.initialize_lots_from_soy(asset, [], tax_year=2025)
        [lot] = ledger.lots
        assert lot.source_transaction_id.startswith("SOY_FALLBACK")

    def test_short_lots_seeded(self):
        ledger = _make_ledger()
        asset = _asset(Decimal("-10"), [
            _lot("2024-06-01", "-10", "1200"),
        ])
        ledger.initialize_lots_from_soy(asset, [], tax_year=2025)

        assert not ledger.lots
        [short] = ledger.short_lots
        assert short.opening_date == "2024-06-01"
        assert short.quantity_shorted == Decimal("10")
        assert short.source_transaction_id.startswith("SOY_SNAPSHOT")

    def test_no_snapshot_keeps_old_fallback(self):
        ledger = _make_ledger()
        asset = _asset(Decimal("10"), [])
        ledger.initialize_lots_from_soy(asset, [], tax_year=2025)
        [lot] = ledger.lots
        assert lot.acquisition_date == "2024-12-31"
        assert lot.source_transaction_id.startswith("SOY_FALLBACK")


# ---------------------------------------------------------------------------
# Parser: LOT rows in the positions CSV
# ---------------------------------------------------------------------------

def _raw_position(**overrides):
    base = {
        "CurrencyPrimary": "USD",
        "AssetClass": "STK",
        "Symbol": "SNAP",
        "Description": "SNAPSHOT CORP",
        "ISIN": "US0000000001",
        "Conid": "12345",
        "Quantity": "10",
        "CostBasisMoney": "1500",
    }
    base.update(overrides)
    return RawPositionRecord(**base)


def _orchestrator():
    from src.classification.asset_classifier import AssetClassifier
    from src.identification.asset_resolver import AssetResolver

    class _Classifier(AssetClassifier):
        def __init__(self):
            super().__init__(cache_file_path="unused.json")

        def save_classifications(self):
            pass

    classifier = _Classifier()
    return ParsingOrchestrator(
        asset_resolver=AssetResolver(asset_classifier=classifier),
        asset_classifier=classifier,
        interactive_classification=False,
    )


class TestLotRowParsing:
    def test_summary_and_lot_rows_split(self):
        orch = _orchestrator()
        orch.raw_positions_start = [
            _raw_position(LevelOfDetail="SUMMARY"),
            _raw_position(LevelOfDetail="LOT", Quantity="4",
                          CostBasisMoney="600", OpenDateTime="2021-11-25;103000"),
            _raw_position(LevelOfDetail="LOT", Quantity="6",
                          CostBasisMoney="900", OpenDateTime="20231224;093000"),
        ]
        orch.process_positions()

        asset = orch.asset_resolver.get_asset_by_alias("ISIN:US0000000001")
        assert asset.soy_quantity == Decimal("10")
        assert asset.soy_cost_basis_amount == Decimal("1500")
        assert [(l.open_date, l.quantity) for l in asset.soy_lots] == [
            ("2021-11-25", Decimal("4")),
            ("2023-12-24", Decimal("6")),
        ]

    def test_rows_without_level_of_detail_behave_as_before(self):
        orch = _orchestrator()
        orch.raw_positions_start = [_raw_position()]
        orch.process_positions()
        asset = orch.asset_resolver.get_asset_by_alias("ISIN:US0000000001")
        assert asset.soy_quantity == Decimal("10")
        assert asset.soy_lots == []

    def test_eoy_lot_rows_do_not_overwrite_totals(self):
        orch = _orchestrator()
        orch.raw_positions_end = [
            _raw_position(LevelOfDetail="SUMMARY", MarkPrice="96.14"),
            _raw_position(LevelOfDetail="LOT", Quantity="4",
                          OpenDateTime="2021-11-25;103000"),
        ]
        orch.process_positions()
        asset = orch.asset_resolver.get_asset_by_alias("ISIN:US0000000001")
        assert asset.eoy_quantity == Decimal("10")

    def test_lot_without_open_date_ignored(self):
        orch = _orchestrator()
        orch.raw_positions_start = [
            _raw_position(LevelOfDetail="SUMMARY"),
            _raw_position(LevelOfDetail="LOT", Quantity="10"),
        ]
        orch.process_positions()
        asset = orch.asset_resolver.get_asset_by_alias("ISIN:US0000000001")
        assert asset.soy_lots == []
        assert asset.soy_quantity == Decimal("10")

    def test_holding_period_date_used_as_fallback(self):
        orch = _orchestrator()
        orch.raw_positions_start = [
            _raw_position(LevelOfDetail="SUMMARY"),
            _raw_position(LevelOfDetail="LOT", Quantity="10",
                          HoldingPeriodDateTime="2022-05-03;120000"),
        ]
        orch.process_positions()
        asset = orch.asset_resolver.get_asset_by_alias("ISIN:US0000000001")
        assert [l.open_date for l in asset.soy_lots] == ["2022-05-03"]
