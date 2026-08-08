# Czech Republic Plugin (CZ)

## Overview

The CZ plugin (`src/countries/cz/`) computes Czech personal income tax figures from IBKR data. It produces audit-friendly output suitable as supporting documentation for the Czech tax return (Přiznání k dani z příjmů fyzických osob).

> **This is not an official tax return.** Output must be verified by a tax professional before filing.

## What It Does

### Processing Pipeline

```
IBKR data → Core FIFO/enrichment → CzTaxItems
  → Time test (§4/1/u)
  → Annual exempt limit (100k CZK)
  → §10 loss offsetting
  → Foreign tax credit (§38f)
  → Tax liability (15%/23%)
  → Form mapping (DAP-oriented)
  → JSON/XLSX export
```

### Income Classification

| IBKR Event | CZ Bucket | Tax Section |
|-----------|-----------|-------------|
| Dividend (DIVIDEND_CASH) | CZ_8_DIVIDENDS | §8 ZDP |
| Fund distribution | CZ_8_DIVIDENDS | §8 ZDP |
| Interest | CZ_8_INTEREST | §8 ZDP |
| Stock/bond/ETF sale | CZ_10_SECURITIES | §10 ZDP |
| Option close/expiry | CZ_10_OPTIONS | §10 ZDP |

### FX Conversion

- **Default:** Daily ČNB rates (`CnbFxProvider`)
- **Method:** Per-event, direct foreign→CZK (not through EUR as intermediate)
- **Disposals:** acquisition cost (výdaj) converted at the **acquisition-date** rate, sale proceeds (příjem) at the **disposal-date** rate — so the currency movement between purchase and sale is reflected in the §10 gain (per NSS judgment 2 Afs 4/2019-35; a single sale-date rate for both legs is *not* used)
- **Fallback:** Last valid rate for weekends/holidays
- Every conversion produces an `FxConversionRecord` with full audit trail
- If a ČNB rate cannot be obtained, the CZK amount is left empty and the item is flagged `PENDING_MANUAL_REVIEW` (the un-converted foreign amount is never treated as CZK)

### Time Test (§4/1/u ZDP)

Securities held longer than 3 years (1095 days) are exempt. Applied to `SECURITY_DISPOSAL` items only — not to dividends, interest, or options. Securities acquired before 2014-01-01 keep the transitional 6-month test instead (čl. II bod 5, 344/2013 Sb.).

Two dates decide two different things, and they are separate fields: `acquisition_date` picks the regime (pre-2014 or not) and the FIFO consumption order, while `holding_period_start` measures the period. They coincide in the common case, which is exactly why the distinction is easy to lose — a merger carry-over is where they part. See `docs/cz-tax-policy.md`.

If `acquisition_date` is missing, the item is marked `PENDING_MANUAL_REVIEW` and conservatively included in the tax base.

### Annual Exempt Limit (100k CZK)

If total gross disposal proceeds (`proceeds_czk`) for eligible security disposals do not exceed CZK 100,000, those items are exempt (2025+ amendment).

- Uses `proceeds_czk` (gross proceeds), not gain/loss
- **ALL** security disposals count toward the sum, time-test-exempt ones included.
  The threshold is tested on the year's total gross proceeds from transfers, and
  only then does the title flip the still-taxable items. The two exemption
  titles cannot be combined; netting the exempt sales out of the sum under-taxed
  the year (corrected per advisor 2026-08-05)
- Options are not eligible
- All-or-nothing: if total exceeds threshold, ALL eligible items are taxable

### Loss Offsetting (§10)

Taxable gains and losses are netted separately for:
- Securities (stocks, bonds, ETFs)
- Options (derivatives)

Only items with `included_in_tax_base=True` participate. Exempt losses do not reduce the tax base. Negative net results are floored at zero (loss carryforward not implemented).

### Pairing Method (§10 lot matching)

A private (non-business) investor may choose which purchase lot is matched to
each sale of fungible securities — the choice changes both the acquisition cost
and the time-test result. `--cz-pairing-method` (web GUI selector; MCP
`run_pipeline(pairing_method=…)`):

