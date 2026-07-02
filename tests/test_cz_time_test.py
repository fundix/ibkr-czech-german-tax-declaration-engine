# tests/test_cz_time_test.py
"""
Tests for Czech holding-period time test and taxability classification.

Covers:
1. Security disposal that passes time test → exempt, not in tax base
2. Security disposal that fails time test → taxable, in tax base
3. Disposal with missing acquisition_date → pending_manual_review
4. Dividend → no time test applied, always taxable
5. Option → no time test applied, always taxable
6. Summary aggregation distinguishing taxable / exempt / pending
7. Time test disabled via config
8. to_dict() includes taxability fields
"""
import os
import tempfile
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import List

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig
from src.countries.cz.plugin import CzechTaxAggregator, CzechTaxClassifier, CzechTaxPlugin
from src.countries.cz.tax_items import (
    CzExemptionReason,
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
)
from src.countries.cz.time_test import evaluate_time_test
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import CashFlowEvent, FinancialEvent, WithholdingTaxEvent
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver
from src.classification.asset_classifier import AssetClassifier
from src.utils.cnb_exchange_rate_provider import CNBExchangeRateProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CNB = """\
25.03.2025 #59
země|měna|množství|kód|kurz
EMU|euro|1|EUR|24,320
USA|dolar|1|USD|22,345
"""


class MockCNBProvider(CNBExchangeRateProvider):
    def __init__(self, responses=None, **kw):
        self._mock_responses = responses or {}
        if "cache_file_path" not in kw:
            kw["cache_file_path"] = os.path.join(tempfile.mkdtemp(), "m.json")
        super().__init__(**kw)

    def _fetch_rates_for_date(self, query_date):
        # Fall back to the sample rate for any date not explicitly mocked, so
        # acquisition-date conversions (cost basis) resolve like the real ČNB
        # provider would instead of spuriously failing.
        text = self._mock_responses.get(query_date) or SAMPLE_CNB
        return self._parse_cnb_text(text, query_date) if text else None


def _resolver():
    class D(AssetClassifier):
        def __init__(self): super().__init__(cache_file_path="d.json")
        def save_classifications(self): pass
    return AssetResolver(asset_classifier=D())


def _make_rgl(
    gross: Decimal,
    cat: AssetCategory = AssetCategory.STOCK,
    realization_type: RealizationType = RealizationType.LONG_POSITION_SALE,
    realization_date: str = "2025-03-25",
    acquisition_date: str = None,
    holding_days: int = 283,
    cost_basis: Decimal = Decimal("1000"),
    proceeds: Decimal = Decimal("1500"),
) -> RealizedGainLoss:
    # Keep dates consistent with holding_days — the time test decides from
    # the DATES (calendar years), holding_period_days is audit/fallback only.
    if acquisition_date is None:
        acquisition_date = (
            date.fromisoformat(realization_date) - timedelta(days=holding_days)
        ).isoformat()
    return RealizedGainLoss(
        originating_event_id=uuid.uuid4(),
        asset_internal_id=uuid.uuid4(),
        asset_category_at_realization=cat,
        acquisition_date=acquisition_date,
        realization_date=realization_date,
        realization_type=realization_type,
        quantity_realized=Decimal("10"),
        unit_cost_basis_eur=cost_basis / Decimal("10"),
        unit_realization_value_eur=proceeds / Decimal("10"),
        total_cost_basis_eur=cost_basis,
        total_realization_value_eur=proceeds,
        gross_gain_loss_eur=gross,
        holding_period_days=holding_days,
    )


def _make_rgl_no_acq_date(gross: Decimal) -> RealizedGainLoss:
    """RGL with no acquisition_date and no holding_period_days."""
    rgl = _make_rgl(gross)
    rgl.acquisition_date = ""
    rgl.holding_period_days = None
    return rgl


def _make_dividend(amount: Decimal, event_date: str = "2025-03-25") -> CashFlowEvent:
    return CashFlowEvent(
        asset_internal_id=uuid.uuid4(),
        event_date=event_date,
        event_type=FinancialEventType.DIVIDEND_CASH,
        gross_amount_foreign_currency=amount,
        local_currency="USD",
        gross_amount_eur=amount / Decimal("1.1"),
    )


