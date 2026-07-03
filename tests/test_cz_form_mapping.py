# tests/test_cz_form_mapping.py
"""
Tests for Czech DAP-oriented form mapping layer.

Covers:
1. §8 mapping contains dividends and interest
2. §10 mapping contains securities + options
3. FTC mapping contains paid / preliminary / final / non-creditable
4. Warnings mapping includes pending/manual-review
5. Mapping does not recompute — only reads pre-computed values
6. Form mapping works when some sections are missing/empty
7. Integration: form_mapping in country_result after full pipeline
8. Line codes are stable and accessible via get_line()
"""
import uuid
from decimal import Decimal
from typing import List

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.foreign_tax_credit import (
    CzForeignTaxCreditSummary,
    CzCountryCreditAggregate,
)
from src.countries.cz.form_mapping import (
    CzFormMappingResult,
    CzFormSection,
    CzFormLine,
    build_form_mapping,
)
from src.countries.cz.loss_offsetting import CzLossOffsettingResult, CzSectionNetting
from src.countries.cz.tax_liability import CzTaxLiabilitySummary, compute_tax_liability
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzTaxReviewStatus
from src.countries.cz.enums import CzTaxSection

ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _netting(sec_gains=ZERO, sec_losses=ZERO, opt_gains=ZERO, opt_losses=ZERO,
             exempt_tt=ZERO, exempt_al=ZERO):
    n = CzLossOffsettingResult()
    n.securities.taxable_gains = sec_gains
    n.securities.taxable_losses = sec_losses
    n.securities.exempt_time_test_total = exempt_tt
    n.securities.exempt_annual_limit_total = exempt_al
    n.options.taxable_gains = opt_gains
    n.options.taxable_losses = opt_losses
    n.compute_combined()
    return n


def _ftc(paid=ZERO, creditable=ZERO, foreign_income=ZERO, countries=None):
    s = CzForeignTaxCreditSummary()
    s.foreign_tax_paid_total_czk = paid
    s.foreign_tax_creditable_total_czk = creditable
    s.foreign_income_total_czk = foreign_income
    if countries:
        for code, agg_data in countries.items():
            s.per_country[code] = CzCountryCreditAggregate(
                country=code,
                foreign_tax_paid_czk=agg_data.get("paid", ZERO),
                creditable_czk=agg_data.get("creditable", ZERO),
                non_creditable_czk=agg_data.get("non_creditable", ZERO),
            )
    return s


def _liability(div=ZERO, interest=ZERO, netting=None, ftc=None, cfg=None):
    cfg = cfg or CzTaxConfig()
    netting = netting or _netting()
    ftc = ftc or _ftc()
    return compute_tax_liability(div, interest, netting, ftc, cfg)


def _make_items(taxable=2, exempt=1, pending=1):
    """Create a list of CzTaxItems with known taxability distribution."""
    items = []
    for _ in range(taxable):
        it = CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
        )
        it.is_taxable = True
        it.is_exempt = False
        it.tax_review_status = CzTaxReviewStatus.RESOLVED
        items.append(it)
    for _ in range(exempt):
        it = CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
        )
        it.is_taxable = False
        it.is_exempt = True
        it.tax_review_status = CzTaxReviewStatus.RESOLVED
        items.append(it)
    for _ in range(pending):
        it = CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date="2025-03-25",
        )
        it.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        items.append(it)
    return items


# =========================================================================
# Test 1: §8 mapping contains dividends and interest
# =========================================================================

