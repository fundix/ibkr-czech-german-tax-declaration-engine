# src/countries/cz/exporters/pdf_exporter.py
"""
PDF export for Czech tax results.

Renders a filing-support report ("podklady pro DAP") from a ``TaxResult``
with CZ-specific ``country_result`` data:

- DAP form mapping (official line refs from ``cz/form_mapping.py``)
- §10 netting overview (securities / options)
- §38f per-country foreign tax credit table
- item detail tables (disposals, options, dividends, interest)
- pending-review items and limitation notes

Czech diacritics require a font with Latin Extended-A glyphs — reportlab's
built-in Helvetica (WinAnsi) cannot render ě/ř/ů. The vendored DejaVu Sans
fonts in ``exporters/fonts/`` are registered on first use; if they are
missing the exporter falls back to Helvetica and strips diacritics so the
output stays readable instead of showing black boxes.
"""
from __future__ import annotations

import logging
import unicodedata
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, IO, List, Optional, Union

from src.countries.base import TaxResult

logger = logging.getLogger(__name__)

_FONTS_DIR = Path(__file__).parent / "fonts"
_FONT_BODY = "CzPdfSans"
_FONT_BOLD = "CzPdfSans-Bold"

ZERO = Decimal(0)
TWO = Decimal("0.01")


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_fonts_state: Dict[str, Any] = {"registered": False, "available": False}


def _ensure_fonts() -> bool:
    """Register vendored DejaVu fonts once; return availability."""
    if _fonts_state["registered"]:
        return _fonts_state["available"]
    _fonts_state["registered"] = True

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular = _FONTS_DIR / "DejaVuSans.ttf"
    bold = _FONTS_DIR / "DejaVuSans-Bold.ttf"
    try:
        pdfmetrics.registerFont(TTFont(_FONT_BODY, str(regular)))
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold)))
        _fonts_state["available"] = True
    except Exception as exc:  # missing/corrupt font files
        logger.warning(
            f"CZ PDF: DejaVu fonts unavailable ({exc}); falling back to "
            "Helvetica — Czech diacritics will be stripped."
        )
        _fonts_state["available"] = False
    return _fonts_state["available"]


