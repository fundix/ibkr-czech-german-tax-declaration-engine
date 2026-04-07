# Engineering Decisions

## Architecture: Why Core vs Country Plugins

- Core (`domain/`, `engine/`, `parsers/`, `processing/`, `utils/`) handles **economic facts** — FIFO, FX conversion, event parsing, lot accounting.
- Country plugins (`countries/de/`, `countries/cz/`) handle **tax interpretation** — what's taxable, what rate, what form line.
- **Reason:** Same IBKR trade data produces different tax outcomes in different jurisdictions. The economic fact (you sold 100 shares at price X) is universal; the tax classification (Anlage KAP vs §10 ZDP) is not.
- Core accepts an injectable `tax_classifier` callback. Country plugins provide the implementation. Core never imports country-specific enums.

## Why FX Policy Is Not in Core

- Core enrichment converts everything to EUR (ECB rates) — this is the German convention.
- CZ plugin needs CZK (ČNB rates) with **per-event** conversion and **direct** foreign→CZK path (not via EUR intermediate).
- FX policy (which provider, which rate date, which fallback rule) is a country-level decision.
- `CzCurrencyConverter` lives in `countries/cz/fx_policy.py`, not in `utils/`.
- `CNBExchangeRateProvider` is in `utils/` because it's a general-purpose FX provider usable by any country that needs CZK rates.

## Why Form Mapping Must Not Compute

- `form_mapping.py` reads from `CzTaxLiabilitySummary`, `CzLossOffsettingResult`, `CzForeignTaxCreditSummary`.
- It maps pre-computed values to DAP-oriented line codes.
- **Invariant:** If you change `form_mapping.py`, no tax number should change. If a tax number is wrong, the fix belongs in the upstream module that computed it.
- **Reason:** Auditability. A tax advisor can verify the liability computation independently from the form layout.

## Why Exporters Only Read Data

- `json_exporter.py` and `xlsx_exporter.py` call `to_dict()` / `to_line_items()` on existing models.
- They never call tax computation functions, never filter items by taxability, never sum amounts.
- **Reason:** Export bugs must never change tax figures. Tax bugs must never require export changes.

## CZ Pipeline Order

```
1. build_tax_items()        → CzTaxItem list (FX conversion + WHT linking)
2. evaluate_time_test()     → sets is_taxable / is_exempt / holding_period
3. evaluate_annual_limit()  → sets exempt_due_to_annual_limit
4. compute_loss_offsetting() → CzLossOffsettingResult (gains - losses)
5. evaluate_foreign_tax_credit() → CzForeignTaxCreditSummary (per-item caps)
6. compute_tax_liability()  → CzTaxLiabilitySummary (rates + FTC finalization)
7. build_form_mapping()     → CzFormMappingResult (DAP-oriented codes)
8. export_cz_to_json/xlsx() → files
```

Each step reads the previous step's output. No step modifies data from two steps back.

## Why Per-Item Processing

- Every `CzTaxItem` carries: source event ID, asset info, dates, original amounts, CZK amounts, FX record, WHT records, taxability fields, FTC record.
- **Reason:** When a tax number looks wrong, you can trace it to exactly one item, one FX conversion, one WHT record, one classification decision.
- Aggregate-first designs lose this traceability.

## Key Invariants

1. **Core contains zero country-specific logic.** No `TaxReportingCategory` usage in `fifo_manager.py` (extracted to classifier callback). No `Teilfreistellung` in `RealizedGainLoss.__post_init__`.
2. **No business logic in exporters.** Exporters serialize; they don't compute.
3. **Form mapping only reads.** Uses `liability.taxable_securities_net`, never `max(0, netting.securities.net_taxable)` — that computation belongs in `tax_liability.py`.
4. **FTC invariant:** `paid = creditable + non_creditable` for every record and for summary totals.
5. **Exempt items stay in output.** They are never removed from `items[]` list, XLSX sheets, or JSON. They are excluded from `included_in_tax_base` aggregation.
6. **Unlinked WHT never disappears.** Creates standalone `CzTaxItem(item_type=OTHER)` with `tax_review_note`. Visible in WithholdingTax sheet, JSON warnings.
7. **Unlinked WHT is not income.** Plugin aggregator skips `OTHER` items from `gross_dividends` sum; only adds their WHT to `wht_paid`.
8. **Pending items are conservatively included.** `included_in_tax_base=True` by default when `PENDING_MANUAL_REVIEW`.
9. **Negative §10 net is floored at zero.** Loss carryforward not implemented. Explicit note in `limitation_notes`.
10. **Elevated-rate threshold is IBKR-only.** Real threshold applies to total taxpayer income. Explicit limitation note in every output.
