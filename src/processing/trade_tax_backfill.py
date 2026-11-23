# src/processing/trade_tax_backfill.py
"""Fill in a trade's transaction tax from the Statement of Funds.

The Trades export carries a market's transaction tax (Hong Kong stamp duty,
Korean securities transaction tax) in its ``Taxes`` column -- but only when the
Flex query asks for it, and older queries do not. The Statement of Funds always
reports the same figure as ``TradeTax`` on the row of the very same
``TransactionID``. Without it a purchase is carried too cheap and a sale too
rich, overstating the §10 gain by the tax on both legs.

Must run BEFORE enrichment: the tax is folded into the EUR cost or proceeds
there.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, Iterable, List, Tuple

from src.domain.events import FinancialEvent, TradeEvent
from src.parsers.raw_models import RawStatementOfFundsRecord

logger = logging.getLogger(__name__)


def apply_statement_of_funds_trade_taxes(
    events: List[FinancialEvent],
    sof_records: Iterable[RawStatementOfFundsRecord],
) -> int:
    """Set ``transaction_tax_foreign`` on trades whose export did not carry it.

    Returns how many trades received a tax. A trade that already has one from
    the Trades export keeps it. Refuses (ValueError) what has no safe reading:
    two tax rows under one transaction, or a tax in a currency other than the
    trade's own.
    """
    taxes: Dict[str, Tuple[Decimal, str]] = {}
    for rec in sof_records:
        tax = rec.trade_tax
        if tax is None or tax == Decimal(0):
            continue
        tid = (rec.transaction_id or "").strip()
        if not tid:
            logger.warning(
                "Statement of Funds row with TradeTax %s %s has no TransactionID; "
                "it cannot be matched to a trade.", tax, rec.currency_primary)
            continue
        if tid in taxes:
            raise ValueError(
                f"Statement of Funds has more than one TradeTax row for transaction "
                f"{tid}; which one belongs to the trade cannot be decided.")
        taxes[tid] = (tax, (rec.currency_primary or "").upper())

    applied = 0
    for ev in events:
        if not isinstance(ev, TradeEvent):
            continue
        tid = (ev.ibkr_transaction_id or "").strip()
        if tid not in taxes:
            continue
        tax, currency = taxes[tid]
        if ev.transaction_tax_foreign:
            if ev.transaction_tax_foreign != -tax:
                logger.warning(
                    "Trade %s: Taxes column says %s, Statement of Funds TradeTax %s. "
                    "Keeping the Trades export's figure.",
                    tid, ev.transaction_tax_foreign, -tax)
            continue
        if (ev.local_currency or "").upper() != currency:
            raise ValueError(
                f"Trade {tid}: Statement of Funds TradeTax is in currency {currency}, "
                f"the trade in {ev.local_currency}. A transaction tax is charged in "
                f"the trade's own currency; this row has no safe reading.")
        # Exported negative when charged; stored as a positive cost.
        ev.transaction_tax_foreign = -tax
        applied += 1

    if applied:
        logger.info("Transaction tax taken from the Statement of Funds for %d trade(s).",
                    applied)
    return applied
