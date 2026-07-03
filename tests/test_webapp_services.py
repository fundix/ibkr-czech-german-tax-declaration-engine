# tests/test_webapp_services.py
"""
Web service layer over the engine — offline, using the synthetic 2024 golden
dataset (data/synthetic_2024) with pinned FX providers. The end-to-end run
must reproduce the same figures test_golden_e2e_cz.py pins (final tax
3 604 CZK), proving the GUI path computes exactly what the CLI does.
"""
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from src.webapp.services import RunService
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider

SYNTHETIC = Path(__file__).resolve().parent.parent / "data" / "synthetic_2024"

# synthetic file name -> canonical slot file name
SYNTHETIC_MAP = {
    "trades.csv": "trades.csv",
    "cash_transactions.csv": "cash_transactions.csv",
    "positions_start_of_year.csv": "positions_start.csv",
    "positions_end_of_year.csv": "positions_end.csv",
    "corporate_actions.csv": "corporate_actions.csv",
}


@pytest.fixture
def service(tmp_path):
    svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    yield svc
    svc.runner.shutdown(wait=False)


def _seed_synthetic_year(svc: RunService, year: int = 2024) -> None:
    year_dir = svc.data_dir / str(year)
    year_dir.mkdir(parents=True)
    for src_name, dst_name in SYNTHETIC_MAP.items():
        shutil.copyfile(SYNTHETIC / src_name, year_dir / dst_name)


class TestDatasets:
    def test_empty_data_dir_lists_nothing(self, service):
        assert service.list_years() == []

    def test_seeded_year_is_run_ready(self, service):
        _seed_synthetic_year(service)
        datasets = service.list_years()
        assert len(datasets) == 1
        assert datasets[0].year == 2024
        assert datasets[0].run_ready

    def test_missing_required_files_reported(self, service):
        (service.data_dir / "2025").mkdir(parents=True)
        shutil.copyfile(SYNTHETIC / "trades.csv", service.data_dir / "2025" / "trades.csv")
        ds = service.get_year(2025)
        assert not ds.run_ready
        assert "cash" in ds.missing_required
        assert "positions_end" in ds.missing_required

    def test_save_upload_writes_canonical_name(self, service):
        service.save_upload(2025, "trades", b"a,b\n1,2\n")
        assert (service.data_dir / "2025" / "trades.csv").read_text() == "a,b\n1,2\n"

    def test_save_upload_rejects_unknown_slot(self, service):
        with pytest.raises(ValueError):
            service.save_upload(2025, "evil", b"x")


class TestInputAssembly:
    def test_trades_merged_across_years_ascending(self, service, tmp_path):
        for year, row in ((2024, "r2024"), (2025, "r2025")):
            d = service.data_dir / str(year)
            d.mkdir(parents=True)
            (d / "trades.csv").write_text(f'"H1","H2"\n"{row}","x"\n')
        merged = service._merge_years("trades", 2025, tmp_path / "merged.csv")
        lines = merged.read_text().splitlines()
        assert lines == ['"H1","H2"', '"r2024","x"', '"r2025","x"']

    def test_merge_rejects_mismatched_headers(self, service, tmp_path):
        for year, header in ((2024, '"A","B"'), (2025, '"A","C"')):
            d = service.data_dir / str(year)
            d.mkdir(parents=True)
            (d / "trades.csv").write_text(f"{header}\n1,2\n")
        with pytest.raises(ValueError, match="hlavičku"):
            service._merge_years("trades", 2025, tmp_path / "merged.csv")

    def test_positions_start_falls_back_to_previous_year_end(self, service, tmp_path):
        _seed_synthetic_year(service, 2024)
        # Year 2025 without positions_start: reuse 2024's positions_end
        d = service.data_dir / "2025"
        d.mkdir(parents=True)
        for name in ("trades.csv", "cash_transactions.csv", "positions_end.csv"):
            shutil.copyfile(SYNTHETIC / "trades.csv", d / name)
        run_dir = tmp_path / "rundir"
        inputs = service._prepare_inputs(run_dir, 2025)
        expected = (service.data_dir / "2024" / "positions_end.csv").read_text()
        assert inputs["positions_start"].read_text() == expected


