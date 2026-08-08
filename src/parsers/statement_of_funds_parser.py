# src/parsers/statement_of_funds_parser.py
"""Parser for IBKR's Statement of Funds — the per-currency cash ledger.

The only statement that carries cash balances, and therefore the only source
of the rate at which a held currency was acquired. Kept standalone, like
``positions_parser``, so a caller can read it without running the engine.
"""
import csv
import logging
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import ValidationError

from .raw_models import RawStatementOfFundsRecord

logger = logging.getLogger(__name__)


def parse_statement_of_funds_csv(
    file_path: str, encoding: str = "utf-8-sig"
) -> List[RawStatementOfFundsRecord]:
    """Read every row, balance markers included.

    Rows whose currency or shape cannot be parsed are logged and skipped
    rather than dropped silently — a missing movement would corrupt a running
    balance, and a corrupted balance produces a wrong gain.
    """
    records: List[RawStatementOfFundsRecord] = []
    try:
        with open(file_path, mode="r", encoding=encoding) as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                # A repeated header row (some Flex layouts emit one per
                # section) parses as data with the column names as values.
                if (row.get("CurrencyPrimary") or "").strip() == "CurrencyPrimary":
                    continue
                try:
                    records.append(RawStatementOfFundsRecord(**row))
                except ValidationError as exc:
                    logger.error(
                        f"Statement of Funds row {i + 2} failed validation and "
                        f"was skipped — a dropped movement corrupts the running "
                        f"balance: {exc.errors()}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        f"Statement of Funds row {i + 2} could not be parsed "
                        f"and was skipped: {exc}"
                    )
    except FileNotFoundError:
        logger.warning(f"Statement of Funds file not found: {file_path}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Error reading Statement of Funds {file_path}: {exc}")
    return records


def starting_balances(
    records: List[RawStatementOfFundsRecord],
) -> Dict[str, Optional["object"]]:
    """Opening balance per currency, from the ``Starting Balance`` markers.

    This is the number the whole query exists for: without it a currency FIFO
    has nothing to consume and goes negative on the first disposal.

    A balance may legitimately be **negative** — on a margin account that is
    borrowed currency, not an error. Repaying it later realises an FX result
    mirroring a holding's (a gain when the currency weakens), so callers must
    handle the sign rather than clamp it.
    """
    out: Dict[str, Optional[object]] = {}
    for rec in records:
        if rec.is_starting_balance and rec.currency_primary not in out:
            out[rec.currency_primary] = rec.balance
    return out


def movements(
    records: List[RawStatementOfFundsRecord],
) -> List[RawStatementOfFundsRecord]:
    """Only the real cash movements — balance markers filtered out."""
    return [r for r in records if not r.is_balance_row]


@dataclass
class Conversion:
    """One FX conversion, reassembled from the rows that make it up.

    ``legs`` are the two currency sides — one given up, one received. ``charges``
    are the ancillary rows IBKR books against the same trade, in practice the
    commission, which lands in whatever currency the commission is billed in
    and is NOT itself a currency disposal.
    """
    trade_id: str
    legs: List[RawStatementOfFundsRecord] = field(default_factory=list)
    charges: List[RawStatementOfFundsRecord] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """Both sides present. A single leg means the other fell outside the
        requested period, and the exchange rate cannot be derived from it."""
        return len(self.legs) == 2

    def leg_for(self, currency: str) -> Optional[RawStatementOfFundsRecord]:
        return next((l for l in self.legs
                     if l.currency_primary.upper() == currency.upper()), None)

    @property
    def disposed(self) -> Optional[RawStatementOfFundsRecord]:
        """The side given up — the negative one. That is what a gain is measured
        on; the other side is merely what it was exchanged for."""
        if not self.is_complete:
            return None
        return min(self.legs, key=lambda l: l.amount or Decimal(0))

    @property
    def received(self) -> Optional[RawStatementOfFundsRecord]:
        if not self.is_complete:
            return None
        return max(self.legs, key=lambda l: l.amount or Decimal(0))


