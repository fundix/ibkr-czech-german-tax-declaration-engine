# src/webapp/routes.py
"""HTTP routes of the local web GUI — thin wrappers over services.RunService."""
import logging
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
    return _tpl(
        request, "index.html",
        datasets=svc.list_years(),
        runs=svc.list_runs(),
    )


@router.post("/runs", response_class=HTMLResponse)
def start_run(request: Request, tax_year: int = Form(...), fx_mode: str = Form("compare")):
    svc = _svc(request)
    try:
        job_id, run_id = svc.start_run(tax_year, fx_mode)
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
def files(request: Request, saved: int = 0):
    svc = _svc(request)
    return _tpl(request, "files.html", datasets=svc.list_years(), saved=saved,
                slots=settings.SLOT_FILES)


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