- **`fifo`** (default) — oldest lots first; unchanged behaviour.
- **`lifo`** — newest lots first.
- **`weighted_average`** — every disposed unit is costed at the blended pool
  average (vážený aritmetický průměr); the deemed-sold lot identity (dates →
  time test) stays FIFO, and surviving lots are re-priced to the average so the
  moving average stays consistent.
- **`optimal`** — a global tax-minimising min-cost-flow solver
  (`src/engine/pairing_solver.py`, per asset) that routes gains onto time-test-
  exempt lots and losses onto taxable lots to minimise the §10 taxable base.
- **`compare`** (CLI only) — scores the full FX-mode × method matrix by *final*
  tax (after time test + 100k limit + netting + rates) and reports the cheapest.

The method must be applied consistently for the whole year; the tool only
*recommends* the cheapest — the filer/advisor chooses. Every method is scored by
the real aggregator, so `optimal` is never worse than FIFO.

### Foreign Tax Credit (§38f ZDP)

Per-item preliminary credit:
```
cap_rate = country_credit_caps.get(country, default_max_credit_rate)
max_creditable = gross_income × cap_rate
actual_creditable = min(wht_paid, max_creditable)
```

Final credit (after liability computation):
```
czech_tax_on_foreign = gross_tax × (foreign_income / combined_base)
final_creditable = min(preliminary_creditable, czech_tax_on_foreign)
```

Under §38f odst. 8 the credit is computed **per state on its own sheet** of
Příloha 3; `per_state_credit` holds those figures and they sum to ř. 328.

What did not get credited splits in two, because the two have opposite
consequences and only one of them belongs on the return:

| Field | Meaning | Where it goes |
|---|---|---|
| `treaty_excess_ftc` | withheld **above** the treaty rate (`paid − preliminary`) | **Not on the return.** §38f odst. 5 counts foreign tax only up to the treaty rate — this is a refund claim against the source state, and usually signals a missing form (W-8BEN for US) |
| `uncredited_ftc` | treaty-eligible credit lost to the §38f/1 proportional or §38f/8 per-state cap | **Příloha 3, ř. 329**; deductible as an expense under §24 odst. 2 písm. ch) in the following period |

Mapping the *total* onto ř. 329 contradicts that line's own definition (ř. 323 −
ř. 328) and would carry over-withheld foreign tax forward as if it were a Czech
credit.

### Tax Liability

```
combined_base = dividends + interest + max(0, securities_net) + max(0, options_net)
tax = base_portion × 15% + elevated_portion × 23%
final_tax = gross_tax - final_creditable_ftc
```

### Form Mapping

DAP-oriented output with stable internal line codes (e.g. `CZ_DAP_8_DIVIDENDS`, `CZ_DAP_10_SECURITIES`). Does not generate official form — serves as structured input for manual filing or future automation.

## Known Limitations

