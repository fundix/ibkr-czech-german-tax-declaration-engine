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

# Marker stamped into the older LIFO run's payload so run_id tests can prove
# which run was actually READ, not merely which id was echoed back.
SENTINEL = "SENTINEL_PINNED_LIFO"

EXPECTED_TOOLS = {
    "list_datasets", "run_pipeline", "refresh_data", "get_tax_summary",
    "get_form_mapping", "get_pending_review_items", "get_positions",
    "get_time_test_status", "get_dividends", "get_disposals", "get_options",
    "compare_runs", "simulate_sale",
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
    # Older LIFO run FIRST so the golden FIFO run below stays the *latest*
    # run of 2024 — run_id-override tests then have a non-latest run to pin.
    svc._execute_run(
        "2024-mcp-lifo", 2024, "daily",
        ecb_provider=GoldenEcbProvider(),
        cz_fx_provider=GoldenCnbProvider(),
        pairing_method="lifo",
    )
    # The synthetic year has one lot per symbol, so FIFO ≡ LIFO and the two
    # runs are figure-identical. Without a distinguishing marker a run_id test
    # could only assert the echoed string — which passes even if the tool
    # loaded the latest run. Stamp the pinned run's payload instead.
    lifo_result = tmp / "runs" / "2024-mcp-lifo" / "result.daily.json"
    payload = json.loads(lifo_result.read_text())
    payload.setdefault("warnings", {})[SENTINEL] = True
    lifo_result.write_text(json.dumps(payload))
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

    def test_get_disposals_aggregates_golden(self, server):
        data = _payload(_call(server, "get_disposals", {"tax_year": 2024}))
        assert data["run_id"] == "2024-mcp"
        rows = {r["symbol"]: r for r in data["by_symbol"]}
        assert len(rows) == 3
        # Golden per-item figures pinned by test_golden_e2e_cz.py
        assert Decimal(rows["ALPHA"]["gain_loss_czk"]) == Decimal("19379.40")
        assert Decimal(rows["ALPHA"]["taxable_gain_loss_czk"]) == Decimal("19379.40")
        assert Decimal(rows["ALPHA"]["exempt_gain_loss_czk"]) == Decimal("0.00")
        assert Decimal(rows["OLDCO"]["gain_loss_czk"]) == Decimal("21810.98")
        assert Decimal(rows["OLDCO"]["exempt_gain_loss_czk"]) == Decimal("21810.98")
        assert Decimal(rows["OLDCO"]["taxable_gain_loss_czk"]) == Decimal("0.00")
        # Sorted by |gain| desc → OLDCO first
        assert data["by_symbol"][0]["symbol"] == "OLDCO"
        assert data["totals"]["count"] == 3
        # No symbol/include_lots → lots withheld, hint present
        assert data["lots"] == []
        assert "lot_detail" in data

    def test_get_disposals_symbol_lot_detail(self, server):
        data = _payload(_call(server, "get_disposals",
                              {"tax_year": 2024, "symbol": "OLDCO"}))
        assert [r["symbol"] for r in data["by_symbol"]] == ["OLDCO"]
        [lot] = data["lots"]
        assert lot["acquisition_date"] == "2020-06-15"
        assert lot["holding_period_days"] == 1435
        assert lot["is_exempt"] is True
        assert lot["is_taxable"] is False

    def test_get_disposals_symbol_matches_option_underlying(self, server):
        # Marker-first option key "P UNDR 20240315 95 M" ← underlying "UNDR"
        data = _payload(_call(server, "get_disposals",
                              {"tax_year": 2024, "symbol": "UNDR"}))
        assert len(data["by_symbol"]) == 1
        assert data["by_symbol"][0]["category"] == "OPTION"
        assert Decimal(data["by_symbol"][0]["gain_loss_czk"]) == Decimal("4657.74")

    def test_get_disposals_include_lots(self, server):
        data = _payload(_call(server, "get_disposals",
                              {"tax_year": 2024, "include_lots": True}))
        assert len(data["lots"]) == 3

    def test_get_disposals_unknown_symbol_is_empty_not_error(self, server):
        data = _payload(_call(server, "get_disposals",
                              {"tax_year": 2024, "symbol": "NOPE"}))
        assert data["by_symbol"] == [] and data["lots"] == []

    def test_run_id_override_actually_loads_the_pinned_run(self, server):
        # Latest 2024 run is "2024-mcp"; pin the older LIFO run explicitly.
        # The sentinel proves the PAYLOAD came from the pinned run — echoing
        # run_id alone would pass even if the tool read the latest run.
        pinned = _payload(_call(server, "get_tax_summary",
                                {"tax_year": 2024, "run_id": "2024-mcp-lifo"}))
        assert pinned["run_id"] == "2024-mcp-lifo"
        assert SENTINEL in pinned["warnings"]

        default = _payload(_call(server, "get_tax_summary", {"tax_year": 2024}))
        assert default["run_id"] == "2024-mcp"
        assert SENTINEL not in (default["warnings"] or {})

    @pytest.mark.parametrize("tool,extra", [
        ("get_tax_summary", {}),
        ("get_form_mapping", {}),
        ("get_pending_review_items", {}),
        ("get_positions", {}),
        ("get_time_test_status", {}),
        ("get_dividends", {}),
        ("get_disposals", {}),
        ("get_options", {"with_quotes": False}),
        ("simulate_sale", {"symbol": "DIVCO", "quantity": 1}),
    ])
    def test_every_tool_honours_run_id(self, server, tool, extra):
        """A tool that forgets to forward run_id to _require_run must fail."""
        data = _payload(_call(server, tool,
                              {"tax_year": 2024, "run_id": "2024-mcp-lifo",
                               **extra}))
        assert data["run_id"] == "2024-mcp-lifo"

    def test_compare_runs_fifo_vs_lifo(self, server):
        data = _payload(_call(server, "compare_runs",
                              {"run_id_a": "2024-mcp",
                               "run_id_b": "2024-mcp-lifo"}))
        assert data["mode"] == "daily"
        assert data["runs"]["a"]["pairing_method"] == "fifo"
        assert data["runs"]["b"]["pairing_method"] == "lifo"
        # Single-lot synthetic scenario: FIFO ≡ LIFO, nothing changes
        assert data["by_symbol"] == []
        assert len(data["unchanged_symbols"]) == 3
        final = next(r for r in data["liability"]
                     if r["line"] == "final_czech_tax_after_credit_czk")
        assert final["a"] == final["b"] == "3604.00"
        assert Decimal(final["delta"]) == 0

    def test_compare_runs_result_is_json_safe(self, server):
        """Decimals/dates must not leak through _jsonable into the client."""
        raw = _call(server, "compare_runs",
                    {"run_id_a": "2024-mcp", "run_id_b": "2024-mcp-lifo"})
        json.loads(raw.content[0].text)   # raises on a Decimal leak
        assert not raw.isError

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

    def test_unknown_run_id_reports_helpful_error(self, server):
        result = _call(server, "get_tax_summary",
                       {"tax_year": 2024, "run_id": "no-such-run"})
        assert result.isError
        assert "not found" in result.content[0].text

    def test_run_id_year_mismatch_rejected(self, server):
        result = _call(server, "get_tax_summary",
                       {"tax_year": 2031, "run_id": "2024-mcp"})
        assert result.isError
        assert "tax year" in result.content[0].text

    def test_compare_runs_unknown_id_rejected(self, server):
        result = _call(server, "compare_runs",
                       {"run_id_a": "2024-mcp", "run_id_b": "ghost"})
        assert result.isError
        assert "not found" in result.content[0].text
