"""
Regression tests for the calculation-audit fixes.

Each test pins the behaviour of one confirmed defect found during the audit so
it cannot silently regress. Grouped by finding id (H1, H2, M1, M2, M3/L3, L1, M6).
"""
import uuid
from decimal import Decimal

import pytest

import src.config as global_config
from src.countries.cz import item_builder
from src.countries.cz.annual_limit import evaluate_annual_limit
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.foreign_tax_credit import evaluate_foreign_tax_credit
from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig
from src.countries.cz.tax_items import (
    CzExemptionReason,
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
    CzWhtRecord,
)
from src.countries.cz.time_test import evaluate_time_test
from src.domain.enums import AssetCategory, FinancialEventType
from src.domain.events import (
    CashFlowEvent,
    CorpActionMergerCash,
    CorporateActionEvent,
    CorpActionSplitForward,
    TradeEvent,
)
from src.engine.calculation_engine import _create_excess_dividend_event
from src.engine.fifo_manager import FifoLedger, FifoLot
from src.utils.currency_converter import CurrencyConverter
from src.utils.sorting_utils import get_event_sort_key
from src.utils.type_utils import numeric_tx_sort_key
from tests.support.mock_providers import MockECBExchangeRateProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _NoRateProvider:
    """FX provider that never has a rate — simulates a failed ČNB lookup."""

    def get_rate(self, _date, _currency):
        return None


def _make_ledger(asset_category=AssetCategory.STOCK, multiplier=None):
    provider = MockECBExchangeRateProvider(foreign_to_eur_init_value=Decimal("1.0"))
    converter = CurrencyConverter(rate_provider=provider)
    return FifoLedger(
        asset_internal_id=uuid.uuid4(),
        asset_category=asset_category,
        asset_multiplier_from_asset=multiplier,
        currency_converter=converter,
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
        tax_classifier=None,
    )


def _disposal_item(proceeds_czk, gain_loss_czk=Decimal("0")):
    return CzTaxItem(
        item_type=CzTaxItemType.SECURITY_DISPOSAL,
        section=CzTaxSection.CZ_10_SECURITIES,
        source_event_id=uuid.uuid4(),
        event_date="2025-06-15",
        acquisition_date="2025-01-01",
        proceeds_czk=proceeds_czk,
        gain_loss_czk=gain_loss_czk,
        is_taxable=True,
        included_in_tax_base=True,
    )


# ---------------------------------------------------------------------------
# H1 — failed FX conversion must NOT leak the foreign amount into a CZK field
# ---------------------------------------------------------------------------

class TestH1FxConversionFailure:
    def test_convert_failure_returns_none_not_foreign_amount(self):
        fx = CzCurrencyConverter(provider=_NoRateProvider(), policy=CzFxPolicyConfig())
        czk, rec = item_builder._convert(Decimal("1000"), "USD", "2025-06-15", fx, [])
        # The bug booked 1000 USD as 1000 "CZK". Now it must be None (flagged upstream).
        assert czk is None
        assert rec is None

    def test_no_fx_converter_keeps_original_amount(self):
        # Legitimate EUR mode (fx is None): amount is passed through unchanged.
        czk, rec = item_builder._convert(Decimal("1000"), "USD", "2025-06-15", None, [])
        assert czk == Decimal("1000")
        assert rec is None

    def test_time_test_flags_fx_failed_item_pending(self):
        item = _disposal_item(proceeds_czk=None)
        item.fx_conversion_failed = True
        evaluate_time_test([item], CzTaxConfig())
        assert item.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        assert item.included_in_tax_base is True
        assert item.is_exempt is False


# ---------------------------------------------------------------------------
# M1 — annual 100k CZK limit must not exempt EUR-denominated proceeds
# ---------------------------------------------------------------------------

