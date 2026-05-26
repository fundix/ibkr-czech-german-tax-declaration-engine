# tests/test_cz_invariants.py
"""
Invariant guard tests for the CZ plugin.

These tests protect architectural contracts that must NEVER be violated.
They run against a realistic mixed-portfolio TaxResult built from the
full CzechTaxAggregator pipeline.

Invariants tested:
1. FTC: paid == creditable + non_creditable (per record + totals)
2. Exempt items have included_in_tax_base=False
3. Unlinked WHT: visible, has WHT amount, NOT counted as dividend income
4. Liability is single source of truth for form mapping values
5. JSON export matches to_dict() values — no mutation
6. No country-specific imports in core modules
7. Pipeline monotonicity — exempt/taxable flags stable after full pipeline
"""
import json
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import List

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.exporters.json_exporter import export_cz_to_json
from src.countries.cz.plugin import CzechTaxAggregator, CzechTaxClassifier
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzTaxReviewStatus
from src.countries.base import TaxResult
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import CashFlowEvent, FinancialEvent, WithholdingTaxEvent
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver
from src.classification.asset_classifier import AssetClassifier


# ---------------------------------------------------------------------------
# Shared fixture: realistic mixed-portfolio TaxResult
# ---------------------------------------------------------------------------

def _resolver():
    class D(AssetClassifier):
        def __init__(self): super().__init__(cache_file_path="d.json")
        def save_classifications(self): pass
    return AssetResolver(asset_classifier=D())


def _rgl(gross, cat=AssetCategory.STOCK, holding_days=200, proceeds=Decimal("5000")):
    return RealizedGainLoss(
        originating_event_id=uuid.uuid4(),
        asset_internal_id=uuid.uuid4(),
        asset_category_at_realization=cat,
        acquisition_date="2024-06-15" if holding_days < 1100 else "2021-01-01",
        realization_date="2025-03-25",
        realization_type=(RealizationType.LONG_POSITION_SALE if cat != AssetCategory.OPTION
                          else RealizationType.OPTION_TRADE_CLOSE_LONG),
        quantity_realized=Decimal("10"),
        unit_cost_basis_eur=Decimal("100"),
        unit_realization_value_eur=proceeds / Decimal("10"),
        total_cost_basis_eur=Decimal("1000"),
        total_realization_value_eur=proceeds,
        gross_gain_loss_eur=gross,
        holding_period_days=holding_days,
    )


@pytest.fixture
def mixed_result():
    """Build a realistic TaxResult with taxable, exempt, pending, option, dividend+WHT, orphan WHT."""
    resolver = _resolver()
    cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
    classifier = CzechTaxClassifier(config=cfg)

    rgls = [
        _rgl(Decimal("500"), AssetCategory.STOCK, holding_days=200),    # taxable
        _rgl(Decimal("800"), AssetCategory.STOCK, holding_days=1200),   # exempt (time test)
        _rgl(Decimal("300"), AssetCategory.OPTION, holding_days=100),   # option taxable
        _rgl(Decimal("-100"), AssetCategory.OPTION, holding_days=50),   # option loss
    ]
    # Pending: no acquisition date
    pending_rgl = _rgl(Decimal("200"), AssetCategory.STOCK)
    pending_rgl.acquisition_date = ""
    pending_rgl.holding_period_days = None
    rgls.append(pending_rgl)

    for rgl in rgls:
        classifier.classify(rgl)

    # Dividend with linked WHT
    div = CashFlowEvent(
        asset_internal_id=uuid.uuid4(),
        event_date="2025-06-15",
        event_type=FinancialEventType.DIVIDEND_CASH,
        gross_amount_foreign_currency=Decimal("100"),
        local_currency="USD",
        gross_amount_eur=Decimal("90"),
    )
    wht_linked = WithholdingTaxEvent(
        asset_internal_id=div.asset_internal_id,
        event_date="2025-06-15",
        gross_amount_foreign_currency=Decimal("15"),
        local_currency="USD",
        gross_amount_eur=Decimal("13.5"),
        taxed_income_event_id=div.event_id,
        source_country_code="US",
    )
    # Orphan WHT (different asset, won't link)
    orphan_wht = WithholdingTaxEvent(
        asset_internal_id=uuid.uuid4(),
        event_date="2025-06-15",
        gross_amount_foreign_currency=Decimal("10"),
        local_currency="USD",
        gross_amount_eur=Decimal("9"),
        source_country_code="DE",
    )

    events: List[FinancialEvent] = [div, wht_linked, orphan_wht]

    aggregator = CzechTaxAggregator(config=cfg)
    return aggregator.aggregate(rgls, events, resolver, 2025)


