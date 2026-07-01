# src/countries/cz/item_builder.py
"""
Builds ``CzTaxItem`` objects from core pipeline outputs.

Responsibilities:
1. Convert each ``RealizedGainLoss`` into a disposal ``CzTaxItem``.
2. Convert each income ``CashFlowEvent`` into an income ``CzTaxItem``.
3. Link ``WithholdingTaxEvent``s to their parent income items as ``CzWhtRecord``.
4. Perform per-event CZK conversion via ``CzCurrencyConverter``.
5. Populate asset metadata from ``AssetResolver``.

This module does **not** apply the time test, expense deductions, or
WHT credit calculation — those are downstream consumers of the items.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import uuid

from src.countries.cz.enums import CzTaxSection, category_to_cz_section
from src.countries.cz.fx_policy import CzCurrencyConverter, FxConversionRecord
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzWhtRecord
from src.domain.assets import Asset
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import (
    CashFlowEvent,
    FinancialEvent,
    WithholdingTaxEvent,
)
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver
from src.utils.type_utils import parse_ibkr_date

logger = logging.getLogger(__name__)

_OPTION_REALIZATION_TYPES = {
    RealizationType.OPTION_TRADE_CLOSE_LONG,
    RealizationType.OPTION_TRADE_CLOSE_SHORT,
    RealizationType.OPTION_EXPIRED_LONG,
    RealizationType.OPTION_EXPIRED_SHORT,
}

# ---------------------------------------------------------------------------
# Asset metadata helper
# ---------------------------------------------------------------------------

def _asset_meta(asset: Optional[Asset]) -> dict:
    if asset is None:
        return {}
    return {
        "asset_id": asset.internal_asset_id,
        "asset_symbol": asset.ibkr_symbol,
        "asset_isin": getattr(asset, "ibkr_isin", None),
        "asset_description": asset.description,
        "asset_category": asset.asset_category.name if asset.asset_category else None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_tax_items(
    realized_gains_losses: List[RealizedGainLoss],
    financial_events: List[FinancialEvent],
    asset_resolver: AssetResolver,
    fx: Optional[CzCurrencyConverter] = None,
) -> Tuple[List[CzTaxItem], List[FxConversionRecord]]:
    """
    Build ``CzTaxItem`` list from core pipeline outputs.

    Returns ``(items, fx_records)`` where *fx_records* is the full
    audit trail of all FX conversions performed.
    """
    fx_records: List[FxConversionRecord] = []

    # --- Phase 1: income events + WHT index ---
    income_items, wht_index = _build_income_items(
        financial_events, asset_resolver, fx, fx_records,
    )

    # --- Phase 2: link WHT to income items ---
    unlinked_wht_ids = _link_wht(income_items, wht_index, asset_resolver, fx, fx_records)

    # --- Phase 2b: create standalone items for unlinked WHT ---
    unlinked_items = _build_unlinked_wht_items(
        unlinked_wht_ids, wht_index, asset_resolver, fx, fx_records,
    )

    # --- Phase 3: disposal items from RGLs ---
    disposal_items = _build_disposal_items(
        realized_gains_losses, asset_resolver, fx, fx_records,
    )

    all_items = income_items + unlinked_items + disposal_items
    return all_items, fx_records


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _convert(
    amount: Optional[Decimal],
    currency: Optional[str],
    date_str: str,
    fx: Optional[CzCurrencyConverter],
    fx_records: List[FxConversionRecord],
) -> Tuple[Optional[Decimal], Optional[FxConversionRecord]]:
    """Convert *amount* to CZK.  Returns (czk_amount, record).

    Behaviour:
    - ``amount is None`` → ``(None, None)``.
    - No FX converter configured (``fx is None``) → return the amount unchanged
      (the plugin runs in EUR mode and labels its output "amounts in EUR").
    - FX configured but the conversion cannot be performed (unparseable date, or
      no ČNB rate even after fallback) → ``(None, None)``.  The caller MUST treat
      a ``None`` CZK value as a *failed* conversion and flag the item for review.
      It must NEVER substitute the un-converted foreign amount into a CZK field —
      doing so would book e.g. 1000 USD as 1000 CZK (~22× understatement).
    """
    if amount is None:
        return None, None
    if fx is None:
        return amount, None
    if currency is None:
        currency = "EUR"
    dt = parse_ibkr_date(date_str)
    if dt is None:
        return None, None
    rec = fx.convert_to_czk(amount, currency, dt)
    if rec is None:
        return None, None
    fx_records.append(rec)
    return rec.converted_amount_czk, rec


def _convert_eur(
    eur_amount: Optional[Decimal],
    date_str: str,
    fx: Optional[CzCurrencyConverter],
    fx_records: List[FxConversionRecord],
) -> Tuple[Optional[Decimal], Optional[FxConversionRecord]]:
    return _convert(eur_amount, "EUR", date_str, fx, fx_records)


# --- Income items ----------------------------------------------------------

def _build_income_items(
    events: List[FinancialEvent],
    resolver: AssetResolver,
    fx: Optional[CzCurrencyConverter],
    fx_records: List[FxConversionRecord],
) -> Tuple[List[CzTaxItem], Dict[uuid.UUID, WithholdingTaxEvent]]:
    """
    Build income ``CzTaxItem``s from ``CashFlowEvent``s.

    Also collects a dict of ``WithholdingTaxEvent``s keyed by event_id
    for later linking.
    """
    items: List[CzTaxItem] = []
    wht_events: Dict[uuid.UUID, WithholdingTaxEvent] = {}

    for ev in events:
        if isinstance(ev, WithholdingTaxEvent):
            wht_events[ev.event_id] = ev
            continue

        if not isinstance(ev, CashFlowEvent):
            continue

        if ev.event_type == FinancialEventType.DIVIDEND_CASH:
            item_type = CzTaxItemType.DIVIDEND
            section = CzTaxSection.CZ_8_DIVIDENDS
        elif ev.event_type == FinancialEventType.DISTRIBUTION_FUND:
            item_type = CzTaxItemType.FUND_DISTRIBUTION
            section = CzTaxSection.CZ_8_DIVIDENDS
        elif ev.event_type == FinancialEventType.INTEREST_RECEIVED:
            item_type = CzTaxItemType.INTEREST
            section = CzTaxSection.CZ_8_INTEREST
        else:
            continue

        # Prefer original currency for direct conversion
        orig_amt = ev.gross_amount_foreign_currency
        orig_cur = ev.local_currency
        if orig_amt is not None and orig_cur is not None:
            czk, fx_rec = _convert(orig_amt, orig_cur, ev.event_date, fx, fx_records)
        else:
            orig_amt = ev.gross_amount_eur
            orig_cur = "EUR"
            czk, fx_rec = _convert_eur(ev.gross_amount_eur, ev.event_date, fx, fx_records)

        asset = resolver.get_asset_by_id(ev.asset_internal_id)
        meta = _asset_meta(asset)

        item = CzTaxItem(
            item_type=item_type,
            section=section,
            source_event_id=ev.event_id,
            event_date=ev.event_date,
            original_amount=orig_amt,
            original_currency=orig_cur,
            amount_eur=ev.gross_amount_eur,
            amount_czk=czk,
            fx=fx_rec,
            **meta,
        )
        if fx is not None and czk is None:
            item.fx_conversion_failed = True
        items.append(item)

    return items, wht_events


# --- WHT linking -----------------------------------------------------------

def _link_wht(
    income_items: List[CzTaxItem],
    wht_events: Dict[uuid.UUID, WithholdingTaxEvent],
    resolver: AssetResolver,
    fx: Optional[CzCurrencyConverter],
    fx_records: List[FxConversionRecord],
) -> set:
    """Attach WHT events to their parent income items.

    Linking strategy (in priority order):
    1. ``wht.taxed_income_event_id`` matches an income item's ``source_event_id``.
    2. Same ``asset_internal_id`` + same ``event_date``.
    3. Same ``asset_internal_id`` (nearest date, ±3 days).

    Returns the set of WHT event IDs that could NOT be linked.
    """
    linked_wht_ids: set = set()

    # Index income items by event_id and by (asset_id, date)
    by_event_id: Dict[uuid.UUID, CzTaxItem] = {
        it.source_event_id: it for it in income_items
    }
    by_asset_date: Dict[Tuple, CzTaxItem] = {}
    by_asset: Dict[uuid.UUID, List[CzTaxItem]] = {}
    for it in income_items:
        if it.asset_id is not None:
            by_asset_date[(it.asset_id, it.event_date)] = it
            by_asset.setdefault(it.asset_id, []).append(it)

    for wht_id, wht in wht_events.items():
        target: Optional[CzTaxItem] = None

        # Strategy 1: explicit link
        if wht.taxed_income_event_id and wht.taxed_income_event_id in by_event_id:
            target = by_event_id[wht.taxed_income_event_id]

        # Strategy 2: same asset + same date
        if target is None:
            target = by_asset_date.get((wht.asset_internal_id, wht.event_date))

        # Strategy 3: same asset, nearest date within ±3 days
        if target is None and wht.asset_internal_id in by_asset:
            wht_dt = parse_ibkr_date(wht.event_date)
            if wht_dt:
                candidates = by_asset[wht.asset_internal_id]
                best = None
                best_delta = 999
                for c in candidates:
                    c_dt = parse_ibkr_date(c.event_date)
                    if c_dt:
                        delta = abs((c_dt - wht_dt).days)
                        if delta <= 3 and delta < best_delta:
                            best = c
                            best_delta = delta
                if best is not None:
                    target = best

        if target is None:
            # Cannot link — will be emitted as standalone item
            continue

        # Build WHT record
        orig_amt = wht.gross_amount_foreign_currency
        orig_cur = wht.local_currency
        if orig_amt is not None and orig_cur is not None:
            czk, fx_rec = _convert(orig_amt, orig_cur, wht.event_date, fx, fx_records)
        else:
            orig_amt = wht.gross_amount_eur or Decimal(0)
            orig_cur = "EUR"
            czk, fx_rec = _convert_eur(wht.gross_amount_eur, wht.event_date, fx, fx_records)

        wht_rec = CzWhtRecord(
            wht_event_id=wht.event_id,
            event_date=wht.event_date,
            original_amount=orig_amt if orig_amt is not None else Decimal(0),
            original_currency=orig_cur or "EUR",
            amount_czk=czk,
            fx=fx_rec,
            source_country=getattr(wht, "source_country_code", None),
        )
        if fx is not None and czk is None:
            target.fx_conversion_failed = True
        target.wht_records.append(wht_rec)
        linked_wht_ids.add(wht_id)

    return set(wht_events.keys()) - linked_wht_ids


# --- Unlinked WHT items ----------------------------------------------------

def _build_unlinked_wht_items(
    unlinked_ids: set,
    wht_events: Dict[uuid.UUID, WithholdingTaxEvent],
    resolver: AssetResolver,
    fx: Optional[CzCurrencyConverter],
    fx_records: List[FxConversionRecord],
) -> List[CzTaxItem]:
    """Create standalone ``CzTaxItem`` for each WHT that could not be linked.

    These items appear in the output with ``item_type=OTHER`` and carry
    the WHT as a self-referencing ``CzWhtRecord`` so the amount is not lost.
    """
    items: List[CzTaxItem] = []
    for wht_id in unlinked_ids:
        wht = wht_events.get(wht_id)
        if wht is None:
            continue

        orig_amt = wht.gross_amount_foreign_currency
        orig_cur = wht.local_currency
        if orig_amt is not None and orig_cur is not None:
            czk, fx_rec = _convert(orig_amt, orig_cur, wht.event_date, fx, fx_records)
        else:
            orig_amt = wht.gross_amount_eur or Decimal(0)
            orig_cur = "EUR"
            czk, fx_rec = _convert_eur(wht.gross_amount_eur, wht.event_date, fx, fx_records)

        asset = resolver.get_asset_by_id(wht.asset_internal_id)
        meta = _asset_meta(asset)

        wht_rec = CzWhtRecord(
            wht_event_id=wht.event_id,
            event_date=wht.event_date,
            original_amount=orig_amt if orig_amt is not None else Decimal(0),
            original_currency=orig_cur or "EUR",
            amount_czk=czk,
            fx=fx_rec,
            source_country=getattr(wht, "source_country_code", None),
        )

        item = CzTaxItem(
            item_type=CzTaxItemType.OTHER,
            section=CzTaxSection.CZ_8_DIVIDENDS,  # WHT most commonly relates to dividends
            source_event_id=wht.event_id,
            event_date=wht.event_date,
            original_amount=orig_amt,
            original_currency=orig_cur,
            amount_eur=wht.gross_amount_eur,
            amount_czk=czk,
            fx=fx_rec,
            wht_records=[wht_rec],
            **meta,
        )
        item.tax_review_note = "Unlinked WHT — no matching income event found"
        if fx is not None and czk is None:
            item.fx_conversion_failed = True
        items.append(item)

    return items


# --- Disposal items --------------------------------------------------------

def _build_disposal_items(
    rgls: List[RealizedGainLoss],
    resolver: AssetResolver,
    fx: Optional[CzCurrencyConverter],
    fx_records: List[FxConversionRecord],
) -> List[CzTaxItem]:
    items: List[CzTaxItem] = []

    for rgl in rgls:
        cat = rgl.asset_category_at_realization
        # Prefer classifier's section if already set on the RGL
        section = getattr(rgl, "cz_tax_section", None) or category_to_cz_section(cat.name)

        # Determine item type
        if cat in (AssetCategory.OPTION, AssetCategory.CFD):
            if rgl.realization_type in (RealizationType.OPTION_EXPIRED_LONG, RealizationType.OPTION_EXPIRED_SHORT):
                item_type = CzTaxItemType.OPTION_EXPIRY_WORTHLESS
            elif rgl.realization_type in (RealizationType.OPTION_TRADE_CLOSE_LONG, RealizationType.OPTION_TRADE_CLOSE_SHORT):
                item_type = CzTaxItemType.OPTION_CLOSE
            else:
                item_type = CzTaxItemType.OPTION_CLOSE
        else:
            item_type = CzTaxItemType.SECURITY_DISPOSAL

        # FX conversion — RGLs have EUR amounts; convert cost basis and proceeds separately
        cost_czk, fx_cost = _convert_eur(rgl.total_cost_basis_eur, rgl.realization_date, fx, fx_records)
        proceeds_czk, fx_proceeds = _convert_eur(rgl.total_realization_value_eur, rgl.realization_date, fx, fx_records)

        gl_czk: Optional[Decimal] = None
        fx_gl: Optional[FxConversionRecord] = None
        if cost_czk is not None and proceeds_czk is not None:
            gl_czk = proceeds_czk - cost_czk
        # Also convert the gain/loss directly for the main amount_czk field
        gl_czk_direct, fx_gl = _convert_eur(rgl.gross_gain_loss_eur, rgl.realization_date, fx, fx_records)

        asset = resolver.get_asset_by_id(rgl.asset_internal_id)
        meta = _asset_meta(asset)

        item = CzTaxItem(
            item_type=item_type,
            section=section,
            source_event_id=rgl.originating_event_id,
            event_date=rgl.realization_date,
            acquisition_date=rgl.acquisition_date,
            holding_period_days=rgl.holding_period_days,
            original_amount=rgl.gross_gain_loss_eur,
            original_currency="EUR",
            amount_eur=rgl.gross_gain_loss_eur,
            amount_czk=gl_czk_direct,
            cost_basis_eur=rgl.total_cost_basis_eur,
            proceeds_eur=rgl.total_realization_value_eur,
            gain_loss_eur=rgl.gross_gain_loss_eur,
            cost_basis_czk=cost_czk,
            proceeds_czk=proceeds_czk,
            gain_loss_czk=gl_czk,
            fx=fx_gl,
            fx_cost_basis=fx_cost,
            fx_proceeds=fx_proceeds,
            quantity=rgl.quantity_realized,
            **meta,
        )
        if fx is not None and (cost_czk is None or proceeds_czk is None or gl_czk_direct is None):
            item.fx_conversion_failed = True
        items.append(item)

    return items
