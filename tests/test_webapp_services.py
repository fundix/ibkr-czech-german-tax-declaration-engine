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
