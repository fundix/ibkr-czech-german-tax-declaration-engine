# tests/test_cz_dividend_separate_base.py
"""
Tests for the §16a separate dividend tax base comparison.

Foreign dividends (§8 odst. 4 ZDP) may, at the taxpayer's election, be taxed
in a separate flat 15 % base (§16a) instead of the general base. The engine
computes both and recommends the cheaper:

1. Below the 23 % threshold → no saving → recommend general (tie).
2. Above the threshold, dividends only → saving = 23 %−15 % on the top slice.
3. Above the threshold, dividends stacked on domestic securities → separate
   base pulls dividends out of the 23 % bracket AND credits their WHT in full.
4. FTC is split by income category (dividend vs interest).
5. Disabled by config / EUR mode / no dividends → no comparison.
6. Line items + recommended_final_tax property.
"""
from decimal import Decimal

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.foreign_tax_credit import (
    CzCountryCreditAggregate,
    CzForeignTaxCreditSummary,
)
from src.countries.cz.loss_offsetting import CzLossOffsettingResult
from src.countries.cz.tax_liability import compute_tax_liability

ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _netting(sec_gains=ZERO, sec_losses=ZERO, opt_gains=ZERO, opt_losses=ZERO):
    n = CzLossOffsettingResult()
    n.securities.taxable_gains = sec_gains
    n.securities.taxable_losses = sec_losses
    n.options.taxable_gains = opt_gains
    n.options.taxable_losses = opt_losses
    n.compute_combined()
    return n


def _ftc_dividends(income=ZERO, creditable=ZERO, paid=None, country=None):
    """FTC summary whose entire foreign income is dividends."""
    paid = creditable if paid is None else paid
    s = CzForeignTaxCreditSummary()
    s.foreign_income_total_czk = income
    s.foreign_tax_creditable_total_czk = creditable
    s.foreign_tax_paid_total_czk = paid
    s.dividend_income_total_czk = income
    s.dividend_creditable_total_czk = creditable
    if country and income > ZERO:
        s.per_country[country] = CzCountryCreditAggregate(
            country=country,
            gross_income_czk=income,
            foreign_tax_paid_czk=paid,
            creditable_czk=creditable,
            dividend_gross_income_czk=income,
            dividend_creditable_czk=creditable,
        )
    return s


# ---------------------------------------------------------------------------
# Test 1: below threshold → no saving, recommend general
# ---------------------------------------------------------------------------

class TestBelowThreshold:
    def test_no_saving_recommends_general(self):
        cfg = CzTaxConfig()  # 2025 threshold ~1.68M, far above
        result = compute_tax_liability(
            taxable_dividends=Decimal("100000"),
            taxable_interest=ZERO,
            netting=_netting(),
            ftc_summary=_ftc_dividends(),
            config=cfg,
            tax_year=2025,
        )
        cmp = result.dividend_separate_base
        assert cmp is not None and cmp.available
        # Both bases are flat 15 % below the threshold → equal
        assert cmp.separate_base_total_tax == cmp.general_base_total_tax
        assert cmp.saving == ZERO
        assert cmp.recommended_mode == "general"
        # Headline tax unchanged
        assert result.recommended_final_tax == result.final_czech_tax_after_credit


# ---------------------------------------------------------------------------
# Test 2: above threshold, dividends only → saving on the 23 % slice
# ---------------------------------------------------------------------------

class TestAboveThresholdDividendsOnly:
    def test_saving_equals_8pct_of_top_slice(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("100000"))
        result = compute_tax_liability(
            taxable_dividends=Decimal("200000"),
            taxable_interest=ZERO,
            netting=_netting(),
            ftc_summary=_ftc_dividends(),
            config=cfg,
        )
        # General: 100000*0.15 + 100000*0.23 = 15000 + 23000 = 38000
        assert result.gross_czech_tax == Decimal("38000.00")
        assert result.final_czech_tax_after_credit == Decimal("38000")

        cmp = result.dividend_separate_base
        # Separate: no general income, dividends 200000 * 0.15 = 30000
        assert cmp.separate_general_base_net_tax == ZERO
        assert cmp.separate_dividend_gross_tax == Decimal("30000.00")
        assert cmp.separate_dividend_net_tax == Decimal("30000.00")
        assert cmp.separate_base_total_tax == Decimal("30000")
        # 8 % of the 100000 slice that had been at 23 %
        assert cmp.saving == Decimal("8000")
        assert cmp.recommended_mode == "separate"
        assert result.recommended_final_tax == Decimal("30000")


# ---------------------------------------------------------------------------
# Test 3: dividends stacked on domestic securities + full WHT credit
# ---------------------------------------------------------------------------

