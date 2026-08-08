# src/countries/cz/exporters/json_exporter.py
"""
JSON export for Czech tax results.

Produces an audit-friendly, diff-stable JSON structure from a
``TaxResult`` with CZ-specific ``country_result`` data.

All ``Decimal`` values are serialised as strings to preserve precision.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Dict, IO, List, Optional, Union

from src.countries.base import TaxResult

logger = logging.getLogger(__name__)

_EXPORT_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Decimal-safe JSON encoder
# ---------------------------------------------------------------------------

class _DecimalEncoder(json.JSONEncoder):
    """Encodes ``Decimal`` as string to preserve precision."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_cz_to_json(
    tax_result: TaxResult,
    output: Union[str, IO[str], None] = None,
    *,
    indent: int = 2,
) -> str:
    """
    Export a CZ ``TaxResult`` to JSON.

    Args:
        tax_result: The ``TaxResult`` from ``CzechTaxAggregator.aggregate()``.
        output: File path (str) or file-like object to write to.
                If ``None``, only the JSON string is returned.
        indent: JSON indentation level.

    Returns:
        The JSON string (always returned, even if also written to file).
    """
    data = _build_json_structure(tax_result)
    json_str = json.dumps(data, cls=_DecimalEncoder, indent=indent, ensure_ascii=False)

    if output is not None:
        if isinstance(output, str):
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(json_str)
            logger.info(f"CZ JSON export written to {output}")
        else:
            output.write(json_str)

    return json_str


# ---------------------------------------------------------------------------
# Structure builder
# ---------------------------------------------------------------------------

def _per_state(liability) -> Dict[str, Any]:
    """Per-state credit after both §38f caps; empty when unavailable."""
    return getattr(liability, "per_state_credit", None) or {}


def _build_json_structure(tax_result: TaxResult) -> Dict[str, Any]:
    cr = tax_result.country_result or {}
    items: list = cr.get("items", [])
    netting = cr.get("netting")
    ftc_summary = cr.get("ftc_summary")
    liability = cr.get("liability")
    fx_policy = cr.get("fx_policy")

    # --- Metadata ---
    metadata: Dict[str, Any] = {
        "country": tax_result.country_code,
        "tax_year": tax_result.tax_year,
        "export_version": _EXPORT_VERSION,
        "currency": cr.get("currency", "EUR"),
    }
    if fx_policy is not None:
        metadata["fx_policy"] = {
            "mode": fx_policy.mode.name,
            "source": fx_policy.source,
            "weekend_fallback": fx_policy.weekend_fallback.name,
        }

    # --- Sections summary ---
    sections_data: Dict[str, Any] = {}
    for key, section in tax_result.sections.items():
        sections_data[key] = {
            "label": section.label,
            "line_items": {k: str(v) if isinstance(v, Decimal) else v
                          for k, v in section.line_items.items()},
            "notes": section.notes,
        }

    # --- Items ---
    items_data: List[Dict[str, Any]] = []
    for it in items:
        d = it.to_dict()
        # Attach FTC record if present
        ftc_rec = getattr(it, "ftc_record", None)
        if ftc_rec is not None:
            d["ftc"] = ftc_rec.to_dict()
        items_data.append(d)

    # --- Warnings / pending review (references only, full data in items) ---
    pending_ids = [d["source_event_id"] for d in items_data
                   if d.get("tax_review_status") == "PENDING_MANUAL_REVIEW"]
    unlinked_wht_ids = [d["source_event_id"] for d in items_data
                        if d.get("item_type") == "OTHER"]

    # --- FTC summary ---
    ftc_data: Optional[Dict[str, Any]] = None
    if ftc_summary is not None:
        ftc_data = {
            "foreign_income_total": str(ftc_summary.foreign_income_total_czk),
            "foreign_tax_paid_total": str(ftc_summary.foreign_tax_paid_total_czk),
            "creditable_total": str(ftc_summary.foreign_tax_creditable_total_czk),
            "non_creditable_total": str(ftc_summary.foreign_tax_non_creditable_total_czk),
            "pending_review_total": str(ftc_summary.pending_review_total_czk),
            "item_count": ftc_summary.item_count,
            "pending_count": ftc_summary.pending_review_count,
            # "creditable" is the PRELIMINARY per-item cap; "credited" is what
            # the state actually got after §38f/8 and /1 and is what its
            # separate Příloha 3 sheet shows (these sum to ř. 328).
            # "non_creditable" is tax withheld ABOVE the treaty rate — a refund
            # claim against the source state, never ř. 329; the alias spells
            # that out for readers who would guess from the older name.
            "per_country": {
                code: {
                    "gross_income": str(agg.gross_income_czk),
                    "paid": str(agg.foreign_tax_paid_czk),
                    "creditable": str(agg.creditable_czk),
                    "credited": str(_per_state(liability).get(
                        code, agg.creditable_czk)),
                    "non_creditable": str(agg.non_creditable_czk),
                    "above_treaty_rate": str(agg.non_creditable_czk),
                    "item_count": agg.item_count,
                }
                for code, agg in sorted(ftc_summary.per_country.items())
            },
            "records": [r.to_dict() for r in ftc_summary.records],
        }

    # --- Assemble ---
    return {
        "metadata": metadata,
        "sections": sections_data,
        "cz_10_netting": _netting_to_dict(netting) if netting else None,
        "ftc_summary": ftc_data,
        "items": items_data,
        "warnings": {
            "pending_review_count": len(pending_ids),
            "pending_review_event_ids": pending_ids,
            "unlinked_wht_count": len(unlinked_wht_ids),
            "unlinked_wht_event_ids": unlinked_wht_ids,
        },
    }


def _netting_to_dict(netting: Any) -> Dict[str, Any]:
    """Convert ``CzLossOffsettingResult`` to a JSON-friendly dict."""
    return {
        "securities": {
            "taxable_gains": str(netting.securities.taxable_gains),
            "taxable_losses": str(netting.securities.taxable_losses),
            "net_taxable": str(netting.securities.net_taxable),
            "exempt_time_test": str(netting.securities.exempt_time_test_total),
            "exempt_annual_limit": str(netting.securities.exempt_annual_limit_total),
            "pending": str(netting.securities.pending_total),
            "item_count_total": netting.securities.item_count_total,
            "item_count_taxable": netting.securities.item_count_taxable,
            "item_count_exempt": netting.securities.item_count_exempt,
            "item_count_pending": netting.securities.item_count_pending,
        },
        "options": {
            "taxable_gains": str(netting.options.taxable_gains),
            "taxable_losses": str(netting.options.taxable_losses),
            "net_taxable": str(netting.options.net_taxable),
            "item_count_total": netting.options.item_count_total,
        },
        "combined_net_taxable": str(netting.combined_net_taxable),
        "annual_limit_applied": netting.annual_limit_applied,
        "annual_limit_eligible_proceeds": str(netting.annual_limit_eligible_proceeds),
        "annual_limit_threshold": str(netting.annual_limit_threshold),
    }