def _strip_diacritics(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


# ---------------------------------------------------------------------------
# Czech formatting helpers
# ---------------------------------------------------------------------------

def _fmt_money(value: Optional[Decimal]) -> str:
    """Czech number format: 1 234 567,89 (non-breaking thousands space)."""
    if value is None:
        return ""
    q = value.quantize(TWO)
    sign = "-" if q < 0 else ""
    units, _, cents = f"{abs(q):.2f}".partition(".")
    groups = []
    while len(units) > 3:
        groups.insert(0, units[-3:])
        units = units[:-3]
    groups.insert(0, units)
    return f"{sign}{' '.join(groups)},{cents}"


def _fmt_qty(value: Optional[Decimal]) -> str:
    if value is None:
        return ""
    q = value.normalize()
    text = f"{q:f}"
    return text.replace(".", ",")


def _fmt_date(iso_date: Optional[str]) -> str:
    """ISO date -> DD.MM.YYYY; passthrough for anything unparseable."""
    if not iso_date:
        return ""
    try:
        return datetime.strptime(iso_date[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso_date


_FX_MODE_LABELS = {
    "DAILY": "denní kurzy ČNB",
    "UNIFORM": "jednotný kurz (GFŘ)",
}

_ITEM_TYPE_LABELS = {
    "SECURITY_DISPOSAL": "prodej CP",
    "OPTION_CLOSE": "uzavření opce",
    "OPTION_EXPIRY_WORTHLESS": "expirace opce",
    "OPTION_EXERCISE_ASSIGNMENT": "uplatnění/přiřazení",
    "DIVIDEND": "dividenda",
    "FUND_DISTRIBUTION": "distribuce fondu",
    "INTEREST": "úrok",
    "OTHER": "nespárovaná srážková daň",
}


def _item_regime(item: Any) -> str:
    """Short Czech label for the tax treatment of an item."""
    from src.countries.cz.tax_items import CzExemptionReason, CzTaxReviewStatus

    if item.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW:
        return "ke kontrole"
    if item.is_exempt:
        if item.exemption_reason == CzExemptionReason.TIME_TEST_PASSED:
            return "osvob. – časový test"
        if item.exemption_reason == CzExemptionReason.ANNUAL_LIMIT_NOT_EXCEEDED:
            return "osvob. – roční limit"
        return "osvobozeno"
    return "zdanitelné"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_cz_to_pdf(
    tax_result: TaxResult,
    output: Union[str, IO[bytes]],
    *,
    taxpayer_name: Optional[str] = None,
    account_id: Optional[str] = None,
) -> None:
    """
    Export a CZ ``TaxResult`` to a PDF report.

    Args:
        tax_result: The ``TaxResult`` from ``CzechTaxAggregator.aggregate()``.
        output: File path (str) or binary file-like object to write to.
        taxpayer_name: Optional name printed in the header.
        account_id: Optional IBKR account id printed in the header.
    """
    builder = _CzPdfBuilder(tax_result, taxpayer_name, account_id)
    builder.build(output)
    if isinstance(output, str):
        logger.info(f"CZ PDF export written to {output}")


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class _CzPdfBuilder:
    def __init__(
        self,
        tax_result: TaxResult,
        taxpayer_name: Optional[str],
        account_id: Optional[str],
    ) -> None:
        self.result = tax_result
        self.taxpayer_name = taxpayer_name
        self.account_id = account_id

        cr = tax_result.country_result or {}
        self.items: list = cr.get("items", [])
        self.netting = cr.get("netting")
        self.ftc_summary = cr.get("ftc_summary")
        self.form_mapping = cr.get("form_mapping")
        self.fx_policy = cr.get("fx_policy")
        self.currency = cr.get("currency", "EUR")
        self.has_czk = self.currency == "CZK"

        self.fonts_ok = _ensure_fonts()
        self.font_body = _FONT_BODY if self.fonts_ok else "Helvetica"
        self.font_bold = _FONT_BOLD if self.fonts_ok else "Helvetica-Bold"
        self.styles = self._make_styles()

    # -- text helper: strip diacritics when the fallback font is in use ----
    def _t(self, text: str) -> str:
        return text if self.fonts_ok else _strip_diacritics(text)

    def _make_styles(self) -> Dict[str, Any]:
        from reportlab.lib.enums import TA_JUSTIFY, TA_RIGHT
        from reportlab.lib.styles import ParagraphStyle

        body = ParagraphStyle(
            "CzBody", fontName=self.font_body, fontSize=9, leading=12,
            spaceAfter=4,
        )
        return {
            "title": ParagraphStyle(
                "CzTitle", parent=body, fontName=self.font_bold,
                fontSize=15, leading=19, spaceAfter=2,
            ),
            "subtitle": ParagraphStyle(
                "CzSubtitle", parent=body, fontSize=10, leading=13,
                spaceAfter=10, textColor="#444444",
            ),
            "h2": ParagraphStyle(
                "CzH2", parent=body, fontName=self.font_bold,
                fontSize=12, leading=15, spaceBefore=14, spaceAfter=6,
            ),
            "h3": ParagraphStyle(
                "CzH3", parent=body, fontName=self.font_bold,
                fontSize=10, leading=13, spaceBefore=10, spaceAfter=4,
            ),
            "body": body,
            "note": ParagraphStyle(
                "CzNote", parent=body, fontSize=8, leading=10,
                textColor="#555555", spaceAfter=2,
            ),
            "disclaimer": ParagraphStyle(
                "CzDisclaimer", parent=body, fontSize=8, leading=10,
                alignment=TA_JUSTIFY, textColor="#555555",
            ),
            "cell": ParagraphStyle(
                "CzCell", parent=body, fontSize=8, leading=10, spaceAfter=0,
            ),
            "cell_right": ParagraphStyle(
                "CzCellRight", parent=body, fontSize=8, leading=10,
                spaceAfter=0, alignment=TA_RIGHT,
            ),
            "cell_head": ParagraphStyle(
                "CzCellHead", parent=body, fontName=self.font_bold,
                fontSize=8, leading=10, spaceAfter=0,
            ),
        }

    # ------------------------------------------------------------------
    # Table helper
    # ------------------------------------------------------------------

    def _table(
        self,
        header: List[str],
        rows: List[List[Any]],
        col_widths: Optional[List[float]] = None,
        right_cols: Optional[List[int]] = None,
    ) -> Any:
        from reportlab.lib import colors
        from reportlab.platypus import Paragraph, Table, TableStyle

        right = set(right_cols or [])
        head = [Paragraph(self._t(h), self.styles["cell_head"]) for h in header]
        body = []
        for row in rows:
            cells = []
            for idx, value in enumerate(row):
                style = self.styles["cell_right" if idx in right else "cell"]
                cells.append(Paragraph(self._t(str(value)), style))
            body.append(cells)

        table = Table([head] + body, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf2")),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#8899aa")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f5f7f9")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return table

    def _meta_table(self, rows: List[List[str]]) -> Any:
        from reportlab.lib import colors
        from reportlab.platypus import Paragraph, Table, TableStyle

        body = [
            [
                Paragraph(self._t(key), self.styles["cell_head"]),
                Paragraph(self._t(value), self.styles["cell"]),
            ]
            for key, value in rows
        ]
        table = Table(body, colWidths=[130, 350])
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return table

    def _para(self, text: str, style: str) -> Any:
        from reportlab.platypus import Paragraph
        return Paragraph(self._t(text), self.styles[style])

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _header_block(self) -> List[Any]:
        from reportlab.platypus import Spacer

        year = self.result.tax_year
        fx_label = ""
        if self.fx_policy is not None:
            fx_label = _FX_MODE_LABELS.get(
                self.fx_policy.mode.name, self.fx_policy.mode.name
            )

        story: List[Any] = [
            self._para(
                f"Podklady pro přiznání k dani z příjmů fyzických osob — {year}",
                "title",
            ),
            self._para(
                "Příjmy z investic u Interactive Brokers (výstup enginu, není daňové poradenství)",
                "subtitle",
            ),
        ]

        meta_rows = [["Zdaňovací období", str(year)]]
        if self.taxpayer_name:
            meta_rows.append(["Poplatník", self.taxpayer_name])
        if self.account_id:
            meta_rows.append(["Účet IBKR", self.account_id])
        meta_rows.append(["Měna výstupu", self.currency])
        if fx_label:
            meta_rows.append(["Kurzový režim", fx_label])
        meta_rows.append(
            ["Vygenerováno", datetime.now().strftime("%d.%m.%Y %H:%M")]
        )
        story.append(self._meta_table(meta_rows))
        story.append(Spacer(0, 6))

        if not self.has_czk:
            story.append(self._para(
                "POZOR: běh bez CZK konverze — částky jsou v EUR a limity/sazby "
                "vázané na CZK nebyly uplatněny.", "note",
            ))
        return story

    def _form_mapping_block(self) -> List[Any]:
        story: List[Any] = []
        fm = self.form_mapping
        if fm is None:
            return story

        story.append(self._para("Přehled pro formulář DAP", "h2"))
        story.append(self._para(
            "Hodnoty pro ruční přepis do přiznání (DAP 25 5405 a přílohy). "
            "Čísla řádků odpovídají tiskopisům pro zdaňovací období 2025.",
            "note",
        ))

        for section in fm.sections:
            # Warnings are rendered once, at the end of the report.
            if section.section_id == "CZ_FORM_WARNINGS":
                continue
            if not section.lines and not section.notes:
                continue
            # Audit lines are item counts, not amounts.
            fmt = (
                (lambda v: str(int(v)))
                if section.section_id == "CZ_FORM_AUDIT" else _fmt_money
            )
            story.append(self._para(section.label, "h3"))
            if section.lines:
                rows = []
                for line in section.lines:
                    label = line.label
                    if line.note:
                        label = f"{label} — {line.note}"
                    rows.append([
                        label,
                        line.official_line_ref or "",
                        fmt(line.value),
                    ])
                story.append(self._table(
                    ["Položka", "Řádek formuláře", f"Hodnota ({fm.currency})"],
                    rows,
                    col_widths=[220, 175, 85],
                    right_cols=[2],
                ))
            for note in section.notes:
                story.append(self._para(f"Pozn.: {note}", "note"))
        return story

    def _netting_block(self) -> List[Any]:
        story: List[Any] = []
        n = self.netting
        if n is None:
            return story

        story.append(self._para(
            "Kompenzace zisků a ztrát (§10 ZDP)", "h2"))
        rows = [
            [
                "Cenné papíry",
                _fmt_money(n.securities.taxable_gains),
                _fmt_money(n.securities.taxable_losses),
                _fmt_money(n.securities.net_taxable),
                _fmt_money(n.securities.exempt_time_test_total),
                _fmt_money(n.securities.exempt_annual_limit_total),
                str(n.securities.item_count_total),
            ],
            [
                "Opce a deriváty",
                _fmt_money(n.options.taxable_gains),
                _fmt_money(n.options.taxable_losses),
                _fmt_money(n.options.net_taxable),
                "—",
                "—",
                str(n.options.item_count_total),
            ],
        ]
        story.append(self._table(
            ["Skupina", "Zdanitelné zisky", "Zdanitelné ztráty", "Netto",
             "Osvob. čas. test", "Osvob. roční limit", "Počet"],
            rows,
            col_widths=[85, 72, 72, 72, 72, 72, 40],
            right_cols=[1, 2, 3, 4, 5, 6],
        ))
        if n.annual_limit_applied:
            story.append(self._para(
                f"Roční limit příjmů uplatněn: úhrn příjmů z prodeje CP "
                f"{_fmt_money(n.annual_limit_eligible_proceeds)} "
                f"{self.currency} nepřekročil práh "
                f"{_fmt_money(n.annual_limit_threshold)} {self.currency}.",
                "note",
            ))
        return story

    def _ftc_block(self) -> List[Any]:
        story: List[Any] = []
        ftc = self.ftc_summary
        if ftc is None or not ftc.per_country:
            return story

        story.append(self._para(
            "Zápočet zahraniční daně po státech (§38f ZDP)", "h2"))
        story.append(self._para(
            "Podklad pro Přílohu 3 — samostatný list za každý stát "
            "(§38f odst. 8 ZDP).", "note",
        ))
        rows = []
        for code, agg in sorted(ftc.per_country.items()):
            rows.append([
                code,
                _fmt_money(agg.gross_income_czk),
                _fmt_money(agg.foreign_tax_paid_czk),
                _fmt_money(agg.creditable_czk),
                _fmt_money(agg.non_creditable_czk),
                str(agg.item_count),
            ])
        story.append(self._table(
            ["Stát", "Hrubý příjem", "Daň zaplacená", "Započitatelná (cap)",
             "Nezapočitatelná", "Počet"],
            rows,
            col_widths=[50, 90, 90, 100, 90, 50],
            right_cols=[1, 2, 3, 4, 5],
        ))
        return story

    # -- item details ---------------------------------------------------

    def _amount(self, item: Any, base: str) -> Optional[Decimal]:
        """Pick the CZK field when available, otherwise the EUR field."""
        suffix = "czk" if self.has_czk else "eur"
        return getattr(item, f"{base}_{suffix}", None)

    def _disposals_block(self) -> List[Any]:
        from src.countries.cz.tax_items import CzTaxItemType

        story: List[Any] = []
        disposals = [
            it for it in self.items
            if it.item_type == CzTaxItemType.SECURITY_DISPOSAL
        ]
        if not disposals:
            return story

        story.append(self._para(
            f"Prodeje cenných papírů — detail ({self.currency})", "h2"))
        rows = []
        for it in sorted(disposals, key=lambda x: (x.event_date, x.asset_symbol or "")):
            acq = _fmt_date(it.acquisition_date)
            if it.acquisition_date_estimated and acq:
                acq += " *"
            rows.append([
                it.asset_symbol or (it.asset_description or "")[:18],
                acq,
                _fmt_date(it.event_date),
                _fmt_qty(it.quantity),
                _fmt_money(self._amount(it, "cost_basis")),
                _fmt_money(self._amount(it, "proceeds")),
                _fmt_money(self._amount(it, "gain_loss")),
                _item_regime(it),
            ])
        story.append(self._table(
            ["Symbol", "Nabytí", "Prodej", "Ks", "Náklady", "Příjem",
             "Zisk/ztráta", "Režim"],
            rows,
            col_widths=[58, 56, 56, 40, 70, 70, 70, 80],
            right_cols=[3, 4, 5, 6],
        ))
        if any(it.acquisition_date_estimated for it in disposals):
            story.append(self._para(
                "* datum nabytí odhadnuto z počáteční pozice roku (skutečné "
                "datum nákupu není ve výpisech) — časový test vyžaduje ruční "
                "ověření.", "note",
            ))
        return story

    def _options_block(self) -> List[Any]:
        from src.countries.cz.tax_items import CzTaxItemType

        story: List[Any] = []
        options = [
            it for it in self.items
            if it.item_type in (
                CzTaxItemType.OPTION_CLOSE,
                CzTaxItemType.OPTION_EXPIRY_WORTHLESS,
                CzTaxItemType.OPTION_EXERCISE_ASSIGNMENT,
            )
        ]
        if not options:
            return story

        story.append(self._para(
            f"Opce a deriváty — detail ({self.currency})", "h2"))
        rows = []
        for it in sorted(options, key=lambda x: (x.event_date, x.asset_symbol or "")):
            rows.append([
                it.asset_symbol or (it.asset_description or "")[:24],
                _fmt_date(it.event_date),
                _ITEM_TYPE_LABELS.get(it.item_type.name, it.item_type.name),
                _fmt_money(self._amount(it, "gain_loss")),
                _item_regime(it),
            ])
        story.append(self._table(
            ["Symbol", "Datum", "Typ", "Zisk/ztráta", "Režim"],
            rows,
            col_widths=[150, 60, 110, 90, 90],
            right_cols=[3],
        ))
        return story

    def _income_block(self) -> List[Any]:
        from src.countries.cz.tax_items import CzTaxItemType

        story: List[Any] = []
        incomes = [
            it for it in self.items
            if it.item_type in (
                CzTaxItemType.DIVIDEND,
                CzTaxItemType.FUND_DISTRIBUTION,
                CzTaxItemType.INTEREST,
            )
        ]
        if not incomes:
            return story

        story.append(self._para(
            f"Dividendy a úroky — detail ({self.currency})", "h2"))
        rows = []
        for it in sorted(incomes, key=lambda x: (x.event_date, x.asset_symbol or "")):
            # WHT records carry only original-currency + CZK amounts, so the
            # column stays empty in the degraded EUR-only mode.
            wht = ZERO
            country = ""
            for rec in it.wht_records:
                if self.has_czk and rec.amount_czk is not None:
                    wht += rec.amount_czk
                if not country and rec.source_country:
                    country = rec.source_country
            creditable = None
            ftc_rec = getattr(it, "ftc_record", None)
            if ftc_rec is not None:
                creditable = ftc_rec.actual_creditable_czk
            rows.append([
                it.asset_symbol or (it.asset_description or "")[:18],
                _fmt_date(it.event_date),
                _ITEM_TYPE_LABELS.get(it.item_type.name, it.item_type.name),
                country,
                _fmt_money(self._amount(it, "amount")),
                _fmt_money(wht) if wht else "",
                _fmt_money(creditable) if creditable is not None else "",
            ])
        story.append(self._table(
            ["Symbol", "Datum", "Typ", "Stát", "Hrubý příjem",
             "Srážková daň", "Započitatelná"],
            rows,
            col_widths=[70, 56, 88, 36, 80, 80, 80],
            right_cols=[4, 5, 6],
        ))
        return story

    def _pending_block(self) -> List[Any]:
        from src.countries.cz.tax_items import CzTaxReviewStatus

        story: List[Any] = []
        pending = [
            it for it in self.items
            if it.tax_review_status == CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        ]
        if not pending:
            return story

        story.append(self._para(
            f"Položky vyžadující ruční kontrolu ({len(pending)})", "h2"))
        rows = []
        for it in sorted(pending, key=lambda x: (x.event_date, x.asset_symbol or "")):
            rows.append([
                it.asset_symbol or (it.asset_description or "")[:18],
                _fmt_date(it.event_date),
                _ITEM_TYPE_LABELS.get(it.item_type.name, it.item_type.name),
                it.tax_review_note or "",
            ])
        story.append(self._table(
            ["Symbol", "Datum", "Typ", "Poznámka"],
            rows,
            col_widths=[65, 56, 100, 259],
        ))
        return story

    def _warnings_block(self) -> List[Any]:
        story: List[Any] = []
        notes: List[str] = []
        lines: List[str] = []

        fm = self.form_mapping
        if fm is not None:
            warn_section = fm.get_section("CZ_FORM_WARNINGS")
            if warn_section is not None:
                notes.extend(warn_section.notes)
                for line in warn_section.lines:
                    text = f"{line.label}: {int(line.value)}"
                    if line.note:
                        text += f" — {line.note}"
                    lines.append(text)
            for note in fm.limitation_notes:
                if note not in notes:
                    notes.append(note)

        for section in self.result.sections.values():
            for note in section.notes:
                if note not in notes:
                    notes.append(note)

        if not notes and not lines:
            return story
        story.append(self._para("Upozornění a omezení", "h2"))
        for text in lines:
            story.append(self._para(f"• {text}", "disclaimer"))
        for note in notes:
            story.append(self._para(f"• {note}", "disclaimer"))
        return story

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def build(self, output: Union[str, IO[bytes]]) -> None:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate

        doc = SimpleDocTemplate(
            output,
            pagesize=A4,
            leftMargin=1.6 * cm,
            rightMargin=1.6 * cm,
            topMargin=1.5 * cm,
            bottomMargin=1.6 * cm,
            title=self._t(f"CZ podklady pro DAP {self.result.tax_year}"),
            author="ibkr-tax-declaration-engine",
        )

        story: List[Any] = []
        story.extend(self._header_block())
        story.extend(self._form_mapping_block())
        story.extend(self._netting_block())
        story.extend(self._ftc_block())
        story.extend(self._disposals_block())
        story.extend(self._options_block())
        story.extend(self._income_block())
        story.extend(self._pending_block())
        story.extend(self._warnings_block())

        doc.build(story, onFirstPage=self._page_footer,
                  onLaterPages=self._page_footer)

    def _page_footer(self, canvas: Any, doc: Any) -> None:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm

        width, _ = A4
        canvas.saveState()
        canvas.setFont(self.font_body, 7)
        canvas.setFillColorRGB(0.4, 0.4, 0.4)
        canvas.drawString(
            1.6 * cm, 0.9 * cm,
            self._t("Není daňové poradenství — čísla ověřte s daňovým poradcem."),
        )
        canvas.drawRightString(
            width - 1.6 * cm, 0.9 * cm,
            self._t(f"Strana {canvas.getPageNumber()}"),
        )
        canvas.restoreState()