# =========================================================================
# 1. FTC invariant: paid == creditable + non_creditable
# =========================================================================

class TestFtcInvariant:
    def test_per_record_invariant(self, mixed_result):
        ftc = mixed_result.country_result["ftc_summary"]
        for rec in ftc.records:
            assert rec.foreign_tax_paid_czk == rec.actual_creditable_czk + rec.non_creditable_czk, (
                f"FTC invariant violated on {rec.source_event_id}: "
                f"paid={rec.foreign_tax_paid_czk} != "
                f"creditable={rec.actual_creditable_czk} + "
                f"non_creditable={rec.non_creditable_czk}"
            )

    def test_summary_totals_invariant(self, mixed_result):
        ftc = mixed_result.country_result["ftc_summary"]
        assert ftc.foreign_tax_paid_total_czk == (
            ftc.foreign_tax_creditable_total_czk + ftc.foreign_tax_non_creditable_total_czk
        )


# =========================================================================
# 2. Exempt items have included_in_tax_base=False
# =========================================================================

class TestExemptNotInTaxBase:
    def test_all_exempt_items_excluded(self, mixed_result):
        items = mixed_result.country_result["items"]
        exempt_items = [it for it in items if it.is_exempt]
        assert len(exempt_items) >= 1, "Test requires at least one exempt item"
        for it in exempt_items:
            assert it.included_in_tax_base is False, (
                f"Exempt item {it.source_event_id} ({it.item_type.name}) "
                f"has included_in_tax_base=True — violates invariant"
            )


# =========================================================================
# 3. Unlinked WHT: visible, has WHT, NOT counted as income
# =========================================================================

class TestUnlinkedWhtBehavior:
    def test_unlinked_wht_exists(self, mixed_result):
        items = mixed_result.country_result["items"]
        other_items = [it for it in items if it.item_type == CzTaxItemType.OTHER]
        assert len(other_items) >= 1, "Expected at least one unlinked WHT standalone item"

    def test_unlinked_wht_has_amount(self, mixed_result):
        items = mixed_result.country_result["items"]
        for it in items:
            if it.item_type == CzTaxItemType.OTHER:
                assert it.total_wht_czk() > Decimal(0) or len(it.wht_records) > 0, (
                    f"Unlinked WHT item {it.source_event_id} has no WHT data"
                )

    def test_unlinked_wht_not_in_dividend_income(self, mixed_result):
        """gross_dividends must not include unlinked WHT amounts."""
        sec = mixed_result.sections["cz_8_dividends"]
        items = mixed_result.country_result["items"]

        # Sum income from real dividend items only
        real_div_income = sum(
            (it.amount_eur or Decimal(0))
            for it in items
            if it.item_type in (CzTaxItemType.DIVIDEND, CzTaxItemType.FUND_DISTRIBUTION)
            and it.included_in_tax_base
        )

        # The section's gross_dividends should match real dividends, not include orphan WHT
        section_div = sec.line_items.get("gross_dividends_eur", Decimal(0))
        assert section_div == real_div_income.quantize(Decimal("0.01"))


# =========================================================================
# 4. Liability is single source of truth for form mapping
# =========================================================================