class TestSection8:
    def test_dividends_and_interest_present(self):
        mapping = build_form_mapping(
            liability=_liability(div=Decimal("10000"), interest=Decimal("5000")),
            netting=_netting(),
            ftc_summary=_ftc(),
            taxable_dividends=Decimal("10000"),
            taxable_interest=Decimal("5000"),
            currency="CZK",
        )

        sec8 = mapping.get_section("CZ_FORM_SECTION_8")
        assert sec8 is not None

        div_line = mapping.get_line("CZ_DAP_8_DIVIDENDS")
        assert div_line is not None
        assert div_line.value == Decimal("10000.00")

        int_line = mapping.get_line("CZ_DAP_8_INTEREST")
        assert int_line is not None
        assert int_line.value == Decimal("5000.00")

        total_line = mapping.get_line("CZ_DAP_8_TOTAL")
        assert total_line is not None
        assert total_line.value == Decimal("15000.00")


# =========================================================================
# Test 2: §10 mapping contains securities + options
# =========================================================================

class TestSection10:
    def test_securities_and_options_present(self):
        netting = _netting(sec_gains=Decimal("50000"), sec_losses=Decimal("10000"),
                           opt_gains=Decimal("20000"), opt_losses=Decimal("5000"))
        mapping = build_form_mapping(
            liability=_liability(netting=netting),
            netting=netting,
            ftc_summary=_ftc(),
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            currency="CZK",
        )

        sec10 = mapping.get_section("CZ_FORM_SECTION_10")
        assert sec10 is not None

        sec_line = mapping.get_line("CZ_DAP_10_SECURITIES")
        assert sec_line is not None
        assert sec_line.value == Decimal("40000.00")

        opt_line = mapping.get_line("CZ_DAP_10_OPTIONS")
        assert opt_line is not None
        assert opt_line.value == Decimal("15000.00")

        total_line = mapping.get_line("CZ_DAP_10_TOTAL")
        assert total_line is not None
        assert total_line.value == Decimal("55000.00")

    def test_exempt_totals_as_supporting_info(self):
        netting = _netting(exempt_tt=Decimal("8000"), exempt_al=Decimal("3000"))
        mapping = build_form_mapping(
            liability=_liability(netting=netting),
            netting=netting,
            ftc_summary=_ftc(),
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            currency="CZK",
        )

        tt_line = mapping.get_line("CZ_DAP_10_EXEMPT_TIME_TEST")
        assert tt_line is not None
        assert tt_line.value == Decimal("8000.00")
        assert "podklad" in (tt_line.note or "").lower()

        al_line = mapping.get_line("CZ_DAP_10_EXEMPT_ANNUAL_LIMIT")
        assert al_line is not None
        assert al_line.value == Decimal("3000.00")


# =========================================================================
# Test 3: FTC mapping
# =========================================================================

class TestFtcMapping:
    def test_ftc_lines_present(self):
        ftc = _ftc(paid=Decimal("1500"), creditable=Decimal("1000"),
                    foreign_income=Decimal("10000"),
                    countries={"US": {"paid": Decimal("1500"), "creditable": Decimal("1000"),
                                      "non_creditable": Decimal("500")}})
        liability = _liability(div=Decimal("10000"), ftc=ftc)

        mapping = build_form_mapping(
            liability=liability,
            netting=_netting(),
            ftc_summary=ftc,
            taxable_dividends=Decimal("10000"),
            taxable_interest=ZERO,
            currency="CZK",
        )

        sec_ftc = mapping.get_section("CZ_FORM_FOREIGN_TAX_CREDIT")
        assert sec_ftc is not None

        paid = mapping.get_line("CZ_DAP_FTC_PAID")
        assert paid is not None
        assert paid.value == Decimal("1500.00")

        preliminary = mapping.get_line("CZ_DAP_FTC_PRELIMINARY")
        assert preliminary is not None

        final = mapping.get_line("CZ_DAP_FTC_FINAL")
        assert final is not None

        non_cred = mapping.get_line("CZ_DAP_FTC_NON_CREDITABLE")
        assert non_cred is not None

        # Per-country
        us_line = mapping.get_line("CZ_DAP_FTC_COUNTRY_US")
        assert us_line is not None
        assert us_line.value == Decimal("1000.00")


