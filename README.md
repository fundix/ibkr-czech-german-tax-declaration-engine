# IBKR Tax Declaration Engine

**Multi-country tax declaration tool for Interactive Brokers (IBKR) users. Processes Flex Query CSV reports and computes tax figures for Germany (DE) and Czech Republic (CZ).**

> **Not tax advice.** This tool generates figures to *assist* your tax preparation. Always verify results with a qualified tax advisor before filing. See [Disclaimer](#disclaimer).

## What is this?

A Python tool that automates the tedious parts of preparing tax declarations from IBKR brokerage data:

1. Parses IBKR Flex Query CSV reports (trades, dividends, corporate actions, positions).
2. Classifies assets (stocks, bonds, ETFs, options, CFDs).
3. Performs FIFO gain/loss calculations with `Decimal` precision.
4. Converts currencies using daily ECB or ČNB rates.
5. Handles corporate actions (splits, mergers, stock dividends).
6. Processes option exercises, assignments, and expirations.
7. Applies **country-specific tax rules** via a plugin architecture.
8. Generates audit-friendly reports (console, PDF, JSON, XLSX).

## Project Status

| Component | Status |
|-----------|--------|
| Core FIFO/enrichment | Stable — spec-driven FIFO/options/loss-offsetting test groups |
| German plugin (DE) | Production — validated for tax year 2023 |
| Czech plugin (CZ) | Beta — calculation audit 2026-07 complete; policy placeholders remain (see [known limitations](docs/cz-plugin.md#known-limitations)) |
| Test suite | 542 tests passing |

The CZ calculation path went through a full audit in 2026-07 (39 findings,
35 fixed, 4 open pending real data / design decisions — see
[AUDIT_REPORT_2026-07.md](AUDIT_REPORT_2026-07.md)) and was end-to-end
validated against an independently hand-computed synthetic scenario
(FIFO, time test, annual limit, §38f FTC caps and final tax all matched).
Planned next steps live in [docs/future-work.md](docs/future-work.md).

## Supported Countries

| Country | Plugin | Status | Output formats |
|---------|--------|--------|----------------|
| **Germany (DE)** | `countries/de/` | Production — validated for 2023 | Console, PDF |
| **Czech Republic (CZ)** | `countries/cz/` | Beta — audited 2026-07; policy placeholders remain | Console, JSON, XLSX |

### Germany (DE)
- Anlage KAP, KAP-INV, SO form figures
- Teilfreistellung for investment funds
- Vorabpauschale
- Derivative loss capping
- PDF tax report

### Czech Republic (CZ)
- §8 ZDP (dividends, interest) + §10 ZDP (securities, options)
- Holding-period time test (§4/1/w ZDP, 3-year rule)
- Annual exempt limit (CZK 100k, 2025+ amendment)
- §10 loss offsetting
- Foreign tax credit (§38f ZDP, per-item treaty caps, per-state §38f/8 cap + proportional finalization)
- Tax liability computation (15 % / 23 % rates)
- DAP-oriented form mapping
- Per-event CZK conversion via ČNB daily rates
- Audit-friendly JSON and XLSX exports

### Core (country-agnostic)
- IBKR Flex Query CSV parsing
- FIFO lot accounting with `Decimal` precision
- ECB + ČNB FX providers with JSON caching
- Corporate actions (splits, mergers, stock dividends)
- Option lifecycle (exercise, assignment, expiration)
- Withholding tax linking

## Quick Start

```bash
# Clone and install
git clone https://github.com/fundix/ibkr-german-tax-declaration-engine-czech.git
cd ibkr-german-tax-declaration-engine-czech
uv sync

# Run tests
uv run pytest

# Run for Germany (default)
uv run python -m src.main --report-tax-declaration

# Run for Czech Republic
uv run python -m src.main --country cz --report-tax-declaration
```

### Prerequisites
- Python 3.8+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) package manager
- IBKR Flex Query CSV reports (see `input_data_spec.md`)

### Configuration
Edit `src/config.py`: set `TAX_YEAR`, file paths, and `TAXPAYER_NAME`. See `src/config_example.py` for all options.

## Project Structure

```
src/
├── domain/          # Core data models (assets, events, results, enums)
├── parsers/         # IBKR CSV parsing
├── engine/          # FIFO ledger, calculation engine, event processors
├── processing/      # Enrichment, option linking, WHT linking
├── identification/  # Asset resolver
├── classification/  # Asset classifier
├── utils/           # FX providers (ECB, ČNB), currency converter
├── reporting/       # German console + PDF reports
├── countries/
│   ├── base.py      # TaxPlugin / TaxClassifier / TaxAggregator Protocols
│   ├── registry.py  # get_tax_plugin("de") / get_tax_plugin("cz")
│   ├── de/          # German tax plugin
│   └── cz/          # Czech tax plugin
│       ├── plugin.py
│       ├── config.py
│       ├── tax_items.py
│       ├── time_test.py
│       ├── annual_limit.py
│       ├── loss_offsetting.py
│       ├── foreign_tax_credit.py
│       ├── tax_liability.py
│       ├── form_mapping.py
│       ├── fx_policy.py
│       └── exporters/    # JSON + XLSX
├── main.py
├── cli.py
├── config.py
└── pipeline_runner.py
```

## Documentation

| Document | Audience |
|----------|----------|
| [Architecture](docs/architecture.md) | Developers, contributors |
| [CZ Plugin](docs/cz-plugin.md) | CZ users, CZ contributors |
| [Roadmap / Future Work](docs/future-work.md) | Contributors, planning |
| [Audit Report 2026-07](AUDIT_REPORT_2026-07.md) | Reviewers, auditors |
| [Development & Testing](docs/development.md) | All contributors |
| [Contributing](CONTRIBUTING.md) | New contributors |
| [CLAUDE.md](CLAUDE.md) | AI coding assistants |

## Disclaimer

This software is provided "as is," without warranty of any kind. The output is **not tax advice**. Always verify figures with a qualified tax professional. Some country-specific policies use configurable placeholder values that require verification against current legislation and applicable tax treaties. The authors are not liable for any damages arising from use of this software.

## License

MIT License — see [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, guidelines, and how to add a new country plugin.
