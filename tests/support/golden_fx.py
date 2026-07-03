# tests/support/golden_fx.py
"""
Pinned REAL FX rates for the golden end-to-end CZ tests.

All values were fetched straight from the providers' public APIs
(ECB data API / ČNB denni_kurz.txt) on 2026-07-02 — they are actual
published fixings, not invented numbers, so the golden expectations stay
comparable with a live-API run on the same input data.

Both providers implement the same weekend/holiday semantics as the real
ones: walk back up to MAX_FALLBACK_DAYS to the last published fixing, and
(ČNB) report the ACTUAL rate date via ``get_rate_info`` so the CZ FX layer
can set ``fx_date_used`` and ``conversion_note`` (audit finding L9).
"""
import datetime
from decimal import Decimal
from typing import Dict, Optional, Set, Tuple

from src.utils.exchange_rate_provider import ExchangeRateProvider

MAX_FALLBACK_DAYS = 7


class GoldenEcbProvider(ExchangeRateProvider):
    """Real ECB reference rates, pinned. Convention: USD units per 1 EUR."""

    USD_PER_EUR: Dict[datetime.date, Decimal] = {
        datetime.date(2020, 6, 15): Decimal("1.1253"),
        # SOY fallback lots ask for Jan 1; the real provider falls back to
        # the 2023-12-29 fixing (1.105) — pinned directly at the query date.
        datetime.date(2024, 1, 1): Decimal("1.105"),
        datetime.date(2024, 2, 12): Decimal("1.0773"),
        datetime.date(2024, 3, 5): Decimal("1.0849"),
        datetime.date(2024, 4, 15): Decimal("1.0656"),
        datetime.date(2024, 5, 20): Decimal("1.0861"),
        datetime.date(2024, 6, 14): Decimal("1.0686"),
        datetime.date(2024, 9, 10): Decimal("1.1031"),
    }

    def get_rate(self, date_of_conversion: datetime.date, currency_code: str) -> Optional[Decimal]:
        ccy = currency_code.upper()
        if ccy == "EUR":
            return Decimal("1")
        if ccy != "USD":
            return None
        for back in range(MAX_FALLBACK_DAYS + 1):
            rate = self.USD_PER_EUR.get(date_of_conversion - datetime.timedelta(days=back))
            if rate is not None:
                return rate
        return None

    def prefetch_rates(self, start_date: datetime.date, end_date: datetime.date, currencies: Set[str]):
        pass

    def get_currency_code_mapping(self) -> Dict[str, str]:
        return {"CNH": "CNY"}

    def get_max_fallback_days(self) -> int:
        return MAX_FALLBACK_DAYS


class GoldenCnbProvider(ExchangeRateProvider):
    """Real ČNB rates, pinned. Stored as CZK per 1 foreign unit; ``get_rate``
    returns the provider convention (foreign units per 1 CZK)."""

    CZK_PER_UNIT: Dict[Tuple[str, datetime.date], Decimal] = {
        ("EUR", datetime.date(2020, 6, 15)): Decimal("26.680"),
        ("EUR", datetime.date(2024, 2, 12)): Decimal("25.215"),
        ("EUR", datetime.date(2024, 3, 5)): Decimal("25.355"),
        # Only gates zero-amount cost legs of option expiries.
        ("EUR", datetime.date(2024, 3, 15)): Decimal("25.155"),
        ("EUR", datetime.date(2024, 5, 20)): Decimal("24.745"),
        ("EUR", datetime.date(2024, 6, 14)): Decimal("24.740"),
        ("EUR", datetime.date(2024, 9, 10)): Decimal("25.055"),
        ("USD", datetime.date(2024, 4, 15)): Decimal("23.768"),
        ("USD", datetime.date(2024, 6, 14)): Decimal("23.154"),
    }

    def _lookup(
        self, date_of_conversion: datetime.date, ccy: str
    ) -> Optional[Tuple[Decimal, datetime.date]]:
        for back in range(MAX_FALLBACK_DAYS + 1):
            d = date_of_conversion - datetime.timedelta(days=back)
            czk = self.CZK_PER_UNIT.get((ccy, d))
            if czk is not None:
                return Decimal("1") / czk, d
        return None

    def get_rate(self, date_of_conversion: datetime.date, currency_code: str) -> Optional[Decimal]:
        info = self.get_rate_info(date_of_conversion, currency_code)
        return info[0] if info else None

    def get_rate_info(
        self, date_of_conversion: datetime.date, currency_code: str
    ) -> Optional[Tuple[Decimal, datetime.date]]:
        ccy = currency_code.upper()
        if ccy == "CZK":
            return Decimal("1"), date_of_conversion
        return self._lookup(date_of_conversion, ccy)

    def prefetch_rates(self, start_date: datetime.date, end_date: datetime.date, currencies: Set[str]):
        pass

    def get_currency_code_mapping(self) -> Dict[str, str]:
        return {}

    def get_max_fallback_days(self) -> int:
        return MAX_FALLBACK_DAYS
