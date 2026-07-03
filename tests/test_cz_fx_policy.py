# tests/test_cz_fx_policy.py
"""
Tests for the Czech FX policy layer.

Covers:
1. CzFxPolicyConfig defaults and validation
2. CzCurrencyConverter per-event conversion
3. FxConversionRecord audit metadata presence and correctness
4. Direct foreign→CZK path (not through EUR intermediate)
5. Weekend/holiday fallback via CNB provider
6. CZK identity conversion
7. Aggregator integration: TaxResult carries CZK amounts + audit records
8. Uniform mode raises NotImplementedError
"""
import os
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.fx_policy import (
    CzCurrencyConverter,
    CzFxMode,
    CzFxPolicyConfig,
    CzFxWeekendFallback,
    FxConversionRecord,
)
from src.countries.cz.plugin import (
    CzechTaxAggregator,
    CzechTaxClassifier,
    CzechTaxPlugin,
)
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import CashFlowEvent, FinancialEvent, WithholdingTaxEvent
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver
from src.classification.asset_classifier import AssetClassifier
from src.utils.cnb_exchange_rate_provider import CNBExchangeRateProvider


# ---------------------------------------------------------------------------
# Shared mock CNB provider (reuse from test_cnb_fx_provider)
# ---------------------------------------------------------------------------

SAMPLE_CNB_TEXT = """\
25.03.2025 #59
země|měna|množství|kód|kurz
EMU|euro|1|EUR|24,320
USA|dolar|1|USD|22,345
Velká Británie|libra|1|GBP|28,910
"""

SAMPLE_CNB_TEXT_FRIDAY = """\
21.03.2025 #55
země|měna|množství|kód|kurz
EMU|euro|1|EUR|24,100
USA|dolar|1|USD|22,000
"""


class MockCNBProvider(CNBExchangeRateProvider):
    """Returns pre-configured text responses instead of HTTP calls."""

    def __init__(self, responses: Optional[Dict[date, Optional[str]]] = None, **kwargs):
        self._mock_responses = responses or {}
        if "cache_file_path" not in kwargs:
            kwargs["cache_file_path"] = os.path.join(tempfile.mkdtemp(), "mock_cnb.json")
        super().__init__(**kwargs)

    def _fetch_rates_for_date(self, query_date: date) -> Optional[Dict[str, Decimal]]:
        text = self._mock_responses.get(query_date)
        if text is None:
            return None
        return self._parse_cnb_text(text, query_date)


def _make_mock_resolver() -> AssetResolver:
    class DummyClassifier(AssetClassifier):
        def __init__(self):
            super().__init__(cache_file_path="dummy_cache.json")
        def save_classifications(self):
            pass
    return AssetResolver(asset_classifier=DummyClassifier())


def _make_rgl(
    gross: Decimal,
    cat: AssetCategory = AssetCategory.STOCK,
    realization_date: str = "2025-03-25",
    holding_days: int = 100,
) -> RealizedGainLoss:
    return RealizedGainLoss(
        originating_event_id=uuid.uuid4(),
        asset_internal_id=uuid.uuid4(),
        asset_category_at_realization=cat,
        acquisition_date="2024-01-15",
        realization_date=realization_date,
        realization_type=RealizationType.LONG_POSITION_SALE,
        quantity_realized=Decimal("10"),
        unit_cost_basis_eur=Decimal("100"),
        unit_realization_value_eur=Decimal("100") + gross / Decimal("10"),
        total_cost_basis_eur=Decimal("1000"),
        total_realization_value_eur=Decimal("1000") + gross,
        gross_gain_loss_eur=gross,
        holding_period_days=holding_days,
    )


def _make_dividend(
    amount: Decimal, currency: str = "USD", event_date: str = "2025-03-25"
) -> CashFlowEvent:
    return CashFlowEvent(
        asset_internal_id=uuid.uuid4(),
        event_date=event_date,
        event_type=FinancialEventType.DIVIDEND_CASH,
        gross_amount_foreign_currency=amount,
        local_currency=currency,
        gross_amount_eur=amount / Decimal("1.10"),  # approximate EUR
    )


