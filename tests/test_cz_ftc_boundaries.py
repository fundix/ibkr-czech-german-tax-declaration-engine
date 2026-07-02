# tests/test_cz_ftc_boundaries.py
"""
Boundary tests for Czech foreign tax credit found during self-audit.

Covers:
1. Unlinked WHT standalone item NOT double-counted in dividend income
2. Negative gross income (dividend correction) — cap clamps to zero
3. Multiple WHT records with different source countries on one item
4. Exempt item (included_in_tax_base=False) skipped by FTC
5. Invariant: paid = creditable + non_creditable for every record
"""
import uuid
from decimal import Decimal
from typing import List

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.foreign_tax_credit import (
    CzForeignTaxCreditSummary,
    evaluate_foreign_tax_credit,
)
from src.countries.cz.tax_items import (
    CzExemptionReason,
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
    CzWhtRecord,
)


def _div(amount_czk, wht_czk=Decimal(0), country="US"):
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
            original_amount=wht_czk,
            original_currency="USD",
            amount_czk=wht_czk,
            source_country=country,
        ))
    return item


# =========================================================================
# 1. Unlinked WHT standalone item NOT double-counted
# =========================================================================

class TestUnlinkedWhtNotDoubleCounted:
    def test_other_item_type_skipped_by_ftc(self):
        """Unlinked WHT standalone item (item_type=OTHER) must NOT be counted by FTC."""
        cfg = CzTaxConfig()
        unlinked = CzTaxItem(
            item_type=CzTaxItemType.OTHER,
            section=CzTaxSection.CZ_8_DIVIDENDS,
            source_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            amount_czk=Decimal("100"),
            amount_eur=Decimal("4"),
            wht_records=[CzWhtRecord(
                wht_event_id=uuid.uuid4(),
                event_date="2025-06-15",
                original_amount=Decimal("100"),
                original_currency="USD",
                amount_czk=Decimal("100"),
                source_country="US",
            )],
        )

        summary = evaluate_foreign_tax_credit([unlinked], cfg, has_fx=True)
        # OTHER is not in _FTC_ELIGIBLE_TYPES → should be skipped
        assert summary.item_count == 0
        assert summary.foreign_tax_paid_total_czk == Decimal(0)

    def test_dividend_income_not_inflated_by_unlinked_wht(self):
        """Integration: unlinked WHT must not inflate gross_dividends in §8 section."""
        from src.countries.cz.plugin import CzechTaxAggregator, CzechTaxClassifier
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
            local_currency="EUR",
            gross_amount_eur=Decimal("100"),
        )
        # Orphan WHT — different asset, won't link
        orphan_wht = WithholdingTaxEvent(
            asset_internal_id=uuid.uuid4(),
            event_date="2025-06-15",
            gross_amount_foreign_currency=Decimal("15"),
            local_currency="EUR",
            gross_amount_eur=Decimal("15"),
            source_country_code="US",
        )

        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        aggregator = CzechTaxAggregator(config=cfg)
        result = aggregator.aggregate([], [div, orphan_wht], resolver, 2025)

        sec = result.sections["cz_8_dividends"]
        # gross_dividends should be 100 (the real dividend), NOT 100+15
        assert sec.line_items["gross_dividends_eur"] == Decimal("100.00")
        # wht_paid should include both linked (0) + unlinked (15) WHT
        assert sec.line_items["wht_paid_eur"] == Decimal("15.00")


# =========================================================================
# 2. Negative gross income — cap clamps to zero
# =========================================================================

