# tests/test_cz_annual_limit_and_netting.py
"""
Tests for CZ annual exempt limit (100k CZK) and §10 loss offsetting.

Covers:
1. Annual limit not exceeded → eligible items exempt
2. Annual limit exceeded → eligible items remain taxable
3. Time-test exempt item ignored by annual limit
4. Options not exempted by annual limit
5. Dividends not exempted by annual limit
6. Taxable losses offset taxable gains
7. Exempt loss does not reduce tax base
8. Pending item behavior explicit
9. Summary contains annual-limit and netting totals
10. Annual limit disabled via config
"""
import os
import tempfile
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import List

import pytest

from src.countries.cz.annual_limit import evaluate_annual_limit
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig
from src.countries.cz.loss_offsetting import CzLossOffsettingResult, compute_loss_offsetting
from src.countries.cz.plugin import CzechTaxAggregator, CzechTaxClassifier, CzechTaxPlugin
from src.countries.cz.tax_items import (
    CzExemptionReason,
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
)
from src.countries.cz.time_test import evaluate_time_test
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import CashFlowEvent, FinancialEvent
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
        text = self._mock_responses.get(query_date)
        return self._parse_cnb_text(text, query_date) if text else None


def _resolver():
    class D(AssetClassifier):
        def __init__(self): super().__init__(cache_file_path="d.json")
        def save_classifications(self): pass
    return AssetResolver(asset_classifier=D())


def _sec_item(
    proceeds_czk: Decimal,
    gain_loss_czk: Decimal,
    holding_days: int = 200,
    acquisition_date: str = None,
) -> CzTaxItem:
    """Create a SECURITY_DISPOSAL item with known CZK amounts."""
    # Keep dates consistent with holding_days — the time test decides from
    # the DATES (calendar years), holding_period_days is audit/fallback only.
    if acquisition_date is None:
        acquisition_date = (
            date.fromisoformat("2025-03-25") - timedelta(days=holding_days)
        ).isoformat()
    return CzTaxItem(
        item_type=CzTaxItemType.SECURITY_DISPOSAL,
        section=CzTaxSection.CZ_10_SECURITIES,
        source_event_id=uuid.uuid4(),
        event_date="2025-03-25",
        acquisition_date=acquisition_date,
        holding_period_days=holding_days,
        proceeds_czk=proceeds_czk,
        proceeds_eur=proceeds_czk / Decimal("24.32"),
        gain_loss_czk=gain_loss_czk,
        gain_loss_eur=gain_loss_czk / Decimal("24.32"),
    )


def _opt_item(gain_loss_czk: Decimal) -> CzTaxItem:
    """Create an OPTION_CLOSE item."""
    return CzTaxItem(
        item_type=CzTaxItemType.OPTION_CLOSE,
        section=CzTaxSection.CZ_10_OPTIONS,
        source_event_id=uuid.uuid4(),
        event_date="2025-03-25",
        gain_loss_czk=gain_loss_czk,
        gain_loss_eur=gain_loss_czk / Decimal("24.32"),
    )


def _div_item(amount_czk: Decimal) -> CzTaxItem:
    """Create a DIVIDEND item."""
    return CzTaxItem(
        item_type=CzTaxItemType.DIVIDEND,
        section=CzTaxSection.CZ_8_DIVIDENDS,
        source_event_id=uuid.uuid4(),
        event_date="2025-03-25",
        amount_czk=amount_czk,
        amount_eur=amount_czk / Decimal("24.32"),
    )


# =========================================================================
# Test 1: Annual limit not exceeded → exempt
# =========================================================================

