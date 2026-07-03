# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **pydantic v1 → v2** (`raw_models.py`; the wildcard Decimal-coercion
  semantics — empty CSV cell on a Decimal field → `0.0` — are preserved
  exactly). `requires-python` raised to `>=3.10`. Unblocks the planned
  local web GUI (FastAPI) and MCP server (official SDK); verified by the
  full suite plus a golden JSON export diff on `data/synthetic_2024`.
- Cache paths (`user_classifications.json`, ECB/ČNB rates) are anchored to
  the project root instead of the caller's cwd.
- `setup_decimal_context()` moved from `main.py` to
  `src/utils/decimal_context.py` — `decimal.getcontext()` is thread-local,
  so non-main-thread callers (job workers) must set it themselves.

### Added

- **Local web GUI (Phase 1)** — `uv run --extra web python -m src.webapp`
  starts a localhost FastAPI + Jinja2 + HTMX app (Czech UI, optional `web`
  dependency group; no Node/build step, htmx vendored):
  - per-year input datasets (`data/webapp/<year>/`, gitignored) with upload
    page; trades/corporate actions merged across years for FIFO history,
    start-of-year positions derived from the previous year's end;
  - runs execute on the Phase 0 `JobRunner` with live HTMX progress
    (captured engine log), each run persists its exact merged inputs +
    JSON/XLSX exports + DAP form mapping under `out/webapp_runs/<run_id>/`;
  - result pages: summary with daily/uniform comparison, filterable
    per-item table, verified DAP form line references, and a
    **manual-review checklist page** (PENDING items + section REVIEW notes
    with a nav badge — the future-work "checklist as first-class output"
    item, resolved at the GUI level), JSON/XLSX downloads;
  - service layer (`src/webapp/services.py`) is framework-free and will be
    shared with the planned MCP server.
- `src/countries/cz/aggregation_service.py` — reusable
  `run_cz_aggregation`/`run_cz_compare` extracted from the CLI (`main.py`
  delegates; supports FX provider injection for offline tests).
- `src/webapp/jobs.py` — Phase 0 server-safety primitives for the
  local web GUI: single-worker `JobRunner` (serializes engine runs, sets the
  decimal context in the worker via initializer, captures log tail and
  failures) and `engine_file_lock` (cross-process `flock` guard for the
  unlocked FX/classification caches). Covered by `tests/test_webapp_jobs.py`.

### Fixed

- **Margin/debit interest no longer reduces the CZ §8 base.** IBKR "Broker
  Interest Paid" rows were mapped to `INTEREST_RECEIVED` with the negative
  amount kept, entered §8 as negative interest income and diluted the §38f
  foreign-income base (found on real 2025 statements: −1,272 CZK base,
  FTC credit understated by 159 CZK). They now map to a new
  `INTEREST_PAID_DEBIT` event type (stored positive = cost; refunds net),
  are excluded from CZ §8 income and the FTC base, and the excluded total
  is surfaced as an audit note on the interest section.

### Added

- **`--tax-year` CLI flag** overriding `config.TAX_YEAR` for the whole run
  (pipeline, loss offsetting, CZ aggregation, default PDF filename), so a
  run no longer requires editing `src/config.py`.
- **Verified `official_line_ref` in `cz/form_mapping.py`** against the
  official 2025-period forms (DAP 25 5405 vzor č. 30, Příloha 2 vzor č. 21,
  Příloha 3 vzor č. 21): §8 → ř. 38; §10 → Příloha 2 tabulka (druh D
  cenné papíry / druh F deriváty, kód "z"), ř. 209 → ř. 40; daň § 16 →
  ř. 57; §38f → Příloha 3 ř. 321–330 (samostatný list za stát), ř. 330 →
  ř. 58. The stale "SZDZ caps are placeholders" warning now reflects the
  verified dividend caps.

## [4.0.0] - 2026-04-02

### Added

**Multi-country architecture**
- Plugin system with `TaxPlugin` / `TaxClassifier` / `TaxAggregator` / `OutputRenderer` Protocols
- Country registry with `--country de|cz` CLI flag
- FX provider factory supporting ECB and ČNB providers

**Czech Republic plugin (`countries/cz/`)**
- Per-event CZK conversion via ČNB daily rates with full audit trail
- §8 ZDP bucket classification (dividends, interest)
- §10 ZDP bucket classification (securities, options)
- Holding-period time test (§4/1/w ZDP, configurable 3-year rule)
- Annual exempt limit (CZK 100k for disposal proceeds, 2025+ amendment)
- §10 loss offsetting (securities and options netted separately)
- Foreign tax credit (§38f ZDP, per-item caps + proportional finalization)
- Tax liability computation (15% / 23% rates with configurable threshold)
- DAP-oriented form mapping with stable internal line codes
- JSON and XLSX audit exports
- `CzTaxItem` model with full audit trail (FX, WHT, taxability, exemption)
- Withholding tax linking (explicit ID match + asset/date proximity)
- Unlinked WHT preserved as standalone audit items

**Core refactoring**
- German tax classification extracted from `fifo_manager.py` into `GermanTaxClassifier`
- `RealizedGainLoss.__post_init__` cleaned — no more auto-calculated Teilfreistellung
- Calculation engine accepts injectable `tax_classifier` callback
- `ExchangeRateProvider` base class satisfies `FxProvider` Protocol

**Documentation**
- `README.md` rewritten for multi-country use
- `docs/architecture.md` — layer diagram, separation of concerns
- `docs/cz-plugin.md` — CZ features, limitations, policy assumptions
- `docs/development.md` — test structure, debugging, mock patterns
- `CONTRIBUTING.md` — setup, coding style, PR checklist

### Changed
- Project name: `ibkr-german-tax-declaration-engine` → `ibkr-tax-declaration-engine`
- Version jump from 3.3.1 to 4.0.0 (breaking: multi-country architecture)
- `pipeline_runner.py` now accepts `country_code` parameter
- German plugin is the default (`--country de`)

### Known Limitations
- CZ: Treaty credit caps are placeholder values — verify per-treaty
- CZ: Jednotný kurz (uniform/annual rate) not implemented
- CZ: Pre-2014 acquisition rule (6-month test) not implemented
- CZ: Expense deduction (§10/4 ZDP) not implemented
- CZ: Loss carryforward not implemented
- CZ: Elevated-rate threshold applies to IBKR income only
- CZ: RGL disposal amounts use EUR→CZK (core converts to EUR first)
- CZ: Official form line references not verified (`official_line_ref=None`)
- Core: Stock-for-stock merger FIFO logic not fully implemented
- DE: Sparer-Pauschbetrag, solidarity surcharge, church tax not calculated

## [3.3.1] - Previous

German-only release. See git history for details.
