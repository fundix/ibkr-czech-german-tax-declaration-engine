# src/countries/cz/tax_liability.py
"""
Czech tax liability computation (§16 ZDP rate application + §38f FTC finalization).

Takes the classified, netted, and FTC-preliminary data and computes:

1. **Partial tax bases** — dividends, interest, securities net, options net.
2. **Combined taxable base** — sum of all partial bases (floored at 0).
3. **Gross Czech tax** — applying 15 % / 23 % rates per configured threshold.
4. **FTC finalization** — ``final_creditable = min(preliminary_creditable,
   czech_tax_on_foreign_income)``.  Foreign income = dividends + interest
   (§8 income that generated the WHT).
5. **Net Czech tax after credit** — ``gross_tax - final_creditable``.

Policy assumptions (explicitly documented):
- The 23 % elevated rate applies to the portion of the COMBINED base that
  exceeds the configured threshold (default CZK 1 935 552 for 2024).
  In a real DAP this threshold applies to the taxpayer's TOTAL income from
  ALL sources, not just IBKR.  Since this plugin only sees IBKR data, the
  threshold is applied to the IBKR-only base — the user must adjust if
  they have other income.  A ``limitation_notes`` list documents this.
- FTC is limited to the Czech tax attributable to foreign income.  We
  approximate this as ``(foreign_income / combined_base) × gross_tax``
  (proportional method, §38f odst. 1 ZDP).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.foreign_tax_credit import CzForeignTaxCreditSummary
from src.countries.cz.loss_offsetting import CzLossOffsettingResult

ZERO = Decimal(0)
TWO = Decimal("0.01")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class CzTaxLiabilitySummary:
    """Full Czech tax liability computation result."""

    # --- Partial tax bases ---
    taxable_dividends: Decimal = ZERO
    taxable_interest: Decimal = ZERO
    taxable_securities_net: Decimal = ZERO
    taxable_options_net: Decimal = ZERO

    # --- Combined ---
    combined_taxable_base: Decimal = ZERO
    # §16/2 ZDP: base rounded down to whole hundreds CZK before applying
    # rates (equals combined_taxable_base in EUR mode).
    combined_taxable_base_rounded: Decimal = ZERO

    # --- Rate application ---
    base_rate: Decimal = Decimal("0.15")
    elevated_rate: Decimal = Decimal("0.23")
    threshold: Decimal = ZERO
    tax_at_base_rate: Decimal = ZERO
    base_for_base_rate: Decimal = ZERO
    tax_at_elevated_rate: Decimal = ZERO
    base_for_elevated_rate: Decimal = ZERO
    gross_czech_tax: Decimal = ZERO

    # --- FTC finalization ---
    foreign_income_total: Decimal = ZERO
    preliminary_ftc: Decimal = ZERO
    czech_tax_on_foreign_income: Decimal = ZERO
    final_creditable_ftc: Decimal = ZERO
    non_creditable_ftc: Decimal = ZERO

    # --- Final ---
    final_czech_tax_after_credit: Decimal = ZERO

    # --- Audit ---
    limitation_notes: List[str] = field(default_factory=list)

    def to_line_items(self, currency: str) -> Dict[str, Decimal]:
        c = currency.lower()
        return {
            f"taxable_dividends_{c}": self.taxable_dividends.quantize(TWO),
            f"taxable_interest_{c}": self.taxable_interest.quantize(TWO),
            f"taxable_securities_net_{c}": self.taxable_securities_net.quantize(TWO),
            f"taxable_options_net_{c}": self.taxable_options_net.quantize(TWO),
            f"combined_taxable_base_{c}": self.combined_taxable_base.quantize(TWO),
            f"base_for_base_rate_{c}": self.base_for_base_rate.quantize(TWO),
            f"tax_at_base_rate_{c}": self.tax_at_base_rate.quantize(TWO),
            f"base_for_elevated_rate_{c}": self.base_for_elevated_rate.quantize(TWO),
            f"tax_at_elevated_rate_{c}": self.tax_at_elevated_rate.quantize(TWO),
            f"gross_czech_tax_{c}": self.gross_czech_tax.quantize(TWO),
            f"foreign_income_total_{c}": self.foreign_income_total.quantize(TWO),
            f"preliminary_ftc_{c}": self.preliminary_ftc.quantize(TWO),
            f"czech_tax_on_foreign_income_{c}": self.czech_tax_on_foreign_income.quantize(TWO),
            f"final_creditable_ftc_{c}": self.final_creditable_ftc.quantize(TWO),
            f"non_creditable_ftc_{c}": self.non_creditable_ftc.quantize(TWO),
            f"final_czech_tax_after_credit_{c}": self.final_czech_tax_after_credit.quantize(TWO),
        }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def compute_tax_liability(
    taxable_dividends: Decimal,
    taxable_interest: Decimal,
    netting: CzLossOffsettingResult,
    ftc_summary: CzForeignTaxCreditSummary,
    config: CzTaxConfig,
    tax_year: Optional[int] = None,
    has_fx: bool = True,
) -> CzTaxLiabilitySummary:
    """
    Compute Czech tax liability from pre-aggregated figures.

    Args:
        taxable_dividends: Gross taxable dividends (CZK or EUR).
        taxable_interest: Gross taxable interest (CZK or EUR).
        netting: §10 loss-offsetting result.
        ftc_summary: Preliminary foreign tax credit summary.
        config: CZ tax plugin configuration.
        tax_year: Selects the statutory 23 % threshold for that year.
        has_fx: ``False`` when the plugin runs without an FX provider, i.e.
            the bases are EUR. The CZK-denominated 23 % threshold and the
            statutory CZK rounding are then skipped (diagnostic output only).

    Returns:
        ``CzTaxLiabilitySummary`` with full audit trail.
    """
    result = CzTaxLiabilitySummary()
    notes: List[str] = []

    # --- 1. Partial tax bases ---
    result.taxable_dividends = taxable_dividends
    result.taxable_interest = taxable_interest
    result.taxable_securities_net = max(ZERO, netting.securities.net_taxable)
    result.taxable_options_net = max(ZERO, netting.options.net_taxable)

    # NOTE: negative §10 net results are floored at 0 for tax base purposes.
    # Loss carryforward is NOT implemented.
    if netting.securities.net_taxable < ZERO:
        notes.append(
            f"§10 securities net loss {netting.securities.net_taxable} "
            "floored to 0 for tax base (loss carryforward not implemented)"
        )
    if netting.options.net_taxable < ZERO:
        notes.append(
            f"§10 options net loss {netting.options.net_taxable} "
            "floored to 0 for tax base (loss carryforward not implemented)"
        )

    # --- 2. Combined taxable base ---
    combined = (
        result.taxable_dividends
        + result.taxable_interest
        + result.taxable_securities_net
        + result.taxable_options_net
    )
    result.combined_taxable_base = max(ZERO, combined)

    # --- 3. Rate application ---
    result.base_rate = config.base_tax_rate
    result.elevated_rate = config.elevated_tax_rate
    result.threshold = config.elevated_rate_threshold_for_year(tax_year)

    if has_fx:
        # §16 odst. 2 ZDP: tax is computed from the base rounded DOWN to
        # whole hundreds of CZK.
        base = (result.combined_taxable_base / Decimal("100")).to_integral_value(
            rounding=ROUND_FLOOR
        ) * Decimal("100")
    else:
        base = result.combined_taxable_base
    result.combined_taxable_base_rounded = base

    if not has_fx:
        # EUR mode: the CZK threshold cannot be compared against a EUR base —
        # apply the base rate to everything and say so, instead of silently
        # never (or wrongly) triggering the 23 % bracket.
        result.base_for_base_rate = base
        result.base_for_elevated_rate = ZERO
        notes.append(
            "LIMITATION: no FX provider — bases are EUR, so the CZK 23% "
            "threshold and statutory CZK rounding are NOT applied. "
            "Figures are diagnostic only."
        )
    elif base <= result.threshold:
        result.base_for_base_rate = base
        result.base_for_elevated_rate = ZERO
    else:
        result.base_for_base_rate = result.threshold
        result.base_for_elevated_rate = base - result.threshold

    result.tax_at_base_rate = (
        result.base_for_base_rate * result.base_rate
    ).quantize(TWO, rounding=ROUND_HALF_UP)

    result.tax_at_elevated_rate = (
        result.base_for_elevated_rate * result.elevated_rate
    ).quantize(TWO, rounding=ROUND_HALF_UP)

    result.gross_czech_tax = result.tax_at_base_rate + result.tax_at_elevated_rate

    notes.append(
        "LIMITATION: elevated-rate threshold applies to taxpayer's TOTAL income. "
        "This computation only sees IBKR income — adjust threshold if other "
        "income sources exist."
    )

    # --- 4. FTC finalization (§38f proportional method) ---
    result.foreign_income_total = ftc_summary.foreign_income_total_czk
    result.preliminary_ftc = ftc_summary.foreign_tax_creditable_total_czk

    if result.combined_taxable_base > ZERO and result.foreign_income_total > ZERO:
        # Proportional: CZ tax attributable to foreign income
        foreign_ratio = result.foreign_income_total / result.combined_taxable_base
        # Cap ratio at 1.0 (foreign income cannot exceed total base)
        foreign_ratio = min(foreign_ratio, Decimal("1"))
        result.czech_tax_on_foreign_income = (
            result.gross_czech_tax * foreign_ratio
        ).quantize(TWO, rounding=ROUND_HALF_UP)
    else:
        result.czech_tax_on_foreign_income = ZERO

    # §38f odst. 8 ZDP: the simple-credit cap is computed FOR EACH STATE
    # SEPARATELY — a single aggregate cap would let excess credit from a
    # high-WHT state ride on another state's unused headroom. Falls back to
    # the aggregate method when no per-country breakdown is available.
    if ftc_summary.per_country and result.combined_taxable_base > ZERO:
        per_state_credit = ZERO
        for country in sorted(ftc_summary.per_country):
            agg = ftc_summary.per_country[country]
            if agg.gross_income_czk <= ZERO or agg.creditable_czk <= ZERO:
                continue
            state_ratio = min(
                agg.gross_income_czk / result.combined_taxable_base, Decimal("1")
            )
            state_cap = (result.gross_czech_tax * state_ratio).quantize(
                TWO, rounding=ROUND_HALF_UP
            )
            state_credit = min(agg.creditable_czk, state_cap)
            per_state_credit += state_credit
            if agg.creditable_czk > state_cap:
                notes.append(
                    f"FTC {country}: per-state cap (§38f/8) limits credit to "
                    f"{state_cap} (preliminary {agg.creditable_czk})"
                )
        result.final_creditable_ftc = min(
            per_state_credit, result.czech_tax_on_foreign_income
        )
    else:
        result.final_creditable_ftc = min(
            result.preliminary_ftc,
            result.czech_tax_on_foreign_income,
        )
    result.non_creditable_ftc = (
        ftc_summary.foreign_tax_paid_total_czk - result.final_creditable_ftc
    )

    if result.preliminary_ftc > result.czech_tax_on_foreign_income:
        notes.append(
            f"FTC capped by Czech tax on foreign income: preliminary "
            f"{result.preliminary_ftc} > CZ tax on foreign "
            f"{result.czech_tax_on_foreign_income} → credit limited to "
            f"{result.final_creditable_ftc}"
        )

    # --- 5. Final tax ---
    result.final_czech_tax_after_credit = max(
        ZERO, result.gross_czech_tax - result.final_creditable_ftc
    )
    if has_fx:
        # §146 odst. 1 daňového řádu: the tax is rounded UP to whole CZK.
        result.final_czech_tax_after_credit = (
            result.final_czech_tax_after_credit.to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    result.limitation_notes = notes
    return result
