# src/countries/cz/currency_gains.py
"""Czech §10 verdicts over the cash ledger's realisations.

``engine/currency_ledger`` says what each cash movement disposed of and what it
had cost. This module says which of those the Czech code counts, and it is
where the tax advisor's two rulings of 2026-08-15 live:

**Narrow reading (the default).** §10 speaks of a *směna peněz z účtu vedeného
v cizí měně* — an exchange of money — not of every use of currency. Paying a
purchase price for a share is payment, not exchange, so only genuine currency
conversions are recognised. (NSS 9 Afs 12/2007-43 on cash and non-cash currency
conversions; by analogy KS Brno 30 Af 29/2020-48, that paying for goods with
bitcoin is not itself such an exchange. No GFŘ statement or NSS ruling covers
paying for a share with legal foreign currency, so the broad reading stays
available as a switch.)

**The narrow reading changes only recognition, never the inventory.** The
ledger already replays every movement for exactly this reason; here the two
readings differ by one predicate and share one FIFO.

**Debt is not a negative holding.** Repaying borrowed currency realises a
mirrored result whose treatment is unsettled — §10 covers exchanging one's own
money, and while NSS 5 Afs 45/2011-94 allowed an FX result on repaying a debt,
it concerned a legal person keeping books. For an individual the default is to
compute it and report it, but keep it out of the §10 base, and never to net a
short loss against long gains.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional

from src.engine.currency_ledger import (
    CurrencyLedger,
    MovementDateField,
    Realisation,
    RealisationKind,
    verify_against_statement,
)
from src.parsers.raw_models import RawStatementOfFundsRecord
from src.parsers.statement_of_funds_parser import conversions
from src.utils.type_utils import parse_ibkr_date

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


class CzCurrencyRecognition(Enum):
    """Which disposals of foreign currency §10 is taken to reach."""

    #: Only genuine currency conversions. The advisor's default.
    NARROW = "narrow"
    #: Every payment out of a foreign-currency balance as well.
    BROAD = "broad"


@dataclass
class CzCurrencyRealisation:
    """One realisation with the Czech verdict attached."""

    realisation: Realisation
    #: A leg of a genuine conversion, as opposed to a payment or a fee.
    is_conversion_leg: bool

    @property
    def gain_czk(self) -> Optional[Decimal]:
        return self.realisation.gain

    @property
    def is_determined(self) -> bool:
        return self.realisation.is_determined

    def recognised_under(self, recognition: CzCurrencyRecognition) -> bool:
        """Whether §10 counts this one under *recognition*.

        Debt repayments are never counted here — they are their own disputed
        category and are reported apart, not folded into either reading.
        """
        if self.realisation.kind is not RealisationKind.LONG_DISPOSAL:
            return False
        if recognition is CzCurrencyRecognition.BROAD:
            return True
        return self.is_conversion_leg


@dataclass
class CzCurrencyGains:
    """Every currency realisation of the run, and the totals per scenario."""

    realisations: List[CzCurrencyRealisation] = field(default_factory=list)
    #: Messages from replaying the statement against IBKR's own Balance
    #: column. Non-empty means the reconstruction is not trustworthy and no
    #: figure from it should be published.
    ledger_problems: List[str] = field(default_factory=list)

    def for_transaction(self, transaction_id: str) -> List[CzCurrencyRealisation]:
        """The realisations belonging to one IBKR transaction.

        The join is exact: a FOREX row's ``TransactionID`` in trades.csv — the
        id that reaches ``CurrencyConversionEvent.ibkr_transaction_id`` — is
        the same id the Statement of Funds puts on that conversion's legs.
        Checked across 2024-2026 on the book this was built against: 89 of 89.
        """
        tid = (transaction_id or "").strip()
        if not tid:
            return []
        return [r for r in self.realisations
                if r.realisation.source_id == tid
                and r.realisation.kind is RealisationKind.LONG_DISPOSAL]

    def total(
        self,
        recognition: CzCurrencyRecognition,
        year: Optional[int] = None,
    ) -> Decimal:
        """§10 total under *recognition*. Undetermined items contribute zero
        to the sum and are counted by ``undetermined`` instead — a figure that
        cannot be stated must not be smuggled in as a nil one."""
        return sum(
            (r.gain_czk for r in self._in_year(year)
             if r.recognised_under(recognition) and r.is_determined),
            ZERO,
        )

    def short_fx_total(self, year: Optional[int] = None) -> Decimal:
        """The mirrored result of repaying borrowed currency. Informative:
        outside the §10 base unless the taxpayer's advisor says otherwise."""
        return sum(
            (r.gain_czk for r in self._in_year(year)
             if r.realisation.kind is RealisationKind.DEBT_REPAYMENT
             and r.is_determined),
            ZERO,
        )

    def undetermined(self, year: Optional[int] = None) -> List[CzCurrencyRealisation]:
        """Realisations whose gain could not be established at all."""
        return [r for r in self._in_year(year) if not r.is_determined]

    def _in_year(self, year: Optional[int]) -> List[CzCurrencyRealisation]:
        if year is None:
            return self.realisations
        return [r for r in self.realisations
                if r.realisation.on and r.realisation.on.year == year]


