# src/countries/cz/foreign_tax_credit.py
"""
Czech preliminary foreign tax credit evaluator (§38f ZDP).

Computes a **preliminary** (per-item) foreign tax credit for §8 income
items (dividends, interest) that have linked withholding-tax records.

"Preliminary" means:
- The credit is capped per-item at ``cap_rate × gross_income_czk``.
- The final §38f credit on the full Czech tax return may be further
  limited by the overall Czech tax liability on foreign income, which
  requires the full DAP computation (not yet implemented).
- This module produces the *maximum* credit that CAN be claimed,
  subject to the per-item / per-country cap.

Key design:
- ``cap_rate`` per country comes from ``CzTaxConfig.country_credit_caps``
  (treaty-based), falling back to ``default_max_credit_rate``.
- Each item gets a ``CzForeignTaxCreditRecord`` attached.
- A ``CzForeignTaxCreditSummary`` aggregates totals + per-country breakdown.
- Items without ``source_country`` are marked ``PENDING_MANUAL_REVIEW``.
- Items without linked WHT get a zero-credit record (no crash).

Run AFTER item building and time-test / annual-limit evaluation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzTaxReviewStatus

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
TWO = Decimal("0.01")

# Item types eligible for foreign tax credit analysis
_FTC_ELIGIBLE_TYPES = {
    CzTaxItemType.DIVIDEND,
    CzTaxItemType.FUND_DISTRIBUTION,
    CzTaxItemType.INTEREST,
}


# ---------------------------------------------------------------------------
# Per-item credit record
# ---------------------------------------------------------------------------

@dataclass
class CzForeignTaxCreditRecord:
    """Preliminary foreign tax credit computation for one income item."""

    source_event_id: str
    source_country: Optional[str]
    gross_income_czk: Decimal
    foreign_tax_paid_czk: Decimal

    # Cap computation
    configured_cap_rate: Decimal
    max_creditable_czk: Decimal       # cap_rate × gross_income_czk
    actual_creditable_czk: Decimal    # min(paid, max_creditable)
    non_creditable_czk: Decimal       # paid - actual_creditable

    review_status: str = "RESOLVED"
    review_note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_event_id": self.source_event_id,
            "source_country": self.source_country,
            "gross_income_czk": str(self.gross_income_czk),
            "foreign_tax_paid_czk": str(self.foreign_tax_paid_czk),
            "configured_cap_rate": str(self.configured_cap_rate),
            "max_creditable_czk": str(self.max_creditable_czk),
            "actual_creditable_czk": str(self.actual_creditable_czk),
            "non_creditable_czk": str(self.non_creditable_czk),
            "review_status": self.review_status,
            "review_note": self.review_note,
        }


# ---------------------------------------------------------------------------
# Per-country aggregate
# ---------------------------------------------------------------------------

@dataclass
class CzCountryCreditAggregate:
    """Aggregated FTC figures for one source country."""
    country: str
    gross_income_czk: Decimal = ZERO
    foreign_tax_paid_czk: Decimal = ZERO
    creditable_czk: Decimal = ZERO
    non_creditable_czk: Decimal = ZERO
    item_count: int = 0


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@dataclass
class CzForeignTaxCreditSummary:
    """Aggregate foreign tax credit summary across all eligible items."""

    foreign_tax_paid_total_czk: Decimal = ZERO
    foreign_tax_creditable_total_czk: Decimal = ZERO
    foreign_tax_non_creditable_total_czk: Decimal = ZERO
    foreign_income_total_czk: Decimal = ZERO
    pending_review_total_czk: Decimal = ZERO
    pending_review_count: int = 0
    item_count: int = 0

    per_country: Dict[str, CzCountryCreditAggregate] = field(default_factory=dict)
    records: List[CzForeignTaxCreditRecord] = field(default_factory=list)

    def to_line_items(self, currency: str) -> Dict[str, Decimal]:
        c = currency.lower()
        d: Dict[str, Decimal] = {
            f"ftc_foreign_income_total_{c}": self.foreign_income_total_czk.quantize(TWO),
            f"ftc_foreign_tax_paid_{c}": self.foreign_tax_paid_total_czk.quantize(TWO),
            f"ftc_creditable_{c}": self.foreign_tax_creditable_total_czk.quantize(TWO),
            f"ftc_non_creditable_{c}": self.foreign_tax_non_creditable_total_czk.quantize(TWO),
            f"ftc_pending_review_{c}": self.pending_review_total_czk.quantize(TWO),
            "ftc_item_count": Decimal(self.item_count),
            "ftc_pending_count": Decimal(self.pending_review_count),
        }
        # Per-country breakdown
        for code, agg in sorted(self.per_country.items()):
            d[f"ftc_{code.lower()}_paid_{c}"] = agg.foreign_tax_paid_czk.quantize(TWO)
            d[f"ftc_{code.lower()}_creditable_{c}"] = agg.creditable_czk.quantize(TWO)
            d[f"ftc_{code.lower()}_non_creditable_{c}"] = agg.non_creditable_czk.quantize(TWO)
        return d


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def evaluate_foreign_tax_credit(
    items: List[CzTaxItem],
    config: CzTaxConfig,
    has_fx: bool,
) -> CzForeignTaxCreditSummary:
    """
    Compute preliminary foreign tax credit for eligible §8 income items.

    Attaches a ``CzForeignTaxCreditRecord`` to each eligible item
    as ``item.ftc_record`` (dynamic attribute).

    Returns a ``CzForeignTaxCreditSummary`` with totals and per-country
    breakdown.
    """
    summary = CzForeignTaxCreditSummary()

    if not config.foreign_tax_credit_enabled:
        return summary

    for it in items:
        if it.item_type not in _FTC_ELIGIBLE_TYPES:
            continue
        if not it.included_in_tax_base:
            continue

        summary.item_count += 1

        gross = (it.amount_czk if has_fx else it.amount_eur) or ZERO
        skipped_foreign_wht = False
        if has_fx:
            wht_paid = it.total_wht_czk()
        else:
            # No FX converter: gross is in EUR. Only WHT already denominated in EUR
            # can be capped against a EUR gross; summing a foreign-currency WHT here
            # would compare e.g. USD against a EUR cap (unit mismatch). Include only
            # EUR WHT and flag the item if any foreign-currency WHT had to be skipped.
            wht_paid = sum(
                (
                    r.original_amount
                    for r in it.wht_records
                    if r.original_amount is not None
                    and (r.original_currency or "EUR").upper() == "EUR"
                ),
                ZERO,
            )
            skipped_foreign_wht = any(
                r.original_amount is not None
                and (r.original_currency or "EUR").upper() != "EUR"
                for r in it.wht_records
            )

        # Determine source country (from first WHT record, or item itself)
        source_country: Optional[str] = None
        for r in it.wht_records:
            if r.source_country:
                source_country = r.source_country.upper()
                break

        # Determine cap rate
        if source_country and source_country in config.country_credit_caps:
            cap_rate = config.country_credit_caps[source_country]
        else:
            cap_rate = config.default_max_credit_rate

        # Compute cap. Negative gross income (a reversal/correction) bears no
        # Czech tax, so it can never support a credit — clamp to zero rather
        # than taking the absolute value.
        max_creditable = (max(gross, ZERO) * cap_rate).quantize(TWO)
        # Net WHT paid can be negative when refunds exceed charges on this
        # item (data anomaly) — a credit can never be negative, so floor at 0.
        # The invariant paid = creditable + non_creditable is preserved.
        actual_creditable = min(max(wht_paid, ZERO), max_creditable)
        non_creditable = wht_paid - actual_creditable

        # Review status
        review_status = "RESOLVED"
        review_note: Optional[str] = None

        if not source_country and wht_paid > ZERO:
            review_status = "PENDING_MANUAL_REVIEW"
            review_note = (
                "Missing source_country on WHT record — "
                "default cap rate applied; manual verification needed"
            )
            summary.pending_review_count += 1
            summary.pending_review_total_czk += wht_paid

        if wht_paid == ZERO:
            review_note = "No linked WHT — zero credit"

        if skipped_foreign_wht:
            review_status = "PENDING_MANUAL_REVIEW"
            review_note = (
                (review_note + "; " if review_note else "")
                + "Foreign-currency WHT excluded from EUR-mode credit cap "
                "(no FX converter) — manual review required"
            )

        record = CzForeignTaxCreditRecord(
            source_event_id=str(it.source_event_id),
            source_country=source_country,
            gross_income_czk=gross,
            foreign_tax_paid_czk=wht_paid,
            configured_cap_rate=cap_rate,
            max_creditable_czk=max_creditable,
            actual_creditable_czk=actual_creditable,
            non_creditable_czk=non_creditable,
            review_status=review_status,
            review_note=review_note,
        )

        # Attach to item as dynamic attribute
        it.ftc_record = record  # type: ignore[attr-defined]

        # Update summary
        summary.records.append(record)
        summary.foreign_income_total_czk += gross
        summary.foreign_tax_paid_total_czk += wht_paid
        summary.foreign_tax_creditable_total_czk += actual_creditable
        summary.foreign_tax_non_creditable_total_czk += non_creditable

        # Per-country aggregate
        country_key = source_country or "UNKNOWN"
        if country_key not in summary.per_country:
            summary.per_country[country_key] = CzCountryCreditAggregate(country=country_key)
        agg = summary.per_country[country_key]
        agg.gross_income_czk += gross
        agg.foreign_tax_paid_czk += wht_paid
        agg.creditable_czk += actual_creditable
        agg.non_creditable_czk += non_creditable
        agg.item_count += 1

    return summary
