# Future Work

Last updated: 2026-07-02 — after the 2026-07 calculation audit (35 of 39
findings fixed, see `AUDIT_REPORT_2026-07.md`) and the first end-to-end
synthetic validation run (local `data/synthetic_2024/`: engine output matched
an independently hand-computed expectation on all 13 tracked figures,
including the §38f/8 per-state FTC cap edge).

Ordered by value-for-effort. Within each theme, items are roughly in
recommended execution order.

## 1. Lock in correctness (highest value, lowest effort)

- [x] **Golden dataset → offline pytest regression.** DONE (2026-07-02):
      `tests/test_golden_e2e_cz.py` runs the full pipeline (CSV parsing →
      enrichment → FIFO → CZ aggregation) on the synthetic 2024 scenario
      with pinned real ECB/ČNB rates and asserts the independently
      hand-computed figures (per-leg CZK conversions, time-test exemption,
      100k limit, §38f/8 FTC cap edge 356.33, final tax 3 604 CZK).
      Runs offline; no network.
- [x] **Extend golden scenarios.** DONE (2026-07-03):
      `tests/test_golden_scenarios_cz.py` — six hand-computed scenarios
      pinning assignment with premium-into-stock-basis + pro-rata partial
      consumption (M5; also documents the current M17/M18 mixed-FX-date
      behaviour of the premium component), weekend-dividend ČNB fallback
      audit trail (L9), forward split preserving the acquisition date,
      cash merger (L6), `C;O` flip (M19), and negative net proceeds (L5).
      S3–S5 additionally pin the under-100k annual-limit exemption branch.
- [x] **Run on real statements + reconciliation.** DONE (2026-07-03): full
      2025 run on real exports (trades 2024+2025 concatenated for SOY FIFO
      reconstruction). EOY quantity validation passed; BYDDY 6:1 split,
      `C;O` flip, 2 put assignments (premium→stock basis), 8 expirations,
      WHT reversal netting and IE/SE/US FTC caps all verified against
      hand-computed sums. FOUND & FIXED: "Broker Interest Paid" (margin
      interest) entered §8 as negative income and diluted the FTC income
      base — now a distinct `INTEREST_PAID_DEBIT` event type, excluded from
      CZ §8/FTC with an audit note (`tests/test_cz_margin_interest.py`).
      Remaining: reconcile against IBKR's official Annual Statement PDF.
      M11 still blocked (no cash-in-lieu in the data); L14 partially
      validated (all 11 WHT rows incl. a reversal linked correctly).

## 2. CZ tax-logic gaps (these change the resulting tax)

- [x] **Uniform FX mode ("jednotný kurz").** DONE (2026-07-03):
      `src/countries/cz/uniform_rates.py` ships the official GFŘ tables
      (2020 partial per D-49; 2024 per D-66; 2025 per D-75) with a
      per-leg-year policy for multi-year holdings; `--cz-fx-mode
      daily|uniform|compare` computes either mode or both and reports which
      is cheaper (exports suffixed `.daily`/`.uniform`). Covered by
      `tests/test_cz_uniform_fx.py` (hand-computed golden run: uniform
      3,822 vs daily 3,604 CZK). LIMITATION: §10 disposal legs convert via
      EUR-enriched amounts (daily-ECB leg × uniform EUR/CZK) until the
      M17/M18 per-component data model lands — noted in the compare output.
- [x] **Treaty-by-treaty FTC cap verification.** DONE (2026-07-03):
      `country_credit_caps` now ships 12 verified portfolio-dividend caps
      with treaty citations (US/DE/IE/GB/CH/CA/JP/AU 15 %; NL/FR/AT/LU
      10 % — NL withholds 15 % domestically but only 10 % is creditable).
      LIMITATION (documented in config): one cap per country is applied to
      all WHT; interest caps differ (often 0 %) — review manually if an
      interest WHT row appears.
- [x] **Pre-2014 time test rule.** DONE (2026-07-03): securities acquired
      before 2014-01-01 use the 6-month test (čl. II bod 5 zák. opatření
      č. 344/2013 Sb.) with the ≤5 % direct-share assumption noted on
      items; month-end clamping per §33 daňového řádu; configurable via
      `pre_2014_rule_enabled`.
- [x] **§10/4 expense deduction rules.** RESOLVED as documentation
      (2026-07-03): acquisition costs and trade commissions are already
      reflected in cost basis / net proceeds per item, so no separate
      expense engine is needed for IBKR data; the output note now says
      exactly that and points out that external expenses directly
      attributable to a sale must be added manually.

## 3. Filing-ready outputs

- [x] **Fill in `official_line_ref` in `cz/form_mapping.py`.** DONE
      (2026-07-03): refs verified against the official 2025-period forms
      from financnisprava.gov.cz — DAP 25 5405 vzor č. 30 (ř. 38 §8, ř. 40
      §10, ř. 41–42, ř. 57, ř. 58), Příloha 2 vzor č. 21 (§10 tabulka:
      druh D = cenné papíry, druh F = jiné ostatní příjmy /deriváty/, kód
      "z" pro zahraniční zdroj; ř. 207–209), Příloha 3 vzor č. 21 (§38f
      ř. 321–330, samostatný list za každý stát dle odst. 8; ř. 330 →
      ř. 58 DAP). Pinned by `TestOfficialLineRefs`; re-verify when a new
      form vzor is published.
- [ ] **CZ PDF report.** The PDF generator currently renders only the
      German Anlage-KAP report; CZ has console/JSON/XLSX.
- [ ] **EPO XML export (longer term).** Direct import into the CZ tax
      portal would be the biggest usability win, but requires tracking the
      official form schema across years.

## 4. Ergonomics

- [x] **`--tax-year` CLI flag.** DONE (2026-07-03): `--tax-year N` overrides
      `config.TAX_YEAR` everywhere (pipeline, loss offsetting, CZ
      aggregation, default PDF filename). Together with the existing file
      path flags, a full run no longer requires editing `src/config.py`
      (only cosmetic PDF fields TAXPAYER_NAME/ACCOUNT_ID remain there).
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
