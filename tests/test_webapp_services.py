# tests/test_webapp_services.py
"""
Web service layer over the engine — offline, using the synthetic 2024 golden
dataset (data/synthetic_2024) with pinned FX providers. The end-to-end run
must reproduce the same figures test_golden_e2e_cz.py pins (final tax
3 604 CZK), proving the GUI path computes exactly what the CLI does.
"""
import re
import shutil
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

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

    def test_delete_year_moves_dataset_to_trash(self, service):
        _seed_synthetic_year(service)
        trades_content = (service.data_dir / "2024" / "trades.csv").read_bytes()

        trash = service.delete_year_dataset(2024)

        assert service.list_years() == []           # gone from datasets
        assert not (service.data_dir / "2024").exists()
        assert trash.parent == service.data_dir / "_trash"
        # Soft delete: the files survive in the trash, byte-identical
        assert (trash / "trades.csv").read_bytes() == trades_content

    def test_delete_year_without_dataset_raises(self, service):
        with pytest.raises(ValueError, match="2031"):
            service.delete_year_dataset(2031)

    def test_trash_dir_not_listed_as_dataset(self, service):
        _seed_synthetic_year(service)
        service.delete_year_dataset(2024)
        _seed_synthetic_year(service)               # re-seed after delete
        assert [d.year for d in service.list_years()] == [2024]


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

    def test_merge_drops_duplicate_transaction_ids(self, service, tmp_path):
        """Overlapping Flex query periods export the same trades into two
        year files — identical rows (same TransactionID) must merge once."""
        header = '"Symbol","TradeDate","TransactionID"'
        (service.data_dir / "2024").mkdir(parents=True)
        (service.data_dir / "2024" / "trades.csv").write_text(
            f'{header}\n"AAA","2024-03-01","t1"\n"BBB","2024-11-05","t2"\n')
        (service.data_dir / "2025").mkdir(parents=True)
        (service.data_dir / "2025" / "trades.csv").write_text(
            f'{header}\n"BBB","2024-11-05","t2"\n"CCC","2025-02-01","t3"\n')

        notes = []
        merged = service._merge_years("trades", 2025, tmp_path / "m.csv", notes=notes)
        lines = merged.read_text().splitlines()
        assert lines == [
            header,
            '"AAA","2024-03-01","t1"',
            '"BBB","2024-11-05","t2"',
            '"CCC","2025-02-01","t3"',
        ]
        assert len(notes) == 1 and "1 duplicitních" in notes[0]

    def test_merge_keeps_distinct_rows_sharing_action_id(self, service, tmp_path):
        """Multi-leg corporate actions may share an ActionID — only
        byte-identical repeats are duplicates, distinct legs stay."""
        header = '"Symbol","Type","ActionID"'
        (service.data_dir / "2024").mkdir(parents=True)
        (service.data_dir / "2024" / "corporate_actions.csv").write_text(
            f'{header}\n"AAA","CASH","a1"\n"AAA","STOCK","a1"\n')
        (service.data_dir / "2025").mkdir(parents=True)
        (service.data_dir / "2025" / "corporate_actions.csv").write_text(
            f'{header}\n"AAA","STOCK","a1"\n')

        merged = service._merge_years("corp_actions", 2025, tmp_path / "m.csv")
        lines = merged.read_text().splitlines()
        assert lines == [header, '"AAA","CASH","a1"', '"AAA","STOCK","a1"']

    def test_merge_drops_headers_repeated_mid_file(self, service, tmp_path):
        """IBKR repeats the header between export sections (multi-account)
        and hand-concatenated uploads carry one per source file. Corp-action
        rows validate even with non-numeric decimals (safe_decimal -> 0), so
        a header-as-data row would become a phantom asset named "Symbol" —
        it must never leave the merge."""
        header = '"Symbol","Type","ActionID"'
        (service.data_dir / "2024").mkdir(parents=True)
        (service.data_dir / "2024" / "corporate_actions.csv").write_text(
            f'{header}\n"AAA","CASH","a1"\n{header}\n"BBB","STOCK","a2"\n')
        (service.data_dir / "2025").mkdir(parents=True)
        (service.data_dir / "2025" / "corporate_actions.csv").write_text(
            f'{header}\n{header}\n"CCC","CASH","a3"\n')

        notes = []
        merged = service._merge_years("corp_actions", 2025, tmp_path / "m.csv",
                                      notes=notes)
        lines = merged.read_text().splitlines()
        assert lines == [
            header,
            '"AAA","CASH","a1"',
            '"BBB","STOCK","a2"',
            '"CCC","CASH","a3"',
        ]
        assert notes == []  # dropped headers are not duplicate-row warnings

    def test_prepare_inputs_drops_repeated_headers_in_copied_files(
            self, service, tmp_path):
        """Cash and positions files are copied, not merged — the same
        mid-file repeated headers must be dropped on that path too."""
        _seed_synthetic_year(service)
        for name in ("cash_transactions.csv", "positions_end.csv"):
            path = service.data_dir / "2024" / name
            lines = path.read_text().splitlines()
            doctored = [lines[0], lines[1], lines[0], *lines[2:]]
            path.write_text("\n".join(doctored) + "\n")

            inputs = service._prepare_inputs(tmp_path / f"run_{name}", 2024)
            slot = "cash" if name.startswith("cash") else "positions_end"
            copied = inputs[slot].read_text().splitlines()
            assert copied == lines  # header once, every data row kept

    def test_merge_without_id_column_keeps_everything(self, service, tmp_path):
        for year in (2024, 2025):
            d = service.data_dir / str(year)
            d.mkdir(parents=True)
            (d / "trades.csv").write_text('"A","B"\n"same","row"\n')
        notes = []
        merged = service._merge_years("trades", 2025, tmp_path / "m.csv", notes=notes)
        lines = merged.read_text().splitlines()
        assert lines == ['"A","B"', '"same","row"', '"same","row"']
        assert notes == []

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
        for name in ("meta.json", "result.daily.json", "result.daily.xlsx",
                     "result.daily.pdf", "form.daily.json"):
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
        assert service.export_path("2024-test", "daily", "pdf").is_file()

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

    def test_portfolio_uses_lot_level_soy_snapshot(self, service):
        """A lot-level positions_start (LevelOfDetail=LOT rows with
        OpenDateTime) seeds SOY lots with REAL acquisition dates — the
        portfolio shows per-lot time-test deadlines instead of the
        estimated 31 Dec fallback, and the tax figures stay golden."""
        _seed_synthetic_year(service)
        src = (service.data_dir / "2024" / "positions_start.csv").read_text().splitlines()
        header = src[0] + ",LevelOfDetail,OpenDateTime"
        rows = [line + ",SUMMARY," for line in src[1:]]
        divco = next(line for line in src[1:] if "DIVCO" in line)
        # DIVCO summary: 100 qty / 2500 USD basis -> two lots 40+60
        rows.append(divco.replace(",100,3000,30,2500,", ",40,1200,30,1000,")
                    + ",LOT,2020-06-15;103000")
        rows.append(divco.replace(",100,3000,30,2500,", ",60,1800,30,1500,")
                    + ",LOT,2020-06-16;110000")
        (service.data_dir / "2024" / "positions_start.csv").write_text(
            "\n".join([header] + rows) + "\n")

        meta = service._execute_run(
            "2024-lots", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )
        # DIVCO is never sold — the golden figures must not move
        assert Decimal(meta["summary"]["daily"]["final_tax_czk"]) == Decimal("3604.00")

        [divco_pos] = service.load_portfolio("2024-lots")["positions"]
        lots = divco_pos["lots"]
        assert [(l["acquisition_date"], Decimal(l["quantity"])) for l in lots] == [
            ("2020-06-15", Decimal("40")),
            ("2020-06-16", Decimal("60")),
        ]
        assert all(l["acquisition_estimated"] is False for l in lots)
        assert all(l["time_test_deadline"] is not None for l in lots)

    def test_run_without_dataset_raises(self, service):
        with pytest.raises(ValueError, match="2031"):
            service._execute_run("2031-test", 2031, "daily")

    def test_start_run_validates_fx_mode(self, service):
        with pytest.raises(ValueError, match="režim"):
            service.start_run(2024, "bogus")

    def test_start_run_validates_pairing_method(self, service):
        _seed_synthetic_year(service)
        with pytest.raises(ValueError, match="párovací"):
            service.start_run(2024, "daily", "bogus")

    def test_execute_run_threads_pairing_method(self, service):
        _seed_synthetic_year(service)
        meta = service._execute_run(
            "2024-opt", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
            pairing_method="optimal",
        )
        assert meta["pairing_method"] == "optimal"
        # Single-lot synthetic scenario → optimal == FIFO golden figure.
        assert Decimal(meta["summary"]["daily"]["final_tax_czk"]) == Decimal("3604.00")


class TestComputeCacheAndDashboard:
    def _run(self, service, run_id, **kw):
        return service._execute_run(
            run_id, 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
            **kw,
        )

    def test_run_records_fingerprint_and_accounts(self, service):
        _seed_synthetic_year(service)
        meta = self._run(service, "2024-fp")
        assert meta["account_ids"] == ["U1234567"]        # from the input CSVs
        assert len(meta["input_fingerprint"]) == 64        # sha256 hex digest
        # The portfolio snapshot is account-tagged for future filtering.
        pf = service.load_portfolio("2024-fp")
        assert pf["account_ids"] == ["U1234567"]
        assert all(p["account"] == "U1234567" for p in pf["positions"])

    def test_find_cached_run_matches_only_identical_inputs(self, service):
        _seed_synthetic_year(service)
        self._run(service, "2024-cache")
        assert service.find_cached_run(2024, "daily", "fifo") == "2024-cache"
        # Different params → different fingerprint → no reuse.
        assert service.find_cached_run(2024, "daily", "optimal") is None
        assert service.find_cached_run(2024, "uniform", "fifo") is None

    def test_cache_invalidated_when_source_file_changes(self, service):
        _seed_synthetic_year(service)
        self._run(service, "2024-inv")
        assert service.find_cached_run(2024, "daily", "fifo") == "2024-inv"
        cash = service.data_dir / "2024" / "cash_transactions.csv"
        cash.write_text(cash.read_text() + "\n")   # touch an input
        assert service.find_cached_run(2024, "daily", "fifo") is None

    def test_start_run_reuses_cached_run_without_queuing_job(self, service):
        _seed_synthetic_year(service)
        self._run(service, "2024-hit")
        job_id, run_id = service.start_run(2024, "daily", "fifo")
        assert job_id is None            # cache short-circuit — no job queued
        assert run_id == "2024-hit"

    def test_dashboard_overview_groups_by_year_with_latest(self, service):
        assert service.dashboard_overview()["has_runs"] is False
        _seed_synthetic_year(service)
        self._run(service, "2024-a")
        self._run(service, "2024-b")
        ov = service.dashboard_overview()
        assert ov["has_runs"] is True
        assert ov["accounts"] == ["U1234567"]
        assert [c["tax_year"] for c in ov["year_cards"]] == [2024]  # one card per year
        assert ov["latest"]["run_id"] in {"2024-a", "2024-b"}

    def test_dashboard_latest_tracks_highest_tax_year(self, service, monkeypatch):
        # Newest run *by time* is a re-run of a closed year; the live net-worth
        # card must still track the highest (current) tax year, not regress.
        runs = [
            {"run_id": "2025-rerun", "tax_year": 2025, "account_ids": ["U1"]},  # newest
            {"run_id": "2026-newest", "tax_year": 2026, "account_ids": ["U1"]},
            {"run_id": "2025-first", "tax_year": 2025, "account_ids": ["U1"]},
        ]
        monkeypatch.setattr(service, "list_runs", lambda limit=100: runs)
        ov = service.dashboard_overview()
        assert ov["latest"]["run_id"] == "2026-newest"
        assert [c["tax_year"] for c in ov["year_cards"]] == [2026, 2025]


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