class TestLiabilityIsTruth:
    def test_form_mapping_reads_liability(self, mixed_result):
        liability = mixed_result.country_result["liability"]
        form = mixed_result.country_result["form_mapping"]

        base_line = form.get_line("CZ_DAP_TAXABLE_BASE")
        assert base_line is not None
        assert base_line.value == liability.combined_taxable_base.quantize(Decimal("0.01"))

        gross_line = form.get_line("CZ_DAP_GROSS_TAX")
        assert gross_line is not None
        assert gross_line.value == liability.gross_czech_tax.quantize(Decimal("0.01"))

        final_line = form.get_line("CZ_DAP_FINAL_TAX")
        assert final_line is not None
        assert final_line.value == liability.final_czech_tax_after_credit.quantize(Decimal("0.01"))

    def test_form_sec10_matches_liability(self, mixed_result):
        liability = mixed_result.country_result["liability"]
        form = mixed_result.country_result["form_mapping"]

        sec_line = form.get_line("CZ_DAP_10_SECURITIES")
        assert sec_line is not None
        assert sec_line.value == liability.taxable_securities_net.quantize(Decimal("0.01"))


# =========================================================================
# 5. JSON export matches to_dict() — no data mutation
# =========================================================================

class TestExportersNoMutation:
    def test_json_items_match_to_dict(self, mixed_result):
        json_str = export_cz_to_json(mixed_result)
        data = json.loads(json_str)
        items = mixed_result.country_result["items"]

        assert len(data["items"]) == len(items)

        for json_item, orig_item in zip(data["items"], items):
            orig_dict = orig_item.to_dict()
            # Core fields must match exactly
            for key in ["item_type", "section", "is_taxable", "is_exempt",
                        "included_in_tax_base", "tax_review_status"]:
                assert json_item[key] == orig_dict[key], (
                    f"JSON field '{key}' mismatch: "
                    f"json={json_item[key]} vs to_dict={orig_dict[key]}"
                )


# =========================================================================
# 6. No country-specific imports in core modules
# =========================================================================

class TestNoCoreContamination:
    """Verify that core modules do not import from countries/."""

    CORE_DIRS = [
        "src/domain",
        "src/parsers",
        "src/processing",
        "src/identification",
        "src/classification",
    ]

    def test_no_countries_import_in_core(self):
        project_root = Path(__file__).parent.parent
        violations = []

        for core_dir in self.CORE_DIRS:
            dir_path = project_root / core_dir
            if not dir_path.exists():
                continue
            for py_file in dir_path.rglob("*.py"):
                content = py_file.read_text(encoding="utf-8")
                for line_no, line in enumerate(content.splitlines(), 1):
                    # Skip comments
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if re.search(r"from\s+src\.countries", line) or re.search(r"import\s+src\.countries", line):
                        rel = py_file.relative_to(project_root)
                        violations.append(f"{rel}:{line_no}: {stripped}")

        assert violations == [], (
            "Core modules must not import from countries/:\n" +
            "\n".join(violations)
        )


# =========================================================================
# 7. Pipeline monotonicity — flags don't flip after full pipeline
# =========================================================================

class TestPipelineMonotonicity:
    def test_exempt_items_stay_exempt(self, mixed_result):
        """Items marked exempt should remain exempt and not-in-tax-base
        through all downstream pipeline steps (netting, FTC, liability)."""
        items = mixed_result.country_result["items"]
        for it in items:
            if it.is_exempt:
                assert it.included_in_tax_base is False, (
                    f"Exempt item {it.source_event_id} flipped to included_in_tax_base=True"
                )
                assert it.is_taxable is False, (
                    f"Exempt item {it.source_event_id} has is_taxable=True"
                )

    def test_taxable_items_consistent(self, mixed_result):
        """Taxable items must have is_taxable=True and included_in_tax_base=True."""
        items = mixed_result.country_result["items"]
        for it in items:
            if it.is_taxable and it.tax_review_status == CzTaxReviewStatus.RESOLVED:
                assert it.included_in_tax_base is True, (
                    f"Taxable resolved item {it.source_event_id} has included_in_tax_base=False"
                )
                assert it.is_exempt is False, (
                    f"Taxable item {it.source_event_id} has is_exempt=True"
                )

    def test_pending_items_conservatively_included(self, mixed_result):
        """Pending items should be conservatively included in tax base."""
        items = mixed_result.country_result["items"]
        pending = [it for it in items if it.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW]
        assert len(pending) >= 1, "Test requires at least one pending item"
        for it in pending:
            assert it.included_in_tax_base is True, (
                f"Pending item {it.source_event_id} excluded from tax base — "
                "should be conservatively included"
            )
