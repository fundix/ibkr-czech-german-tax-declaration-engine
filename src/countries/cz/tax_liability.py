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
    taxable_currency_net: Decimal = ZERO

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
    # Everything not credited here, for any reason: treaty_excess + uncredited.
    non_creditable_ftc: Decimal = ZERO
    # Withheld above the treaty rate. §38f odst. 5 counts foreign tax only up
    # to that rate, so this is never a Czech deduction — the source state has
    # to refund it. Must stay off ř. 329 and out of §24 odst. 2 písm. ch).
    treaty_excess_ftc: Decimal = ZERO
    # Treaty-eligible credit lost to the §38f/1 or §38f/8 caps. This is the
    # real ř. 329, and the part that §24 odst. 2 písm. ch) can carry.
    uncredited_ftc: Decimal = ZERO
    # What each state actually got AFTER both §38f caps, keyed by country code.
    # The per-country FTC aggregate holds the PRELIMINARY figure, which is what
    # the separate Příloha 3 sheets used to show — so they could exceed ř. 328.
    # Every state of that aggregate is here, with zero if it got nothing. In
    # whole hellers that sum exactly to final_creditable_ftc rounded to
    # hellers, i.e. to the credit as it is printed.
    per_state_credit: Dict[str, Decimal] = field(default_factory=dict)

    # --- Final ---
    final_czech_tax_after_credit: Decimal = ZERO

    # --- §16a separate-base comparison (foreign §8 income), advisory ---
    dividend_separate_base: Optional["CzDividendSeparateBaseComparison"] = None

    # --- Audit ---
    limitation_notes: List[str] = field(default_factory=list)

    @property
    def recommended_final_tax(self) -> Decimal:
        """Cheaper of the general-base and §16a separate-base total tax.

        Equals ``final_czech_tax_after_credit`` unless the §16a separate base
        for foreign §8 income is recommended: strictly cheaper, and by more
        than rounding (the election itself is the taxpayer's).
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
            f"taxable_currency_net_{c}": self.taxable_currency_net.quantize(TWO),
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
            f"treaty_excess_ftc_{c}": self.treaty_excess_ftc.quantize(TWO),
            f"uncredited_ftc_{c}": self.uncredited_ftc.quantize(TWO),
            f"final_czech_tax_after_credit_{c}": self.final_czech_tax_after_credit.quantize(TWO),
        }
        if self.dividend_separate_base is not None and self.dividend_separate_base.available:
            d.update(self.dividend_separate_base.to_line_items(currency))
        return d


@dataclass
class CzDividendSeparateBaseComparison:
    """§16a ZDP separate-base comparison for foreign §8 income.

    Foreign income from capital (§8 odst. 4 ZDP: profit shares, interest on
    deposits and account balances, ...) may, at the taxpayer's election, be
    taxed in a SEPARATE flat 15 % tax base (samostatný základ daně, §16a)
    instead of the general base. Advantageous once the general base reaches
    the 23 % bracket. This holds both scenarios' totals so the caller can pick
    the cheaper; the election itself is the taxpayer's.

    The election is all or nothing — §8 odst. 8 ZDP: "Zahrne-li se takový
    příjem do tohoto základu daně, zahrnou se do tohoto základu daně veškeré
    příjmy podle věty první" (read in the wording in force on 2024-01-01 and
    on 2026-11-27; the instructions to Příloha č. 4, form 25 5405/P4, repeat
    it). Foreign dividends therefore never move alone: the foreign interest
    goes with them, and so does the tax withheld on it.

    ``general`` scenario = the primary ``CzTaxLiabilitySummary`` (dividends
    and interest in the general base). ``separate`` scenario = foreign
    dividends AND interest taxed at 15 % in the separate base, the §10
    securities/options result in the general base.

    The ``dividend`` in the field and line-item names is older than that rule
    being applied here and is kept so stored runs and exports keep their keys;
    the ``separate_dividend_*`` figures describe the whole separate base.
    """
    available: bool = False
    dividends: Decimal = ZERO
    # Foreign interest, which moves into the separate base with the dividends.
    interest: Decimal = ZERO

    # General-base scenario total (dividends and interest in the general base).
    general_base_total_tax: Decimal = ZERO

    # Separate scenario — general base part (§10 only; all foreign §8 income
    # has left, and with it everything a foreign tax credit could be claimed
    # against, so the credit here is always zero)
    separate_general_base: Decimal = ZERO
    separate_general_base_tax: Decimal = ZERO
    separate_general_base_ftc: Decimal = ZERO
    separate_general_base_net_tax: Decimal = ZERO

    # Separate scenario — the §16a base: foreign dividends + foreign interest
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
    # general - separate when the election really saves tax, else 0 — also
    # when the separate total is lower through rounding alone (see
    # _compute_separate_base); the two totals above are never adjusted.
    saving: Decimal = ZERO
    # True when the two variants tax the same income at the same rate, so
    # their totals can differ through rounding only. A lower separate total
    # is then not a saving.
    same_tax_before_rounding: bool = False

    def to_line_items(self, currency: str) -> Dict[str, Decimal]:
        c = currency.lower()
        return {
            "sep_available": Decimal(1),
            f"sep_dividends_{c}": self.dividends.quantize(TWO),
            f"sep_interest_{c}": self.interest.quantize(TWO),
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
) -> tuple[Decimal, Decimal, Dict[str, Decimal]]:
    """Finalize the §38f simple credit for one tax base.

    Returns ``(czech_tax_on_foreign_income, final_creditable_ftc,
    per_state_credit)`` — the last being what each state actually got AFTER
    both caps, which is what its separate Příloha 3 sheet must show: one entry
    per state handed in (zero for a state that got nothing), in whole hellers
    that add up to the final credit rounded to hellers.

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

    per_state: Dict[str, Decimal] = {}
    if per_country and base > ZERO:
        per_state_credit = ZERO
        for country, gross_income, creditable in sorted(per_country):
            if gross_income <= ZERO or creditable <= ZERO:
                # Credited nothing, and recorded as such: a state left out
                # here is shown with its PRELIMINARY figure by whoever looks
                # it up, as if that had been credited.
                per_state[country] = ZERO
                continue
            state_ratio = min(gross_income / base, Decimal("1"))
            state_cap = (gross_tax * state_ratio).quantize(TWO, rounding=ROUND_HALF_UP)
            state_credit = min(creditable, state_cap)
            per_state[country] = state_credit
            per_state_credit += state_credit
            if creditable > state_cap:
                notes.append(
                    f"FTC {country}{label}: per-state cap (§38f/8) limits credit to "
                    f"{state_cap} (preliminary {creditable})"
                )
        final_creditable = min(per_state_credit, cz_tax_on_foreign)
        # The §38f/1 aggregate cap can bite on top of the per-state ones. When
        # it does, scale the states down proportionally so the separate sheets
        # still add up to ř. 328 — a form whose parts exceed its total is one
        # the taxpayer cannot file.
        if per_state_credit > final_creditable and per_state_credit > ZERO:
            notes.append(
                f"FTC{label}: §38f/1 limits the total to {final_creditable}; "
                "per-state amounts scaled proportionally to match."
            )
        # The states are reported in whole hellers that add up to the credit
        # as it is printed. Converted withholding seldom ends on a heller, and
        # states rounded one by one miss the total by one; this is also what
        # scales them down when the aggregate cap bit. The credit the tax is
        # computed from stays unrounded.
        per_state = _scale_to_total(per_state, final_creditable.quantize(TWO))
    else:
        final_creditable = min(preliminary_creditable, cz_tax_on_foreign)
        # No base to credit against: every state that was handed in got nothing.
        per_state = {country: ZERO for country, _, _ in per_country}
    return cz_tax_on_foreign, final_creditable, per_state