def _long_option():
    """A bought call, numbers lifted from the real 2026 book.

    ``SOFI 280616C00018000``: 1 contract, 31 Dec mark 6.7047 per underlying
    share, FIFO cost 467.04 EUR *per contract*. Its ``eoy_position_value`` in
    the snapshot is 670.47 — price x multiplier — which is what the valuation
    has to reproduce.
    """
    return {
        "symbol": "SOFI  280616C00018000", "description": "SOFI 18 CALL",
        "category": "OPTION", "time_test_applicable": False,
        "quantity_long": "1", "quantity_short": "0", "multiplier": 100,
        "eoy_currency": "USD", "eoy_market_price": "6.7047",
        "eoy_position_value": "670.47", "total_cost_eur": "467.04",
        "lots": [{"acquisition_date": "2025-03-03", "quantity": "1",
                  "unit_cost_eur": "467.04", "acquisition_estimated": False,
                  "time_test_deadline": None}],
        "short_lots": [],
    }


def _short_option():
    """A written put — the liability side, also from the real 2026 book.

    ``SOFI 280616P00015000``: 4 contracts written for 1460.11 EUR of premium,
    now marked at 3.4756 per share. IBKR carries it as ``eoy_position_value``
    -1390.24, and buying it back cheaper than it was sold is a GAIN.
    """
    return {
        "symbol": "SOFI  280616P00015000", "description": "SOFI 15 PUT",
        "category": "OPTION", "time_test_applicable": False,
        "quantity_long": "0", "quantity_short": "4", "multiplier": 100,
        "eoy_currency": "USD", "eoy_market_price": "3.4756",
        "eoy_position_value": "-1390.24", "total_cost_eur": "0",
        "lots": [],
        "short_lots": [{"opening_date": "2025-05-05", "quantity": "4",
                        "unit_proceeds_eur": "365.02",
                        "total_proceeds_eur": "1460.11"}],
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

    def test_option_proceeds_carry_the_contract_multiplier(self, stub_service):
        """Cost per contract vs price per share — the simulator reported a
        467 EUR loss on a contract actually worth 670 USD."""
        sim = stub_service._compute_simulation(
            _long_option(), Decimal("1"), None, _result_with_proceeds()
        )
        assert sim["price_source"] == "eoy"        # no chain data for options
        assert sim["proceeds_czk"] == Decimal("13409.40")   # 6.7047 x100 x20
        assert sim["taxable_gain_czk"] == Decimal("1733.40")
        assert sim["estimated_tax_czk"] == Decimal("260.01")

    def test_option_without_multiplier_refuses_to_simulate(self, stub_service):
        pos = _long_option()
        del pos["multiplier"]
        with pytest.raises(ValueError, match="multiplier"):
            stub_service._compute_simulation(
                pos, Decimal("1"), None, _result_with_proceeds())

    def test_annual_limit_citation_comes_from_the_year_table(self, stub_service):
        """The §4 odst. 1 letter must never be spelled out by hand.

        It was renumbered, the engine cited písm. w) until an advisor corrected
        it (limit = t, time test = u), and the simulation template had drifted to
        a third letter entirely — „§4/1/x", which is neither. Only
        ``paragraph_4_citation`` knows the year-mapped designation.
        """
        from datetime import date as _date

        from src.countries.cz.config import CzTaxConfig

        sim = stub_service._compute_simulation(
            _sim_position(), Decimal("10"), Decimal("12"), _result_with_proceeds())
        citation = sim["annual_limit"]["citation"]
        assert citation == CzTaxConfig().paragraph_4_citation(
            "annual_limit", _date.today().year)
        # The annual limit is písm. t); u) is the time test, and mixing them up
        # is the specific error this guards.
        assert "písm. t)" in citation
        assert "písm. u)" not in citation


class TestParagraph4CitationsInTemplates:
    """No template may invent a §4 odst. 1 letter.

    The letter was renumbered, the engine cited písm. w) until an advisor
    corrected it on 2026-08-05, and `paragraph_4_citation` exists so exactly one
    place knows the year-mapped answer. A template that spells a letter out is
    outside that mechanism — which is how „§4/1/x" survived in the simulation
    card, a letter belonging to neither rule.

    Citing the paragraph WITHOUT a letter stays allowed: that is the deliberate
    fallback for years whose designation is unverified.
    """

    _PATTERNS = (
        re.compile(r"§\s*4\s*/\s*1\s*/\s*([a-zA-Z])"),
        re.compile(r"§\s*4\s+odst\.\s*1\s+písm\.\s*([a-zA-Z])\)"),
    )

    def _known_letters(self):
        from src.countries.cz.config import CzTaxConfig

        return {letter
                for kinds in CzTaxConfig().paragraph_4_letters_by_year.values()
                for letter in kinds.values()}

    def test_every_hardcoded_letter_is_one_the_config_knows(self):
        known = self._known_letters()
        templates = (Path(__file__).resolve().parent.parent
                     / "src" / "webapp" / "templates")
        offenders = []
        for path in templates.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for pattern in self._PATTERNS:
                for match in pattern.finditer(text):
                    if match.group(1) not in known:
                        line = text[:match.start()].count("\n") + 1
                        offenders.append(
                            f"{path.name}:{line} cites písm. {match.group(1)}) "
                            f"— not among {sorted(known)}")
        assert not offenders, (
            "Hardcoded §4 odst. 1 letters that the config does not recognise:\n"
            + "\n".join(offenders))


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


class TestSnapshotSeries:
    """One line must not be drawn out of incompatible series.

    The real portfolio.db held both: 2025-book rows (809k) chronologically
    interleaved with 2026 rows (1.19M), plotted as a 32% crash; and a formula
    change worth ~+11% (1 353 459 -> 1 504 177 overnight) with nothing marking
    the step.
    """

    def _write(self, svc, rows):
        """rows = [(taken_at, tax_year, value, formula_version)]"""
        from src.webapp.services import VALUATION_FORMULA_VERSION  # noqa: F401
        with svc._snapshot_db() as conn:
            conn.executemany(
                "INSERT INTO snapshots (taken_at, tax_year, total_value_czk,"
                " total_cost_czk, quotes_ok, formula_version)"
                " VALUES (?, ?, ?, '', 1, ?)", rows)

    def test_a_different_tax_year_is_left_out(self, stub_service):
        from src.webapp.services import VALUATION_FORMULA_VERSION as V
        self._write(stub_service, [
            ("2026-07-03T10:00:00", 2025, "809647", V),
            ("2026-07-04T10:00:00", 2026, "1185834", V),
            ("2026-07-05T10:00:00", 2026, "1190000", V),
        ])
        s = stub_service.snapshot_series(2026)
        assert [p["total_value_czk"] for p in s["points"]] == ["1185834", "1190000"]
        assert s["excluded_other_years"] == 1
        assert s["excluded_older_formula"] == 0

    def test_an_older_valuation_formula_is_left_out(self, stub_service):
        from src.webapp.services import VALUATION_FORMULA_VERSION as V
        self._write(stub_service, [
            ("2026-08-06T10:00:00", 2026, "1367530", "v1"),
            ("2026-08-07T10:00:00", 2026, "1353459", None),   # pre-column rows
            ("2026-08-08T10:00:00", 2026, "1504177", V),
        ])
        s = stub_service.snapshot_series(2026)
        assert [p["total_value_czk"] for p in s["points"]] == ["1504177"]
        assert s["excluded_older_formula"] == 2

    def test_points_stay_in_chronological_order(self, stub_service):
        from src.webapp.services import VALUATION_FORMULA_VERSION as V
        self._write(stub_service, [
            ("2026-08-08T10:00:00", 2026, "300", V),
            ("2026-08-06T10:00:00", 2026, "100", V),
            ("2026-08-07T10:00:00", 2026, "200", V),
        ])
        s = stub_service.snapshot_series(2026)
        assert [p["total_value_czk"] for p in s["points"]] == ["100", "200", "300"]

    def test_new_snapshots_carry_the_current_formula_version(self, stub_service):
        from src.webapp.services import VALUATION_FORMULA_VERSION as V
        stub_service._compute_live_portfolio(
            {"tax_year": 2025, "positions": [
                {**_sim_position(), "total_cost_eur": "280"}]})
        with stub_service._snapshot_db() as conn:
            versions = [r[0] for r in conn.execute(
                "SELECT formula_version FROM snapshots")]
        assert versions == [V]

    def test_an_unreadable_database_is_empty_not_an_error(self, stub_service):
        stub_service.data_dir.mkdir(parents=True, exist_ok=True)
        (stub_service.data_dir / "portfolio.db").write_bytes(b"not a database")
        assert stub_service.snapshot_series(2026) == {
            "points": [], "excluded_other_years": 0, "excluded_older_formula": 0}


class TestAllocationSlices:
    """Chart.js normalises its input to a full circle, so a bare top-N lied."""

    def _rows(self, n, start=100):
        return [{"symbol": f"S{i}", "value_czk": Decimal(start + i)}
                for i in range(n)]

    def test_everything_fits_below_the_limit(self):
        alloc = RunService.allocation_slices(self._rows(3))
        assert [s["label"] for s in alloc["slices"]] == ["S2", "S1", "S0"]
        assert alloc["folded"] == 0

    def test_the_tail_becomes_one_labelled_slice(self):
        alloc = RunService.allocation_slices(self._rows(15))
        assert len(alloc["slices"]) == 13          # 12 + "ostatní"
        assert alloc["slices"][-1]["label"] == "ostatní (3)"
        assert alloc["folded"] == 3

    def test_the_slices_still_add_up_to_the_whole(self):
        rows = self._rows(15)
        alloc = RunService.allocation_slices(rows)
        assert (sum(Decimal(s["value"]) for s in alloc["slices"])
                == sum(r["value_czk"] for r in rows))

    def test_written_positions_are_excluded_and_disclosed(self):
        """A negative wedge renders broken, and a liability is not a holding."""
        rows = self._rows(2) + [{"symbol": "PUT", "value_czk": Decimal("-500")}]
        alloc = RunService.allocation_slices(rows)
        assert [s["label"] for s in alloc["slices"]] == ["S1", "S0"]
        assert alloc["short_excluded"] == 1
        assert alloc["short_value_czk"] == Decimal("-500")

    def test_unpriced_rows_are_skipped(self):
        rows = self._rows(1) + [{"symbol": "NOPRICE", "value_czk": None}]
        alloc = RunService.allocation_slices(rows)
        assert [s["label"] for s in alloc["slices"]] == ["S0"]
        assert alloc["folded"] == 0


class TestPortfolioBreakdown:
    """Three groupings of the same long value: class, currency, per-row weight."""

    def _rows(self):
        return [
            {"symbol": "A", "category": "STOCK", "live_currency": "USD",
             "value_czk": Decimal("600")},
            {"symbol": "B", "category": "STOCK", "live_currency": "EUR",
             "value_czk": Decimal("300")},
            {"symbol": "C", "category": "OPTION", "eoy_currency": "USD",
             "value_czk": Decimal("100")},
            # A written leg: a liability, so it is not part of what you own.
            {"symbol": "D", "category": "OPTION", "eoy_currency": "USD",
             "value_czk": Decimal("-400")},
            {"symbol": "E", "category": "STOCK", "value_czk": None},
        ]

    def test_splits_by_asset_class(self):
        bd = RunService.portfolio_breakdown(self._rows())
        assert bd["total_czk"] == Decimal("1000")
        assert [(s["label"], s["value"]) for s in bd["by_category"]] == [
            ("STOCK", "900"), ("OPTION", "100")]

    def test_splits_by_currency_preferring_the_priced_one(self):
        bd = RunService.portfolio_breakdown(self._rows())
        assert [(s["label"], s["value"]) for s in bd["by_currency"]] == [
            ("USD", "700"), ("EUR", "300")]

    def test_weights_sum_to_100_and_skip_the_liability(self):
        rows = self._rows()
        RunService.portfolio_breakdown(rows)
        weights = {r["symbol"]: r["weight_pct"] for r in rows}
        assert weights["A"] == Decimal("60")
        assert weights["D"] is None          # written leg
        assert weights["E"] is None          # no price
        assert sum(w for w in weights.values() if w is not None) == Decimal("100")

    def test_percentages_are_json_safe(self):
        """These slices go through tojson into Chart.js, which has no Decimal."""
        import json
        bd = RunService.portfolio_breakdown(self._rows())
        json.dumps(bd["by_category"])
        assert all(isinstance(s["pct"], float) for s in bd["by_category"])

    def test_an_empty_book_does_not_divide_by_zero(self):
        bd = RunService.portfolio_breakdown(
            [{"symbol": "X", "category": "STOCK", "value_czk": None}])
        assert bd["total_czk"] is None
        assert bd["by_category"] == []


class TestCountryBreakdown:
    """Where a holding is from, and how confident we are about it."""

    def _pos(self, symbol, value, isin=None, issuer=None, sub="COMMON",
             category="STOCK"):
        return {"symbol": symbol, "category": category, "isin": isin,
                "issuer_country": issuer, "ibkr_sub": sub,
                "value_czk": Decimal(value)}

    def test_ibkr_issuer_code_wins(self):
        pos = self._pos("BABA", "100", isin="US01609W1027", issuer="cn",
                        sub="ADR")
        assert RunService.resolve_country(pos, {"BABA": "HK"}) == ("CN", "ibkr")

    def test_income_event_beats_the_isin_prefix(self):
        """The real case: BABA's ISIN is American, the issuer is not."""
        pos = self._pos("BABA", "100", isin="US01609W1027", sub="ADR")
        assert RunService.resolve_country(pos, {"BABA": "CN"}) == ("CN", "event")

    def test_an_adr_without_an_income_row_stays_unknown(self):
        """Better a gap than three ADRs silently counted as American.

        BYDDY, DIDIY and NICE all carry US ISINs and paid nothing this year.
        """
        pos = self._pos("BYDDY", "100", isin="US05606L1008", sub="ADR")
        assert RunService.resolve_country(pos, {}) == (None, "unknown")

    def test_isin_prefix_is_used_for_ordinary_shares(self):
        pos = self._pos("RHM", "100", isin="DE0007030009")
        assert RunService.resolve_country(pos, {}) == ("DE", "isin")

    def test_no_isin_and_no_event_is_unknown(self):
        assert RunService.resolve_country(self._pos("X", "100"), {}) == \
            (None, "unknown")

    def test_breakdown_groups_and_counts_its_sources(self):
        rows = [
            self._pos("RHM", "300", isin="DE0007030009"),
            self._pos("TUI1", "200", isin="DE000TUAG505"),
            self._pos("BABA", "400", isin="US01609W1027", sub="ADR"),
            self._pos("NICE", "100", isin="US6536561086", sub="ADR"),
        ]
        bd = RunService.country_breakdown(rows, {"BABA": "CN"})
        assert bd["total_czk"] == Decimal("1000")
        assert [(s["label"], s["value"]) for s in bd["by_country"]] == [
            ("DE", "500"), ("CN", "400"), ("neznámé", "100")]
        assert bd["sources"] == {"isin": 2, "event": 1, "unknown": 1}

    def test_options_are_left_out_of_geography(self):
        """A contract's country is the underlying's; its notional is not."""
        rows = [self._pos("RHM", "300", isin="DE0007030009"),
                self._pos("RHM  280616C00018000", "9000", category="OPTION")]
        bd = RunService.country_breakdown(rows, {})
        assert bd["total_czk"] == Decimal("300")
        assert [s["label"] for s in bd["by_country"]] == ["DE"]

    def test_written_legs_are_left_out_like_everywhere_else(self):
        rows = [self._pos("RHM", "300", isin="DE0007030009"),
                self._pos("TUI1", "-500", isin="DE000TUAG505")]
        bd = RunService.country_breakdown(rows, {})
        assert bd["total_czk"] == Decimal("300")

    def test_rows_carry_their_own_attribution(self):
        rows = [self._pos("RHM", "300", isin="DE0007030009")]
        RunService.country_breakdown(rows, {})
        assert (rows[0]["country"], rows[0]["country_source"]) == ("DE", "isin")

    def test_event_countries_read_from_the_persisted_result(self, service):
        from src.webapp.serializers import dump_json
        run_dir = service.runs_dir / "ec-run"
        run_dir.mkdir(parents=True)
        dump_json({"items": [
            {"asset_symbol": "BABA", "source_country": "CN"},
            {"asset_symbol": "BABA", "source_country": "HK"},   # first wins
            {"asset_symbol": "NOCOUNTRY", "source_country": None},
        ]}, run_dir / "result.daily.json")
        assert service.event_countries("ec-run", "daily") == {"BABA": "CN"}


class TestDividendSummaryCountry:
    """The country must not depend on which payout happens to come first."""

    def _run_with(self, svc, items):
        from src.webapp.serializers import dump_json
        run_dir = svc.runs_dir / "2025-test"
        run_dir.mkdir(parents=True, exist_ok=True)
        dump_json({"items": items}, run_dir / "result.daily.json")
        return "2025-test"

    def _div(self, month, country, gross="100"):
        return {"item_type": "DIVIDEND", "asset_symbol": "PYPL",
                "asset_description": "PayPal", "source_country": country,
                "amount_czk": gross, "wht_total_czk": "0",
                "event_date": f"2025-{month}-15"}

    def test_country_backfilled_from_a_later_payout(self, stub_service):
        # January withheld nothing, so that row carries no country; the three
        # later US payouts do. setdefault only ever saw the January one.
        run_id = self._run_with(stub_service, [
            self._div("01", None), self._div("04", "US"),
            self._div("07", "US"), self._div("10", "US"),
        ])
        summary = stub_service.dividend_summary(run_id, "daily")
        assert summary["assets"][0]["country"] == "US"

    def test_country_stays_none_when_no_payout_knows_it(self, stub_service):
        run_id = self._run_with(stub_service, [self._div("01", None)])
        summary = stub_service.dividend_summary(run_id, "daily")
        assert summary["assets"][0]["country"] is None


class TestDividendWithholdingRates:
    """Is it worth reclaiming? Answered from the FTC record already on the item."""

    def _run_with(self, svc, items):
        from src.webapp.serializers import dump_json
        run_dir = svc.runs_dir / "2025-wht"
        run_dir.mkdir(parents=True, exist_ok=True)
        dump_json({"items": items}, run_dir / "result.daily.json")
        return "2025-wht"

    def _div(self, symbol, gross, wht, creditable, cap="0.15", month="05"):
        return {
            "item_type": "DIVIDEND", "asset_symbol": symbol,
            "asset_description": symbol, "source_country": "XX",
            "amount_czk": gross, "wht_total_czk": wht,
            "event_date": f"2025-{month}-15",
            "ftc": {"actual_creditable_czk": creditable,
                    "non_creditable_czk": str(Decimal(wht) - Decimal(creditable)),
                    "configured_cap_rate": cap},
        }

    def _asset(self, svc, *items):
        run = self._run_with(svc, list(items))
        return svc.dividend_summary(run, "daily")

    def test_over_withheld_payer_is_flagged_with_the_reclaimable_amount(
            self, stub_service):
        # The real EVO row: 30% taken by Sweden against a 15% treaty cap.
        s = self._asset(stub_service,
                        self._div("EVO", "1395.52", "418.66", "209.33"))
        a = s["assets"][0]
        assert a["effective_rate"] == Decimal("418.66") / Decimal("1395.52")
        assert round(a["effective_rate"], 3) == Decimal("0.300")
        assert a["cap_rate"] == Decimal("0.15")
        assert a["over_treaty"] is True
        assert a["excess_czk"] == Decimal("209.33")

    def test_a_payer_at_the_treaty_rate_is_not_flagged(self, stub_service):
        s = self._asset(stub_service,
                        self._div("PYPL", "408.54", "61.28", "61.28"))
        assert s["assets"][0]["over_treaty"] is False

    def test_rounding_noise_does_not_raise_the_flag(self, stub_service):
        """The real CVS row: three payouts, per-item caps rounded to hellers,
        so the year lands at 15.03% with nothing actually over-withheld."""
        s = self._asset(stub_service,
                        self._div("CVS", "230.07", "34.58", "34.51"))
        a = s["assets"][0]
        assert a["effective_rate"] > a["cap_rate"]        # genuinely above...
        assert a["over_treaty"] is False                  # ...but not by enough
        assert a["excess_czk"] == Decimal("0.07")         # still reported

    def test_rates_aggregate_over_the_whole_year(self, stub_service):
        """Several payouts at different rates: what matters for a refund
        claim is the aggregate, not any single payment."""
        s = self._asset(
            stub_service,
            self._div("X", "100.00", "30.00", "15.00", month="03"),
            self._div("X", "100.00", "15.00", "15.00", month="09"),
        )
        a = s["assets"][0]
        assert a["count"] == 2
        assert a["effective_rate"] == Decimal("0.225")    # 45 / 200
        assert a["over_treaty"] is True
        assert a["excess_czk"] == Decimal("15.00")

    def test_totals_split_creditable_from_reclaimable(self, stub_service):
        s = self._asset(stub_service,
                        self._div("EVO", "1395.52", "418.66", "209.33"),
                        self._div("PYPL", "408.54", "61.28", "61.28"))
        assert s["total_wht_czk"] == Decimal("479.94")
        assert s["total_creditable_czk"] == Decimal("270.61")
        assert s["total_excess_czk"] == Decimal("209.33")

    def test_a_defaulted_treaty_rate_is_flagged_for_the_page(self, stub_service):
        """15% is both the default and a real cap, so the column would present
        an unverified guess exactly like a verified rate."""
        run = self._run_with(stub_service, [
            {**self._div("EVO", "1395.52", "418.66", "209.33"),
             "ftc": {"actual_creditable_czk": "209.33",
                     "non_creditable_czk": "209.33",
                     "configured_cap_rate": "0.15",
                     "cap_rate_defaulted": True}},
        ])
        a = stub_service.dividend_summary(run, "daily")["assets"][0]
        assert a["cap_rate"] == Decimal("0.15")
        assert a["cap_defaulted"] is True

    def test_a_verified_treaty_rate_is_not_flagged(self, stub_service):
        run = self._run_with(stub_service, [
            self._div("PYPL", "408.54", "61.28", "61.28")])
        assert stub_service.dividend_summary(run, "daily")[
            "assets"][0]["cap_defaulted"] is False

    def test_a_run_without_ftc_records_still_renders(self, stub_service):
        """Older runs predate the per-item ftc block."""
        run = self._run_with(stub_service, [
            {"item_type": "DIVIDEND", "asset_symbol": "OLD",
             "amount_czk": "100.00", "wht_total_czk": "15.00",
             "event_date": "2025-05-15"},
        ])
        a = stub_service.dividend_summary(run, "daily")["assets"][0]
        assert a["cap_rate"] is None
        assert a["over_treaty"] is False
        assert a["creditable_czk"] == Decimal("0.00")


class TestOptionValuation:
    """An option is quoted per underlying share but held in contracts of 100.

    Its FIFO cost is already per contract, so dropping the multiplier put the
    two legs 100x apart and every contract showed as 1% of its worth.
    """

    def _live(self, svc, *positions):
        return svc._compute_live_portfolio(
            {"tax_year": 2026, "positions": list(positions)})

    def test_long_contract_values_at_price_times_multiplier(self, stub_service):
        live = self._live(stub_service, _long_option())
        row = live["positions"][0]
        # 1 x 6.7047 x 100 = 670.47 — exactly IBKR's own eoy_position_value
        assert row["value_ccy"] == Decimal(_long_option()["eoy_position_value"])
        assert row["value_czk"] == Decimal("13409.40")        # x20 USD
        assert row["cost_czk"] == Decimal("11676.00")         # 467.04 EUR x25
        assert row["unrealized_czk"] == Decimal("1733.40")
        assert row["price_source"] == "eoy"

    def test_written_contract_is_a_liability_and_the_premium_is_its_cost(
            self, stub_service):
        live = self._live(stub_service, _short_option())
        row = live["positions"][0]
        assert row["net_quantity"] == Decimal("-4")
        assert row["value_ccy"] == Decimal("-1390.24")        # matches IBKR
        assert row["value_czk"] == Decimal("-27804.80")
        # Premium received is a credit, so the cost side is negative...
        assert row["cost_czk"] == Decimal("-36502.75")
        # ...which makes "sold high, now cheaper" come out as a gain.
        assert row["unrealized_czk"] == Decimal("8697.95")
        assert row["unrealized_pct"] > 0

    def test_written_contract_reaches_the_totals_at_all(self, stub_service):
        """It used to be skipped outright, so net worth was overstated."""
        live = self._live(stub_service, _long_option(), _short_option())
        assert [p["symbol"] for p in live["positions"]] == [
            _long_option()["symbol"], _short_option()["symbol"]]
        assert live["total_value_czk"] == Decimal("13409.40") - Decimal("27804.80")

    def test_missing_multiplier_is_refused_not_guessed(self, stub_service):
        """A contract from a pre-metadata run: no number beats a 100x-light one."""
        pos = _long_option()
        del pos["multiplier"]
        row = self._live(stub_service, pos)["positions"][0]
        assert row["price_source"] == "none"
        assert row["value_czk"] is None

    def test_eoy_priced_options_are_counted_for_the_freshness_note(
            self, stub_service):
        live = self._live(stub_service, _long_option(), _short_option())
        # Neither is quotable, so the live ratio must not claim them as fresh.
        assert live["options_at_eoy"] == 2
        assert live["quotes_ok"] == 0
        assert live["quotes_total"] == 0


class TestQuoteFetching:
    """One HTTP round trip per symbol, so they go out concurrently.

    The sell-zone ladder pays for these inline before it can render; the real
    portfolio's 24 holdings took 4.5 s one at a time.
    """

    @staticmethod
    def _quote(symbol):
        return SimpleNamespace(ibkr_symbol=symbol, yahoo_symbol=symbol,
                               price=Decimal("10"), currency="USD", fetched_at=0.0)

    def _pf(self, count):
        return {"tax_year": 2025, "positions": [
            {"symbol": f"S{i}", "description": "x", "category": "STOCK",
             "time_test_applicable": True, "quantity_long": "1",
             "eoy_currency": "USD", "eoy_market_price": "9",
             "total_cost_eur": "5", "lots": []}
            for i in range(count)
        ]}

    def _service(self, tmp_path, quotes):
        return RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
                          quote_service=quotes, converter_factory=StubConverter)

    def test_every_symbol_is_in_flight_at_once(self, tmp_path):
        """A barrier is the proof — fetching one at a time could never pass it."""
        barrier = threading.Barrier(3, timeout=5)
        quote = self._quote

        class BarrierQuotes:
            def get_quote(self, symbol, currency):
                barrier.wait()      # releases only once all three have arrived
                return quote(symbol)

        svc = self._service(tmp_path, BarrierQuotes())
        try:
            live = svc._compute_live_portfolio(self._pf(3))
        finally:
            svc.runner.shutdown(wait=False)
        assert live["quotes_ok"] == 3        # no BrokenBarrierError ⇒ concurrent

    def test_one_symbol_held_twice_costs_one_request(self, tmp_path):
        quotes = _CountingQuotes(self._quote)
        pf = self._pf(1)
        pf["positions"].append(dict(pf["positions"][0]))   # e.g. a second account
        svc = self._service(tmp_path, quotes)
        try:
            live = svc._compute_live_portfolio(pf)
        finally:
            svc.runner.shutdown(wait=False)
        assert quotes.calls == [("S0", "USD")]
        assert live["quotes_ok"] == 2         # both rows still priced

    def test_options_and_closed_positions_cost_nothing(self, tmp_path):
        quotes = _CountingQuotes(self._quote)
        pf = self._pf(1)
        pf["positions"] += [
            {"symbol": "OPT", "category": "OPTION", "quantity_long": "2",
             "quantity_short": "0", "eoy_currency": "USD", "lots": []},
            {"symbol": "SOLD", "category": "STOCK", "quantity_long": "0",
             "eoy_currency": "USD", "eoy_market_price": "5", "lots": []},
        ]
        svc = self._service(tmp_path, quotes)
        try:
            svc._compute_live_portfolio(pf)
        finally:
            svc.runner.shutdown(wait=False)
        assert quotes.calls == [("S0", "USD")]

    def test_a_short_only_holding_is_still_quoted(self, tmp_path):
        """The prefetch and the valuation loop carry the same skip rule.

        They are two copies of one condition; when the loop learned to value
        written positions, a prefetch still keyed on ``quantity_long`` would
        have dropped them onto the EOY fallback without a word.
        """
        quotes = _CountingQuotes(self._quote)
        pf = self._pf(0)
        pf["positions"].append(
            {"symbol": "SHORTED", "description": "x", "category": "STOCK",
             "time_test_applicable": True, "quantity_long": "0",
             "quantity_short": "5", "eoy_currency": "USD",
             "eoy_market_price": "9", "total_cost_eur": "0",
             "lots": [], "short_lots": []})
        svc = self._service(tmp_path, quotes)
        try:
            live = svc._compute_live_portfolio(pf)
        finally:
            svc.runner.shutdown(wait=False)
        assert quotes.calls == [("SHORTED", "USD")]
        assert live["positions"][0]["price_source"] == "live"

    def test_a_broken_quote_service_still_surfaces(self, tmp_path):
        """Not swallowed into a silent EOY fallback, same as before the pool."""
        class BoomQuotes:
            def get_quote(self, symbol, currency):
                raise RuntimeError("yahoo down")

        svc = self._service(tmp_path, BoomQuotes())
        try:
            with pytest.raises(RuntimeError, match="yahoo down"):
                svc._compute_live_portfolio(self._pf(3))
        finally:
            svc.runner.shutdown(wait=False)


