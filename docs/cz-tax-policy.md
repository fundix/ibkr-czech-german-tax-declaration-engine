# CZ Tax Policy Reference

Definitive reference for Czech tax rules as implemented in this project. Each rule is marked:
- **IMPLEMENTED** — fully functional with tests
- **PARTIAL** — functional but with known simplifications
- **NOT IMPLEMENTED** — architecture prepared, logic missing

---

## FX Policy

| Rule | Status |
|------|--------|
| Daily ČNB rate as default | **IMPLEMENTED** |
| Per-event conversion (not aggregate) | **IMPLEMENTED** |
| Direct foreign→CZK (not via EUR intermediate) for §8 events | **IMPLEMENTED** |
| EUR→CZK for RGL disposals (core pipeline converts to EUR first) | **PARTIAL** — EUR intermediate unavoidable for disposal amounts |
| Weekend/holiday fallback to last valid rate | **IMPLEMENTED** — `max_fallback_days=7` |
| Jednotný kurz (annual uniform rate) | **NOT IMPLEMENTED** — `CzFxMode.UNIFORM` raises `NotImplementedError` |
| FxConversionRecord audit trail on every conversion | **IMPLEMENTED** |

---

## §8 ZDP — Příjmy z kapitálového majetku

| Rule | Status |
|------|--------|
| Dividends (DIVIDEND_CASH) → CZ_8_DIVIDENDS | **IMPLEMENTED** |
| Fund distributions → CZ_8_DIVIDENDS | **IMPLEMENTED** |
| Interest (INTEREST_RECEIVED) → CZ_8_INTEREST | **IMPLEMENTED** |
| §8 income is always taxable (no time test, no annual limit) | **IMPLEMENTED** |
| WHT linked to parent dividend/interest via event ID, asset+date, or ±3 day proximity | **IMPLEMENTED** |
| Unlinked WHT → standalone item (item_type=OTHER), NOT counted as income | **IMPLEMENTED** |

---

## §10 ZDP — Ostatní příjmy

| Rule | Status |
|------|--------|
| Stocks, bonds, ETFs → CZ_10_SECURITIES | **IMPLEMENTED** |
| Options, CFDs → CZ_10_OPTIONS | **IMPLEMENTED** |
| Options are derivative instruments, NOT securities under §4/1/w | **IMPLEMENTED** — no time test for options |
| PrivateSaleAsset → CZ_10_SECURITIES | **IMPLEMENTED** |

---

## Time Test (§4/1/w ZDP)

| Rule | Status |
|------|--------|
| Securities held > 3 years (1095 days) → exempt | **IMPLEMENTED** |
| Threshold: `holding_period_days > holding_test_years × 365` (strict >) | **IMPLEMENTED** |
| Missing acquisition_date → PENDING_MANUAL_REVIEW, conservatively taxable | **IMPLEMENTED** |
| Unparseable dates → PENDING_MANUAL_REVIEW | **IMPLEMENTED** |
| Holding period computed from acquisition_date and event_date when not preset | **IMPLEMENTED** |
| Time test configurable (enable/disable, custom years) | **IMPLEMENTED** |
| Pre-2014 acquisition rule (6-month test) | **NOT IMPLEMENTED** |
| Fund-specific time test rules | **NOT IMPLEMENTED** |

---

## Annual Exempt Limit (2025+ Amendment)

| Rule | Status |
|------|--------|
| Threshold: CZK 100,000 of gross disposal proceeds | **IMPLEMENTED** |
| Metric: `proceeds_czk` (gross proceeds), NOT gain/loss | **IMPLEMENTED** |
| Applies only to SECURITY_DISPOSAL items | **IMPLEMENTED** |
| Options NOT eligible | **IMPLEMENTED** |
| Dividends/interest NOT eligible | **IMPLEMENTED** |
| Time-test-exempt items excluded from proceeds sum | **IMPLEMENTED** |
| All-or-nothing: if total exceeds threshold, ALL eligible items taxable | **IMPLEMENTED** |
| Items without `proceeds_czk` (no FX converter) excluded from test | **IMPLEMENTED** |
| Configurable (enable/disable, custom threshold) | **IMPLEMENTED** |

---

## Loss Offsetting (§10)

| Rule | Status |
|------|--------|
| Securities gains/losses netted separately | **IMPLEMENTED** |
| Options gains/losses netted separately | **IMPLEMENTED** |
| Combined §10 net = securities net + options net | **IMPLEMENTED** |
| Only `included_in_tax_base=True` items participate | **IMPLEMENTED** |
| Exempt losses do NOT reduce tax base | **IMPLEMENTED** |
| Pending items conservatively included with warning | **IMPLEMENTED** |
| Negative net floored at zero for tax base | **IMPLEMENTED** |
| Loss carryforward | **NOT IMPLEMENTED** |
| Expense deduction (§10/4 ZDP) | **NOT IMPLEMENTED** — `cost_basis_czk` on items, rule not applied |

---

## Foreign Tax Credit (§38f ZDP)

| Rule | Status |
|------|--------|
| Per-item preliminary cap: `min(wht_paid, cap_rate × gross_income)` | **IMPLEMENTED** |
| Default cap rate: 15% (`default_max_credit_rate`) | **IMPLEMENTED** |
| Per-country treaty cap: `country_credit_caps` dict | **PARTIAL** — placeholder values, not treaty-verified |
| Missing source_country → PENDING_MANUAL_REVIEW, default cap applied | **IMPLEMENTED** |
| No linked WHT → zero credit record, no crash | **IMPLEMENTED** |
| Multiple WHT records on one item: first source_country used for cap | **IMPLEMENTED** |
| FTC invariant: `paid = creditable + non_creditable` | **IMPLEMENTED** |
| Final FTC = `min(preliminary, czech_tax_on_foreign_income)` | **IMPLEMENTED** |
| Czech tax on foreign income: proportional method `gross_tax × (foreign_income / combined_base)` | **IMPLEMENTED** |
| Per-country FTC aggregation in summary | **IMPLEMENTED** |
| Treaty-by-treaty verification of cap rates | **NOT IMPLEMENTED** |

---

## Tax Liability (§16 ZDP)

| Rule | Status |
|------|--------|
| Base rate: 15% | **IMPLEMENTED** — configurable |
| Elevated rate: 23% above threshold | **IMPLEMENTED** — configurable |
| Default threshold: CZK 1,935,552 (2024 value) | **IMPLEMENTED** — configurable |
| Combined base = dividends + interest + max(0, sec_net) + max(0, opt_net) | **IMPLEMENTED** |
| FTC finalization against CZ tax on foreign income | **IMPLEMENTED** |
| `final_tax = gross_tax - final_creditable_ftc` | **IMPLEMENTED** |
| Threshold applies to TOTAL taxpayer income (not just IBKR) | **PARTIAL** — limitation note: IBKR-only view |
| Solidarity surcharge | **NOT IMPLEMENTED** |
| Sparer-Pauschbetrag equivalent | **NOT IMPLEMENTED** (CZ has no equivalent) |

---

## Form Mapping

| Rule | Status |
|------|--------|
| DAP-oriented line codes (CZ_DAP_8_*, CZ_DAP_10_*, etc.) | **IMPLEMENTED** |
| No recomputation in form mapping layer | **IMPLEMENTED** |
| Official form line references (ř. XX) | **NOT IMPLEMENTED** — `official_line_ref=None` |
| PDF/XML DAP generation | **NOT IMPLEMENTED** |