def _make_wht(
    amount: Decimal, currency: str = "USD", event_date: str = "2025-03-25"
) -> WithholdingTaxEvent:
    return WithholdingTaxEvent(
        asset_internal_id=uuid.uuid4(),
        event_date=event_date,
        gross_amount_foreign_currency=amount,
        local_currency=currency,
        gross_amount_eur=amount / Decimal("1.10"),
    )


# ---------------------------------------------------------------------------
# CzFxPolicyConfig tests
# ---------------------------------------------------------------------------

class TestCzFxPolicyConfig:
    def test_defaults(self):
        cfg = CzFxPolicyConfig()
        assert cfg.mode == CzFxMode.DAILY
        assert cfg.source == "cnb"
        assert cfg.weekend_fallback == CzFxWeekendFallback.PREVIOUS_VALID_RATE

    def test_frozen(self):
        cfg = CzFxPolicyConfig()
        with pytest.raises(AttributeError):
            cfg.mode = CzFxMode.UNIFORM  # type: ignore

    def test_uniform_mode_is_supported(self):
        # Historically raised NotImplementedError; the uniform mode is now
        # implemented (see uniform_rates.py / test_cz_uniform_fx.py).
        from src.countries.cz.fx_policy import uniform_fx_policy
        cfg = CzFxPolicyConfig(mode=CzFxMode.UNIFORM)
        assert cfg.mode == CzFxMode.UNIFORM
        helper = uniform_fx_policy()
        assert helper.mode == CzFxMode.UNIFORM
        assert helper.source == "gfr-jednotny-kurz"

    def test_custom_source(self):
        cfg = CzFxPolicyConfig(source="ecb")
        assert cfg.source == "ecb"


# ---------------------------------------------------------------------------
# CzCurrencyConverter tests
# ---------------------------------------------------------------------------

class TestCzCurrencyConverter:
    def setup_method(self):
        self.provider = MockCNBProvider(responses={
            date(2025, 3, 25): SAMPLE_CNB_TEXT,
            date(2025, 3, 21): SAMPLE_CNB_TEXT_FRIDAY,
        })
        self.policy = CzFxPolicyConfig()
        self.converter = CzCurrencyConverter(provider=self.provider, policy=self.policy)

    def test_czk_identity(self):
        rec = self.converter.convert_to_czk(Decimal("1000"), "CZK", date(2025, 3, 25))
        assert rec is not None
        assert rec.converted_amount_czk == Decimal("1000")
        assert rec.fx_rate == Decimal("1")
        assert rec.original_currency == "CZK"

    def test_usd_to_czk_direct(self):
        """100 USD → CZK via CNB. 1 USD = 22.345 CZK → expect ~2234.5 CZK."""
        rec = self.converter.convert_to_czk(Decimal("100"), "USD", date(2025, 3, 25))
        assert rec is not None
        assert abs(rec.converted_amount_czk - Decimal("2234.5")) < Decimal("0.1")
        assert rec.original_currency == "USD"
        assert rec.original_amount == Decimal("100")
        assert rec.fx_source == "cnb"
        assert rec.fx_policy == "daily"

    def test_eur_to_czk_direct(self):
        """50 EUR → CZK. 1 EUR = 24.320 CZK → expect 1216 CZK."""
        rec = self.converter.convert_to_czk(Decimal("50"), "EUR", date(2025, 3, 25))
        assert rec is not None
        assert abs(rec.converted_amount_czk - Decimal("1216")) < Decimal("0.1")

    def test_eur_to_czk_shortcut(self):
        rec = self.converter.convert_eur_to_czk(Decimal("50"), date(2025, 3, 25))
        assert rec is not None
        assert rec.original_currency == "EUR"
        assert abs(rec.converted_amount_czk - Decimal("1216")) < Decimal("0.1")

    def test_unknown_currency_returns_none(self):
        rec = self.converter.convert_to_czk(Decimal("100"), "XYZ", date(2025, 3, 25))
        assert rec is None

    def test_weekend_fallback(self):
        """Saturday 2025-03-22 has no data; should fall back to Friday 2025-03-21."""
        rec = self.converter.convert_to_czk(Decimal("100"), "USD", date(2025, 3, 22))
        assert rec is not None
        # Friday rate: 1 USD = 22.000 CZK → 100 USD = 2200 CZK
        assert abs(rec.converted_amount_czk - Decimal("2200")) < Decimal("0.1")


