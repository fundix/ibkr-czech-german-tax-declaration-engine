# src/mcp_server/server.py
"""
Local MCP server for the IBKR tax engine.

Every tool is a thin wrapper over ``src/webapp/services.RunService`` — the
exact layer the web GUI uses, so Claude and the browser can never disagree.
The web server and this process share state only through files
(``out/webapp_runs/``, caches); concurrent pipeline runs across processes
are prevented by the ``engine_file_lock`` flock inside the service layer.

Registration (Claude Code):

    claude mcp add ibkr-tax -- uv --directory /path/to/repo run --extra mcp python -m src.mcp_server

Claude Desktop (`claude_desktop_config.json`):

    {"mcpServers": {"ibkr-tax": {"command": "uv", "args": ["--directory",
     "/path/to/repo", "run", "--extra", "mcp", "python", "-m", "src.mcp_server"]}}}
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from src.webapp.serializers import json_default
from src.webapp.services import RunService

logger = logging.getLogger(__name__)


def _jsonable(data: Any) -> Any:
    """Decimals/dates → JSON-safe primitives (MCP results must serialize)."""
    return json.loads(json.dumps(data, default=json_default))


def create_server(service: Optional[RunService] = None) -> FastMCP:
    svc = service or RunService()
    mcp = FastMCP(
        "ibkr-tax",
        instructions=(
            "Czech tax engine over the user's Interactive Brokers statements. "
            "Amounts are CZK unless suffixed otherwise; figures are a filing "
            "aid, not tax advice. Tools read the latest persisted run for a "
            "tax year — call run_pipeline first if data changed or no run "
            "exists; pass run_id (see list_datasets) to pin an older run, "
            "e.g. one computed with a different pairing method. To explain a "
            "method or FX difference, run run_pipeline twice and call "
            "compare_runs. The sale simulator converts at today's ČNB rate "
            "(approximation) and estimates the 15% rate only."
        ),
    )

    def _require_run(tax_year: int, run_id: Optional[str] = None) -> str:
        if run_id:
            meta = svc.get_run(run_id)
            if meta is None:
                raise ValueError(
                    f"Run '{run_id}' not found — list_datasets shows recent "
                    f"run ids."
                )
            if meta.get("tax_year") != tax_year:
                raise ValueError(
                    f"Run '{run_id}' is for tax year {meta.get('tax_year')}, "
                    f"not {tax_year}."
                )
            return run_id
        run_id = svc.latest_run_id(tax_year)
        if run_id is None:
            raise ValueError(
                f"No persisted run for tax year {tax_year}. "
                f"Call run_pipeline(tax_year={tax_year}) first."
            )
        return run_id

    def _default_mode(run_id: str) -> str:
        meta = svc.get_run(run_id) or {}
        modes = meta.get("modes") or ["daily"]
        return "daily" if "daily" in modes else modes[0]

    # ------------------------------------------------------------------

    @mcp.tool()
    def list_datasets() -> dict:
        """List uploaded IBKR datasets per tax year (file completeness) and the latest computed run for each year."""
        years = []
        for ds in svc.list_years():
            years.append({
                "tax_year": ds.year,
                "run_ready": ds.run_ready,
                "missing": ds.missing_required,
                "notes": ds.notes,
                "latest_run_id": svc.latest_run_id(ds.year),
            })
        return _jsonable({"datasets": years, "runs": svc.list_runs(limit=10)})

    @mcp.tool()
    def run_pipeline(tax_year: int, fx_mode: str = "compare", pairing_method: str = "fifo") -> dict:
        """Run the full tax computation for a year. fx_mode: daily | uniform | compare. pairing_method: fifo | lifo | weighted_average | optimal (the §10 lot-matching method for disposed securities; 'optimal' is the tax-minimising solver). Takes seconds with warm FX caches, up to ~1 min cold. Returns the run summary."""
        return _jsonable(svc.run_pipeline_sync(tax_year, fx_mode, pairing_method))

    @mcp.tool()
    def refresh_data(fx_mode: str = "compare") -> dict:
        """Download fresh Year-to-Date statements from IBKR (Flex Web Service) for the CURRENT year and recompute. Requires the token + query IDs configured on the web GUI's Files page. May take a few minutes (IBKR generates the statements server-side)."""
        from datetime import date as _date
        return _jsonable(svc.fetch_and_run_sync(_date.today().year, fx_mode))

    @mcp.tool()
    def get_tax_summary(tax_year: int, fx_mode: str = "daily",
                        run_id: Optional[str] = None) -> dict:
        """Tax result sections for a year (§8 dividends/interest, §10 securities/options netting, §38f foreign tax credit, final liability). Reads the latest run; pass run_id for an older one."""
        run_id = _require_run(tax_year, run_id)
        result = svc.load_result(run_id, fx_mode)
        if result is None:
            raise ValueError(
                f"Run {run_id} has no '{fx_mode}' result. Available modes: "
                f"{(svc.get_run(run_id) or {}).get('modes')}."
            )
        return _jsonable({
            "run_id": run_id,
            "metadata": result.get("metadata"),
            "sections": result.get("sections"),
            "warnings": result.get("warnings"),
            "compare": (svc.get_run(run_id) or {}).get("compare_lines"),
        })

    @mcp.tool()
    def get_form_mapping(tax_year: int, fx_mode: str = "daily",
                         run_id: Optional[str] = None) -> dict:
        """Czech DAP form lines with verified official line references (ř. 38, Příloha 2 ř. 209 → ř. 40, Příloha 3 ř. 321-330) — what to write where in the tax return."""
        run_id = _require_run(tax_year, run_id)
        form = svc.load_form(run_id, fx_mode)
        if form is None:
            raise ValueError(f"Run {run_id} has no '{fx_mode}' form mapping.")
        return _jsonable({"run_id": run_id, **form})

    @mcp.tool()
    def get_pending_review_items(tax_year: int, fx_mode: str = "daily",
                                 run_id: Optional[str] = None) -> dict:
        """Manual-review checklist: items flagged PENDING_MANUAL_REVIEW plus section-level REVIEW notes (e.g. FX conversions not computed, excluded margin interest)."""
        run_id = _require_run(tax_year, run_id)
        result = svc.load_result(run_id, fx_mode) or {}
        pending = [it for it in result.get("items", [])
                   if it.get("tax_review_status") == "PENDING_MANUAL_REVIEW"]
        notes = []
        for key, sec in (result.get("sections") or {}).items():
            for note in sec.get("notes", []):
                if "REVIEW" in note.upper() or "excluded" in note:
                    notes.append({"section": sec.get("label", key), "note": note})
        return _jsonable({"run_id": run_id, "pending_items": pending,
                          "section_notes": notes,
                          "warnings": result.get("warnings")})

    @mcp.tool()
    def get_positions(tax_year: int, run_id: Optional[str] = None) -> dict:
        """End-of-year open positions from the FIFO ledgers: per-position quantities, EUR cost basis, EOY valuation, and per-lot acquisition dates. Pass run_id to read the leftover lots of a specific run (pairing methods leave different lots open)."""
        run_id = _require_run(tax_year, run_id)
        pf = svc.load_portfolio(run_id)
        if pf is None:
            raise ValueError(f"Run {run_id} has no portfolio snapshot.")
        return _jsonable({"run_id": run_id, **pf})

    @mcp.tool()
    def get_time_test_status(tax_year: int, symbol: Optional[str] = None,
                             run_id: Optional[str] = None) -> dict:
        """Per-lot §4/1/u time-test countdown: for each open lot the 'exempt_from' date, days remaining, and status (exempt_now / running / not applicable for derivatives). Optionally filter by symbol."""
        run_id = _require_run(tax_year, run_id)
        overview = svc.time_test_overview(run_id, symbol)
        if overview is None:
            raise ValueError(f"Run {run_id} has no portfolio snapshot.")
        return _jsonable({"run_id": run_id, **overview})

    @mcp.tool()
    def get_dividends(tax_year: int, fx_mode: str = "daily",
                      run_id: Optional[str] = None) -> dict:
        """Dividend overview for a year: per-asset gross/withholding-tax CZK totals and per-month breakdown."""
        run_id = _require_run(tax_year, run_id)
        summary = svc.dividend_summary(run_id, fx_mode)
        if summary is None:
            raise ValueError(f"Run {run_id} has no '{fx_mode}' result.")
        return _jsonable({"run_id": run_id, **summary})

    @mcp.tool()
    def get_disposals(tax_year: int, symbol: Optional[str] = None,
                      fx_mode: str = "daily", include_lots: bool = False,
                      run_id: Optional[str] = None) -> dict:
        """Realized §10 disposals (movements) for a year: per-symbol totals — quantity sold, proceeds, cost basis, gain/loss CZK, taxable vs time-test-exempt split, first/last sale date. Pass symbol (a stock symbol also matches options on it, including German listings where the tickers differ, e.g. TUI1 ↔ 'C TUI …') or include_lots=true for per-lot pairing detail: which acquisition date each sale consumed, per-lot holding days and exemption. Answers 'why is PYPL a loss this year?' or 'what did I sell in March?'. An fx_warning field appears when some items have a failed FX→CZK conversion — their money legs count as 0, so those gains are unknown rather than zero."""
        run_id = _require_run(tax_year, run_id)
        summary = svc.disposal_summary(run_id, fx_mode, symbol=symbol,
                                       include_lots=include_lots)
        if summary is None:
            raise ValueError(
                f"Run {run_id} has no '{fx_mode}' result. Available modes: "
                f"{(svc.get_run(run_id) or {}).get('modes')}."
            )
        return _jsonable({"run_id": run_id, **summary})

    @mcp.tool()
    def compare_runs(run_id_a: str, run_id_b: str,
                     fx_mode: Optional[str] = None) -> dict:
        """Diff two computed runs (run ids from list_datasets): headline tax-liability lines plus per-symbol deltas — realized gain decomposed into proceeds vs cost-basis legs, plus taxable_gain_delta_czk so a pure §4/1/u exemption flip (same gross gain, different time-test outcome) still surfaces. All deltas are b − a. Use for what-ifs: run run_pipeline twice with different pairing_method (fifo vs lifo) or fx_mode (daily vs uniform), then compare. fx_mode defaults to a mode both runs share."""
        return _jsonable(svc.compare_runs(run_id_a, run_id_b, fx_mode))

    @mcp.tool()
    def simulate_sale(tax_year: int, symbol: str, quantity: float,
                      price: Optional[float] = None,
                      run_id: Optional[str] = None) -> dict:
        """Simulate selling N units of an open position: FIFO lots consumed, exempt vs taxable gain (§4/1/u), 100k annual-limit interplay with this year's realized proceeds, 15% tax estimate, and the wait-until date after which remaining lots become exempt. price is optional (live quote / EOY price used); converts at today's ČNB rate — an approximation, not tax advice."""
        run_id = _require_run(tax_year, run_id)
        sim = svc.simulate_sale(
            run_id, symbol,
            quantity=Decimal(str(quantity)),
            price=Decimal(str(price)) if price is not None else None,
        )
        return _jsonable({"run_id": run_id, **sim})

    return mcp