class TestAnnualLimitNotExceeded:
    def test_small_proceeds_exempt(self):
        """Total proceeds 80k CZK < 100k threshold → items exempt."""
        cfg = CzTaxConfig(annual_exempt_limit_czk=Decimal("100000"))
        items = [
            _sec_item(proceeds_czk=Decimal("50000"), gain_loss_czk=Decimal("5000")),
            _sec_item(proceeds_czk=Decimal("30000"), gain_loss_czk=Decimal("3000")),
        ]
        # Time test first (makes them taxable, short holding)
        evaluate_time_test(items, cfg)
        assert all(it.is_taxable for it in items)

        proceeds = evaluate_annual_limit(items, cfg)

        assert proceeds == Decimal("80000")
        for it in items:
            assert it.is_exempt is True
            assert it.exempt_due_to_annual_limit is True
            assert it.exemption_reason == CzExemptionReason.ANNUAL_LIMIT_NOT_EXCEEDED
            assert it.included_in_tax_base is False
            assert it.qualifies_for_annual_limit is True

    def test_exempt_item_to_dict(self):
        cfg = CzTaxConfig()
        items = [_sec_item(proceeds_czk=Decimal("50000"), gain_loss_czk=Decimal("5000"))]
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        d = items[0].to_dict()
        assert d["exempt_due_to_annual_limit"] is True
        assert d["qualifies_for_annual_limit"] is True
        assert d["exemption_reason"] == "ANNUAL_LIMIT_NOT_EXCEEDED"


# =========================================================================
# Test 2: Annual limit exceeded → taxable
# =========================================================================

class TestAnnualLimitExceeded:
    def test_large_proceeds_remain_taxable(self):
        """Total proceeds 150k CZK > 100k → all remain taxable."""
        cfg = CzTaxConfig(annual_exempt_limit_czk=Decimal("100000"))
        items = [
            _sec_item(proceeds_czk=Decimal("100000"), gain_loss_czk=Decimal("10000")),
            _sec_item(proceeds_czk=Decimal("50000"), gain_loss_czk=Decimal("5000")),
        ]
        evaluate_time_test(items, cfg)
        proceeds = evaluate_annual_limit(items, cfg)

        assert proceeds == Decimal("150000")
        for it in items:
            assert it.is_taxable is True
            assert it.exempt_due_to_annual_limit is False
            assert it.included_in_tax_base is True
            assert it.qualifies_for_annual_limit is True

    def test_exactly_100k_is_exempt(self):
        """Exactly 100k → proceeds ≤ threshold → exempt."""
        cfg = CzTaxConfig(annual_exempt_limit_czk=Decimal("100000"))
        items = [_sec_item(proceeds_czk=Decimal("100000"), gain_loss_czk=Decimal("10000"))]
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)
        assert items[0].exempt_due_to_annual_limit is True

    def test_100001_is_not_exempt(self):
        """100001 > threshold → NOT exempt."""
        cfg = CzTaxConfig(annual_exempt_limit_czk=Decimal("100000"))
        items = [_sec_item(proceeds_czk=Decimal("100001"), gain_loss_czk=Decimal("10000"))]
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)
        assert items[0].exempt_due_to_annual_limit is False


# =========================================================================
# Test 3: Time-test exempt item ignored by annual limit
# =========================================================================

class TestTimeTestExemptIgnoredByAnnualLimit:
    def test_time_test_exempt_not_counted(self):
        """Item exempt via time test should NOT be in the annual-limit proceeds sum."""
        cfg = CzTaxConfig(annual_exempt_limit_czk=Decimal("100000"))
        items = [
            # Time-test exempt (1200 days > 1095)
            _sec_item(proceeds_czk=Decimal("200000"), gain_loss_czk=Decimal("50000"), holding_days=1200),
            # Taxable (200 days < 1095)
            _sec_item(proceeds_czk=Decimal("60000"), gain_loss_czk=Decimal("5000"), holding_days=200),
        ]
        evaluate_time_test(items, cfg)

        # First should be exempt by time test
        assert items[0].is_exempt is True

        proceeds = evaluate_annual_limit(items, cfg)

        # Only the taxable item's proceeds count: 60k < 100k → exempt by annual limit
        assert proceeds == Decimal("60000")
        assert items[0].exempt_due_to_annual_limit is False  # already exempt by time test
        assert items[1].exempt_due_to_annual_limit is True


# =========================================================================
# Test 4: Options NOT exempted by annual limit
# =========================================================================

