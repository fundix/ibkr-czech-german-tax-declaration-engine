# Czech Republic Plugin (CZ)

## Overview

The CZ plugin (`src/countries/cz/`) computes Czech personal income tax figures from IBKR data. It produces audit-friendly output suitable as supporting documentation for the Czech tax return (Přiznání k dani z příjmů fyzických osob).

> **This is not an official tax return.** Output must be verified by a tax professional before filing.

## What It Does

### Processing Pipeline

```
IBKR data → Core FIFO/enrichment → CzTaxItems
  → Time test (§4/1/w)
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

### Time Test (§4/1/w ZDP)

Securities held longer than 3 years (1095 days) are exempt. Applied to `SECURITY_DISPOSAL` items only — not to dividends, interest, or options.

If `acquisition_date` is missing, the item is marked `PENDING_MANUAL_REVIEW` and conservatively included in the tax base.

### Annual Exempt Limit (100k CZK)

If total gross disposal proceeds (`proceeds_czk`) for eligible security disposals do not exceed CZK 100,000, those items are exempt (2025+ amendment).

- Uses `proceeds_czk` (gross proceeds), not gain/loss
- Items already exempt by time test are excluded from the proceeds sum
- Options are not eligible
- All-or-nothing: if total exceeds threshold, ALL eligible items are taxable

### Loss Offsetting (§10)

Taxable gains and losses are netted separately for:
- Securities (stocks, bonds, ETFs)
- Options (derivatives)

Only items with `included_in_tax_base=True` participate. Exempt losses do not reduce the tax base. Negative net results are floored at zero (loss carryforward not implemented).

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
| Treaty verification | Verified (2026-07) | `country_credit_caps` ship 12 verified portfolio-dividend caps with Sb. citations (NL/FR/AT/LU are 10 %, not 15 %). One cap per country applies to all WHT — interest caps differ (often 0 %); review interest WHT rows manually |
| Jednotný kurz (uniform rate) | Implemented (2026-07) | `--cz-fx-mode uniform` uses the GFŘ uniform rates (`uniform_rates.py`, pokyny D-49/D-66/D-75 transcribed); `--cz-fx-mode compare` computes both modes and reports the cheaper one. §10 disposal legs convert via the EUR-enriched amounts (approximation until per-leg original-currency data exists — M17/M18) |
| Pre-2014 acquisition rule | Implemented (2026-07) | Securities acquired before 2014-01-01 use the 6-month test (čl. II bod 5, 344/2013 Sb.); assumes direct issuer share ≤ 5 % (noted on items) |
| Expense deduction (§10/4) | Documented | Acquisition costs and commissions are already in cost basis / net proceeds; external sale-related expenses must be added manually (see §10 section note) |
| Loss carryforward | Not implemented | Negative §10 net floored at zero |
| Multi-source taxpayer | Limitation | Elevated-rate threshold applies to IBKR income only; adjust if other income exists |
| EUR intermediate on RGL | Known | Disposal amounts go EUR→CZK (core converts to EUR first) |
| Official form line numbers | None | `official_line_ref` is `None` on all form lines |
| Stock-for-stock mergers | Core limitation | `CORP_MERGER_STOCK` FIFO logic not fully implemented |

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
3. **Treaty caps** are configurable placeholders — not verified against specific SZDZ texts.
4. **Annual limit** uses `proceeds_czk` (gross disposal proceeds), matching the legislative term "příjem".
5. **Time test** uses simple day count (holding_period_days > 3×365), not calendar-year boundary logic.
