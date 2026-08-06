# src/webapp/quotes.py
"""
Live quotes for portfolio valuation — Yahoo Finance chart API via ``requests``
(no yfinance/pandas dependency; the endpoint needs no API key).

Past closing prices moved to ``src/utils/historical_price_provider.py``: they
are a tax-calculation input the engine has to reach, and ``src/engine`` does not
import from ``src/webapp``. The IBKR→Yahoo symbol mapping and the overrides file
are shared from there, so both paths resolve a symbol identically.

Symbol mapping IBKR → Yahoo:
1. explicit user overrides in ``data/webapp/symbol_map.json``
   (``{"AMV0": "AMV.DE", ...}`` keyed by IBKR symbol), then
2. heuristics: trailing lowercase venue markers are stripped (IBKR "COPNz"
   → "COPN"), USD symbols pass through, other currencies get the usual
   Yahoo exchange suffix (EUR → .DE, GBP → .L, SEK → .ST, CHF → .SW,
   HKD → zero-padded .HK, ...).

Failures are cached for the TTL as well — one bad symbol must not hammer
the API on every page load. Callers always have the EOY mark price as a
fallback, so a missing quote degrades the display, never breaks it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import requests

from src.utils.historical_price_provider import (
    _UA,
    SYMBOL_MAP_PATH,
    HistoricalPrice,          # re-exported: existing importers expect it here
    HistoricalPriceProvider,  # re-exported
    map_symbol,
)
from src.webapp import settings

logger = logging.getLogger(__name__)

QUOTE_TTL_SECONDS = 900  # 15 min

# The live endpoint. The historical one lives with the provider that uses it.
_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"


@dataclass
class Quote:
    ibkr_symbol: str
    yahoo_symbol: str
    price: Decimal
    currency: str
    fetched_at: float


def yahoo_fetch(yahoo_symbol: str) -> Optional[Tuple[Decimal, str]]:
    """Fetch (price, currency) from the Yahoo chart endpoint; None on failure."""
    try:
        resp = requests.get(_YAHOO_URL.format(symbol=yahoo_symbol), headers=_UA, timeout=8)
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
        price = Decimal(str(meta["regularMarketPrice"]))
        currency = str(meta.get("currency") or "")
        # London quotes come in pence
        if currency == "GBp":
            price /= Decimal("100")
            currency = "GBP"
        return price, currency
    except Exception as exc:
        logger.info(f"Quote fetch failed for {yahoo_symbol}: {exc}")
        return None


class QuoteService:
    """TTL-cached quote lookup with pluggable fetcher (tests inject a fake)."""

    def __init__(
        self,
        fetcher: Optional[Callable[[str], Optional[Tuple[Decimal, str]]]] = None,
        overrides_path: Optional[Path] = None,
        ttl_seconds: int = QUOTE_TTL_SECONDS,
    ):
        self._fetcher = fetcher or yahoo_fetch
        self._overrides_path = overrides_path or SYMBOL_MAP_PATH
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[float, Optional[Quote]]] = {}

    def _overrides(self) -> Dict[str, str]:
        try:
            if self._overrides_path.is_file():
                return json.loads(self._overrides_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Unreadable symbol_map.json: {exc}")
        return {}

    def get_quote(self, ibkr_symbol: str, currency: str) -> Optional[Quote]:
        yahoo_symbol = map_symbol(ibkr_symbol, currency, self._overrides())
        if not yahoo_symbol:
            return None
        now = time.monotonic()
        cached = self._cache.get(yahoo_symbol)
        if cached and now - cached[0] < self._ttl:
            return cached[1]

        fetched = self._fetcher(yahoo_symbol)
        quote = None
        if fetched is not None:
            price, quote_currency = fetched
            quote = Quote(
                ibkr_symbol=ibkr_symbol,
                yahoo_symbol=yahoo_symbol,
                price=price,
                currency=quote_currency or currency,
                fetched_at=now,
            )
        self._cache[yahoo_symbol] = (now, quote)  # failures cached too
        return quote
