# src/utils/cnb_exchange_rate_provider.py
"""
Czech National Bank (ČNB) exchange rate provider.

Fetches daily FX rates from the ČNB public text API and caches them
in a JSON file, following the same pattern as ``ECBExchangeRateProvider``.

ČNB publishes rates as **units of foreign currency per 1 CZK-equivalent
amount**.  For example, the USD row might read ``1|USD|22.345`` meaning
1 USD = 22.345 CZK.  Some currencies use a different *amount* column
(e.g. ``100|JPY|15.123`` → 100 JPY = 15.123 CZK).

This provider normalises all rates to the convention used by
``CurrencyConverter``: **foreign-currency units per 1 CZK**.  That way
``CurrencyConverter.convert_to_eur`` logic (amount / rate) works the same
regardless of whether the target (home) currency is EUR or CZK.

Usage::

    provider = CNBExchangeRateProvider(
        cache_file_path="cache/cnb_exchange_rates.json",
    )
    # Returns e.g. Decimal("0.04477") meaning 1 CZK = 0.04477 USD
    rate = provider.get_rate(date(2024, 6, 15), "USD")
    # CZK amount = foreign_amount / rate
    #            = 100 USD / 0.04477 = 2233.6 CZK

The provider is **not** hard-wired to the CZ plugin — any code that needs
CZK-based rates can use it.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import requests

from src.utils.exchange_rate_provider import ExchangeRateProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ČNB text-format URL for a single day.
# Example: https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt?date=25.03.2025
DEFAULT_CNB_API_URL_TEMPLATE = (
    "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
    "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/"
    "denni_kurz.txt?date={date_str}"
)
DEFAULT_CNB_MAX_FALLBACK_DAYS = 7
DEFAULT_CNB_REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_CNB_CURRENCY_CODE_MAPPING: Dict[str, str] = {
    "CNH": "CNY",
}

# A cache entry records, under this key, the date printed in the header of
# the sheet it holds (ISO, or None when the header could not be read). An
# entry WITHOUT the key was written before sheets were told apart and may
# hold the previous day's rates — see ``_recheck_unstamped_entry``.
SHEET_KEY = "_sheet"

_HEADER_DATE_RE = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\b")


class CnbSheet(dict):
    """The rates of one ČNB sheet: ``{currency_code: foreign_units_per_1_CZK}``.

    ``sheet_date`` is the date ČNB printed in the header — the day the rates
    were fixed for. It is NOT always the day that was asked for: a weekend, a
    holiday, and today before the afternoon fixing are all answered with the
    last sheet published before them. ``None`` when the header is unreadable.
    """

    sheet_date: Optional[datetime.date] = None


def _parse_header_date(header_line: str) -> Optional[datetime.date]:
    """The date of a sheet header such as ``25.03.2025 #59``."""
    match = _HEADER_DATE_RE.match(header_line)
    if not match:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class CNBExchangeRateProvider(ExchangeRateProvider):
    """
    Fetches and caches ČNB daily exchange rates.

    ``get_rate()`` returns the rate as *foreign-currency units per 1 CZK*
    (mirroring ECB's *foreign-currency units per 1 EUR*).
    """

    def __init__(
        self,
        cache_file_path: str = "cache/cnb_exchange_rates.json",
        api_url_template_override: Optional[str] = None,
        max_fallback_days_override: Optional[int] = None,
        currency_code_mapping_override: Optional[Dict[str, str]] = None,
        request_timeout_seconds_override: Optional[int] = None,
    ):
        super().__init__()
        self.cache_file_path = cache_file_path
        self.api_url_template = api_url_template_override or DEFAULT_CNB_API_URL_TEMPLATE
        self.max_fallback_days = (
            max_fallback_days_override
            if max_fallback_days_override is not None
            else DEFAULT_CNB_MAX_FALLBACK_DAYS
        )
        self.currency_code_mapping = (
            currency_code_mapping_override
            if currency_code_mapping_override is not None
            else DEFAULT_CNB_CURRENCY_CODE_MAPPING.copy()
        )
        self.request_timeout_seconds = (
            request_timeout_seconds_override or DEFAULT_CNB_REQUEST_TIMEOUT_SECONDS
        )

        # cache layout: { "2024-06-15": { "USD": "0.04477", "EUR": "0.04132",
        #                                 "_sheet": "2024-06-14", ... }, ... }
        # A ``None`` rate means "fetched but not available". ``_sheet`` is
        # SHEET_KEY: the date of the sheet the entry holds.
        self.rates_cache: Dict[str, Dict[str, Optional[str]]] = {}
        # Rechecking entries the old code wrote needs ČNB. Once it does not
        # answer, the provider stops asking for the rest of its life (a run
        # touches hundreds of dates, each a 15 s timeout when offline); a
        # single entry ČNB cannot settle is likewise asked about only once.
        self._recheck_suspended = False
        self._recheck_skipped: Set[str] = set()
        # Sheets ČNB returned for a day whose own sheet is not out yet, by
        # that day. Never written to disk; held so that a page converting
        # forty positions at today's rate asks ČNB once, not forty times.
        self._pending_sheets: Dict[str, Dict[str, Decimal]] = {}
        # Dates whose rate was served from an entry that could NOT be
        # re-checked (ČNB unreachable or its answer undated). The caller
        # decides what a result resting on them is worth.
        self.unverified_dates_used: Set[str] = set()
        self._load_cache()

    # -- cache I/O ----------------------------------------------------------

    def _load_cache(self) -> None:
        if os.path.exists(self.cache_file_path):
            try:
                with open(self.cache_file_path, "r", encoding="utf-8") as fh:
                    self.rates_cache = json.load(fh)
                n_rates = sum(
                    1 for dr in self.rates_cache.values()
                    for key, v in dr.items()
                    if key != SHEET_KEY and v is not None
                )
                logger.info(
                    f"Loaded {n_rates} CNB exchange rates from {self.cache_file_path}"
                )
            except (json.JSONDecodeError, Exception) as exc:
                logger.error(
                    f"Error loading CNB cache from {self.cache_file_path}: {exc}. "
                    "Starting with empty cache."
                )
                self.rates_cache = {}
        else:
            logger.info(
                f"CNB cache file {self.cache_file_path} not found. "
                "Will create when rates are fetched."
            )
            self.rates_cache = {}

        cache_dir = os.path.dirname(self.cache_file_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _save_cache(self) -> None:
        try:
            with open(self.cache_file_path, "w", encoding="utf-8") as fh:
                json.dump(self.rates_cache, fh, indent=2, ensure_ascii=False)
            logger.debug(f"Saved CNB exchange rate cache to {self.cache_file_path}")
        except Exception as exc:
            logger.error(f"Error saving CNB cache to {self.cache_file_path}: {exc}")

    # -- currency code mapping ----------------------------------------------

    def _get_effective_currency_code(self, currency_code: str) -> str:
        return self.currency_code_mapping.get(currency_code.upper(), currency_code.upper())

    # -- ČNB API ------------------------------------------------------------

    def _fetch_rates_for_date(self, query_date: datetime.date) -> Optional[Dict[str, Decimal]]:
        """
        Fetch **all** currency rates published by ČNB for *query_date*.

        Returns a dict ``{currency_code: rate_as_foreign_per_czk}`` or
        ``None`` on failure.

        The ČNB text format looks like::

            25.03.2025 #59
            země|měna|množství|kód|kurz
            Austrálie|dolar|1|AUD|14,536
            ...
            USA|dolar|1|USD|22,345

        ``kurz`` is *CZK per <množství> units of foreign currency*.
        We normalise to *foreign units per 1 CZK*:
        ``rate = množství / kurz``.
        """
        date_str = query_date.strftime("%d.%m.%Y")  # ČNB uses DD.MM.YYYY
        url = self.api_url_template.format(date_str=date_str)

        logger.debug(f"Fetching CNB rates for {date_str} from {url}")
        try:
            resp = requests.get(url, timeout=self.request_timeout_seconds)
            resp.raise_for_status()
            text = resp.text
        except requests.exceptions.RequestException as exc:
            logger.error(f"CNB request failed for {date_str}: {exc}")
            return None

        return self._parse_cnb_text(text, query_date)

    @staticmethod
    def _parse_cnb_text(
        text: str, query_date: datetime.date
    ) -> Optional[CnbSheet]:
        """
        Parse the ČNB plain-text rate table into a ``CnbSheet``:
        ``{currency_code: foreign_units_per_1_CZK}`` plus the date of the
        sheet, which is the header's date and not necessarily *query_date*.
        """
        lines = text.strip().splitlines()
        if len(lines) < 3:
            logger.warning(
                f"CNB response for {query_date} has fewer than 3 lines — "
                "possibly a holiday or invalid date."
            )
            return None

        # Line 0: date header, Line 1: column headers, Lines 2+: data
        rates = CnbSheet()
        rates.sheet_date = _parse_header_date(lines[0])
        if rates.sheet_date is None:
            logger.warning(
                f"CNB response for {query_date} has an unreadable header "
                f"'{lines[0][:40]}' — the date of the sheet is unknown."
            )
        for line in lines[2:]:
            parts = line.split("|")
            if len(parts) < 5:
                continue
            try:
                amount = Decimal(parts[2].strip())
                code = parts[3].strip().upper()
                # ČNB uses comma as decimal separator
                rate_czk = Decimal(parts[4].strip().replace(",", "."))
                if rate_czk <= Decimal(0) or amount <= Decimal(0):
                    continue
                # Normalise: foreign_units_per_1_CZK = amount / rate_czk
                rates[code] = amount / rate_czk
            except (InvalidOperation, ValueError, ArithmeticError) as exc:
                logger.debug(f"Skipping CNB line '{line}': {exc}")
                continue

        if not rates:
            logger.warning(f"No valid rates parsed from CNB response for {query_date}")
            return None

        logger.info(f"Parsed {len(rates)} CNB rates for {query_date}")
        return rates

    # -- which sheet belongs to which day -----------------------------------

    def _publishing_today(
        self, now: Optional[datetime.datetime] = None
    ) -> datetime.date:
        """Today's date in Prague, where the sheet of the day is published.

        Whether a day's sheet can still appear depends on the calendar ČNB
        lives by, not on the clock of the machine asking: seen from a
        timezone east of Prague, "yesterday" may be the day ČNB has not
        fixed yet. ``now`` is an aware instant; it defaults to the present.
        """
        now = now or datetime.datetime.now(datetime.timezone.utc)
        try:
            return now.astimezone(ZoneInfo("Europe/Prague")).date()
        except Exception:  # noqa: BLE001 — no timezone database on this machine
            return now.astimezone().date()

    def _awaits_its_own_sheet(
        self, query_date: datetime.date, sheet_date: Optional[datetime.date]
    ) -> bool:
        """True when ČNB's answer for *query_date* is not final yet.

        The sheet of a day comes out around 14:30 Prague time. Asked earlier,
        ČNB answers with the previous sheet, and that answer must never be
        cached under *query_date*: it would stand in for the real sheet in
        every later run. A past date is final whatever the header says — it
        was a weekend or a holiday. An unreadable header on today's date is
        treated as not final, because nothing proves that it is.
        """
        return query_date >= self._publishing_today() and sheet_date != query_date

    @staticmethod
    def _entry_of(sheet: Dict[str, Decimal]) -> Dict[str, Optional[str]]:
        """A sheet as a cache entry: its rates as text, stamped with its date."""
        entry: Dict[str, Optional[str]] = {
            code: str(rate) for code, rate in sheet.items()
        }
        sheet_date = getattr(sheet, "sheet_date", None)
        entry[SHEET_KEY] = sheet_date.isoformat() if sheet_date else None
        return entry

    def _file_sheet(self, date_str: str, sheet: Dict[str, Decimal]) -> bool:
        """Merge *sheet* into the entry of *date_str*; True if anything changed."""
        entry = self.rates_cache.setdefault(date_str, {})
        changed = False
        for key, value in self._entry_of(sheet).items():
            if key not in entry or entry[key] != value:
                entry[key] = value
                changed = True
        return changed

    def _file_early_sheet(self, sheet: Dict[str, Decimal],
                          query_date: datetime.date) -> Optional[datetime.date]:
        """Keep a sheet that came back instead of *query_date*'s own.

        It goes under the date it carries — where it is the right sheet by
        ČNB's own word — and that date is returned. ``None`` when the header
        gives no usable date, in which case nothing is kept.
        """
        sheet_date = getattr(sheet, "sheet_date", None)
        if sheet_date is None or sheet_date >= query_date:
            return None
        if self._file_sheet(sheet_date.strftime("%Y-%m-%d"), sheet):
            self._save_cache()
        return sheet_date

    def _recheck_unstamped_entry(self, entry_date: datetime.date) -> None:
        """Compare an entry the old code wrote with ČNB, the first time it is used.

        Before sheets were stamped, a request for "today" made before the
        fixing stored the previous day's sheet under today's date, and the
        entry looks no different from a good one. ČNB is asked again: for a
        past date its answer is final, so the entry is replaced by it and
        stamped. Without ČNB the entry is used as it is, as before.
        """
        date_str = entry_date.strftime("%Y-%m-%d")
        entry = self.rates_cache.get(date_str)
        if not entry or SHEET_KEY in entry:
            return
        if not any(value is not None for value in entry.values()):
            return  # only failure markers — nothing to compare
        if self._recheck_suspended or date_str in self._recheck_skipped:
            return

        fetched = self._fetch_rates_for_date(entry_date)
        if not fetched:
            self._recheck_suspended = True
            logger.warning(
                f"CNB could not be asked about the cached rates of {date_str}; "
                "unverified cache entries are used as they are for now."
            )
            return

        sheet_date = getattr(fetched, "sheet_date", None)
        if self._awaits_its_own_sheet(entry_date, sheet_date):
            # Stored as this day's sheet while ČNB has not published one.
            del self.rates_cache[date_str]
            self._pending_sheets[date_str] = fetched
            self._file_early_sheet(fetched, entry_date)
            self._save_cache()
            logger.warning(
                f"CNB cache entry {date_str} was stored before that day's "
                "sheet was published — dropped."
            )
            return
        if sheet_date is None or sheet_date > entry_date:
            # Nothing reliable to compare with; keep the entry, ask no more.
            self._recheck_skipped.add(date_str)
            return

        fresh = self._entry_of(fetched)
        differing = sorted(
            code for code, value in fresh.items()
            if code != SHEET_KEY and entry.get(code) != value
        )
        self.rates_cache[date_str] = fresh
        self._save_cache()
        if differing:
            logger.warning(
                f"CNB cache entry {date_str} held another day's rates "
                f"({len(differing)} currencies differed) — replaced with the "
                f"sheet of {sheet_date.isoformat()}."
            )

    # -- ExchangeRateProvider interface -------------------------------------

    def get_rate(
        self, date_of_conversion: datetime.date, currency_code: str
    ) -> Optional[Decimal]:
        """
        Return *foreign-currency units per 1 CZK* for *currency_code*
        on *date_of_conversion*, with up to ``max_fallback_days`` of
        lookback for weekends/holidays.

        Returns ``Decimal("1.0")`` for ``CZK``.
        """
        info = self.get_rate_info(date_of_conversion, currency_code)
        return info[0] if info else None

    def get_rate_info(
        self, date_of_conversion: datetime.date, currency_code: str
    ) -> Optional[Tuple[Decimal, datetime.date]]:
        """
        Like :meth:`get_rate`, but returns ``(rate, actual_rate_date)`` so
        callers can record the REAL date of the rate in audit trails when
        the weekend/holiday fallback was applied.
        """
        original_upper = currency_code.upper()
        if original_upper == "CZK":
            return (Decimal("1.0"), date_of_conversion)

        # ČNB answers a FUTURE-dated request with today's sheet — a silent
        # wrong rate. A future event date is bad input data; refuse loudly.
        if date_of_conversion > datetime.date.today():
            logger.error(
                f"CNB rate requested for FUTURE date {date_of_conversion} "
                f"({original_upper}) — refusing (ČNB would silently return "
                "the current sheet). Check the input data."
            )
            return None

        effective_code = self._get_effective_currency_code(original_upper)
        target_date_str = date_of_conversion.strftime("%Y-%m-%d")

        for i in range(self.max_fallback_days + 1):
            search_date = date_of_conversion - datetime.timedelta(days=i)
            search_date_str = search_date.strftime("%Y-%m-%d")
            cache_modified = False

            # An entry written before sheets were stamped may hold the
            # previous day's rates — settle that before trusting it.
            self._recheck_unstamped_entry(search_date)

            # -- check cache --
            if search_date_str in self.rates_cache:
                cached = self.rates_cache[search_date_str].get(effective_code)
                if cached is not None:
                    try:
                        rate = Decimal(cached)
                        logger.debug(
                            f"CNB cache hit: {effective_code} on {search_date_str} "
                            f"(fallback {i}d for {target_date_str}) = {rate}"
                        )
                        if SHEET_KEY not in self.rates_cache[search_date_str]:
                            self.unverified_dates_used.add(search_date_str)
                        return (rate, search_date)
                    except InvalidOperation:
                        logger.error(
                            f"Invalid cached rate '{cached}' for "
                            f"{effective_code} on {search_date_str}. Removing."
                        )
                        del self.rates_cache[search_date_str][effective_code]
                        cache_modified = True
                elif effective_code in self.rates_cache[search_date_str]:
                    # Explicit None — previously failed
                    if i > 0:
                        if cache_modified:
                            self._save_cache()
                        continue
                    # For day 0 fall through to refetch

            # -- fetch from API --
            # A remembered early sheet is good only while its day is still
            # open. Once the day has passed ČNB has the real sheet, and the
            # remembered one would be filed under that date as if it were it.
            fetched = (self._pending_sheets.get(search_date_str)
                       if search_date >= self._publishing_today() else None)
            if fetched is None:
                fetched = self._fetch_rates_for_date(search_date)

            if fetched and self._awaits_its_own_sheet(
                    search_date, getattr(fetched, "sheet_date", None)):
                # This day's sheet is not out yet and ČNB answered with the
                # last one before it. It is kept under its own date and the
                # caller is told that date — the fallback, made explicit.
                self._pending_sheets[search_date_str] = fetched
                if cache_modified:
                    self._save_cache()
                early_date = self._file_early_sheet(fetched, search_date)
                if (early_date is not None and effective_code in fetched
                        and (date_of_conversion - early_date).days
                        <= self.max_fallback_days):
                    logger.info(
                        f"CNB sheet of {search_date_str} is not published yet; "
                        f"using {effective_code} of {early_date.isoformat()} "
                        f"for {target_date_str}: {fetched[effective_code]}"
                    )
                    return (fetched[effective_code], early_date)
                continue

            if search_date_str not in self.rates_cache:
                self.rates_cache[search_date_str] = {}

            if fetched:
                # Store ALL fetched rates for this date (bulk efficiency)
                if self._file_sheet(search_date_str, fetched):
                    cache_modified = True

                if effective_code in fetched:
                    if cache_modified:
                        self._save_cache()
                    logger.info(
                        f"CNB rate for {effective_code} on {search_date_str} "
                        f"(fallback {i}d for {target_date_str}): {fetched[effective_code]}"
                    )
                    return (fetched[effective_code], search_date)
                else:
                    # ČNB published rates but not for this currency
                    if self.rates_cache[search_date_str].get(effective_code) is not None:
                        self.rates_cache[search_date_str][effective_code] = None
                        cache_modified = True
            else:
                # Fetch failed entirely — mark all as None? No, just mark the
                # specific code so we don't retry in this session.
                if self.rates_cache[search_date_str].get(effective_code) is not None:
                    self.rates_cache[search_date_str][effective_code] = None
                    cache_modified = True

            if cache_modified:
                self._save_cache()

        logger.warning(
            f"No CNB rate for {effective_code} (orig: {original_upper}) for "
            f"{target_date_str} after {self.max_fallback_days} days lookback."
        )
        return None

    def prefetch_rates(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        currencies: Set[str],
    ) -> None:
        """Pre-warm the cache for a date range (fetches one day at a time)."""
        current = start_date
        cache_modified = False
        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            if date_str not in self.rates_cache:
                fetched = self._fetch_rates_for_date(current)
                if fetched and self._awaits_its_own_sheet(
                        current, getattr(fetched, "sheet_date", None)):
                    # Not this day's sheet yet — see get_rate_info.
                    self._file_early_sheet(fetched, current)
                elif fetched:
                    # Store the fetched rates — previously the result was
                    # discarded and get_rate() re-fetched every date.
                    self.rates_cache[date_str] = self._entry_of(fetched)
                    cache_modified = True
            current += datetime.timedelta(days=1)
        if cache_modified:
            self._save_cache()

    def get_currency_code_mapping(self) -> Dict[str, str]:
        return self.currency_code_mapping.copy()

    def get_max_fallback_days(self) -> int:
        return self.max_fallback_days
