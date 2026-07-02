"""
Regression tests for the 2026-07 calculation-audit fixes.

Each test pins the behaviour of one confirmed defect from AUDIT_REPORT_2026-07
so it cannot silently regress. Grouped by finding id (H1, H2, …).
Follows the pattern of tests/test_audit_fixes.py (previous audit round).
"""
import uuid
from decimal import Decimal

from src.countries.cz import item_builder
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig
from src.countries.cz.tax_items import (
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
)
from src.countries.cz.time_test import evaluate_time_test
from src.domain.enums import AssetCategory, RealizationType
from src.domain.results import RealizedGainLoss
from tests.test_audit_fixes import _DatedEurToCzkProvider, _NoneResolver


def _make_rgl(
    realization_type: RealizationType,
    asset_category: AssetCategory = AssetCategory.STOCK,
    acquisition_date: str = "2024-01-05",
    realization_date: str = "2024-06-05",
    cost_eur: Decimal = Decimal("800"),
    proceeds_eur: Decimal = Decimal("1000"),
) -> RealizedGainLoss:
    return RealizedGainLoss(
        originating_event_id=uuid.uuid4(),
        asset_internal_id=uuid.uuid4(),
        asset_category_at_realization=asset_category,
        acquisition_date=acquisition_date,
        realization_date=realization_date,
        realization_type=realization_type,
        quantity_realized=Decimal("100"),
        unit_cost_basis_eur=cost_eur / Decimal("100"),
        unit_realization_value_eur=proceeds_eur / Decimal("100"),
        total_cost_basis_eur=cost_eur,
        total_realization_value_eur=proceeds_eur,
        gross_gain_loss_eur=proceeds_eur - cost_eur,
    )


# Open 2024-01-05: 1 EUR = 25 CZK (rate 0.04). Cover 2024-06-05: 1 EUR = 20 CZK (rate 0.05).
_OPEN_25_COVER_20 = {
    "2024-01-05": Decimal("0.04"),
    "2024-06-05": Decimal("0.05"),
}


def _fx(rates=None) -> CzCurrencyConverter:
    return CzCurrencyConverter(
        provider=_DatedEurToCzkProvider(rates or _OPEN_25_COVER_20),
        policy=CzFxPolicyConfig(),
    )


# ---------------------------------------------------------------------------
# H1 — short positions: each leg converted at the date of its OWN cash flow
# (proceeds at the short OPENING, cost at the COVER — not the other way round)
# ---------------------------------------------------------------------------

class TestH1ShortLegsFxDates:
    def test_short_cover_proceeds_at_open_cost_at_cover(self):
        rgl = _make_rgl(RealizationType.SHORT_POSITION_COVER)
        items = item_builder._build_disposal_items([rgl], _NoneResolver(), _fx(), [])
        it = items[0]

        # Proceeds were received at the short OPENING (acquisition_date):
        # 1000 EUR @ 25 CZK = 25 000 CZK (NOT 1000 / 0.05 = 20 000).
        assert it.proceeds_czk == Decimal("25000")
        # Cost was paid at the COVER (realization_date):
        # 800 EUR @ 20 CZK = 16 000 CZK (NOT 800 / 0.04 = 20 000).
        assert it.cost_basis_czk == Decimal("16000")
        # Gain reflects the FX move of each actual cash flow: +9 000 CZK.
        assert it.gain_loss_czk == Decimal("9000")
        assert it.is_short_position is True

    def test_written_option_close_and_expiry_use_swapped_dates(self):
        for rt in (
            RealizationType.OPTION_TRADE_CLOSE_SHORT,
            RealizationType.OPTION_EXPIRED_SHORT,
        ):
            rgl = _make_rgl(rt, asset_category=AssetCategory.OPTION)
            items = item_builder._build_disposal_items([rgl], _NoneResolver(), _fx(), [])
            it = items[0]
            assert it.proceeds_czk == Decimal("25000"), rt
            assert it.cost_basis_czk == Decimal("16000"), rt
            assert it.is_short_position is True, rt

    def test_long_sale_unchanged_cost_at_acquisition(self):
        # Control: long positions keep the N1 semantics (cost @ acquisition,
        # proceeds @ disposal).
        rgl = _make_rgl(RealizationType.LONG_POSITION_SALE)
        items = item_builder._build_disposal_items([rgl], _NoneResolver(), _fx(), [])
        it = items[0]
        assert it.cost_basis_czk == Decimal("20000")   # 800 / 0.04
        assert it.proceeds_czk == Decimal("20000")     # 1000 / 0.05
        assert it.is_short_position is False


