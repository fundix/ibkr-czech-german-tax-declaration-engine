# src/countries/cz/config.py
"""
Czech Republic country-specific configuration model.

Defines CZ-specific settings that are independent of the global
application config (file paths, precision, etc.).

PLACEHOLDER: Values here are reasonable defaults but need validation
against current Czech tax legislation before production use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional

from src.countries.cz.fx_policy import CzFxPolicyConfig

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@dataclass
class CzTaxConfig:
    """Configuration for the Czech tax plugin."""

    # --- Currency ---
    home_currency: str = "CZK"

    # --- FX policy ---
    fx_policy: CzFxPolicyConfig = field(default_factory=CzFxPolicyConfig)

    # --- Tax rates (§16 ZDP) ---
    # 15 % base rate; 23 % on the base portion above the year's threshold.
    # For IBKR income this is almost always 15 %.
    base_tax_rate: Decimal = Decimal("0.15")
    elevated_tax_rate: Decimal = Decimal("0.23")
    # Explicit override of the 23 % threshold. Leave as None to use the
    # statutory per-year value from elevated_rate_thresholds_by_year.
    elevated_rate_threshold_czk: Optional[Decimal] = None
    # Statutory thresholds: 2023 = 48× average wage; 2024+ = 36× average
    # wage (konsolidační balíček). Extend this table as new years are set.
    elevated_rate_thresholds_by_year: Dict[int, Decimal] = field(default_factory=lambda: {
        2023: Decimal("1935552"),   # 48 × 40 324
        2024: Decimal("1582812"),   # 36 × 43 967
        2025: Decimal("1676052"),   # 36 × 46 557
    })

    def elevated_rate_threshold_for_year(self, tax_year: Optional[int] = None) -> Decimal:
        """23 % threshold for *tax_year* (explicit override wins).

        Unknown years fall back to the nearest known EARLIER year (or the
        earliest known year) — extend the table when new values are set.
        """
        if self.elevated_rate_threshold_czk is not None:
            return self.elevated_rate_threshold_czk
        table = self.elevated_rate_thresholds_by_year
        if tax_year in table:
            return table[tax_year]
        known = sorted(table)
        if tax_year is None:
            return table[known[-1]]
        earlier = [y for y in known if y < tax_year]
        return table[earlier[-1]] if earlier else table[known[0]]

    # --- Holding-period time test (§4/1/w ZDP) ---
    # Securities acquired after 2014-01-01: exempt if held > 3 years.
    time_test_enabled: bool = True
    holding_test_years: int = 3
    # Securities acquired BEFORE 2014-01-01 keep the pre-2014 exemption
    # regime (přechodné ustanovení čl. II bod 5 zákonného opatření Senátu
    # č. 344/2013 Sb.): 6-month holding test instead of 3 years.
    # ASSUMPTION: the taxpayer's direct share in the issuer did not exceed
    # 5 % in the 24 months before the sale (the pre-2014 6-month test only
    # applied below that threshold) — typical for portfolio investors; the
    # evaluator notes this assumption on affected items.
    pre_2014_rule_enabled: bool = True
    pre_2014_holding_test_months: int = 6
    # Annual exempt limit for security disposal proceeds (2025+ amendment).
    # If total gross disposal proceeds (proceeds_czk) for eligible items
    # do not exceed this threshold, those items are exempt.
    annual_exempt_limit_enabled: bool = True
    annual_exempt_limit_czk: Decimal = Decimal("100000")
    # §4/3 ZDP (effective 2025): time-test-exempt income above this annual
    # cap loses the exemption proportionally. The engine FLAGS affected
    # items for manual review (the proportional mechanics incl. the
    # optional cost step-up are left to the preparer).
    exempt_income_cap_czk: Decimal = Decimal("40000000")
    exempt_income_cap_start_year: int = 2025

    @property
    def holding_test_days(self) -> int:
        """Threshold in days (years * 365). Item must exceed this to be exempt."""
        return self.holding_test_years * 365

    # --- Foreign tax credit / §38f ZDP (zápočet daně) ---
    foreign_tax_credit_enabled: bool = True
    # Default cap: creditable WHT cannot exceed this rate × gross income.
    # 0.15 = 15 % is the Czech base tax rate and a common treaty cap.
    default_max_credit_rate: Decimal = Decimal("0.15")
    # Per-country treaty cap overrides (ISO-2 → max rate).
    # If a country is NOT in this dict, default_max_credit_rate applies.
    #
    # Rates below are the SZDZ caps for PORTFOLIO DIVIDENDS (the "all other
    # cases" rate; participation rates for ≥10/25 % holdings are NOT
    # modelled). Verified 2026-07-03 against the treaty overview (KODAP) and
    # spot-checked against treaty texts; the Sb. number identifies the
    # treaty (all čl. 10). LIMITATION: one cap per country is applied to ALL
    # WHT of that country — interest caps differ (often 0 %); IBKR normally
    # withholds no treaty-country interest WHT, but review manually if an
    # interest WHT row appears.
    country_credit_caps: Dict[str, Decimal] = field(default_factory=lambda: {
        "US": Decimal("0.15"),  # 32/1994 Sb.
        "DE": Decimal("0.15"),  # 18/1984 Sb.
        "IE": Decimal("0.15"),  # 163/1996 Sb.
        "GB": Decimal("0.15"),  # 89/1992 Sb. (UK withholds 0 % domestically)
        "CH": Decimal("0.15"),  # 281/1996 Sb.
        "CA": Decimal("0.15"),  # 83/2002 Sb.m.s.
        "JP": Decimal("0.15"),  # 46/1979 Sb.
        "AU": Decimal("0.15"),  # 5/1996 Sb.
        # NOTE: NL withholds 15 % domestically but the treaty cap is 10 % —
        # only 10 % is creditable; the excess must be reclaimed in NL.
        "NL": Decimal("0.10"),  # 138/1974 Sb.
        "FR": Decimal("0.10"),  # 79/2005 Sb.m.s.
        "AT": Decimal("0.10"),  # 31/2007 Sb.m.s.
        "LU": Decimal("0.10"),  # 51/2014 Sb.
    })

    # --- CNB cache path (anchored to project root — cwd-independent) ---
    cnb_cache_file_path: str = str(_PROJECT_ROOT / "cache" / "cnb_exchange_rates.json")

    # --- Income bucket labels (for TaxResult sections) ---
    section_labels: Dict[str, str] = field(default_factory=lambda: {
        "cz_8_dividends":  "§8 ZDP – Dividendy",
        "cz_8_interest":   "§8 ZDP – Úroky",
        "cz_10_securities": "§10 ZDP – Cenné papíry",
        "cz_10_options":   "§10 ZDP – Opce a deriváty",
    })