def _scale_to_total(amounts: Dict[str, Decimal], total: Decimal) -> Dict[str, Decimal]:
    """Scale *amounts* proportionally so they sum EXACTLY to *total*.

    Rounding each share independently would leave a heller of drift, and the
    per-state sheets have to reconcile against ř. 328 to the last one — so the
    largest share absorbs the residue rather than spreading it.
    """
    subtotal = sum(amounts.values(), ZERO)
    if subtotal <= ZERO:
        return dict(amounts)
    scaled = {
        code: (value / subtotal * total).quantize(TWO, rounding=ROUND_HALF_UP)
        for code, value in amounts.items()
    }
    residue = total - sum(scaled.values(), ZERO)
    if residue != ZERO:
        biggest = max(scaled, key=lambda c: scaled[c])
        scaled[biggest] += residue
    return scaled


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
    # Already floored (and possibly zeroed by the occasional-income exemption)
    # inside CzCurrencyNetting, because an FX loss is disregarded rather than
    # allowed to reduce another kind.
    result.taxable_currency_net = getattr(netting, "currency", None) and \
        netting.currency.net_taxable or ZERO

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
    fx = getattr(netting, "currency", None)
    if fx is not None and fx.unutilized_loss > ZERO:
        notes.append(
            f"§10 currency net loss {fx.raw_net} disregarded (§10 odst. 4 — "
            "where a kind's expenses exceed its income the difference is not "
            "taken into account); it does not reduce another kind and does not "
            "carry to another year"
        )
    if fx is not None and fx.exempt_occasional:
        notes.append(
            f"§10 currency gains {fx.gains} are exempt — the sum of POSITIVE "
            f"FX gains did not exceed {fx.exemption_threshold} "
            "(occasional income); the threshold is tested on gross gains, not "
            "on the net result"
        )

    # --- 2. Combined taxable base ---
    combined = (
        result.taxable_dividends
        + result.taxable_interest
        + result.taxable_securities_net
        + result.taxable_options_net
        + result.taxable_currency_net
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
    (result.czech_tax_on_foreign_income, result.final_creditable_ftc,
     result.per_state_credit) = _finalize_ftc(
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
    # Two unrelated reasons hide inside that total and only one is a Czech
    # matter. Splitting them exactly (the two parts still sum back to it) is
    # what keeps the treaty excess off ř. 329, where it would look like a
    # credit the taxpayer may carry forward instead of money to reclaim
    # abroad. Both are non-negative by construction: the per-item cap makes
    # preliminary <= paid, and the §38f caps make final <= preliminary.
    result.treaty_excess_ftc = (
        ftc_summary.foreign_tax_paid_total_czk - result.preliminary_ftc
    )
    result.uncredited_ftc = result.preliminary_ftc - result.final_creditable_ftc

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

    # --- 6. §16a separate-base comparison (advisory) ---
    # Offered for any foreign §8 income, not for dividends alone: interest is
    # just as much within the election, and a year with interest only can
    # reach the 23 % bracket too.
    foreign_capital_income = result.taxable_dividends + result.taxable_interest
    if config.dividend_separate_base_enabled and has_fx and foreign_capital_income > ZERO:
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

    Only reached in CZK mode with foreign §8 income. All of that income — the
    dividends and the interest — forms the flat-rate separate base, because
    the election cannot be made for a part of it (§8 odst. 8 ZDP). The general
    base keeps the §10 result only. Every item a foreign tax credit can be
    claimed for is §8 income, so the whole §38f credit follows it into the
    separate base and none is left for the general one.

    ASSUMPTION: every interest row is income the election covers (§8 odst. 4:
    interest on deposits and account balances, coupons). For interest on a
    loan (§8 odst. 1 písm. g)) that is less clear — the statute's list does
    not name it, the instructions to Příloha č. 4 mention it — and the
    statements do not let the engine tell it apart anyway.
    """
    cmp = CzDividendSeparateBaseComparison(
        available=True,
        dividends=result.taxable_dividends,
        interest=result.taxable_interest,
        general_base_total_tax=result.final_czech_tax_after_credit,
        separate_dividend_rate=config.dividend_separate_base_rate,
    )

    # Per-state §38f/8 cap notes from the alternative scenario are internal
    # detail — keep them out of the main audit trail (which describes the
    # general base) and let the single recommendation note below summarize.
    _sep_notes: List[str] = []

    # --- General base without the foreign §8 income (§10 only) ---
    gen_base_raw = max(
        ZERO,
        result.taxable_securities_net + result.taxable_options_net,
    )
    gen_base = _round_hundreds_down(gen_base_raw)
    cmp.separate_general_base = gen_base
    cmp.separate_general_base_tax = _apply_brackets(
        gen_base, result.threshold, result.base_rate, result.elevated_rate
    )
    cmp.separate_general_base_ftc = ZERO
    cmp.separate_general_base_net_tax = cmp.separate_general_base_tax

    # --- Separate base (§16a, flat rate): foreign dividends and interest ---
    sep_base_raw = max(ZERO, result.taxable_dividends + result.taxable_interest)
    sep_base = _round_hundreds_down(sep_base_raw)
    cmp.separate_dividend_base = sep_base_raw
    cmp.separate_dividend_base_rounded = sep_base
    cmp.separate_dividend_gross_tax = (
        sep_base * config.dividend_separate_base_rate
    ).quantize(TWO, rounding=ROUND_HALF_UP)
    state_pairs = [
        (c, a.gross_income_czk, a.creditable_czk)
        for c, a in ftc_summary.per_country.items()
    ]
    # The §38f ratio is income over the base AS IT STANDS — the convention of
    # the general path (Příloha č. 3, row 324: ř. 321 / ř. 42); only the tax
    # comes from the rounded base. Dividing by the rounded base here made a
    # state's limit the full rate on its income, credited more than the
    # general path would, and showed up as a "saving".
    #
    # One convention for both variants was the owner's decision of 2026-11-27,
    # following the audit, and it is the STRICTER reading: the annex for this
    # base (Příloha č. 4, form 25 5405/P4, row 413) allows the tax paid abroad
    # "maximálně do výše 15 % z částky uvedené na ř. 411" — the flat rate on
    # the income, which is what the rounded divisor gave. The scenario
    # therefore credits a few crowns less than that annex would, never more.
    _, cmp.separate_dividend_ftc, _ = _finalize_ftc(
        cmp.separate_dividend_gross_tax,
        sep_base_raw,
        ftc_summary.foreign_income_total_czk,
        ftc_summary.foreign_tax_creditable_total_czk,
        state_pairs,
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

    difference = cmp.general_base_total_tax - cmp.separate_base_total_tax
    # When every crown of the variant-A base is taxed at the separate-base
    # rate, the two variants are the same sum before rounding: the same income
    # at the same rate, and each state's §38f limit the same share of the same
    # tax. Variant B can then come out lower through rounding only — it rounds
    # two bases down to hundreds where A rounds one. Those few crowns are
    # neither what the election is for nor a reason to make it, so they are
    # not reported as a saving and not advised. Both totals stay as computed.
    #
    # That holds only while both variants carry the same general income. The
    # scenario above leaves the §10 currency result out of B's general base,
    # so with a taxable one the totals differ for that reason as well; the
    # difference is then not rounding and is left as it is.
    cmp.same_tax_before_rounding = (
        result.base_for_elevated_rate == ZERO
        and result.base_rate == config.dividend_separate_base_rate
        and result.taxable_currency_net == ZERO
    )
    # Prefer the general base on a tie so the default DAP layout (and existing
    # golden figures) stay unchanged unless the separate base is strictly better.
    if difference > ZERO and not cmp.same_tax_before_rounding:
        cmp.saving = difference
        cmp.recommended_mode = "separate"
    else:
        cmp.saving = ZERO
        cmp.recommended_mode = "general"

    if cmp.recommended_mode == "separate":
        notes.append(
            f"DOPORUČENÍ (§16a): zdanění zahraničních dividend a úroků v samostatném "
            f"základu daně (sazba {config.dividend_separate_base_rate * 100:.0f} %) "
            f"snižuje celkovou daň z {cmp.general_base_total_tax} na "
            f"{cmp.separate_base_total_tax} CZK (úspora {cmp.saving} CZK). "
            f"Tyto příjmy se pak nezahrnují do obecného základu (§8), ale do "
            f"samostatného základu daně dle §16a — všechny najednou, dividendy "
            f"i úroky (§8 odst. 8 ZDP). Volba je na poplatníkovi."
        )

    return cmp
