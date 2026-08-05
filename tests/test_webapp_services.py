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
        overview = service.options_overview("opt-run")
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
        [row] = service.options_overview("opt-run")["options"]
        assert row["expired"] is False
        assert row["days_to_expiry"] == 16  # 2025-12-31 -> 2026-01-16

    def test_options_overview_missing_run_is_empty(self, service):
        assert service.options_overview("does-not-exist") == {
            "as_of": None, "tax_year": None, "options": []}


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
        # Same gross gain/proceeds/cost in both runs; only the §4/1/w
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