class _CountingQuotes:
    """Records what was asked for. ``calls`` is appended from pool threads;
    list.append is atomic, and the assertions never depend on the order."""

    def __init__(self, factory):
        self.calls = []
        self._factory = factory

    def get_quote(self, symbol, currency):
        self.calls.append((symbol, currency))
        return self._factory(symbol)


class TestOptionsOverview:
    def _write_portfolio(self, svc, positions):
        from src.webapp.serializers import dump_json
        run_dir = svc.runs_dir / "opt-run"
        run_dir.mkdir(parents=True)
        dump_json({"tax_year": 2025, "positions": positions}, run_dir / "portfolio.json")

    def test_options_only_sorted_by_expiry_with_days_and_net(self, service):
        # A far Call, a near short Put, a non-option stock, and an undated Call.
        self._write_portfolio(service, [
            {"symbol": "STOCKX", "category": "STOCK", "quantity_long": "10",
             "quantity_short": "0"},
            {"symbol": "FAR", "category": "OPTION", "option_type": "C",
             "strike_price": "20", "expiry_date": "2099-01-01",
             "eoy_currency": "USD", "quantity_long": "2.00000000",
             "quantity_short": "0", "underlying_symbol": "AAA"},
            {"symbol": "SHORTPUT", "category": "OPTION", "option_type": "P",
             "strike_price": "6.8", "expiry_date": "2000-01-01",
             "eoy_currency": "EUR", "quantity_long": "0",
             "quantity_short": "1.00000000", "underlying_symbol": "BBB"},
            {"symbol": "NODATE", "category": "OPTION", "option_type": "C",
             "strike_price": "5", "expiry_date": None,
             "quantity_long": "1", "quantity_short": "0"},
        ])
        overview = service.options_overview("opt-run", with_quotes=False)
        # Days count from the tax-year end (2025-12-31), not today — the FIFO
        # book is a year-end snapshot.
        assert overview["as_of"] == "2025-12-31"
        rows = overview["options"]

        # Stock is excluded; options only, sorted by expiry ascending (undated last).
        assert [r["symbol"] for r in rows] == ["SHORTPUT", "FAR", "NODATE"]

        short = rows[0]
        assert short["net_quantity"] == Decimal("-1")
        assert short["net_display"] == "-1"            # FIFO tail zeros trimmed
        assert short["expired"] is True                # 2000 predates the 2025 year-end
        assert short["days_to_expiry"] < 0
        assert short["option_type"] == "P"

        far = rows[1]
        assert far["net_display"] == "2"
        assert far["expired"] is False
        assert far["days_to_expiry"] > 0

        undated = rows[2]
        assert undated["expiry_date"] is None
        assert undated["days_to_expiry"] is None
        assert undated["expired"] is False

    def test_expiry_after_year_end_is_not_flagged_expired(self, service):
        # A contract open at 31 Dec 2025 that expires in Jan 2026 (like the real
        # SPYM 16JAN26 put) must read as ~16 days left, NOT expired-vs-today.
        self._write_portfolio(service, [
            {"symbol": "SPYM 260116P00078000", "category": "OPTION",
             "option_type": "P", "strike_price": "78", "expiry_date": "2026-01-16",
             "eoy_currency": "USD", "quantity_long": "2", "quantity_short": "0"},
        ])
        [row] = service.options_overview(
            "opt-run", with_quotes=False)["options"]
        assert row["expired"] is False
        assert row["days_to_expiry"] == 16  # 2025-12-31 -> 2026-01-16

    def test_options_overview_missing_run_is_empty(self, service):
        assert service.options_overview("does-not-exist") == {
            "as_of": None, "tax_year": None, "options": [], "historical": False}