class TestH1ShortTimeTest:
    def _short_item(self) -> CzTaxItem:
        return CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-06-01",
            acquisition_date="2020-01-01",
            holding_period_days=1978,       # would pass the >1095d test
            is_short_position=True,
        )

    def test_short_position_never_time_test_exempt(self):
        item = self._short_item()
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is False
        assert item.is_taxable is True
        assert item.included_in_tax_base is True
        assert item.tax_review_status == CzTaxReviewStatus.RESOLVED
        assert "Short position" in (item.tax_review_note or "")

    def test_long_position_still_exempt(self):
        item = self._short_item()
        item.is_short_position = False
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is True
        assert item.included_in_tax_base is False


# ---------------------------------------------------------------------------
# H2 — the CZ plugin only produces CZK figures when an FX provider is wired in;
# pin the wiring building blocks used by main.py
# ---------------------------------------------------------------------------

class TestH2CnbProviderWiring:
    def test_config_defaults_point_to_cnb(self):
        cfg = CzTaxConfig()
        assert cfg.fx_policy.source == "cnb"
        assert cfg.cnb_cache_file_path

    def test_factory_builds_cnb_provider(self, tmp_path):
        from src.utils.cnb_exchange_rate_provider import CNBExchangeRateProvider
        from src.utils.fx_provider_factory import create_fx_provider

        provider = create_fx_provider(
            "cnb", cache_file_path=str(tmp_path / "cnb.json")
        )
        assert isinstance(provider, CNBExchangeRateProvider)

    def test_plugin_with_provider_gets_fx_converter(self, tmp_path):
        from src.countries.registry import get_tax_plugin
        from src.utils.fx_provider_factory import create_fx_provider

        provider = create_fx_provider(
            "cnb", cache_file_path=str(tmp_path / "cnb.json")
        )
        plugin = get_tax_plugin("cz", fx_provider=provider)
        aggregator = plugin.get_tax_aggregator()
        assert aggregator._fx is not None

    def test_plugin_without_provider_degrades_to_eur_mode(self):
        # Documents WHY main.py must pass the provider explicitly:
        # the plugin has no implicit fallback.
        from src.countries.registry import get_tax_plugin

        aggregator = get_tax_plugin("cz").get_tax_aggregator()
        assert aggregator._fx is None


# ---------------------------------------------------------------------------
# H3 — WHT refunds/reversals net against the original charge instead of
# counting as additional tax paid
# ---------------------------------------------------------------------------

class TestH3ParserWhtSign:
    def _wht_event_for_amount(self, amount: Decimal):
        from src.classification.asset_classifier import AssetClassifier
        from src.domain.events import WithholdingTaxEvent
        from src.identification.asset_resolver import AssetResolver
        from src.parsers.domain_event_factory import DomainEventFactory
        from src.parsers.raw_models import RawCashTransactionRecord

        factory = DomainEventFactory(AssetResolver(AssetClassifier()))
        rct = RawCashTransactionRecord(
            CurrencyPrimary="USD",
            Description="MSFT(US5949181045) CASH DIVIDEND - US TAX",
            SettleDate="2024-03-15",
            Type="Withholding Tax",
            Amount=amount,
            Symbol="MSFT",
            AssetClass="STK",
            TransactionID="1001",
            IssuerCountryCode="US",
            **{"ReportDate": "2024-03-15"},
        )
        events = factory.create_events_from_cash_transactions([rct])
        whts = [e for e in events if isinstance(e, WithholdingTaxEvent)]
        assert len(whts) == 1
        return whts[0]

    def test_withheld_tax_stored_positive(self):
        evt = self._wht_event_for_amount(Decimal("-10"))
        assert evt.gross_amount_foreign_currency == Decimal("10")

    def test_refund_stored_negative(self):
        # A positive IBKR WHT row is a refund/reversal — it must NOT become
        # additional tax paid.
        evt = self._wht_event_for_amount(Decimal("10"))
        assert evt.gross_amount_foreign_currency == Decimal("-10")


