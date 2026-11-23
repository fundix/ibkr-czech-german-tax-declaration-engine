# src/countries/cz/aggregation_service.py
"""
Reusable CZ aggregation entry points.

Wires the correct ``CzTaxConfig`` + FX provider for the chosen FX mode and
runs the CZ tax aggregator over a finished core-pipeline output. Extracted
from the CLI (``src/main.py``) so that the web/MCP layers can aggregate
without going through argument parsing.

Without an FX provider the plugin degrades to EUR-only output (no CZK
figures, annual limit and rate threshold inactive), so a real CZ run always
wires a provider for the chosen mode.
"""
from __future__ import annotations

from decimal import Decimal

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from src.countries.base import TaxResultSection

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.fx_mode_compare import CzFxModeComparison
from src.countries.cz.pairing_compare import CzPairingComparison
from src.countries.cz.fx_policy import uniform_fx_policy
from src.countries.cz.uniform_rates import CzUniformRateProvider
from src.countries.registry import get_tax_plugin
from src.engine.pairing import PairingMethod, ALL_METHODS
from src.utils.fx_provider_factory import create_fx_provider

if TYPE_CHECKING:
    from src.countries.base import TaxResult
    from src.pipeline_runner import ProcessingOutput


def run_cz_aggregation(
    processing_results: "ProcessingOutput",
    tax_year: int,
    fx_mode: str = "daily",
    fx_provider=None,
    statement_of_funds_path: "str | None" = None,
) -> "TaxResult":
    """Aggregate a pipeline output under one FX mode ('daily' or 'uniform').

    ``fx_provider`` overrides the default provider for the mode — used by
    offline tests to pin rates without network access.

    ``statement_of_funds_path`` is the merged multi-year cash ledger. With it
    the currency disposals carry a computed FX gain; without it they are
    recorded but not valued, which is all the other statements allow.
    """
    if fx_mode == "uniform":
        cfg = CzTaxConfig(fx_policy=uniform_fx_policy())
        provider = fx_provider or CzUniformRateProvider()
    else:
        cfg = CzTaxConfig()
        provider = fx_provider or create_fx_provider(
            cfg.fx_policy.source,
            cache_file_path=cfg.cnb_cache_file_path,
        )
    plugin = get_tax_plugin("cz", config=cfg, fx_provider=provider)
    result = plugin.get_tax_aggregator().aggregate(
        realized_gains_losses=processing_results.realized_gains_losses,
        financial_events=processing_results.processed_income_events,
        asset_resolver=processing_results.asset_resolver,
        tax_year=tax_year,
        statement_of_funds_path=statement_of_funds_path,
    )
    eoy = eoy_reconciliation_section(
        getattr(processing_results, "eoy_mismatch_details", None) or [])
    if eoy is not None:
        result.sections[eoy.section_key] = eoy
    return result


def eoy_reconciliation_section(
    mismatches: List[Dict[str, Any]],
) -> Optional[TaxResultSection]:
    """A blocking review section when the ledger does not reconcile to the
    broker's year-end positions, or None when it does.

    If the two disagree about what is held, at least one disposal was missed,
    double-counted or matched to the wrong lot, so every figure from that
    ledger is unsafe -- not merely the quantity. Upstream (1c4c981) aborts the
    run; here it completes, so a running-year estimate survives a moving
    snapshot, but the note says plainly that the result must not be filed. It
    reaches the review page, the MCP checklist, the PDF and the XLSX like any
    other REVIEW note.
    """
    if not mismatches:
        return None
    listed = "; ".join(
        f"{m.get('symbol')}"
        + (f" ({m['description']})" if m.get("description") else "")
        + f": calculated {m.get('calculated')}, broker reports {m.get('reported')}"
        for m in mismatches
    )
    note = (
        f"REVIEW REQUIRED — DO NOT FILE: {len(mismatches)} position(s) at year end "
        f"do not match the broker's report — {listed}. A ledger that disagrees with "
        "the broker about what is held means a trade is missing, doubled or paired "
        "with the wrong lot, so every gain computed from it is unsafe. Typical "
        "causes: trade history that does not reach back far enough, a missing "
        "corporate action, or a positions file from a different date than the "
        "trades."
    )
    return TaxResultSection(
        section_key="eoy_reconciliation",
        label="Kontrola pozic ke konci roku",
        line_items={"mismatch_count": Decimal(len(mismatches))},
        notes=[note],
    )


def run_cz_compare(
    processing_results: "ProcessingOutput",
    tax_year: int,
    statement_of_funds_path: "str | None" = None,
) -> CzFxModeComparison:
    """Aggregate under BOTH FX modes and return the comparison."""
    return CzFxModeComparison(
        daily=run_cz_aggregation(processing_results, tax_year, "daily",
                                 statement_of_funds_path=statement_of_funds_path),
        uniform=run_cz_aggregation(processing_results, tax_year, "uniform",
                                   statement_of_funds_path=statement_of_funds_path),
    )


def run_cz_pairing_matrix(
    run_pipeline_for_method: Callable[[PairingMethod], "ProcessingOutput"],
    tax_year: int,
    fx_modes: Sequence[str] = ("daily", "uniform"),
    pairing_methods: Sequence[PairingMethod] = ALL_METHODS,
) -> CzPairingComparison:
    """Score the full FX-mode × pairing-method grid and return the comparison.

    ``run_pipeline_for_method`` re-runs the CORE pipeline for one pairing
    method (the method changes the RealizedGainLoss set, unlike the FX mode
    which only affects downstream aggregation). Each resulting output is then
    aggregated under every requested FX mode. Every cell is a real aggregation
    run, so the reported figures are exact and the cheapest cell is a safe
    recommendation.
    """
    grid: dict = {}
    for method in pairing_methods:
        processing = run_pipeline_for_method(method)
        for fx in fx_modes:
            grid[(fx, method.value)] = run_cz_aggregation(processing, tax_year, fx)
    return CzPairingComparison(
        grid=grid,
        fx_modes=list(fx_modes),
        pairing_methods=list(pairing_methods),
    )
