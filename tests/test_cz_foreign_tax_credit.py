# tests/test_cz_foreign_tax_credit.py
"""
Tests for Czech preliminary foreign tax credit (§38f ZDP).

Covers:
1. Dividend with WHT under cap → full creditable
2. Dividend with WHT over cap → partially creditable
3. Two dividends from different countries → per-country aggregation
4. Item without source_country → pending_manual_review
5. Item without linked WHT → zero credit, no crash
6. Summary totals for paid / creditable / non-creditable
7. Interest item with WHT → same infrastructure works
8. FTC disabled via config
"""
import uuid
from decimal import Decimal
from typing import List

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.foreign_tax_credit import (
    CzForeignTaxCreditRecord,
    CzForeignTaxCreditSummary,
    evaluate_foreign_tax_credit,
)
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzWhtRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _div(
    amount_czk: Decimal,
    wht_czk: Decimal = Decimal(0),
    source_country: str = "US",
) -> CzTaxItem:
    """Create a dividend CzTaxItem with optional linked WHT."""
    item = CzTaxItem(
        item_type=CzTaxItemType.DIVIDEND,
        section=CzTaxSection.CZ_8_DIVIDENDS,
        source_event_id=uuid.uuid4(),
        event_date="2025-06-15",
        amount_czk=amount_czk,
        amount_eur=amount_czk / Decimal("24"),
    )
    if wht_czk > Decimal(0):
        item.wht_records.append(CzWhtRecord(
            wht_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            original_amount=wht_czk / Decimal("22"),  # approximate USD
            original_currency="USD",
            amount_czk=wht_czk,
            source_country=source_country,
        ))
    return item


def _div_no_country(amount_czk: Decimal, wht_czk: Decimal) -> CzTaxItem:
    """Dividend with WHT but missing source_country."""
    item = CzTaxItem(
        item_type=CzTaxItemType.DIVIDEND,
        section=CzTaxSection.CZ_8_DIVIDENDS,
        source_event_id=uuid.uuid4(),
        event_date="2025-06-15",
        amount_czk=amount_czk,
        amount_eur=amount_czk / Decimal("24"),
    )
    item.wht_records.append(CzWhtRecord(
        wht_event_id=uuid.uuid4(),
        event_date="2025-06-15",
        original_amount=wht_czk,
        original_currency="USD",
        amount_czk=wht_czk,
        source_country=None,  # missing!
    ))
    return item


def _interest(amount_czk: Decimal, wht_czk: Decimal = Decimal(0), country: str = "US") -> CzTaxItem:
    """Interest item with optional WHT."""
    item = CzTaxItem(
        item_type=CzTaxItemType.INTEREST,
        section=CzTaxSection.CZ_8_INTEREST,
        source_event_id=uuid.uuid4(),
        event_date="2025-06-15",
        amount_czk=amount_czk,
        amount_eur=amount_czk / Decimal("24"),
    )
    if wht_czk > Decimal(0):
        item.wht_records.append(CzWhtRecord(
            wht_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            original_amount=wht_czk,
            original_currency="USD",
            amount_czk=wht_czk,
            source_country=country,
        ))
    return item


# =========================================================================
# Test 1: WHT under cap → full creditable
# =========================================================================

class TestWhtUnderCap:
    def test_full_credit(self):
        """WHT 10% on 1000 CZK = 100 CZK, cap 15% = 150 CZK → full 100 creditable."""
        cfg = CzTaxConfig()
        items = [_div(Decimal("1000"), wht_czk=Decimal("100"), source_country="US")]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)

        assert len(summary.records) == 1
        rec = summary.records[0]
        assert rec.foreign_tax_paid_czk == Decimal("100")
        assert rec.configured_cap_rate == Decimal("0.15")
        assert rec.max_creditable_czk == Decimal("150.00")
        assert rec.actual_creditable_czk == Decimal("100")
        assert rec.non_creditable_czk == Decimal("0")
        assert rec.review_status == "RESOLVED"

    def test_ftc_record_attached_to_item(self):
        cfg = CzTaxConfig()
        items = [_div(Decimal("1000"), wht_czk=Decimal("100"))]
        evaluate_foreign_tax_credit(items, cfg, has_fx=True)

        assert hasattr(items[0], "ftc_record")
        assert items[0].ftc_record.actual_creditable_czk == Decimal("100")


# =========================================================================
# Test 2: WHT over cap → partially creditable
# =========================================================================