def compute_currency_gains(
    records: Iterable[RawStatementOfFundsRecord],
    rate_lookup: Callable,
    home_currency: str = "CZK",
    date_field: MovementDateField = MovementDateField.SETTLE_DATE,
) -> CzCurrencyGains:
    """Replay the cash ledger and rule on what it realised.

    *records* must span EVERY year the book covers, not just the tax year:
    a dollar sold this year may have arrived two years ago, and so may the
    draw of a debt repaid now.
    """
    records = list(records)
    problems = verify_against_statement(records)
    if problems:
        logger.error(
            "Statement of Funds does not replay to IBKR's own balances (%d "
            "mismatches, first: %s). The currency FIFO is not trustworthy on "
            "this input.", len(problems), problems[0],
        )

    # Identity, not the transaction id: IBKR bills a conversion's fee under
    # the legs' own id on some trades and its own on others, so the id cannot
    # separate a leg from a charge. The ledger hands back the very record it
    # consumed, which can.
    leg_records = {id(leg) for conv in conversions(records) for leg in conv.legs}

    # A conversion is one taxable event on one date, so its rows are grouped
    # and given a single shared date. Every row of the trade goes in the group,
    # charges included — a commission cannot be consumed between the two sides
    # of the exchange it belongs to.
    conversion_legs: Dict[int, str] = {}
    conversion_dates: Dict[str, Optional[object]] = {}
    for conv in conversions(records):
        group = f"conv:{conv.trade_id}"
        for rec in list(conv.legs) + list(conv.charges):
            conversion_legs[id(rec)] = group
        conversion_dates[group] = _conversion_date(conv, date_field)

    ledger = CurrencyLedger(rate_lookup=rate_lookup, home_currency=home_currency,
                            date_field=date_field)
    out = CzCurrencyGains(ledger_problems=problems)
    for realisation in ledger.replay(records, conversion_legs, conversion_dates):
        out.realisations.append(CzCurrencyRealisation(
            realisation=realisation,
            is_conversion_leg=id(realisation.source_record) in leg_records,
        ))
    return out


def _conversion_date(conv, date_field: MovementDateField):
    """The single date a whole conversion is attributed to.

    Under settlement this is simply the legs' shared settle date — they have
    never been seen to differ. Under the booking date they can, on 19 of 89
    conversions of the book this was built against, and the LATER one is the
    execution's own: the execution-level ``trades.csv`` reports exactly that
    single TradeDate for each of those 19. The earlier leg is the artefact, so
    taking the maximum reproduces what IBKR itself calls the trade date rather
    than inventing a third answer.
    """
    dates = [parse_ibkr_date(getattr(leg, date_field.value, None))
             for leg in conv.legs]
    dates = [d for d in dates if d is not None]
    if not dates:
        return None
    return max(dates)


def rate_lookup_from_converter(converter) -> Callable:
    """Adapt a ``CzCurrencyConverter`` to the ledger's rate contract.

    Converting one unit yields exactly the CZK-per-unit rate, and going
    through the converter rather than the provider keeps the FX policy of the
    run — daily ČNB rate or the yearly uniform rate, weekend fallback and all
    — instead of quietly introducing a second one.
    """
    def _lookup(on, currency):
        record = converter.convert_to_czk(Decimal("1"), currency, on)
        return record.converted_amount_czk if record else None
    return _lookup
