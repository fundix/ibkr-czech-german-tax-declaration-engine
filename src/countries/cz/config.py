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

from src.countries.cz.currency_gains import CzCurrencyRecognition
from src.countries.cz.fx_policy import CzFxPolicyConfig
from src.engine.currency_ledger import MovementDateField

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@dataclass
class CzTaxConfig:
    """Configuration for the Czech tax plugin."""

    # --- Currency ---
    home_currency: str = "CZK"

    # --- FX policy ---
    fx_policy: CzFxPolicyConfig = field(default_factory=CzFxPolicyConfig)

    # --- Currency disposals (§10 ZDP) ---
    # Confirmed by a tax advisor on 2026-08-15 for a Czech resident individual
    # holding a private account outside business assets and keeping no books.
    #
    # The FIFO over cash is unconditional — the two readings differ only in
    # what they RECOGNISE, and both must consume the same layers, or a
    # conversion gets measured against currency that was already spent.
    currency_recognition: CzCurrencyRecognition = CzCurrencyRecognition.NARROW
    # Repaying borrowed currency realises a mirrored result whose treatment is
    # unsettled for an individual, so it is computed and reported but kept out
    # of the base. Its losses are never netted against long gains either way.
    currency_short_fx_in_tax_base: bool = False
    # Both blocking questions were answered on 2026-08-15, so the computed
    # figures now reach the tax base: losses net against gains inside the kind
    # and the result floors at zero (§10 odst. 4 and 5 — an FX loss is an
    # expense, and where a kind's expenses exceed its income the difference is
    # disregarded), with settlement as the rate and layer date.
    currency_gains_in_tax_base: bool = True
    # Occasional-income exemption on FX gains. Tested on the SUM OF POSITIVE
    # gains — not the net, not the volume converted — and losses neither lower
    # the amount tested nor earn the exemption. It is a cliff: above it the
    # whole amount is taxable.
    currency_occasional_exempt_enabled: bool = True
    currency_occasional_exempt_limit_czk: Decimal = Decimal("50000")
    # Which of IBKR's two dates orders the layers, picks the rate and decides
    # the tax year. SETTLE_DATE on the advisor's ruling: a taxpayer keeping no
    # books is on the cash basis, and a spot exchange only moves the currency
    # balances on settlement — until then there is a receivable and a payable.
    # DATE stays available as an advisory scenario; either way a conversion is
    # ONE event on ONE date.
    currency_movement_date: MovementDateField = MovementDateField.SETTLE_DATE

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

    # --- Dividend separate tax base (§16a ZDP) ---
    # Foreign dividends (§8 odst. 4 ZDP — podíly na zisku ze zdrojů
    # v zahraničí) may, at the taxpayer's ELECTION, be taxed in a SEPARATE
    # 15 % tax base (samostatný základ daně §16a) instead of the general
    # base. This is advantageous once the general base reaches the 23 %
    # bracket, because the separate base is a flat 15 % with no progression.
    # When enabled, the engine computes BOTH scenarios and reports which is
    # cheaper (the election itself stays with the taxpayer). The rate is the
    # statutory §16a odst. 2 rate (15 %), kept independent of base_tax_rate.
    dividend_separate_base_enabled: bool = True
    dividend_separate_base_rate: Decimal = Decimal("0.15")

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

    # --- §4 odst. 1 ZDP letter designations ---
    # The letters are renumbered by amendments, and the citation reaches
    # user-facing review notes (and from there the PDF/XLSX exports), so it
    # lives here instead of being hardcoded at every site. Confirmed by a tax
    # advisor on 2026-08-05 for the wording in force: the holding-period time
    # test is písm. u), the 100k proceeds limit písm. t) — NOT písm. w), which
    # this engine cited until then.
    #
    # Years before the earliest entry are deliberately absent rather than
    # assumed: the letters differed and are unverified, so citations for those
    # years render as "§4 odst. 1 ZDP" without a letter. Add a year to the
    # table once its designation is confirmed.
    paragraph_4_letters_by_year: Dict[int, Dict[str, str]] = field(
        default_factory=lambda: {2025: {"time_test": "u", "annual_limit": "t"}}
    )

    # --- Holding-period time test (§4 odst. 1 ZDP, see letters above) ---
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

    def paragraph_4_citation(self, kind: str, tax_year: Optional[int] = None) -> str:
        """Cite §4 odst. 1 ZDP with the letter valid for *tax_year*.

        *kind* is ``"time_test"`` or ``"annual_limit"``. Falls back to the
        paragraph without a letter when the year predates the earliest
        confirmed designation — an unverified letter would be worse than none,
        since the citation ends up in the preparer's review notes.
        """
        table = self.paragraph_4_letters_by_year
        known = sorted(y for y in table if kind in table[y])
        if not known:
            return "§4 odst. 1 ZDP"
        if tax_year is None:
            letter = table[known[-1]][kind]
        else:
            earlier = [y for y in known if y <= tax_year]
            if not earlier:
                return "§4 odst. 1 ZDP"
            letter = table[earlier[-1]][kind]
        return f"§4 odst. 1 písm. {letter}) ZDP"

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
        # Added 2026-08-08 after a book review found these four falling back to
        # the 15 % default while every one of their treaties caps LOWER — so the
        # credit was too generous and the Czech tax understated. Each rate is
        # the Art. 10 "all other cases" figure (the retail holder's), read from
        # the Sbírka text and independently cross-checked.
        "SE": Decimal("0.10"),  # 9/1981 Sb. — flat 10 %, no two-tier split.
                                # SE withholds 30 % domestically; the 20-point
                                # excess is reclaimable from Skatteverket.
        "CN": Decimal("0.10"),  # 65/2011 Sb.m.s. (5 % only for a company ≥25 %)
        "HK": Decimal("0.05"),  # 49/2012 Sb.m.s. — Hong Kong SAR has its OWN
                                # treaty, separate from the PRC one; flat 5 %.
        "KZ": Decimal("0.10"),  # 3/2000 Sb.m.s.
    })

    # --- CNB cache path (anchored to project root — cwd-independent) ---
    cnb_cache_file_path: str = str(_PROJECT_ROOT / "cache" / "cnb_exchange_rates.json")

    # --- Income bucket labels (for TaxResult sections) ---
    section_labels: Dict[str, str] = field(default_factory=lambda: {
        "cz_8_dividends":  "§8 ZDP – Dividendy",
        "cz_8_interest":   "§8 ZDP – Úroky",
        "cz_10_securities": "§10 ZDP – Cenné papíry",
        "cz_10_options":   "§10 ZDP – Opce a deriváty",
        "cz_10_currency":  "§10 ZDP – Konverze měn (k ručnímu posouzení)",
    })