class TestH3RefundLinking:
    def _events(self, refund_amount=Decimal("-15"), refund_date="2024-05-20"):
        from src.domain.events import CashFlowEvent, WithholdingTaxEvent
        from src.domain.enums import FinancialEventType

        asset_id = uuid.uuid4()
        div = CashFlowEvent(
            asset_internal_id=asset_id,
            event_date="2024-03-15",
            event_type=FinancialEventType.DIVIDEND_CASH,
            gross_amount_foreign_currency=Decimal("100"),
            local_currency="USD",
            ibkr_transaction_id="1000",
            ibkr_activity_description="MSFT(US5949181045) CASH DIVIDEND",
        )
        charge = WithholdingTaxEvent(
            asset_internal_id=asset_id,
            event_date="2024-03-15",
            source_country_code="US",
            gross_amount_foreign_currency=Decimal("15"),
            local_currency="USD",
            ibkr_transaction_id="1001",
            ibkr_activity_description="MSFT(US5949181045) CASH DIVIDEND - US TAX",
        )
        refund = WithholdingTaxEvent(
            asset_internal_id=asset_id,
            event_date=refund_date,
            source_country_code="US",
            gross_amount_foreign_currency=refund_amount,
            local_currency="USD",
            ibkr_transaction_id="5000",
            ibkr_activity_description="MSFT(US5949181045) CASH DIVIDEND - US TAX (REFUND)",
        )
        return div, charge, refund

    def test_late_refund_links_to_same_income_as_prior_charge(self):
        from src.processing.withholding_tax_linker import WithholdingTaxLinker

        div, charge, refund = self._events()
        links, unlinked = WithholdingTaxLinker().link_withholding_tax_events(
            [div, charge, refund]
        )
        assert len(links) == 2
        assert unlinked == []
        assert refund.taxed_income_event_id == div.event_id
        refund_links = [l for l in links if "refund_of_prior_wht" in l.match_criteria]
        assert len(refund_links) == 1
        assert refund_links[0].linked_income_event_id == div.event_id

    def test_oversized_refund_stays_unlinked(self):
        from src.processing.withholding_tax_linker import WithholdingTaxLinker

        div, charge, refund = self._events(refund_amount=Decimal("-20"))
        links, unlinked = WithholdingTaxLinker().link_withholding_tax_events(
            [div, charge, refund]
        )
        assert len(links) == 1
        assert unlinked == [refund]


class TestH3FtcNetting:
    def _div_with_wht(self, wht_czk_amounts) -> CzTaxItem:
        from src.countries.cz.tax_items import CzWhtRecord

        item = CzTaxItem(
            item_type=CzTaxItemType.DIVIDEND,
            section=CzTaxSection.CZ_8_DIVIDENDS,
            source_event_id=uuid.uuid4(),
            event_date="2024-03-15",
            amount_czk=Decimal("2500"),
            amount_eur=Decimal("100"),
        )
        for amount in wht_czk_amounts:
            item.wht_records.append(CzWhtRecord(
                wht_event_id=uuid.uuid4(),
                event_date="2024-03-15",
                original_amount=amount / Decimal("25"),
                original_currency="USD",
                amount_czk=amount,
                source_country="US",
            ))
        return item

    def test_full_refund_nets_to_zero_credit(self):
        from src.countries.cz.foreign_tax_credit import evaluate_foreign_tax_credit

        item = self._div_with_wht([Decimal("375"), Decimal("-375")])
        summary = evaluate_foreign_tax_credit([item], CzTaxConfig(), has_fx=True)
        rec = summary.records[0]
        assert rec.foreign_tax_paid_czk == Decimal("0")
        assert rec.actual_creditable_czk == Decimal("0")

    def test_partial_refund_credits_net_amount(self):
        from src.countries.cz.foreign_tax_credit import evaluate_foreign_tax_credit

        item = self._div_with_wht([Decimal("750"), Decimal("-375")])
        summary = evaluate_foreign_tax_credit([item], CzTaxConfig(), has_fx=True)
        rec = summary.records[0]
        assert rec.foreign_tax_paid_czk == Decimal("375")
        # Cap 15 % × 2500 = 375 → the net amount is fully creditable.
        assert rec.actual_creditable_czk == Decimal("375")

    def test_negative_net_paid_never_yields_negative_credit(self):
        from src.countries.cz.foreign_tax_credit import evaluate_foreign_tax_credit

        item = self._div_with_wht([Decimal("-125")])
        summary = evaluate_foreign_tax_credit([item], CzTaxConfig(), has_fx=True)
        rec = summary.records[0]
        assert rec.actual_creditable_czk == Decimal("0")
        # Invariant paid = creditable + non_creditable still holds.
        assert rec.foreign_tax_paid_czk == rec.actual_creditable_czk + rec.non_creditable_czk