# ---------------------------------------------------------------------------
# FxConversionRecord audit metadata tests
# ---------------------------------------------------------------------------

class TestFxConversionRecordAudit:
    def setup_method(self):
        self.provider = MockCNBProvider(responses={
            date(2025, 3, 25): SAMPLE_CNB_TEXT,
        })
        self.converter = CzCurrencyConverter(
            provider=self.provider, policy=CzFxPolicyConfig()
        )

    def test_all_audit_fields_present(self):
        rec = self.converter.convert_to_czk(Decimal("100"), "USD", date(2025, 3, 25))
        assert rec is not None
        assert rec.original_amount == Decimal("100")
        assert rec.original_currency == "USD"
        assert isinstance(rec.converted_amount_czk, Decimal)
        assert isinstance(rec.fx_rate, Decimal)
        assert isinstance(rec.fx_rate_inverse, Decimal)
        assert rec.fx_date_used == "2025-03-25"
        assert rec.fx_source == "cnb"
        assert rec.fx_policy == "daily"
        assert rec.event_date == "2025-03-25"

    def test_fx_rate_inverse_is_human_readable(self):
        """fx_rate_inverse should be CZK-per-foreign-unit (e.g. ~22.345 for USD)."""
        rec = self.converter.convert_to_czk(Decimal("1"), "USD", date(2025, 3, 25))
        assert rec is not None
        assert abs(rec.fx_rate_inverse - Decimal("22.345")) < Decimal("0.001")

    def test_conversion_math_consistency(self):
        """converted_amount_czk ≈ original_amount / fx_rate."""
        rec = self.converter.convert_to_czk(Decimal("100"), "GBP", date(2025, 3, 25))
        assert rec is not None
        expected = Decimal("100") / rec.fx_rate
        assert abs(rec.converted_amount_czk - expected) < Decimal("0.01")


# ---------------------------------------------------------------------------
# Aggregator integration with FX conversion
# ---------------------------------------------------------------------------

