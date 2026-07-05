# src/countries/cz/form_mapping.py
"""
Czech DAP-oriented form mapping layer.

Reads computed tax results (liability, netting, FTC) and assembles a
structured ``CzFormMappingResult`` oriented towards the Czech personal
income tax return (Přiznání k dani z příjmů fyzických osob).

This module does **not** compute any tax figures — it only reads
pre-computed data from ``CzTaxLiabilitySummary``, ``CzLossOffsettingResult``,
``CzForeignTaxCreditSummary``, and aggregated §8 totals.

Internal line codes (e.g. ``CZ_DAP_8_DIVIDENDS``) are stable identifiers
suitable for programmatic use.  ``official_line_ref`` values were verified
on 2026-07-03 against the official forms for tax period 2025:

- DAP 25 5405 MFin 5405 vzor č. 30 (2. oddíl ř. 36–45, 4. oddíl ř. 57–61)
- Příloha 2 – 25 5405/P2 vzor č. 21 (§10: tabulka druhů příjmů, ř. 207–209;
  druh D = prodej cenných papírů, druh F = jiné ostatní příjmy, kód „z" ve
  sloupci 5 pro příjmy ze zdrojů v zahraničí)
- Příloha 3 – 25 5405/P3 vzor č. 21 (§38f: ř. 321–330; metoda prostého
  zápočtu se podle §38f odst. 8 provádí za každý stát na samostatném listu)

The line numbering has been stable for years, but re-verify when a new
form vzor is published (source: financnisprava.gov.cz/assets/tiskopisy/).

Usage::

    mapping = build_form_mapping(liability, netting, ftc, div, interest, cur, items)
    for section in mapping.sections:
        for line in section.lines:
            print(f"{line.code}: {line.label} = {line.value}")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.countries.cz.foreign_tax_credit import CzForeignTaxCreditSummary
from src.countries.cz.loss_offsetting import CzLossOffsettingResult
from src.countries.cz.tax_items import CzTaxItem, CzTaxReviewStatus
from src.countries.cz.tax_liability import CzTaxLiabilitySummary

ZERO = Decimal(0)
TWO = Decimal("0.01")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class CzFormLine:
    """One line in a DAP-oriented form section."""
    code: str                              # stable internal code
    label: str                             # human-readable Czech label
    value: Decimal                         # amount in CZK (or EUR fallback)
    official_line_ref: Optional[str] = None  # e.g. "ř. 38" — None until verified
    note: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "value": str(self.value),
            "official_line_ref": self.official_line_ref,
            "note": self.note,
        }


@dataclass
class CzFormSection:
    """Logical section of the DAP form mapping."""
    section_id: str
    label: str
    lines: List[CzFormLine] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "section_id": self.section_id,
            "label": self.label,
            "lines": [ln.to_dict() for ln in self.lines],
            "notes": self.notes,
        }


@dataclass
class CzFormMappingResult:
    """Complete DAP-oriented form mapping."""
    sections: List[CzFormSection] = field(default_factory=list)
    currency: str = "CZK"
    limitation_notes: List[str] = field(default_factory=list)

    # Audit counts
    total_item_count: int = 0
    exempt_item_count: int = 0
    pending_item_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "currency": self.currency,
            "sections": [s.to_dict() for s in self.sections],
            "limitation_notes": self.limitation_notes,
            "total_item_count": self.total_item_count,
            "exempt_item_count": self.exempt_item_count,
            "pending_item_count": self.pending_item_count,
        }

    def get_section(self, section_id: str) -> Optional[CzFormSection]:
        for s in self.sections:
            if s.section_id == section_id:
                return s
        return None

    def get_line(self, code: str) -> Optional[CzFormLine]:
        for s in self.sections:
            for ln in s.lines:
                if ln.code == code:
                    return ln
        return None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_form_mapping(
    liability: Optional[CzTaxLiabilitySummary],
    netting: Optional[CzLossOffsettingResult],
    ftc_summary: Optional[CzForeignTaxCreditSummary],
    taxable_dividends: Decimal,
    taxable_interest: Decimal,
    currency: str,
    items: Optional[List[CzTaxItem]] = None,
) -> CzFormMappingResult:
    """
    Build a DAP-oriented form mapping from pre-computed tax results.

    All values are read-only from existing computations — no tax logic here.
    """
    q = lambda v: v.quantize(TWO) if v is not None else ZERO

    result = CzFormMappingResult(currency=currency)

    # --- Item counts ---
    if items:
        result.total_item_count = len(items)
        result.exempt_item_count = sum(1 for it in items if it.is_exempt)
        result.pending_item_count = sum(
            1 for it in items
            if it.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        )

    # =====================================================================
    # §8 — Příjmy z kapitálového majetku
    # =====================================================================
    sec8 = CzFormSection(
        section_id="CZ_FORM_SECTION_8",
        label="§8 ZDP – Příjmy z kapitálového majetku",
    )

    sec8.lines.append(CzFormLine(
        code="CZ_DAP_8_DIVIDENDS",
        label="Dividendy ze zahraničí (hrubý příjem)",
        value=q(taxable_dividends),
        official_line_ref="ř. 38 DAP (součást úhrnu §8)",
    ))
    sec8.lines.append(CzFormLine(
        code="CZ_DAP_8_INTEREST",
        label="Úroky ze zahraničí (hrubý příjem)",
        value=q(taxable_interest),
        official_line_ref="ř. 38 DAP (součást úhrnu §8)",
    ))
    sec8_total = (liability.taxable_dividends + liability.taxable_interest) if liability else (taxable_dividends + taxable_interest)
    sec8.lines.append(CzFormLine(
        code="CZ_DAP_8_TOTAL",
        label="Dílčí základ §8 celkem",
        value=q(sec8_total),
        official_line_ref="ř. 38 DAP",
    ))

    if ftc_summary and ftc_summary.foreign_income_total_czk > ZERO:
        sec8.lines.append(CzFormLine(
            code="CZ_DAP_8_FOREIGN_INCOME",
            label="Zahraniční příjmy pro zápočet daně",
            value=q(ftc_summary.foreign_income_total_czk),
            official_line_ref="Příloha 3, ř. 321 (za každý stát samostatný list)",
            note="Podklad pro §38f",
        ))

    result.sections.append(sec8)

    # =====================================================================
    # §10 — Ostatní příjmy
    # =====================================================================
    sec10 = CzFormSection(
        section_id="CZ_FORM_SECTION_10",
        label="§10 ZDP – Ostatní příjmy",
    )

    # Use liability's floored values when available (no recomputation)
    sec_net_raw = netting.securities.net_taxable if netting else ZERO
    opt_net_raw = netting.options.net_taxable if netting else ZERO
    sec_val = liability.taxable_securities_net if liability else max(ZERO, sec_net_raw)
    opt_val = liability.taxable_options_net if liability else max(ZERO, opt_net_raw)

    sec10.lines.append(CzFormLine(
        code="CZ_DAP_10_SECURITIES",
        label="Cenné papíry – čistý zdanitelný výsledek",
        value=q(sec_val),
        official_line_ref="Příloha 2, tabulka §10 – druh D (prodej cenných papírů), kód „z“",
        note="Po kompenzaci zisků a ztrát; záporný výsledek = 0 pro DZD" if sec_net_raw < ZERO else None,
    ))
    sec10.lines.append(CzFormLine(
        code="CZ_DAP_10_OPTIONS",
        label="Opce a deriváty – čistý zdanitelný výsledek",
        value=q(opt_val),
        official_line_ref="Příloha 2, tabulka §10 – druh F (jiné ostatní příjmy), kód „z“",
        note="Po kompenzaci zisků a ztrát; záporný výsledek = 0 pro DZD" if opt_net_raw < ZERO else None,
    ))
    sec10.lines.append(CzFormLine(
        code="CZ_DAP_10_TOTAL",
        label="Dílčí základ §10 celkem",
        value=q(sec_val + opt_val),
        official_line_ref="Příloha 2, ř. 209 → ř. 40 DAP",
    ))

    # Supporting / audit info
    if netting:
        sec10.lines.append(CzFormLine(
            code="CZ_DAP_10_EXEMPT_TIME_TEST",
            label="Osvobozeno – časový test (§4/1/w)",
            value=q(netting.securities.exempt_time_test_total),
            note="Pouze podklad; nezahrnuto v DZD",
        ))
        sec10.lines.append(CzFormLine(
            code="CZ_DAP_10_EXEMPT_ANNUAL_LIMIT",
            label="Osvobozeno – roční limit příjmů",
            value=q(netting.securities.exempt_annual_limit_total),
            note="Pouze podklad; nezahrnuto v DZD",
        ))

    sec10.notes.append("§10/4 expenses: acquisition costs and trade commissions are already reflected in cost basis / net proceeds per item; additional external expenses directly attributable to the sale (if any) must be added manually")
    result.sections.append(sec10)

    # =====================================================================
    # Daňová povinnost
    # =====================================================================
    sec_liability = CzFormSection(
        section_id="CZ_FORM_TAX_LIABILITY",
        label="Daňová povinnost",
    )

    if liability:
        sec_liability.lines.append(CzFormLine(
            code="CZ_DAP_TAXABLE_BASE",
            label="Základ daně celkem",
            value=q(liability.combined_taxable_base),
            official_line_ref="ř. 41–42 DAP (jen příjmy z IBKR, bez §6/§7/§9)",
        ))
        sec_liability.lines.append(CzFormLine(
            code="CZ_DAP_TAX_BASE_RATE",
            label=f"Daň ze základu do {liability.threshold} {currency} ({liability.base_rate*100:.0f} %)",
            value=q(liability.tax_at_base_rate),
            official_line_ref="ř. 57 DAP (součást výpočtu daně §16)",
        ))
        if liability.base_for_elevated_rate > ZERO:
            sec_liability.lines.append(CzFormLine(
                code="CZ_DAP_TAX_ELEVATED_RATE",
                label=f"Daň ze základu nad {liability.threshold} {currency} ({liability.elevated_rate*100:.0f} %)",
                value=q(liability.tax_at_elevated_rate),
                official_line_ref="ř. 57 DAP (součást výpočtu daně §16)",
            ))
        sec_liability.lines.append(CzFormLine(
            code="CZ_DAP_GROSS_TAX",
            label="Daň celkem (před zápočtem)",
            value=q(liability.gross_czech_tax),
            official_line_ref="ř. 57 DAP",
        ))
        sec_liability.lines.append(CzFormLine(
            code="CZ_DAP_FINAL_TAX",
            label="Daň po zápočtu zahraniční daně",
            value=q(liability.final_czech_tax_after_credit),
            official_line_ref="ř. 58 DAP (= ř. 330 Přílohy 3)",
        ))

        sec_liability.notes.extend(liability.limitation_notes)

    result.sections.append(sec_liability)

    # =====================================================================
    # Samostatný základ daně (§16a) — porovnání pro zahraniční dividendy
    # =====================================================================
    cmp = liability.dividend_separate_base if liability else None
    if cmp is not None and cmp.available:
        sec_sep = CzFormSection(
            section_id="CZ_FORM_DIVIDEND_SEPARATE_BASE",
            label="Samostatný základ daně §16a – zahraniční dividendy (porovnání)",
        )
        sec_sep.lines.append(CzFormLine(
            code="CZ_DAP_16A_GENERAL_TOTAL",
            label="Varianta A – dividendy v obecném základu: celková daň",
            value=q(cmp.general_base_total_tax),
            official_line_ref="ř. 58 DAP (dividendy součástí §8, ř. 38)",
        ))
        sec_sep.lines.append(CzFormLine(
            code="CZ_DAP_16A_SEPARATE_BASE",
            label="Varianta B – samostatný základ daně (dividendy)",
            value=q(cmp.separate_dividend_base_rounded),
            official_line_ref="DAP – samostatný základ daně §16a (ověřit řádek dle vzoru)",
            note=f"Sazba {cmp.separate_dividend_rate * 100:.0f} %",
        ))
        sec_sep.lines.append(CzFormLine(
            code="CZ_DAP_16A_SEPARATE_DIVIDEND_TAX",
            label="Varianta B – daň ze samostatného základu po zápočtu",
            value=q(cmp.separate_dividend_net_tax),
            note=f"Daň {q(cmp.separate_dividend_gross_tax)} − zápočet {q(cmp.separate_dividend_ftc)}",
        ))
        sec_sep.lines.append(CzFormLine(
            code="CZ_DAP_16A_SEPARATE_GENERAL_TAX",
            label="Varianta B – daň z obecného základu (bez dividend) po zápočtu",
            value=q(cmp.separate_general_base_net_tax),
        ))
        sec_sep.lines.append(CzFormLine(
            code="CZ_DAP_16A_SEPARATE_TOTAL",
            label="Varianta B – celková daň (samostatný + obecný základ)",
            value=q(cmp.separate_base_total_tax),
        ))
        sec_sep.lines.append(CzFormLine(
            code="CZ_DAP_16A_SAVING",
            label="Úspora varianty B oproti variantě A",
            value=q(cmp.saving),
        ))
        if cmp.recommended_mode == "separate":
            sec_sep.notes.append(
                f"DOPORUČENÍ: varianta B (samostatný základ §16a) je levnější o "
                f"{q(cmp.saving)} {currency}. Volba §16a je na poplatníkovi; při "
                f"jejím uplatnění dividendy nepatří do ř. 38 (§8), ale do "
                f"samostatného základu daně."
            )
        else:
            sec_sep.notes.append(
                "Samostatný základ daně §16a by nepřinesl úsporu (obecný základ "
                "nedosahuje sazby 23 %) — doporučena varianta A."
            )
        result.sections.append(sec_sep)

    # =====================================================================
    # §38f — Zápočet zahraniční daně
    # =====================================================================
    sec_ftc = CzFormSection(
        section_id="CZ_FORM_FOREIGN_TAX_CREDIT",
        label="§38f ZDP – Zápočet zahraniční daně",
    )

    if ftc_summary:
        sec_ftc.lines.append(CzFormLine(
            code="CZ_DAP_FTC_PAID",
            label="Zahraniční daň zaplacená celkem",
            value=q(ftc_summary.foreign_tax_paid_total_czk),
            note="Skutečně sražená daň; nad rámec smluvní sazby nelze uvést na ř. 323",
        ))
        sec_ftc.lines.append(CzFormLine(
            code="CZ_DAP_FTC_PRELIMINARY",
            label="Předběžný zápočet (per-item cap)",
            value=q(ftc_summary.foreign_tax_creditable_total_czk),
            official_line_ref="Příloha 3, ř. 323 (úhrn za všechny státy; daň max. dle SZDZ)",
        ))

    if liability:
        sec_ftc.lines.append(CzFormLine(
            code="CZ_DAP_FTC_CZ_TAX_ON_FOREIGN",
            label="Česká daň připadající na zahraniční příjmy",
            value=q(liability.czech_tax_on_foreign_income),
            official_line_ref="Příloha 3, ř. 325 (úhrn za všechny státy)",
            note="Proporční metoda (§38f odst. 1 ZDP)",
        ))
        sec_ftc.lines.append(CzFormLine(
            code="CZ_DAP_FTC_FINAL",
            label="Konečný zápočet zahraniční daně",
            value=q(liability.final_creditable_ftc),
            official_line_ref="Příloha 3, ř. 328",
        ))
        sec_ftc.lines.append(CzFormLine(
            code="CZ_DAP_FTC_NON_CREDITABLE",
            label="Nezapočitatelná zahraniční daň",
            value=q(liability.non_creditable_ftc),
            official_line_ref="Příloha 3, ř. 329",
        ))

    # Per-country breakdown
    if ftc_summary and ftc_summary.per_country:
        for code, agg in sorted(ftc_summary.per_country.items()):
            sec_ftc.lines.append(CzFormLine(
                code=f"CZ_DAP_FTC_COUNTRY_{code}",
                label=f"Země {code} – zaplaceno / započteno",
                value=q(agg.creditable_czk),
                official_line_ref=f"Příloha 3 – samostatný list za stát {code} (§38f odst. 8)",
                note=f"Zaplaceno {q(agg.foreign_tax_paid_czk)}, nezapočitatelné {q(agg.non_creditable_czk)}",
            ))

    result.sections.append(sec_ftc)

    # =====================================================================
    # Upozornění
    # =====================================================================
    sec_warnings = CzFormSection(
        section_id="CZ_FORM_WARNINGS",
        label="Upozornění a omezení",
    )

    if result.pending_item_count > 0:
        sec_warnings.lines.append(CzFormLine(
            code="CZ_DAP_WARN_PENDING",
            label="Položky vyžadující ruční kontrolu",
            value=Decimal(result.pending_item_count),
            note="Zkontrolujte items s tax_review_status=PENDING_MANUAL_REVIEW",
        ))

    sec_warnings.notes.extend([
        "Threshold pro zvýšenou sazbu se vztahuje na CELKOVÝ příjem poplatníka, "
        "nikoli jen na příjmy z IBKR. Pokud máte další příjmy, práh upravte.",
        "Sazby SZDZ (country_credit_caps) jsou ověřené pro portfoliové "
        "dividendy (12 států); na úrokové WHT se mohou vztahovat jiné "
        "smluvní sazby — objeví-li se, ověřte ručně.",
        "Čísla řádků odpovídají tiskopisům za zdaňovací období 2025 "
        "(DAP vzor č. 30, Příloha 2 vzor č. 21, Příloha 3 vzor č. 21) — "
        "při novém vzoru ověřte.",
        "Tento výstup NENÍ oficiální daňové přiznání. Slouží jako podklad "
        "pro ruční přepis do DAP nebo konzultaci s daňovým poradcem.",
    ])

    if liability and liability.limitation_notes:
        sec_warnings.notes.extend(liability.limitation_notes)

    result.sections.append(sec_warnings)

    # =====================================================================
    # Audit / přílohy
    # =====================================================================
    sec_audit = CzFormSection(
        section_id="CZ_FORM_AUDIT",
        label="Audit podklady",
    )
    sec_audit.lines.append(CzFormLine(
        code="CZ_DAP_AUDIT_TOTAL_ITEMS",
        label="Celkový počet položek",
        value=Decimal(result.total_item_count),
    ))
    sec_audit.lines.append(CzFormLine(
        code="CZ_DAP_AUDIT_EXEMPT_ITEMS",
        label="Osvobozené položky",
        value=Decimal(result.exempt_item_count),
    ))
    sec_audit.lines.append(CzFormLine(
        code="CZ_DAP_AUDIT_PENDING_ITEMS",
        label="Položky k ruční kontrole",
        value=Decimal(result.pending_item_count),
    ))
    sec_audit.notes.append("Podrobnosti v JSON/XLSX exportu")

    result.sections.append(sec_audit)

    # Propagate liability limitation notes to top-level
    if liability and liability.limitation_notes:
        result.limitation_notes = list(liability.limitation_notes)

    return result