class TestDividendsStackedOnSecurities:
    def test_separate_base_avoids_23pct_and_credits_wht(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("100000"))
        # 100000 domestic securities + 100000 foreign dividends (15 % WHT)
        ftc = _ftc_dividends(
            income=Decimal("100000"), creditable=Decimal("15000"), country="US"
        )
        result = compute_tax_liability(
            taxable_dividends=Decimal("100000"),
            taxable_interest=ZERO,
            netting=_netting(sec_gains=Decimal("100000")),
            ftc_summary=ftc,
            config=cfg,
        )
        # General: base 200000 → 15000 + 23000 = 38000 gross.
        # czech tax on foreign = 38000 * 0.5 = 19000; credit = min(15000, 19000)
        assert result.gross_czech_tax == Decimal("38000.00")
        assert result.final_creditable_ftc == Decimal("15000.00")
        assert result.final_czech_tax_after_credit == Decimal("23000")

        cmp = result.dividend_separate_base
        # Separate general base = 100000 securities → 15000, no foreign → net 15000
        assert cmp.separate_general_base_net_tax == Decimal("15000.00")
        # Separate dividend base = 100000 * 0.15 = 15000, WHT 15000 fully credited
        assert cmp.separate_dividend_gross_tax == Decimal("15000.00")
        assert cmp.separate_dividend_ftc == Decimal("15000.00")
        assert cmp.separate_dividend_net_tax == ZERO
        assert cmp.separate_base_total_tax == Decimal("15000")
        assert cmp.saving == Decimal("8000")
        assert cmp.recommended_mode == "separate"
        # Recommendation note surfaced
        assert any("§16a" in n for n in result.limitation_notes)


# ---------------------------------------------------------------------------
# Test 4: interest stays in the general base (not eligible for §16a)
# ---------------------------------------------------------------------------

class TestInterestStaysGeneral:
    def test_interest_not_moved_to_separate_base(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("100000"))
        # dividends 150000 (foreign), interest 50000 (foreign)
        s = CzForeignTaxCreditSummary()
        s.foreign_income_total_czk = Decimal("200000")
        s.dividend_income_total_czk = Decimal("150000")
        s.interest_income_total_czk = Decimal("50000")
        result = compute_tax_liability(
            taxable_dividends=Decimal("150000"),
            taxable_interest=Decimal("50000"),
            netting=_netting(),
            ftc_summary=s,
            config=cfg,
        )
        cmp = result.dividend_separate_base
        # General base in the separate scenario = interest only (50000)
        assert cmp.separate_general_base == Decimal("50000")
        # dividend base = 150000
        assert cmp.separate_dividend_base_rounded == Decimal("150000")


# ---------------------------------------------------------------------------
# Test 5: disabled / EUR mode / no dividends → no comparison
# ---------------------------------------------------------------------------

class TestNoComparison:
    def test_disabled_by_config(self):
        cfg = CzTaxConfig(dividend_separate_base_enabled=False)
        result = compute_tax_liability(
            Decimal("200000"), ZERO, _netting(), _ftc_dividends(), cfg,
        )
        assert result.dividend_separate_base is None

    def test_eur_mode_no_comparison(self):
        cfg = CzTaxConfig()
        result = compute_tax_liability(
            Decimal("200000"), ZERO, _netting(), _ftc_dividends(), cfg,
            has_fx=False,
        )
        assert result.dividend_separate_base is None

    def test_no_dividends_no_comparison(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("100000"))
        result = compute_tax_liability(
            ZERO, ZERO, _netting(sec_gains=Decimal("200000")), _ftc_dividends(), cfg,
        )
        assert result.dividend_separate_base is None


# ---------------------------------------------------------------------------
# Test 6: line items + recommended_final_tax
# ---------------------------------------------------------------------------

class TestLineItems:
    def test_separate_base_line_items_present(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("100000"))
        result = compute_tax_liability(
            Decimal("200000"), ZERO, _netting(), _ftc_dividends(), cfg,
        )
        li = result.to_line_items("CZK")
        for key in (
            "sep_available",
            "sep_dividends_czk",
            "sep_total_tax_czk",
            "sep_saving_czk",
            "sep_recommended_separate",
        ):
            assert key in li, f"Missing key {key}"
        assert li["sep_recommended_separate"] == Decimal(1)
        assert li["sep_saving_czk"] == Decimal("8000.00")

    def test_line_items_absent_when_no_comparison(self):
        cfg = CzTaxConfig()
        result = compute_tax_liability(
            ZERO, ZERO, _netting(sec_gains=Decimal("50000")), _ftc_dividends(), cfg,
        )
        li = result.to_line_items("CZK")
        assert "sep_available" not in li