class TestExecuteRun:
    def test_golden_run_reproduces_cli_figures(self, service):
        _seed_synthetic_year(service)
        meta = service._execute_run(
            "2024-test", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )

        # Golden figures pinned by test_golden_e2e_cz.py
        assert Decimal(meta["summary"]["daily"]["final_tax_czk"]) == Decimal("3604.00")
        assert meta["tax_year"] == 2024
        assert meta["modes"] == ["daily"]
        assert meta["eoy_mismatch_error_count"] == 0

        run_dir = service.runs_dir / "2024-test"
        for name in ("meta.json", "result.daily.json", "result.daily.xlsx", "form.daily.json"):
            assert (run_dir / name).is_file(), f"missing {name}"
        # The exact inputs the engine consumed are preserved for audit
        assert (run_dir / "inputs" / "trades.csv").is_file()

        # Persisted results readable through the service API
        assert service.get_run("2024-test")["run_id"] == "2024-test"
        result = service.load_result("2024-test", "daily")
        assert result["metadata"]["tax_year"] == 2024
        assert len(result["items"]) > 0
        form = service.load_form("2024-test", "daily")
        codes = {ln["code"] for sec in form["sections"] for ln in sec["lines"]}
        assert "CZ_DAP_8_TOTAL" in codes
        assert service.list_runs()[0]["run_id"] == "2024-test"
        assert service.export_path("2024-test", "daily", "xlsx").is_file()

    def test_portfolio_snapshot_from_open_fifo_lots(self, service):
        _seed_synthetic_year(service)
        service._execute_run(
            "2024-pf", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )
        pf = service.load_portfolio("2024-pf")
        assert pf["tax_year"] == 2024

        # Golden scenario: only DIVCO stays open at EOY (100 shares held all
        # year, no trades — SOY fallback lot). ALPHA/OLDCO sold, put expired.
        assert [p["symbol"] for p in pf["positions"]] == ["DIVCO"]
        divco = pf["positions"][0]
        assert Decimal(divco["quantity_long"]) == Decimal("100")
        # Open lots must equal the reported EOY quantity (cross-validation)
        assert Decimal(divco["quantity_long"]) == Decimal(divco["eoy_quantity"])
        assert divco["time_test_applicable"] is True

        [lot] = divco["lots"]
        # SOY fallback: synthetic 31 Dec acquisition — no reliable deadline
        assert lot["acquisition_estimated"] is True
        assert lot["time_test_deadline"] is None

    def test_run_without_dataset_raises(self, service):
        with pytest.raises(ValueError, match="2031"):
            service._execute_run("2031-test", 2031, "daily")

    def test_start_run_validates_fx_mode(self, service):
        with pytest.raises(ValueError, match="režim"):
            service.start_run(2024, "bogus")


# ---------------------------------------------------------------------------
# Live valuation + sale simulator (stubbed quotes + FX: USD→CZK 20, EUR→CZK 25)
# ---------------------------------------------------------------------------

class StubConverter:
    RATES = {"USD": Decimal("20"), "EUR": Decimal("25"), "CZK": Decimal("1")}

    def convert_to_czk(self, amount, currency, event_date):
        from types import SimpleNamespace
        rate = self.RATES.get(currency)
        if rate is None:
            return None
        return SimpleNamespace(converted_amount_czk=amount * rate)


class StubQuotes:
    def __init__(self, prices):
        self.prices = prices  # symbol -> (price, currency)

    def get_quote(self, symbol, currency):
        from types import SimpleNamespace
        hit = self.prices.get(symbol)
        if hit is None:
            return None
        return SimpleNamespace(ibkr_symbol=symbol, yahoo_symbol=symbol,
                               price=hit[0], currency=hit[1], fetched_at=0.0)


def _sim_position():
    return {
        "symbol": "TEST", "description": "Test Corp", "category": "STOCK",
        "time_test_applicable": True, "quantity_long": "30",
        "eoy_currency": "USD", "eoy_market_price": "11",
        "lots": [
            {"acquisition_date": "2020-01-10", "quantity": "10",
             "unit_cost_eur": "8", "acquisition_estimated": False,
             "time_test_deadline": "2023-01-10"},   # long past → exempt
            {"acquisition_date": "2025-06-01", "quantity": "20",
             "unit_cost_eur": "10", "acquisition_estimated": False,
             "time_test_deadline": "2028-06-01"},   # still running
        ],
    }


def _result_with_proceeds(existing="715704.73"):
    return {"sections": {"cz_10_summary": {"line_items": {
        "annual_limit_eligible_proceeds_czk": existing,
        "annual_limit_threshold_czk": "100000.00",
    }}}}


