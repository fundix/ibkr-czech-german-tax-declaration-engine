# src/countries/cz/annual_limit.py
"""
Czech annual exempt limit evaluator for security disposal proceeds.

Implements the 2025+ amendment rule: if total gross disposal proceeds
(``proceeds_czk``) for eligible SECURITY_DISPOSAL items do not exceed
the configured threshold (default CZK 100 000), those items are exempt.

Key design decisions:
- **Metric used for the limit test**: ``proceeds_czk`` (gross disposal
  proceeds in CZK), NOT gain/loss.  This matches the legislative text
  which refers to "příjem" (income/proceeds), not "zisk" (profit).
- **Proceeds sum**: ALL ``SECURITY_DISPOSAL`` items with CZK proceeds,
  including those the time test already exempted. The threshold is tested
  on the year's total gross proceeds from transfers and only then is the
  holding period assessed per item — the two exemption titles cannot be
  combined, so time-test-exempt disposals must not be netted out of the sum.
- **Exemptible items**: of those, only the ones still taxable after the
  time test — an already-exempt item needs no second title.
- **Options**: NOT eligible (derivative instruments, not securities).
- **Dividends / Interest**: NOT eligible (§8 income, not §10 disposals).
- **All-or-nothing**: if total proceeds exceed the threshold, ALL eligible
  items remain taxable.  No partial exemption.

Run this evaluator AFTER ``evaluate_time_test()`` and BEFORE aggregation.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import List, Optional

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.tax_items import (
    CzExemptionReason,
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
)

logger = logging.getLogger(__name__)

# Item types eligible for the annual exempt limit
_ANNUAL_LIMIT_ELIGIBLE_TYPES = {
    CzTaxItemType.SECURITY_DISPOSAL,
}


def evaluate_annual_limit(
    items: List[CzTaxItem],
    config: CzTaxConfig,
    has_fx: bool = True,
    tax_year: Optional[int] = None,
) -> Decimal:
    """
    Evaluate the CZK annual exempt limit on *items* **in-place**.

    Returns the total disposal proceeds used for the limit test — all
    security disposals with CZK proceeds, time-test-exempt ones included
    (for audit / summary purposes).

    Precondition: ``evaluate_time_test()`` has already run on *items*.

    ``has_fx`` must be ``True`` for the exemption to be applied: the threshold
    is denominated in CZK, so it is only meaningful once ``proceeds_czk`` holds a
    real CZK figure.  When ``has_fx`` is ``False`` the pipeline runs in EUR mode
    and ``proceeds_czk`` actually carries EUR proceeds — comparing those to a
    100,000 CZK threshold would wrongly exempt (or tax) items on a unit mismatch,
    so the exemption is skipped and eligible items are kept taxable.
    """
    if not config.annual_exempt_limit_enabled:
        # Mark all eligible items as qualifies but not exempted
        for it in items:
            if _can_be_exempted(it):
                it.qualifies_for_annual_limit = True
        return Decimal(0)

    if not has_fx:
        # No FX converter: proceeds are in EUR, not CZK. Do NOT compare EUR
        # proceeds against a CZK threshold. Mark eligibility for audit but keep
        # items taxable (conservative — no exemption without a valid CZK amount).
        for it in items:
            if _can_be_exempted(it):
                it.qualifies_for_annual_limit = True
        logger.warning(
            "Annual exempt limit NOT applied: no FX converter configured, so "
            "proceeds are in EUR and cannot be compared to the CZK threshold. "
            "Eligible security disposals kept taxable."
        )
        return Decimal(0)

    threshold = config.annual_exempt_limit_czk
    ZERO = Decimal(0)

    # --- Phase 1: sum ALL disposal proceeds, then pick who the limit helps ---
    # The threshold is tested on the year's total gross proceeds from
    # security transfers — time-test-exempt disposals included. Only the
    # still-taxable ones can be flipped by this title.
    eligible: List[CzTaxItem] = []
    total_proceeds = ZERO

    for it in items:
        if _counts_toward_limit(it):
            total_proceeds += it.proceeds_czk

        if _can_be_exempted(it):
            it.qualifies_for_annual_limit = True
            eligible.append(it)

    # Disposals whose CZK proceeds are missing (failed FX conversion) make
    # the annual total unknowable: their proceeds are absent from the sum,
    # so granting the "≤ threshold" exemption to the remaining items could
    # exempt a year that is actually over the limit.
    fx_failed_disposals = [
        it for it in items
        if it.item_type in _ANNUAL_LIMIT_ELIGIBLE_TYPES
        and it.proceeds_czk is None
    ]

    if not eligible:
        # Nothing left for this title to exempt (e.g. every disposal already
        # passed the time test) — but still report the tested total.
        return total_proceeds

    # --- Phase 2: apply the all-or-nothing rule ---
    if total_proceeds <= threshold and fx_failed_disposals:
        # Total is under the threshold but incomplete — do NOT exempt.
        for it in eligible:
            it.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
            note = it.tax_review_note or ""
            it.tax_review_note = (
                f"{note + '; ' if note else ''}"
                f"Annual limit undeterminable: {len(fx_failed_disposals)} "
                f"disposal(s) missing CZK proceeds (FX conversion failed) — "
                f"known proceeds {total_proceeds} CZK ≤ {threshold} CZK, but "
                "the true total may exceed the limit. Exemption NOT granted; "
                "item kept taxable pending manual review."
            )
        logger.warning(
            f"Annual limit NOT applied: known proceeds {total_proceeds} CZK ≤ "
            f"{threshold} CZK but {len(fx_failed_disposals)} disposal(s) have "
            "no CZK proceeds (FX failed) — total unknowable, eligible items "
            "kept taxable and flagged for review."
        )
    elif total_proceeds <= threshold:
        # All eligible items are exempt
        for it in eligible:
            it.is_taxable = False
            it.is_exempt = True
            it.exempt_due_to_annual_limit = True
            it.exemption_reason = CzExemptionReason.ANNUAL_LIMIT_NOT_EXCEEDED
            it.included_in_tax_base = False
            it.tax_review_status = CzTaxReviewStatus.RESOLVED
            it.tax_review_note = (
                f"Exempt ({config.paragraph_4_citation('annual_limit', tax_year)}): "
                f"annual disposal proceeds {total_proceeds} CZK "
                f"≤ {threshold} CZK threshold"
            )
        logger.info(
            f"Annual limit: {len(eligible)} items exempt — "
            f"total proceeds {total_proceeds} CZK ≤ {threshold} CZK"
        )
    else:
        # All eligible items remain taxable — annotate for audit
        for it in eligible:
            # Don't overwrite tax_review_note if already set by time_test
            if it.tax_review_note is None or "annual" not in it.tax_review_note:
                note = it.tax_review_note or ""
                it.tax_review_note = (
                    f"{note + '; ' if note else ''}"
                    f"Annual limit exceeded: total proceeds {total_proceeds} CZK "
                    f"> {threshold} CZK threshold — item remains taxable"
                )
        logger.info(
            f"Annual limit: NOT applied — "
            f"total proceeds {total_proceeds} CZK > {threshold} CZK"
        )

    return total_proceeds


def evaluate_exempt_income_cap(
    items: List[CzTaxItem],
    config: CzTaxConfig,
    tax_year: int,
    has_fx: bool = True,
) -> Decimal:
    """§4/3 ZDP (2025+): cap on time-test-exempt income, evaluated in-place.

    When the sum of proceeds exempted by the time test exceeds the annual
    cap (40M CZK), the exemption applies only proportionally. The engine
    FLAGS all affected items ``PENDING_MANUAL_REVIEW`` with the computed
    ratio — the proportional mechanics (including the optional cost
    step-up) are left to the preparer.

    Returns the total time-test-exempt proceeds (audit figure).
    """
    ZERO = Decimal(0)
    if tax_year < config.exempt_income_cap_start_year or not has_fx:
        return ZERO

    exempt_items = [
        it for it in items
        if it.is_exempt
        and it.exemption_reason == CzExemptionReason.TIME_TEST_PASSED
        and it.proceeds_czk is not None
    ]
    total_exempt_proceeds = sum((it.proceeds_czk for it in exempt_items), ZERO)
    cap = config.exempt_income_cap_czk

    if total_exempt_proceeds > cap:
        ratio = (total_exempt_proceeds - cap) / total_exempt_proceeds
        for it in exempt_items:
            it.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
            note = it.tax_review_note or ""
            it.tax_review_note = (
                f"{note + '; ' if note else ''}"
                f"§4/3 ZDP cap exceeded: time-test-exempt proceeds total "
                f"{total_exempt_proceeds} CZK > {cap} CZK — approx. "
                f"{(ratio * 100).quantize(Decimal('0.01'))} % of the gain is "
                "NOT exempt (optional cost step-up may apply). Resolve manually."
            )
        logger.warning(
            f"§4/3 ZDP exemption cap exceeded: {total_exempt_proceeds} CZK "
            f"time-test-exempt proceeds > {cap} CZK — "
            f"{len(exempt_items)} item(s) flagged for manual review."
        )
    return total_exempt_proceeds


def _counts_toward_limit(it: CzTaxItem) -> bool:
    """Do this item's proceeds count toward the annual threshold?

    EVERY security disposal does — including ones the time test already
    exempted. The threshold is tested on the year's total gross proceeds
    from transfers; only then is the holding period assessed per item, and
    the two exemption titles cannot be combined. Excluding time-test-exempt
    disposals from the sum would understate the total and wrongly exempt a
    year that is actually over the limit.
    """
    return (
        it.item_type in _ANNUAL_LIMIT_ELIGIBLE_TYPES
        and it.proceeds_czk is not None
    )


def _can_be_exempted(it: CzTaxItem) -> bool:
    """Can the limit still change this item's outcome?

    Only disposals that are currently taxable: an item already exempt via
    the time test needs no second title, and one without CZK proceeds
    cannot participate in a CZK-denominated test.
    """
    return (
        it.item_type in _ANNUAL_LIMIT_ELIGIBLE_TYPES
        and it.is_taxable
        and it.included_in_tax_base
        and not it.is_exempt
        and it.proceeds_czk is not None
    )
