# AI Guidelines

Rules for AI assistants modifying this codebase.

---

## NEVER Do

1. **Add country-specific logic to core.** No tax enums, rates, time tests, or classification rules in `domain/`, `engine/`, `parsers/`, `processing/`, `utils/`.
2. **Recompute tax logic in exporters.** `json_exporter.py` and `xlsx_exporter.py` call `to_dict()` and `to_line_items()`. They never sum, filter by taxability, or apply rates.
3. **Recompute tax logic in form mapping.** `form_mapping.py` reads from `liability`, `netting`, `ftc_summary`. If a value needs `max(0, x)`, that computation belongs in `tax_liability.py`.
4. **Duplicate the category→section mapping.** Single source: `category_to_cz_section()` in `enums.py`.
5. **Count unlinked WHT as income.** Standalone WHT items (`item_type=OTHER`) contribute to `wht_paid` only, never to `gross_dividends`.
6. **Silently drop items.** Exempt, pending, and unlinked WHT items must remain in `items[]`, JSON, XLSX. They may be excluded from `included_in_tax_base` aggregation but never removed from output.
7. **Break the FTC invariant.** `paid = creditable + non_creditable` must hold for every `CzForeignTaxCreditRecord` and for summary totals.
8. **Auto-exempt items with missing data.** Missing `acquisition_date` → `PENDING_MANUAL_REVIEW` + conservatively taxable. Never silently exempt.

## When Modifying CZ Tax Logic

- Work through `CzTaxItem`. Every tax-relevant fact becomes a `CzTaxItem` with full audit trail.
- Preserve: `source_event_id`, `fx` record, `fx_date_used`, `source_country`, `wht_records`, `tax_review_note`.
- New tax rules = new pipeline phase. Insert after the appropriate existing phase. Don't modify an earlier phase's output format.
- If a rule is uncertain, add to `CzTaxConfig` as a configurable flag + `limitation_notes` / `tax_review_note`.

## When Adding a New Feature

1. Create a new module in `countries/cz/` (e.g. `new_rule.py`).
2. Implement as a function that takes `List[CzTaxItem]` + `CzTaxConfig` and modifies items in-place or returns a summary.
3. Call it in `CzechTaxAggregator.aggregate()` at the correct pipeline position.
4. Add tests in `tests/test_cz_new_rule.py`.
5. If the feature adds a new summary, add a `TaxResultSection` and store the result in `country_result`.

## When Working with WHT

- Linked WHT: `CzWhtRecord` inside `CzTaxItem.wht_records` with `source_country`.
- Unlinked WHT: standalone `CzTaxItem(item_type=OTHER, section=CZ_8_DIVIDENDS)` with `wht_records=[self_referencing_record]`.
- WHT is **tax paid**, not income. Never add it to gross income totals.
- If a WHT cannot be linked, it still appears in export. The `tax_review_note` says "Unlinked WHT".

## When Working with FTC

- Preliminary credit: per-item `min(wht_paid, cap_rate × gross_income)`. Lives in `foreign_tax_credit.py`.
- Final credit: `min(preliminary_total, czech_tax_on_foreign_income)`. Lives in `tax_liability.py`.
- `czech_tax_on_foreign_income = gross_tax × (foreign_income / combined_base)` (proportional method).
- Cap rates come from `CzTaxConfig.country_credit_caps` → `default_max_credit_rate`. Never hardcode.

## When Working with Tax Liability

- `tax_liability.py` is the **single source of truth** for:
  - `combined_taxable_base`
  - `gross_czech_tax`
  - `final_creditable_ftc`
  - `final_czech_tax_after_credit`
- Form mapping and exporters read these values. They never recompute them.

## Tests

- Every change must have a test.
- Run `uv run pytest` before committing. Must be zero failures.
- New modules: add `tests/test_cz_{module_name}.py`.
- Edge cases to always test: zero input, missing data, negative amounts, boundary values.

## Change Checklist

Before submitting any change, verify:

- [ ] Does it break separation of concerns? (country logic in core?)
- [ ] Does it duplicate logic? (same computation in two places?)
- [ ] Does it affect auditability? (items lost, FX records missing, notes dropped?)
- [ ] Does it change tax results without a test?
- [ ] Does the FTC invariant still hold? (`paid = creditable + non_creditable`)
- [ ] Are exempt items still in the output?
- [ ] Are pending items conservatively handled?
- [ ] Are unlinked WHT items preserved?
- [ ] Does `uv run pytest` pass with zero failures?