class TestNegativeGrossIncome:
    def test_negative_income_zero_creditable(self):
        """Negative gross income (correction) → max_creditable should be ≥ 0."""
        cfg = CzTaxConfig()
        item = CzTaxItem(
            item_type=CzTaxItemType.DIVIDEND,
            section=CzTaxSection.CZ_8_DIVIDENDS,
            source_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            amount_czk=Decimal("-500"),  # negative = correction/reversal
            amount_eur=Decimal("-20"),
        )
        item.wht_records.append(CzWhtRecord(
            wht_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            original_amount=Decimal("75"),
            original_currency="USD",
            amount_czk=Decimal("75"),
            source_country="US",
        ))

        summary = evaluate_foreign_tax_credit([item], cfg, has_fx=True)
        rec = summary.records[0]

        # Negative income bears no Czech tax → cap clamps to zero and the
        # WHT is entirely non-creditable.
        assert rec.max_creditable_czk == Decimal("0.00")
        assert rec.actual_creditable_czk == Decimal("0.00")
        assert rec.non_creditable_czk == Decimal("75")


# =========================================================================
# 3. Multiple WHT records with different countries on one item
# =========================================================================

class TestMultipleWhtRecords:
    def test_first_country_used(self):
        """When multiple WHT records exist, source_country from first non-None is used."""
        cfg = CzTaxConfig(country_credit_caps={
            "US": Decimal("0.15"),
            "DE": Decimal("0.26"),  # different rate
        })
        item = CzTaxItem(
            item_type=CzTaxItemType.DIVIDEND,
            section=CzTaxSection.CZ_8_DIVIDENDS,
            source_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            amount_czk=Decimal("1000"),
            amount_eur=Decimal("40"),
        )
        item.wht_records.append(CzWhtRecord(
            wht_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            original_amount=Decimal("100"),
            original_currency="USD",
            amount_czk=Decimal("100"),
            source_country="US",  # first
        ))
        item.wht_records.append(CzWhtRecord(
            wht_event_id=uuid.uuid4(),
            event_date="2025-06-15",
            original_amount=Decimal("50"),
            original_currency="EUR",
            amount_czk=Decimal("50"),
            source_country="DE",  # second — ignored for cap rate
        ))

        summary = evaluate_foreign_tax_credit([item], cfg, has_fx=True)
        rec = summary.records[0]

        assert rec.source_country == "US"
        assert rec.configured_cap_rate == Decimal("0.15")
        # total wht = 100 + 50 = 150, cap = 1000*0.15 = 150
        assert rec.foreign_tax_paid_czk == Decimal("150")
        assert rec.actual_creditable_czk == Decimal("150.00")


# =========================================================================
# 4. Exempt item skipped by FTC
# =========================================================================

class TestExemptItemSkipped:
    def test_exempt_dividend_not_in_ftc(self):
        """Item with included_in_tax_base=False should be skipped by FTC."""
        cfg = CzTaxConfig()
        item = _div(Decimal("1000"), wht_czk=Decimal("150"))
        item.is_exempt = True
        item.included_in_tax_base = False

        summary = evaluate_foreign_tax_credit([item], cfg, has_fx=True)
        assert summary.item_count == 0
        assert summary.foreign_tax_paid_total_czk == Decimal(0)


# =========================================================================
# 5. Invariant: paid = creditable + non_creditable
# =========================================================================

class TestInvariant:
    def test_paid_equals_creditable_plus_non_creditable(self):
        """For every FTC record: paid == creditable + non_creditable."""
        cfg = CzTaxConfig()
        items = [
            _div(Decimal("1000"), wht_czk=Decimal("100"), country="US"),   # under cap
            _div(Decimal("500"), wht_czk=Decimal("200"), country="DE"),    # over cap
            _div(Decimal("2000"), wht_czk=Decimal("0")),                    # no WHT
        ]

        summary = evaluate_foreign_tax_credit(items, cfg, has_fx=True)

        for rec in summary.records:
            assert rec.foreign_tax_paid_czk == rec.actual_creditable_czk + rec.non_creditable_czk, (
                f"Invariant violated for {rec.source_event_id}: "
                f"paid={rec.foreign_tax_paid_czk} != "
                f"creditable={rec.actual_creditable_czk} + "
                f"non_creditable={rec.non_creditable_czk}"
            )

        # Also check summary totals
        assert (
            summary.foreign_tax_paid_total_czk ==
            summary.foreign_tax_creditable_total_czk + summary.foreign_tax_non_creditable_total_czk
        )
