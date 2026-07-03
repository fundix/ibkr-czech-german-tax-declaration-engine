# src/countries/cz/fx_policy.py
"""
Czech FX conversion policy and per-event currency converter.

Implements the decided CZ FX policy:
1. Default: daily ČNB rates (denní kurz).
2. Per-event conversion to CZK (not aggregate).
3. Direct foreign → CZK (not through EUR as intermediate).
4. Weekend/holiday fallback: last valid ČNB rate.
5. "uniform" (jednotný) rate mode: placeholder for future.

Every conversion produces an ``FxConversionRecord`` with full audit
metadata so that each CZK amount is traceable.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto
from typing import Optional

from src.utils.exchange_rate_provider import ExchangeRateProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FX policy configuration
# ---------------------------------------------------------------------------

class CzFxMode(Enum):
    """FX rate selection mode for a CZ tax year."""
    DAILY = auto()       # Denní kurz ČNB (default)
    UNIFORM = auto()     # Jednotný kurz podle §38/1 ZDP (pokyn GFŘ řady D)


class CzFxWeekendFallback(Enum):
    """How to handle weekends / holidays with no published rate."""
    PREVIOUS_VALID_RATE = auto()  # Use last published rate (default)


@dataclass(frozen=True)
class CzFxPolicyConfig:
    """
    Immutable FX policy descriptor for one Czech tax year.

    The CZ plugin enforces that ``mode`` does NOT change mid-year — a
    single ``CzFxPolicyConfig`` applies to the whole tax period.
    """
    mode: CzFxMode = CzFxMode.DAILY
    source: str = "cnb"
    weekend_fallback: CzFxWeekendFallback = CzFxWeekendFallback.PREVIOUS_VALID_RATE


def uniform_fx_policy() -> "CzFxPolicyConfig":
    """Policy descriptor for the uniform ("jednotný kurz") mode — pair it
    with ``CzUniformRateProvider`` when constructing the plugin."""
    return CzFxPolicyConfig(mode=CzFxMode.UNIFORM, source="gfr-jednotny-kurz")


# ---------------------------------------------------------------------------
# Audit metadata for every conversion
# ---------------------------------------------------------------------------

@dataclass
class FxConversionRecord:
    """
    Immutable audit record attached to every CZK amount.

    Answers "where did this CZK number come from?" for any tax item.
    """
    original_amount: Decimal
    original_currency: str
    converted_amount_czk: Decimal
    fx_rate: Decimal          # foreign-currency units per 1 CZK (CNB convention)
    fx_rate_inverse: Decimal  # CZK per 1 foreign-currency unit (human-readable)
    fx_date_used: str         # YYYY-MM-DD — actual date of the rate used
    fx_source: str            # "cnb", "ecb", …
    fx_policy: str            # "daily", "uniform", …
    event_date: str           # YYYY-MM-DD — date of the tax-relevant event
    conversion_note: Optional[str] = None  # e.g. "weekend fallback from 2024-03-22"


# ---------------------------------------------------------------------------
# CZ currency converter
# ---------------------------------------------------------------------------

class CzCurrencyConverter:
    """
    Converts any foreign-currency amount to CZK using a supplied
    ``ExchangeRateProvider`` (typically ``CNBExchangeRateProvider``).

    The converter is stateless — policy is passed in at construction
    time and the provider handles caching / fallback internally.

    This class does **not** touch the EUR amounts already on events;
    it performs a fresh, direct foreign→CZK lookup for each call.
    """

    def __init__(
        self,
        provider: ExchangeRateProvider,
        policy: CzFxPolicyConfig,
    ):
        self._provider = provider
        self._policy = policy

    @property
    def policy(self) -> CzFxPolicyConfig:
        return self._policy

    # -- public API ---------------------------------------------------------

    def convert_to_czk(
        self,
        amount: Decimal,
        currency: str,
        event_date: datetime.date,
    ) -> Optional[FxConversionRecord]:
        """
        Convert *amount* in *currency* to CZK using the rate for *event_date*.

        Returns an ``FxConversionRecord`` with full audit trail,
        or ``None`` if the rate could not be obtained.

        If *currency* is already ``CZK``, returns a trivial record
        with rate = 1.
        """
        currency_upper = currency.upper()

        if currency_upper == "CZK":
            return FxConversionRecord(
                original_amount=amount,
                original_currency="CZK",
                converted_amount_czk=amount,
                fx_rate=Decimal("1"),
                fx_rate_inverse=Decimal("1"),
                fx_date_used=event_date.strftime("%Y-%m-%d"),
                fx_source=self._policy.source,
                fx_policy=self._policy.mode.name.lower(),
                event_date=event_date.strftime("%Y-%m-%d"),
            )

        # get_rate returns foreign-currency-units-per-1-CZK. Prefer the
        # info variant so the audit record carries the REAL rate date when
        # the weekend/holiday fallback was applied.
        actual_rate_date = event_date
        if hasattr(self._provider, "get_rate_info"):
            rate_info = self._provider.get_rate_info(event_date, currency_upper)
            rate = rate_info[0] if rate_info else None
            if rate_info is not None:
                actual_rate_date = rate_info[1]
        else:
            rate = self._provider.get_rate(event_date, currency_upper)

        if rate is None or rate <= Decimal(0):
            logger.warning(
                f"No CNB rate for {currency_upper} on {event_date}. "
                f"Cannot convert {amount} {currency_upper} to CZK."
            )
            return None

        # CZK = amount / rate  (rate = foreign_per_czk)
        czk_amount = amount / rate

        event_date_str = event_date.strftime("%Y-%m-%d")
        fx_date_str = actual_rate_date.strftime("%Y-%m-%d")
        conversion_note: Optional[str] = None
        if actual_rate_date != event_date:
            conversion_note = (
                f"Weekend/holiday fallback: ČNB rate of {fx_date_str} used "
                f"for event date {event_date_str}."
            )
        if self._policy.mode == CzFxMode.UNIFORM:
            conversion_note = (
                f"Jednotný kurz {event_date.year} podle §38 odst. 1 ZDP "
                "(pokyn GFŘ řady D)."
            )
        rate_inverse = Decimal("1") / rate if rate != Decimal(0) else Decimal(0)

        return FxConversionRecord(
            original_amount=amount,
            original_currency=currency_upper,
            converted_amount_czk=czk_amount,
            fx_rate=rate,
            fx_rate_inverse=rate_inverse,
            fx_date_used=fx_date_str,
            fx_source=self._policy.source,
            fx_policy=self._policy.mode.name.lower(),
            event_date=event_date_str,
            conversion_note=conversion_note,
        )

    def convert_eur_to_czk(
        self,
        eur_amount: Decimal,
        event_date: datetime.date,
    ) -> Optional[FxConversionRecord]:
        """
        Convenience shortcut: convert an EUR amount to CZK.

        This is the path used for RealizedGainLoss items where only
        EUR totals are available from the core pipeline.  It is still
        a *direct* EUR→CZK conversion via CNB (not EUR→USD→CZK etc.).
        """
        return self.convert_to_czk(eur_amount, "EUR", event_date)