# =========================================================================
# Test 4: Warnings includes pending items
# =========================================================================

class TestWarnings:
    def test_pending_warning_present(self):
        items = _make_items(taxable=2, exempt=1, pending=3)
        mapping = build_form_mapping(
            liability=_liability(),
            netting=_netting(),
            ftc_summary=_ftc(),
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            currency="CZK",
            items=items,
        )

        warn_sec = mapping.get_section("CZ_FORM_WARNINGS")
        assert warn_sec is not None

        pending_line = mapping.get_line("CZ_DAP_WARN_PENDING")
        assert pending_line is not None
        assert pending_line.value == Decimal("3")

        assert mapping.pending_item_count == 3

    def test_standard_warnings_always_present(self):
        mapping = build_form_mapping(
            liability=_liability(),
            netting=_netting(),
            ftc_summary=_ftc(),
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            currency="CZK",
        )
        warn_sec = mapping.get_section("CZ_FORM_WARNINGS")
        assert len(warn_sec.notes) >= 3  # threshold, SZDZ, not-official notes


# =========================================================================
# Test 5: Mapping does not recompute
# =========================================================================

class TestNoRecomputation:
    def test_values_match_input(self):
        """Form mapping must use the exact values passed in, not recompute."""
        liability = _liability(div=Decimal("12345.67"))
        mapping = build_form_mapping(
            liability=liability,
            netting=_netting(),
            ftc_summary=_ftc(),
            taxable_dividends=Decimal("12345.67"),
            taxable_interest=ZERO,
            currency="CZK",
        )

        div_line = mapping.get_line("CZ_DAP_8_DIVIDENDS")
        assert div_line.value == Decimal("12345.67")

        base_line = mapping.get_line("CZ_DAP_TAXABLE_BASE")
        assert base_line.value == liability.combined_taxable_base.quantize(Decimal("0.01"))


# =========================================================================
# Test 6: Works with missing/empty sections
# =========================================================================

class TestMissingSections:
    def test_no_liability_no_crash(self):
        mapping = build_form_mapping(
            liability=None,
            netting=None,
            ftc_summary=None,
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            currency="EUR",
        )
        assert len(mapping.sections) >= 4  # §8, §10, warnings, audit at minimum

        # §8 should still exist with zero values
        div_line = mapping.get_line("CZ_DAP_8_DIVIDENDS")
        assert div_line is not None
        assert div_line.value == ZERO

    def test_no_items_no_crash(self):
        mapping = build_form_mapping(
            liability=_liability(),
            netting=_netting(),
            ftc_summary=_ftc(),
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            currency="CZK",
            items=None,
        )
        assert mapping.total_item_count == 0
        assert mapping.exempt_item_count == 0


# =========================================================================
# Test 7: Integration — form_mapping in country_result
# =========================================================================

class TestIntegration:
    def test_form_mapping_in_country_result(self):
        from src.countries.cz.plugin import CzechTaxAggregator, CzechTaxClassifier
        from src.identification.asset_resolver import AssetResolver
        from src.classification.asset_classifier import AssetClassifier

        class D(AssetClassifier):
            def __init__(self): super().__init__(cache_file_path="d.json")
            def save_classifications(self): pass
        resolver = AssetResolver(asset_classifier=D())

        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        aggregator = CzechTaxAggregator(config=cfg)
        result = aggregator.aggregate([], [], resolver, 2025)

        cr = result.country_result
        assert "form_mapping" in cr
        fm = cr["form_mapping"]
        assert isinstance(fm, CzFormMappingResult)
        assert len(fm.sections) >= 4

    def test_to_dict_serializable(self):
        mapping = build_form_mapping(
            liability=_liability(div=Decimal("1000")),
            netting=_netting(sec_gains=Decimal("5000")),
            ftc_summary=_ftc(paid=Decimal("150"), creditable=Decimal("150"),
                             foreign_income=Decimal("1000")),
            taxable_dividends=Decimal("1000"),
            taxable_interest=ZERO,
            currency="CZK",
            items=_make_items(),
        )
        d = mapping.to_dict()
        assert isinstance(d, dict)
        assert "sections" in d
        assert len(d["sections"]) >= 4
        # All line values should be strings (from Decimal)
        for sec in d["sections"]:
            for ln in sec["lines"]:
                assert isinstance(ln["value"], str)