class TestOptionKeyParsing:
    """The positions statement exports no Strike, Expiry or Put/Call, so a
    positions-only refresh reads them back out of the contract key."""

    def test_occ_key_underlying_first(self):
        from src.webapp.services import parse_option_key
        assert parse_option_key("NU    280121C00013000") == {
            "option_type": "C", "expiry_date": "2028-01-21",
            "strike_price": Decimal("13")}

    def test_occ_strike_is_in_thousandths(self):
        from src.webapp.services import parse_option_key
        assert parse_option_key("SPYM  270115P00080500")["strike_price"] == \
            Decimal("80.5")

    def test_marker_first_european_key(self):
        from src.webapp.services import parse_option_key
        assert parse_option_key("P TUI  20260918 6.8 M") == {
            "option_type": "P", "expiry_date": "2026-09-18",
            "strike_price": Decimal("6.8")}

    @pytest.mark.parametrize("key", ["", "PLAINSTOCK", "NU 999999C00013000",
                                     "C TUI  20261340 8 M"])
    def test_unparseable_keys_are_not_guessed(self, key):
        from src.webapp.services import parse_option_key
        assert parse_option_key(key) == {
            "option_type": None, "strike_price": None, "expiry_date": None}


class TestOptionsFromPositions:
    """Today's contracts without a pipeline run."""

    HEADER = ("ClientAccountID,CurrencyPrimary,AssetClass,SubCategory,Symbol,"
              "Description,Conid,ISIN,UnderlyingSymbol,Multiplier,Quantity,"
              "MarkPrice,PositionValue,CostBasisMoney,UnderlyingConid,"
              "LevelOfDetail,OpenDateTime,HoldingPeriodDateTime")

    def _write(self, svc, rows, year=2026):
        year_dir = svc.data_dir / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        (year_dir / "positions_end.csv").write_text(
            "\n".join([self.HEADER, *rows]) + "\n", encoding="utf-8")

    def _row(self, symbol, qty, under="SOFI", detail="SUMMARY", cls="OPT"):
        return (f"U1,USD,{cls},,{symbol},{symbol} DESC,1,,{under},100,{qty},"
                f"1.5,150,100,2,{detail},,")

    def test_signed_quantity_becomes_a_long_or_written_leg(self, service):
        self._write(service, [
            self._row("SOFI  280616C00018000", "2"),
            self._row("SOFI  280616P00015000", "-4"),
        ])
        rows = {r["symbol"]: r for r in service.options_from_positions(
            2026, with_quotes=False)["options"]}

        long_leg = rows["SOFI  280616C00018000"]
        assert (long_leg["quantity_long"], long_leg["quantity_short"]) == \
            (Decimal("2"), Decimal("0"))
        written = rows["SOFI  280616P00015000"]
        assert (written["quantity_long"], written["quantity_short"]) == \
            (Decimal("0"), Decimal("4"))
        assert written["net_display"] == "-4"

    def test_strike_expiry_and_type_come_from_the_key(self, service):
        self._write(service, [self._row("SOFI  280616P00015000", "-1")])
        [row] = service.options_from_positions(2026, with_quotes=False)["options"]
        assert row["option_type"] == "P"
        assert row["strike_price"] == Decimal("15")
        assert row["expiry_date"] == "2028-06-16"

    def test_lot_rows_do_not_double_the_position(self, service):
        """SUMMARY is the position; LOT repeats it per acquisition."""
        self._write(service, [
            self._row("SOFI  280616C00018000", "3", detail="SUMMARY"),
            self._row("SOFI  280616C00018000", "1", detail="LOT"),
            self._row("SOFI  280616C00018000", "2", detail="LOT"),
        ])
        [row] = service.options_from_positions(2026, with_quotes=False)["options"]
        assert row["net_quantity"] == Decimal("3")

    def test_lot_only_files_still_work(self, service):
        self._write(service, [
            self._row("SOFI  280616C00018000", "1", detail="LOT"),
            self._row("SOFI  280616C00018000", "2", detail="LOT"),
        ])
        rows = service.options_from_positions(2026, with_quotes=False)["options"]
        assert [r["net_quantity"] for r in rows] == [Decimal("1"), Decimal("2")]

    def test_non_options_and_closed_rows_are_dropped(self, service):
        self._write(service, [
            self._row("SOFI", "100", cls="STK"),
            self._row("SOFI  280616C00018000", "0"),
        ])
        assert service.options_from_positions(
            2026, with_quotes=False)["options"] == []

    def test_days_count_from_today_not_a_year_end(self, service):
        """This file is a snapshot of right now — that is the point of it."""
        from datetime import date as _date, timedelta as _timedelta
        expiry = _date.today() + _timedelta(days=10)
        key = f"SOFI  {expiry:%y%m%d}C00018000"
        self._write(service, [self._row(key, "1")], year=2026)
        [row] = service.options_from_positions(2026, with_quotes=False)["options"]
        assert row["days_to_expiry"] == 10
        assert row["expired"] is False

    def test_a_missing_statement_is_empty_not_an_error(self, service):
        out = service.options_from_positions(2099, with_quotes=False)
        assert out["options"] == [] and out["age_hours"] is None

    @staticmethod
    def _this_year():
        from datetime import date as _date
        return _date.today().year

    def _arm_flex(self, service, monkeypatch, payload=None):
        from src.webapp import services as services_mod
        from src.webapp.ibkr_flex import FlexConfig, save_flex_config

        save_flex_config(service.flex_config_path,
                         FlexConfig(token="tok", queries={"positions": "42"}))
        calls = []

        def fake_fetch(token, query_id, from_date=None, to_date=None):
            calls.append((token, query_id))
            return payload if payload is not None else (
                self.HEADER + "\n"
                + self._row("SOFI  280616P00015000", "-1") + "\n").encode()

        monkeypatch.setattr(services_mod, "fetch_statement", fake_fetch)
        return calls

    def test_refresh_writes_the_statement_without_running_the_engine(
            self, service, monkeypatch):
        year = self._this_year()
        calls = self._arm_flex(service, monkeypatch)
        service.refresh_positions_sync(year)

        assert calls == [("tok", "42")]
        [row] = service.options_from_positions(year, with_quotes=False)["options"]
        assert row["quantity_short"] == Decimal("1")
        # No run was created — the tax figures are untouched.
        assert service.list_runs() == []

    def test_a_closed_year_is_refused_before_anything_is_fetched(
            self, service, monkeypatch):
        """The dashboard shows the newest run, which in filing season is the
        year just closed. Its positions_end.csv is the EOY input for every
        re-run, the seed for the next year's opening lots and part of the
        cache fingerprint — today's holdings must never land on it."""
        calls = self._arm_flex(service, monkeypatch)
        closed = self._this_year() - 1

        with pytest.raises(ValueError, match=str(closed)):
            service.refresh_positions_sync(closed)
        assert calls == []                      # refused before the round trip

    def test_omitting_the_year_refreshes_the_current_one(
            self, service, monkeypatch):
        self._arm_flex(service, monkeypatch)
        out = service.refresh_positions_sync()
        assert out["tax_year"] == self._this_year()

    def test_the_previous_statement_is_kept_as_a_backup(
            self, service, monkeypatch):
        year = self._this_year()
        year_dir = service.data_dir / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        original = year_dir / "positions_end.csv"
        original.write_text("PŮVODNÍ", encoding="utf-8")

        self._arm_flex(service, monkeypatch)
        service.refresh_positions_sync(year)

        assert original.read_text(encoding="utf-8") != "PŮVODNÍ"
        assert (year_dir / "positions_end.csv.bak").read_text(
            encoding="utf-8") == "PŮVODNÍ"

    def test_refresh_without_a_positions_query_explains_itself(self, service):
        with pytest.raises(ValueError, match="positions"):
            service.refresh_positions_sync(self._this_year())


