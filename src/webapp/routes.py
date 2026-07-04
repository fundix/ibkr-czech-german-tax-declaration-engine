# src/webapp/routes.py
"""HTTP routes of the local web GUI — thin wrappers over services.RunService."""
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from src.webapp import settings
from src.webapp.jobs import JobStatus

logger = logging.getLogger(__name__)

router = APIRouter()

PENDING_STATUS = "PENDING_MANUAL_REVIEW"


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
    svc = _svc(request)
    current_year = date.today().year
    flex = svc.get_flex_config()
    return _tpl(
        request, "index.html",
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
):
    svc = _svc(request)
    try:
        job_id, run_id = svc.start_run(tax_year, fx_mode, pairing_method)
    except ValueError as exc:
        return _tpl(request, "partials/job_error.html", error=str(exc))
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
    allocation = [
        {"label": p["symbol"], "value": str(p["value_czk"])}
        for p in live["positions"] if p.get("value_czk")
    ][:12]
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