| Area | Status | Detail |
|------|--------|--------|
| Treaty verification | Verified (2026-07, extended 2026-08) | `country_credit_caps` ship 16 verified portfolio-dividend caps with Sb. citations (NL/FR/AT/LU are 10 %, not 15 %; SE/CN/KZ 10 %, HK 5 %). A country **absent** from the dict falls back to the 15 % default, which credits too much wherever the real treaty cap is lower — the FTC record carries `cap_rate_defaulted` and the dividends page marks those rows. One cap per country applies to all WHT — interest caps differ (often 0 %); review interest WHT rows manually |
| Jednotný kurz (uniform rate) | Implemented (2026-07) | `--cz-fx-mode uniform` uses the GFŘ uniform rates (`uniform_rates.py`, pokyny D-49/D-66/D-75 transcribed); `--cz-fx-mode compare` computes both modes and reports the cheaper one. §10 disposal legs convert via the EUR-enriched amounts (approximation until per-leg original-currency data exists — M17/M18) |
| Pairing method (§10) | Implemented (2026-07) | `--cz-pairing-method fifo/lifo/weighted_average/optimal/compare` (`pairing.py`, `pairing_solver.py`). `optimal` solver covers long securities only — options, shorts, and assets with a mid-year corp action / capital repayment stay FIFO; exact for base+rates, near-optimal at the 100k all-or-nothing cliff (every method is scored by the real aggregator, so never worse than FIFO). Web GUI offers single methods; the full FX×method matrix is CLI-only |
| Pre-2014 acquisition rule | Implemented (2026-07) | Securities acquired before 2014-01-01 use the 6-month test (čl. II bod 5, 344/2013 Sb.); assumes direct issuer share ≤ 5 % (noted on items) |
| Expense deduction (§10/4) | Documented | Acquisition costs and commissions are already in cost basis / net proceeds; external sale-related expenses must be added manually (see §10 section note) |
| Loss carryforward | Not implemented | Negative §10 net floored at zero |
| Multi-source taxpayer | Limitation | Elevated-rate threshold applies to IBKR income only; adjust if other income exists |
| EUR intermediate on RGL | Known | Disposal amounts go EUR→CZK (core converts to EUR first) |
| Official form line numbers | Verified (2026-07) | `official_line_ref` cites DAP vzor č. 30 and Příloha 2/3 vzor č. 21 (period 2025); re-verify when a new vzor is published. The official form itself (PDF vzor / EPO XML) is still not generated |
| Stock-for-stock mergers | Implemented (2026-08) | Both regimes, chosen per event in `cache/merger_treatments.json`: §23b/§23c carry-over, or a taxable disposal at the consideration's fair value. The statement cannot tell them apart — what decides is the parties' residence and legal form — so an unclassified merger **refuses the run** rather than guessing. Refused cases: cash in lieu of a fractional share, a prior-year merger replayed through SOY, a short position on the disposing side. See `docs/cz-tax-policy.md` |
| Currency conversions (§10 FX gain) | Flagged only | Each disposal of a foreign currency becomes a `CURRENCY_CONVERSION` item in `CZ_10_CURRENCY` marked `PENDING_MANUAL_REVIEW`, outside the tax base. The gain itself needs a FIFO over cash balances — `statement_of_funds_parser.py` reads them, nothing consumes it yet |

## Configuration

All CZ-specific settings are in `CzTaxConfig` (`src/countries/cz/config.py`):

```python
CzTaxConfig(
    home_currency="CZK",
    base_tax_rate=Decimal("0.15"),
    elevated_tax_rate=Decimal("0.23"),
    elevated_rate_threshold_czk=Decimal("1935552"),
    time_test_enabled=True,
    holding_test_years=3,
    annual_exempt_limit_enabled=True,
    annual_exempt_limit_czk=Decimal("100000"),
    foreign_tax_credit_enabled=True,
    default_max_credit_rate=Decimal("0.15"),
    country_credit_caps={"US": Decimal("0.15"), ...},
)
```

## Exports

### JSON
```python
from src.countries.cz.exporters import export_cz_to_json
json_str = export_cz_to_json(tax_result, output="report.json")
```

### XLSX
```python
from src.countries.cz.exporters import export_cz_to_xlsx
export_cz_to_xlsx(tax_result, "report.xlsx")
```

XLSX sheets: Summary, Securities, Options, Dividends, Interest, WithholdingTax, PendingReview, Metadata.

### PDF
```python
from src.countries.cz.exporters import export_cz_to_pdf
export_cz_to_pdf(tax_result, "report.pdf", taxpayer_name="…", account_id="…")
```

A filing-support report ("podklady pro DAP", Czech): DAP form-mapping
tables with official line references, §10 netting overview, §38f
per-country table, item detail tables, pending-review list and all
limitation notes. CLI: `--output-pdf report.pdf` (in `--cz-fx-mode compare`
both modes are written as `report.daily.pdf` / `report.uniform.pdf`).
Czech diacritics are rendered with the vendored DejaVu Sans fonts
(`exporters/fonts/`); if the font files are missing the exporter falls
back to Helvetica and strips diacritics.

## Policy Assumptions

These are explicitly documented in the code and output:

1. **Elevated rate threshold** applies to total taxpayer income. This tool only sees IBKR income.
2. **FTC proportional method** (§38f/1): `czech_tax × (foreign_income / total_base)`.
3. **Treaty caps** are configurable. The 16 countries in `country_credit_caps` were
   verified against the SZDZ texts with Sb. citations; anything outside that dict
   falls back to the 15 % default and is flagged `cap_rate_defaulted`.
4. **Annual limit** uses `proceeds_czk` (gross disposal proceeds), matching the legislative term "příjem".
5. **Time test** uses simple day count (holding_period_days > 3×365), not calendar-year boundary logic.
