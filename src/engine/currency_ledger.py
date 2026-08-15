# src/engine/currency_ledger.py
"""FIFO inventory over the CASH held in each currency.

The cash counterpart of ``fifo_manager``'s securities FIFO, and it differs in
the one way that decides every figure it produces: a cash balance may go
NEGATIVE, and on a margin account a negative balance is a debt to the broker,
not a negative holding. Drawing it disposes of nothing, and repaying it
realises a result that mirrors a holding's. So each currency keeps TWO queues
and neither is ever the other's negative:

    outgoing  ->  consume long lots first;  any excess below zero OPENS a debt
    incoming  ->  repay debt lots first;    only a surplus above zero OPENS a lot

Every settled movement is replayed, not only the currency conversions. A
purchase paid for in dollars may not itself be a taxable currency event, but it
certainly spends dollars, and the layers it consumes are gone by the time the
next conversion looks for them. Leaving purchases out would measure later
conversions against layers that were spent long ago. Which movements are
RECOGNISED for tax is a separate question, answered by the caller over the
realisations this ledger returns — never by skipping a movement here.

The ledger deals in mechanics only. It knows nothing of any tax code: it says
what was disposed of, what it had cost, and what it fetched. Whether that is
taxable income belongs to a country layer.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date as date_type
from decimal import Decimal
from enum import Enum, auto
from typing import Callable, Deque, Dict, Iterable, List, Optional

from src.parsers.raw_models import RawStatementOfFundsRecord
from src.utils.type_utils import parse_ibkr_date

logger = logging.getLogger(__name__)

ZERO = Decimal("0")

# Home-currency units per one unit of the foreign currency, for a given date.
# Returning None means "no rate available", which propagates into the result as
# an undetermined figure rather than a zero one.
RateLookup = Callable[[date_type, str], Optional[Decimal]]


class MovementDateField(Enum):
    """Which of IBKR's two dates orders the layers and picks the rate.

    They disagree on most rows — 169 of 267 on the book this was built
    against — so the choice is a real one and is left to the caller rather
    than being hardcoded. ``DATE`` is the economic date and is what the rest
    of this engine already converts on.
    """
    DATE = "date"
    SETTLE_DATE = "settle_date"


class RealisationKind(Enum):
    """Which queue produced a result, because the two are taxed differently.

    ``LONG_DISPOSAL`` is giving up currency that was held. ``DEBT_REPAYMENT``
    is paying back currency that was borrowed; its result is mirrored (the
    debt getting cheaper in home-currency terms is a gain) and, in Czech law
    at least, its treatment is unsettled — hence a separate kind rather than
    a sign convention the caller has to remember.
    """
    LONG_DISPOSAL = auto()
    DEBT_REPAYMENT = auto()


@dataclass
class CashLayer:
    """One acquisition (or one draw) still open, and where it came from.

    ``rate`` is the home-currency rate on the day the layer opened. It may be
    None when no rate could be had for that day; the result then reports
    itself undetermined instead of silently valuing the layer at zero.
    """
    quantity: Decimal
    opened_on: Optional[date_type]
    rate: Optional[Decimal]
    source_id: str
    activity: str


@dataclass
class ConsumedLayer:
    """The part of a layer that one movement took — the audit trail.

    Every figure this module returns can be traced back to these: how much
    came from which acquisition, on what date, at what rate.
    """
    quantity: Decimal
    opened_on: Optional[date_type]
    rate: Optional[Decimal]
    source_id: str
    activity: str

    @property
    def home_value(self) -> Optional[Decimal]:
        """What this slice was worth when its layer opened."""
        return None if self.rate is None else self.quantity * self.rate


@dataclass
class Realisation:
    """An FX result the ledger realised, before anyone rules on its taxability.

    Sign convention is the holder's: positive is a gain. For a
    ``DEBT_REPAYMENT`` that means the mirror — a debt repaid when the currency
    is weaker than when it was drawn is a gain.
    """
    kind: RealisationKind
    currency: str
    quantity: Decimal
    on: Optional[date_type]
    rate: Optional[Decimal]
    activity: str
    source_id: str
    layers: List[ConsumedLayer] = field(default_factory=list)

    @property
    def movement_value(self) -> Optional[Decimal]:
        """The disposed (or repaid) amount at the movement's own rate."""
        return None if self.rate is None else self.quantity * self.rate

    @property
    def layer_value(self) -> Optional[Decimal]:
        """What the consumed layers had been worth when they opened."""
        if any(l.rate is None for l in self.layers):
            return None
        return sum((l.home_value for l in self.layers), ZERO)

    @property
    def is_determined(self) -> bool:
        """Whether a gain can be stated at all.

        A missing rate anywhere makes the figure unknowable. The advisor's
        instruction is explicit: report that it cannot be determined, never
        report zero.
        """
        return self.movement_value is not None and self.layer_value is not None

    @property
    def gain(self) -> Optional[Decimal]:
        """Home-currency result, or None when it cannot be determined."""
        if not self.is_determined:
            return None
        if self.kind is RealisationKind.LONG_DISPOSAL:
            return self.movement_value - self.layer_value
        # Mirrored: what the debt was worth when drawn, less what repaying it
        # cost. Borrow 100 USD at 23 and repay at 22 and the holder is 100 up.
        return self.layer_value - self.movement_value


