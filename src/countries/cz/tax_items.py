# src/countries/cz/tax_items.py
"""
Czech tax item model — the atomic unit of the CZ tax result.

Every tax-relevant economic fact (dividend, interest payment, security
disposal, option close/expiry) becomes one ``CzTaxItem``.  Each item
carries:

- **bucket** — which CZ tax section it belongs to (§8 or §10),
- **source identifiers** — event IDs, asset info, dates,
- **original amounts** — in the transaction currency,
- **CZK amounts** — converted per-event via the CZ FX policy,
- **FX metadata** — full ``FxConversionRecord`` audit trail,
- **withholding tax link** — for income items that have associated WHT.

The model is designed to be trivially serialisable to JSON / XLSX
and to support later additions (time test, WHT credit calculation)
without structural changes.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Dict, List, Optional

from src.countries.cz.enums import CzTaxSection
from src.countries.cz.fx_policy import FxConversionRecord


# ---------------------------------------------------------------------------
# Item type — distinguishes the economic nature of the item
# ---------------------------------------------------------------------------

class CzTaxItemType(Enum):
    """What kind of economic event produced this tax item."""
    DIVIDEND = auto()
    FUND_DISTRIBUTION = auto()
    INTEREST = auto()
    SECURITY_DISPOSAL = auto()     # Stock, bond, ETF/fund sale
    OPTION_CLOSE = auto()          # Option traded to close (buy-to-close / sell-to-close)
    OPTION_EXPIRY_WORTHLESS = auto()
    OPTION_EXERCISE_ASSIGNMENT = auto()  # Realized outcome from exercise/assignment
    OTHER = auto()


class CzTaxReviewStatus(Enum):
    """Tax-classification review status for a CzTaxItem."""
    RESOLVED = auto()              # Taxability fully determined
    PENDING_MANUAL_REVIEW = auto() # Missing data — needs human input


class CzExemptionReason(Enum):
    """Why a CzTaxItem is exempt from Czech income tax."""
    TIME_TEST_PASSED = auto()      # §4/1/w ZDP — holding period exceeded
    ANNUAL_LIMIT_NOT_EXCEEDED = auto()  # Annual proceeds below CZK threshold
    NOT_APPLICABLE = auto()        # Item type not subject to exemption test


# ---------------------------------------------------------------------------
# Withholding-tax record linked to a parent income item
# ---------------------------------------------------------------------------

@dataclass
class CzWhtRecord:
    """Withholding tax linked to a parent income item (dividend / interest)."""
    wht_event_id: uuid.UUID
    event_date: str                        # YYYY-MM-DD
    original_amount: Decimal               # positive, in original currency
    original_currency: str
    amount_czk: Optional[Decimal] = None   # converted via FX policy
    fx: Optional[FxConversionRecord] = None
    source_country: Optional[str] = None   # ISO country code (e.g. "US")

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "wht_event_id": str(self.wht_event_id),
            "event_date": self.event_date,
            "original_amount": str(self.original_amount),
            "original_currency": self.original_currency,
            "amount_czk": str(self.amount_czk) if self.amount_czk is not None else None,
            "source_country": self.source_country,
        }
        if self.fx is not None:
            d["fx_rate"] = str(self.fx.fx_rate_inverse)
            d["fx_date_used"] = self.fx.fx_date_used
        return d


# ---------------------------------------------------------------------------
# CzTaxItem — the main model
# ---------------------------------------------------------------------------

@dataclass
class CzTaxItem:
    """
    One atomic Czech tax item.

    Designed for easy JSON/XLSX export and downstream processing
    (time test, WHT credit, form-line mapping).
    """

    # --- Classification ---
    item_type: CzTaxItemType
    section: CzTaxSection

    # --- Source identification ---
    source_event_id: uuid.UUID           # originating event or RGL event ID
    asset_id: Optional[uuid.UUID] = None
    asset_symbol: Optional[str] = None
    asset_isin: Optional[str] = None
    asset_description: Optional[str] = None
    asset_category: Optional[str] = None  # e.g. "STOCK", "OPTION"

    # --- Dates ---
    event_date: str = ""                  # primary date (trade / payment date)
    acquisition_date: Optional[str] = None  # for disposals: lot acquisition date
    holding_period_days: Optional[int] = None
    # True when acquisition_date is a synthetic SOY fallback (31 Dec of the
    # prior year) — the real purchase date is unknown; the time test flags
    # such items for manual review instead of trusting the date.
    acquisition_date_estimated: bool = False

    # --- Original amounts (transaction currency) ---
    original_amount: Optional[Decimal] = None
    original_currency: Optional[str] = None

    # --- EUR amounts (from core pipeline) ---
    amount_eur: Optional[Decimal] = None

    # --- CZK amounts (per-event converted) ---
    amount_czk: Optional[Decimal] = None

    # --- For disposals: cost basis and proceeds ---
    cost_basis_eur: Optional[Decimal] = None
    proceeds_eur: Optional[Decimal] = None
    gain_loss_eur: Optional[Decimal] = None
    cost_basis_czk: Optional[Decimal] = None
    proceeds_czk: Optional[Decimal] = None
    gain_loss_czk: Optional[Decimal] = None

    # --- FX audit ---
    fx: Optional[FxConversionRecord] = None
    fx_cost_basis: Optional[FxConversionRecord] = None
    fx_proceeds: Optional[FxConversionRecord] = None

    # --- Withholding tax link ---
    wht_records: List[CzWhtRecord] = field(default_factory=list)

    # --- Quantity (for disposals) ---
    quantity: Optional[Decimal] = None

    # --- Short positions ---
    # True when the item realizes a SHORT position (short cover, closing or
    # expiry of a written option). The §4/1/w holding-period time test can
    # never exempt such items: the security is not held between acquisition
    # and transfer (the sale precedes the purchase), and acquisition_date on
    # the item is the short OPENING date, not an ownership start.
    is_short_position: bool = False

    # --- Taxability classification (set by time_test + annual_limit evaluators) ---
    is_taxable: bool = True
    is_exempt: bool = False
    exemption_reason: Optional[CzExemptionReason] = None
    included_in_tax_base: bool = True
    tax_review_status: CzTaxReviewStatus = CzTaxReviewStatus.RESOLVED
    tax_review_note: Optional[str] = None

    # --- Annual exempt limit fields (set by annual_limit evaluator) ---
    qualifies_for_annual_limit: bool = False   # eligible for the 100k test
    exempt_due_to_annual_limit: bool = False    # actually exempted by it

    # --- FX conversion status ---
    # Set by the item builder when an FX converter IS configured but the
    # foreign→CZK conversion could not be performed (unparseable date, or no
    # ČNB rate even after fallback).  The CZK amounts are left as ``None`` in
    # that case — they must NOT be filled with the un-converted foreign amount.
    # Downstream (time test) marks such items PENDING_MANUAL_REVIEW so a wrong
    # CZK figure never silently enters the tax base.
    fx_conversion_failed: bool = False

    def total_wht_czk(self) -> Decimal:
        """Sum of all linked WHT amounts in CZK."""
        return sum(
            (r.amount_czk for r in self.wht_records if r.amount_czk is not None),
            Decimal(0),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Flat dict suitable for JSON / XLSX export."""
        d: Dict[str, Any] = {
            "item_type": self.item_type.name,
            "section": self.section.name,
            "source_event_id": str(self.source_event_id),
            "asset_symbol": self.asset_symbol,
            "asset_isin": self.asset_isin,
            "asset_description": self.asset_description,
            "asset_category": self.asset_category,
            "event_date": self.event_date,
            "acquisition_date": self.acquisition_date,
            "holding_period_days": self.holding_period_days,
            "original_amount": str(self.original_amount) if self.original_amount is not None else None,
            "original_currency": self.original_currency,
            "amount_eur": str(self.amount_eur) if self.amount_eur is not None else None,
            "amount_czk": str(self.amount_czk) if self.amount_czk is not None else None,
            "cost_basis_eur": str(self.cost_basis_eur) if self.cost_basis_eur is not None else None,
            "proceeds_eur": str(self.proceeds_eur) if self.proceeds_eur is not None else None,
            "gain_loss_eur": str(self.gain_loss_eur) if self.gain_loss_eur is not None else None,
            "cost_basis_czk": str(self.cost_basis_czk) if self.cost_basis_czk is not None else None,
            "proceeds_czk": str(self.proceeds_czk) if self.proceeds_czk is not None else None,
            "gain_loss_czk": str(self.gain_loss_czk) if self.gain_loss_czk is not None else None,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "wht_total_czk": str(self.total_wht_czk()),
            "wht_records": [r.to_dict() for r in self.wht_records],
        }
        # --- Taxability ---
        d["is_taxable"] = self.is_taxable
        d["is_exempt"] = self.is_exempt
        d["exemption_reason"] = self.exemption_reason.name if self.exemption_reason else None
        d["included_in_tax_base"] = self.included_in_tax_base
        d["tax_review_status"] = self.tax_review_status.name
        d["tax_review_note"] = self.tax_review_note
        d["qualifies_for_annual_limit"] = self.qualifies_for_annual_limit
        d["exempt_due_to_annual_limit"] = self.exempt_due_to_annual_limit
        d["fx_conversion_failed"] = self.fx_conversion_failed
        # --- Source country (from first WHT record) ---
        src_country = None
        for r in self.wht_records:
            if r.source_country:
                src_country = r.source_country
                break
        d["source_country"] = src_country
        # --- FX ---
        if self.fx is not None:
            d["fx_source"] = self.fx.fx_source
            d["fx_policy"] = self.fx.fx_policy
            d["fx_rate"] = str(self.fx.fx_rate_inverse)
            d["fx_date_used"] = self.fx.fx_date_used
        return d
