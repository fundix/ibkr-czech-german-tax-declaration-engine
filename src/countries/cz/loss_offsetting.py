# src/countries/cz/loss_offsetting.py
"""
Czech §10 loss offsetting (kompenzace zisků a ztrát).

Nets taxable gains against taxable losses for items that are
``included_in_tax_base=True``.  Exempt and pending items are
tracked separately for audit.

Run AFTER ``evaluate_time_test()`` and ``evaluate_annual_limit()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List

from src.countries.cz.enums import CzTaxSection
from src.countries.cz.tax_items import CzTaxItem, CzTaxReviewStatus

ZERO = Decimal(0)
TWO = Decimal("0.01")


@dataclass
class CzSectionNetting:
    """Netting result for one §10 sub-section (securities or options)."""
    taxable_gains: Decimal = ZERO
    taxable_losses: Decimal = ZERO  # positive absolute value
    net_taxable: Decimal = ZERO
    exempt_time_test_total: Decimal = ZERO
    exempt_annual_limit_total: Decimal = ZERO
    pending_total: Decimal = ZERO
    item_count_total: int = 0
    item_count_taxable: int = 0
    item_count_exempt: int = 0
    item_count_pending: int = 0

    def compute_net(self) -> None:
        self.net_taxable = self.taxable_gains - self.taxable_losses


@dataclass
class CzLossOffsettingResult:
    """Full §10 netting result with per-section detail and combined total."""
    securities: CzSectionNetting = field(default_factory=CzSectionNetting)
    options: CzSectionNetting = field(default_factory=CzSectionNetting)
    combined_net_taxable: Decimal = ZERO
    # Annual limit audit
    annual_limit_applied: bool = False
    annual_limit_eligible_proceeds: Decimal = ZERO
    annual_limit_threshold: Decimal = ZERO

    def compute_combined(self) -> None:
        self.securities.compute_net()
        self.options.compute_net()
        self.combined_net_taxable = self.securities.net_taxable + self.options.net_taxable

    def to_line_items(self, currency: str) -> Dict[str, Decimal]:
        """Flat dict of all netting figures for TaxResult line_items."""
        c = currency.lower()
        d: Dict[str, Decimal] = {}

        # Securities
        d[f"sec_taxable_gains_{c}"] = self.securities.taxable_gains.quantize(TWO)
        d[f"sec_taxable_losses_{c}"] = self.securities.taxable_losses.quantize(TWO)
        d[f"sec_net_taxable_{c}"] = self.securities.net_taxable.quantize(TWO)
        d[f"sec_exempt_time_test_{c}"] = self.securities.exempt_time_test_total.quantize(TWO)
        d[f"sec_exempt_annual_limit_{c}"] = self.securities.exempt_annual_limit_total.quantize(TWO)
        d[f"sec_pending_{c}"] = self.securities.pending_total.quantize(TWO)
        d["sec_item_count_total"] = Decimal(self.securities.item_count_total)
        d["sec_item_count_taxable"] = Decimal(self.securities.item_count_taxable)
        d["sec_item_count_exempt"] = Decimal(self.securities.item_count_exempt)
        d["sec_item_count_pending"] = Decimal(self.securities.item_count_pending)

        # Options
        d[f"opt_taxable_gains_{c}"] = self.options.taxable_gains.quantize(TWO)
        d[f"opt_taxable_losses_{c}"] = self.options.taxable_losses.quantize(TWO)
        d[f"opt_net_taxable_{c}"] = self.options.net_taxable.quantize(TWO)
        d["opt_item_count"] = Decimal(self.options.item_count_total)

        # Combined
        d[f"combined_net_taxable_{c}"] = self.combined_net_taxable.quantize(TWO)

        # Annual limit audit
        d["annual_limit_applied"] = Decimal(1 if self.annual_limit_applied else 0)
        d[f"annual_limit_eligible_proceeds_{c}"] = self.annual_limit_eligible_proceeds.quantize(TWO)
        d[f"annual_limit_threshold_{c}"] = self.annual_limit_threshold.quantize(TWO)

        return d


def compute_loss_offsetting(
    items: List[CzTaxItem],
    has_fx: bool,
) -> CzLossOffsettingResult:
    """
    Compute §10 loss offsetting from classified ``CzTaxItem`` list.

    Only items with ``included_in_tax_base=True`` contribute to
    taxable gains/losses.  Exempt and pending items are tracked
    separately.
    """
    result = CzLossOffsettingResult()

    for it in items:
        gl = (it.gain_loss_czk if has_fx else it.gain_loss_eur) or ZERO

        if it.section == CzTaxSection.CZ_10_SECURITIES:
            sec = result.securities
            sec.item_count_total += 1

            if it.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW:
                sec.item_count_pending += 1
                sec.pending_total += gl.copy_abs()
                # Conservative defaults are asymmetric: a pending GAIN stays
                # in the tax base, but a pending LOSS must NOT reduce it —
                # if the position actually passed the time test, the loss
                # belongs to exempt income and cannot be claimed (§10 nets
                # losses only within taxable income). The amount stays
                # visible via pending_total for manual review.
                if it.included_in_tax_base and gl >= ZERO:
                    sec.taxable_gains += gl
                    sec.item_count_taxable += 1

            elif it.is_exempt:
                sec.item_count_exempt += 1
                if it.exempt_due_to_annual_limit:
                    sec.exempt_annual_limit_total += gl.copy_abs()
                else:
                    sec.exempt_time_test_total += gl.copy_abs()

            elif it.included_in_tax_base:
                sec.item_count_taxable += 1
                if gl >= ZERO:
                    sec.taxable_gains += gl
                else:
                    sec.taxable_losses += gl.copy_abs()

        elif it.section == CzTaxSection.CZ_10_OPTIONS:
            opt = result.options
            opt.item_count_total += 1

            if it.included_in_tax_base:
                if gl >= ZERO:
                    opt.taxable_gains += gl
                else:
                    opt.taxable_losses += gl.copy_abs()

    result.compute_combined()
    return result
