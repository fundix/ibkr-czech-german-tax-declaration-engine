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
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Set, Tuple

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

        # cache layout: { "2024-06-15": { "USD": "0.04477", "EUR": "0.04132", ... }, ... }
        # A ``None`` value means "fetched but not available".
        self.rates_cache: Dict[str, Dict[str, Optional[str]]] = {}
        self._load_cache()

    # -- cache I/O ----------------------------------------------------------

    def _load_cache(self) -> None:
        if os.path.exists(self.cache_file_path):
            try:
                with open(self.cache_file_path, "r", encoding="utf-8") as fh:
                    self.rates_cache = json.load(fh)
                n_rates = sum(
                    1 for dr in self.rates_cache.values()
                    for v in dr.values() if v is not None
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
    ) -> Optional[Dict[str, Decimal]]:
        """
        Parse the ČNB plain-text rate table into a dict of
        ``{currency_code: foreign_units_per_1_CZK}``.
        """
        lines = text.strip().splitlines()
        if len(lines) < 3:
            logger.warning(
                f"CNB response for {query_date} has fewer than 3 lines — "
                "possibly a holiday or invalid date."
            )
            return None

        # Line 0: date header, Line 1: column headers, Lines 2+: data
        rates: Dict[str, Decimal] = {}
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
        original_upper = currency_code.upper()
        if original_upper == "CZK":
            return Decimal("1.0")

        effective_code = self._get_effective_currency_code(original_upper)
        target_date_str = date_of_conversion.strftime("%Y-%m-%d")

        for i in range(self.max_fallback_days + 1):
            search_date = date_of_conversion - datetime.timedelta(days=i)
            search_date_str = search_date.strftime("%Y-%m-%d")
            cache_modified = False

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
                        return rate
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
            fetched = self._fetch_rates_for_date(search_date)
            if search_date_str not in self.rates_cache:
                self.rates_cache[search_date_str] = {}

            if fetched:
                # Store ALL fetched rates for this date (bulk efficiency)
                for code, rate_val in fetched.items():
                    prev = self.rates_cache[search_date_str].get(code)
                    if prev != str(rate_val):
                        self.rates_cache[search_date_str][code] = str(rate_val)
                        cache_modified = True

                if effective_code in fetched:
                    if cache_modified:
                        self._save_cache()
                    logger.info(
                        f"CNB rate for {effective_code} on {search_date_str} "
                        f"(fallback {i}d for {target_date_str}): {fetched[effective_code]}"
                    )
                    return fetched[effective_code]
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
                if fetched:
                    # Store the fetched rates — previously the result was
                    # discarded and get_rate() re-fetched every date.
                    self.rates_cache[date_str] = {
                        code: str(rate) for code, rate in fetched.items()
                    }
                    cache_modified = True
            current += datetime.timedelta(days=1)
        if cache_modified:
            self._save_cache()

    def get_currency_code_mapping(self) -> Dict[str, str]:
        return self.currency_code_mapping.copy()

    def get_max_fallback_days(self) -> int:
        return self.max_fallback_days
