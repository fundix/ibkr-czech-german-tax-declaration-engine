# src/engine/pairing.py
"""
Pairing-method (lot-matching) strategies for disposals.

A CZ private (non-business) investor filing §10 ZDP may choose *any* method
for matching sold securities to their purchase lots, provided purchases
precede sales (GFŘ výklad; only business-asset holders are restricted to
FIFO / weighted average per účetnictví). The chosen method decides *which*
lot is matched to each sale, which changes both the acquisition cost **and**
the time-test result (§4/1/u).

This module defines the method enum and the small, pure helpers the
``FifoLedger`` uses to vary its consumption order / costing. The global
tax-minimising ``OPTIMAL`` method is computed by ``pairing_solver`` (a
per-asset min-cost flow); at the ledger level it falls back to FIFO order
(used for historical replay and any leftover consumption).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import List, Mapping, Optional, Tuple


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


# One filed sale of an ``optimal`` year: which lots it consumed, as
# ((lot source_transaction_id, quantity), ...).
PlanEntry = Tuple[Tuple[str, Decimal], ...]


def _quantity_text(q: Decimal) -> str:
    """A canonical spelling of a quantity: 2, 2.0 and 2.00 are one quantity."""
    text = format(Decimal(q).normalize(), "f")
    return text


@dataclass(frozen=True)
class PairingHistory:
    """How each year BEFORE the tax year was filed.

    The start-of-year lots are rebuilt by replaying every earlier year; each
    year has to be replayed with the method it was filed with, or the opening
    lots (their cost and their dates) disagree with the returns already
    filed. A year that is not listed was filed FIFO, the statutory default.

    ``plans`` holds, for a year filed ``optimal``, the solver's assignment:
    sale IBKR TransactionID -> ((lot source_transaction_id, quantity), ...).
    The solver itself cannot be re-run for a past year (its answer depends on
    the rates and on fixes made since), so the filed answer is stored.
    """

    methods: Mapping[int, PairingMethod] = field(default_factory=dict)
    plans: Mapping[int, Mapping[str, PlanEntry]] = field(default_factory=dict)
    default: PairingMethod = PairingMethod.FIFO

    def method_for(self, year: int) -> PairingMethod:
        return coerce(self.methods.get(year, self.default))

    def plan_for(self, year: int, sale_tx_id) -> Optional[PlanEntry]:
        if not sale_tx_id:
            return None
        return (self.plans.get(year) or {}).get(str(sale_tx_id))

    def fingerprint(self) -> str:
        """A stable text naming this history, for a run's input fingerprint."""
        methods = ",".join(
            f"{year}:{coerce(m).value}" for year, m in sorted(self.methods.items())
        )
        canonical = {
            str(year): {
                str(tx): [[str(lot), _quantity_text(qty)] for lot, qty in entry]
                for tx, entry in sorted(plan.items())
            }
            for year, plan in sorted(self.plans.items())
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"methods={methods}|default={coerce(self.default).value}|plans={digest}"


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