@pytest.fixture
def stub_service(tmp_path):
    svc = RunService(
        data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
        quote_service=StubQuotes({"TEST": (Decimal("12"), "USD")}),
        converter_factory=StubConverter,
    )
    yield svc
    svc.runner.shutdown(wait=False)


class TestSimulator:
    def test_fifo_split_exempt_vs_taxable_with_loss(self, stub_service):
        # Sell 25 @ 12 USD: lot A (10 ks, exempt) gain 2400−2000 = +400;
        # lot B (15 ks) gain 3600−3750 = −150 taxable → tax 0 (loss)
        sim = stub_service._compute_simulation(
            _sim_position(), Decimal("25"), Decimal("12"), _result_with_proceeds()
        )
        assert [c["quantity"] for c in sim["consumed"]] == [Decimal("10"), Decimal("15")]
        assert sim["exempt_gain_czk"] == Decimal("400")
        assert sim["taxable_gain_czk"] == Decimal("-150")
        assert sim["estimated_tax_czk"] == Decimal("0")
        assert sim["proceeds_czk"] == Decimal("6000")
        assert sim["annual_limit"]["under_limit"] is False
        assert sim["wait_until"] == "2028-06-02"

    def test_positive_taxable_gain_taxed_at_15_percent(self, stub_service):
        # Sell all 30 @ 15 USD: lot A +1000 exempt; lot B 6000−5000 = +1000
        # taxable → tax 150.00
        sim = stub_service._compute_simulation(
            _sim_position(), Decimal("30"), Decimal("15"), _result_with_proceeds()
        )
        assert sim["exempt_gain_czk"] == Decimal("1000")
        assert sim["taxable_gain_czk"] == Decimal("1000")
        assert sim["estimated_tax_czk"] == Decimal("150.00")

    def test_annual_limit_exempts_everything(self, stub_service):
        # No proceeds yet this year: 30×15×20 = 9 000 Kč ≤ 100 000 → no tax
        sim = stub_service._compute_simulation(
            _sim_position(), Decimal("30"), Decimal("15"), _result_with_proceeds("0")
        )
        assert sim["annual_limit"]["under_limit"] is True
        assert sim["estimated_tax_czk"] == Decimal("0")

    def test_quantity_capped_at_available(self, stub_service):
        sim = stub_service._compute_simulation(
            _sim_position(), Decimal("999"), Decimal("12"), _result_with_proceeds()
        )
        assert sim["quantity"] == Decimal("30")

    def test_live_quote_used_when_price_missing(self, stub_service):
        sim = stub_service._compute_simulation(
            _sim_position(), Decimal("10"), None, _result_with_proceeds()
        )
        assert sim["price"] == Decimal("12")
        assert sim["price_source"] == "live"


class TestLivePortfolio:
    def _pf(self):
        return {"tax_year": 2025, "positions": [
            {**_sim_position(), "total_cost_eur": "280"},
            {"symbol": "NOQUOTE", "description": "x", "category": "STOCK",
             "time_test_applicable": True, "quantity_long": "5",
             "eoy_currency": "USD", "eoy_market_price": "10",
             "total_cost_eur": "30", "lots": []},
        ]}

    def test_live_valuation_with_fallback_and_totals(self, stub_service):
        live = stub_service._compute_live_portfolio(self._pf())
        by_symbol = {p["symbol"]: p for p in live["positions"]}
        # TEST: live 12 USD → 30×12×20 = 7 200; cost 280 EUR → 7 000
        assert by_symbol["TEST"]["price_source"] == "live"
        assert by_symbol["TEST"]["value_czk"] == Decimal("7200")
        assert by_symbol["TEST"]["unrealized_czk"] == Decimal("200")
        # NOQUOTE: falls back to EOY price 10 USD → 5×10×20 = 1 000
        assert by_symbol["NOQUOTE"]["price_source"] == "eoy"
        assert by_symbol["NOQUOTE"]["value_czk"] == Decimal("1000")
        assert live["total_value_czk"] == Decimal("8200")
        assert live["quotes_ok"] == 1

    def test_snapshot_saved_once_per_day(self, stub_service):
        stub_service._compute_live_portfolio(self._pf())
        stub_service._compute_live_portfolio(self._pf())
        snaps = stub_service.list_snapshots()
        assert len(snaps) == 1
        assert Decimal(snaps[0]["total_value_czk"]) == Decimal("8200")
