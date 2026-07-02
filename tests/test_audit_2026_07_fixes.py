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