# ---------------------------------------------------------------------------
# M4 — two same-day dividends on one asset: each WHT goes to its own dividend
# (an id-only tie-break sent both WHTs to one dividend, and the per-item FTC
# cap then swallowed part of the credit)
# ---------------------------------------------------------------------------

class TestM4WhtDistribution:
    def _make_income(self, asset_id, amount, tx_id):
        from src.domain.enums import FinancialEventType
        from src.domain.events import CashFlowEvent

        return CashFlowEvent(
            asset_internal_id=asset_id,
            event_date="2024-03-15",
            event_type=FinancialEventType.DIVIDEND_CASH,
            gross_amount_foreign_currency=amount,
            local_currency="USD",
            ibkr_transaction_id=tx_id,
            ibkr_activity_description="ABC(US000) CASH DIVIDEND",
        )

    def _make_wht(self, asset_id, amount, tx_id):
        from src.domain.events import WithholdingTaxEvent

        return WithholdingTaxEvent(
            asset_internal_id=asset_id,
            event_date="2024-03-15",
            source_country_code="US",
            gross_amount_foreign_currency=amount,
            local_currency="USD",
            ibkr_transaction_id=tx_id,
            ibkr_activity_description="ABC(US000) CASH DIVIDEND - US TAX",
        )

    def test_core_linker_distributes_by_plausible_rate(self):
        from src.processing.withholding_tax_linker import WithholdingTaxLinker

        asset_id = uuid.uuid4()
        # Non-sequential tx ids → all pairings score the same confidence (80).
        div_a = self._make_income(asset_id, Decimal("100"), "5000000")
        div_b = self._make_income(asset_id, Decimal("200"), "7000000")
        wht_a = self._make_wht(asset_id, Decimal("15"), "6000000")
        wht_b = self._make_wht(asset_id, Decimal("30"), "8000000")

        links, unlinked = WithholdingTaxLinker().link_withholding_tax_events(
            [div_a, div_b, wht_a, wht_b]
        )
        assert unlinked == []
        by_wht = {l.withholding_tax_event_id: l.linked_income_event_id for l in links}
        # 15/100 and 30/200 are both exactly 15 % — the plausible pairing.
        assert by_wht[wht_a.event_id] == div_a.event_id
        assert by_wht[wht_b.event_id] == div_b.event_id

    def test_item_builder_fallback_does_not_overwrite(self):
        asset_id = uuid.uuid4()

        def _income_item(amount):
            return CzTaxItem(
                item_type=CzTaxItemType.DIVIDEND,
                section=CzTaxSection.CZ_8_DIVIDENDS,
                source_event_id=uuid.uuid4(),
                event_date="2024-03-15",
                asset_id=asset_id,
                original_amount=amount,
                original_currency="USD",
                amount_eur=amount,
            )

        item_a = _income_item(Decimal("100"))
        item_b = _income_item(Decimal("200"))
        wht_a = self._make_wht(asset_id, Decimal("15"), "6000000")
        wht_b = self._make_wht(asset_id, Decimal("30"), "8000000")

        unlinked = item_builder._link_wht(
            [item_a, item_b],
            {wht_a.event_id: wht_a, wht_b.event_id: wht_b},
            _NoneResolver(),
            None,
            [],
        )
        assert unlinked == set()
        assert [r.original_amount for r in item_a.wht_records] == [Decimal("15")]
        assert [r.original_amount for r in item_b.wht_records] == [Decimal("30")]


# ---------------------------------------------------------------------------
# M20 — a dividend reversal row keeps its negative sign even when the IBKR
# Code column (Di/In/Po) is populated
# ---------------------------------------------------------------------------