class TestAssignmentRisk:
    """A written contract in the money can be assigned; a bought one cannot.

    The old expiry badge was purely time-based and lit up on long contracts
    too, where there is nothing to act on.
    """

    def _svc(self, tmp_path, prices):
        return RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
                          quote_service=StubQuotes(prices),
                          converter_factory=StubConverter)

    def _contract(self, symbol, kind, strike, qty_short, days, underlying="XYZ",
                  qty_long="0"):
        from datetime import date as _date, timedelta as _timedelta
        # Days are measured from min(today, 31 Dec of the tax year); with the
        # tax year set to today's, that is today.
        expiry = (_date.today() + _timedelta(days=days)).isoformat()
        return {"symbol": symbol, "category": "OPTION", "option_type": kind,
                "strike_price": strike, "expiry_date": expiry,
                "eoy_currency": "USD", "multiplier": 100,
                "quantity_long": qty_long, "quantity_short": qty_short,
                "underlying_symbol": underlying}

    def _rows(self, tmp_path, positions, prices={"XYZ": (Decimal("20"), "USD")}):
        from datetime import date as _date
        from src.webapp.serializers import dump_json
        svc = self._svc(tmp_path, prices)
        run_dir = svc.runs_dir / "risk-run"
        run_dir.mkdir(parents=True)
        dump_json({"tax_year": _date.today().year, "positions": positions},
                  run_dir / "portfolio.json")
        try:
            overview = svc.options_overview("risk-run")
        finally:
            svc.runner.shutdown(wait=False)
        return overview

    def test_written_itm_put_close_to_expiry_is_urgent(self, tmp_path):
        # Spot 20, strike 25 → the put is 5 in the money, 20% of strike.
        ov = self._rows(tmp_path, [
            self._contract("P25", "P", "25", "1", days=3)])
        [row] = ov["options"]
        assert row["underlying_price"] == Decimal("20")
        assert row["intrinsic_value"] == Decimal("5")
        assert row["moneyness_pct"] == Decimal("20")
        assert row["in_the_money"] is True
        assert row["assignment_risk"] == "high"
        assert ov["at_risk"] == 1

    def test_written_itm_call_further_out_is_elevated_then_watch(self, tmp_path):
        # Spot 20, strike 15 → the call is 5 in the money.
        ov = self._rows(tmp_path, [
            self._contract("C15a", "C", "15", "1", days=20),
            self._contract("C15b", "C", "15", "1", days=200),
        ])
        by = {r["symbol"]: r for r in ov["options"]}
        assert by["C15a"]["assignment_risk"] == "elevated"
        assert by["C15b"]["assignment_risk"] == "watch"
        assert ov["at_risk"] == 1          # only the near one counts

    def test_written_otm_contract_is_explicitly_safe(self, tmp_path):
        ov = self._rows(tmp_path, [
            self._contract("P15", "P", "15", "1", days=3)])
        [row] = ov["options"]
        assert row["in_the_money"] is False
        assert row["moneyness_pct"] == Decimal("-100") / Decimal("3")  # (15-20)/15
        assert row["assignment_risk"] == "none"
        assert ov["at_risk"] == 0

    def test_a_bought_contract_carries_no_assignment_risk(self, tmp_path):
        """Being long an in-the-money option is the good case."""
        ov = self._rows(tmp_path, [
            self._contract("C15", "C", "15", "0", days=3, qty_long="1")])
        [row] = ov["options"]
        assert row["in_the_money"] is True
        assert row["assignment_risk"] is None

    def test_an_expired_contract_is_not_re_flagged(self, tmp_path):
        ov = self._rows(tmp_path, [
            self._contract("P25", "P", "25", "1", days=-5)])
        [row] = ov["options"]
        assert row["expired"] is True
        assert row["assignment_risk"] is None

    def test_a_missing_quote_leaves_moneyness_unknown(self, tmp_path):
        """No guessing: an unmapped underlying yields no risk verdict at all."""
        ov = self._rows(tmp_path, [
            self._contract("P25", "P", "25", "1", days=3, underlying="NOPE")])
        [row] = ov["options"]
        assert row["underlying_price"] is None
        assert row["in_the_money"] is None
        assert row["assignment_risk"] is None
        assert ov["quotes_ok"] == 0

    def test_a_closed_years_book_is_never_priced(self, tmp_path):
        """days_to_expiry is frozen at 31 December by design, so pairing it with
        today's spot yields a live-looking verdict out of two unrelated moments:
        a January expiry still reads "16 days left" in February, and a put
        closed months ago would be badged 'hrozí přiřazení'."""
        from datetime import date as _date
        from src.webapp.serializers import dump_json

        quotes = _CountingQuotes(
            lambda s: SimpleNamespace(ibkr_symbol=s, yahoo_symbol=s,
                                      price=Decimal("20"), currency="USD",
                                      fetched_at=0.0))
        svc = RunService(data_dir=tmp_path / "d", runs_dir=tmp_path / "r",
                         quote_service=quotes, converter_factory=StubConverter)
        run_dir = svc.runs_dir / "old-run"
        run_dir.mkdir(parents=True)
        closed = _date.today().year - 1
        dump_json({"tax_year": closed, "positions": [
            self._contract("P25", "P", "25", "1", days=3),
        ]}, run_dir / "portfolio.json")
        try:
            ov = svc.options_overview("old-run")
        finally:
            svc.runner.shutdown(wait=False)

        assert ov["historical"] is True
        assert quotes.calls == []                 # no round trip at all
        [row] = ov["options"]
        assert row["underlying_price"] is None
        assert row["moneyness_pct"] is None
        assert row["assignment_risk"] is None
        # The expiry record itself survives — that is what the book is for.
        assert row["symbol"] == "P25" and row["expiry_date"]

    def test_the_running_year_is_still_priced(self, tmp_path):
        ov = self._rows(tmp_path, [
            self._contract("P25", "P", "25", "1", days=3)])
        assert ov["historical"] is False
        assert ov["options"][0]["assignment_risk"] == "high"

    def test_one_request_per_underlying_not_per_contract(self, tmp_path):
        quotes = _CountingQuotes(
            lambda s: SimpleNamespace(ibkr_symbol=s, yahoo_symbol=s,
                                      price=Decimal("20"), currency="USD",
                                      fetched_at=0.0))
        from datetime import date as _date
        from src.webapp.serializers import dump_json
        svc = RunService(data_dir=tmp_path / "d", runs_dir=tmp_path / "r",
                         quote_service=quotes, converter_factory=StubConverter)
        run_dir = svc.runs_dir / "risk-run"
        run_dir.mkdir(parents=True)
        dump_json({"tax_year": _date.today().year, "positions": [
            self._contract("A", "C", "15", "1", days=10),
            self._contract("B", "P", "25", "1", days=20),
            self._contract("C", "C", "30", "1", days=30),
        ]}, run_dir / "portfolio.json")
        try:
            svc.options_overview("risk-run")
        finally:
            svc.runner.shutdown(wait=False)
        assert quotes.calls == [("XYZ", "USD")]     # three contracts, one XYZ