class TestWhtOverCap:
    def test_partial_credit(self):
        """WHT 30% on 1000 CZK = 300 CZK, cap 15% = 150 → 150 creditable, 150 non-creditable."""
        cfg = CzTaxConfig()
        items = [_div(Decimal("1000"), wht_czk=Decimal("300"), source_country="US")]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        rec = summary.records[0]

        assert rec.foreign_tax_paid_czk == Decimal("300")
        assert rec.max_creditable_czk == Decimal("150.00")
        assert rec.actual_creditable_czk == Decimal("150.00")
        assert rec.non_creditable_czk == Decimal("150.00")

    def test_custom_country_cap(self):
        """Country-specific cap: IE at 25%."""
        cfg = CzTaxConfig(country_credit_caps={"IE": Decimal("0.25")})
        items = [_div(Decimal("1000"), wht_czk=Decimal("300"), source_country="IE")]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        rec = summary.records[0]

        assert rec.configured_cap_rate == Decimal("0.25")
        assert rec.max_creditable_czk == Decimal("250.00")
        assert rec.actual_creditable_czk == Decimal("250.00")
        assert rec.non_creditable_czk == Decimal("50.00")


# =========================================================================
# Test 3: Two dividends from different countries
# =========================================================================

class TestPerCountryAggregation:
    def test_two_countries(self):
        cfg = CzTaxConfig(country_credit_caps={
            "US": Decimal("0.15"),
            "DE": Decimal("0.15"),
        })
        items = [
            _div(Decimal("1000"), wht_czk=Decimal("150"), source_country="US"),
            _div(Decimal("2000"), wht_czk=Decimal("500"), source_country="DE"),
        ]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)

        assert len(summary.records) == 2
        assert "US" in summary.per_country
        assert "DE" in summary.per_country

        us = summary.per_country["US"]
        assert us.foreign_tax_paid_czk == Decimal("150")
        assert us.creditable_czk == Decimal("150")  # 150 ≤ 1000*0.15=150
        assert us.item_count == 1

        de = summary.per_country["DE"]
        assert de.foreign_tax_paid_czk == Decimal("500")
        assert de.creditable_czk == Decimal("300.00")  # 2000*0.15=300 < 500
        assert de.non_creditable_czk == Decimal("200.00")

    def test_summary_totals(self):
        cfg = CzTaxConfig()
        items = [
            _div(Decimal("1000"), wht_czk=Decimal("100"), source_country="US"),
            _div(Decimal("2000"), wht_czk=Decimal("400"), source_country="DE"),
        ]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)

        assert summary.foreign_tax_paid_total_czk == Decimal("500")
        # US: 100 ≤ 150 → 100 creditable. DE: 400 > 300 → 300 creditable.
        assert summary.foreign_tax_creditable_total_czk == Decimal("400")
        assert summary.foreign_tax_non_creditable_total_czk == Decimal("100")
        assert summary.foreign_income_total_czk == Decimal("3000")


# =========================================================================
# Test 4: Missing source_country → pending
# =========================================================================

class TestMissingCountry:
    def test_pending_review(self):
        cfg = CzTaxConfig()
        items = [_div_no_country(Decimal("1000"), wht_czk=Decimal("150"))]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        rec = summary.records[0]

        assert rec.review_status == "PENDING_MANUAL_REVIEW"
        assert rec.source_country is None
        assert "Missing source_country" in (rec.review_note or "")
        # Default cap still applied
        assert rec.configured_cap_rate == Decimal("0.15")
        assert rec.actual_creditable_czk == Decimal("150.00")

        assert summary.pending_review_count == 1
        assert summary.pending_review_total_czk == Decimal("150")

    def test_unknown_in_per_country(self):
        cfg = CzTaxConfig()
        items = [_div_no_country(Decimal("1000"), wht_czk=Decimal("100"))]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        assert "UNKNOWN" in summary.per_country


# =========================================================================
# Test 5: No linked WHT → zero credit
# =========================================================================

class TestNoWht:
    def test_zero_credit_no_crash(self):
        cfg = CzTaxConfig()
        items = [_div(Decimal("1000"), wht_czk=Decimal(0))]  # no WHT

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        assert len(summary.records) == 1
        rec = summary.records[0]

        assert rec.foreign_tax_paid_czk == Decimal("0")
        assert rec.actual_creditable_czk == Decimal("0")
        assert rec.non_creditable_czk == Decimal("0")
        assert "No linked WHT" in (rec.review_note or "")


# =========================================================================
# Test 6: Summary line_items for TaxResult
# =========================================================================

class TestSummaryLineItems:
    def test_line_items_structure(self):
        cfg = CzTaxConfig()
        items = [
            _div(Decimal("1000"), wht_czk=Decimal("100"), source_country="US"),
            _div(Decimal("500"), wht_czk=Decimal("200"), source_country="DE"),
        ]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        li = summary.to_line_items("CZK")

        assert "ftc_foreign_tax_paid_czk" in li
        assert "ftc_creditable_czk" in li
        assert "ftc_non_creditable_czk" in li
        assert "ftc_foreign_income_total_czk" in li
        assert "ftc_item_count" in li
        # Per-country
        assert "ftc_us_paid_czk" in li
        assert "ftc_us_creditable_czk" in li
        assert "ftc_de_paid_czk" in li

    def test_record_to_dict(self):
        cfg = CzTaxConfig()
        items = [_div(Decimal("1000"), wht_czk=Decimal("100"), source_country="US")]
        evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        d = items[0].ftc_record.to_dict()

        assert d["source_country"] == "US"
        assert d["gross_income_czk"] == "1000"
        assert d["foreign_tax_paid_czk"] == "100"
        assert d["configured_cap_rate"] == "0.15"
        assert d["actual_creditable_czk"] == "100"