class TestM20DividendReversalSign:
    def test_reversal_with_code_di_stays_negative(self):
        from src.classification.asset_classifier import AssetClassifier
        from src.domain.enums import FinancialEventType
        from src.identification.asset_resolver import AssetResolver
        from src.parsers.domain_event_factory import DomainEventFactory
        from src.parsers.raw_models import RawCashTransactionRecord

        factory = DomainEventFactory(AssetResolver(AssetClassifier()))
        rct = RawCashTransactionRecord(
            CurrencyPrimary="USD",
            Description="ABC(US000) CASH DIVIDEND - REVERSAL",
            SettleDate="2024-03-20",
            Type="Dividends",
            Amount=Decimal("-100"),
            Symbol="ABC",
            AssetClass="STK",
            TransactionID="2001",
            Code="Di",
            IssuerCountryCode="US",
            **{"ReportDate": "2024-03-20"},
        )
        events = factory.create_events_from_cash_transactions([rct])
        divs = [e for e in events if e.event_type == FinancialEventType.DIVIDEND_CASH]
        assert len(divs) == 1
        assert divs[0].gross_amount_foreign_currency == Decimal("-100")


# ---------------------------------------------------------------------------
# M1 — time test compares CALENDAR years, not a fixed 1095-day count
# ---------------------------------------------------------------------------

class TestM1TimeTestCalendarYears:
    def _item(self, acquisition: str, event: str) -> CzTaxItem:
        return CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date=event,
            acquisition_date=acquisition,
        )

    def test_third_anniversary_across_leap_day_is_taxable(self):
        # 2021-06-01 → 2024-06-01 is 1096 days (window contains 2024-02-29),
        # but the holding period is EXACTLY 3 years — not exceeded → taxable.
        item = self._item("2021-06-01", "2024-06-01")
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is False
        assert item.is_taxable is True

    def test_day_after_third_anniversary_is_exempt(self):
        item = self._item("2021-06-01", "2024-06-02")
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is True

    def test_feb29_acquisition_anniversary_is_feb28(self):
        # Feb 29 acquisition: the 3-year period ends on 2027-02-28 (§33 DŘ) —
        # selling that day is still within the period → taxable.
        item = self._item("2024-02-29", "2027-02-28")
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is False

        item = self._item("2024-02-29", "2027-03-01")
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is True

    def test_non_leap_window_boundary_unchanged(self):
        # Control: no Feb 29 in the window — anniversary day still taxable,
        # the day after exempt (same as the old 1095-day behaviour).
        item = self._item("2021-03-01", "2024-03-01")
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is False

        item = self._item("2021-03-02", "2024-03-03")
        evaluate_time_test([item], CzTaxConfig())
        assert item.is_exempt is True


# ---------------------------------------------------------------------------
# M16 — paid Stückzinsen (accrued interest on a bond purchase) reduce §8
# interest income instead of silently vanishing
# ---------------------------------------------------------------------------

class TestM16Stueckzinsen:
    def test_paid_accrued_interest_reduces_interest_income(self):
        from src.domain.enums import FinancialEventType
        from src.domain.events import CashFlowEvent

        aid = uuid.uuid4()
        received = CashFlowEvent(
            asset_internal_id=aid,
            event_date="2024-04-01",
            event_type=FinancialEventType.INTEREST_RECEIVED,
            gross_amount_foreign_currency=Decimal("500"),
            local_currency="EUR",
            gross_amount_eur=Decimal("500"),
        )
        paid = CashFlowEvent(
            asset_internal_id=aid,
            event_date="2024-03-01",
            event_type=FinancialEventType.INTEREST_PAID_STUECKZINSEN,
            gross_amount_foreign_currency=Decimal("400"),   # stored as positive cost
            local_currency="EUR",
            gross_amount_eur=Decimal("400"),
        )
        items, _ = item_builder.build_tax_items([], [received, paid], _NoneResolver(), None)
        interest = [i for i in items if i.item_type == CzTaxItemType.INTEREST]
        assert len(interest) == 2
        amounts = sorted(i.amount_eur for i in interest)
        # Net §8 interest is 500 − 400 = 100, not 500.
        assert amounts == [Decimal("-400"), Decimal("500")]
        assert all(i.section == CzTaxSection.CZ_8_INTEREST for i in interest)