class CurrencyLedger:
    """Replays cash movements and reports what each of them realised.

    Movements are applied in the order given, which is deliberate: IBKR's
    Statement of Funds is already in the canonical order, proven by its own
    running ``Balance`` column reproducing exactly when the rows are summed
    top to bottom. ``balances`` can be compared against that column after
    every row, which is the cheapest correctness proof available here — see
    ``verify_against_statement``.

    The home currency is tracked like any other so balances stay checkable,
    but it never realises anything: koruna held against the koruna cannot
    move.
    """

    def __init__(
        self,
        rate_lookup: RateLookup,
        home_currency: str = "CZK",
        date_field: MovementDateField = MovementDateField.DATE,
    ):
        self.home_currency = home_currency.upper()
        self.date_field = date_field
        self._rate_lookup = rate_lookup
        self._long: Dict[str, Deque[CashLayer]] = defaultdict(deque)
        self._debt: Dict[str, Deque[CashLayer]] = defaultdict(deque)
        self._balances: Dict[str, Decimal] = defaultdict(Decimal)
        # Only the earliest opening balance per currency is honoured; see
        # seed_opening_balance for why re-seeding would be a revaluation.
        self._seeded: set = set()

    # ------------------------------------------------------------------
    # Reading the state
    # ------------------------------------------------------------------

    @property
    def balances(self) -> Dict[str, Decimal]:
        return dict(self._balances)

    def open_lots(self, currency: str) -> List[CashLayer]:
        return list(self._long[currency.upper()])

    def open_debts(self, currency: str) -> List[CashLayer]:
        return list(self._debt[currency.upper()])

    def position(self, currency: str) -> Decimal:
        """Net position: held minus borrowed. Equals the broker's balance."""
        cur = currency.upper()
        return (sum((l.quantity for l in self._long[cur]), ZERO)
                - sum((d.quantity for d in self._debt[cur]), ZERO))

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay(
        self, records: Iterable[RawStatementOfFundsRecord]
    ) -> List[Realisation]:
        """Apply every movement in order and collect what they realised.

        Balance-marker rows are skipped except for the opening one of each
        currency, which seeds the ledger. Callers that already filtered to
        movements should seed the opening balances themselves via
        ``seed_opening_balance``.
        """
        out: List[Realisation] = []
        for rec in records:
            if rec.is_starting_balance:
                self.seed_opening_balance(rec)
                continue
            if rec.is_balance_row:
                continue
            out.extend(self.apply(rec))
        return out

    def seed_opening_balance(self, rec: RawStatementOfFundsRecord) -> None:
        """Open the ledger for a currency at its reported starting balance.

        Only the FIRST seed per currency counts, so a ledger fed several
        years of statements takes the earliest opening and lets the movements
        carry it forward — re-seeding at each year boundary would throw away
        the layers behind the balance and, worse, revalue them at the 1
        January rate. That revaluation is exactly what must not happen: it
        would invent an acquisition cost the holder never paid.

        A seeded balance carries NO rate, because the statement does not say
        when the currency behind it was acquired. Results consuming a seeded
        layer therefore report themselves undetermined. A book whose history
        starts at zero never has one, which is the case worth aiming for.
        """
        cur = (rec.currency_primary or "").upper()
        if not cur or cur in self._seeded:
            return
        self._seeded.add(cur)
        amount = rec.balance or ZERO
        self._balances[cur] = amount
        if amount == ZERO:
            return
        layer = CashLayer(
            quantity=abs(amount),
            opened_on=self._date_of(rec),
            rate=None,
            source_id=f"opening:{cur}",
            activity="OPENING BALANCE",
        )
        (self._long if amount > ZERO else self._debt)[cur].append(layer)

    def apply(self, rec: RawStatementOfFundsRecord) -> List[Realisation]:
        """Apply one movement, returning whatever it realised (often nothing)."""
        cur = (rec.currency_primary or "").upper()
        amount = rec.amount or ZERO
        if not cur or amount == ZERO:
            return []

        on = self._date_of(rec)
        rate = self._rate_for(on, cur)
        activity = (rec.activity_code or "").strip().upper() or "?"
        source_id = (rec.transaction_id or rec.trade_id or "").strip()

        self._balances[cur] += amount
        if amount < ZERO:
            return self._outgoing(cur, -amount, on, rate, activity, source_id)
        return self._incoming(cur, amount, on, rate, activity, source_id)

    # ------------------------------------------------------------------
    # The two directions
    # ------------------------------------------------------------------

    def _outgoing(self, cur, quantity, on, rate, activity, source_id):
        """Currency leaving: held layers go first, the rest becomes debt."""
        taken = self._consume(self._long[cur], quantity)
        realisations: List[Realisation] = []
        consumed = sum((l.quantity for l in taken), ZERO)
        if consumed > ZERO and cur != self.home_currency:
            realisations.append(Realisation(
                kind=RealisationKind.LONG_DISPOSAL, currency=cur,
                quantity=consumed, on=on, rate=rate, activity=activity,
                source_id=source_id, layers=taken,
            ))
        shortfall = quantity - consumed
        if shortfall > ZERO:
            # Below zero: this is drawing on the broker's money, not disposing
            # of the holder's. Nothing is realised (§3/4/b ZDP — receiving
            # loan principal is not income); the draw's own rate is recorded
            # so repaying it later can be measured against it.
            self._debt[cur].append(CashLayer(
                quantity=shortfall, opened_on=on, rate=rate,
                source_id=source_id, activity=activity,
            ))
        return realisations

    def _incoming(self, cur, quantity, on, rate, activity, source_id):
        """Currency arriving: it repays debt before it becomes a holding."""
        repaid = self._consume(self._debt[cur], quantity)
        realisations: List[Realisation] = []
        settled = sum((l.quantity for l in repaid), ZERO)
        if settled > ZERO and cur != self.home_currency:
            realisations.append(Realisation(
                kind=RealisationKind.DEBT_REPAYMENT, currency=cur,
                quantity=settled, on=on, rate=rate, activity=activity,
                source_id=source_id, layers=repaid,
            ))
        surplus = quantity - settled
        if surplus > ZERO:
            self._long[cur].append(CashLayer(
                quantity=surplus, opened_on=on, rate=rate,
                source_id=source_id, activity=activity,
            ))
        return realisations

    @staticmethod
    def _consume(queue: Deque[CashLayer], quantity: Decimal) -> List[ConsumedLayer]:
        """Take ``quantity`` off the front of a queue, splitting the last layer.

        Returns less than asked for when the queue runs out; the caller
        decides what the shortfall means, which differs by direction.
        """
        taken: List[ConsumedLayer] = []
        left = quantity
        while left > ZERO and queue:
            layer = queue[0]
            slice_qty = min(left, layer.quantity)
            taken.append(ConsumedLayer(
                quantity=slice_qty, opened_on=layer.opened_on,
                rate=layer.rate, source_id=layer.source_id,
                activity=layer.activity,
            ))
            layer.quantity -= slice_qty
            left -= slice_qty
            if layer.quantity <= ZERO:
                queue.popleft()
        return taken

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _date_of(self, rec: RawStatementOfFundsRecord) -> Optional[date_type]:
        raw = getattr(rec, self.date_field.value, None)
        return parse_ibkr_date(raw)

    def _rate_for(self, on: Optional[date_type], currency: str) -> Optional[Decimal]:
        if on is None:
            return None
        if currency == self.home_currency:
            return Decimal("1")
        try:
            return self._rate_lookup(on, currency)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"No {currency} rate for {on}: {exc}. The results consuming "
                "this movement will report as undetermined."
            )
            return None


