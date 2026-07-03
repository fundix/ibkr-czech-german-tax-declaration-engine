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

from typing import TYPE_CHECKING

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.fx_mode_compare import CzFxModeComparison
from src.countries.cz.fx_policy import uniform_fx_policy
from src.countries.cz.uniform_rates import CzUniformRateProvider
from src.countries.registry import get_tax_plugin
from src.utils.fx_provider_factory import create_fx_provider

if TYPE_CHECKING:
    from src.countries.base import TaxResult
    from src.pipeline_runner import ProcessingOutput


def run_cz_aggregation(
    processing_results: "ProcessingOutput",
    tax_year: int,
    fx_mode: str = "daily",
    fx_provider=None,
) -> "TaxResult":
    """Aggregate a pipeline output under one FX mode ('daily' or 'uniform').

    ``fx_provider`` overrides the default provider for the mode — used by
    offline tests to pin rates without network access.
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
    return plugin.get_tax_aggregator().aggregate(
        realized_gains_losses=processing_results.realized_gains_losses,
        financial_events=processing_results.processed_income_events,
        asset_resolver=processing_results.asset_resolver,
        tax_year=tax_year,
    )


def run_cz_compare(
    processing_results: "ProcessingOutput",
    tax_year: int,
) -> CzFxModeComparison:
    """Aggregate under BOTH FX modes and return the comparison."""
    return CzFxModeComparison(
        daily=run_cz_aggregation(processing_results, tax_year, "daily"),
        uniform=run_cz_aggregation(processing_results, tax_year, "uniform"),
    )