class TestOptionsNotExemptByAnnualLimit:
    def test_option_not_eligible(self):
        cfg = CzTaxConfig()
        items = [
            _opt_item(gain_loss_czk=Decimal("5000")),
            _sec_item(proceeds_czk=Decimal("50000"), gain_loss_czk=Decimal("5000")),
        ]
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        assert items[0].qualifies_for_annual_limit is False
        assert items[0].exempt_due_to_annual_limit is False
        assert items[0].is_taxable is True
        # Security should be exempt (50k < 100k)
        assert items[1].exempt_due_to_annual_limit is True


# =========================================================================
# Test 5: Dividends NOT exempted by annual limit
# =========================================================================

class TestDividendsNotExemptByAnnualLimit:
    def test_dividend_not_eligible(self):
        cfg = CzTaxConfig()
        items = [_div_item(amount_czk=Decimal("50000"))]
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        assert items[0].qualifies_for_annual_limit is False
        assert items[0].exempt_due_to_annual_limit is False


# =========================================================================
# Test 6: Taxable losses offset taxable gains
# =========================================================================

class TestLossOffsetting:
    def test_losses_offset_gains(self):
        items = [
            _sec_item(proceeds_czk=Decimal("200000"), gain_loss_czk=Decimal("50000")),
            _sec_item(proceeds_czk=Decimal("80000"), gain_loss_czk=Decimal("-20000")),
        ]
        # Make them taxable (short holding, exceeds 100k so no annual limit)
        cfg = CzTaxConfig()
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        netting = compute_loss_offsetting(items, has_fx=True)
        netting.compute_combined()

        assert netting.securities.taxable_gains == Decimal("50000")
        assert netting.securities.taxable_losses == Decimal("20000")
        assert netting.securities.net_taxable == Decimal("30000")

    def test_options_netted_separately(self):
        items = [
            _opt_item(gain_loss_czk=Decimal("10000")),
            _opt_item(gain_loss_czk=Decimal("-3000")),
        ]
        cfg = CzTaxConfig()
        evaluate_time_test(items, cfg)

        netting = compute_loss_offsetting(items, has_fx=True)
        netting.compute_combined()

        assert netting.options.taxable_gains == Decimal("10000")
        assert netting.options.taxable_losses == Decimal("3000")
        assert netting.options.net_taxable == Decimal("7000")

    def test_combined_netting(self):
        items = [
            _sec_item(proceeds_czk=Decimal("200000"), gain_loss_czk=Decimal("30000")),
            _sec_item(proceeds_czk=Decimal("80000"), gain_loss_czk=Decimal("-10000")),
            _opt_item(gain_loss_czk=Decimal("5000")),
            _opt_item(gain_loss_czk=Decimal("-2000")),
        ]
        cfg = CzTaxConfig()
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        netting = compute_loss_offsetting(items, has_fx=True)

        assert netting.securities.net_taxable == Decimal("20000")
        assert netting.options.net_taxable == Decimal("3000")
        assert netting.combined_net_taxable == Decimal("23000")


# =========================================================================
# Test 7: Exempt loss does NOT reduce tax base
# =========================================================================

class TestExemptLossNoReduction:
    def test_exempt_loss_not_in_netting(self):
        """Loss exempt by time test must NOT appear in taxable_losses."""
        items = [
            _sec_item(proceeds_czk=Decimal("200000"), gain_loss_czk=Decimal("50000"), holding_days=200),
            _sec_item(proceeds_czk=Decimal("80000"), gain_loss_czk=Decimal("-20000"), holding_days=1200),  # exempt
        ]
        cfg = CzTaxConfig()
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        netting = compute_loss_offsetting(items, has_fx=True)
        assert netting.securities.taxable_losses == Decimal("0")
        assert netting.securities.taxable_gains == Decimal("50000")
        assert netting.securities.exempt_time_test_total == Decimal("20000")


# =========================================================================
# Test 8: Pending item behavior
# =========================================================================

class TestPendingItemBehavior:
    def test_pending_included_conservatively(self):
        """Pending items are conservatively included in taxable gains/losses."""
        items = [
            _sec_item(proceeds_czk=Decimal("200000"), gain_loss_czk=Decimal("30000"), holding_days=200),
        ]
        # Make one item pending by removing acquisition_date
        pending = _sec_item(proceeds_czk=Decimal("50000"), gain_loss_czk=Decimal("8000"))
        pending.acquisition_date = None
        pending.holding_period_days = None
        items.append(pending)

        cfg = CzTaxConfig()
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        netting = compute_loss_offsetting(items, has_fx=True)
        assert netting.securities.item_count_pending == 1
        assert netting.securities.pending_total == Decimal("8000")
        # Pending item is conservatively in taxable gains
        assert netting.securities.taxable_gains == Decimal("38000")  # 30000 + 8000


