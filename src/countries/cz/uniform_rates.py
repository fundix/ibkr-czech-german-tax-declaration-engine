# src/countries/cz/uniform_rates.py
"""
Uniform exchange rates ("jednotný kurz", §38 odst. 1 ZDP) and the provider
that serves them to the CZ FX layer.

Taxpayers who do NOT keep accounting books may convert foreign currency
using the uniform rate published by GFŘ for each tax year (pokyn řady D),
instead of daily ČNB rates. One mode must be used consistently for the
whole tax year — no mixing (enforced by ``CzFxPolicyConfig``).

Policy for multi-year items (documented assumption): each cash flow
converts at the uniform rate of the calendar year in which it occurred —
e.g. the acquisition cost of a security bought in 2020 and sold in 2024
converts at the 2020 uniform rate, the proceeds at the 2024 rate. This
mirrors the per-leg logic of NSS 2 Afs 4/2019-35 with year-average rates.

Rates below are transcribed from the official pokyny (per-1-unit values are
derived from the published quantity/value pairs):

- 2025: pokyn GFŘ-D-75 (č. j. 95534/25/7100-10111-802540, FZ 2/2026)
- 2024: pokyn GFŘ-D-66 (supersedes GFŘ-D-65; EUR/USD identical in both).
  The 2024 TRY value is intentionally OMITTED: D-65 printed "1 TRY = 70,48"
  (an obvious quantity misprint) and the corrected D-66 table was not
  verified against the official PDF — supply it via ``rates_overrides``
  if you need TRY for 2024.
- 2020: pokyn GFŘ-D-49 — only EUR/USD transcribed (extend as needed).

Verify against the pokyn for your tax year before filing.
"""
from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Dict, Optional, Set, Tuple

from src.utils.exchange_rate_provider import ExchangeRateProvider

logger = logging.getLogger(__name__)

# Year -> currency -> (quantity, CZK per quantity), exactly as published in
# the pokyn table ("Množství", "Průměr"). Kept in published form so the
# values can be audited against the source document line by line.
OFFICIAL_UNIFORM_RATES: Dict[int, Dict[str, Tuple[str, str]]] = {
    2020: {  # GFŘ-D-49 (partial transcription)
        "EUR": ("1", "26.50"),
        "USD": ("1", "23.14"),
    },
    2024: {  # GFŘ-D-66 / D-65
        "AUD": ("1", "15.31"),
        "BRL": ("1", "4.29"),
        "BGN": ("1", "12.86"),
        "CNY": ("1", "3.24"),
        "DKK": ("1", "3.37"),
        "EUR": ("1", "25.16"),
        "PHP": ("100", "40.55"),
        "HKD": ("1", "2.98"),
        "INR": ("100", "27.80"),
        "IDR": ("1000", "1.47"),
        "ISK": ("100", "16.89"),
        "ILS": ("1", "6.32"),
        "JPY": ("100", "15.35"),
        "ZAR": ("1", "1.27"),
        "CAD": ("1", "16.96"),
        "KRW": ("100", "1.70"),
        "HUF": ("100", "6.34"),
        "MYR": ("1", "5.11"),
        "MXN": ("1", "1.26"),
        "XDR": ("1", "30.87"),
        "NOK": ("1", "2.16"),
        "NZD": ("1", "14.03"),
        "PLN": ("1", "5.85"),
        "RON": ("1", "5.06"),
        "SGD": ("1", "17.41"),
        "SEK": ("1", "2.20"),
        "CHF": ("1", "26.40"),
        "THB": ("100", "66.21"),
        # "TRY": omitted for 2024 — see module docstring
        "USD": ("1", "23.28"),
        "GBP": ("1", "29.78"),
    },
    2025: {  # GFŘ-D-75
        "AUD": ("1", "14.06"),
        "BRL": ("1", "3.92"),
        "BGN": ("1", "12.61"),
        "CNY": ("1", "3.05"),
        "DKK": ("1", "3.30"),
        "EUR": ("1", "24.66"),
        "PHP": ("100", "37.90"),
        "HKD": ("1", "2.80"),
        "INR": ("100", "25.03"),
        "IDR": ("1000", "1.32"),
        "ISK": ("100", "17.06"),
        "ILS": ("1", "6.37"),
        "JPY": ("100", "14.59"),
        "ZAR": ("1", "1.22"),
        "CAD": ("1", "15.61"),
        "KRW": ("100", "1.54"),
        "HUF": ("100", "6.22"),
        "MYR": ("1", "5.11"),
        "MXN": ("1", "1.14"),
        "XDR": ("1", "29.49"),
        "NOK": ("1", "2.11"),
        "NZD": ("1", "12.67"),
        "PLN": ("1", "5.82"),
        "RON": ("1", "4.89"),
        "SGD": ("1", "16.71"),
        "SEK": ("1", "2.23"),
        "CHF": ("1", "26.33"),
        "THB": ("100", "66.45"),
        "TRY": ("100", "55.11"),
        "USD": ("1", "21.84"),
        "GBP": ("1", "28.80"),
    },
}


class CzUniformRateProvider(ExchangeRateProvider):
    """
    ``ExchangeRateProvider`` serving the GFŘ uniform rates.

    The rate for a conversion date is the uniform rate of that date's
    CALENDAR YEAR (see module docstring for the multi-year policy).
    ``get_rate`` returns the provider convention used across the engine:
    foreign-currency units per 1 CZK.

    Years/currencies missing from the table yield ``None`` — the CZ layer
    then flags the item as a failed conversion for manual review instead of
    silently substituting a wrong rate. Extend coverage via
    ``rates_overrides`` (year -> currency -> (quantity, czk) as published).
    """

    def __init__(
        self,
        rates_overrides: Optional[Dict[int, Dict[str, Tuple[str, str]]]] = None,
    ):
        merged: Dict[int, Dict[str, Tuple[str, str]]] = {
            year: dict(table) for year, table in OFFICIAL_UNIFORM_RATES.items()
        }
        for year, table in (rates_overrides or {}).items():
            merged.setdefault(year, {}).update(table)
        # Normalize to CZK per 1 unit of foreign currency
        self._czk_per_unit: Dict[Tuple[int, str], Decimal] = {}
        for year, table in merged.items():
            for ccy, (quantity, czk) in table.items():
                self._czk_per_unit[(year, ccy.upper())] = (
                    Decimal(czk) / Decimal(quantity)
                )

    def get_rate(
        self, date_of_conversion: datetime.date, currency_code: str
    ) -> Optional[Decimal]:
        ccy = currency_code.upper()
        if ccy == "CZK":
            return Decimal("1")
        czk_per_unit = self._czk_per_unit.get((date_of_conversion.year, ccy))
        if czk_per_unit is None or czk_per_unit == Decimal(0):
            logger.warning(
                f"No uniform rate for {ccy} in year {date_of_conversion.year} "
                "— extend OFFICIAL_UNIFORM_RATES or pass rates_overrides."
            )
            return None
        return Decimal("1") / czk_per_unit

    def get_rate_info(
        self, date_of_conversion: datetime.date, currency_code: str
    ) -> Optional[Tuple[Decimal, datetime.date]]:
        rate = self.get_rate(date_of_conversion, currency_code)
        if rate is None:
            return None
        # The uniform rate has no daily fixing date; the event date is the
        # correct audit anchor (the year identifies the pokyn).
        return rate, date_of_conversion

    def prefetch_rates(
        self, start_date: datetime.date, end_date: datetime.date, currencies: Set[str]
    ):
        pass

    def get_currency_code_mapping(self) -> Dict[str, str]:
        return {}

    def get_max_fallback_days(self) -> int:
        return 0
