# src/countries/cz/fx_mode_compare.py
"""
Side-by-side comparison of the two legal CZ FX conversion modes.

A taxpayer who does not keep accounting books may choose, for the WHOLE
tax year, either daily ČNB rates or the GFŘ uniform rate (§38/1 ZDP).
Both are legal; they generally yield different tax. This module compares
the final liability computed under each mode so the preparer can pick the
cheaper one.

Caveats surfaced in the output:
- One mode must be applied to ALL foreign-currency amounts of the year —
  no cherry-picking per transaction.
- Disposal legs are converted from their EUR-enriched amounts (daily ECB
  leg from the core pipeline x uniform/daily EUR->CZK), so the uniform
  figures for §10 carry a triangulation approximation until per-leg
  original-currency amounts exist (see M17/M18 in docs/future-work.md).
  Income and WHT convert directly from the original currency (exact).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from src.countries.base import TaxResult

_LIABILITY_SECTION = "cz_tax_liability"
_FINAL_LINE = "final_czech_tax_after_credit_czk"


def _liability_line(result: TaxResult, line: str) -> Optional[Decimal]:
    section = result.sections.get(_LIABILITY_SECTION)
    if section is None:
        return None
    value = section.line_items.get(line)
    return Decimal(value) if value is not None else None


@dataclass
class CzFxModeComparison:
    """Final-liability comparison of DAILY vs UNIFORM FX modes."""

    daily: TaxResult
    uniform: TaxResult

    @property
    def daily_final_tax(self) -> Optional[Decimal]:
        return _liability_line(self.daily, _FINAL_LINE)

    @property
    def uniform_final_tax(self) -> Optional[Decimal]:
        return _liability_line(self.uniform, _FINAL_LINE)

    @property
    def cheaper_mode(self) -> Optional[str]:
        """"daily" / "uniform" / "equal"; None if either run lacks a figure."""
        d, u = self.daily_final_tax, self.uniform_final_tax
        if d is None or u is None:
            return None
        if d == u:
            return "equal"
        return "daily" if d < u else "uniform"

    def render_lines(self) -> List[str]:
        """Human-readable summary for console output."""
        lines = [
            "=== Porovnání kurzových režimů (§38/1 ZDP) ===",
            f"{'':28}{'denní kurz':>14}{'jednotný kurz':>16}",
        ]
        for label, key in [
            ("kombinovaný základ", "combined_taxable_base_czk"),
            ("daň před zápočtem", "gross_czech_tax_czk"),
            ("zápočet §38f", "final_creditable_ftc_czk"),
            ("výsledná daň", _FINAL_LINE),
        ]:
            d = _liability_line(self.daily, key)
            u = _liability_line(self.uniform, key)
            d_s = f"{d:,.2f}" if d is not None else "n/a"
            u_s = f"{u:,.2f}" if u is not None else "n/a"
            lines.append(f"{label:<28}{d_s:>14}{u_s:>16}")

        d, u = self.daily_final_tax, self.uniform_final_tax
        if d is not None and u is not None:
            delta = (d - u).copy_abs()
            verdict = {
                "daily": f"Výhodnější je DENNÍ kurz (úspora {delta:,.2f} CZK).",
                "uniform": f"Výhodnější je JEDNOTNÝ kurz (úspora {delta:,.2f} CZK).",
                "equal": "Oba režimy vycházejí stejně.",
            }[self.cheaper_mode]
            lines.append(verdict)
        lines += [
            "Pozn.: režim se volí pro CELÝ rok a všechny měny najednou;",
            "u §10 prodejů jde jednotný přepočet přes EUR nohy (aproximace,",
            "viz docs/future-work.md M17/M18). Před podáním ověřte s poradcem.",
        ]
        return lines