# =========================================================================
# Test 8: Line codes accessible via get_line()
# =========================================================================

class TestLineCodes:
    def test_stable_line_codes(self):
        mapping = build_form_mapping(
            liability=_liability(div=Decimal("1000"), interest=Decimal("500")),
            netting=_netting(sec_gains=Decimal("5000"), opt_gains=Decimal("2000")),
            ftc_summary=_ftc(paid=Decimal("150"), creditable=Decimal("150"),
                             foreign_income=Decimal("1000")),
            taxable_dividends=Decimal("1000"),
            taxable_interest=Decimal("500"),
            currency="CZK",
        )

        expected_codes = [
            "CZ_DAP_8_DIVIDENDS", "CZ_DAP_8_INTEREST", "CZ_DAP_8_TOTAL",
            "CZ_DAP_10_SECURITIES", "CZ_DAP_10_OPTIONS", "CZ_DAP_10_TOTAL",
            "CZ_DAP_TAXABLE_BASE", "CZ_DAP_GROSS_TAX", "CZ_DAP_FINAL_TAX",
            "CZ_DAP_FTC_PAID", "CZ_DAP_FTC_FINAL",
            "CZ_DAP_AUDIT_TOTAL_ITEMS",
        ]
        for code in expected_codes:
            line = mapping.get_line(code)
            assert line is not None, f"Missing line code: {code}"


# =========================================================================
# Test 9: official_line_ref verified against the 2025 forms
# (DAP 25 5405 vzor č. 30, Příloha 2 vzor č. 21, Příloha 3 vzor č. 21)
# =========================================================================

class TestOfficialLineRefs:
    def test_verified_official_refs(self):
        mapping = build_form_mapping(
            liability=_liability(div=Decimal("1000"), interest=Decimal("500")),
            netting=_netting(sec_gains=Decimal("5000"), opt_gains=Decimal("2000")),
            ftc_summary=_ftc(paid=Decimal("150"), creditable=Decimal("150"),
                             foreign_income=Decimal("1000")),
            taxable_dividends=Decimal("1000"),
            taxable_interest=Decimal("500"),
            currency="CZK",
        )

        expected_refs = {
            "CZ_DAP_8_TOTAL": "ř. 38 DAP",
            "CZ_DAP_10_TOTAL": "Příloha 2, ř. 209 → ř. 40 DAP",
            "CZ_DAP_GROSS_TAX": "ř. 57 DAP",
            "CZ_DAP_FINAL_TAX": "ř. 58 DAP (= ř. 330 Přílohy 3)",
            "CZ_DAP_FTC_FINAL": "Příloha 3, ř. 328",
            "CZ_DAP_FTC_NON_CREDITABLE": "Příloha 3, ř. 329",
        }
        for code, ref in expected_refs.items():
            line = mapping.get_line(code)
            assert line is not None, f"Missing line code: {code}"
            assert line.official_line_ref == ref, (
                f"{code}: expected {ref!r}, got {line.official_line_ref!r}"
            )

        # Securities vs options map to distinct income kinds in the
        # Příloha 2 §10 table (druh D vs druh F) — losses of one druh
        # cannot offset gains of another, so the split must stay visible.
        sec_ref = mapping.get_line("CZ_DAP_10_SECURITIES").official_line_ref
        opt_ref = mapping.get_line("CZ_DAP_10_OPTIONS").official_line_ref
        assert "druh D" in sec_ref
        assert "druh F" in opt_ref
