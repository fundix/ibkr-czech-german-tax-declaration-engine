# tests/test_webapp_routes.py
"""
Route smoke tests over a real persisted run (generated offline through the
service layer with pinned FX providers). Skipped when the `web` extra is not
installed — the service layer itself is covered framework-free in
test_webapp_services.py.
"""
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from src.webapp.app import create_app  # noqa: E402
from src.webapp.services import RunService  # noqa: E402
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider  # noqa: E402
from tests.test_webapp_services import (  # noqa: E402
    SYNTHETIC, StubConverter, StubQuotes, _seed_synthetic_year,
)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("webapp")
    # Stubbed quotes and FX: the live-valuation routes would otherwise reach
    # Yahoo and ČNB, which is why they had no render test at all — and the
    # allocation payload they pass to Chart.js changed shape twice recently
    # with the whole suite staying green.
    svc = RunService(
        data_dir=tmp / "data", runs_dir=tmp / "runs",
        quote_service=StubQuotes({"DIVCO": (Decimal("40"), "USD")}),
        converter_factory=StubConverter,
    )
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
    def test_index_dashboard_surfaces_latest_run(self, client):
        # The home page is now a dashboard: it links to the latest run and
        # offers the "run calculation" call-to-action (the form lives on /runs).
        r = client.get("/")
        assert r.status_code == 200
        assert "Přehled" in r.text
        assert "2024-test" in r.text          # links to /results/2024-test
        assert "Spustit výpočet" in r.text    # CTA to /runs

    @staticmethod
    def _assert_json_payloads(html, markers):
        """Every payload the inline JS parses must be a JSON ARRAY of objects.

        Two ways this breaks silently. `tojson` over a Decimal raises a
        TypeError, taking the fragment to a 500 — a box that never fills. And
        handing the wrapper dict where the array was expected still renders and
        still parses, but `alloc.map(...)` then iterates the keys, so the chart
        comes out empty with no error anywhere. Asserting the TYPE is what
        catches the second one; valid-JSON alone does not.
        """
        for marker in markers:
            assert marker in html, marker
            tail = html.split(marker, 1)[1]
            depth, end = 0, None
            for k, ch in enumerate(tail):
                if ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                    if depth == 0:
                        end = k + 1
                        break
            assert end is not None, marker
            payload = json.loads(tail[:end])
            assert isinstance(payload, list), f"{marker} is not an array"
            assert all(isinstance(x, dict) for x in payload), marker

    def test_valuation_fragment_renders_its_chart_payloads(self, client):
        """The homepage's net-worth box had no render test at all, so the
        payloads it hands Chart.js changed shape twice with the suite green.
        """
        r = client.get("/dashboard/valuation")
        assert r.status_code == 200
        assert "Aktuální hodnota" in r.text
        # Live quote reached the row and was converted (40 USD x 100 x 20);
        # format_czk uses a non-breaking thousands space.
        assert "DIVCO" in r.text
        assert "80\u00a0000,00" in r.text
        self._assert_json_payloads(
            r.text, ("const alloc = ", "const snaps = "))

    def test_portfolio_live_fragment_renders_its_chart_payloads(self, client):
        r = client.get("/results/2024-test/portfolio/live")
        assert r.status_code == 200
        assert 'id="portfolio-live-table"' in r.text
        assert "Váha %" in r.text
        assert "DIVCO" in r.text
        self._assert_json_payloads(r.text, (
            "const alloc = ", "const snaps = ",
            "doughnut('category-chart', ", "doughnut('currency-chart', ",
            "doughnut('country-chart', ",
        ))

    def test_runs_page_lists_dataset_and_run(self, client):
        r = client.get("/runs")
        assert r.status_code == 200
        assert "2024" in r.text
        assert "2024-test" in r.text
        assert "Spustit výpočet" in r.text

    def test_runs_page_shows_pairing_method_selector(self, client):
        r = client.get("/runs")
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

    def test_results_header_labels_are_self_explanatory(self, client):
        r = client.get("/results/2024-test")
        assert r.status_code == 200
        # The big number is labelled as the tax, and the FX/pairing basis is
        # spelled out (not the raw 'daily' / 'fifo' tokens).
        assert "Výsledná daň z příjmů" in r.text
        assert "denní kurz ČNB" in r.text
        assert "Metoda párování §10" in r.text
        assert "Režim: daily" not in r.text

    def test_items_page_renders_and_filters(self, client):
        r = client.get("/results/2024-test/items")
        assert r.status_code == 200
        assert "ALPHA" in r.text
        r = client.get("/results/2024-test/items?status=exempt")
        assert r.status_code == 200
        assert "OLDCO" in r.text          # exempt by 3y time test
        assert "ALPHA" not in r.text      # taxable — filtered out

    @staticmethod
    def _ranking(html):
        """Symbol order inside the ranking table.

        Sliced past the table marker on purpose: the datalist feeding the
        filter box repeats every symbol higher up the page.
        """
        table = html.split('id="disposals-table"', 1)[1]
        return sorted(("OLDCO", "ALPHA", "UNDR"), key=table.index)

    @staticmethod
    def _ranking_present(html):
        """Which of the golden year's three symbols the table actually lists."""
        if 'id="disposals-table"' not in html:
            return []
        table = html.split('id="disposals-table"', 1)[1]
        return sorted((s for s in ("OLDCO", "ALPHA", "UNDR") if s in table),
                      key=table.index)

    def test_disposals_page_ranks_by_gain(self, client):
        r = client.get("/results/2024-test/disposals")
        assert r.status_code == 200
        assert "Realizované prodeje" in r.text
        # Golden year gains: OLDCO 21 810.98, ALPHA 19 379.40, the put 4 657.74.
        assert self._ranking(r.text) == ["OLDCO", "ALPHA", "UNDR"]
        # The table opts into the shared sort/filter JS.
        assert 'class="items sortable"' in r.text

    def test_disposals_page_sorts_by_proceeds_independently_of_gain(self, client):
        """ALPHA has the largest proceeds (136 256) but not the largest gain,
        so this order can only come from the server honouring ?sort=."""
        r = client.get("/results/2024-test/disposals?sort=proceeds_desc")
        assert r.status_code == 200
        assert self._ranking(r.text) == ["ALPHA", "OLDCO", "UNDR"]

    def test_disposals_page_symbol_brings_the_lot_rows(self, client):
        # Without a symbol the service does not build per-lot rows at all.
        assert 'id="disposal-lots-table"' not in client.get(
            "/results/2024-test/disposals").text
        r = client.get("/results/2024-test/disposals?symbol=ALPHA")
        assert r.status_code == 200
        assert 'id="disposal-lots-table"' in r.text
        assert "OLDCO" not in r.text          # filtered out

    def test_disposals_page_accepts_a_sort_order(self, client):
        r = client.get("/results/2024-test/disposals?sort=gain_asc")
        assert r.status_code == 200
        assert self._ranking(r.text) == ["UNDR", "ALPHA", "OLDCO"]

    def test_shared_filters_are_bookmarkable_on_both_pages(self, client):
        """Same query names on /items and /disposals, and on the MCP tool."""
        # The golden year sells ALPHA and OLDCO as stock, the put as an option.
        r = client.get("/results/2024-test/disposals?category=OPTION")
        assert r.status_code == 200
        assert self._ranking_present(r.text) == ["UNDR"]

        r = client.get("/results/2024-test/items?category=STOCK")
        assert r.status_code == 200
        assert "UNDR" not in r.text.split('id="items-table"', 1)[1]

    def test_date_window_narrows_both_pages(self, client):
        # ALPHA sells 2024-09-10, OLDCO 2024-05-20, the put expires 2024-03-15.
        r = client.get("/results/2024-test/disposals"
                       "?date_from=2024-06-01&date_to=2024-12-31")
        assert r.status_code == 200
        assert self._ranking_present(r.text) == ["ALPHA"]

        r = client.get("/results/2024-test/items"
                       "?date_from=2024-01-01&date_to=2024-04-01")
        body = r.text.split('id="items-table"', 1)[1]
        assert "ALPHA" not in body and "OLDCO" not in body

    def test_a_malformed_date_says_so_instead_of_showing_zero_rows(self, client):
        """Only reachable by hand-editing the URL — the date inputs cannot
        produce it — but an empty table would read as "nothing sold then"."""
        for path in ("items", "disposals"):
            r = client.get(f"/results/2024-test/{path}?date_to=2024-03")
            assert r.status_code == 200
            assert "YYYY-MM-DD" in r.text

    def test_items_symbol_filter_pulls_in_the_options_on_it(self, client):
        """Typing a ticker means the same thing here as on the disposals page."""
        r = client.get("/results/2024-test/items?symbol=UNDR")
        assert r.status_code == 200
        body = r.text.split('id="items-table"', 1)[1]
        assert "UNDR" in body
        assert "ALPHA" not in body

    def test_positions_refresh_renders_the_options_table_alone(
            self, client, monkeypatch):
        """No engine run: one Flex slot, then just the fragment re-rendered."""
        from src.webapp import services as services_mod
        from src.webapp.ibkr_flex import FlexConfig, save_flex_config

        svc = client.app.state.services
        save_flex_config(svc.flex_config_path,
                         FlexConfig(token="tok", queries={"positions": "42"}))
        header = ("ClientAccountID,CurrencyPrimary,AssetClass,SubCategory,Symbol,"
                  "Description,Conid,ISIN,UnderlyingSymbol,Multiplier,Quantity,"
                  "MarkPrice,PositionValue,CostBasisMoney,UnderlyingConid,"
                  "LevelOfDetail,OpenDateTime,HoldingPeriodDateTime")
        row = ("U1,USD,OPT,,SOFI  280616P00015000,SOFI PUT,1,,SOFI,100,-4,"
               "3.5,-1400,0,2,SUMMARY,,")
        monkeypatch.setattr(
            services_mod, "fetch_statement",
            lambda token, query_id, from_date=None, to_date=None:
                (header + "\n" + row + "\n").encode())

        runs_before = len(svc.list_runs())
        r = client.post("/dashboard/options/refresh")
        assert r.status_code == 200
        assert 'id="dash-options"' in r.text
        assert "SOFI  280616P00015000" in r.text
        assert "výpisu pozic" in r.text          # says where the numbers came from
        assert len(svc.list_runs()) == runs_before
        # Written into the current year, never the run's (2024) year.
        from datetime import date
        assert (svc.data_dir / str(date.today().year) / "positions_end.csv").is_file()
        assert not (svc.data_dir / "2024" / "positions_end.csv.bak").exists()

    def test_a_posted_year_cannot_steer_the_refresh(self, client, monkeypatch):
        """The 2024 run is on screen, so a form-carried year would have aimed
        the write at a closed year's authoritative statement."""
        from datetime import date

        from src.webapp import services as services_mod
        from src.webapp.ibkr_flex import FlexConfig, save_flex_config

        svc = client.app.state.services
        save_flex_config(svc.flex_config_path,
                         FlexConfig(token="tok", queries={"positions": "42"}))
        header = ("ClientAccountID,CurrencyPrimary,AssetClass,SubCategory,Symbol,"
                  "Description,Conid,ISIN,UnderlyingSymbol,Multiplier,Quantity,"
                  "MarkPrice,PositionValue,CostBasisMoney,UnderlyingConid,"
                  "LevelOfDetail,OpenDateTime,HoldingPeriodDateTime")
        monkeypatch.setattr(
            services_mod, "fetch_statement",
            lambda token, query_id, from_date=None, to_date=None:
                (header + "\n").encode())

        before = (svc.data_dir / "2024" / "positions_end.csv").read_bytes()
        r = client.post("/dashboard/options/refresh", data={"tax_year": 2024})
        assert r.status_code == 200
        # The closed year's statement is byte-identical afterwards.
        assert (svc.data_dir / "2024" / "positions_end.csv").read_bytes() == before
        assert (svc.data_dir / str(date.today().year)
                / "positions_end.csv").is_file()

    def test_positions_refresh_surfaces_a_failure_as_a_card(
            self, client, monkeypatch):
        from src.webapp.ibkr_flex import FlexConfig, save_flex_config

        svc = client.app.state.services
        save_flex_config(svc.flex_config_path, FlexConfig(token="", queries={}))
        r = client.post("/dashboard/options/refresh")
        assert r.status_code == 200
        assert "Načtení pozic selhalo" in r.text

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


