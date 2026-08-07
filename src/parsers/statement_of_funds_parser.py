# src/parsers/statement_of_funds_parser.py
"""Parser for IBKR's Statement of Funds — the per-currency cash ledger.

The only statement that carries cash balances, and therefore the only source
of the rate at which a held currency was acquired. Kept standalone, like
``positions_parser``, so a caller can read it without running the engine.
"""
import csv
import logging
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


def conversion_legs(
    records: List[RawStatementOfFundsRecord],
) -> Dict[str, List[RawStatementOfFundsRecord]]:
    """FOREX rows grouped into their two legs, keyed by transaction id.

    Both legs of a conversion share ``trade_id`` and ``transaction_id`` — the
    descriptions differ ("Traded" vs "Trading" Currency Leg), so the ids are
    the only reliable join. A group with one leg means the other fell outside
    the requested period and the caller must not treat it as a full exchange.
    """
    groups: Dict[str, List[RawStatementOfFundsRecord]] = {}
    for rec in movements(records):
        if (rec.activity_code or "").strip().upper() != "FOREX":
            continue
        key = (rec.transaction_id or rec.trade_id or "").strip()
        if not key:
            logger.warning(
                f"FOREX row in {rec.currency_primary} on {rec.date} has neither "
                "transaction nor trade id — cannot be paired to its other leg."
            )
            continue
        groups.setdefault(key, []).append(rec)
    return groups
