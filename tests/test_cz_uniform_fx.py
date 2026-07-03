# tests/test_cz_uniform_fx.py
"""
Uniform FX mode ("jednotný kurz", §38/1 ZDP) — provider, converter audit
trail, golden end-to-end run, and the daily-vs-uniform comparison helper.

Golden expectations are HAND-COMPUTED (2026-07-03) from the official
uniform rates (2024: EUR 25.16 / USD 23.28 per GFŘ-D-66; 2020: EUR 26.50
per GFŘ-D-49) combined with the pinned daily ECB enrichment legs — see
``tests/support/golden_fx.py`` and the docstrings below. Under the uniform
mode the same golden dataset yields a FINAL TAX of 3,822 CZK vs 3,604 CZK
under daily rates, so the comparison must recommend the DAILY mode.
"""
import datetime
from decimal import Decimal

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.fx_mode_compare import CzFxModeComparison
from src.countries.cz.fx_policy import CzCurrencyConverter, uniform_fx_policy
from src.countries.cz.uniform_rates import CzUniformRateProvider
from src.countries.registry import get_tax_plugin
from tests.support.base import FifoTestCaseBase
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider
from tests.test_golden_e2e_cz import (
    CASH_TRANSACTIONS,
    POSITIONS_EOY,
    POSITIONS_SOY,
    TRADES,
)

TAX_YEAR = 2024
TWO = Decimal("0.01")


def q2(value) -> Decimal:
    return Decimal(value).quantize(TWO)


# ---------------------------------------------------------------------------
# Provider unit tests
# ---------------------------------------------------------------------------

class TestUniformRateProvider:
    def setup_method(self):
        self.provider = CzUniformRateProvider()

    def test_eur_2024_rate_matches_gfr_d66(self):
        rate = self.provider.get_rate(datetime.date(2024, 7, 1), "EUR")
        assert rate == Decimal("1") / Decimal("25.16")

    def test_usd_2025_rate_matches_gfr_d75(self):
        rate = self.provider.get_rate(datetime.date(2025, 3, 3), "USD")
        assert rate == Decimal("1") / Decimal("21.84")

    def test_quantity_normalisation_jpy(self):
        # Published as 100 JPY = 15.35 CZK -> 0.1535 CZK per 1 JPY
        rate = self.provider.get_rate(datetime.date(2024, 7, 1), "JPY")
        assert rate == Decimal("1") / (Decimal("15.35") / Decimal("100"))

    def test_same_rate_for_any_date_within_year(self):
        d1 = self.provider.get_rate(datetime.date(2024, 1, 2), "USD")
        d2 = self.provider.get_rate(datetime.date(2024, 12, 30), "USD")
        assert d1 == d2 == Decimal("1") / Decimal("23.28")

    def test_missing_year_returns_none(self):
        assert self.provider.get_rate(datetime.date(2019, 5, 5), "USD") is None

    def test_missing_currency_returns_none(self):
        # TRY 2024 deliberately omitted (D-65 misprint, see module docstring)
        assert self.provider.get_rate(datetime.date(2024, 5, 5), "TRY") is None

    def test_overrides_extend_official_table(self):
        provider = CzUniformRateProvider(
            rates_overrides={2019: {"USD": ("1", "22.93")}}
        )
        assert provider.get_rate(datetime.date(2019, 5, 5), "USD") == (
            Decimal("1") / Decimal("22.93")
        )

    def test_get_rate_info_anchors_to_event_date(self):
        info = self.provider.get_rate_info(datetime.date(2024, 8, 15), "USD")
        assert info is not None
        assert info[1] == datetime.date(2024, 8, 15)


class TestUniformConverterAuditTrail:
    def test_conversion_note_names_the_uniform_rate(self):
        converter = CzCurrencyConverter(
            provider=CzUniformRateProvider(), policy=uniform_fx_policy()
        )
        rec = converter.convert_to_czk(
            Decimal("100"), "USD", datetime.date(2024, 4, 15)
        )
        assert rec is not None
        assert q2(rec.converted_amount_czk) == Decimal("2328.00")
        assert rec.fx_policy == "uniform"
        assert rec.fx_source == "gfr-jednotny-kurz"
        assert rec.conversion_note and "Jednotný kurz 2024" in rec.conversion_note


# ---------------------------------------------------------------------------
# Golden E2E under the uniform mode (same dataset as test_golden_e2e_cz)
# ---------------------------------------------------------------------------

