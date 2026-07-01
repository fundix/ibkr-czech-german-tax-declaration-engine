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
- **Eligible items**: only ``SECURITY_DISPOSAL`` items that are currently
  ``is_taxable=True`` and ``included_in_tax_base=True`` after time-test.
  Items already exempt via time test are excluded from the proceeds sum.
- **Options**: NOT eligible (derivative instruments, not securities).
- **Dividends / Interest**: NOT eligible (§8 income, not §10 disposals).
- **All-or-nothing**: if total proceeds exceed the threshold, ALL eligible
  items remain taxable.  No partial exemption.

Run this evaluator AFTER ``evaluate_time_test()`` and BEFORE aggregation.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import List

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
) -> Decimal:
    """
    Evaluate the CZK annual exempt limit on *items* **in-place**.

    Returns the total eligible proceeds used for the limit test
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
            if _is_eligible(it):
                it.qualifies_for_annual_limit = True
        return Decimal(0)

    if not has_fx:
        # No FX converter: proceeds are in EUR, not CZK. Do NOT compare EUR
        # proceeds against a CZK threshold. Mark eligibility for audit but keep
        # items taxable (conservative — no exemption without a valid CZK amount).
        for it in items:
            if _is_eligible(it):
                it.qualifies_for_annual_limit = True
        logger.warning(
            "Annual exempt limit NOT applied: no FX converter configured, so "
            "proceeds are in EUR and cannot be compared to the CZK threshold. "
            "Eligible security disposals kept taxable."
        )
        return Decimal(0)

    threshold = config.annual_exempt_limit_czk
    ZERO = Decimal(0)

    # --- Phase 1: identify eligible items and sum proceeds ---
    eligible: List[CzTaxItem] = []
    total_proceeds = ZERO

    for it in items:
        if not _is_eligible(it):
            continue

        it.qualifies_for_annual_limit = True
        proceeds = it.proceeds_czk if it.proceeds_czk is not None else ZERO
        total_proceeds += proceeds
        eligible.append(it)

    if not eligible:
        return ZERO

    # --- Phase 2: apply the all-or-nothing rule ---
    if total_proceeds <= threshold:
        # All eligible items are exempt
        for it in eligible:
            it.is_taxable = False
            it.is_exempt = True
            it.exempt_due_to_annual_limit = True
            it.exemption_reason = CzExemptionReason.ANNUAL_LIMIT_NOT_EXCEEDED
            it.included_in_tax_base = False
            it.tax_review_status = CzTaxReviewStatus.RESOLVED
            it.tax_review_note = (
                f"Exempt: annual disposal proceeds {total_proceeds} CZK "
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


def _is_eligible(it: CzTaxItem) -> bool:
    """Is this item eligible for the annual exempt limit test?

    Eligible = SECURITY_DISPOSAL + currently taxable + included in tax base
    + has non-None proceeds_czk (items without CZK conversion cannot
    participate in a CZK-denominated limit test).
    Items already exempt (e.g. time test) are NOT eligible.
    """
    return (
        it.item_type in _ANNUAL_LIMIT_ELIGIBLE_TYPES
        and it.is_taxable
        and it.included_in_tax_base
        and not it.is_exempt
        and it.proceeds_czk is not None
    )