# =========================================================================
# Test 1: Security disposal — passes time test → exempt
# =========================================================================

class TestSecurityExempt:
    def test_stock_held_4_years_is_exempt(self):
        """Stock held 1500 days (> 3*365=1095) → exempt."""
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2021-01-15",
            holding_period_days=1530,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.is_exempt is True
        assert it.is_taxable is False
        assert it.included_in_tax_base is False
        assert it.exemption_reason == CzExemptionReason.TIME_TEST_PASSED
        assert it.tax_review_status == CzTaxReviewStatus.RESOLVED
        assert "§4/1/w ZDP" in (it.tax_review_note or "")

    def test_exempt_item_to_dict(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2021-01-15",
            holding_period_days=1530,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        d = items[0].to_dict()

        assert d["is_exempt"] is True
        assert d["is_taxable"] is False
        assert d["included_in_tax_base"] is False
        assert d["exemption_reason"] == "TIME_TEST_PASSED"
        assert d["tax_review_status"] == "RESOLVED"


# =========================================================================
# Test 2: Security disposal — fails time test → taxable
# =========================================================================

class TestSecurityTaxable:
    def test_stock_held_200_days_is_taxable(self):
        """Stock held 200 days (< 1095) → taxable."""
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2024-09-06",
            holding_period_days=200,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.is_taxable is True
        assert it.is_exempt is False
        assert it.included_in_tax_base is True
        assert it.exemption_reason is None
        assert it.tax_review_status == CzTaxReviewStatus.RESOLVED

    def test_taxable_item_to_dict(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            holding_period_days=200,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        d = items[0].to_dict()

        assert d["is_taxable"] is True
        assert d["is_exempt"] is False
        assert d["included_in_tax_base"] is True

    def test_boundary_exactly_3_years_is_taxable(self):
        """Exactly 1095 days — NOT exempt (must EXCEED threshold)."""
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            holding_period_days=1095,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        assert items[0].is_taxable is True
        assert items[0].is_exempt is False

    def test_boundary_1096_days_is_exempt(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2022-03-24",
            holding_period_days=1096,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        assert items[0].is_exempt is True


# =========================================================================
# Test 3: Disposal with missing acquisition_date → pending
# =========================================================================

class TestMissingAcquisitionDate:
    def test_no_acq_date_no_holding_days_is_pending(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date=None,
            holding_period_days=None,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        assert it.is_taxable is True  # conservative default
        assert it.included_in_tax_base is True
        assert it.is_exempt is False
        assert "Missing acquisition_date" in (it.tax_review_note or "")

    def test_empty_acq_date_string_is_pending(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="",
            holding_period_days=None,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        assert items[0].tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW

    def test_pending_item_to_dict(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date=None,
            holding_period_days=None,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        d = items[0].to_dict()
        assert d["tax_review_status"] == "PENDING_MANUAL_REVIEW"
        assert d["included_in_tax_base"] is True


# =========================================================================
# Test 4: Dividend → no time test applied
# =========================================================================

class TestDividendNoTimeTest:
    def test_dividend_always_taxable(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.DIVIDEND,
            section=CzTaxSection.CZ_8_DIVIDENDS,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            amount_eur=Decimal("100"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.is_taxable is True
        assert it.is_exempt is False
        assert it.included_in_tax_base is True
        assert it.tax_review_status == CzTaxReviewStatus.RESOLVED
        assert it.exemption_reason is None

    def test_interest_always_taxable(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.INTEREST,
            section=CzTaxSection.CZ_8_INTEREST,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            amount_eur=Decimal("50"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        assert items[0].is_taxable is True
        assert items[0].included_in_tax_base is True


# =========================================================================
# Test 5: Option → no time test applied
# =========================================================================

class TestOptionNoTimeTest:
    def test_option_close_always_taxable(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.OPTION_CLOSE,
            section=CzTaxSection.CZ_10_OPTIONS,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            holding_period_days=2000,  # even with long holding period
            gain_loss_eur=Decimal("300"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.is_taxable is True
        assert it.is_exempt is False
        assert it.included_in_tax_base is True
        assert it.tax_review_status == CzTaxReviewStatus.RESOLVED

    def test_option_expiry_always_taxable(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.OPTION_EXPIRY_WORTHLESS,
            section=CzTaxSection.CZ_10_OPTIONS,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            holding_period_days=2000,
            gain_loss_eur=Decimal("-200"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        assert items[0].is_taxable is True
        assert items[0].is_exempt is False


# =========================================================================
# Test 6: Summary aggregation — taxable / exempt / pending
# =========================================================================

class TestSummaryAggregation:
    def test_aggregation_splits_taxable_exempt_pending(self):
        """3 security disposals: 1 taxable, 1 exempt, 1 pending."""
        resolver = _resolver()
        provider = MockCNBProvider(responses={date(2025, 3, 25): SAMPLE_CNB})
        converter = CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())
        from src.countries.cz.config import CzTaxConfig as _Cfg

        # Taxable: held 200 days
        rgl_taxable = _make_rgl(Decimal("500"), holding_days=200)
        # Exempt: held 1200 days
        rgl_exempt = _make_rgl(Decimal("800"), holding_days=1200)
        # Pending: no acquisition date
        rgl_pending = _make_rgl_no_acq_date(Decimal("300"))

        classifier = CzechTaxClassifier()
        for rgl in [rgl_taxable, rgl_exempt, rgl_pending]:
            classifier.classify(rgl)

        aggregator = CzechTaxAggregator(
            config=_Cfg(annual_exempt_limit_enabled=False),
            fx_converter=converter,
        )
        result = aggregator.aggregate(
            [rgl_taxable, rgl_exempt, rgl_pending], [], resolver, 2025,
        )

        s = result.sections["cz_10_summary"]

        # Counts
        assert s.line_items["sec_item_count_total"] == Decimal(3)
        assert s.line_items["sec_item_count_exempt"] == Decimal(1)
        assert s.line_items["sec_item_count_pending"] == Decimal(1)

        # Taxable gains should only include the 200-day item (500 EUR → CZK)
        # and the pending item (300 EUR → CZK, included as conservative default)
        taxable_czk = s.line_items["sec_taxable_gains_czk"]
        assert taxable_czk > Decimal(0)

        # Exempt total should include the 1200-day item (time test)
        exempt_tt = s.line_items["sec_exempt_time_test_czk"]
        assert exempt_tt > Decimal(0)

        # Pending total should include the no-acq-date item
        pending_czk = s.line_items["sec_pending_czk"]
        assert pending_czk > Decimal(0)

    def test_items_in_country_result_have_taxability(self):
        resolver = _resolver()
        from src.countries.cz.config import CzTaxConfig as _Cfg
        aggregator = CzechTaxAggregator(config=_Cfg(annual_exempt_limit_enabled=False))

        rgl = _make_rgl(Decimal("500"), holding_days=200)
        CzechTaxClassifier().classify(rgl)

        result = aggregator.aggregate([rgl], [], resolver, 2025)
        items = result.country_result["items"]
        assert len(items) == 1
        it = items[0]
        assert it.is_taxable is True
        assert it.tax_review_status == CzTaxReviewStatus.RESOLVED


# =========================================================================
# Test 7: Time test disabled via config
# =========================================================================

class TestTimeTestDisabled:
    def test_disabled_makes_everything_taxable(self):
        cfg = CzTaxConfig(time_test_enabled=False)
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            holding_period_days=2000,  # would be exempt if enabled
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, cfg)
        it = items[0]

        assert it.is_taxable is True
        assert it.is_exempt is False
        assert it.included_in_tax_base is True
        assert "disabled" in (it.tax_review_note or "").lower()


# =========================================================================
# Test 8: Holding period computed from dates when not preset
# =========================================================================

class TestHoldingPeriodComputation:
    def test_computed_from_dates_when_none(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2021-01-15",
            holding_period_days=None,  # not preset
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.holding_period_days is not None
        assert it.holding_period_days > 1095  # > 3 years
        assert it.is_exempt is True

    def test_custom_holding_years(self):
        cfg = CzTaxConfig(holding_test_years=5)
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            holding_period_days=1500,  # > 3y but < 5y
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, cfg)
        assert items[0].is_taxable is True  # 1500 < 5*365=1825
        assert items[0].is_exempt is False
