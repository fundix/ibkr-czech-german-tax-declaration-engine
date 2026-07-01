# tests/test_cz_time_test_boundaries.py
"""
Boundary and edge-case tests for the CZ time test and item builder cleanup.

Covers gaps found in the self-audit:
1. Disposal where acquisition_date > event_date (negative holding period)
2. Same-day trade (holding_period_days=0)
3. FUND_DISTRIBUTION through time test (always taxable)
4. Exempt LOSS disposal (exempt loss must NOT reduce tax base)
5. Unlinked WHT appears as standalone item (not silently dropped)
6. category_to_cz_section shared mapping correctness
"""
import os
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from typing import List

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection, category_to_cz_section
from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig
from src.countries.cz.item_builder import build_tax_items
from src.countries.cz.plugin import CzechTaxAggregator
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


class MockCNB(CNBExchangeRateProvider):
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


# =========================================================================
# 1. Negative holding period (acquisition_date > event_date)
# =========================================================================

class TestNegativeHoldingPeriod:
    def test_future_acquisition_date_is_pending(self):
        """If acquisition_date is after event_date, holding period cannot be computed → pending."""
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2026-01-01",  # AFTER event_date
            holding_period_days=None,
            gain_loss_eur=Decimal("500"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        assert it.is_taxable is True
        assert it.included_in_tax_base is True
        assert "Cannot compute" in (it.tax_review_note or "")


# =========================================================================
# 2. Same-day trade (holding_period_days=0)
# =========================================================================

class TestSameDayTrade:
    def test_zero_holding_days_is_taxable(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2025-03-25",
            holding_period_days=0,
            gain_loss_eur=Decimal("100"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.is_taxable is True
        assert it.is_exempt is False
        assert it.included_in_tax_base is True
        assert it.tax_review_status == CzTaxReviewStatus.RESOLVED


# =========================================================================
# 3. FUND_DISTRIBUTION through time test
# =========================================================================

class TestFundDistribution:
    def test_fund_distribution_always_taxable(self):
        items = [CzTaxItem(
            item_type=CzTaxItemType.FUND_DISTRIBUTION,
            section=CzTaxSection.CZ_8_DIVIDENDS,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            amount_eur=Decimal("200"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.is_taxable is True
        assert it.is_exempt is False
        assert it.included_in_tax_base is True


# =========================================================================
# 4. Exempt LOSS disposal
# =========================================================================

class TestExemptLoss:
    def test_exempt_loss_not_in_tax_base(self):
        """A loss on a security held > 3 years should be exempt
        and must NOT reduce the tax base."""
        items = [CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
            acquisition_date="2021-01-01",
            holding_period_days=1544,
            gain_loss_eur=Decimal("-500"),   # LOSS
            gain_loss_czk=Decimal("-12160"),
        )]
        evaluate_time_test(items, CzTaxConfig())
        it = items[0]

        assert it.is_exempt is True
        assert it.included_in_tax_base is False
        assert it.exemption_reason == CzExemptionReason.TIME_TEST_PASSED

    def test_exempt_loss_excluded_from_aggregation(self):
        """Exempt loss must not appear in deductible_losses in the summary."""
        resolver = _resolver()
        provider = MockCNB(responses={date(2025, 3, 25): SAMPLE_CNB})
        converter = CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())

        # Taxable gain: 500 EUR, held 200 days
        rgl_gain = RealizedGainLoss(
            originating_event_id=uuid.uuid4(),
            asset_internal_id=uuid.uuid4(),
            asset_category_at_realization=AssetCategory.STOCK,
            acquisition_date="2024-09-06",
            realization_date="2025-03-25",
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=Decimal("10"),
            unit_cost_basis_eur=Decimal("100"),
            unit_realization_value_eur=Decimal("150"),
            total_cost_basis_eur=Decimal("1000"),
            total_realization_value_eur=Decimal("1500"),
            gross_gain_loss_eur=Decimal("500"),
            holding_period_days=200,
        )

        # Exempt loss: -300 EUR, held 1200 days
        rgl_loss = RealizedGainLoss(
            originating_event_id=uuid.uuid4(),
            asset_internal_id=uuid.uuid4(),
            asset_category_at_realization=AssetCategory.STOCK,
            acquisition_date="2021-11-15",
            realization_date="2025-03-25",
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=Decimal("5"),
            unit_cost_basis_eur=Decimal("200"),
            unit_realization_value_eur=Decimal("140"),
            total_cost_basis_eur=Decimal("1000"),
            total_realization_value_eur=Decimal("700"),
            gross_gain_loss_eur=Decimal("-300"),
            holding_period_days=1200,
        )

        from src.countries.cz.plugin import CzechTaxClassifier
        classifier = CzechTaxClassifier()
        for rgl in [rgl_gain, rgl_loss]:
            classifier.classify(rgl)

        from src.countries.cz.config import CzTaxConfig as _Cfg
        aggregator = CzechTaxAggregator(
            config=_Cfg(annual_exempt_limit_enabled=False),
            fx_converter=converter,
        )
        result = aggregator.aggregate([rgl_gain, rgl_loss], [], resolver, 2025)

        s = result.sections["cz_10_summary"]
        # Exempt loss should NOT be in deductible_losses
        assert s.line_items["sec_taxable_losses_czk"] == Decimal("0.00")
        # Only the taxable gain should be in taxable_gains
        assert s.line_items["sec_taxable_gains_czk"] > Decimal("0")
        # Exempt total should reflect the loss (absolute value) under time_test
        assert s.line_items["sec_exempt_time_test_czk"] > Decimal("0")


# =========================================================================
# 5. Unlinked WHT appears as standalone item
# =========================================================================

class TestUnlinkedWht:
    def test_unlinked_wht_creates_standalone_item(self):
        """WHT without a matching dividend must appear as a standalone item."""
        resolver = _resolver()

        orphan_wht = WithholdingTaxEvent(
            asset_internal_id=uuid.uuid4(),
            event_date="2025-03-25",
            gross_amount_foreign_currency=Decimal("15"),
            local_currency="USD",
            gross_amount_eur=Decimal("13.64"),
            source_country_code="US",
        )

        items, _ = build_tax_items([], [orphan_wht], resolver, fx=None)

        assert len(items) == 1
        it = items[0]
        assert it.item_type == CzTaxItemType.OTHER
        assert len(it.wht_records) == 1
        assert "Unlinked WHT" in (it.tax_review_note or "")

    def test_unlinked_wht_amount_not_lost(self):
        """Standalone unlinked WHT item carries the correct amount."""
        resolver = _resolver()
        provider = MockCNB(responses={date(2025, 3, 25): SAMPLE_CNB})
        fx = CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())

        orphan_wht = WithholdingTaxEvent(
            asset_internal_id=uuid.uuid4(),
            event_date="2025-03-25",
            gross_amount_foreign_currency=Decimal("15"),
            local_currency="USD",
            gross_amount_eur=Decimal("13.64"),
            source_country_code="US",
        )

        items, fx_recs = build_tax_items([], [orphan_wht], resolver, fx=fx)
        it = items[0]
        assert it.amount_czk is not None
        # 15 USD * 22.345 ≈ 335 CZK
        assert abs(it.amount_czk - Decimal("335")) < Decimal("2")
        assert it.wht_records[0].amount_czk is not None


# =========================================================================
# 6. Shared category_to_cz_section mapping
# =========================================================================

class TestSharedMapping:
    def test_all_categories_mapped(self):
        assert category_to_cz_section("STOCK") == CzTaxSection.CZ_10_SECURITIES
        assert category_to_cz_section("BOND") == CzTaxSection.CZ_10_SECURITIES
        assert category_to_cz_section("INVESTMENT_FUND") == CzTaxSection.CZ_10_SECURITIES
        assert category_to_cz_section("OPTION") == CzTaxSection.CZ_10_OPTIONS
        assert category_to_cz_section("CFD") == CzTaxSection.CZ_10_OPTIONS
        assert category_to_cz_section("PRIVATE_SALE_ASSET") == CzTaxSection.CZ_10_SECURITIES

    def test_unknown_defaults_to_securities(self):
        assert category_to_cz_section("UNKNOWN_THING") == CzTaxSection.CZ_10_SECURITIES