# ---------------------------------------------------------------------------
# L7 — event sort keys must not compare raw AssetCategory enums (TypeError)
# ---------------------------------------------------------------------------

class TestL7SortKeyEnums:
    def test_same_txid_different_categories_sortable(self):
        from src.domain.enums import FinancialEventType
        from src.domain.events import TradeEvent
        from src.utils.sorting_utils import get_event_sort_key

        cats = {}

        class _CatResolver:
            def get_asset_by_id(self, aid):
                class _A:
                    pass
                a = _A()
                a.asset_category = cats[aid]
                return a

        aid_stock, aid_bond = uuid.uuid4(), uuid.uuid4()
        cats[aid_stock] = AssetCategory.STOCK
        cats[aid_bond] = AssetCategory.BOND

        def _trade(aid):
            return TradeEvent(
                asset_internal_id=aid, event_date="2024-05-01",
                quantity=Decimal("10"), price_foreign_currency=Decimal("5"),
                event_type=FinancialEventType.TRADE_BUY_LONG,
                ibkr_transaction_id="100",   # identical → comparison reaches the category
            )

        resolver = _CatResolver()
        events = [_trade(aid_stock), _trade(aid_bond)]
        # Raw enums in the key tuple raised TypeError here.
        ordered = sorted(events, key=lambda e: get_event_sort_key(e, resolver))
        assert len(ordered) == 2


# ---------------------------------------------------------------------------
# M2 — 23 % threshold is a per-year statutory value (1 935 552 was 2023 only)
# L2 — statutory DAP rounding (§16/2 ZDP base, §146/1 DŘ tax)
# M21 — EUR mode must not compare a EUR base against the CZK threshold
# ---------------------------------------------------------------------------

def _liability(base_czk, tax_year=2024, has_fx=True, config=None):
    from src.countries.cz.foreign_tax_credit import CzForeignTaxCreditSummary
    from src.countries.cz.loss_offsetting import CzLossOffsettingResult
    from src.countries.cz.tax_liability import compute_tax_liability

    netting = CzLossOffsettingResult()
    netting.securities.taxable_gains = base_czk
    netting.compute_combined()
    return compute_tax_liability(
        taxable_dividends=Decimal("0"),
        taxable_interest=Decimal("0"),
        netting=netting,
        ftc_summary=CzForeignTaxCreditSummary(),
        config=config or CzTaxConfig(),
        tax_year=tax_year,
        has_fx=has_fx,
    )


class TestM2ThresholdPerYear:
    def test_statutory_values(self):
        cfg = CzTaxConfig()
        assert cfg.elevated_rate_threshold_for_year(2023) == Decimal("1935552")
        assert cfg.elevated_rate_threshold_for_year(2024) == Decimal("1582812")
        assert cfg.elevated_rate_threshold_for_year(2025) == Decimal("1676052")

    def test_unknown_year_falls_back_to_nearest_earlier(self):
        cfg = CzTaxConfig()
        assert cfg.elevated_rate_threshold_for_year(2027) == Decimal("1676052")

    def test_explicit_override_wins(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("100000"))
        assert cfg.elevated_rate_threshold_for_year(2024) == Decimal("100000")

    def test_2024_base_above_new_threshold_hits_23_percent(self):
        # Base 1 600 000 was below the old (2023) threshold of 1 935 552 —
        # for 2024 the portion above 1 582 812 must be taxed at 23 %.
        result = _liability(Decimal("1600000"), tax_year=2024)
        assert result.threshold == Decimal("1582812")
        assert result.base_for_elevated_rate == Decimal("17188")
        # 1 582 812 × 15 % + 17 188 × 23 % = 237 421.80 + 3 953.24 → ceil
        assert result.final_czech_tax_after_credit == Decimal("241376")


class TestL2DapRounding:
    def test_base_rounded_down_to_hundreds_tax_up_to_whole_czk(self):
        result = _liability(Decimal("123456.78"), tax_year=2024)
        assert result.combined_taxable_base_rounded == Decimal("123400")
        # 123 400 × 15 % = 18 510 exactly.
        assert result.final_czech_tax_after_credit == Decimal("18510")


