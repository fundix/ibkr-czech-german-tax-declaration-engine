# Future Work

Last updated: 2026-07-02 — after the 2026-07 calculation audit (35 of 39
findings fixed, see `AUDIT_REPORT_2026-07.md`) and the first end-to-end
synthetic validation run (local `data/synthetic_2024/`: engine output matched
an independently hand-computed expectation on all 13 tracked figures,
including the §38f/8 per-state FTC cap edge).

Ordered by value-for-effort. Within each theme, items are roughly in
recommended execution order.

## 1. Lock in correctness (highest value, lowest effort)

- [ ] **Golden dataset → offline pytest regression.** Convert the synthetic
      end-to-end scenario (`data/synthetic_2024/`, currently local-only and
      network-dependent) into a pytest test using the mock FX providers in
      `tests/support/mock_providers.py`, with the hand-computed expected
      values pinned. Runs in CI without network; locks the audit fixes
      against regression.
- [ ] **Extend golden scenarios** to audit-fixed mechanics not yet covered
      end-to-end: option exercise/assignment with premium folded into stock
      basis, partial fills (M5), weekend/holiday dividend (rate fallback +
      `conversion_note`, L9), splits/mergers, `C;O` position flip (M19),
      negative net proceeds (L5).
- [ ] **Run on real statements + reconciliation.** Process the user's real
      2024/2025 IBKR exports and reconcile against IBKR's own annual
      statements. Also unblocks M11 and L14 (both waiting on real data —
      see "Open audit findings" below).

## 2. CZ tax-logic gaps (these change the resulting tax)

- [ ] **Uniform FX mode ("jednotný kurz", GFŘ D-59).** Currently raises
      `NotImplementedError` (`CzFxMode.UNIFORM`). Most useful shape: compute
      BOTH daily and uniform modes in one run and report which one yields
      the lower tax — the taxpayer may legally choose (one mode per year,
      no mixing; the plugin already enforces mode consistency).
- [ ] **Treaty-by-treaty FTC cap verification.** `country_credit_caps` in
      `src/countries/cz/config.py` are placeholders (US/DE/IE/GB flat 15 %).
      Verify against the actual SZDZ at least for states that appear in the
      user's data; extend the table.
- [ ] **Pre-2014 time test rule** (6-month test for securities acquired
      before 2014-01-01). Only relevant for very old positions.
- [ ] **§10/4 expense deduction rules.** The export currently carries the
      note "PLACEHOLDER: expense deduction rules (§10/4 ZDP) not applied".

## 3. Filing-ready outputs

- [ ] **Fill in `official_line_ref` in `cz/form_mapping.py`.** Verify the
      stable internal line codes against the current DAP form: ř. 38 (§8),
      Příloha 2 (§10), Příloha 3 + per-state "Seznam" for §38f.
- [ ] **CZ PDF report.** The PDF generator currently renders only the
      German Anlage-KAP report; CZ has console/JSON/XLSX.
- [ ] **EPO XML export (longer term).** Direct import into the CZ tax
      portal would be the biggest usability win, but requires tracking the
      official form schema across years.

## 4. Ergonomics

- [ ] **`--tax-year` CLI flag** and runtime configuration outside
      `src/config.py` (the file is gitignored and edited in place — fragile
      across updates).
- [ ] **Direct IBKR Flex Query download** (token + query ID) instead of
      manual CSV export.
- [ ] **PENDING / manual-review checklist as a first-class output** —
      dedicated XLSX sheet and/or non-zero exit code, so flagged items
      cannot be overlooked.
- [ ] UI / web interface (low priority).

## 5. Documentation

- [ ] DE plugin documentation
- [ ] API reference

## Open audit findings (AUDIT_REPORT_2026-07, awaiting design/data)

- **M11 — cash-in-lieu for split fractions**: a reverse split leaving a
  fraction keeps it in the ledger forever; the CIL cash is never taxed and
  the fraction's cost basis is lost. Needs a sample of how CIL appears in
  real IBKR data (cash transaction row vs. corporate action detail) to wire
  the fraction disposal.
- **M17/M18 — mixed FX dates on premium/repayment components**: an option
  premium folded into the stock basis carries the ECB rate of the option's
  opening day but is converted to CZK at the stock trade date (capital
  repayments analogously). Error ∝ component size × EUR/CZK drift between
  the dates. Proper fix = per-component cash-flow dates on RealizedGainLoss.
- **L14 — WHT linker tolerances**: amount-relationship windows are generous
  (up to 100 % of income in the proximity strategy); tightening should be
  validated against real statements to avoid breaking legitimate links.