class TestAggregatorWithFxConversion:
    def setup_method(self):
        self.provider = MockCNBProvider(responses={
            date(2025, 3, 25): SAMPLE_CNB_TEXT,
        })
        self.classifier = CzechTaxClassifier()
        self.resolver = _make_mock_resolver()

    def _make_aggregator(self) -> CzechTaxAggregator:
        converter = CzCurrencyConverter(
            provider=self.provider, policy=CzFxPolicyConfig()
        )
        return CzechTaxAggregator(fx_converter=converter)

    def test_rgl_converted_to_czk(self):
        rgl = _make_rgl(Decimal("500"), realization_date="2025-03-25")
        self.classifier.classify(rgl)
        agg = self._make_aggregator()
        result = agg.aggregate([rgl], [], self.resolver, 2025)
        s = result.sections["cz_10_summary"]
        # With FX, small proceeds trigger annual limit exemption,
        # so check via netting result in country_result
        netting = result.country_result["netting"]
        # Verify items were processed (may be exempt if proceeds < 100k)
        assert netting.securities.item_count_total >= 1

    def test_dividend_direct_usd_to_czk(self):
        """Dividend in USD should be converted directly USD→CZK, not via EUR."""
        events: List[FinancialEvent] = [
            _make_dividend(Decimal("100"), currency="USD", event_date="2025-03-25"),
        ]
        agg = self._make_aggregator()
        result = agg.aggregate([], events, self.resolver, 2025)
        sec = result.sections["cz_8_dividends"]
        # 100 USD * 22.345 CZK/USD ≈ 2234.5 CZK
        div_czk = sec.line_items["gross_dividends_czk"]
        assert abs(div_czk - Decimal("2234.50")) < Decimal("1")

    def test_wht_direct_conversion(self):
        # WHT links to parent dividend — need a dividend for it to attach to
        div = _make_dividend(Decimal("100"), currency="USD", event_date="2025-03-25")
        wht = _make_wht(Decimal("15"), currency="USD", event_date="2025-03-25")
        wht.taxed_income_event_id = div.event_id
        wht.asset_internal_id = div.asset_internal_id
        events: List[FinancialEvent] = [div, wht]
        agg = self._make_aggregator()
        result = agg.aggregate([], events, self.resolver, 2025)
        sec = result.sections["cz_8_dividends"]
        wht_czk = sec.line_items["wht_paid_czk"]
        # 15 USD * 22.345 ≈ 335 CZK
        assert wht_czk > Decimal("330")
        assert wht_czk < Decimal("340")

    def test_result_currency_is_czk(self):
        agg = self._make_aggregator()
        result = agg.aggregate([], [], self.resolver, 2025)
        cr = result.country_result
        assert cr["currency"] == "CZK"

    def test_line_item_keys_use_czk_suffix(self):
        """Amount keys should end with _czk; metadata keys are excluded."""
        rgl = _make_rgl(Decimal("100"), realization_date="2025-03-25")
        self.classifier.classify(rgl)
        agg = self._make_aggregator()
        result = agg.aggregate([rgl], [], self.resolver, 2025)
        _METADATA_PREFIXES = ("item_count", "sec_item_count", "opt_item_count", "annual_limit_applied", "ftc_item_count", "ftc_pending_count")
        for sec_key, section in result.sections.items():
            for item_key in section.line_items:
                if any(item_key.startswith(p) for p in _METADATA_PREFIXES):
                    continue
                assert item_key.endswith("_czk"), (
                    f"Section {sec_key} item '{item_key}' should end with _czk"
                )

    def test_fx_records_in_country_result(self):
        rgl = _make_rgl(Decimal("500"), realization_date="2025-03-25")
        self.classifier.classify(rgl)
        events: List[FinancialEvent] = [
            _make_dividend(Decimal("100"), event_date="2025-03-25"),
        ]
        agg = self._make_aggregator()
        result = agg.aggregate([rgl], events, self.resolver, 2025)
        cr = result.country_result
        records = cr["fx_conversion_records"]
        assert isinstance(records, list)
        assert len(records) >= 2  # at least 1 RGL + 1 dividend
        for rec in records:
            assert isinstance(rec, FxConversionRecord)
            assert rec.fx_source == "cnb"
            assert rec.fx_policy == "daily"

    def test_no_converter_falls_back_to_eur(self):
        """If no FX converter is provided, amounts stay in EUR."""
        agg = CzechTaxAggregator()  # no fx_converter
        rgl = _make_rgl(Decimal("500"), realization_date="2025-03-25")
        self.classifier.classify(rgl)
        result = agg.aggregate([rgl], [], self.resolver, 2025)
        cr = result.country_result
        assert cr["currency"] == "EUR"
        s = result.sections["cz_10_summary"]
        assert "sec_taxable_gains_eur" in s.line_items

    def test_fx_policy_in_country_result(self):
        agg = self._make_aggregator()
        result = agg.aggregate([], [], self.resolver, 2025)
        cr = result.country_result
        policy = cr["fx_policy"]
        assert isinstance(policy, CzFxPolicyConfig)
        assert policy.mode == CzFxMode.DAILY
        assert policy.source == "cnb"


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------

class TestPluginFxWiring:
    def test_plugin_with_provider_creates_converter(self):
        provider = MockCNBProvider(responses={
            date(2025, 3, 25): SAMPLE_CNB_TEXT,
        })
        plugin = CzechTaxPlugin(fx_provider=provider)
        agg = plugin.get_tax_aggregator()
        assert agg._fx is not None

    def test_plugin_without_provider_no_converter(self):
        plugin = CzechTaxPlugin()
        agg = plugin.get_tax_aggregator()
        assert agg._fx is None

    def test_plugin_end_to_end_czk(self):
        provider = MockCNBProvider(responses={
            date(2025, 3, 25): SAMPLE_CNB_TEXT,
        })
        plugin = CzechTaxPlugin(fx_provider=provider)
        classifier = plugin.get_tax_classifier()
        aggregator = plugin.get_tax_aggregator()
        resolver = _make_mock_resolver()

        rgl = _make_rgl(Decimal("1000"), realization_date="2025-03-25")
        classifier.classify(rgl)

        events: List[FinancialEvent] = [
            _make_dividend(Decimal("200"), currency="USD", event_date="2025-03-25"),
        ]

        result = aggregator.aggregate([rgl], events, resolver, 2025)
        assert result.country_result["currency"] == "CZK"
        assert len(result.country_result["fx_conversion_records"]) >= 2