class TestM1AnnualLimitUnitSafety:
    def test_no_fx_mode_does_not_exempt(self):
        # 50,000 EUR proceeds (~1.25M CZK) must NOT be treated as "<= 100,000 CZK".
        item = _disposal_item(proceeds_czk=Decimal("50000"))
        evaluate_annual_limit([item], CzTaxConfig(), has_fx=False)
        assert item.is_exempt is False
        assert item.included_in_tax_base is True

    def test_fx_mode_still_exempts_below_threshold(self):
        # With real CZK, 50,000 CZK <= 100,000 CZK is genuinely exempt.
        item = _disposal_item(proceeds_czk=Decimal("50000"))
        evaluate_annual_limit([item], CzTaxConfig(), has_fx=True)
        assert item.is_exempt is True
        assert item.exemption_reason == CzExemptionReason.ANNUAL_LIMIT_NOT_EXCEEDED


# ---------------------------------------------------------------------------
# M2 — FTC no-FX mode must not compare foreign-currency WHT against a EUR cap
# ---------------------------------------------------------------------------

class TestM2FtcNoFxCurrency:
    def _dividend(self, wht_currency):
        item = CzTaxItem(
            item_type=CzTaxItemType.DIVIDEND,
            section=CzTaxSection.CZ_8_DIVIDENDS,
            source_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            amount_eur=Decimal("1000"),
        )
        item.wht_records.append(CzWhtRecord(
            wht_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            original_amount=Decimal("100"),
            original_currency=wht_currency,
            amount_czk=None,
            source_country="US",
        ))
        return item

    def test_foreign_currency_wht_excluded_and_flagged(self):
        item = self._dividend("USD")
        summary = evaluate_foreign_tax_credit([item], CzTaxConfig(), has_fx=False)
        # USD WHT must not be summed against the EUR gross cap.
        assert summary.foreign_tax_creditable_total_czk == Decimal("0")
        assert item.ftc_record.review_status == "PENDING_MANUAL_REVIEW"

    def test_eur_wht_is_credited_in_no_fx_mode(self):
        item = self._dividend("EUR")
        summary = evaluate_foreign_tax_credit([item], CzTaxConfig(), has_fx=False)
        # gross 1000 EUR, cap 15% = 150 EUR, WHT 100 EUR -> 100 creditable.
        assert summary.foreign_tax_creditable_total_czk == Decimal("100")


# ---------------------------------------------------------------------------
# H2 — reverse splits must be applied to FIFO lots
# ---------------------------------------------------------------------------

class TestH2ReverseSplit:
    def test_adjust_lots_for_reverse_ratio(self):
        ledger = _make_ledger(AssetCategory.STOCK)
        ledger.lots.append(FifoLot(
            acquisition_date="2020-01-01",
            quantity=Decimal("1000"),
            unit_cost_basis_eur=Decimal("10"),
            total_cost_basis_eur=Decimal("10000"),
            source_transaction_id="T1",
        ))
        event = CorpActionSplitForward(
            asset_internal_id=ledger.asset_internal_id,
            event_date="2023-05-01",
            new_shares_per_old_share=Decimal("0.1"),  # 1-for-10 reverse
        )
        ledger.adjust_lots_for_split(event)
        lot = ledger.lots[0]
        assert lot.quantity == Decimal("100")
        assert lot.total_cost_basis_eur == Decimal("10000")  # total unchanged
        assert lot.unit_cost_basis_eur == Decimal("100")

    def test_factory_routes_reverse_split_with_correct_ratio(self):
        from src.classification.asset_classifier import AssetClassifier
        from src.identification.asset_resolver import AssetResolver
        from src.parsers.domain_event_factory import DomainEventFactory
        from src.parsers.raw_models import RawCorporateActionRecord

        resolver = AssetResolver(AssetClassifier())
        factory = DomainEventFactory(resolver)
        rca = RawCorporateActionRecord(
            Symbol="ABC",
            Description="ABC(US0000) REVERSE SPLIT 1 FOR 10 (ABC, ...)",
            **{"Report Date": "2023-05-01"},
            Type="RS",
            AssetClass="STK",
        )
        events = factory.create_events_from_corporate_actions([rca])
        splits = [e for e in events if isinstance(e, CorpActionSplitForward)]
        assert len(splits) == 1, f"reverse split not routed to CorpActionSplitForward: {events}"
        assert splits[0].new_shares_per_old_share == Decimal("0.1")


