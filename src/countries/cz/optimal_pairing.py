# src/countries/cz/optimal_pairing.py
"""
Apply the tax-optimal (``optimal``) pairing method to a finished FIFO run.

The core pipeline runs FIFO (``OPTIMAL`` behaves as FIFO at the ledger level),
which yields, per asset, the full lot supply — reconstructable as the consumed
lot portions (each FIFO ``RealizedGainLoss``) plus the surviving end-of-year
lots — and the sale demand (RGLs grouped by their originating sale event). This
module re-solves that per-asset transportation problem (``pairing_solver``) to
minimise the §10 taxable net gain, then replaces the asset's FIFO RGLs with the
solver's, re-running the country tax classifier on each.

Scope (v1): long securities disposals only. Assets with short positions,
option closes, cash mergers, or any tax-year corporate action / capital
repayment (which shift the lot basis mid-year and would make the
supply reconstruction inconsistent) keep their FIFO RGLs unchanged. The caller
scores the result with the real aggregator, so ``optimal`` is never worse than
FIFO even where it falls back.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as date_obj
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.time_test import time_test_deadline
from src.domain.enums import RealizationType
from src.domain.events import CorporateActionEvent, FinancialEvent
from src.domain.enums import FinancialEventType
from src.domain.results import RealizedGainLoss
from src.engine.pairing_solver import (
    Assignment, SaleDemand, SupplyLot, solve_optimal_matching,
)
from src.utils.type_utils import parse_ibkr_date

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


def _complex_asset_ids(events: List[FinancialEvent], tax_year: int) -> set:
    """Assets whose lot basis shifts mid-year (corp action / capital repayment).

    These break the supply reconstruction (a sale before the event carries a
    different unit basis than the surviving lots after it), so they are kept on
    FIFO.
    """
    complex_ids: set = set()
    for ev in events:
        d = parse_ibkr_date(ev.event_date)
        if d is None or d.year != tax_year:
            continue
        if isinstance(ev, CorporateActionEvent) or ev.event_type == FinancialEventType.CAPITAL_REPAYMENT:
            complex_ids.add(ev.asset_internal_id)
    return complex_ids


def _asset_eligible(rgls: List[RealizedGainLoss], asset_id, complex_ids: set) -> bool:
    if asset_id in complex_ids:
        return False
    # Only plain long securities disposals — no shorts, options, cash mergers.
    return all(r.realization_type == RealizationType.LONG_POSITION_SALE for r in rgls)


def _build_supplies(
    rgls: List[RealizedGainLoss],
    ledger,
) -> Optional[List[SupplyLot]]:
    """Full lot supply = consumed RGL portions + surviving EOY lots."""
    supplies: List[SupplyLot] = []
    for r in rgls:
        acq = parse_ibkr_date(r.acquisition_date)
        if acq is None:
            return None
        supplies.append(SupplyLot(
            acq_date=acq,
            quantity=r.quantity_realized,
            unit_cost_eur=r.unit_cost_basis_eur,
            source_id="SOY_FALLBACK" if r.is_acquisition_estimated else "REAL",
            estimated=bool(r.is_acquisition_estimated),
        ))
    if ledger is not None:
        for lot in getattr(ledger, "lots", []):
            acq = parse_ibkr_date(lot.acquisition_date)
            if acq is None:
                return None
            supplies.append(SupplyLot(
                acq_date=acq,
                quantity=lot.quantity,
                unit_cost_eur=lot.unit_cost_basis_eur,
                source_id=lot.source_transaction_id,
                estimated=str(lot.source_transaction_id or "").startswith("SOY_FALLBACK"),
            ))
    return supplies


def _build_demands(rgls: List[RealizedGainLoss]) -> Optional[List[SaleDemand]]:
    """One demand per originating sale event (RGLs of a sale share date/price)."""
    by_event: "defaultdict[object, List[RealizedGainLoss]]" = defaultdict(list)
    for r in rgls:
        by_event[r.originating_event_id].append(r)
    demands: List[SaleDemand] = []
    for event_id, group in by_event.items():
        sale_date = parse_ibkr_date(group[0].realization_date)
        if sale_date is None:
            return None
        total_qty = sum((r.quantity_realized for r in group), ZERO)
        total_proceeds = sum((r.total_realization_value_eur for r in group), ZERO)
        if total_qty <= ZERO:
            return None
        demands.append(SaleDemand(
            sale_date=sale_date,
            quantity=total_qty,
            unit_proceeds_eur=total_proceeds / total_qty,
            originating_event_id=event_id,
        ))
    return demands


def _emit_rgls(
    template: RealizedGainLoss,
    supplies: List[SupplyLot],
    demands: List[SaleDemand],
    assignments: List[Assignment],
    ctx_multiply: Callable[[Decimal, Decimal], Decimal],
    classifier: Optional[Callable[[RealizedGainLoss], None]],
) -> List[RealizedGainLoss]:
    out: List[RealizedGainLoss] = []
    for a in assignments:
        lot = supplies[a.supply_index]
        sale = demands[a.demand_index]
        qty = a.quantity
        total_cost = ctx_multiply(qty, lot.unit_cost_eur)
        total_proceeds = ctx_multiply(qty, sale.unit_proceeds_eur)
        gross = total_proceeds - total_cost
        holding_days = (sale.sale_date - lot.acq_date).days
        rgl = RealizedGainLoss(
            originating_event_id=sale.originating_event_id,
            asset_internal_id=template.asset_internal_id,
            asset_category_at_realization=template.asset_category_at_realization,
            acquisition_date=lot.acq_date.isoformat(),
            realization_date=sale.sale_date.isoformat(),
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=qty,
            unit_cost_basis_eur=lot.unit_cost_eur,
            unit_realization_value_eur=sale.unit_proceeds_eur,
            total_cost_basis_eur=total_cost,
            total_realization_value_eur=total_proceeds,
            gross_gain_loss_eur=gross,
            holding_period_days=holding_days,
            fund_type_at_sale=template.fund_type_at_sale,
            is_acquisition_estimated=lot.estimated,
        )
        if classifier is not None:
            classifier(rgl)
        out.append(rgl)
    return out


def apply_cz_optimal_pairing(
    realized_gains_losses: List[RealizedGainLoss],
    fifo_ledgers: Dict,
    all_financial_events: List[FinancialEvent],
    tax_year: int,
    classifier: Optional[Callable[[RealizedGainLoss], None]] = None,
    config: Optional[CzTaxConfig] = None,
) -> List[RealizedGainLoss]:
    """Return a new RGL list with eligible assets re-matched tax-optimally."""
    config = config or CzTaxConfig()
    complex_ids = _complex_asset_ids(all_financial_events, tax_year)

    def _exempt(lot: SupplyLot, sale: SaleDemand) -> bool:
        # Mirror evaluate_time_test: estimated SOY lots are never exempt; a
        # disposal strictly after the deadline is exempt.
        if lot.estimated:
            return False
        return sale.sale_date > time_test_deadline(lot.acq_date, config)

    def _mul(a: Decimal, b: Decimal) -> Decimal:
        return a * b

    by_asset: "defaultdict[object, List[RealizedGainLoss]]" = defaultdict(list)
    for r in realized_gains_losses:
        by_asset[r.asset_internal_id].append(r)

    new_rgls: List[RealizedGainLoss] = []
    reoptimised = 0
    for asset_id, rgls in by_asset.items():
        if not _asset_eligible(rgls, asset_id, complex_ids):
            new_rgls.extend(rgls)
            continue
        supplies = _build_supplies(rgls, fifo_ledgers.get(asset_id))
        demands = _build_demands(rgls)
        if supplies is None or demands is None:
            new_rgls.extend(rgls)
            continue
        assignments = solve_optimal_matching(supplies, demands, _exempt)
        if assignments is None:
            logger.info(
                "Optimal pairing: asset %s not solvable — keeping FIFO.", asset_id
            )
            new_rgls.extend(rgls)
            continue
        new_rgls.extend(_emit_rgls(rgls[0], supplies, demands, assignments, _mul, classifier))
        reoptimised += 1

    logger.info(
        "Optimal pairing: re-matched %d asset(s); %d kept on FIFO.",
        reoptimised, len(by_asset) - reoptimised,
    )
    return new_rgls
