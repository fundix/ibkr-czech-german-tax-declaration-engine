# src/countries/cz/pairing_compare.py
"""
Side-by-side comparison of §10 pairing methods × FX conversion modes.

A CZ private investor may choose the lot-matching (pairing) method AND the
FX mode (daily ČNB vs GFŘ uniform) for the whole tax year. Both choices are
legal and generally yield different tax. This module scores the final
liability for each (fx_mode, pairing_method) cell — always via the *real*
CZ aggregator — and reports the cheapest, so the preparer can pick it.

Correctness guarantee: every cell (including the ``optimal`` solver) is a
real aggregation run, so the reported figure is the true tax and the winner
is never worse than plain FIFO.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from src.countries.base import TaxResult
from src.engine.pairing import PairingMethod

_LIABILITY_SECTION = "cz_tax_liability"
_FINAL_LINE = "final_czech_tax_after_credit_czk"

# Cell key: (fx_mode, pairing_method_value)
CellKey = Tuple[str, str]

_FX_LABELS = {"daily": "denní kurz", "uniform": "jednotný kurz"}


def _final_tax(result: TaxResult) -> Optional[Decimal]:
    section = result.sections.get(_LIABILITY_SECTION)
    if section is None:
        return None
    value = section.line_items.get(_FINAL_LINE)
    return Decimal(value) if value is not None else None


@dataclass
class CzPairingComparison:
    """Grid of final liabilities across FX modes × pairing methods."""

    grid: Dict[CellKey, TaxResult] = field(default_factory=dict)
    fx_modes: List[str] = field(default_factory=list)
    pairing_methods: List[PairingMethod] = field(default_factory=list)

    def result_for(self, fx_mode: str, method: PairingMethod) -> Optional[TaxResult]:
        return self.grid.get((fx_mode, method.value))

    def final_tax_for(self, fx_mode: str, method: PairingMethod) -> Optional[Decimal]:
        result = self.result_for(fx_mode, method)
        return _final_tax(result) if result is not None else None

    @property
    def best_cell(self) -> Optional[CellKey]:
        """(fx_mode, method_value) with the lowest final tax; None if no figure."""
        best: Optional[CellKey] = None
        best_tax: Optional[Decimal] = None
        # Deterministic tie-break: prefer the declared fx/method display order.
        for method in self.pairing_methods:
            for fx in self.fx_modes:
                tax = self.final_tax_for(fx, method)
                if tax is None:
                    continue
                if best_tax is None or tax < best_tax:
                    best_tax, best = tax, (fx, method.value)
        return best

    @property
    def best_result(self) -> Optional[TaxResult]:
        cell = self.best_cell
        return self.grid.get(cell) if cell is not None else None

    def _baseline_tax(self) -> Optional[Decimal]:
        """FIFO under the first FX mode — the 'do nothing' reference."""
        if not self.fx_modes:
            return None
        return self.final_tax_for(self.fx_modes[0], PairingMethod.FIFO)

    def render_lines(self) -> List[str]:
        lines = ["=== Porovnání párovacích metod × kurz (§10 ZDP) ==="]
        row_label = "metoda \\ kurz"
        header = f"{row_label:<22}" + "".join(
            f"{_FX_LABELS.get(fx, fx):>16}" for fx in self.fx_modes
        )
        lines.append(header)
        for method in self.pairing_methods:
            row = f"{method.label_cs:<22}"
            for fx in self.fx_modes:
                tax = self.final_tax_for(fx, method)
                row += f"{(f'{tax:,.2f}' if tax is not None else 'n/a'):>16}"
            lines.append(row)

        cell = self.best_cell
        if cell is not None:
            fx, method_value = cell
            method = PairingMethod(method_value)
            best_tax = self.final_tax_for(fx, method)
            baseline = self._baseline_tax()
            verdict = (
                f"Nejvýhodnější: {method.label_cs} @ {_FX_LABELS.get(fx, fx)} "
                f"(výsledná daň {best_tax:,.2f} CZK"
            )
            if baseline is not None and best_tax is not None and baseline > best_tax:
                verdict += f", úspora {baseline - best_tax:,.2f} CZK proti FIFO/{_FX_LABELS.get(self.fx_modes[0], self.fx_modes[0])}"
            verdict += ")."
            lines.append(verdict)

        lines += [
            "Pozn.: metodu i kurzový režim je nutné použít konzistentně pro CELÝ",
            "rok; nástroj jen doporučuje nejlevnější variantu. Vážený průměr drží",
            "identitu lotů pro časový test dle FIFO. Ověřte s daňovým poradcem.",
        ]
        return lines
