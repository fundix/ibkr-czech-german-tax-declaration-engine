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


def cz_fx_setup(fx_mode: str, fx_provider=None):
    """The CZ config, rate provider and CZK converter of one FX mode.

    One place decides them, so the aggregation that files a mode and the
    optimal pairing that minimises its base (audit W06) cannot use different
    rates. ``fx_provider`` overrides the mode's default provider.
    """
    from src.countries.cz.fx_policy import CzCurrencyConverter

    if fx_mode == "uniform":
        cfg = CzTaxConfig(fx_policy=uniform_fx_policy())
        provider = fx_provider or CzUniformRateProvider()
    else:
        cfg = CzTaxConfig()
        provider = fx_provider or create_fx_provider(
            cfg.fx_policy.source,
            cache_file_path=cfg.cnb_cache_file_path,
        )
    return cfg, provider, CzCurrencyConverter(provider, cfg.fx_policy)


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
    cfg, provider, _converter = cz_fx_setup(fx_mode, fx_provider)
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
    unapplied = unapplied_corporate_actions_section(
        getattr(processing_results, "unapplied_corporate_actions", None) or [])
    if unapplied is not None:
        result.sections[unapplied.section_key] = unapplied
    closed_at_zero = option_events_closed_at_zero_section(
        getattr(processing_results, "option_events_closed_at_zero", None) or [])
    if closed_at_zero is not None:
        result.sections[closed_at_zero.section_key] = closed_at_zero
    return result


def option_events_closed_at_zero_section(
    events: List[Dict[str, Any]],
) -> Optional[TaxResultSection]:
    """A blocking review section for option exercises and assignments that
    came with neither a delivery trade nor an amount, or None when there are
    none.

    An exercise or assignment either delivers shares -- then its premium moves
    into their cost or proceeds -- or settles in cash on the option's own row.
    One that shows neither was closed on the option at zero so that its premium
    is not lost, but something is missing from the statements: the delivery
    trade, or the settlement amount.
    """
    if not events:
        return None
    listed = "; ".join(
        f"{e.get('symbol')}"
        + (f" ({e['description']})" if e.get("description") else "")
        + f": {e.get('event')} of {e.get('contracts')} contract(s) on {e.get('event_date')}"
        + f", transaction {e.get('transaction_id') or '?'}"
        for e in events
    )
    note = (
        f"REVIEW REQUIRED — DO NOT FILE: {len(events)} option exercise(s)/assignment(s) "
        f"came with neither a delivery trade nor an amount on their own row — {listed}. "
        "Each was closed on the option at zero, so its premium is in the result as an "
        "option gain or loss on that day. If shares were delivered, their trade is "
        "missing from the export or could not be matched to the option, and the "
        "premium is NOT in their cost or proceeds; if the contract was settled in "
        "cash, the settlement amount is missing."
    )
    return TaxResultSection(
        section_key="option_events_closed_at_zero",
        label="Opce uplatněné nebo přidělené bez dodávky",
        line_items={"event_count": Decimal(len(events))},
        notes=[note],
    )


def unapplied_corporate_actions_section(
    actions: List[Dict[str, Any]],
) -> Optional[TaxResultSection]:
    """A blocking review section for corporate actions the engine did not
    apply, or None when there are none.

    A spin-off, a tender or a delisting moves shares, but what the new shares
    cost -- or what the old ones yielded, and how that is taxed -- is not on
    the statement row, and the engine has no rule for it. Such an action used
    to vanish; now the run completes and this note says what is missing.
    Shares it added are held at ZERO cost as a placeholder: the acquisition
    cost of spun-off shares (a share of the parent's cost, or their value as
    income in kind) and whether the parent's holding period carries over is a
    question for the tax advisor, not something to guess here.
    """
    if not actions:
        return None
    listed = "; ".join(
        f"{a.get('symbol')}"
        + (f" ({a['description']})" if a.get("description") else "")
        + f": {a.get('quantity_change'):+} on {a.get('event_date')}"
        + f", type {a.get('ca_type') or '?'}, action {a.get('action_id') or '?'}"
        + (f" [{a['ca_description']}]" if a.get("ca_description") else "")
        + (" — held at zero cost" if a.get("placeholder_lot") else " — ledger left as it was")
        for a in actions
    )
    note = (
        f"REVIEW REQUIRED — DO NOT FILE: {len(actions)} corporate action(s) changed a "
        f"position in a way the engine has no rule for and did not apply — {listed}. "
        "Shares ADDED by such an action are held as a placeholder lot at ZERO cost "
        "with an unestablished acquisition date: a sale of them is computed against "
        "that zero cost and cannot pass the time test, and the cost of a related "
        "holding (the parent of a spin-off) is NOT reduced. Their real acquisition "
        "cost and holding period have to be determined by hand. Where the ledger was "
        "left as it was, shares the action took away are still in it and whatever "
        "was received for them is not in the results at all."
    )
    return TaxResultSection(
        section_key="unapplied_corporate_actions",
        label="Neuplatněné korporátní akce",
        line_items={"action_count": Decimal(len(actions))},
        notes=[note],
    )


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
    daily_fx_provider=None,
) -> CzFxModeComparison:
    """Aggregate under BOTH FX modes and return the comparison.

    ``daily_fx_provider`` overrides the daily leg's provider, as
    ``fx_provider`` does in ``run_cz_aggregation`` — so the caller can ask it
    afterwards how far its rates could be trusted, and tests can pin them.
    """
    return CzFxModeComparison(
        daily=run_cz_aggregation(processing_results, tax_year, "daily",
                                 fx_provider=daily_fx_provider,
                                 statement_of_funds_path=statement_of_funds_path),
        uniform=run_cz_aggregation(processing_results, tax_year, "uniform",
                                   statement_of_funds_path=statement_of_funds_path),
    )


def run_cz_pairing_matrix(
    run_pipeline_for_method: Callable[..., "ProcessingOutput"],
    tax_year: int,
    fx_modes: Sequence[str] = ("daily", "uniform"),
    pairing_methods: Sequence[PairingMethod] = ALL_METHODS,
    fx_providers: Optional[Dict[str, Any]] = None,
) -> CzPairingComparison:
    """Score the full FX-mode × pairing-method grid and return the comparison.

    ``run_pipeline_for_method(method, objective_converter=None)`` re-runs the
    CORE pipeline for one pairing method (the method changes the
    RealizedGainLoss set, unlike the FX mode which only affects downstream
    aggregation). Each resulting output is then aggregated under every
    requested FX mode -- except ``optimal``, whose pairing minimises the CZK
    base of one FX mode: it is run once per mode, with that mode's converter
    (audit W06). Every cell is a real aggregation run, so the reported
    figures are exact. ``fx_providers`` overrides a mode's rate provider.
    """
    fx_providers = fx_providers or {}
    grid: dict = {}
    for method in pairing_methods:
        if method == PairingMethod.OPTIMAL:
            for fx in fx_modes:
                _cfg, provider, converter = cz_fx_setup(fx, fx_providers.get(fx))
                processing = run_pipeline_for_method(method, objective_converter=converter)
                grid[(fx, method.value)] = run_cz_aggregation(
                    processing, tax_year, fx, fx_provider=provider)
            continue
        processing = run_pipeline_for_method(method)
        for fx in fx_modes:
            grid[(fx, method.value)] = run_cz_aggregation(
                processing, tax_year, fx, fx_provider=fx_providers.get(fx))
    return CzPairingComparison(
        grid=grid,
        fx_modes=list(fx_modes),
        pairing_methods=list(pairing_methods),
    )