def verify_against_statement(
    records: Iterable[RawStatementOfFundsRecord],
) -> List[str]:
    """Check that summing the rows reproduces IBKR's own ``Balance`` column.

    The statement carries a running balance on every movement row, so a
    reconstruction can be proven right rather than merely believed: replay the
    amounts top to bottom and every balance must land on IBKR's. A mismatch
    means a dropped, duplicated or misordered row — the three ways a currency
    FIFO silently produces wrong gains — so callers should treat any returned
    message as a reason not to publish a figure.

    Returns one message per mismatch; an empty list is a clean bill.
    """
    running: Dict[str, Decimal] = {}
    seeded: set = set()
    problems: List[str] = []
    for rec in records:
        cur = (rec.currency_primary or "").upper()
        if not cur:
            continue
        if rec.is_starting_balance:
            if cur not in seeded:
                seeded.add(cur)
                running[cur] = rec.balance or ZERO
            continue
        if rec.is_balance_row:
            continue
        running[cur] = running.get(cur, ZERO) + (rec.amount or ZERO)
        if rec.balance is not None and running[cur] != rec.balance:
            problems.append(
                f"{cur} {rec.date}: replay gives {running[cur]}, statement "
                f"says {rec.balance} (transaction {rec.transaction_id})"
            )
    return problems
