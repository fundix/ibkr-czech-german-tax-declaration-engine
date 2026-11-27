# tests/test_audit_2026_11_pairing.py
"""
Regression tests for the lot-pairing findings of the 2026-11 audit:

* V04 — the start-of-year lots were rebuilt by replaying EVERY earlier year
  with the method of the current run, not with the method that year was
  filed with; an ``optimal`` year was replayed as FIFO.
* W05 — after ``optimal`` the ledger (portfolio, simulator) kept FIFO's lots.
* W06 — the optimal solver minimised the gain in EUR while the CZK base is
  what is filed.
* W07 — the solver compared dates only, so a lot bought later the same day
  could fill an earlier sale.
* W08 — the weighted average was taken in EUR and converted at the rate of
  the lot FIFO picked.

Everything is synthetic: an invented symbol, account U1234567, round EUR
amounts. Expected figures are worked out by hand in each test.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

import src.config as global_config
from src.domain.enums import AssetCategory
from src.engine.fifo_manager import FifoLedger
from src.engine.pairing import PairingHistory, PairingMethod
from src.pipeline_runner import run_core_processing_pipeline
from src.utils.currency_converter import CurrencyConverter
from tests.support.csv_creators import (
    create_cash_transactions_csv_string, create_corporate_actions_csv_string,
    create_positions_csv_string, create_trades_csv_string,
)
from tests.support.mock_providers import MockECBExchangeRateProvider

ACCOUNT = "U1234567"

PAIRA = dict(asset_class="STK", sub_category="COMMON", symbol="PAIRA",
             description="PAIRING ALPHA INC", isin="US0000000901",
             conid="900000901")


def _trade(security, trade_date, quantity, price, tx_id, *, buy_sell=None,
           open_close=None, commission="0"):
    """One Trades row (order of ``TRADES_FILE_HEADERS``), in EUR."""
    qty = Decimal(quantity)
    buy_sell = buy_sell or ("BUY" if qty > 0 else "SELL")
    open_close = open_close or ("O" if qty > 0 else "C")
    return [
        ACCOUNT, "EUR", security["asset_class"], security["sub_category"],
        security["symbol"], security["description"], security["isin"],
        None, None, None, trade_date, quantity, price, commission, "EUR",
        buy_sell, tx_id, "", "", security["conid"], "", "1", open_close,
    ]


def _position(security, quantity, cost_basis="0", mark_price="1"):
    """One Positions row (order of ``POSITIONS_FILE_HEADERS``), in EUR."""
    return [
        ACCOUNT, "EUR", security["asset_class"], security["sub_category"],
        security["symbol"], security["description"], security["isin"], quantity,
        str(Decimal(quantity) * Decimal(mark_price)), mark_price, cost_basis,
        None, security["conid"], None, "1",
    ]


def _one_to_one():
    return MockECBExchangeRateProvider(foreign_to_eur_init_value=Decimal("1.0"))


@pytest.fixture
def run(mock_config_paths, monkeypatch):
    """Run the core pipeline on synthetic rows; returns the ProcessingOutput."""
    monkeypatch.setattr(global_config, "IS_INTERACTIVE_CLASSIFICATION", False)
    paths = mock_config_paths

    def _run(trades, *, tax_year, soy=(), eoy=(), method=PairingMethod.FIFO,
             history=None, country="cz", **kwargs):
        monkeypatch.setattr(global_config, "TAX_YEAR", tax_year)
        files = {
            paths["trades"]: create_trades_csv_string(list(trades)),
            paths["pos_start"]: create_positions_csv_string(list(soy)),
            paths["pos_end"]: create_positions_csv_string(list(eoy)),
            paths["cash"]: create_cash_transactions_csv_string([]),
            paths["corp_actions"]: create_corporate_actions_csv_string([]),
        }
        for path, text in files.items():
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(text)
        return run_core_processing_pipeline(
            trades_file_path=paths["trades"],
            cash_transactions_file_path=paths["cash"],
            positions_start_file_path=paths["pos_start"],
            positions_end_file_path=paths["pos_end"],
            corporate_actions_file_path=paths["corp_actions"],
            interactive_classification_mode=False,
            tax_year_to_process=tax_year,
            custom_rate_provider=kwargs.pop("rates", None) or _one_to_one(),
            country_code=country,
            pairing_method=method,
            pairing_history=history,
            **kwargs,
        )
    return _run


def _rgl_view(rgls):
    """What a disposal row says, without the per-run ids."""
    return sorted(
        (r.acquisition_date, r.realization_date, r.quantity_realized,
         r.total_cost_basis_eur, r.total_realization_value_eur,
         r.gross_gain_loss_eur, r.holding_period_start,
         r.is_acquisition_estimated, r.holding_period_start_estimated)
        for r in rgls
    )


def _lots_view(output):
    rows = []
    for ledger in output.fifo_ledgers_by_asset_id.values():
        for lot in ledger.lots:
            rows.append((lot.acquisition_date, lot.quantity,
                         lot.total_cost_basis_eur, lot.source_transaction_id,
                         lot.acquisition_date_estimated))
        for lot in ledger.short_lots:
            rows.append((lot.opening_date, lot.quantity_shorted,
                         lot.total_sale_proceeds_eur, lot.source_transaction_id,
                         lot.opening_date_estimated))
    return sorted(rows)


# ---------------------------------------------------------------------------
# V04 — earlier years are replayed with the method they were filed with
# ---------------------------------------------------------------------------

# Year 1 (2024): buy 10 @ 100 in January, 10 @ 130 in March, sell 10 @ 150 in
# June. Year 2 (2025): sell the remaining 10 @ 160.
YEAR1 = [
    _trade(PAIRA, "2024-01-10", "10", "100", "101"),
    _trade(PAIRA, "2024-03-11", "10", "130", "102"),
    _trade(PAIRA, "2024-06-10", "-10", "150", "103"),
]
YEAR2 = [_trade(PAIRA, "2025-06-10", "-10", "160", "104")]


def _year2(run, *, method=PairingMethod.FIFO, history=None, soy_qty="10",
           sale_qty=None, soy_cost="1000"):
    trades = YEAR1 + ([_trade(PAIRA, "2025-06-10", f"-{sale_qty}", "160", "104")]
                      if sale_qty else YEAR2)
    return run(trades, tax_year=2025, soy=[_position(PAIRA, soy_qty, soy_cost)],
               method=method, history=history)


class TestV04PairingHistoryReplay:

    def test_lifo_filed_year_then_fifo_year(self, run):
        """Year 1 filed LIFO consumed the March lot (cost 1 300, gain 200), so
        the January lot is what is left: year 2 gains 1 600 - 1 000 = 600 and
        the lot's date is January. Before the fix the FIFO run replayed year 1
        as FIFO and sold the March lot a second time (gain 300)."""
        year1 = run(YEAR1, tax_year=2024, method=PairingMethod.LIFO)
        (r1,) = year1.realized_gains_losses
        assert r1.gross_gain_loss_eur == Decimal("200")
        assert r1.total_cost_basis_eur == Decimal("1300")

        year2 = _year2(run, history=PairingHistory(
            methods={2024: PairingMethod.LIFO}))
        (r2,) = year2.realized_gains_losses
        assert r2.gross_gain_loss_eur == Decimal("600")
        assert r2.acquisition_date == "2024-01-10"
        # Every purchase is claimed exactly once over the two years.
        assert r1.total_cost_basis_eur + r2.total_cost_basis_eur == Decimal("2300")
        assert year2.soy_unverified_symbols == []

    def test_no_history_means_earlier_years_are_fifo(self, run):
        """A LIFO run without a history replays the earlier year as FIFO (it
        was filed FIFO), not as LIFO: the March lot is what is left."""
        out = _year2(run, method=PairingMethod.LIFO)
        (r,) = out.realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("300")
        assert r.acquisition_date == "2024-03-11"

    def test_fifo_filed_year_then_lifo_year(self, run):
        out = _year2(run, method=PairingMethod.LIFO, history=PairingHistory(
            methods={2024: PairingMethod.FIFO}))
        (r,) = out.realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("300")

    def test_weighted_average_filed_year_then_fifo_year(self, run):
        """Average 115: year 1 gains 1 500 - 1 150 = 350; the March lot is left,
        re-priced to 10 x 115; year 2 gains 1 600 - 1 150 = 450."""
        year1 = run(YEAR1, tax_year=2024, method=PairingMethod.WEIGHTED_AVERAGE)
        (r1,) = year1.realized_gains_losses
        assert r1.gross_gain_loss_eur == Decimal("350")

        year2 = _year2(run, history=PairingHistory(
            methods={2024: PairingMethod.WEIGHTED_AVERAGE}))
        (r2,) = year2.realized_gains_losses
        assert r2.gross_gain_loss_eur == Decimal("450")
        assert r2.acquisition_date == "2024-03-11"
        assert r1.total_cost_basis_eur + r2.total_cost_basis_eur == Decimal("2300")

    def test_fifo_invariance(self, run):
        """No history, an all-FIFO history and an empty history give the same
        disposals and the same open lots."""
        trades = YEAR1 + [
            _trade(PAIRA, "2025-02-03", "5", "140", "105"),
            _trade(PAIRA, "2025-06-10", "-12", "160", "104"),
        ]
        soy = [_position(PAIRA, "10", "1300")]
        eoy = [_position(PAIRA, "3", "420")]
        outs = [
            run(trades, tax_year=2025, soy=soy, eoy=eoy, history=h)
            for h in (None, PairingHistory(),
                      PairingHistory(methods={2024: PairingMethod.FIFO}))
        ]
        views = {tuple(_rgl_view(o.realized_gains_losses)) for o in outs}
        lots = {tuple(_lots_view(o)) for o in outs}
        assert len(views) == 1 and len(lots) == 1
        # And it is the FIFO answer: 10 March @ 130 + 2 February @ 140.
        assert sum(r.gross_gain_loss_eur for r in outs[0].realized_gains_losses) \
            == Decimal("1920") - Decimal("1300") - Decimal("280")

    def test_optimal_filed_year_replays_its_plan(self, run):
        """Year 1 filed ``optimal`` sold the March lot (tx 102); the plan says
        so, and replaying it leaves the January lot."""
        history = PairingHistory(
            methods={2024: PairingMethod.OPTIMAL},
            plans={2024: {"103": (("102", Decimal("10")),)}},
        )
        (r,) = _year2(run, history=history).realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("600")
        assert r.acquisition_date == "2024-01-10"

    def test_optimal_year_without_plan_entry_is_fifo(self, run):
        history = PairingHistory(methods={2024: PairingMethod.OPTIMAL}, plans={})
        (r,) = _year2(run, history=history).realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("300")

    def test_unusable_plan_falls_back_and_is_flagged(self, run):
        """A plan naming a lot that does not exist cannot be replayed: the
        opening lots come from the fallback, dated estimated, and the symbol
        is listed for review."""
        history = PairingHistory(
            methods={2024: PairingMethod.OPTIMAL},
            plans={2024: {"103": (("999", Decimal("10")),)}},
        )
        out = _year2(run, history=history)
        (r,) = out.realized_gains_losses
        assert r.is_acquisition_estimated
        assert r.holding_period_start_estimated
        assert out.soy_unverified_symbols == ["PAIRA"]

    def test_non_fifo_history_with_fallback_lots_is_flagged(self, run):
        """The broker reports fewer shares than the replay leaves (5 vs 10):
        the surviving lots are trimmed by the broker's own FIFO rule, which
        need not match a LIFO filing, so they are marked for review."""
        out = _year2(run, history=PairingHistory(
            methods={2024: PairingMethod.LIFO}), soy_qty="5", sale_qty="5",
            soy_cost="500")
        (r,) = out.realized_gains_losses
        assert r.is_acquisition_estimated and r.holding_period_start_estimated
        assert out.soy_unverified_symbols == ["PAIRA"]

    def test_fifo_history_with_fallback_lots_is_not_flagged(self, run):
        out = _year2(run, soy_qty="5", sale_qty="5", soy_cost="500")
        (r,) = out.realized_gains_losses
        assert not r.is_acquisition_estimated
        assert out.soy_unverified_symbols == []


def test_option_ledger_ignores_the_history():
    provider = _one_to_one()
    ledger = FifoLedger(
        asset_internal_id=uuid.uuid4(), asset_category=AssetCategory.OPTION,
        asset_multiplier_from_asset=Decimal("100"),
        currency_converter=CurrencyConverter(rate_provider=provider),
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
        pairing_history=PairingHistory(methods={2024: PairingMethod.LIFO}),
    )
    assert ledger.pairing_history is None


class TestPairingHistory:

    def test_defaults_and_lookups(self):
        h = PairingHistory(
            methods={2024: PairingMethod.LIFO},
            plans={2025: {"7": (("1", Decimal("2")),)}},
        )
        assert h.method_for(2024) == PairingMethod.LIFO
        assert h.method_for(2023) == PairingMethod.FIFO
        assert h.plan_for(2025, "7") == (("1", Decimal("2")),)
        assert h.plan_for(2025, "8") is None
        assert h.plan_for(2025, None) is None

    def test_fingerprint_is_stable_and_sensitive(self):
        a = PairingHistory(methods={2025: PairingMethod.FIFO,
                                    2024: PairingMethod.LIFO})
        b = PairingHistory(methods={2024: "lifo", 2025: "fifo"})
        assert a.fingerprint() == b.fingerprint()
        c = PairingHistory(methods={2024: PairingMethod.LIFO,
                                    2025: PairingMethod.FIFO},
                           plans={2025: {"7": (("1", Decimal("2.0")),)}})
        d = PairingHistory(methods={2024: PairingMethod.LIFO,
                                    2025: PairingMethod.FIFO},
                           plans={2025: {"7": (("1", Decimal("2")),)}})
        assert c.fingerprint() != a.fingerprint()
        assert c.fingerprint() == d.fingerprint()
