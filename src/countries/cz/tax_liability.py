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

    # --- §16a separate-base comparison (foreign dividends), advisory ---
    dividend_separate_base: Optional["CzDividendSeparateBaseComparison"] = None

    # --- Audit ---
    limitation_notes: List[str] = field(default_factory=list)

    @property
    def recommended_final_tax(self) -> Decimal:
        """Cheaper of the general-base and §16a separate-base total tax.

        Equals ``final_czech_tax_after_credit`` unless the §16a separate base
        for foreign dividends is strictly cheaper (the taxpayer's election).
        """
        cmp = self.dividend_separate_base
        if cmp is not None and cmp.available and cmp.recommended_mode == "separate":
            return cmp.separate_base_total_tax
        return self.final_czech_tax_after_credit

    def to_line_items(self, currency: str) -> Dict[str, Decimal]:
        c = currency.lower()
        d = {
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
        if self.dividend_separate_base is not None and self.dividend_separate_base.available:
            d.update(self.dividend_separate_base.to_line_items(currency))
        return d


@dataclass
class CzDividendSeparateBaseComparison:
    """§16a ZDP separate-base comparison for foreign dividends.

    Foreign dividends (§8 odst. 4 ZDP) may, at the taxpayer's election, be
    taxed in a SEPARATE flat 15 % tax base (samostatný základ daně, §16a)
    instead of the general base. Advantageous once the general base reaches
    the 23 % bracket. This holds both scenarios' totals so the caller can pick
    the cheaper; the election itself is the taxpayer's.

    ``general`` scenario = the primary ``CzTaxLiabilitySummary`` (dividends in
    the general base). ``separate`` scenario = dividends taxed at 15 % in the
    separate base, everything else (interest + §10 securities/options) in the
    general base.
    """
    available: bool = False
    dividends: Decimal = ZERO

    # General-base scenario total (dividends inside the general base).
    general_base_total_tax: Decimal = ZERO

    # Separate scenario — general base part (interest + §10, dividends removed)
    separate_general_base: Decimal = ZERO
    separate_general_base_tax: Decimal = ZERO
    separate_general_base_ftc: Decimal = ZERO
    separate_general_base_net_tax: Decimal = ZERO

    # Separate scenario — dividend §16a base part
    separate_dividend_base: Decimal = ZERO
    separate_dividend_base_rounded: Decimal = ZERO
    separate_dividend_rate: Decimal = Decimal("0.15")
    separate_dividend_gross_tax: Decimal = ZERO
    separate_dividend_ftc: Decimal = ZERO
    separate_dividend_net_tax: Decimal = ZERO

    # Combined separate-scenario total tax.
    separate_base_total_tax: Decimal = ZERO

    # Outcome
    recommended_mode: str = "general"   # "general" | "separate"
    saving: Decimal = ZERO              # general - separate, floored at 0

    def to_line_items(self, currency: str) -> Dict[str, Decimal]:
        c = currency.lower()
        return {
            "sep_available": Decimal(1),
            f"sep_dividends_{c}": self.dividends.quantize(TWO),
            f"sep_general_base_total_tax_{c}": self.general_base_total_tax.quantize(TWO),
            f"sep_general_base_{c}": self.separate_general_base.quantize(TWO),
            f"sep_general_base_net_tax_{c}": self.separate_general_base_net_tax.quantize(TWO),
            f"sep_dividend_base_{c}": self.separate_dividend_base_rounded.quantize(TWO),
            f"sep_dividend_gross_tax_{c}": self.separate_dividend_gross_tax.quantize(TWO),
            f"sep_dividend_ftc_{c}": self.separate_dividend_ftc.quantize(TWO),
            f"sep_dividend_net_tax_{c}": self.separate_dividend_net_tax.quantize(TWO),
            f"sep_total_tax_{c}": self.separate_base_total_tax.quantize(TWO),
            f"sep_saving_{c}": self.saving.quantize(TWO),
            "sep_recommended_separate": Decimal(1 if self.recommended_mode == "separate" else 0),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_hundreds_down(value: Decimal) -> Decimal:
    """§16/2 & §16a/2 ZDP: tax base rounded DOWN to whole hundreds of CZK."""
    return (value / Decimal("100")).to_integral_value(
        rounding=ROUND_FLOOR
    ) * Decimal("100")


def _apply_brackets(
    base: Decimal, threshold: Decimal, base_rate: Decimal, elevated_rate: Decimal
) -> Decimal:
    """§16 progressive rate: base_rate up to threshold, elevated_rate above."""
    if base <= threshold:
        low, high = base, ZERO
    else:
        low, high = threshold, base - threshold
    return (low * base_rate).quantize(TWO, rounding=ROUND_HALF_UP) + (
        high * elevated_rate
    ).quantize(TWO, rounding=ROUND_HALF_UP)


def _finalize_ftc(
    gross_tax: Decimal,
    base: Decimal,
    foreign_income: Decimal,
    preliminary_creditable: Decimal,
    per_country: List[tuple],
    notes: List[str],
    label: str = "",
) -> tuple[Decimal, Decimal]:
    """Finalize the §38f simple credit for one tax base.

    Returns ``(czech_tax_on_foreign_income, final_creditable_ftc)``.

    Applies the proportional §38f/1 cap (CZ tax attributable to foreign
    income) and, when a ``per_country`` breakdown ``[(code, gross, creditable)]``
    is supplied, the per-state §38f/8 cap; otherwise it falls back to the
    aggregate ``preliminary_creditable``. ``label`` disambiguates notes when
    the helper runs for more than one base (e.g. the §16a scenario).
    """
    if base > ZERO and foreign_income > ZERO:
        foreign_ratio = min(foreign_income / base, Decimal("1"))
        cz_tax_on_foreign = (gross_tax * foreign_ratio).quantize(
            TWO, rounding=ROUND_HALF_UP
        )
    else:
        cz_tax_on_foreign = ZERO

    if per_country and base > ZERO:
        per_state_credit = ZERO
        for country, gross_income, creditable in sorted(per_country):
            if gross_income <= ZERO or creditable <= ZERO:
                continue
            state_ratio = min(gross_income / base, Decimal("1"))
            state_cap = (gross_tax * state_ratio).quantize(TWO, rounding=ROUND_HALF_UP)
            state_credit = min(creditable, state_cap)
            per_state_credit += state_credit
            if creditable > state_cap:
                notes.append(
                    f"FTC {country}{label}: per-state cap (§38f/8) limits credit to "
                    f"{state_cap} (preliminary {creditable})"
                )
        final_creditable = min(per_state_credit, cz_tax_on_foreign)
    else:
        final_creditable = min(preliminary_creditable, cz_tax_on_foreign)
    return cz_tax_on_foreign, final_creditable


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

    # §38f odst. 8 ZDP: the simple-credit cap is computed FOR EACH STATE
    # SEPARATELY — a single aggregate cap would let excess credit from a
    # high-WHT state ride on another state's unused headroom. Falls back to
    # the aggregate method when no per-country breakdown is available.
    all_country_pairs = [
        (c, a.gross_income_czk, a.creditable_czk)
        for c, a in ftc_summary.per_country.items()
    ]
    result.czech_tax_on_foreign_income, result.final_creditable_ftc = _finalize_ftc(
        result.gross_czech_tax,
        result.combined_taxable_base,
        result.foreign_income_total,
        result.preliminary_ftc,
        all_country_pairs,
        notes,
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

    # --- 6. §16a separate dividend base comparison (advisory) ---
    if config.dividend_separate_base_enabled and has_fx and result.taxable_dividends > ZERO:
        result.dividend_separate_base = _compute_separate_base(
            result, ftc_summary, config, notes
        )

    result.limitation_notes = notes
    return result


def _compute_separate_base(
    result: CzTaxLiabilitySummary,
    ftc_summary: CzForeignTaxCreditSummary,
    config: CzTaxConfig,
    notes: List[str],
) -> CzDividendSeparateBaseComparison:
    """Compute the §16a separate-base scenario and pick the cheaper of the two.

    Only reached in CZK mode with non-zero foreign dividends. The general base
    then excludes dividends (interest + §10 securities/options only); the
    dividends form a flat-rate separate base (§16a). The §38f credit is split
    by income category so each base credits its own foreign tax.
    """
    cmp = CzDividendSeparateBaseComparison(
        available=True,
        dividends=result.taxable_dividends,
        general_base_total_tax=result.final_czech_tax_after_credit,
        separate_dividend_rate=config.dividend_separate_base_rate,
    )

    # Per-state §38f/8 cap notes from the alternative scenario are internal
    # detail — keep them out of the main audit trail (which describes the
    # general base) and let the single recommendation note below summarize.
    _sep_notes: List[str] = []

    # --- General base without dividends (interest + §10) ---
    gen_base_raw = max(
        ZERO,
        result.taxable_interest
        + result.taxable_securities_net
        + result.taxable_options_net,
    )
    gen_base = _round_hundreds_down(gen_base_raw)
    cmp.separate_general_base = gen_base
    cmp.separate_general_base_tax = _apply_brackets(
        gen_base, result.threshold, result.base_rate, result.elevated_rate
    )
    interest_pairs = [
        (c, a.interest_gross_income_czk, a.interest_creditable_czk)
        for c, a in ftc_summary.per_country.items()
    ]
    _, cmp.separate_general_base_ftc = _finalize_ftc(
        cmp.separate_general_base_tax,
        gen_base,
        ftc_summary.interest_income_total_czk,
        ftc_summary.interest_creditable_total_czk,
        interest_pairs,
        _sep_notes,
        label=" (samostatný scénář, obecný základ)",
    )
    cmp.separate_general_base_net_tax = max(
        ZERO, cmp.separate_general_base_tax - cmp.separate_general_base_ftc
    )

    # --- Separate dividend base (§16a, flat rate) ---
    div_base = _round_hundreds_down(result.taxable_dividends)
    cmp.separate_dividend_base = result.taxable_dividends
    cmp.separate_dividend_base_rounded = div_base
    cmp.separate_dividend_gross_tax = (
        div_base * config.dividend_separate_base_rate
    ).quantize(TWO, rounding=ROUND_HALF_UP)
    dividend_pairs = [
        (c, a.dividend_gross_income_czk, a.dividend_creditable_czk)
        for c, a in ftc_summary.per_country.items()
    ]
    _, cmp.separate_dividend_ftc = _finalize_ftc(
        cmp.separate_dividend_gross_tax,
        div_base,
        ftc_summary.dividend_income_total_czk,
        ftc_summary.dividend_creditable_total_czk,
        dividend_pairs,
        _sep_notes,
        label=" (samostatný základ §16a)",
    )
    cmp.separate_dividend_net_tax = max(
        ZERO, cmp.separate_dividend_gross_tax - cmp.separate_dividend_ftc
    )

    # --- Combined separate-scenario total (rounded up to whole CZK, §146 DŘ) ---
    cmp.separate_base_total_tax = (
        cmp.separate_general_base_net_tax + cmp.separate_dividend_net_tax
    ).to_integral_value(rounding=ROUND_CEILING)

    cmp.saving = max(ZERO, cmp.general_base_total_tax - cmp.separate_base_total_tax)
    # Prefer the general base on a tie so the default DAP layout (and existing
    # golden figures) stay unchanged unless the separate base is strictly better.
    cmp.recommended_mode = "separate" if cmp.saving > ZERO else "general"

    if cmp.recommended_mode == "separate":
        notes.append(
            f"DOPORUČENÍ (§16a): zdanění zahraničních dividend v samostatném "
            f"základu daně (sazba {config.dividend_separate_base_rate * 100:.0f} %) "
            f"snižuje celkovou daň z {cmp.general_base_total_tax} na "
            f"{cmp.separate_base_total_tax} CZK (úspora {cmp.saving} CZK). "
            f"Dividendy se pak nezahrnují do obecného základu (§8), ale do "
            f"samostatného základu daně dle §16a. Volba je na poplatníkovi."
        )

    return cmp
