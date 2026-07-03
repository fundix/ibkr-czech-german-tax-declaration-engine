# tests/test_cz_pdf_exporter.py
"""
Tests for the CZ PDF exporter.

Covers:
1. PDF is generated and parseable (pymupdf), with Czech diacritics intact
2. DAP form-mapping lines with official refs are present
3. §10 netting, §38f per-country table and item details are rendered
4. Exempt / pending regimes are labelled
5. Czech number and date formatting helpers
6. Empty result and file-like output don't crash
"""
import io
import os
import tempfile
from decimal import Decimal

import pytest

fitz = pytest.importorskip("fitz")  # pymupdf — dev dependency

from src.countries.base import TaxResult
from src.countries.cz.exporters.pdf_exporter import (
    _fmt_date,
    _fmt_money,
    export_cz_to_pdf,
)
from tests.test_cz_exporters import _build_test_result


def _pdf_text(result: TaxResult, **kwargs) -> str:
    buf = io.BytesIO()
    export_cz_to_pdf(result, buf, **kwargs)
    buf.seek(0)
    doc = fitz.open(stream=buf.read(), filetype="pdf")
    raw = "\n".join(page.get_text() for page in doc)
    # Table cells wrap long labels — normalise whitespace so assertions
    # match phrases across line breaks.
    return " ".join(raw.split())


@pytest.fixture(scope="module")
def pdf_text() -> str:
    return _pdf_text(
        _build_test_result(),
        taxpayer_name="Jan Novák",
        account_id="U1234567",
    )


class TestPdfContent:
    def test_header_and_diacritics(self, pdf_text):
        # Diacritics survive extraction => the embedded font has the glyphs
        assert "Podklady pro přiznání k dani z příjmů fyzických osob" in pdf_text
        assert "Jan Novák" in pdf_text
        assert "U1234567" in pdf_text
        assert "denní kurzy ČNB" in pdf_text

    def test_form_mapping_with_official_refs(self, pdf_text):
        assert "Přehled pro formulář DAP" in pdf_text
        assert "ř. 38 DAP" in pdf_text
        assert "Příloha 2" in pdf_text
        assert "Příloha 3" in pdf_text
        assert "Dílčí základ §8 celkem" in pdf_text

    def test_netting_section(self, pdf_text):
        assert "Kompenzace zisků a ztrát (§10 ZDP)" in pdf_text
        assert "Cenné papíry" in pdf_text
        assert "Opce a deriváty" in pdf_text

    def test_ftc_per_country_table(self, pdf_text):
        assert "Zápočet zahraniční daně po státech" in pdf_text
        assert "US" in pdf_text

    def test_item_details_and_regimes(self, pdf_text):
        assert "Prodeje cenných papírů — detail" in pdf_text
        assert "osvob. – časový test" in pdf_text     # 1200-day holding
        assert "zdanitelné" in pdf_text
        assert "Dividendy a úroky — detail" in pdf_text
        assert "dividenda" in pdf_text
        assert "úrok" in pdf_text

    def test_pending_review_section(self, pdf_text):
        assert "Položky vyžadující ruční kontrolu" in pdf_text
        assert "ke kontrole" in pdf_text

    def test_warnings_rendered_once(self, pdf_text):
        needle = "NENÍ oficiální daňové přiznání"
        assert pdf_text.count(needle) == 1

    def test_footer(self, pdf_text):
        assert "Strana 1" in pdf_text
        assert "Není daňové poradenství" in pdf_text

    def test_eur_fallback_note(self, pdf_text):
        # The fixture aggregates without an FX provider => EUR-only mode
        assert "Hodnota (EUR)" in pdf_text
        assert "běh bez CZK konverze" in pdf_text


class TestPdfOutput:
    def test_write_to_file(self):
        tmp = os.path.join(tempfile.mkdtemp(), "test_cz.pdf")
        export_cz_to_pdf(_build_test_result(), tmp)
        assert os.path.exists(tmp)
        with open(tmp, "rb") as fh:
            assert fh.read(5) == b"%PDF-"

    def test_empty_result_no_crash(self):
        result = TaxResult(country_code="cz", tax_year=2025, sections={})
        buf = io.BytesIO()
        export_cz_to_pdf(result, buf)
        buf.seek(0)
        doc = fitz.open(stream=buf.read(), filetype="pdf")
        text = "\n".join(page.get_text() for page in doc)
        assert "2025" in text

    def test_no_optional_header_fields(self):
        text = _pdf_text(_build_test_result())
        assert "Poplatník" not in text
        assert "Účet IBKR" not in text


class TestFormattingHelpers:
    @pytest.mark.parametrize("value, expected", [
        (Decimal("0"), "0,00"),
        (Decimal("1234.5"), "1 234,50"),
        (Decimal("-9876543.21"), "-9 876 543,21"),
        (Decimal("999"), "999,00"),
        (None, ""),
    ])
    def test_fmt_money(self, value, expected):
        assert _fmt_money(value) == expected

    @pytest.mark.parametrize("value, expected", [
        ("2025-03-25", "25.03.2025"),
        ("", ""),
        (None, ""),
        ("not-a-date", "not-a-date"),
    ])
    def test_fmt_date(self, value, expected):
        assert _fmt_date(value) == expected