def conversions(records: List[RawStatementOfFundsRecord]) -> List[Conversion]:
    """Reassemble FOREX rows into conversions, keyed by ``trade_id``.

    A conversion is **two or three** rows, not two:

    * ``Trading Currency Leg`` — carries the pair in ``symbol`` plus the
      quantity, gross and commission,
    * ``Traded Currency Leg`` — the other side, no symbol,
    * optionally ``Commission from Forex Trade`` — the fee, in the currency it
      is billed in, with its **own** ``transaction_id``.

    Which is why ``trade_id`` is the key and ``transaction_id`` is not.
    Grouping on the transaction id split every commission off as a bogus
    one-legged conversion — 26 apparent conversions where the book holds 17.

    The legs are identified from the **pair symbol**, which one leg always
    carries: ``USD.CZK`` names both currencies, so the two legs are the rows in
    those currencies and everything else on the trade is a charge. Neither the
    transaction id nor the description works for this. The commission
    sometimes shares the legs' transaction id (real trade 1295097860) and
    sometimes does not (1466896593); and the leg descriptions already differ
    from each other ("Traded" vs "Trading"), so the text is not something to
    build on either.
    """
    by_trade: Dict[str, List[RawStatementOfFundsRecord]] = {}
    for rec in movements(records):
        if (rec.activity_code or "").strip().upper() != "FOREX":
            continue
        key = (rec.trade_id or "").strip()
        if not key:
            logger.warning(
                f"FOREX row in {rec.currency_primary} on {rec.date} carries no "
                "trade id — it cannot be joined to the rest of its conversion."
            )
            continue
        by_trade.setdefault(key, []).append(rec)

    out: List[Conversion] = []
    for trade_id, rows in by_trade.items():
        conv = Conversion(trade_id=trade_id)
        pair = next((r for r in rows if "." in (r.symbol or "")), None)
        if pair is None:
            # No pair symbol on any row (a leg outside the period, or a layout
            # we have not seen). Fall back to the shared transaction id and let
            # is_complete tell the caller it cannot be priced.
            counts = Counter((r.transaction_id or "").strip() for r in rows)
            leg_tx = counts.most_common(1)[0][0] if counts else ""
            for r in rows:
                (conv.legs if (r.transaction_id or "").strip() == leg_tx
                 else conv.charges).append(r)
            out.append(conv)
            continue

        base, _, quote = (pair.symbol or "").partition(".")
        other_currency = ({base.strip().upper(), quote.strip().upper()}
                          - {pair.currency_primary.upper()})
        conv.legs.append(pair)

        def _is_charge(r: RawStatementOfFundsRecord) -> bool:
            """A fee booked to this trade rather than one of its currency sides.

            Two independent signals, because neither has to carry it alone: the
            fee equals the pair row's own ``trade_commission`` (structural, and
            true for 10 of 10 such rows on the real book), and IBKR labels it
            (uniform across all 17). A zero commission is not a signal — a leg
            can be zero too.
            """
            comm = pair.trade_commission
            if comm and r.amount == comm:
                return True
            return "COMMISSION" in (r.activity_description or "").upper()

        # Charges are taken out FIRST. Selecting the second leg by "currency is
        # the other side of the pair" alone meant that a fee billed in exactly
        # that currency competed for the slot, and iteration order decided the
        # winner: the fee could become the disposed leg, yielding a nonsense
        # rate while the real disposal was filed as a charge and the pair still
        # looked complete. Latent on this book (0 occurrences) — but a pair
        # whose base is the settlement currency, e.g. CZK.JPY, produces it.
        for r in rows:
            if r is pair:
                continue
            if _is_charge(r):
                conv.charges.append(r)
            elif (r.currency_primary.upper() in other_currency
                  and len(conv.legs) < 2):
                conv.legs.append(r)
            else:
                conv.charges.append(r)
        if not conv.is_complete:
            logger.warning(
                f"Forex trade {trade_id} ({pair.symbol}) has only "
                f"{len(conv.legs)} of its two currency legs in this period — "
                "no rate can be derived from it."
            )
        out.append(conv)
    return out
