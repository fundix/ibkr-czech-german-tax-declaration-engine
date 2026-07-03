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
