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
class CzCurrencyNetting:
    """The §10 currency result, which nets inside itself and floors at zero.

    Per a tax advisor on 2026-08-15: §10 odst. 5 calls an FX loss an expense
    and odst. 4 says that where a kind's expenses exceed its income the
    difference is disregarded. So losses DO net against gains inside the kind —
    across currencies, pairs and accounts of one taxpayer, since the "kind" is
    none of those — and the result never falls below zero.

    The unused part of a loss is not a tax loss. It does not reach securities
    or any other kind of §10, and it does not carry to another year. It is kept
    here only so the preparer can see what was disregarded.

    NOT in this bucket, deliberately: the disputed result of repaying borrowed
    currency, and FX on a demonstrably regulated European market — both need
    their own legal classification. The first is excluded by construction
    (``currency_gains`` never recognises it); the second cannot be told from
    these statements at all and would have to be carved out by hand.
    """
    gains: Decimal = ZERO             # sum of the positive results
    losses: Decimal = ZERO            # sum of the negative results, absolute
    raw_net: Decimal = ZERO           # gains - losses; may be negative
    net_taxable: Decimal = ZERO       # max(0, raw_net), or 0 when exempt
    unutilized_loss: Decimal = ZERO   # what §10/4 disregards
    #: The occasional-income exemption, tested on the SUM OF POSITIVE gains
    #: rather than the net and never reduced by losses.
    exempt_occasional: bool = False
    exemption_threshold: Decimal = ZERO
    pending_total: Decimal = ZERO
    item_count_total: int = 0
    item_count_taxable: int = 0
    item_count_pending: int = 0

    def compute_net(self, threshold: Decimal, exemption_enabled: bool) -> None:
        self.raw_net = self.gains - self.losses
        self.exemption_threshold = threshold
        # The threshold is a cliff on gross positive income, and it is tested
        # BEFORE netting: a loss neither lowers the income being tested nor
        # earns the exemption. Above the cliff the whole amount is taxable.
        self.exempt_occasional = bool(
            exemption_enabled and self.gains > ZERO and self.gains <= threshold
        )
        if self.exempt_occasional:
            self.net_taxable = ZERO
            self.unutilized_loss = ZERO
            return
        self.net_taxable = max(ZERO, self.raw_net)
        self.unutilized_loss = -self.raw_net if self.raw_net < ZERO else ZERO


@dataclass
class CzLossOffsettingResult:
    """Full §10 netting result with per-section detail and combined total."""
    securities: CzSectionNetting = field(default_factory=CzSectionNetting)
    options: CzSectionNetting = field(default_factory=CzSectionNetting)
    currency: CzCurrencyNetting = field(default_factory=CzCurrencyNetting)
    #: Threshold and switch for the currency exemption, set by the caller from
    #: CzTaxConfig before compute_combined().
    currency_exemption_threshold: Decimal = ZERO
    currency_exemption_enabled: bool = False
    combined_net_taxable: Decimal = ZERO
    # Annual limit audit
    annual_limit_applied: bool = False
    annual_limit_eligible_proceeds: Decimal = ZERO
    annual_limit_threshold: Decimal = ZERO

    def compute_combined(self) -> None:
        self.securities.compute_net()
        self.options.compute_net()
        self.currency.compute_net(self.currency_exemption_threshold,
                                  self.currency_exemption_enabled)
        # The currency kind contributes only its floored figure: its loss is
        # disregarded rather than allowed to reduce securities or options.
        self.combined_net_taxable = (
            self.securities.net_taxable
            + self.options.net_taxable
            + self.currency.net_taxable
        )

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

        # Currency (§10 FX). raw_net is reported beside the floored figure so
        # a disregarded loss stays visible instead of looking like a zero.
        d[f"fx_gains_{c}"] = self.currency.gains.quantize(TWO)
        d[f"fx_losses_{c}"] = self.currency.losses.quantize(TWO)
        d[f"fx_raw_net_{c}"] = self.currency.raw_net.quantize(TWO)
        d[f"fx_net_taxable_{c}"] = self.currency.net_taxable.quantize(TWO)
        d[f"fx_unutilized_loss_{c}"] = self.currency.unutilized_loss.quantize(TWO)
        d[f"fx_exemption_threshold_{c}"] = self.currency.exemption_threshold.quantize(TWO)
        d["fx_exempt_occasional"] = Decimal(1 if self.currency.exempt_occasional else 0)
        d["fx_item_count_total"] = Decimal(self.currency.item_count_total)
        d["fx_item_count_taxable"] = Decimal(self.currency.item_count_taxable)
        d["fx_item_count_pending"] = Decimal(self.currency.item_count_pending)

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

        elif it.section == CzTaxSection.CZ_10_CURRENCY:
            fx = result.currency
            fx.item_count_total += 1
            if it.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW:
                fx.item_count_pending += 1
                fx.pending_total += gl.copy_abs()
            if it.included_in_tax_base:
                fx.item_count_taxable += 1
                if gl >= ZERO:
                    fx.gains += gl
                else:
                    fx.losses += gl.copy_abs()

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
