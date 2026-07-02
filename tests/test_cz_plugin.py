# tests/test_cz_plugin.py
"""
Tests for the Czech Republic tax plugin skeleton.

Verifies:
1. Protocol conformance (CzechTaxPlugin satisfies TaxPlugin, etc.)
2. CzechTaxClassifier assigns correct CzTaxSection for each AssetCategory
3. Holding-period exemption logic
4. CzechTaxAggregator produces structured TaxResult with all 4 sections
5. Registry lookup for "cz" works
6. CzTaxConfig defaults are sensible
7. End-to-end: classifier → aggregator → TaxResult has correct structure
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import List

import pytest

from src.countries.base import (
    OutputRenderer,
    TaxAggregator,
    TaxClassifier,
    TaxPlugin,
    TaxResult,
)
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.fx_policy import CzFxPolicyConfig
from src.countries.cz.plugin import (
    CzechOutputRenderer,
    CzechTaxAggregator,
    CzechTaxClassifier,
    CzechTaxPlugin,
)
from src.countries.registry import available_countries, get_tax_plugin
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import CashFlowEvent, FinancialEvent, WithholdingTaxEvent
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver
from src.classification.asset_classifier import AssetClassifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rgl(
    asset_category: AssetCategory,
    gross_gain_loss: Decimal,
    holding_period_days: int = 100,
    realization_type: RealizationType = RealizationType.LONG_POSITION_SALE,
) -> RealizedGainLoss:
    """Create a minimal RealizedGainLoss for CZ classification tests."""
    # Keep dates consistent with holding_period_days — the time test decides
    # from the DATES (calendar years), the day count is audit/fallback only.
    acquisition_date = (
        date.fromisoformat("2023-06-20") - timedelta(days=holding_period_days)
    ).isoformat()
    return RealizedGainLoss(
        originating_event_id=uuid.uuid4(),
        asset_internal_id=uuid.uuid4(),
        asset_category_at_realization=asset_category,
        acquisition_date=acquisition_date,
        realization_date="2023-06-20",
        realization_type=realization_type,
        quantity_realized=Decimal("10"),
        unit_cost_basis_eur=Decimal("100"),
        unit_realization_value_eur=Decimal("100") + gross_gain_loss / Decimal("10"),
        total_cost_basis_eur=Decimal("1000"),
        total_realization_value_eur=Decimal("1000") + gross_gain_loss,
        gross_gain_loss_eur=gross_gain_loss,
        holding_period_days=holding_period_days,
    )


def _make_dividend_event(amount_eur: Decimal) -> CashFlowEvent:
    return CashFlowEvent(
        asset_internal_id=uuid.uuid4(),
        event_date="2023-06-15",
        event_type=FinancialEventType.DIVIDEND_CASH,
        gross_amount_eur=amount_eur,
        gross_amount_foreign_currency=amount_eur,
        local_currency="EUR",
    )


def _make_interest_event(amount_eur: Decimal) -> CashFlowEvent:
    return CashFlowEvent(
        asset_internal_id=uuid.uuid4(),
        event_date="2023-06-15",
        event_type=FinancialEventType.INTEREST_RECEIVED,
        gross_amount_eur=amount_eur,
        gross_amount_foreign_currency=amount_eur,
        local_currency="EUR",
    )


def _make_wht_event(amount_eur: Decimal) -> WithholdingTaxEvent:
    return WithholdingTaxEvent(
        asset_internal_id=uuid.uuid4(),
        event_date="2023-06-15",
        gross_amount_eur=amount_eur,
        gross_amount_foreign_currency=amount_eur,
        local_currency="EUR",
    )


def _make_mock_asset_resolver() -> AssetResolver:
    class DummyClassifier(AssetClassifier):
        def __init__(self):
            super().__init__(cache_file_path="dummy_cache.json")
        def save_classifications(self):
            pass
    return AssetResolver(asset_classifier=DummyClassifier())


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestCzProtocolConformance:
    def test_classifier_is_tax_classifier(self):
        assert isinstance(CzechTaxClassifier(), TaxClassifier)

    def test_aggregator_is_tax_aggregator(self):
        assert isinstance(CzechTaxAggregator(), TaxAggregator)

    def test_renderer_is_output_renderer(self):
        assert isinstance(CzechOutputRenderer(), OutputRenderer)

    def test_plugin_is_tax_plugin(self):
        assert isinstance(CzechTaxPlugin(), TaxPlugin)


# ---------------------------------------------------------------------------
# CzTaxConfig
# ---------------------------------------------------------------------------

class TestCzTaxConfig:
    def test_defaults(self):
        cfg = CzTaxConfig()
        assert cfg.home_currency == "CZK"
        assert cfg.base_tax_rate == Decimal("0.15")
        assert cfg.holding_test_years == 3
        assert cfg.fx_policy.source == "cnb"

    def test_custom_config(self):
        cfg = CzTaxConfig(holding_test_years=5, fx_policy=CzFxPolicyConfig(source="ecb"))
        assert cfg.holding_test_years == 5
        assert cfg.fx_policy.source == "ecb"

    def test_section_labels(self):
        cfg = CzTaxConfig()
        assert "cz_8_dividends" in cfg.section_labels
        assert "cz_10_securities" in cfg.section_labels
        assert "cz_10_options" in cfg.section_labels


# ---------------------------------------------------------------------------
# CzechTaxClassifier
# ---------------------------------------------------------------------------

class TestCzechTaxClassifier:
    def setup_method(self):
        self.classifier = CzechTaxClassifier()

    def test_stock_maps_to_securities(self):
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"))
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

    def test_bond_maps_to_securities(self):
        rgl = _make_rgl(AssetCategory.BOND, Decimal("200"))
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

    def test_fund_maps_to_securities(self):
        rgl = _make_rgl(AssetCategory.INVESTMENT_FUND, Decimal("300"))
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

    def test_option_maps_to_options(self):
        rgl = _make_rgl(AssetCategory.OPTION, Decimal("1000"))
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_OPTIONS

    def test_cfd_maps_to_options(self):
        rgl = _make_rgl(AssetCategory.CFD, Decimal("50"))
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_OPTIONS

    def test_no_teilfreistellung_for_cz(self):
        """CZ has no partial exemption — net should equal gross."""
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"))
        self.classifier.classify(rgl)
        assert rgl.net_gain_loss_after_teilfreistellung_eur == Decimal("500")
        assert rgl.teilfreistellung_rate_applied is None

    def test_holding_test_exempt_3y(self):
        """Classifier no longer pre-exempts — always CZ_10_SECURITIES.
        Time test is applied later by evaluate_time_test() on CzTaxItem."""
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"), holding_period_days=1200)
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

    def test_holding_test_not_exempt_short(self):
        """Securities held < 3 years should be taxable."""
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"), holding_period_days=100)
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

    def test_holding_test_boundary(self):
        """Exactly 3 years (1095 days) — should NOT be exempt (> required, not >=)."""
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"), holding_period_days=1095)
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

    def test_holding_test_one_day_over(self):
        """Classifier always assigns CZ_10_SECURITIES regardless of holding period."""
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"), holding_period_days=1096)
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

    def test_options_no_holding_test(self):
        """Options are not subject to the holding-period test."""
        rgl = _make_rgl(AssetCategory.OPTION, Decimal("500"), holding_period_days=2000)
        self.classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_OPTIONS

    def test_custom_holding_test_years(self):
        """Classifier always assigns CZ_10_SECURITIES — time test is on CzTaxItem."""
        cfg = CzTaxConfig(holding_test_years=5)
        classifier = CzechTaxClassifier(config=cfg)
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"), holding_period_days=1500)
        classifier.classify(rgl)
        assert rgl.cz_tax_section == CzTaxSection.CZ_10_SECURITIES

        rgl2 = _make_rgl(AssetCategory.STOCK, Decimal("500"), holding_period_days=1900)
        classifier.classify(rgl2)
        assert rgl2.cz_tax_section == CzTaxSection.CZ_10_SECURITIES


# ---------------------------------------------------------------------------
# CzechTaxAggregator
# ---------------------------------------------------------------------------

class TestCzechTaxAggregator:
    def setup_method(self):
        # Disable annual limit so these tests focus on bucketing, not limit logic
        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        self.aggregator = CzechTaxAggregator(config=cfg)
        self.classifier = CzechTaxClassifier(config=cfg)
        self.resolver = _make_mock_asset_resolver()

    def test_empty_input_returns_all_sections(self):
        result = self.aggregator.aggregate([], [], self.resolver, 2023)
        assert result.country_code == "cz"
        assert result.tax_year == 2023
        assert "cz_8_dividends" in result.sections
        assert "cz_8_interest" in result.sections
        assert "cz_10_summary" in result.sections

    def test_stock_gains_appear_in_summary(self):
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"))
        self.classifier.classify(rgl)
        result = self.aggregator.aggregate([rgl], [], self.resolver, 2023)
        s = result.sections["cz_10_summary"]
        assert s.line_items["sec_taxable_gains_eur"] == Decimal("500.00")

    def test_option_gains_appear_in_summary(self):
        rgl = _make_rgl(AssetCategory.OPTION, Decimal("300"))
        self.classifier.classify(rgl)
        result = self.aggregator.aggregate([rgl], [], self.resolver, 2023)
        s = result.sections["cz_10_summary"]
        assert s.line_items["opt_taxable_gains_eur"] == Decimal("300.00")

    def test_exempt_securities_not_in_taxable(self):
        rgl = _make_rgl(AssetCategory.STOCK, Decimal("500"), holding_period_days=1200)
        self.classifier.classify(rgl)
        result = self.aggregator.aggregate([rgl], [], self.resolver, 2023)
        s = result.sections["cz_10_summary"]
        assert s.line_items["sec_taxable_gains_eur"] == Decimal("0.00")

    def test_dividends_from_events(self):
        events: List[FinancialEvent] = [
            _make_dividend_event(Decimal("100")),
            _make_dividend_event(Decimal("200")),
        ]
        result = self.aggregator.aggregate([], events, self.resolver, 2023)
        sec = result.sections["cz_8_dividends"]
        assert sec.line_items["gross_dividends_eur"] == Decimal("300.00")

    def test_interest_from_events(self):
        events: List[FinancialEvent] = [
            _make_interest_event(Decimal("50")),
        ]
        result = self.aggregator.aggregate([], events, self.resolver, 2023)
        sec = result.sections["cz_8_interest"]
        assert sec.line_items["gross_interest_eur"] == Decimal("50.00")

    def test_wht_from_events(self):
        # WHT links to parent dividend — need a dividend for it to attach to
        div = _make_dividend_event(Decimal("200"))
        wht1 = _make_wht_event(Decimal("15"))
        wht1.taxed_income_event_id = div.event_id
        wht1.asset_internal_id = div.asset_internal_id
        wht2 = _make_wht_event(Decimal("30"))
        wht2.taxed_income_event_id = div.event_id
        wht2.asset_internal_id = div.asset_internal_id
        events: List[FinancialEvent] = [div, wht1, wht2]
        result = self.aggregator.aggregate([], events, self.resolver, 2023)
        sec = result.sections["cz_8_dividends"]
        assert sec.line_items["wht_paid_eur"] == Decimal("45.00")

    def test_placeholder_notes_present(self):
        """Without FX converter, sections should have PLACEHOLDER notes."""
        result = self.aggregator.aggregate([], [], self.resolver, 2023)
        # Only sections with EUR fallback have PLACEHOLDER notes
        for key in ("cz_8_dividends", "cz_8_interest"):
            section = result.sections[key]
            assert any("PLACEHOLDER" in n or "no FX converter" in n for n in section.notes), (
                f"Section {key} should have a note about missing FX converter"
            )

    def test_mixed_gains_and_losses(self):
        rgls = [
            _make_rgl(AssetCategory.STOCK, Decimal("500")),
            _make_rgl(AssetCategory.STOCK, Decimal("-200")),
        ]
        for rgl in rgls:
            self.classifier.classify(rgl)
        result = self.aggregator.aggregate(rgls, [], self.resolver, 2023)
        s = result.sections["cz_10_summary"]
        assert s.line_items["sec_taxable_gains_eur"] == Decimal("500.00")
        assert s.line_items["sec_taxable_losses_eur"] == Decimal("200.00")
        assert s.line_items["sec_net_taxable_eur"] == Decimal("300.00")


# ---------------------------------------------------------------------------
# CzechTaxPlugin
# ---------------------------------------------------------------------------

class TestCzechTaxPlugin:
    def test_country_code(self):
        assert CzechTaxPlugin().country_code == "cz"

    def test_get_tax_classifier(self):
        plugin = CzechTaxPlugin()
        assert isinstance(plugin.get_tax_classifier(), CzechTaxClassifier)

    def test_get_tax_aggregator(self):
        plugin = CzechTaxPlugin()
        assert isinstance(plugin.get_tax_aggregator(), CzechTaxAggregator)

    def test_get_output_renderer(self):
        plugin = CzechTaxPlugin()
        assert isinstance(plugin.get_output_renderer(), CzechOutputRenderer)

    def test_custom_config_propagates(self):
        cfg = CzTaxConfig(holding_test_years=5)
        plugin = CzechTaxPlugin(config=cfg)
        assert plugin.config.holding_test_years == 5
        classifier = plugin.get_tax_classifier()
        assert classifier.config.holding_test_years == 5

    def test_ignores_unknown_kwargs(self):
        """Registry may pass DE-specific kwargs; CZ plugin should ignore them."""
        plugin = CzechTaxPlugin(vorabpauschale_items=[], eoy_mismatch_count=0)
        assert plugin.country_code == "cz"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestCzRegistry:
    def test_cz_in_available_countries(self):
        assert "cz" in available_countries()

    def test_get_cz_plugin(self):
        plugin = get_tax_plugin("cz")
        assert isinstance(plugin, CzechTaxPlugin)
        assert plugin.country_code == "cz"

    def test_get_cz_plugin_uppercase(self):
        plugin = get_tax_plugin("CZ")
        assert isinstance(plugin, CzechTaxPlugin)


# ---------------------------------------------------------------------------
# End-to-end: classifier → aggregator → structured TaxResult
# ---------------------------------------------------------------------------

class TestCzEndToEnd:
    def test_full_flow(self):
        """Simulate a small portfolio and verify the TaxResult structure."""
        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        plugin = CzechTaxPlugin(config=cfg)
        classifier = plugin.get_tax_classifier()
        aggregator = plugin.get_tax_aggregator()
        resolver = _make_mock_asset_resolver()

        rgls = [
            _make_rgl(AssetCategory.STOCK, Decimal("1000"), holding_period_days=200),
            _make_rgl(AssetCategory.STOCK, Decimal("-300"), holding_period_days=100),
            _make_rgl(AssetCategory.STOCK, Decimal("800"), holding_period_days=1200),  # exempt
            _make_rgl(AssetCategory.OPTION, Decimal("500")),
            _make_rgl(AssetCategory.OPTION, Decimal("-100")),
            _make_rgl(AssetCategory.BOND, Decimal("200")),
        ]
        for rgl in rgls:
            classifier.classify(rgl)

        events: List[FinancialEvent] = [
            _make_dividend_event(Decimal("150")),
            _make_interest_event(Decimal("25")),
            _make_wht_event(Decimal("22.50")),
        ]

        result = aggregator.aggregate(rgls, events, resolver, 2023)

        # Structure checks
        assert result.country_code == "cz"
        assert result.tax_year == 2023
        assert len(result.sections) == 5

        s = result.sections["cz_10_summary"]
        # Securities: 1000 + 200 taxable gain, 300 loss (800 exempt by time test)
        assert s.line_items["sec_taxable_gains_eur"] == Decimal("1200.00")
        assert s.line_items["sec_taxable_losses_eur"] == Decimal("300.00")
        assert s.line_items["sec_net_taxable_eur"] == Decimal("900.00")

        # Options: 500 gain, 100 loss
        assert s.line_items["opt_taxable_gains_eur"] == Decimal("500.00")
        assert s.line_items["opt_taxable_losses_eur"] == Decimal("100.00")
        assert s.line_items["opt_net_taxable_eur"] == Decimal("400.00")

        # Dividends: 150 EUR real dividend income (unlinked WHT goes to wht_paid only)
        div_sec = result.sections["cz_8_dividends"]
        assert div_sec.line_items["gross_dividends_eur"] == Decimal("150.00")

        # Interest
        intr = result.sections["cz_8_interest"]
        assert intr.line_items["gross_interest_eur"] == Decimal("25.00")
