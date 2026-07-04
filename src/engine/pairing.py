# src/engine/pairing.py
"""
Pairing-method (lot-matching) strategies for disposals.

A CZ private (non-business) investor filing §10 ZDP may choose *any* method
for matching sold securities to their purchase lots, provided purchases
precede sales (GFŘ výklad; only business-asset holders are restricted to
FIFO / weighted average per účetnictví). The chosen method decides *which*
lot is matched to each sale, which changes both the acquisition cost **and**
the time-test result (§4/1/w).

This module defines the method enum and the small, pure helpers the
``FifoLedger`` uses to vary its consumption order / costing. The global
tax-minimising ``OPTIMAL`` method is computed by ``pairing_solver`` (a
per-asset min-cost flow); at the ledger level it falls back to FIFO order
(used for historical replay and any leftover consumption).
"""
from __future__ import annotations

from enum import Enum
from typing import List


class PairingMethod(str, Enum):
    """Lot-matching strategy for disposals of fungible securities."""

    FIFO = "fifo"                       # oldest lots first (statutory default)
    LIFO = "lifo"                       # newest lots first
    WEIGHTED_AVERAGE = "weighted_average"  # blended pool cost (vážený průměr)
    OPTIMAL = "optimal"                 # global tax-minimising solver

    @property
    def label_cs(self) -> str:
        return {
            PairingMethod.FIFO: "FIFO (nejstarší první)",
            PairingMethod.LIFO: "LIFO (nejnovější první)",
            PairingMethod.WEIGHTED_AVERAGE: "Vážený průměr",
            PairingMethod.OPTIMAL: "Daňově optimální",
        }[self]


# Methods that vary how the per-asset FIFO ledger consumes lots. OPTIMAL is
# realised by a separate solver, so at the ledger level it behaves as FIFO.
LEDGER_ORDERING_METHODS = (
    PairingMethod.FIFO,
    PairingMethod.LIFO,
    PairingMethod.WEIGHTED_AVERAGE,
)

# All methods offered on the pairing axis (order = display order).
ALL_METHODS = (
    PairingMethod.FIFO,
    PairingMethod.LIFO,
    PairingMethod.WEIGHTED_AVERAGE,
    PairingMethod.OPTIMAL,
)


def coerce(method) -> PairingMethod:
    """Accept a PairingMethod or its string value; default to FIFO on None."""
    if method is None:
        return PairingMethod.FIFO
    if isinstance(method, PairingMethod):
        return method
    return PairingMethod(str(method))


def consumption_order_indices(num_lots: int, method: PairingMethod) -> List[int]:
    """Indices into a FIFO-sorted (oldest-first) lot list, in consumption order.

    FIFO / WEIGHTED_AVERAGE / OPTIMAL consume oldest-first (WA keeps FIFO lot
    *identity* for the time test but overrides the cost — see
    ``uses_pool_average_cost``); LIFO consumes newest-first.
    """
    if method == PairingMethod.LIFO:
        return list(range(num_lots - 1, -1, -1))
    return list(range(num_lots))


def uses_pool_average_cost(method: PairingMethod) -> bool:
    """True when the disposed lots are costed at the blended pool average."""
    return method == PairingMethod.WEIGHTED_AVERAGE