# ---------------------------------------------------------------------------
# M3 — a disposal with failed FX conversion makes the 100k annual total
# unknowable: the remaining items must NOT be exempted
# ---------------------------------------------------------------------------

def _limit_item(proceeds_czk, fx_failed=False) -> CzTaxItem:
    item = CzTaxItem(
        item_type=CzTaxItemType.SECURITY_DISPOSAL,
        section=CzTaxSection.CZ_10_SECURITIES,
        source_event_id=uuid.uuid4(),
        event_date="2024-06-15",
        acquisition_date="2024-01-10",
        proceeds_czk=proceeds_czk,
        gain_loss_czk=Decimal("1000") if proceeds_czk is not None else None,
        is_taxable=True,
        included_in_tax_base=True,
    )
    if fx_failed:
        item.fx_conversion_failed = True
        item.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
    return item


class TestM3AnnualLimitFxFailed:
    def test_fx_failed_disposal_blocks_exemption(self):
        from src.countries.cz.annual_limit import evaluate_annual_limit

        ok = _limit_item(Decimal("60000"))
        failed = _limit_item(None, fx_failed=True)   # true proceeds unknown
        evaluate_annual_limit([ok, failed], CzTaxConfig(), has_fx=True)

        # 60 000 ≤ 100 000, but the total is incomplete → no exemption.
        assert ok.is_exempt is False
        assert ok.is_taxable is True
        assert ok.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        assert "FX conversion failed" in (ok.tax_review_note or "")

    def test_without_fx_failures_exemption_still_granted(self):
        from src.countries.cz.annual_limit import evaluate_annual_limit
        from src.countries.cz.tax_items import CzExemptionReason

        ok = _limit_item(Decimal("60000"))
        evaluate_annual_limit([ok], CzTaxConfig(), has_fx=True)
        assert ok.is_exempt is True
        assert ok.exemption_reason == CzExemptionReason.ANNUAL_LIMIT_NOT_EXCEEDED


# ---------------------------------------------------------------------------
# M14 — a PENDING loss must not reduce the tax base (a pending GAIN stays in,
# conservative in both directions)
# ---------------------------------------------------------------------------

class TestM14PendingLossExcluded:
    def _pending_item(self, gain_loss_czk) -> CzTaxItem:
        return CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2024-06-15",
            gain_loss_czk=gain_loss_czk,
            is_taxable=True,
            included_in_tax_base=True,
            tax_review_status=CzTaxReviewStatus.PENDING_MANUAL_REVIEW,
        )

    def _taxable_gain(self, gain_loss_czk) -> CzTaxItem:
        return CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2024-06-15",
            gain_loss_czk=gain_loss_czk,
            is_taxable=True,
            included_in_tax_base=True,
        )

    def test_pending_loss_does_not_reduce_base(self):
        from src.countries.cz.loss_offsetting import compute_loss_offsetting

        result = compute_loss_offsetting(
            [self._taxable_gain(Decimal("100000")), self._pending_item(Decimal("-50000"))],
            has_fx=True,
        )
        assert result.securities.taxable_losses == Decimal("0")
        assert result.securities.net_taxable == Decimal("100000")
        # The excluded amount stays visible for manual review.
        assert result.securities.pending_total == Decimal("50000")

    def test_pending_gain_stays_in_base(self):
        from src.countries.cz.loss_offsetting import compute_loss_offsetting

        result = compute_loss_offsetting(
            [self._pending_item(Decimal("50000"))],
            has_fx=True,
        )
        assert result.securities.taxable_gains == Decimal("50000")
        assert result.securities.net_taxable == Decimal("50000")


class TestM21EurModeGuards:
    def test_eur_mode_skips_threshold_and_rounding(self):
        # 2 000 000 EUR ≈ 50M CZK is far above any CZK threshold, but in EUR
        # mode the 23 % bracket must not fire (and must be flagged) instead
        # of comparing EUR against a CZK constant.
        result = _liability(Decimal("2000000"), tax_year=2024, has_fx=False)
        assert result.base_for_elevated_rate == Decimal("0")
        assert result.combined_taxable_base_rounded == Decimal("2000000")
        assert result.final_czech_tax_after_credit == Decimal("300000.00")
        assert any("no FX provider" in n for n in result.limitation_notes)
