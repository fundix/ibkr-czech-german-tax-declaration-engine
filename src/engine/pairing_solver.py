# src/engine/pairing_solver.py
"""
Self-contained min-cost-flow solver for tax-optimal lot matching.

A CZ private investor may match any purchase lot to any sale (purchases must
precede sales). The tax-minimising matching is the assignment of sale demand
to lot supply that minimises the §10 taxable net gain: a match whose holding
period passes the time test contributes 0 to the base, otherwise it
contributes its unit gain (losses stay in the base to offset gains). Routing
gains onto exempt (old) lots and losses onto taxable lots minimises tax.

Because a lot of asset X can only fill a sale of asset X, the global problem
decomposes into one small **transportation problem per asset**, solved here
with a successive-shortest-paths min-cost flow (SPFA/Bellman-Ford shortest
paths, so negative arc costs from loss matches are handled). No third-party
dependency — the per-asset graphs are tiny.

The objective is exact for the base + rate part of the liability; the 100k
all-or-nothing cliff can make the true optimum non-convex, but the caller
always scores the resulting matching with the real aggregator and reports the
cheapest method, so ``optimal`` is never worse than plain FIFO.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_obj
from decimal import Decimal
from typing import Callable, List, Optional, Tuple

ZERO = Decimal(0)
_INF = Decimal("1e30")


@dataclass
class SupplyLot:
    """A purchase lot available to fill sales of one asset."""
    acq_date: date_obj
    quantity: Decimal
    unit_cost_eur: Decimal
    source_id: str
    # Synthetic SOY-fallback acquisition (31 Dec) — the real date is unknown,
    # so downstream keeps it taxable; the solver must not route gains here.
    estimated: bool = False


@dataclass
class SaleDemand:
    """A tax-year sale (disposal) of one asset that must be fully matched."""
    sale_date: date_obj
    quantity: Decimal
    unit_proceeds_eur: Decimal
    originating_event_id: object


@dataclass
class Assignment:
    """One matched (lot → sale) portion produced by the solver."""
    supply_index: int
    demand_index: int
    quantity: Decimal


class _MinCostFlow:
    """Min-cost max-flow via SPFA successive shortest paths (Decimal capacities)."""

    def __init__(self, num_nodes: int):
        self.n = num_nodes
        # each edge: [to, capacity, cost, rev_index]
        self.graph: List[List[list]] = [[] for _ in range(num_nodes)]

    def add_edge(self, u: int, v: int, cap: Decimal, cost: Decimal) -> None:
        self.graph[u].append([v, cap, cost, len(self.graph[v])])
        self.graph[v].append([u, ZERO, -cost, len(self.graph[u]) - 1])

    def _shortest_path(self, s: int, t: int) -> Optional[Tuple[List[int], List[int]]]:
        dist = [_INF] * self.n
        in_queue = [False] * self.n
        prev_node = [-1] * self.n
        prev_edge = [-1] * self.n
        dist[s] = ZERO
        queue = [s]
        in_queue[s] = True
        while queue:
            u = queue.pop(0)
            in_queue[u] = False
            for ei, edge in enumerate(self.graph[u]):
                v, cap, cost, _rev = edge
                if cap > ZERO and dist[u] + cost < dist[v]:
                    dist[v] = dist[u] + cost
                    prev_node[v] = u
                    prev_edge[v] = ei
                    if not in_queue[v]:
                        queue.append(v)
                        in_queue[v] = True
        if dist[t] >= _INF:
            return None
        return prev_node, prev_edge

    def solve(self, s: int, t: int, max_flow: Decimal) -> Decimal:
        """Push up to ``max_flow`` units s→t at minimum cost; returns flow pushed."""
        pushed_total = ZERO
        guard = 0
        max_iters = 4 * (sum(len(a) for a in self.graph) + self.n) + 16
        while pushed_total < max_flow:
            guard += 1
            if guard > max_iters:
                break
            sp = self._shortest_path(s, t)
            if sp is None:
                break
            prev_node, prev_edge = sp
            # bottleneck along the path
            bottleneck = max_flow - pushed_total
            v = t
            while v != s:
                u = prev_node[v]
                edge = self.graph[u][prev_edge[v]]
                if edge[1] < bottleneck:
                    bottleneck = edge[1]
                v = u
            if bottleneck <= ZERO:
                break
            v = t
            while v != s:
                u = prev_node[v]
                edge = self.graph[u][prev_edge[v]]
                edge[1] -= bottleneck
                self.graph[v][edge[3]][1] += bottleneck
                v = u
            pushed_total += bottleneck
        return pushed_total


def solve_optimal_matching(
    supplies: List[SupplyLot],
    demands: List[SaleDemand],
    is_exempt: Callable[[SupplyLot, SaleDemand], bool],
) -> Optional[List[Assignment]]:
    """Return the tax-minimising lot→sale assignment, or None if infeasible.

    ``is_exempt(lot, sale)`` decides whether a match passes the time test
    (→ zero-cost arc). ``None`` means the total demand could not be met from
    the eligible supply — the caller should fall back to FIFO.
    """
    if not demands:
        return []
    if not supplies:
        return None

    total_demand = sum((d.quantity for d in demands), ZERO)
    total_supply = sum((s.quantity for s in supplies), ZERO)
    if total_supply < total_demand:
        return None

    L = len(supplies)
    S = len(demands)
    source = 0
    lot_base = 1
    sale_base = 1 + L
    sink = 1 + L + S
    flow = _MinCostFlow(sink + 1)

    for i, lot in enumerate(supplies):
        flow.add_edge(source, lot_base + i, lot.quantity, ZERO)
    for j, sale in enumerate(demands):
        flow.add_edge(sale_base + j, sink, sale.quantity, ZERO)

    has_feasible_arc = [False] * S
    for i, lot in enumerate(supplies):
        for j, sale in enumerate(demands):
            if lot.acq_date > sale.sale_date:
                continue  # purchases must precede sales
            has_feasible_arc[j] = True
            if is_exempt(lot, sale):
                cost = ZERO
            else:
                cost = sale.unit_proceeds_eur - lot.unit_cost_eur
            cap = lot.quantity if lot.quantity < sale.quantity else sale.quantity
            flow.add_edge(lot_base + i, sale_base + j, cap, cost)

    if not all(has_feasible_arc):
        return None  # some sale has no lot acquired before it — fall back

    pushed = flow.solve(source, sink, total_demand)
    if pushed < total_demand - Decimal("1e-9"):
        return None

    assignments: List[Assignment] = []
    for i in range(L):
        for edge in flow.graph[lot_base + i]:
            v, cap, cost, rev = edge
            if sale_base <= v < sink:
                # flow on this arc = original cap - residual cap = reverse residual
                used = flow.graph[v][rev][1]
                if used > Decimal("1e-12"):
                    assignments.append(Assignment(i, v - sale_base, used))
    return assignments
