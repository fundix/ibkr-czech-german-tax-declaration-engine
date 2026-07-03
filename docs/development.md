# Development & Testing

## Setup

```bash
uv sync          # install all dependencies (creates .venv)
uv run pytest    # run all tests
```

## Test Structure

```
tests/
├── test_fifo_groups.py              # Core FIFO scenarios (Groups 1-5)
├── test_group6_loss_offsetting.py   # German loss offsetting (Group 6)
├── test_options_lifecycle.py        # Option exercise/assignment/expiry
├── test_dividend_handling.py        # Dividend + capital repayment
├── test_withholding_tax_linker.py   # WHT linking logic
├── test_precision.py                # Decimal precision edge cases
├── test_mock_providers.py           # FX provider mocks
├── test_country_plugin_interface.py # Protocol conformance (DE + CZ)
├── test_cnb_fx_provider.py          # ČNB FX provider
├── test_cz_plugin.py               # CZ plugin basics
├── test_cz_fx_policy.py            # CZ FX conversion policy
├── test_cz_tax_items.py            # CZ item building + WHT linking
├── test_cz_time_test.py            # CZ holding-period test
├── test_cz_time_test_boundaries.py  # CZ time test edge cases
├── test_cz_annual_limit_and_netting.py  # 100k limit + §10 netting
├── test_cz_foreign_tax_credit.py    # CZ FTC computation
├── test_cz_ftc_boundaries.py        # CZ FTC edge cases
├── test_cz_tax_liability.py         # CZ tax liability + rate application
├── test_cz_form_mapping.py          # CZ DAP-oriented form mapping
├── test_cz_exporters.py             # JSON + XLSX export
├── test_cz_pdf_exporter.py          # PDF export (pymupdf text extraction)
├── support/                         # Shared test infrastructure
│   ├── base.py
│   ├── helpers.py
│   ├── expected.py
│   ├── mock_providers.py
│   ├── csv_creators.py
│   └── option_helpers.py
└── fixtures/                        # Test data specs (YAML)
```

## Running Specific Test Groups

```bash
# All tests
uv run pytest

# Only CZ plugin tests
uv run pytest tests/test_cz_*.py

# Only core FIFO tests
uv run pytest tests/test_fifo_groups.py

# Only export tests
uv run pytest tests/test_cz_exporters.py

# Only German loss offsetting
uv run pytest tests/test_group6_loss_offsetting.py

# Single test with verbose output
uv run pytest tests/test_cz_tax_liability.py::TestFtcCapped -v

# Stop on first failure
uv run pytest -x --tb=short
```

## Adding a Test Scenario

### For CZ tax items
Create `CzTaxItem` instances directly in your test:

```python
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType
from src.countries.cz.enums import CzTaxSection

item = CzTaxItem(
    item_type=CzTaxItemType.SECURITY_DISPOSAL,
    section=CzTaxSection.CZ_10_SECURITIES,
    source_event_id=uuid.uuid4(),
    event_date="2025-03-25",
    gain_loss_czk=Decimal("5000"),
)
```

### For full pipeline integration
Use `CzechTaxAggregator` with mock data:

```python
from src.countries.cz.plugin import CzechTaxAggregator
from src.countries.cz.config import CzTaxConfig

cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
aggregator = CzechTaxAggregator(config=cfg)
result = aggregator.aggregate(rgls, events, resolver, 2025)
```

### For FIFO scenarios
Use the spec-driven approach from `tests/support/`:

```python
from tests.support import FifoTestCaseBase
```

## Debugging Regressions

1. Run `uv run pytest -x` to find the first failing test.
2. Run the failing test with `-v --tb=long` for full traceback.
3. Check if the failure is in a CZ test (section keys changed?) or core test.
4. Common causes:
   - Section key renamed (e.g. `cz_10_securities` → `cz_10_summary`)
   - New section added (section count assertion outdated)
   - Annual limit auto-exempting items in tests with small EUR amounts
   - Unlinked WHT creating standalone items that change totals

## MockCNBProvider Pattern

For CZ tests needing FX conversion:

```python
SAMPLE_CNB = """\
25.03.2025 #59
země|měna|množství|kód|kurz
EMU|euro|1|EUR|24,320
USA|dolar|1|USD|22,345
"""

class MockCNBProvider(CNBExchangeRateProvider):
    def __init__(self, responses=None, **kw):
        self._mock_responses = responses or {}
        if "cache_file_path" not in kw:
            kw["cache_file_path"] = os.path.join(tempfile.mkdtemp(), "m.json")
        super().__init__(**kw)

    def _fetch_rates_for_date(self, query_date):
        text = self._mock_responses.get(query_date)
        return self._parse_cnb_text(text, query_date) if text else None
```