# ---------------------------------------------------------------------------
# L1 — option cash merger must apply the contract multiplier
# ---------------------------------------------------------------------------

class TestL1OptionCashMergerMultiplier:
    def test_multiplier_applied(self):
        ledger = _make_ledger(AssetCategory.OPTION, multiplier=Decimal("100"))
        ledger.lots.append(FifoLot(
            acquisition_date="2023-01-01",
            quantity=Decimal("1"),           # 1 contract
            unit_cost_basis_eur=Decimal("200"),
            total_cost_basis_eur=Decimal("200"),
            source_transaction_id="OPT1",
        ))
        event = CorpActionMergerCash(
            asset_internal_id=ledger.asset_internal_id,
            event_date="2023-06-01",
            cash_per_share_foreign_currency=Decimal("2.50"),
            quantity_disposed=Decimal("1"),
        )
        event.cash_per_share_eur = Decimal("2.50")  # per underlying share
        rgls = ledger.consume_all_lots_for_cash_merger(event)
        assert len(rgls) == 1
        # 1 contract * 2.50/share * 100 shares/contract = 250, not 2.50.
        assert rgls[0].total_realization_value_eur == Decimal("250")
        assert rgls[0].gross_gain_loss_eur == Decimal("50")  # 250 - 200


# ---------------------------------------------------------------------------
# M3 / L3 — same-day event ordering
# ---------------------------------------------------------------------------

class _StubAsset:
    ibkr_symbol = "ABC"
    asset_category = AssetCategory.STOCK


class _StubResolver:
    def get_asset_by_id(self, _id):
        return _StubAsset()


class TestM3L3EventOrdering:
    def test_numeric_tx_sort_key_orders_across_digit_boundary(self):
        # String sort would put "10000000001" before "9999999999"; numeric must not.
        assert numeric_tx_sort_key("9999999999") < numeric_tx_sort_key("10000000001")
        # Numeric ids sort before non-numeric / fallback markers.
        assert numeric_tx_sort_key("42") < numeric_tx_sort_key("SOY_FALLBACK")

    def test_corporate_action_sorts_before_same_day_trade(self):
        resolver = _StubResolver()
        aid = uuid.uuid4()
        ca = CorporateActionEvent(
            asset_internal_id=aid, event_date="2023-05-01",
            event_type=FinancialEventType.CORP_SPLIT_FORWARD,
            ibkr_transaction_id="100",  # higher id than the trade
        )
        trade = TradeEvent(
            asset_internal_id=aid, event_date="2023-05-01",
            quantity=Decimal("10"), price_foreign_currency=Decimal("5"),
            event_type=FinancialEventType.TRADE_SELL_LONG,
            ibkr_transaction_id="50",
        )
        # Despite the higher tx id, the corporate action must sort first (intra-day order).
        assert get_event_sort_key(ca, resolver) < get_event_sort_key(trade, resolver)

    def test_same_day_trades_ordered_numerically(self):
        resolver = _StubResolver()
        aid = uuid.uuid4()
        early = TradeEvent(
            asset_internal_id=aid, event_date="2023-05-01",
            quantity=Decimal("10"), price_foreign_currency=Decimal("5"),
            event_type=FinancialEventType.TRADE_BUY_LONG,
            ibkr_transaction_id="9999999999",
        )
        later = TradeEvent(
            asset_internal_id=aid, event_date="2023-05-01",
            quantity=Decimal("10"), price_foreign_currency=Decimal("6"),
            event_type=FinancialEventType.TRADE_BUY_LONG,
            ibkr_transaction_id="10000000001",
        )
        assert get_event_sort_key(early, resolver) < get_event_sort_key(later, resolver)


