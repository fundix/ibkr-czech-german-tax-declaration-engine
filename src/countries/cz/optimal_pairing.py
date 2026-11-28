# src/countries/cz/optimal_pairing.py
"""
Apply the tax-optimal (``optimal``) pairing method to a finished FIFO run.

The core pipeline runs FIFO (``OPTIMAL`` behaves as FIFO at the ledger level).
Its ledger journals every tax-year sale and exactly which part of which lot it
consumed, so per asset the full lot supply is known -- every lot open at any
point of the year, with its identity and its place in the day -- and so is the
sale demand. This module re-solves that per-asset transportation problem
(``pairing_solver``) to minimise the §10 taxable base, then:

* replaces the asset's FIFO RGLs with the solver's, re-running the country
  tax classifier on each;
* rebuilds the ledger's open lots from what the assignment left (audit W05),
  so the portfolio, the time-test countdown and the simulator see the lots
  the filed return leaves, not FIFO's;
* returns the assignment as a plan (sale TransactionID -> lots), which is
  stored when the year is marked filed and replayed by later years (V04).

A lot may fill only a sale booked after it (audit W07). With a CZK converter
the solver minimises the CZK base -- proceeds at the sale day's rate minus the
cost at the rate of the day it was paid, through the same helper the item
builder files with (audit W06); without one (EUR-only mode) the EUR gain.

Scope: long securities disposals only. Assets with short positions, option
closes, cash mergers, or any tax-year corporate action / capital repayment
(which shift the lot basis mid-year) keep their FIFO RGLs unchanged. The
solver minimises the base; the 100 000 CZK exemption cliff is outside its
objective, so compare the result with FIFO before filing.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace as dc_replace
from datetime import date as date_obj
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.time_test import time_test_exempt
from src.domain.enums import RealizationType
from src.domain.events import CorporateActionEvent, FinancialEvent
from src.domain.enums import FinancialEventType
from src.domain.results import RealizedGainLoss
from src.engine.pairing_solver import (
    Assignment, SaleDemand, SupplyLot, solve_optimal_matching,
)
from src.engine.fifo_manager import ConsumedPortion, _scale_components, plan_key
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
        hps = parse_ibkr_date(r.holding_period_start or r.acquisition_date)
        if hps is None:
            return None
        supplies.append(SupplyLot(
            acq_date=acq,
            quantity=r.quantity_realized,
            unit_cost_eur=r.unit_cost_basis_eur,
            source_id="SOY_FALLBACK" if r.is_acquisition_estimated else "REAL",
            estimated=bool(r.is_acquisition_estimated),
            holding_start=hps,
            holding_start_estimated=bool(r.holding_period_start_estimated),
        ))
    if ledger is not None:
        for lot in getattr(ledger, "lots", []):
            acq = parse_ibkr_date(lot.acquisition_date)
            if acq is None:
                return None
            hps = parse_ibkr_date(lot.holding_period_start or lot.acquisition_date)
            if hps is None:
                return None
            supplies.append(SupplyLot(
                acq_date=acq,
                quantity=lot.quantity,
                unit_cost_eur=lot.unit_cost_basis_eur,
                source_id=lot.source_transaction_id,
                # The lot carries the flag now; sniffing the id prefix would stop
                # recognising a carried-over lot minted under a corp-action id.
                estimated=bool(lot.acquisition_date_estimated),
                holding_start=hps,
                holding_start_estimated=bool(lot.holding_period_start_estimated),
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
        holding_days = (sale.sale_date - lot.holding_start).days
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
            holding_period_start=lot.holding_start.isoformat(),
            holding_period_start_estimated=lot.holding_start_estimated,
            fund_type_at_sale=template.fund_type_at_sale,
            is_acquisition_estimated=lot.estimated,
        )
        if classifier is not None:
            classifier(rgl)
        out.append(rgl)
    return out


@dataclass
class OptimalPairingOutcome:
    """What the optimal pairing changed."""
    realized_gains_losses: List[RealizedGainLoss]
    # sale IBKR TransactionID -> [(lot source_transaction_id, quantity)], for
    # every sale of a re-matched asset that carries a TransactionID.
    plan: Dict[str, List[Tuple[str, Decimal]]] = field(default_factory=dict)
    rematched_asset_ids: set = field(default_factory=set)


def _time_test_predicate(config: CzTaxConfig):
    def _exempt(lot: SupplyLot, sale: SaleDemand) -> bool:
        # Scores with the SAME predicate evaluate_time_test applies, rather than
        # re-deriving the arithmetic — a mirrored copy drifts, and a second date
        # would only double the drift. Either synthetic date disqualifies the
        # lot, matching the evaluator's PENDING_MANUAL_REVIEW gate.
        if lot.estimated or lot.holding_start_estimated:
            return False
        if not config.time_test_enabled:
            return False
        return time_test_exempt(
            acquisition_date=lot.acq_date,
            holding_period_start=lot.holding_start,
            sale_date=sale.sale_date,
            config=config,
        )
    return _exempt


def _whole_lots(ledger) -> Optional[List[Any]]:
    """Every lot of the year at its full size: open part + consumed parts.

    Keyed by the ledger's lot_id; the open lot (or the first consumed part)
    gives the identity, the parts add their quantity and cost.
    """
    open_lots = list(getattr(ledger, "lots", []))
    consumed = [portion.lot for portion in getattr(ledger, "consumed_portions", [])]
    open_ids = [lot.lot_id for lot in open_lots]
    if (any(not lot.lot_id for lot in [*open_lots, *consumed])
            or len(set(open_ids)) != len(open_ids)):
        # The id its ledger stamped on a lot is all that tells it from the
        # others here. A lot without one, or two open lots sharing one, would
        # be put together with another lot and their costs blended.
        logger.warning(
            "Optimal pairing: asset %s holds lots without a ledger id of their "
            "own — keeping FIFO.", getattr(ledger, "asset_internal_id", "?"))
        return None
    parts: "Dict[int, List[Any]]" = defaultdict(list)
    for lot in open_lots:
        parts[lot.lot_id].append(lot)
    for lot in consumed:
        parts[lot.lot_id].append(lot)
    whole = []
    for _lot_id, pieces in sorted(parts.items()):
        template = pieces[0]
        quantity = sum((p.quantity for p in pieces), ZERO)
        total = sum((p.total_cost_basis_eur for p in pieces), ZERO)
        if quantity <= ZERO:
            return None
        originals = [p.total_cost_original for p in pieces]
        original = (sum(originals, ZERO)
                    if all(o is not None for o in originals)
                    and len({p.cost_currency for p in pieces}) == 1 else None)
        components = None
        if any(p.cost_components is not None for p in pieces):
            merged: Dict[str, Decimal] = {}
            for p in pieces:
                for day, amount in (p.cost_components if p.cost_components is not None
                                    else [(p.holding_period_start or p.acquisition_date,
                                           p.total_cost_basis_eur)]):
                    merged[day] = merged.get(day, ZERO) + amount
            components = sorted(merged.items())
        whole.append(dc_replace(
            template, quantity=quantity, total_cost_basis_eur=total,
            unit_cost_basis_eur=total / quantity,
            total_cost_original=original,
            cost_currency=template.cost_currency if original is not None else None,
            cost_components=components,
        ))
    return whole


def _part_of(lot, quantity: Decimal):
    """*quantity* of a whole lot, its cost scaled; the lot itself when all."""
    if quantity == lot.quantity:
        return dc_replace(lot)
    total = lot.total_cost_basis_eur * quantity / lot.quantity
    original = (lot.total_cost_original * quantity / lot.quantity
                if lot.total_cost_original is not None else None)
    from decimal import getcontext
    return dc_replace(
        lot, quantity=quantity, total_cost_basis_eur=total,
        total_cost_original=original,
        cost_currency=lot.cost_currency if original is not None else None,
        cost_components=(_scale_components(lot.cost_components, total, getcontext())
                         if lot.cost_components is not None else None),
    )


def _czk_arc_cost(fx, lots: List[Any], sales: List[Any]):
    """Per-unit CZK gain of a taxable match: proceeds at the sale day's rate
    minus the cost at the rate(s) of the day(s) it was paid -- the very
    conversion ``item_builder`` files with. None where a rate is missing."""
    from src.countries.cz.item_builder import cost_leg_czk

    unit_cost_czk: Dict[int, Optional[Decimal]] = {}
    for index, lot in enumerate(lots):
        unit_total = lot.total_cost_basis_eur / lot.quantity
        unit_components = (
            [(d, a / lot.quantity) for d, a in lot.cost_components]
            if lot.cost_components is not None else None)
        czk, _rec = cost_leg_czk(
            unit_total, lot.holding_period_start or lot.acquisition_date,
            unit_components, fx)
        unit_cost_czk[index] = czk
    unit_proceeds_czk: Dict[int, Optional[Decimal]] = {}
    for index, sale in enumerate(sales):
        czk, _rec = cost_leg_czk(sale.unit_proceeds_eur, sale.sale_date, None, fx)
        unit_proceeds_czk[index] = czk

    def _arc(supply: SupplyLot, demand: SaleDemand) -> Optional[Decimal]:
        cost = unit_cost_czk.get(supply.source_id)
        proceeds = unit_proceeds_czk.get(demand.originating_event_id)
        if cost is None or proceeds is None:
            return None
        return proceeds - cost
    return _arc


def _rematch_from_journal(
    asset_id, rgls: List[RealizedGainLoss], ledger, exempt, classifier, fx,
) -> Optional[Tuple[List[RealizedGainLoss], Dict[str, List[Tuple[str, Decimal]]]]]:
    """Re-match one asset from its ledger journal; None keeps FIFO."""
    sales = list(getattr(ledger, "tax_year_sales", []))
    if not sales:
        return None
    if any(not s.ibkr_transaction_id for s in sales):
        # The plan names a sale by its TransactionID; a sale without one
        # could not be replayed as filed, so the asset stays FIFO.
        logger.info("Optimal pairing: asset %s has a sale without a TransactionID "
                    "— keeping FIFO.", asset_id)
        return None
    sold = sum((s.quantity for s in sales), ZERO)
    if sold != sum((r.quantity_realized for r in rgls), ZERO):
        logger.info("Optimal pairing: asset %s journal disagrees with its "
                    "disposals — keeping FIFO.", asset_id)
        return None
    lots = _whole_lots(ledger)
    if not lots:
        return None

    supplies: List[SupplyLot] = []
    for index, lot in enumerate(lots):
        acq = parse_ibkr_date(lot.acquisition_date)
        hps = parse_ibkr_date(lot.holding_period_start or lot.acquisition_date)
        if acq is None or hps is None:
            return None
        supplies.append(SupplyLot(
            acq_date=acq, quantity=lot.quantity,
            unit_cost_eur=lot.total_cost_basis_eur / lot.quantity,
            # The index into ``lots``: the CZK arc cost looks the lot up by it.
            source_id=index,
            estimated=bool(lot.acquisition_date_estimated),
            holding_start=hps,
            holding_start_estimated=bool(lot.holding_period_start_estimated),
            order_key=lot.sequence,
        ))
    demands: List[SaleDemand] = []
    for index, sale in enumerate(sales):
        sale_date = parse_ibkr_date(sale.sale_date)
        if sale_date is None or sale.quantity <= ZERO:
            return None
        demands.append(SaleDemand(
            sale_date=sale_date, quantity=sale.quantity,
            unit_proceeds_eur=sale.unit_proceeds_eur,
            originating_event_id=index, order_key=sale.sequence,
        ))

    arc_cost = _czk_arc_cost(fx, lots, sales) if fx is not None else None
    assignments = solve_optimal_matching(supplies, demands, exempt, arc_cost=arc_cost)
    if assignments is None:
        logger.info("Optimal pairing: asset %s not solvable — keeping FIFO.", asset_id)
        return None

    template = rgls[0]
    new_rgls: List[RealizedGainLoss] = []
    used: Dict[int, Decimal] = defaultdict(lambda: ZERO)
    plan: Dict[str, List[Tuple[str, Decimal]]] = {}
    consumed: List[ConsumedPortion] = []
    for a in assignments:
        lot, supply = lots[a.supply_index], supplies[a.supply_index]
        sale, demand = sales[a.demand_index], demands[a.demand_index]
        qty = a.quantity
        used[a.supply_index] += qty
        total_cost = qty * supply.unit_cost_eur
        total_proceeds = qty * sale.unit_proceeds_eur
        rgl = RealizedGainLoss(
            originating_event_id=sale.event_id,
            asset_internal_id=template.asset_internal_id,
            asset_category_at_realization=template.asset_category_at_realization,
            acquisition_date=lot.acquisition_date,
            realization_date=sale.sale_date,
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=qty,
            unit_cost_basis_eur=supply.unit_cost_eur,
            unit_realization_value_eur=sale.unit_proceeds_eur,
            total_cost_basis_eur=total_cost,
            total_realization_value_eur=total_proceeds,
            gross_gain_loss_eur=total_proceeds - total_cost,
            holding_period_days=(demand.sale_date - supply.holding_start).days,
            holding_period_start=lot.holding_period_start,
            holding_period_start_estimated=bool(lot.holding_period_start_estimated),
            fund_type_at_sale=template.fund_type_at_sale,
            is_acquisition_estimated=bool(lot.acquisition_date_estimated),
        )
        if lot.cost_components is not None:
            from decimal import getcontext
            rgl.cost_basis_components_eur = _scale_components(
                lot.cost_components, total_cost, getcontext())
        if classifier is not None:
            classifier(rgl)
        new_rgls.append(rgl)
        consumed.append(ConsumedPortion(
            sale_sequence=sale.sequence, lot=_part_of(lot, qty)))
        if sale.ibkr_transaction_id:
            plan.setdefault(str(sale.ibkr_transaction_id), []).append(
                (plan_key(lot), qty))

    # W05: the open lots are what the assignment left, not what FIFO left --
    # and the journal says what the assignment consumed, not what FIFO did.
    remaining = []
    for index, lot in enumerate(lots):
        left = lot.quantity - used[index]
        if left > Decimal("1e-10"):
            remaining.append(_part_of(lot, left))
    ledger.replace_open_long_lots(remaining, consumed_portions=consumed)
    return new_rgls, plan


def rematch_cz_optimal(
    realized_gains_losses: List[RealizedGainLoss],
    fifo_ledgers: Dict,
    all_financial_events: List[FinancialEvent],
    tax_year: int,
    classifier: Optional[Callable[[RealizedGainLoss], None]] = None,
    config: Optional[CzTaxConfig] = None,
    fx=None,
) -> OptimalPairingOutcome:
    """Re-match eligible assets tax-optimally; see the module docstring.

    *fx* (a ``CzCurrencyConverter``) makes the objective the CZK base of
    that FX mode; without it the EUR gain is minimised.
    """
    config = config or CzTaxConfig()
    complex_ids = _complex_asset_ids(all_financial_events, tax_year)
    exempt = _time_test_predicate(config)

    def _mul(a: Decimal, b: Decimal) -> Decimal:
        return a * b

    by_asset: "defaultdict[object, List[RealizedGainLoss]]" = defaultdict(list)
    for r in realized_gains_losses:
        by_asset[r.asset_internal_id].append(r)

    outcome = OptimalPairingOutcome(realized_gains_losses=[])
    for asset_id, rgls in by_asset.items():
        if not _asset_eligible(rgls, asset_id, complex_ids):
            outcome.realized_gains_losses.extend(rgls)
            continue
        ledger = fifo_ledgers.get(asset_id)
        if ledger is not None and getattr(ledger, "tax_year_sales", None):
            done = _rematch_from_journal(asset_id, rgls, ledger, exempt, classifier, fx)
            if done is None:
                outcome.realized_gains_losses.extend(rgls)
                continue
            new_rgls, plan = done
            outcome.realized_gains_losses.extend(new_rgls)
            outcome.plan.update(plan)
            outcome.rematched_asset_ids.add(asset_id)
            continue
        # No journal (a caller holding only RGLs): reconstruct the supply from
        # the disposals and the surviving lots, as before. Nothing to rebuild.
        supplies = _build_supplies(rgls, ledger)
        demands = _build_demands(rgls)
        if supplies is None or demands is None:
            outcome.realized_gains_losses.extend(rgls)
            continue
        assignments = solve_optimal_matching(supplies, demands, exempt)
        if assignments is None:
            logger.info(
                "Optimal pairing: asset %s not solvable — keeping FIFO.", asset_id
            )
            outcome.realized_gains_losses.extend(rgls)
            continue
        outcome.realized_gains_losses.extend(
            _emit_rgls(rgls[0], supplies, demands, assignments, _mul, classifier))
        outcome.rematched_asset_ids.add(asset_id)

    logger.info(
        "Optimal pairing: re-matched %d asset(s); %d kept on FIFO.",
        len(outcome.rematched_asset_ids),
        len(by_asset) - len(outcome.rematched_asset_ids),
    )
    return outcome


def apply_cz_optimal_pairing(
    realized_gains_losses: List[RealizedGainLoss],
    fifo_ledgers: Dict,
    all_financial_events: List[FinancialEvent],
    tax_year: int,
    classifier: Optional[Callable[[RealizedGainLoss], None]] = None,
    config: Optional[CzTaxConfig] = None,
) -> List[RealizedGainLoss]:
    """Return a new RGL list with eligible assets re-matched tax-optimally
    (EUR objective). See ``rematch_cz_optimal`` for the plan and the CZK one."""
    return rematch_cz_optimal(
        realized_gains_losses, fifo_ledgers, all_financial_events, tax_year,
        classifier=classifier, config=config,
    ).realized_gains_losses