class TestDisposalsAndCompare:
    """disposal_summary + compare_runs over fabricated persisted runs.

    Fabricated results let us pin the delta arithmetic (proceeds vs cost
    decomposition) — the synthetic golden year has one lot per symbol, so
    FIFO ≡ LIFO there and the changed-rows path would go untested.
    """

    # Marker value per FX mode so tests can detect WHICH result.<mode>.json
    # a reader actually opened.
    MODE_MARKER = {"daily": "1.00", "uniform": "2.00", "compare": "3.00"}

    def _write_run(self, svc, run_id, meta_extra, items,
                   liability=None, modes=("daily",), legacy_meta=False):
        from src.webapp.serializers import dump_json
        run_dir = svc.runs_dir / run_id
        run_dir.mkdir(parents=True)
        meta = {"run_id": run_id, "tax_year": 2026, "fx_mode": "daily",
                "pairing_method": "fifo",
                "created_at": "2026-01-01T00:00:00+00:00",
                **({} if legacy_meta else {"modes": list(modes)}),
                **meta_extra}
        dump_json(meta, run_dir / "meta.json")
        for mode in modes:
            result = {
                "sections": {"cz_tax_liability": {"line_items": {
                    "taxable_interest_czk": self.MODE_MARKER[mode],
                    **(liability or {})}}},
                "items": items,
            }
            dump_json(result, run_dir / f"result.{mode}.json")

    @staticmethod
    def _sale(symbol, gain, proceeds, cost, *, category="STOCK",
              item_type="SECURITY_DISPOSAL", **extra):
        return {"item_type": item_type, "asset_symbol": symbol,
                "asset_description": f"{symbol} DESC",
                "asset_category": category, "gain_loss_czk": gain,
                "proceeds_czk": proceeds, "cost_basis_czk": cost,
                "quantity": "10", "event_date": "2026-03-01",
                "acquisition_date": "2025-01-01", "holding_period_days": 424,
                "is_taxable": True, "is_exempt": False, **extra}

    def test_default_sort_ranks_the_biggest_gain_first(self, service):
        """"What did I earn on" — not "what moved most".

        Ordering by magnitude put a 900 loss above a 400 win, which is the
        opposite of what the page is asked.
        """
        self._write_run(service, "run-s", {}, [
            self._sale("WIN", "400.00", "1400.00", "1000.00"),
            self._sale("LOSS", "-900.00", "100.00", "1000.00"),
            self._sale("MID", "50.00", "150.00", "100.00"),
        ])
        data = service.disposal_summary("run-s", "daily")
        assert [a["symbol"] for a in data["by_symbol"]] == ["WIN", "MID", "LOSS"]
        assert data["sort"] == "gain_desc"

    def test_other_sort_orders(self, service):
        self._write_run(service, "run-s2", {}, [
            self._sale("WIN", "400.00", "1400.00", "1000.00"),
            self._sale("LOSS", "-900.00", "100.00", "1000.00"),
            self._sale("MID", "50.00", "150.00", "100.00"),
        ])
        order = lambda s: [a["symbol"] for a in                     # noqa: E731
                           service.disposal_summary("run-s2", "daily", sort=s)["by_symbol"]]
        assert order("gain_asc") == ["LOSS", "MID", "WIN"]
        assert order("abs_desc") == ["LOSS", "WIN", "MID"]
        assert order("proceeds_desc") == ["WIN", "MID", "LOSS"]
        assert order("symbol") == ["LOSS", "MID", "WIN"]

    def test_unknown_sort_falls_back_to_the_default(self, service):
        self._write_run(service, "run-s3", {}, [
            self._sale("WIN", "400.00", "1400.00", "1000.00"),
            self._sale("LOSS", "-900.00", "100.00", "1000.00"),
        ])
        data = service.disposal_summary("run-s3", "daily", sort="nonsense")
        assert [a["symbol"] for a in data["by_symbol"]] == ["WIN", "LOSS"]

    def test_quantities_lose_their_fifo_tail_zeros(self, service):
        self._write_run(service, "run-q", {}, [
            self._sale("ABC", "10.00", "110.00", "100.00", quantity="100.00000000"),
        ])
        data = service.disposal_summary("run-q", "daily", include_lots=True)
        assert data["by_symbol"][0]["quantity_display"] == "100"
        assert data["lots"][0]["quantity_display"] == "100"

    def _mixed_run(self, service, run_id):
        self._write_run(service, run_id, {}, [
            self._sale("ABC", "100.00", "1100.00", "1000.00",
                       event_date="2026-02-10"),
            self._sale("XYZ", "200.00", "1200.00", "1000.00",
                       category="OPTION", item_type="OPTION_CLOSE",
                       event_date="2026-05-20"),
            # A CFD is emitted as OPTION_CLOSE too — the whole reason the
            # filter keys on asset_category and not item_type.
            self._sale("CFD1", "300.00", "1300.00", "1000.00",
                       category="CFD", item_type="OPTION_CLOSE",
                       event_date="2026-08-30"),
        ])
        return run_id

    def _symbols(self, service, run_id, **kw):
        return [a["symbol"] for a in
                service.disposal_summary(run_id, "daily", **kw)["by_symbol"]]

    def test_category_filter_does_not_confuse_a_cfd_with_an_option(self, service):
        run = self._mixed_run(service, "run-f1")
        assert self._symbols(service, run, category="OPTION") == ["XYZ"]
        assert self._symbols(service, run, category="CFD") == ["CFD1"]
        assert self._symbols(service, run, category="STOCK") == ["ABC"]

    def test_date_window_is_inclusive_on_both_ends(self, service):
        run = self._mixed_run(service, "run-f2")
        assert self._symbols(service, run, date_from="2026-05-20",
                             date_to="2026-05-20") == ["XYZ"]
        assert sorted(self._symbols(service, run, date_from="2026-05-20")) == \
            ["CFD1", "XYZ"]
        assert sorted(self._symbols(service, run, date_to="2026-05-20")) == \
            ["ABC", "XYZ"]

    def test_filters_combine(self, service):
        run = self._mixed_run(service, "run-f3")
        assert self._symbols(service, run, category="OPTION",
                             date_from="2026-06-01") == []
        assert self._symbols(service, run, category="CFD",
                             date_from="2026-06-01") == ["CFD1"]

    def test_a_shortened_date_refuses_instead_of_emptying_the_result(self, service):
        """The window compares as text, so "2026-03" sorts below every day in
        March: as date_to it would exclude the whole month and answer "you sold
        nothing", which an MCP client cannot tell from the truth."""
        run = self._mixed_run(service, "run-d1")
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            service.disposal_summary(run, "daily", date_to="2026-03")
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            service.disposal_summary(run, "daily", date_from="not-a-date")

    def test_a_compact_iso_date_is_canonicalised_not_passed_through(self, service):
        """"20260315" is a valid ISO date but a wrong comparison key — it sorts
        above every hyphenated one, so it would match nothing."""
        run = self._mixed_run(service, "run-d2")
        assert self._symbols(service, run, date_from="20260501") == \
            self._symbols(service, run, date_from="2026-05-01")
        assert self._symbols(service, run, date_from="20260501") != []

    def test_an_inverted_window_is_refused(self, service):
        run = self._mixed_run(service, "run-d3")
        with pytest.raises(ValueError, match="date_from"):
            service.disposal_summary(run, "daily", date_from="2026-06-01",
                                     date_to="2026-01-01")

    def test_a_bad_bound_is_refused_even_without_a_run(self, service):
        """The caller's mistake should be named whether or not the run exists."""
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            service.disposal_summary("no-such-run", "daily", date_to="2026-03")

    def test_totals_follow_the_filter(self, service):
        """The headline numbers are of the filtered set, not the whole year."""
        run = self._mixed_run(service, "run-f4")
        data = service.disposal_summary(run, "daily", category="STOCK")
        assert data["totals"]["count"] == 1
        assert data["totals"]["gain_loss_czk"] == Decimal("100.00")
        assert data["filters"]["category"] == "STOCK"

    def test_compare_runs_decomposes_gain_delta(self, service):
        self._write_run(
            service, "run-a", {"pairing_method": "fifo"},
            [self._sale("PYPL", "-100.00", "500.00", "600.00"),
             self._sale("SAME", "50.00", "150.00", "100.00")],
            liability={"final_czech_tax_after_credit_czk": "1000.00",
                       "combined_taxable_base_czk": "7000.00"},
        )
        self._write_run(
            service, "run-b", {"pairing_method": "lifo"},
            [self._sale("PYPL", "200.00", "500.00", "300.00"),
             self._sale("SAME", "50.00", "150.00", "100.00"),
             self._sale("NEWCO", "30.00", "80.00", "50.00")],
            liability={"final_czech_tax_after_credit_czk": "1500.00",
                       "combined_taxable_base_czk": "9500.00"},
        )

        data = service.compare_runs("run-a", "run-b")

        assert data["mode"] == "daily"
        assert data["unchanged_symbols"] == ["SAME"]
        rows = {r["symbol"]: r for r in data["by_symbol"]}
        # PYPL: same proceeds, cost dropped 300 → gain up 300
        assert rows["PYPL"]["gain_delta_czk"] == Decimal("300.00")
        assert rows["PYPL"]["proceeds_delta_czk"] == Decimal("0.00")
        assert rows["PYPL"]["cost_basis_delta_czk"] == Decimal("-300.00")
        # NEWCO exists only in run b
        assert rows["NEWCO"]["gain_a_czk"] == Decimal("0.00")
        assert rows["NEWCO"]["count_a"] == 0 and rows["NEWCO"]["count_b"] == 1
        # Sorted by |gain delta| desc
        assert [r["symbol"] for r in data["by_symbol"]] == ["PYPL", "NEWCO"]
        final = next(r for r in data["liability"]
                     if r["line"] == "final_czech_tax_after_credit_czk")
        assert final["delta"] == Decimal("500.00")
        assert data["notes"][0].startswith("All deltas are b")

    def test_compare_runs_requires_shared_mode(self, service):
        self._write_run(service, "run-daily", {}, [], modes=("daily",))
        self._write_run(service, "run-uniform", {"fx_mode": "uniform"}, [],
                        modes=("uniform",))
        with pytest.raises(ValueError, match="No shared FX mode"):
            service.compare_runs("run-daily", "run-uniform")

    def test_compare_runs_unknown_run_raises(self, service):
        with pytest.raises(ValueError, match="not found"):
            service.compare_runs("ghost-a", "ghost-b")

    def test_compare_runs_flags_year_and_data_differences(self, service):
        self._write_run(service, "run-2025", {"tax_year": 2025,
                                              "input_fingerprint": "aaa"}, [])
        self._write_run(service, "run-2026", {"tax_year": 2026,
                                              "input_fingerprint": "bbb"}, [])
        data = service.compare_runs("run-2025", "run-2026")
        assert any("different tax years" in n for n in data["notes"])
        # Different years → the data-changed note must NOT fire
        assert not any("Input data changed" in n for n in data["notes"])

        self._write_run(service, "run-2026b", {"tax_year": 2026,
                                               "input_fingerprint": "ccc"}, [])
        data = service.compare_runs("run-2026", "run-2026b")
        assert any("Input data changed" in n for n in data["notes"])

    def test_disposal_summary_taxable_exempt_buckets(self, service):
        self._write_run(service, "run-d", {}, [
            self._sale("TAXCO", "100.00", "300.00", "200.00"),
            self._sale("FREECO", "400.00", "900.00", "500.00",
                       is_taxable=False, is_exempt=True,
                       exemption_reason="TIME_TEST_EXEMPT"),
            # Non-disposal items must be ignored entirely
            {"item_type": "DIVIDEND", "asset_symbol": "TAXCO",
             "amount_czk": "50.00"},
        ])
        data = service.disposal_summary("run-d", "daily")
        assert data["totals"]["count"] == 2
        assert data["totals"]["gain_loss_czk"] == Decimal("500.00")
        assert data["totals"]["taxable_gain_loss_czk"] == Decimal("100.00")
        assert data["totals"]["exempt_gain_loss_czk"] == Decimal("400.00")
        rows = {r["symbol"]: r for r in data["by_symbol"]}
        assert rows["FREECO"]["exempt_gain_loss_czk"] == Decimal("400.00")
        assert rows["TAXCO"]["quantity_sold"] == Decimal("10")

    def test_disposal_summary_symbol_matches_both_option_key_styles(self, service):
        self._write_run(service, "run-o", {}, [
            self._sale("PYPL", "10.00", "20.00", "10.00"),
            self._sale("PYPL  260731C00061000", "5.00", "5.00", "0.00",
                       category="OPTION", item_type="OPTION_CLOSE"),
            self._sale("C TUI  20260619 9 M", "7.00", "7.00", "0.00",
                       category="OPTION", item_type="OPTION_CLOSE"),
            self._sale("PYPLX", "99.00", "99.00", "0.00"),
        ])
        pypl = service.disposal_summary("run-o", "daily", symbol="PYPL")
        # Stock exact + OCC-style option; NOT the unrelated PYPLX stock
        assert {r["symbol"] for r in pypl["by_symbol"]} == {
            "PYPL", "PYPL  260731C00061000"}
        assert len(pypl["lots"]) == 2

        tui = service.disposal_summary("run-o", "daily", symbol="tui")
        # Marker-first option key, case-insensitive filter
        assert [r["symbol"] for r in tui["by_symbol"]] == ["C TUI  20260619 9 M"]

        # symbol + include_lots combined: lots must stay filtered
        both = service.disposal_summary("run-o", "daily", symbol="PYPL",
                                        include_lots=True)
        assert {l["symbol"] for l in both["lots"]} == {
            "PYPL", "PYPL  260731C00061000"}

    def test_disposal_summary_description_fallback_for_german_listings(self, service):
        # Real-data shape: stock ticker TUI1, option keyed on underlying TUI —
        # only the option's IBKR description starts with the stock ticker.
        self._write_run(service, "run-de", {}, [
            self._sale("TUI1", "10.00", "20.00", "10.00"),
            self._sale("C TUI  20251219 6.4 M", "5.00", "5.00", "0.00",
                       category="OPTION", item_type="OPTION_CLOSE",
                       asset_description="TUI1 19DEC25 6.4 C"),
        ])
        data = service.disposal_summary("run-de", "daily", symbol="TUI1")
        assert {r["symbol"] for r in data["by_symbol"]} == {
            "TUI1", "C TUI  20251219 6.4 M"}

    def test_disposal_summary_single_letter_symbol_no_marker_overmatch(self, service):
        self._write_run(service, "run-c", {}, [
            # Marker-first TUI call — must NOT match symbol='C' (Citigroup)
            self._sale("C TUI  20260619 9 M", "7.00", "7.00", "0.00",
                       category="OPTION", item_type="OPTION_CLOSE",
                       asset_description="TUI1 19JUN26 9 C"),
            # Genuine OCC option ON underlying C — must match
            self._sale("C     260417C00050000", "3.00", "3.00", "0.00",
                       category="OPTION", item_type="OPTION_CLOSE",
                       asset_description="C 17APR26 50 C"),
        ])
        data = service.disposal_summary("run-c", "daily", symbol="C")
        assert [r["symbol"] for r in data["by_symbol"]] == [
            "C     260417C00050000"]

    def test_disposal_summary_flags_failed_fx_conversions(self, service):
        self._write_run(service, "run-fx", {}, [
            self._sale("OKCO", "100.00", "300.00", "200.00"),
            # Failed conversion: null money legs, flag set
            self._sale("BADCO", None, None, "150.00",
                       fx_conversion_failed=True),
        ])
        data = service.disposal_summary("run-fx", "daily")
        assert data["totals"]["fx_failed_count"] == 1
        assert data["totals"]["count"] == 2
        assert "UNKNOWN" in data["fx_warning"]
        rows = {r["symbol"]: r for r in data["by_symbol"]}
        assert rows["BADCO"]["fx_failed_count"] == 1
        assert rows["OKCO"]["fx_failed_count"] == 0
        # Clean run → no warning key at all
        clean = service.disposal_summary("run-fx", "daily", symbol="OKCO")
        assert "fx_warning" not in clean

    def test_compare_runs_flags_fx_failures_per_symbol(self, service):
        self._write_run(service, "run-fxa", {}, [
            self._sale("FXCO", "5000.00", "50000.00", "45000.00")])
        self._write_run(service, "run-fxb", {}, [
            self._sale("FXCO", None, "50000.00", None,
                       fx_conversion_failed=True)])
        data = service.compare_runs("run-fxa", "run-fxb")
        [row] = data["by_symbol"]
        assert row["fx_failed_a"] == 0 and row["fx_failed_b"] == 1
        assert any("failed FX" in n for n in data["notes"])

    def test_compare_runs_detects_taxable_exempt_flip(self, service):
        # Same gross gain/proceeds/cost in both runs; only the §4/1/u
        # exemption flips — the symbol must still surface as changed.
        self._write_run(service, "run-ta", {}, [
            self._sale("FLIPCO", "1000.00", "5000.00", "4000.00",
                       is_taxable=False, is_exempt=True,
                       exemption_reason="TIME_TEST_EXEMPT")])
        self._write_run(service, "run-tb", {"pairing_method": "lifo"}, [
            self._sale("FLIPCO", "1000.00", "5000.00", "4000.00")])
        data = service.compare_runs("run-ta", "run-tb")
        [row] = data["by_symbol"]
        assert row["symbol"] == "FLIPCO"
        assert row["gain_delta_czk"] == Decimal("0.00")
        assert row["taxable_gain_delta_czk"] == Decimal("1000.00")
        assert data["unchanged_symbols"] == []

    def test_compare_runs_row_arithmetic_is_self_consistent(self, service):
        """Sub-haléř inputs must not make a displayed row look wrong.

        Raw gains 10.004 → 10.996: the delta is the accurate rounded 0.99
        (not 11.00 − 10.00 = 1.00), and the displayed gain_b absorbs the
        haléř so both row identities still hold.
        """
        self._write_run(service, "run-ea", {}, [
            self._sale("EPCO", "10.004", "20.004", "10.00")])
        self._write_run(service, "run-eb", {}, [
            self._sale("EPCO", "10.996", "20.996", "10.00")])
        data = service.compare_runs("run-ea", "run-eb")
        [row] = data["by_symbol"]
        assert row["gain_delta_czk"] == Decimal("0.99")
        # Identity 1: displayed endpoints agree with the displayed delta
        assert row["gain_b_czk"] - row["gain_a_czk"] == row["gain_delta_czk"]
        # Identity 2: the decomposition adds up (gain = proceeds − cost)
        assert (row["proceeds_delta_czk"] - row["cost_basis_delta_czk"]
                == row["gain_delta_czk"])
        # Identity 3: all items taxable → taxable delta equals the gross one
        assert row["taxable_gain_delta_czk"] == row["gain_delta_czk"]

    def test_compare_runs_explicit_mode_reads_that_modes_file(self, service):
        self._write_run(service, "run-ma", {}, [], modes=("daily", "uniform"))
        self._write_run(service, "run-mb", {}, [], modes=("daily", "uniform"))
        data = service.compare_runs("run-ma", "run-mb", "uniform")
        assert data["mode"] == "uniform"
        row = next(r for r in data["liability"]
                   if r["line"] == "taxable_interest_czk")
        assert row["a"] == self.MODE_MARKER["uniform"]

    def test_compare_runs_falls_back_to_shared_mode(self, service):
        # 'daily' not shared → first shared mode wins
        self._write_run(service, "run-ua", {"fx_mode": "uniform"}, [],
                        modes=("uniform",))
        self._write_run(service, "run-ub", {"fx_mode": "uniform"}, [],
                        modes=("uniform",))
        data = service.compare_runs("run-ua", "run-ub")
        assert data["mode"] == "uniform"

    def test_compare_runs_legacy_meta_defaults_to_daily(self, service):
        self._write_run(service, "run-la", {}, [], legacy_meta=True)
        self._write_run(service, "run-lb", {}, [], legacy_meta=True)
        data = service.compare_runs("run-la", "run-lb")
        assert data["mode"] == "daily"

    def test_compare_runs_explicit_mode_unavailable_names_the_mode(self, service):
        self._write_run(service, "run-xa", {}, [])
        self._write_run(service, "run-xb", {}, [])
        with pytest.raises(ValueError) as exc:
            service.compare_runs("run-xa", "run-xb", "uniform")
        assert "uniform" in str(exc.value)
        assert "not available in both runs" in str(exc.value)
        assert "No shared FX mode" not in str(exc.value)

    def test_compare_runs_missing_result_file_raises(self, service):
        self._write_run(service, "run-wa", {}, [], modes=("daily", "uniform"))
        self._write_run(service, "run-wb", {}, [], modes=("daily", "uniform"))
        (service.runs_dir / "run-wb" / "result.uniform.json").unlink()
        with pytest.raises(ValueError, match="has no 'uniform' result file"):
            service.compare_runs("run-wa", "run-wb", "uniform")

    def test_compare_runs_liability_line_missing_on_one_side(self, service):
        # Legacy persisted run predating a liability line: key absent
        self._write_run(service, "run-new", {}, [],
                        liability={"final_czech_tax_after_credit_czk": "1000.00"})
        self._write_run(service, "run-old", {}, [], liability={})
        data = service.compare_runs("run-new", "run-old")
        rows = {r["line"]: r for r in data["liability"]}
        row = rows["final_czech_tax_after_credit_czk"]
        assert row["a"] == "1000.00"
        assert row["b"] is None
        assert row["delta"] == Decimal("-1000.00")
        # Keys absent on both sides are omitted, not emitted as zero rows
        assert "taxable_dividends_czk" not in rows

    def test_compare_runs_data_note_requires_matching_settings(self, service):
        # Pairing differs (the headline FIFO-vs-LIFO case) → fingerprints
        # legitimately differ, the data-changed note must NOT fire.
        self._write_run(service, "run-fp1", {"input_fingerprint": "aaa"}, [])
        self._write_run(service, "run-fp2", {"input_fingerprint": "bbb",
                                             "pairing_method": "lifo"}, [])
        data = service.compare_runs("run-fp1", "run-fp2")
        assert not any("Input data changed" in n for n in data["notes"])

        # fx_mode differs → same suppression
        self._write_run(service, "run-fp3", {"input_fingerprint": "ccc",
                                             "fx_mode": "compare"}, [])
        data = service.compare_runs("run-fp1", "run-fp3")
        assert not any("Input data changed" in n for n in data["notes"])

        # Identical fingerprints → no note either
        self._write_run(service, "run-fp4", {"input_fingerprint": "aaa"}, [])
        data = service.compare_runs("run-fp1", "run-fp4")
        assert not any("Input data changed" in n for n in data["notes"])

    def test_disposal_summary_missing_run_returns_none(self, service):
        assert service.disposal_summary("nope", "daily") is None


class TestProceedsAreCalledPrijem:
    """Gross sale proceeds are labelled "příjem", never "výnos".

    §10 calls what you receive on a transfer a *příjem*; "výnos" is a finance
    word that most often means yield or return, so on a figure that is the whole
    sale amount it reads as the profit. It misled the tool's own author twice —
    once in the sell planner, and again on the simulation card where "Hrubý
    výnos" sat directly beside "Zdanitelný zisk" and the contrast was supposed
    to carry the meaning. It did not.

    Only the LABELS are constrained. The word is fine in prose, and in another
    sense entirely (a bond's yield) it would be the correct one.
    """

    _LABELS = re.compile(
        r"<(?:th|h3)[^>]*>\s*((?:Hrubý\s+)?[Vv]ýnos[^<]*)</(?:th|h3)>")

    def test_no_column_or_stat_is_labelled_vynos(self):
        templates = (Path(__file__).resolve().parent.parent
                     / "src" / "webapp" / "templates")
        offenders = []
        for path in templates.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for match in self._LABELS.finditer(text):
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line} labels a figure "
                                 f"{match.group(1).strip()!r}")
        assert not offenders, (
            "Proceeds must be labelled 'příjem' (the §10 term), not 'výnos':\n"
            + "\n".join(offenders))
