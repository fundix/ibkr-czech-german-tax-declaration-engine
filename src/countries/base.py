# src/countries/base.py
"""
Base Protocol definitions for the country tax plugin system.

Each country module (e.g., countries/de/, countries/cz/) implements these
Protocols to provide country-specific tax classification, aggregation,
and reporting logic.

Existing shared data models used across countries:
- RealizedGainLoss  (src.domain.results)  — per-lot disposal result
- CashFlowEvent     (src.domain.events)   — dividends, distributions, interest
- WithholdingTaxEvent (src.domain.events) — withholding tax records
- FinancialEvent    (src.domain.events)   — base for all economic events
- Asset hierarchy   (src.domain.assets)   — Stock, Bond, InvestmentFund, Option, …

These are NOT duplicated here. Country plugins consume and annotate them.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol, Set, runtime_checkable
import uuid

from src.domain.assets import Asset
from src.domain.enums import AssetCategory, RealizationType
from src.domain.events import FinancialEvent
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver


# ---------------------------------------------------------------------------
# FxProvider — abstract exchange-rate source
# ---------------------------------------------------------------------------

@runtime_checkable
class FxProvider(Protocol):
    """
    Structural interface for exchange-rate providers.

    The existing ``ExchangeRateProvider`` base class and its
    ``ECBExchangeRateProvider`` subclass already satisfy this Protocol
    without any modification.
    """

    def get_rate(
        self, date_of_conversion: datetime.date, currency_code: str
    ) -> Optional[Decimal]:
        """Return foreign-currency-units-per-1-EUR for *currency_code* on *date_of_conversion*."""
        ...

    def prefetch_rates(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        currencies: Set[str],
    ) -> None:
        """Optional bulk prefetch (no-op by default)."""
        ...


# ---------------------------------------------------------------------------
# TaxClassifier — assigns country-specific tax category to a raw RGL
# ---------------------------------------------------------------------------

@runtime_checkable
class TaxClassifier(Protocol):
    """
    Assigns a country-specific tax reporting category to a
    ``RealizedGainLoss`` object.

    The classifier may mutate the RGL in-place (setting
    ``tax_reporting_category`` and related fields) **or** return a
    separate annotation object — the contract is intentionally flexible
    so that the German implementation can keep its current in-place
    style while future implementations may choose a different approach.
    """

    def classify(
        self,
        rgl: RealizedGainLoss,
        asset: Optional[Asset] = None,
    ) -> None:
        """Classify *rgl* and set its ``tax_reporting_category`` (and any
        country-specific optional fields) in-place."""
        ...


# ---------------------------------------------------------------------------
# TaxResult — country-agnostic container for aggregated tax figures
# ---------------------------------------------------------------------------

@dataclass
class TaxResultSection:
    """One section of a tax result (e.g. 'Capital gains', 'Fund income')."""

    section_key: str
    label: str
    line_items: Dict[str, Decimal] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class TaxResult:
    """
    Country-agnostic container returned by ``TaxAggregator.aggregate()``.

    ``sections`` holds human-readable summaries keyed by section name.
    ``country_result`` holds the **original** country-specific result object
    (e.g. ``LossOffsettingResult`` for Germany) so that existing reporting
    code can access it without changes during the transition period.
    """

    country_code: str
    tax_year: int
    sections: Dict[str, TaxResultSection] = field(default_factory=dict)
    country_result: Any = None  # e.g. LossOffsettingResult for DE


# ---------------------------------------------------------------------------
# TaxAggregator — country-specific loss offsetting / form-line calculation
# ---------------------------------------------------------------------------

@runtime_checkable
class TaxAggregator(Protocol):
    """
    Aggregates classified ``RealizedGainLoss`` items, income events, and
    withholding-tax events into a ``TaxResult``.

    For Germany this wraps ``LossOffsettingEngine``.
    """

    def aggregate(
        self,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
        tax_year: int,
    ) -> TaxResult:
        ...


# ---------------------------------------------------------------------------
# OutputRenderer — renders a TaxResult to console / PDF / …
# ---------------------------------------------------------------------------

@runtime_checkable
class OutputRenderer(Protocol):
    """Renders a ``TaxResult`` for the end user."""

    def render_console(
        self,
        tax_result: TaxResult,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
    ) -> None:
        ...

    def render_pdf(
        self,
        tax_result: TaxResult,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
        output_path: str,
    ) -> None:
        ...


# ---------------------------------------------------------------------------
# TaxPlugin — top-level façade wiring a country module together
# ---------------------------------------------------------------------------

@runtime_checkable
class TaxPlugin(Protocol):
    """
    Top-level entry point for a country tax module.

    ``main.py`` (or a future dispatcher) calls::

        plugin = get_tax_plugin("de")
        classifier = plugin.get_tax_classifier()
        aggregator = plugin.get_tax_aggregator()
        renderer  = plugin.get_output_renderer()
    """

    @property
    def country_code(self) -> str:
        """ISO 3166-1 alpha-2 country code (lowercase), e.g. ``'de'``."""
        ...

    def get_tax_classifier(self) -> TaxClassifier:
        ...

    def get_tax_aggregator(self) -> TaxAggregator:
        ...

    def get_output_renderer(self) -> OutputRenderer:
        ...

    def get_merger_policy(self) -> Optional["MergerPolicy"]:
        """How this jurisdiction treats a stock-for-stock merger in the ledger.

        ``None`` means the country module has no rule on file, and the engine
        will refuse such an event rather than pick a treatment. See
        ``src/engine/merger_policy.py``.
        """
        ...