class _UniformGoldenBase(FifoTestCaseBase):
    def _run_golden(self, fx_mode: str):
        results = self._run_pipeline(
            trades_data=TRADES,
            positions_start_data=POSITIONS_SOY,
            positions_end_data=POSITIONS_EOY,
            cash_transactions_data=CASH_TRANSACTIONS,
            corporate_actions_data=None,
            custom_rate_provider=GoldenEcbProvider(),
            tax_year=TAX_YEAR,
        )
        if fx_mode == "uniform":
            cfg = CzTaxConfig(fx_policy=uniform_fx_policy())
            provider = CzUniformRateProvider()
        else:
            cfg = CzTaxConfig()
            provider = GoldenCnbProvider()
        plugin = get_tax_plugin("cz", config=cfg, fx_provider=provider)
        return plugin.get_tax_aggregator().aggregate(
            realized_gains_losses=results.realized_gains_losses,
            financial_events=results.processed_income_events,
            asset_resolver=results.asset_resolver,
            tax_year=TAX_YEAR,
        )


class TestGoldenUniformMode(_UniformGoldenBase):
    """Hand-computed uniform expectations (legs: daily-ECB EUR x uniform
    EUR/CZK of the leg's year; income directly USD x uniform USD rate):
      ALPHA: 5001/1.0849x25.16 = 115,978.58; 5999/1.1031x25.16 = 136,827.89
      OLDCO: 1001/1.1253x26.50 = 23,572.83 (2020 rate!); 1999/1.0861x25.16
      PUTX:  199/1.0773x25.16 = 4,647.58
      DIVCO: 100x23.28 = 2,328.00; WHT 15x23.28 = 349.20
      liability: base 27,824.89 -> 27,800 -> tax 4,170.00; FTC capped
      at CZ tax on foreign 348.89; FINAL 3,822 CZK
    """

    def test_disposal_items_use_uniform_rates_per_leg_year(self):
        cz_result = self._run_golden("uniform")
        items = {
            it.asset_symbol: it
            for it in cz_result.country_result["items"]
            if it.cost_basis_czk is not None or it.proceeds_czk is not None
        }

        alpha = items["ALPHA"]
        assert q2(alpha.cost_basis_czk) == Decimal("115978.58")
        assert q2(alpha.proceeds_czk) == Decimal("136827.89")
        assert q2(alpha.gain_loss_czk) == Decimal("20849.31")
        assert alpha.is_taxable

        oldco = items["OLDCO"]
        # Acquisition leg converts at the 2020 uniform rate (26.50), the
        # disposal leg at the 2024 rate (25.16) — per-leg-year policy.
        assert q2(oldco.cost_basis_czk) == Decimal("23572.83")
        assert q2(oldco.proceeds_czk) == Decimal("46307.74")
        assert not oldco.is_taxable  # time test unaffected by FX mode

        putx = items["P UNDR 20240315 95 M"]
        assert q2(putx.gain_loss_czk) == Decimal("4647.58")

    def test_income_and_liability_under_uniform_mode(self):
        cz_result = self._run_golden("uniform")

        div_line = cz_result.sections["cz_8_dividends"].line_items
        assert q2(div_line["gross_dividends_czk"]) == Decimal("2328.00")
        assert q2(div_line["wht_paid_czk"]) == Decimal("349.20")

        s10 = cz_result.sections["cz_10_summary"].line_items
        assert q2(s10["annual_limit_eligible_proceeds_czk"]) == Decimal("136827.89")
        assert Decimal(s10["annual_limit_applied"]) == 0

        liab = cz_result.sections["cz_tax_liability"].line_items
        assert q2(liab["combined_taxable_base_czk"]) == Decimal("27824.89")
        assert q2(liab["base_for_base_rate_czk"]) == Decimal("27800.00")
        assert q2(liab["gross_czech_tax_czk"]) == Decimal("4170.00")
        assert q2(liab["preliminary_ftc_czk"]) == Decimal("349.20")
        assert q2(liab["final_creditable_ftc_czk"]) == Decimal("348.89")
        assert q2(liab["final_czech_tax_after_credit_czk"]) == Decimal("3822.00")

    def test_uniform_conversion_note_present_on_items(self):
        cz_result = self._run_golden("uniform")
        noted = [
            it for it in cz_result.country_result["items"]
            if it.fx is not None and it.fx.conversion_note
            and "Jednotný kurz" in it.fx.conversion_note
        ]
        assert noted, "uniform conversions must carry the jednotný kurz audit note"


class TestFxModeComparison(_UniformGoldenBase):
    def test_daily_mode_is_cheaper_on_the_golden_dataset(self):
        comparison = CzFxModeComparison(
            daily=self._run_golden("daily"),
            uniform=self._run_golden("uniform"),
        )
        assert q2(comparison.daily_final_tax) == Decimal("3604.00")
        assert q2(comparison.uniform_final_tax) == Decimal("3822.00")
        assert comparison.cheaper_mode == "daily"

        rendered = "\n".join(comparison.render_lines())
        assert "DENNÍ" in rendered
        assert "3,604.00" in rendered and "3,822.00" in rendered

    def test_equal_results_reported_as_equal(self):
        result = self._run_golden("daily")
        comparison = CzFxModeComparison(daily=result, uniform=result)
        assert comparison.cheaper_mode == "equal"