class TestSellPlannerMarkup:
    """Structure of the zone editor, which has no other render coverage.

    Uses the module-scoped client, so it saves a zone on the synthetic holding
    and removes it again — no other test reads /targets.
    """

    SYMBOL = "DIVCO"

    @staticmethod
    def _forms_without_a_flex_parent(html):
        """Elements holding two or more sibling `inline-form`s directly.

        Each action is its own one-button <form>, and `.inline-form` is a
        block-level flex container — so siblings with no flex parent each claim
        a line and the buttons come out stacked in a column. `.action-row` is
        what puts them in a row; anything else holding several of them is the
        bug. Checked structurally because CSS layout itself is not testable
        here, and this is the condition the CSS depends on.
        """
        from html.parser import HTMLParser

        class Scan(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []          # (tag, classes, inline_form_children)
                self.bad = []

            def handle_starttag(self, tag, attrs):
                cls = dict(attrs).get("class", "")
                if tag == "form" and "inline-form" in cls and self.stack:
                    self.stack[-1][2].append(1)
                if tag in ("form", "input", "br", "img", "hr"):
                    if tag != "form":
                        return
                self.stack.append([tag, cls, []])

            def handle_endtag(self, tag):
                for i in range(len(self.stack) - 1, -1, -1):
                    if self.stack[i][0] == tag:
                        frame = self.stack.pop(i)
                        if (len(frame[2]) > 1
                                and "action-row" not in frame[1]):
                            self.bad.append(f"<{frame[0]} class={frame[1]!r}> "
                                            f"holds {len(frame[2])} inline-forms")
                        del self.stack[i:]
                        return

        scan = Scan()
        scan.feed(html)
        return scan.bad

    def test_action_buttons_sit_in_a_flex_row(self, client):
        client.post("/targets/zone", data={"symbol": self.SYMBOL, "price": "50",
                                           "quantity": "10"},
                    follow_redirects=False)
        try:
            r = client.get("/targets")
            assert r.status_code == 200
            assert 'class="action-row"' in r.text
            assert self._forms_without_a_flex_parent(r.text) == []
        finally:
            client.post("/targets/symbol/delete", data={"symbol": self.SYMBOL},
                        follow_redirects=False)

    def test_quantities_render_without_the_fifo_tail(self, client):
        """"10.00000000" is eight zeros of noise in a column of share counts."""
        client.post("/targets/zone", data={"symbol": self.SYMBOL, "price": "50",
                                           "quantity": "10"},
                    follow_redirects=False)
        try:
            r = client.get("/targets")
            assert "Odhad zisku" in r.text          # the gain column exists
            assert "10.00000000" not in r.text
            assert "0.00000000" not in r.text
        finally:
            client.post("/targets/symbol/delete", data={"symbol": self.SYMBOL},
                        follow_redirects=False)