# =========================================================================
# Test 7: Interest with WHT
# =========================================================================

class TestInterestFtc:
    def test_interest_with_wht(self):
        """Interest items with linked WHT use the same FTC infrastructure."""
        cfg = CzTaxConfig()
        items = [_interest(Decimal("500"), wht_czk=Decimal("50"), country="US")]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        assert len(summary.records) == 1
        rec = summary.records[0]

        assert rec.foreign_tax_paid_czk == Decimal("50")
        assert rec.actual_creditable_czk == Decimal("50")  # 50 ≤ 500*0.15=75
        assert rec.review_status == "RESOLVED"

    def test_interest_without_wht(self):
        """Interest without WHT → zero credit, no crash."""
        cfg = CzTaxConfig()
        items = [_interest(Decimal("500"))]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        assert len(summary.records) == 1
        assert summary.records[0].foreign_tax_paid_czk == Decimal("0")


# =========================================================================
# Test 8: FTC disabled
# =========================================================================

class TestFtcDisabled:
    def test_disabled_returns_empty_summary(self):
        cfg = CzTaxConfig(foreign_tax_credit_enabled=False)
        items = [_div(Decimal("1000"), wht_czk=Decimal("150"))]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        assert len(summary.records) == 0
        assert summary.foreign_tax_paid_total_czk == Decimal(0)


# =========================================================================
# Test 9: Integration via aggregator
# =========================================================================

class TestAggregatorIntegration:
    def test_ftc_section_in_result(self):
        from src.countries.cz.plugin import CzechTaxAggregator
        from src.identification.asset_resolver import AssetResolver
        from src.classification.asset_classifier import AssetClassifier
        from src.domain.events import CashFlowEvent, WithholdingTaxEvent
        from src.domain.enums import FinancialEventType

        class D(AssetClassifier):
            def __init__(self): super().__init__(cache_file_path="d.json")
            def save_classifications(self): pass
        resolver = AssetResolver(asset_classifier=D())

        div = CashFlowEvent(
            asset_internal_id=uuid.uuid4(),
            event_date="2025-06-15",
            event_type=FinancialEventType.DIVIDEND_CASH,
            gross_amount_foreign_currency=Decimal("100"),
            local_currency="USD",
            gross_amount_eur=Decimal("90"),
        )
        wht = WithholdingTaxEvent(
            asset_internal_id=div.asset_internal_id,
            event_date="2025-06-15",
            gross_amount_foreign_currency=Decimal("15"),
            local_currency="USD",
            gross_amount_eur=Decimal("13.5"),
            taxed_income_event_id=div.event_id,
            source_country_code="US",
        )

        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        aggregator = CzechTaxAggregator(config=cfg)
        result = aggregator.aggregate([], [div, wht], resolver, 2025)

        assert "cz_ftc_summary" in result.sections
        ftc_sec = result.sections["cz_ftc_summary"]
        assert "ftc_foreign_tax_paid_eur" in ftc_sec.line_items
        assert "ftc_creditable_eur" in ftc_sec.line_items

        cr = result.country_result
        assert "ftc_summary" in cr
        assert isinstance(cr["ftc_summary"], CzForeignTaxCreditSummary)
        assert cr["ftc_summary"].item_count >= 1


# ---------------------------------------------------------------------------
# Verified default treaty caps (2026-07-03, see cz/config.py citations)
# ---------------------------------------------------------------------------

class TestVerifiedDefaultTreatyCaps:
    """The shipped country_credit_caps were verified treaty-by-treaty for
    portfolio dividends. The NL case is the important one: NL withholds
    15 % domestically but the treaty cap (138/1974 Sb.) is only 10 % —
    the excess 5 % is NOT creditable (reclaim in NL instead)."""

    def test_nl_dividend_credit_capped_at_treaty_10_percent(self):
        cfg = CzTaxConfig()
        items = [_div(Decimal("1000"), wht_czk=Decimal("150"), source_country="NL")]
        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        assert summary.foreign_tax_paid_total_czk == Decimal("150")
        assert summary.foreign_tax_creditable_total_czk == Decimal("100")
        assert summary.per_country["NL"].non_creditable_czk == Decimal("50")

    def test_default_caps_match_verified_treaty_rates(self):
        cfg = CzTaxConfig()
        assert cfg.country_credit_caps["US"] == Decimal("0.15")
        assert cfg.country_credit_caps["DE"] == Decimal("0.15")
        assert cfg.country_credit_caps["CH"] == Decimal("0.15")
        assert cfg.country_credit_caps["CA"] == Decimal("0.15")
        for low_cap_country in ("NL", "FR", "AT", "LU"):
            assert cfg.country_credit_caps[low_cap_country] == Decimal("0.10")