# ---------------------------------------------------------------------------
# M4 — option→stock linker must not drop a premium on a duplicate key
# ---------------------------------------------------------------------------

class TestM4OptionLinkerQueue:
    def test_two_option_events_same_key_link_to_two_trades(self):
        from src.classification.asset_classifier import AssetClassifier
        from src.domain.assets import Option, Stock
        from src.domain.events import OptionExerciseEvent
        from src.identification.asset_resolver import AssetResolver
        from src.processing.option_trade_linker import OptionTradeLinker

        resolver = AssetResolver(AssetClassifier())

        stock = Stock(ibkr_symbol="ABC", ibkr_conid="U1")
        opt1 = Option(ibkr_symbol="ABC1", underlying_ibkr_conid="U1", multiplier=Decimal("100"))
        opt2 = Option(ibkr_symbol="ABC2", underlying_ibkr_conid="U1", multiplier=Decimal("100"))
        for a in (stock, opt1, opt2):
            resolver.assets_by_internal_id[a.internal_asset_id] = a

        # Two exercises on the same underlying, same day, same resulting share qty
        # → identical lookup key. Both premiums must survive.
        oe1 = OptionExerciseEvent(asset_internal_id=opt1.internal_asset_id,
                                  event_date="2023-05-01", quantity_contracts=Decimal("1"),
                                  ibkr_transaction_id="O1")
        oe2 = OptionExerciseEvent(asset_internal_id=opt2.internal_asset_id,
                                  event_date="2023-05-01", quantity_contracts=Decimal("1"),
                                  ibkr_transaction_id="O2")

        t1 = TradeEvent(asset_internal_id=stock.internal_asset_id, event_date="2023-05-01",
                        quantity=Decimal("100"), price_foreign_currency=Decimal("10"),
                        event_type=FinancialEventType.TRADE_BUY_LONG, ibkr_transaction_id="S1")
        t2 = TradeEvent(asset_internal_id=stock.internal_asset_id, event_date="2023-05-01",
                        quantity=Decimal("100"), price_foreign_currency=Decimal("10"),
                        event_type=FinancialEventType.TRADE_BUY_LONG, ibkr_transaction_id="S2")

        linker = OptionTradeLinker(resolver)
        lookup = linker._build_option_event_lookup([oe1, oe2])
        linker.link_trades([t1, t2], lookup)

        linked_ids = {t1.related_option_event_id, t2.related_option_event_id}
        assert None not in linked_ids, "a stock trade was left unlinked (premium dropped)"
        assert linked_ids == {oe1.event_id, oe2.event_id}, "both option events must be consumed distinctly"


# ---------------------------------------------------------------------------
# M6 — excess-repayment dividend event is currency-consistent (EUR)
# ---------------------------------------------------------------------------

class TestM6ExcessDividendCurrency:
    def test_excess_dividend_labelled_eur(self):
        from src.domain.assets import Stock

        original = CashFlowEvent(
            asset_internal_id=uuid.uuid4(),
            event_date="2023-03-01",
            event_type=FinancialEventType.CAPITAL_REPAYMENT,
            local_currency="USD",
            ibkr_transaction_id="R1",
            ibkr_activity_description="RETURN OF CAPITAL",
        )
        stock = Stock(ibkr_symbol="ABC")
        event = _create_excess_dividend_event(original, Decimal("50"), stock)
        assert event.event_type == FinancialEventType.DIVIDEND_CASH
        assert event.gross_amount_eur == Decimal("50")
        # The excess is an EUR figure — it must be labelled EUR, not the original USD.
        assert event.local_currency == "EUR"
        assert event.gross_amount_foreign_currency == Decimal("50")
