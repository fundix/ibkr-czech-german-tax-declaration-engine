# tests/test_mcp_server.py
"""
MCP server over the tax engine — exercised through the official SDK's
in-memory client session against a real persisted golden run (offline,
pinned FX providers). Skipped when the `mcp` extra is not installed.

Pins two contracts:
1. every tool result is JSON-serializable (no raw Decimal/date leaks), and
2. the figures Claude sees equal the CLI/GUI golden figures.
"""
import asyncio
import json

import pytest

mcp_sdk = pytest.importorskip("mcp")
from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session as client_session,
)

from src.mcp_server.server import create_server  # noqa: E402
from src.webapp.services import RunService  # noqa: E402
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider  # noqa: E402
from tests.test_webapp_services import (  # noqa: E402
    StubConverter,
    StubQuotes,
    _seed_synthetic_year,
)

from decimal import Decimal  # noqa: E402

EXPECTED_TOOLS = {
    "list_datasets", "run_pipeline", "refresh_data", "get_tax_summary",
    "get_form_mapping", "get_pending_review_items", "get_positions",
    "get_time_test_status", "get_dividends", "simulate_sale",
}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mcp")
    svc = RunService(
        data_dir=tmp / "data", runs_dir=tmp / "runs",
        quote_service=StubQuotes({"DIVCO": (Decimal("35"), "USD")}),
        converter_factory=StubConverter,
    )
    _seed_synthetic_year(svc)
    svc._execute_run(
        "2024-mcp", 2024, "daily",
        ecb_provider=GoldenEcbProvider(),
        cz_fx_provider=GoldenCnbProvider(),
    )
    yield create_server(svc)
    svc.runner.shutdown(wait=False)


def _call(server, tool, args=None):
    async def _run():
        async with client_session(server._mcp_server) as client:
            return await client.call_tool(tool, args or {})
    return asyncio.run(_run())


def _payload(result):
    assert result.content, "empty tool result"
    text = result.content[0].text
    return json.loads(text)  # raises if the tool leaked non-JSON content


class TestToolRegistry:
    def test_all_tools_registered(self, server):
        async def _list():
            async with client_session(server._mcp_server) as client:
                return await client.list_tools()
        tools = asyncio.run(_list())
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS

    def test_tools_have_descriptions(self, server):
        async def _list():
            async with client_session(server._mcp_server) as client:
                return await client.list_tools()
        tools = asyncio.run(_list())
        for t in tools.tools:
            assert t.description and len(t.description) > 20, t.name


class TestTools:
    def test_list_datasets(self, server):
        data = _payload(_call(server, "list_datasets"))
        years = {d["tax_year"]: d for d in data["datasets"]}
        assert years[2024]["run_ready"] is True
        assert years[2024]["latest_run_id"] == "2024-mcp"

    def test_get_tax_summary_matches_golden(self, server):
        data = _payload(_call(server, "get_tax_summary", {"tax_year": 2024}))
        line = data["sections"]["cz_tax_liability"]["line_items"]
        assert line["final_czech_tax_after_credit_czk"] == "3604.00"
        assert data["run_id"] == "2024-mcp"

    def test_get_form_mapping_has_official_refs(self, server):
        data = _payload(_call(server, "get_form_mapping", {"tax_year": 2024}))
        refs = [ln.get("official_line_ref") for sec in data["sections"]
                for ln in sec["lines"]]
        assert "ř. 38 DAP" in refs

    def test_get_positions_and_time_test(self, server):
        pos = _payload(_call(server, "get_positions", {"tax_year": 2024}))
        assert [p["symbol"] for p in pos["positions"]] == ["DIVCO"]

        tt = _payload(_call(server, "get_time_test_status",
                            {"tax_year": 2024, "symbol": "DIVCO"}))
        [lot] = tt["positions"][0]["lots"]
        # SOY fallback lot: synthetic acquisition date → cannot promise a deadline
        assert lot["status"] == "unknown_verify_manually"
        assert lot["acquisition_estimated"] is True

    def test_get_dividends(self, server):
        data = _payload(_call(server, "get_dividends", {"tax_year": 2024}))
        assert data["assets"][0]["symbol"] == "DIVCO"
        assert Decimal(data["total_gross_czk"]) > 0

    def test_get_pending_review_items(self, server):
        data = _payload(_call(server, "get_pending_review_items", {"tax_year": 2024}))
        assert "pending_items" in data and "section_notes" in data

    def test_simulate_sale_json_safe(self, server):
        data = _payload(_call(server, "simulate_sale",
                              {"tax_year": 2024, "symbol": "DIVCO",
                               "quantity": 50}))
        assert data["symbol"] == "DIVCO"
        assert data["price_source"] == "live"      # stub quote 35 USD
        assert Decimal(data["proceeds_czk"]) == Decimal("35000")  # 50×35×20


class TestErrors:
    def test_missing_year_reports_helpful_error(self, server):
        result = _call(server, "get_tax_summary", {"tax_year": 2031})
        assert result.isError
        assert "run_pipeline" in result.content[0].text

    def test_run_pipeline_rejects_bad_fx_mode(self, server):
        result = _call(server, "run_pipeline",
                       {"tax_year": 2024, "fx_mode": "bogus"})
        assert result.isError

    def test_refresh_data_without_flex_config_reports_setup_hint(self, server):
        result = _call(server, "refresh_data", {})
        assert result.isError
        assert "not configured" in result.content[0].text