# =========================================================================
# Test 9: Summary contains annual-limit and netting totals
# =========================================================================

class TestSummaryStructure:
    def test_full_summary_via_aggregator(self):
        resolver = _resolver()
        provider = MockCNB(responses={date(2025, 3, 25): SAMPLE_CNB})
        converter = CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())

        # Small proceeds: 2000 EUR ≈ 48640 CZK < 100k
        rgl_gain = RealizedGainLoss(
            originating_event_id=uuid.uuid4(),
            asset_internal_id=uuid.uuid4(),
            asset_category_at_realization=AssetCategory.STOCK,
            acquisition_date="2024-09-06",
            realization_date="2025-03-25",
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=Decimal("10"),
            unit_cost_basis_eur=Decimal("100"),
            unit_realization_value_eur=Decimal("200"),
            total_cost_basis_eur=Decimal("1000"),
            total_realization_value_eur=Decimal("2000"),
            gross_gain_loss_eur=Decimal("1000"),
            holding_period_days=200,
        )

        classifier = CzechTaxClassifier()
        classifier.classify(rgl_gain)

        aggregator = CzechTaxAggregator(fx_converter=converter)
        result = aggregator.aggregate([rgl_gain], [], resolver, 2025)

        cr = result.country_result
        netting = cr["netting"]
        assert isinstance(netting, CzLossOffsettingResult)

        # Check summary section exists
        assert "cz_10_summary" in result.sections
        summary = result.sections["cz_10_summary"]

        # Check all expected keys
        li = summary.line_items
        assert "sec_taxable_gains_czk" in li
        assert "sec_taxable_losses_czk" in li
        assert "sec_net_taxable_czk" in li
        assert "opt_taxable_gains_czk" in li
        assert "opt_net_taxable_czk" in li
        assert "combined_net_taxable_czk" in li
        assert "annual_limit_applied" in li
        assert "annual_limit_eligible_proceeds_czk" in li
        assert "sec_exempt_annual_limit_czk" in li
        assert "sec_exempt_time_test_czk" in li

    def test_annual_limit_applied_flag_in_summary(self):
        resolver = _resolver()
        provider = MockCNB(responses={date(2025, 3, 25): SAMPLE_CNB})
        converter = CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())

        # Small proceeds: 500 EUR ≈ 12160 CZK < 100k
        rgl = RealizedGainLoss(
            originating_event_id=uuid.uuid4(),
            asset_internal_id=uuid.uuid4(),
            asset_category_at_realization=AssetCategory.STOCK,
            acquisition_date="2024-09-06",
            realization_date="2025-03-25",
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=Decimal("10"),
            unit_cost_basis_eur=Decimal("0"),
            unit_realization_value_eur=Decimal("50"),
            total_cost_basis_eur=Decimal("0"),
            total_realization_value_eur=Decimal("500"),
            gross_gain_loss_eur=Decimal("500"),
            holding_period_days=200,
        )
        CzechTaxClassifier().classify(rgl)

        aggregator = CzechTaxAggregator(fx_converter=converter)
        result = aggregator.aggregate([rgl], [], resolver, 2025)

        li = result.sections["cz_10_summary"].line_items
        assert li["annual_limit_applied"] == Decimal(1)


# =========================================================================
# Test 10: Annual limit disabled
# =========================================================================

class TestAnnualLimitDisabled:
    def test_disabled_does_not_exempt(self):
        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        items = [_sec_item(proceeds_czk=Decimal("50000"), gain_loss_czk=Decimal("5000"))]
        evaluate_time_test(items, cfg)
        evaluate_annual_limit(items, cfg)

        assert items[0].qualifies_for_annual_limit is True
        assert items[0].exempt_due_to_annual_limit is False
        assert items[0].is_taxable is True
