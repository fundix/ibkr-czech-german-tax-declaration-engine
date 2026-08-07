# src/webapp/routes.py
"""HTTP routes of the local web GUI — thin wrappers over services.RunService."""
import logging
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from src.webapp import settings
from src.webapp.jobs import JobStatus

logger = logging.getLogger(__name__)

router = APIRouter()

PENDING_STATUS = "PENDING_MANUAL_REVIEW"
_FAVICON_ICO = Path(__file__).resolve().parent / "static" / "favicon" / "favicon.ico"


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Serve the icon at the root path browsers hard-code (the /static mount
    doesn't cover it)."""
    return FileResponse(_FAVICON_ICO)


def _tpl(request: Request, name: str, **ctx) -> HTMLResponse:
    templates = request.app.state.templates
    ctx.setdefault("slot_labels", settings.SLOT_LABELS)
    return templates.TemplateResponse(request, name, ctx)


def _svc(request: Request):
    return request.app.state.services


# ---------------------------------------------------------------------------
# Dashboard + runs
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Dashboard: net worth (live, lazy) + latest tax result per year + status."""
    svc = _svc(request)
    current_year = date.today().year
    flex = svc.get_flex_config()
    return _tpl(
        request, "index.html",
        overview=svc.dashboard_overview(),
        current_year=current_year,
        flex_configured=flex.configured,
        dataset_age=svc.dataset_age_hours(current_year),
    )


@router.get("/dashboard/valuation", response_class=HTMLResponse)
def dashboard_valuation(request: Request):
    """Lazy-loaded net-worth card — live quotes for the latest run in CZK."""
    svc = _svc(request)
    try:
        data = svc.get_dashboard_valuation()
    except Exception as exc:  # noqa: BLE001 — surface a friendly card, not a 500
        logger.exception("Dashboard valuation failed")
        return _tpl(request, "partials/job_error.html", error=f"Ocenění selhalo: {exc}")
    if data is None:
        return HTMLResponse("")
    live = data["live"]
    allocation = svc.allocation_slices(live["positions"])
    opt = svc.options_overview(data["run_id"])
    return _tpl(request, "partials/dashboard_valuation.html",
                run_id=data["run_id"], meta=data["meta"], live=live,
                allocation=allocation, options=opt["options"],
                options_as_of=opt["as_of"], snapshots=svc.list_snapshots())


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    """Control panel: run a calculation, dataset readiness, recent runs."""
    svc = _svc(request)
    current_year = date.today().year
    flex = svc.get_flex_config()
    return _tpl(
        request, "runs.html",
        datasets=svc.list_years(),
        runs=svc.list_runs(),
        current_year=current_year,
        flex_configured=flex.configured,
        auto_fetch=svc.should_auto_fetch(current_year),
        dataset_age=svc.dataset_age_hours(current_year),
    )


@router.post("/ibkr/fetch", response_class=HTMLResponse)
def ibkr_fetch(request: Request, tax_year: Optional[int] = Form(None)):
    svc = _svc(request)
    year = tax_year or date.today().year
    try:
        job_id, run_id = svc.start_fetch_and_run(year)
    except ValueError as exc:
        return _tpl(request, "partials/job_error.html", error=str(exc))
    return _tpl(request, "partials/job_status.html", job_id=job_id, run_id=run_id)


@router.post("/runs", response_class=HTMLResponse)
def start_run(
    request: Request,
    tax_year: int = Form(...),
    fx_mode: str = Form("compare"),
    pairing_method: str = Form("fifo"),
    force: bool = Form(False),
):
    svc = _svc(request)
    try:
        job_id, run_id = svc.start_run(tax_year, fx_mode, pairing_method, force=force)
    except ValueError as exc:
        return _tpl(request, "partials/job_error.html", error=str(exc))
    if job_id is None:  # identical inputs — reuse the cached run, skip recompute
        return Response(status_code=200, headers={"HX-Redirect": f"/results/{run_id}"})
    return _tpl(request, "partials/job_status.html", job_id=job_id, run_id=run_id)


@router.get("/runs/{job_id}/status", response_class=HTMLResponse)
def job_status(request: Request, job_id: str):
    svc = _svc(request)
    state = svc.get_job(job_id)
    if state is None:
        return _tpl(request, "partials/job_error.html", error="Neznámý běh.")
    if state.status == JobStatus.DONE:
        run_id = state.result.get("run_id") if isinstance(state.result, dict) else None
        return Response(status_code=200, headers={"HX-Redirect": f"/results/{run_id}"})
    if state.status == JobStatus.FAILED:
        return _tpl(request, "partials/job_error.html", error=state.error,
                    log_tail=list(state.log_tail)[-10:])
    return _tpl(request, "partials/job_status.html", job_id=job_id,
                state=state, log_tail=list(state.log_tail)[-6:])


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def _run_context(svc, run_id: str, mode: Optional[str]):
    meta = svc.get_run(run_id)
    if meta is None:
        return None
    modes = meta.get("modes", [])
    active = mode if mode in modes else (modes[0] if modes else None)
    return meta, modes, active


@router.get("/results/{run_id}", response_class=HTMLResponse)
def results(request: Request, run_id: str, mode: Optional[str] = None):
    svc = _svc(request)
    ctx = _run_context(svc, run_id, mode)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    meta, modes, active = ctx
    result = svc.load_result(run_id, active) if active else None
    return _tpl(request, "results.html", meta=meta, modes=modes, mode=active,
                result=result, page="results")


@router.get("/results/{run_id}/items", response_class=HTMLResponse)
def items(request: Request, run_id: str, mode: Optional[str] = None,
          section: str = "", status: str = ""):
    svc = _svc(request)
    ctx = _run_context(svc, run_id, mode)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    meta, modes, active = ctx
    result = svc.load_result(run_id, active) or {}
    rows = result.get("items", [])
    sections = sorted({it.get("section", "") for it in rows})
    if section:
        rows = [it for it in rows if it.get("section") == section]
    if status == "taxable":
        rows = [it for it in rows if it.get("included_in_tax_base")]
    elif status == "exempt":
        rows = [it for it in rows if it.get("is_exempt")]
    elif status == "pending":
        rows = [it for it in rows if it.get("tax_review_status") == PENDING_STATUS]
    return _tpl(request, "items.html", meta=meta, modes=modes, mode=active,
                items=rows, sections=sections, section=section, status=status,
                page="items")


@router.get("/results/{run_id}/form", response_class=HTMLResponse)
def form_mapping(request: Request, run_id: str, mode: Optional[str] = None):
    svc = _svc(request)
    ctx = _run_context(svc, run_id, mode)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    meta, modes, active = ctx
    form = svc.load_form(run_id, active) if active else None
    return _tpl(request, "form.html", meta=meta, modes=modes, mode=active,
                form=form, page="form")


@router.get("/results/{run_id}/review", response_class=HTMLResponse)
def review(request: Request, run_id: str, mode: Optional[str] = None):
    svc = _svc(request)
    ctx = _run_context(svc, run_id, mode)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    meta, modes, active = ctx
    result = svc.load_result(run_id, active) or {}
    pending = [it for it in result.get("items", [])
               if it.get("tax_review_status") == PENDING_STATUS]
    # Section-level REVIEW notes (e.g. FX conversions, excluded margin interest)
    section_notes = []
    for key, sec in (result.get("sections") or {}).items():
        for note in sec.get("notes", []):
            if "REVIEW" in note.upper() or "excluded" in note:
                section_notes.append({"section": sec.get("label", key), "note": note})
    return _tpl(request, "review.html", meta=meta, modes=modes, mode=active,
                pending=pending, section_notes=section_notes,
                warnings=result.get("warnings", {}), page="review")


@router.get("/results/{run_id}/portfolio", response_class=HTMLResponse)
def portfolio(request: Request, run_id: str, mode: Optional[str] = None):
    svc = _svc(request)
    ctx = _run_context(svc, run_id, mode)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    meta, modes, active = ctx
    pf = svc.load_portfolio(run_id)
    today = date.today()
    positions = (pf or {}).get("positions", [])
    exempt_qty = Decimal(0)
    soon_qty = Decimal(0)
    for pos in positions:
        for lot in pos.get("lots", []):
            deadline = lot.get("time_test_deadline")
            if deadline:
                d = date.fromisoformat(deadline)
                lot["days_remaining"] = (d - today).days + 1  # exempt AFTER deadline
                lot["exempt_from"] = (d + timedelta(days=1)).isoformat()
                if lot["days_remaining"] <= 0:
                    lot["tt_status"] = "exempt"
                    exempt_qty += Decimal(lot["quantity"])
                elif lot["days_remaining"] <= 90:
                    lot["tt_status"] = "soon"
                    soon_qty += Decimal(lot["quantity"])
                else:
                    lot["tt_status"] = "running"
            else:
                lot["tt_status"] = "none"
    return _tpl(request, "portfolio.html", meta=meta, modes=modes, mode=active,
                portfolio=pf, positions=positions, today=today.isoformat(),
                exempt_qty=exempt_qty, soon_qty=soon_qty, page="portfolio")


@router.get("/results/{run_id}/portfolio/live", response_class=HTMLResponse)
def portfolio_live(request: Request, run_id: str):
    svc = _svc(request)
    try:
        live = svc.get_live_portfolio(run_id)
    except Exception as exc:
        logger.exception("Live valuation failed")
        return _tpl(request, "partials/job_error.html", error=f"Ocenění selhalo: {exc}")
    if live is None:
        return HTMLResponse("")
    snapshots = svc.list_snapshots()
    allocation = svc.allocation_slices(live["positions"])
    return _tpl(request, "partials/portfolio_live.html", run_id=run_id, live=live,
                allocation=allocation, snapshots=snapshots)


@router.post("/results/{run_id}/portfolio/snapshot", response_class=HTMLResponse)
def save_snapshot(request: Request, run_id: str):
    svc = _svc(request)
    svc.save_snapshot(run_id)
    return RedirectResponse(f"/results/{run_id}/portfolio", status_code=303)


@router.get("/results/{run_id}/simulate", response_class=HTMLResponse)
def simulate_form(request: Request, run_id: str, mode: Optional[str] = None,
                  symbol: str = ""):
    svc = _svc(request)
    ctx = _run_context(svc, run_id, mode)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    meta, modes, active = ctx
    pf = svc.load_portfolio(run_id) or {}
    sellable = [p for p in pf.get("positions", [])
                if Decimal(str(p.get("quantity_long") or 0)) > 0]
    return _tpl(request, "simulate.html", meta=meta, modes=modes, mode=active,
                positions=sellable, selected=symbol, page="simulate")


@router.post("/results/{run_id}/simulate", response_class=HTMLResponse)
def simulate_run(request: Request, run_id: str,
                 symbol: str = Form(...), quantity: str = Form(...),
                 price: str = Form("")):
    svc = _svc(request)
    try:
        sim = svc.simulate_sale(
            run_id, symbol,
            quantity=Decimal(quantity.replace(",", ".")),
            price=Decimal(price.replace(",", ".")) if price.strip() else None,
        )
    except (ValueError, ArithmeticError) as exc:
        return _tpl(request, "partials/job_error.html", error=str(exc))
    return _tpl(request, "partials/sim_result.html", sim=sim)


@router.get("/results/{run_id}/dividends", response_class=HTMLResponse)
def dividends(request: Request, run_id: str, mode: Optional[str] = None):
    svc = _svc(request)
    ctx = _run_context(svc, run_id, mode)
    if ctx is None:
        return RedirectResponse("/", status_code=303)
    meta, modes, active = ctx
    summary = svc.dividend_summary(run_id, active) or {
        "assets": [], "months": [],
        "total_gross_czk": Decimal(0), "total_wht_czk": Decimal(0),
    }
    months = summary["months"]
    max_month = max((v for _, v in months), default=Decimal(0))
    return _tpl(request, "dividends.html", meta=meta, modes=modes, mode=active,
                assets=summary["assets"], months=months, max_month=max_month,
                total_czk=summary["total_gross_czk"],
                total_wht=summary["total_wht_czk"], page="dividends")


@router.get("/results/{run_id}/download/{mode}.{fmt}")
def download(request: Request, run_id: str, mode: str, fmt: str):
    svc = _svc(request)
    path = svc.export_path(run_id, mode, fmt)
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, filename=f"cz_tax_{run_id}.{mode}.{fmt}")


# ---------------------------------------------------------------------------
# Asset classification
# ---------------------------------------------------------------------------

def _default_classify_year(svc, requested: Optional[int]) -> Optional[int]:
    ready = [d.year for d in svc.list_years() if d.run_ready]
    if requested in ready:
        return requested
    return max(ready) if ready else None


@router.get("/classify", response_class=HTMLResponse)
def classify(request: Request, year: Optional[int] = None, saved: str = "", deleted: str = ""):
    svc = _svc(request)
    ready_years = [d.year for d in svc.list_years() if d.run_ready]
    active = _default_classify_year(svc, year)
    scan = error = None
    if active is not None:
        try:
            scan = svc.scan_unclassified_assets(active)
        except ValueError as exc:
            error = str(exc)
    return _tpl(request, "classify.html", year=active, ready_years=ready_years,
                scan=scan, error=error, choices=svc.classification_choices(),
                saved=saved, deleted=deleted)


@router.post("/classify")
def classify_save(request: Request, year: int = Form(...), key: str = Form(...),
                  choice: str = Form(...), notes: str = Form("")):
    svc = _svc(request)
    try:
        svc.save_classification(key, choice, notes)
    except ValueError:
        return RedirectResponse(f"/classify?year={year}", status_code=303)
    return RedirectResponse(f"/classify?year={year}&saved={key}", status_code=303)


@router.post("/classify/delete")
def classify_delete(request: Request, year: int = Form(...), key: str = Form(...)):
    svc = _svc(request)
    svc.delete_classification(key)
    return RedirectResponse(f"/classify?year={year}&deleted={key}", status_code=303)


@router.get("/classify/count", response_class=HTMLResponse)
def classify_count(request: Request, year: Optional[int] = None):
    """Lazy HTMX badge for the dashboard — a scan is too slow for page load."""
    svc = _svc(request)
    active = _default_classify_year(svc, year)
    if active is None:
        return HTMLResponse("")
    try:
        scan = svc.scan_unclassified_assets(active)
    except ValueError:
        return HTMLResponse("")
    return _tpl(request, "partials/classify_count.html", year=active, scan=scan)


# ---------------------------------------------------------------------------
# Sell targets (prodejní zóny)
# ---------------------------------------------------------------------------

def _targets_run(svc, run_id: Optional[str] = None):
    """Resolve the run the ladder is read against.

    Top-level page, so there is no run in the URL: default to the newest run
    of the highest tax year, exactly as get_dashboard_valuation() does. An
    explicit ?run_id= wins. Returns (run_id, meta) — both None when no run
    exists yet, which the page renders as an empty-state, not an error.
    """
    if run_id:
        meta = svc.get_run(run_id)
        if meta is not None:
            return run_id, meta
    latest = (svc.dashboard_overview() or {}).get("latest")
    if not latest:
        return None, None
    return latest.get("run_id"), latest


@router.get("/targets", response_class=HTMLResponse)
def targets(request: Request, run_id: Optional[str] = None, saved: str = "",
            deleted: str = "", error: str = "", imported: int = 0):
    svc = _svc(request)
    active_run, meta = _targets_run(svc, run_id)
    try:
        overview = svc.sell_targets_overview(active_run)
    except Exception as exc:  # noqa: BLE001 — a quote outage must not 500 the page
        logger.exception("Sell-target overview failed")
        return _tpl(request, "partials/job_error.html", error=f"Načtení selhalo: {exc}")
    pf = svc.load_portfolio(active_run) if active_run else None
    held = {p["symbol"]: p for p in (pf or {}).get("positions", [])
            if Decimal(str(p.get("quantity_long") or 0)) > 0}
    # Lot buckets per planned symbol, so each ladder can offer a lot picker.
    lots = {r["symbol"]: svc.position_lots(active_run, r["symbol"])
            for r in overview["rows"]} if active_run else {}
    # Options are excluded from the symbol picker: their key embeds expiry and
    # strike, so a ladder on one goes stale with the contract.
    choices = sorted(s for s, p in held.items() if p.get("category") != "OPTION")
    return _tpl(request, "targets.html", overview=overview, rows=overview["rows"],
                lots=lots, meta=meta, run_id=active_run, symbol_choices=choices,
                saved=saved, deleted=deleted, error=error, imported=imported)


@router.post("/targets/zone")
def targets_save_zone(request: Request, symbol: str = Form(...),
                      price: str = Form(...), quantity: str = Form(""),
                      zone_id: str = Form(""), note: str = Form(""),
                      lot_acquired: str = Form("")):
    svc = _svc(request)
    currency = isin = None
    active_run, _ = _targets_run(svc)
    for pos in (svc.load_portfolio(active_run) or {}).get("positions", []) if active_run else []:
        if pos.get("symbol") == symbol.strip():
            currency, isin = pos.get("eoy_currency"), pos.get("isin")
            break
    try:
        svc.save_sell_zone(symbol, price, quantity, zone_id=zone_id.strip() or None,
                           note=note, currency=currency, isin=isin,
                           lot_acquired=lot_acquired)
    except ValueError as exc:
        return RedirectResponse(f"/targets?error={quote_plus(str(exc))}", status_code=303)
    sym = quote_plus(symbol.strip())
    return RedirectResponse(f"/targets?saved={sym}#s-{sym}", status_code=303)


@router.post("/targets/zone/delete")
def targets_delete_zone(request: Request, symbol: str = Form(...),
                        zone_id: str = Form(...)):
    _svc(request).delete_sell_zone(symbol.strip(), zone_id)
    return RedirectResponse(f"/targets?deleted={quote_plus(symbol.strip())}",
                            status_code=303)


@router.post("/targets/zone/state")
def targets_zone_state(request: Request, symbol: str = Form(...),
                       zone_id: str = Form(...), action: str = Form(...)):
    svc = _svc(request)
    try:
        svc.set_zone_state(symbol.strip(), zone_id, action)
    except ValueError as exc:
        return RedirectResponse(f"/targets?error={quote_plus(str(exc))}", status_code=303)
    sym = quote_plus(symbol.strip())
    return RedirectResponse(f"/targets#s-{sym}", status_code=303)


@router.post("/targets/symbol/delete")
def targets_delete_symbol(request: Request, symbol: str = Form(...)):
    _svc(request).delete_sell_target(symbol.strip())
    return RedirectResponse(f"/targets?deleted={quote_plus(symbol.strip())}",
                            status_code=303)


@router.get("/targets/badge", response_class=HTMLResponse)
def targets_badge(request: Request):
    """Lazy HTMX badge for the nav — a store read only, no quotes.

    It therefore shows the state of the last evaluation, which happens when
    the dashboard or the portfolio page loads live prices.
    """
    count = _svc(request).sell_alert_count()
    if not count:
        return HTMLResponse("")        # empty so the nav does not jitter
    return _tpl(request, "partials/targets_badge.html", count=count)


@router.get("/targets/tax/{symbol}/{zone_id}", response_class=HTMLResponse)
def targets_zone_tax(request: Request, symbol: str, zone_id: str,
                     run_id: Optional[str] = None):
    """On-demand tax impact of one zone — one click, one simulation."""
    svc = _svc(request)
    active_run, _ = _targets_run(svc, run_id)
    try:
        impact = svc.zone_tax_impact(active_run, symbol, zone_id)
    except (ValueError, ArithmeticError) as exc:
        return _tpl(request, "partials/job_error.html", error=str(exc))
    return _tpl(request, "partials/targets_tax.html", **impact)


@router.get("/targets/import", response_class=HTMLResponse)
def targets_import_form(request: Request, run_id: Optional[str] = None):
    svc = _svc(request)
    active_run, meta = _targets_run(svc, run_id)
    return _tpl(request, "targets_import.html", meta=meta, run_id=active_run)


@router.post("/targets/import/preview", response_class=HTMLResponse)
def targets_import_preview(request: Request, text: str = Form(""),
                           run_id: str = Form("")):
    """Step 1 of 2: parse and show what was understood, write nothing.

    A 25-row paste with one bad decimal must never half-import, and pasted
    company names need a chance to be corrected before they become a plan.
    """
    svc = _svc(request)
    active_run, _ = _targets_run(svc, run_id or None)
    positions = svc._sellable_positions(svc.load_portfolio(active_run) if active_run else None)
    try:
        parsed = svc.parse_sell_targets(text, positions)
    except ValueError as exc:
        return _tpl(request, "partials/job_error.html", error=str(exc))
    if not parsed["rows"] and not parsed["skipped"]:
        return _tpl(request, "partials/job_error.html",
                    error="Nepodařilo se přečíst žádný řádek. Zkopíruj tabulku "
                          "včetně hlavičky (oddělovač tabulátor nebo středník).")
    return _tpl(request, "partials/targets_import_preview.html", parsed=parsed,
                symbol_choices=sorted(p["symbol"] for p in positions))


@router.post("/targets/import/apply")
def targets_import_apply(request: Request,
                         symbol: List[str] = Form([]),
                         price: List[str] = Form([]),
                         quantity: List[str] = Form([]),
                         include: List[int] = Form([]),
                         replace: str = Form("")):
    """Step 2 of 2: store exactly the rows shown in the preview.

    Parallel lists are full-length; `include` carries the indices still
    ticked, so unchecking a row cannot shift the others out of alignment.
    """
    svc = _svc(request)
    keep = set(include)
    rows = [
        {"symbol": symbol[i], "price": price[i], "quantity": quantity[i]}
        for i in sorted(keep)
        if i < len(symbol) and i < len(price) and i < len(quantity)
    ]
    try:
        result = svc.import_sell_targets(rows, replace=bool(replace))
    except ValueError as exc:
        return RedirectResponse(f"/targets/import?error={quote_plus(str(exc))}",
                                status_code=303)
    return RedirectResponse(f"/targets?imported={result['imported']}", status_code=303)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@router.get("/files", response_class=HTMLResponse)
def files(request: Request, saved: int = 0, flex_saved: int = 0, deleted: int = 0):
    svc = _svc(request)
    return _tpl(request, "files.html", datasets=svc.list_years(), saved=saved,
                slots=settings.SLOT_FILES, flex=svc.get_flex_config(),
                flex_saved=flex_saved, deleted=deleted)


@router.post("/files/delete-year")
def delete_year(request: Request, tax_year: int = Form(...)):
    svc = _svc(request)
    try:
        svc.delete_year_dataset(tax_year)
    except ValueError:
        return RedirectResponse("/files", status_code=303)
    return RedirectResponse(f"/files?deleted={tax_year}", status_code=303)


@router.post("/files/flex")
def save_flex(request: Request, token: str = Form(""),
              q_trades: str = Form(""), q_cash: str = Form(""),
              q_positions: str = Form(""), q_corp_actions: str = Form(""),
              first_year: str = Form("")):
    svc = _svc(request)
    svc.save_flex_settings(token, {
        "trades": q_trades, "cash": q_cash,
        "positions": q_positions, "corp_actions": q_corp_actions,
    }, first_year=first_year)
    return RedirectResponse("/files?flex_saved=1", status_code=303)


@router.post("/files/upload")
async def upload(request: Request, tax_year: int = Form(...)):
    svc = _svc(request)
    form = await request.form()
    saved = 0
    for slot in settings.SLOT_FILES:
        upload_file = form.get(slot)
        if isinstance(upload_file, UploadFile) and upload_file.filename:
            content = await upload_file.read()
            if content.strip():
                svc.save_upload(tax_year, slot, content)
                saved += 1
    return RedirectResponse(f"/files?saved={saved}", status_code=303)
