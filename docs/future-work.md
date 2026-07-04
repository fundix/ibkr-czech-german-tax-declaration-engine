# Future Work

Last updated: 2026-07-04 — added section 5 (feature-parity gaps vs.
taxomat.cz) and implemented 5a's pairing-method axis (FIFO / LIFO /
weighted-average / optimal solver + FX×method compare matrix). Previously
2026-07-02, after the 2026-07 calculation audit
(35 of 39 findings fixed, see `AUDIT_REPORT_2026-07.md`) and the first
end-to-end synthetic validation run (local `data/synthetic_2024/`: engine
output matched an independently hand-computed expectation on all 13 tracked
figures, including the §38f/8 per-state FTC cap edge).

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
- [x] **CZ PDF report.** DONE (2026-07-03):
      `src/countries/cz/exporters/pdf_exporter.py` renders a filing-support
      report (podklady pro DAP): form-mapping tables with verified official
      line refs, §10 netting, §38f per-country table, item detail tables
      (disposals / options / dividends+interest), pending-review list and
      all limitation notes. Czech diacritics via vendored DejaVu Sans
      (reportlab's built-in Helvetica lacks ě/ř/ů). CLI `--output-pdf`
      (mode-suffixed in compare mode), web GUI exports `result.<mode>.pdf`
      per run with a download link. Covered by `tests/test_cz_pdf_exporter.py`
      (pymupdf text extraction).
- [ ] **EPO XML export (longer term).** Direct import into the CZ tax
      portal would be the biggest usability win, but requires tracking the
      official form schema across years.

## 4. Ergonomics

- [x] **`--tax-year` CLI flag.** DONE (2026-07-03): `--tax-year N` overrides
      `config.TAX_YEAR` everywhere (pipeline, loss offsetting, CZ
      aggregation, default PDF filename). Together with the existing file
      path flags, a full run no longer requires editing `src/config.py`
      (only cosmetic PDF fields TAXPAYER_NAME/ACCOUNT_ID remain there).
- [x] **Direct IBKR Flex Query download.** DONE (2026-07-03): Flex Web
      Service client (`src/webapp/ibkr_flex.py` — SendRequest →
      ReferenceCode → GetStatement with 1019 polling and friendly error
      hints incl. token expiry). Token + the 4 query IDs configured on the
      Files page (stored in gitignored `data/webapp/ibkr_flex.json`);
      queries must be set to Year-to-Date (positions: Last Business Day).
      One job downloads all statements and recomputes; dashboard button +
      auto-fetch on open when data are older than 12 h; MCP tool
      `refresh_data`. For the RUNNING year the downloaded positions serve
      as positions_end ("state as of the last business day") — the engine
      validates FIFO against current holdings and the tax summary is a
      running estimate (badge in the GUI).
- [x] **API-only bootstrap (empty calculator).** DONE (2026-07-03): a fresh
      install fills itself via the Flex Web Service without manual exports.
      (1) Lot-level SOY: the positions query's "Lot" level of detail
      (`LevelOfDetail=LOT` rows with `OpenDateTime`) feeds
      `Asset.soy_lots`; when trade-history reconstruction can't cover the
      reported SOY position, `FifoLedger` seeds per-lot FifoLots with REAL
      acquisition dates (`SOY_SNAPSHOT_*` ids — time test works, no
      "odhad" badge) instead of one estimated 31 Dec lot; any snapshot
      inconsistency falls back to the old behaviour. (2) Historical
      backfill via the documented `fd`/`td` period override on the SAME
      queries: with "first trading year" configured, a fetch fills every
      missing older dataset year (one calendar-year window per request;
      old-year positions = 31 Dec snapshot). Verified live against real
      data: full 2024 and 2025 statements fetched in 2026 were
      byte-identical (TransactionID sets) to the manual exports — the
      365-day figure limits the request WINDOW, not history depth. The
      original "Last Calendar Year queries" design was replaced by the
      override before first use.
- [x] **PENDING / manual-review checklist as a first-class output** —
      RESOLVED at the GUI level (2026-07-03): the web GUI's "Ke kontrole"
      page lists PENDING items + section REVIEW notes with a nav badge.
      (A non-zero exit code for the CLI remains open — deliberate, since
      it would make automated runs look failed; revisit if CLI automation
      appears.)
- [x] **UI / web interface.** Phase 1 DONE (2026-07-03): local FastAPI +
      HTMX app (`uv run --extra web python -m src.webapp`) — upload per-year
      CSVs, run daily/uniform/compare, browse summary/items/DAP-lines/review
      checklist, download exports. Phase 3 DONE (2026-07-03): portfolio view
      (EOY open FIFO lots via `ProcessingOutput.fifo_ledgers_by_asset_id`,
      per-lot §4/1/w time-test countdown via pure `time_test_deadline()`,
      EOY valuation, dividend overview per asset/month). Phase 4 DONE
      (2026-07-03): live quotes (Yahoo via requests, symbol_map.json
      overrides), unrealized P/L in CZK, allocation + value-history charts
      (vendored Chart.js, SQLite snapshots), sale simulator with FIFO
      preview, time-test split, annual-limit interplay and wait-hint.
      Phase 5 DONE (2026-07-03): MCP server (`uv run --extra mcp python -m
      src.mcp_server`, stdio) — nine tools over the shared service layer;
      registered via `claude mcp add ibkr-tax -- uv --directory <repo> run
      --extra mcp python -m src.mcp_server`. Remaining from the roadmap:
      classification UI (Phase 2) — becomes relevant with the first
      non-stock asset (fund/bond).

## 5. Feature-parity gaps vs. taxomat.cz (competitive scan, 2026-07-04)

Comparison against the taxomat.cz Portfolio Tracker feature list. The
multi-broker / multi-platform gap is **deliberately out of scope** — we
are IBKR-only by design. What remains, ordered by value-for-effort:

### 5a. Tax-optimisation gaps (change the resulting tax — highest value)

- [x] **Multiple pairing methods (FIFO / LIFO / weighted-average /
      optimal) with cheaper-of selection.** DONE (2026-07-04): a private
      §10 investor may pick any method (GFŘ výklad; taxomat article
      confirms). `src/engine/pairing.py` adds a `PairingMethod` enum and
      pluggable lot ordering/costing in `FifoLedger`
      (`src/engine/fifo_manager.py`): FIFO (default, unchanged), LIFO,
      weighted average (blended pool cost, FIFO lot identity for the time
      test). `optimal` is a global tax-minimising min-cost-flow solver
      (`src/engine/pairing_solver.py` + `src/countries/cz/optimal_pairing.py`)
      that routes gains onto time-test-exempt lots and losses onto taxable
      ones. `--cz-pairing-method fifo|lifo|weighted_average|optimal|compare`
      (`compare` = full FX-mode × method matrix, cheapest by final tax via
      `src/countries/cz/pairing_compare.py`); web GUI selector + MCP
      `run_pipeline(pairing_method=…)`. Covered by
      `tests/test_cz_pairing_methods.py` (taxomat worked example
      FIFO=130 / LIFO=30 / WA=72.5, solver optima, E2E). LIMITATIONS: the
      `optimal` solver covers long securities only (options/shorts/assets
      with mid-year corp actions or capital repayments stay FIFO); it is
      exact for base+rates, near-optimal at the 100k cliff (mitigated by
      scoring every method with the real aggregator — never worse than
      FIFO).
- [ ] **Dividend separate tax base (samostatný základ daně).** taxomat
      computes dividends under both the general base and the separate
      15 % base and picks the better one. We only run the general
      15 %/23 % base (`src/countries/cz/tax_liability.py`). Small logic,
      real tax impact for higher-income filers — good second step.
- [ ] **Sale-impact / max-gain-loss optimiser surfacing.** We already
      have per-sale simulation (`simulate_sale`); taxomat additionally
      highlights which lots to sell to hit a target gain/loss (e.g. use
      up the 100k exemption, realise offsetting losses). Layer on top of
      the existing simulator + time-test + annual-limit logic.

### 5b. Asset-class gaps

- [ ] **Futures as a first-class category.** IBKR trades futures and the
      Flex data carries them, but we only model `CFD`/`OPTION`
      (`AssetCategory` in `src/domain/enums.py`). Real gap in incoming
      data, not just a product-scope choice.
- [ ] **Native crypto with §4 time test.** Today crypto only enters as
      crypto-ETP/ETC mapped to `PRIVATE_SALE_ASSET`. Direct crypto (if
      ever sourced) would need its own category + time-test handling.
      Lower priority — IBKR spot-crypto coverage is narrow.
- [ ] **Real estate / rental income.** Out of IBKR scope entirely; noted
      only for completeness. Not planned.

### 5c. Portfolio-tracking / UX gaps

- [ ] **Cumulative time-test exemption timeline.** We show a per-lot
      countdown (`time_test_deadline()`); taxomat adds a cumulative "how
      much becomes tax-free and when" timeline graph. Low effort over
      data we already hold.
- [ ] **Closed-positions / year-by-year performance view.** We have a
      value-history chart; missing is realised-performance history broken
      down by year / asset type / security.
- [ ] **Target vs. actual allocation (rebalancing).** We render current
      allocation only — no target weights / drift.
- [ ] **Heat map** of position sizes. Pure UX, lowest priority.
- [ ] **Option expiry / assignment monitoring & alerts.** Lifecycle is
      handled for tax; a monitoring/alerts surface in the GUI is missing.
- [ ] **Mobile app (iOS/Android).** Out of scope — we are a local
      web/CLI tool. Noted only for completeness.

Already at parity (for reference, not gaps): current valuation +
realised/unrealised P/L, live quotes, per-lot time-test countdown,
dividend overview, ČNB-vs-GFŘ uniform-rate comparison, sale simulation,
audit-friendly PDF/JSON/XLSX exports.

## 6. Documentation

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
