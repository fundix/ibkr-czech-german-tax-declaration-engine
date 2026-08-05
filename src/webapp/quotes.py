# src/webapp/quotes.py
"""
Quotes from the Yahoo Finance chart API via ``requests`` (no yfinance/pandas
dependency; the endpoint needs no API key). Two consumers, two lifetimes:

* ``QuoteService`` — live prices for portfolio valuation, TTL-cached in memory.
* ``HistoricalPriceProvider`` — closing price on a past date, for valuing a
  transaction whose consideration the broker does not price (a stock-for-stock
  merger). Past closes never change, so these persist to ``cache/``.

Both live here because they share the IBKR→Yahoo symbol mapping, the overrides
file and the GBp handling. ``HistoricalPriceProvider`` is a tax-calculation
input rather than a display concern, so if the engine ever needs it directly,
move ``map_symbol`` to ``src/utils`` first — ``src/utils`` and ``src/engine``
deliberately do not import from ``src/webapp``.

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

import datetime
import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from src.webapp import settings

logger = logging.getLogger(__name__)

QUOTE_TTL_SECONDS = 900  # 15 min

_SUFFIX_BY_CURRENCY = {
    "EUR": ".DE",
    "GBP": ".L",
    "SEK": ".ST",
    "CHF": ".SW",
    "CZK": ".PR",
    "NOK": ".OL",
    "DKK": ".CO",
    "JPY": ".T",
    "CAD": ".TO",
    "AUD": ".AX",
}

_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d"
_YAHOO_HISTORY_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?interval=1d&period1={start}&period2={end}"
)
_UA = {"User-Agent": "Mozilla/5.0 (local portfolio tracker)"}

# §19 zákona o oceňování majetku: when the relevant market has no closing price
# on the valuation date, the last one within the preceding 30 days is used.
HISTORY_MAX_LOOKBACK_DAYS = 30
HISTORY_CACHE_PATH = Path("cache/historical_closes.json")

# Distinguishes "this day was never fetched" from "this day did not trade" when
# walking the look-back — see HistoricalPriceProvider._lookup.
_GAP = object()

# Yahoo serialises closes as float32-derived doubles, so a 12.73 close arrives
# as 12.729999542236328. float32 carries ~7 significant digits, so rounding to
# six decimals recovers the quoted figure and drops the artefact; no real venue
# precision (nor a pence quote divided by 100) is lost at this scale.
_CLOSE_PRECISION = Decimal("0.000001")


@dataclass
class Quote:
    ibkr_symbol: str
    yahoo_symbol: str
    price: Decimal
    currency: str
    fetched_at: float


@dataclass
class HistoricalPrice:
    """A past closing price plus the provenance a tax file has to show."""
    ibkr_symbol: str
    yahoo_symbol: str
    requested_date: datetime.date
    price_date: datetime.date   # equals requested_date unless the look-back ran
    price: Decimal
    currency: str
    source: str

    @property
    def used_lookback(self) -> bool:
        """Did the valuation date have no close of its own?"""
        return self.price_date != self.requested_date


def map_symbol(ibkr_symbol: str, currency: str, overrides: Optional[Dict[str, str]] = None) -> str:
    """Map an IBKR symbol to its likely Yahoo Finance symbol."""
    if overrides and ibkr_symbol in overrides:
        return overrides[ibkr_symbol]
    base = re.sub(r"[a-z]+$", "", ibkr_symbol or "").strip()
    cur = (currency or "USD").upper()
    if cur == "USD":
        return base
    if cur == "HKD":
        return (f"{int(base):04d}.HK" if base.isdigit() else f"{base}.HK")
    return base + _SUFFIX_BY_CURRENCY.get(cur, "")


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


def yahoo_fetch_closes(
    yahoo_symbol: str,
    start: datetime.date,
    end: datetime.date,
) -> Optional[Tuple[Dict[datetime.date, Decimal], str]]:
    """Daily closes for ``[start, end]`` inclusive; ``None`` when the call fails.

    Returns ``({trading date: close}, currency)``. Only days the market
    actually traded appear — weekends and holidays are simply absent, which is
    what drives the caller's look-back.

    Yahoo timestamps each bar at the exchange's market open, so the bar is
    shifted by the exchange's ``gmtoffset`` before its calendar date is taken;
    reading them as UTC would misdate venues far from Greenwich.
    """
    # period2 is exclusive of the following midnight, so pad a day to keep
    # `end` itself in range whatever the venue's offset.
    url = _YAHOO_HISTORY_URL.format(
        symbol=yahoo_symbol,
        start=int(datetime.datetime.combine(
            start, datetime.time.min, tzinfo=datetime.timezone.utc).timestamp()),
        end=int(datetime.datetime.combine(
            end + datetime.timedelta(days=1), datetime.time.min,
            tzinfo=datetime.timezone.utc).timestamp()),
    )
    try:
        resp = requests.get(url, headers=_UA, timeout=10)
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        currency = str(meta.get("currency") or "")
        offset = int(meta.get("gmtoffset") or 0)
        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []

        # London quotes come in pence — normalise as the live path does.
        divisor = Decimal("100") if currency == "GBp" else Decimal("1")
        if currency == "GBp":
            currency = "GBP"

        out: Dict[datetime.date, Decimal] = {}
        for ts, close in zip(timestamps, closes):
            if close is None:  # halted/empty bar
                continue
            day = (
                datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
                + datetime.timedelta(seconds=offset)
            ).date()
            if start <= day <= end:
                out[day] = (Decimal(str(close)) / divisor).quantize(
                    _CLOSE_PRECISION, rounding=ROUND_HALF_UP
                ).normalize()
        return out, currency
    except Exception as exc:
        logger.info(
            f"Historical close fetch failed for {yahoo_symbol} "
            f"({start}..{end}): {exc}"
        )
        return None


class HistoricalPriceProvider:
    """Closing price on a past date, with the §19 look-back and a disk cache.

    Past closes are immutable, so the cache is permanent (no TTL) and keyed by
    Yahoo symbol and trading date. Non-trading days inside a fetched window are
    stored as ``null`` so a weekend is never re-fetched; days outside any
    fetched window are simply absent.

    ``get_close`` reports which date the price actually came from, so a
    valuation that fell back can be documented rather than silently claiming
    the requested date.
    """

    def __init__(
        self,
        fetcher: Optional[Callable[
            [str, datetime.date, datetime.date],
            Optional[Tuple[Dict[datetime.date, Decimal], str]],
        ]] = None,
        overrides_path: Optional[Path] = None,
        cache_file_path: Optional[Path] = None,
        max_lookback_days: int = HISTORY_MAX_LOOKBACK_DAYS,
    ):
        self._fetcher = fetcher or yahoo_fetch_closes
        self._overrides_path = overrides_path or (settings.DATA_DIR / "symbol_map.json")
        self._cache_path = cache_file_path or HISTORY_CACHE_PATH
        self._max_lookback = max_lookback_days
        # {yahoo symbol: {"currency": str, "closes": {"YYYY-MM-DD": str | None}}}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            if self._cache_path.is_file():
                self._cache = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                f"Unreadable historical close cache {self._cache_path}: {exc}. "
                "Starting empty."
            )
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(f"Cannot save historical close cache {self._cache_path}: {exc}")

    def _overrides(self) -> Dict[str, str]:
        try:
            if self._overrides_path.is_file():
                return json.loads(self._overrides_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Unreadable symbol_map.json: {exc}")
        return {}

    def get_close(
        self,
        ibkr_symbol: str,
        currency: str,
        on_date: datetime.date,
    ) -> Optional[HistoricalPrice]:
        """Closing price for *on_date*, or the last one within the look-back.

        ``None`` when nothing is available in the whole window — the caller
        must then refuse to value the transaction rather than guess.
        """
        yahoo_symbol = map_symbol(ibkr_symbol, currency, self._overrides())
        if not yahoo_symbol:
            return None

        window_start = on_date - datetime.timedelta(days=self._max_lookback)
        hit = self._lookup(yahoo_symbol, on_date, window_start)
        if hit is _GAP:
            # Some day between the target and the newest cached close was never
            # fetched, so no conclusion is possible yet: an earlier request may
            # have covered a window ending before this date.
            self._fill(yahoo_symbol, window_start, on_date)
            hit = self._lookup(yahoo_symbol, on_date, window_start)
        if hit is None or hit is _GAP:
            logger.warning(
                f"No closing price for {ibkr_symbol} ({yahoo_symbol}) on {on_date} "
                f"nor in the {self._max_lookback} days before it."
            )
            return None

        price_date, price = hit
        return HistoricalPrice(
            ibkr_symbol=ibkr_symbol,
            yahoo_symbol=yahoo_symbol,
            requested_date=on_date,
            price_date=price_date,
            price=price,
            currency=(self._cache.get(yahoo_symbol, {}).get("currency") or currency),
            source="yahoo:chart",
        )

    def _lookup(
        self,
        yahoo_symbol: str,
        on_date: datetime.date,
        window_start: datetime.date,
    ) -> Any:
        """Walk back from *on_date* for the newest cached close in the window.

        Three outcomes, because "no price cached for this day" is ambiguous:
        a priced day returns ``(date, price)``; a day explicitly known not to
        have traded is skipped; a day never fetched returns ``_GAP``, since
        continuing past it could return a close from before an unfetched
        trading day. ``None`` means the whole window is covered and empty.
        """
        closes = (self._cache.get(yahoo_symbol) or {}).get("closes") or {}
        day = on_date
        while day >= window_start:
            key = day.isoformat()
            if key not in closes:
                return _GAP
            raw = closes[key]
            if raw is not None:
                return day, Decimal(raw)
            day -= datetime.timedelta(days=1)
        return None

    def _fill(
        self,
        yahoo_symbol: str,
        start: datetime.date,
        end: datetime.date,
    ) -> None:
        """Fetch the window once and record every day in it, traded or not."""
        fetched = self._fetcher(yahoo_symbol, start, end)
        if fetched is None:
            return
        prices, currency = fetched
        entry = self._cache.setdefault(yahoo_symbol, {"currency": currency, "closes": {}})
        if currency:
            entry["currency"] = currency
        closes = entry["closes"]
        day = start
        while day <= end:
            key = day.isoformat()
            price = prices.get(day)
            # A traded day wins; a day the venue did not trade is pinned as
            # null so the look-back skips it without another round trip.
            if price is not None:
                closes[key] = str(price)
            else:
                closes.setdefault(key, None)
            day += datetime.timedelta(days=1)
        self._save_cache()


class QuoteService:
    """TTL-cached quote lookup with pluggable fetcher (tests inject a fake)."""

    def __init__(
        self,
        fetcher: Optional[Callable[[str], Optional[Tuple[Decimal, str]]]] = None,
        overrides_path: Optional[Path] = None,
        ttl_seconds: int = QUOTE_TTL_SECONDS,
    ):
        self._fetcher = fetcher or yahoo_fetch
        self._overrides_path = overrides_path or (settings.DATA_DIR / "symbol_map.json")
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
