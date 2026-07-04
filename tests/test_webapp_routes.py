# tests/test_webapp_routes.py
"""
Route smoke tests over a real persisted run (generated offline through the
service layer with pinned FX providers). Skipped when the `web` extra is not
installed — the service layer itself is covered framework-free in
test_webapp_services.py.
"""
import shutil
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from src.webapp.app import create_app  # noqa: E402
from src.webapp.services import RunService  # noqa: E402
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider  # noqa: E402
from tests.test_webapp_services import SYNTHETIC, _seed_synthetic_year  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("webapp")
    svc = RunService(data_dir=tmp / "data", runs_dir=tmp / "runs")
    _seed_synthetic_year(svc)
    svc._execute_run(
        "2024-test", 2024, "daily",
        ecb_provider=GoldenEcbProvider(),
        cz_fx_provider=GoldenCnbProvider(),
    )
    app = create_app(services=svc)
    with TestClient(app) as tc:
        yield tc
    svc.runner.shutdown(wait=False)


class TestPages:
    def test_index_lists_dataset_and_run(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "2024" in r.text
        assert "2024-test" in r.text
        assert "Spustit výpočet" in r.text

    def test_index_shows_pairing_method_selector(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert 'name="pairing_method"' in r.text
        assert 'value="optimal"' in r.text
        assert "Daňově optimální" in r.text
        assert "Vážený průměr" in r.text

    def test_results_page_shows_final_tax(self, client):
        r = client.get("/results/2024-test")
        assert r.status_code == 200
        assert "3 604,00" in r.text  # 3 604,00 Kč, Czech formatting
        assert "§8 ZDP" in r.text

    def test_items_page_renders_and_filters(self, client):
        r = client.get("/results/2024-test/items")
        assert r.status_code == 200
        assert "ALPHA" in r.text
        r = client.get("/results/2024-test/items?status=exempt")
        assert r.status_code == 200
        assert "OLDCO" in r.text          # exempt by 3y time test
        assert "ALPHA" not in r.text      # taxable — filtered out

    def test_form_page_shows_official_line_refs(self, client):
        r = client.get("/results/2024-test/form")
        assert r.status_code == 200
        assert "ř. 38 DAP" in r.text
        assert "Příloha 2, ř. 209" in r.text

    def test_review_page_renders(self, client):
        r = client.get("/results/2024-test/review")
        assert r.status_code == 200
        assert "kontrol" in r.text.lower()

    def test_portfolio_page_shows_open_position_with_lots(self, client):
        r = client.get("/results/2024-test/portfolio")
        assert r.status_code == 200
        assert "DIVCO" in r.text
        assert "odhad" in r.text        # SOY-fallback lot flagged
        assert "ALPHA" not in r.text    # sold — not an open position

    def test_dividends_page_aggregates_by_asset(self, client):
        r = client.get("/results/2024-test/dividends")
        assert r.status_code == 200
        assert "DIVCO" in r.text
        assert "§38f" in r.text

    def test_downloads(self, client):
        r = client.get("/results/2024-test/download/daily.json")
        assert r.status_code == 200
        assert r.json()["metadata"]["tax_year"] == 2024
        r = client.get("/results/2024-test/download/daily.xlsx")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/")
        r = client.get("/results/2024-test/download/daily.pdf")
        assert r.status_code == 200
        assert r.content[:5] == b"%PDF-"
        assert client.get("/results/2024-test/download/daily.exe").status_code == 404

    def test_unknown_run_redirects_home(self, client):
        r = client.get("/results/nope", follow_redirects=False)
        assert r.status_code == 303


class TestRunFlow:
    def test_start_run_with_missing_year_shows_error_fragment(self, client):
        r = client.post("/runs", data={"tax_year": "2031", "fx_mode": "daily"})
        assert r.status_code == 200
        assert "2031" in r.text  # error message names the year

    def test_unknown_job_status(self, client):
        r = client.get("/runs/deadbeef/status")
        assert "Neznámý běh" in r.text


class TestLiveAndSimulateRoutes:
    @pytest.fixture()
    def stub_client(self, tmp_path_factory):
        from decimal import Decimal
        from tests.test_webapp_services import StubConverter, StubQuotes
        tmp = tmp_path_factory.mktemp("webapp-stub")
        svc = RunService(
            data_dir=tmp / "data", runs_dir=tmp / "runs",
            quote_service=StubQuotes({"DIVCO": (Decimal("35"), "USD")}),
            converter_factory=StubConverter,
        )
        _seed_synthetic_year(svc)
        svc._execute_run(
            "2024-live", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )
        with TestClient(create_app(services=svc)) as tc:
            yield tc
        svc.runner.shutdown(wait=False)

    def test_live_fragment_values_and_allocation(self, stub_client):
        r = stub_client.get("/results/2024-live/portfolio/live")
        assert r.status_code == 200
        assert "DIVCO" in r.text
        assert "Aktuální hodnota" in r.text
        assert "alloc-chart" in r.text

    def test_simulate_form_and_post(self, stub_client):
        r = stub_client.get("/results/2024-live/simulate")
        assert r.status_code == 200
        assert "DIVCO" in r.text
        r = stub_client.post("/results/2024-live/simulate",
                             data={"symbol": "DIVCO", "quantity": "50", "price": ""})
        assert r.status_code == 200
        assert "Odhad daně" in r.text
        assert "odhad" in r.text  # SOY lot flagged in consumed lots

    def test_simulate_unknown_symbol_shows_error(self, stub_client):
        r = stub_client.post("/results/2024-live/simulate",
                             data={"symbol": "GHOST", "quantity": "1", "price": "5"})
        assert "GHOST" in r.text


class TestUpload:
    def test_upload_saves_canonical_files(self, client):
        files = {
            "trades": ("TaxEngine-Trades-2030.csv", (SYNTHETIC / "trades.csv").read_bytes(), "text/csv"),
        }
        r = client.post("/files/upload", data={"tax_year": "2030"}, files=files,
                        follow_redirects=False)
        assert r.status_code == 303
        r = client.get("/files")
        assert "2030" in r.text


class TestDeleteYear:
    def test_delete_moves_year_to_trash_and_flashes(self, tmp_path):
        svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
        _seed_synthetic_year(svc, 2029)
        try:
            with TestClient(create_app(services=svc)) as tc:
                r = tc.post("/files/delete-year", data={"tax_year": "2029"},
                            follow_redirects=False)
                assert r.status_code == 303
                assert r.headers["location"] == "/files?deleted=2029"
                page = tc.get("/files?deleted=2029")
                assert "přesunuta do koše" in page.text
                assert (svc.data_dir / "_trash").is_dir()
                assert svc.get_year(2029) is None

                # Unknown year: no crash, plain redirect without the flash
                r = tc.post("/files/delete-year", data={"tax_year": "2031"},
                            follow_redirects=False)
                assert r.headers["location"] == "/files"
        finally:
            svc.runner.shutdown(wait=False)


class TestIbkrFlexRoutes:
    def test_files_page_shows_flex_section(self, client):
        r = client.get("/files")
        assert "Flex Web Service" in r.text
        assert 'name="q_trades"' in r.text

    def test_flex_settings_roundtrip(self, client):
        r = client.post("/files/flex", data={
            "token": "secret-token-xyz",
            "q_trades": "111", "q_cash": "222",
            "q_positions": "333", "q_corp_actions": "444",
        }, follow_redirects=False)
        assert r.status_code == 303
        r = client.get("/files")
        assert "nastaveno" in r.text
        assert "secret-token-xyz" not in r.text  # token never echoed back

    def test_fetch_without_config_shows_error(self, tmp_path):
        svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
        try:
            with TestClient(create_app(services=svc)) as tc:
                r = tc.post("/ibkr/fetch", data={"tax_year": "2026"})
                assert r.status_code == 200
                assert "není nastavená" in r.text
        finally:
            svc.runner.shutdown(wait=False)
