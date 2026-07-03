# src/webapp/services.py
"""
Service layer of the local web GUI.

Every capability lives here first; web routes (and later MCP tools) are thin
wrappers. This module deliberately imports NO web framework — it is plain
engine-as-a-library orchestration, unit-testable without FastAPI.

Data layout:
- inputs:  ``data/webapp/<year>/<canonical name>`` (see settings.SLOT_FILES)
- runs:    ``out/webapp_runs/<run_id>/`` with ``inputs/`` (the exact merged
           files the engine consumed), ``meta.json``, ``result.<mode>.json``,
           ``result.<mode>.xlsx``, ``form.<mode>.json``

Trades and corporate actions are merged across ALL dataset years <= the tax
year (ascending) before a run: the engine reconstructs start-of-year FIFO
lots by replaying pre-tax-year trades, so history must be present. Cash
transactions are taken from the tax year only (out-of-year income events are
filtered by the engine anyway). ``positions_start`` falls back to the
previous year's ``positions_end``; ``corp_actions`` falls back to a
header-only file.
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.countries.cz.aggregation_service import run_cz_aggregation, run_cz_compare
from src.pipeline_runner import run_core_processing_pipeline
from src.webapp import settings
from src.webapp.jobs import JobRunner, JobState, engine_file_lock
from src.webapp.serializers import dump_json, load_json

logger = logging.getLogger(__name__)

FX_MODES = ("daily", "uniform", "compare")


@dataclass
class YearDataset:
    year: int
    files: Dict[str, Optional[Path]]  # slot -> path or None
    notes: List[str] = field(default_factory=list)

    @property
    def missing_required(self) -> List[str]:
        return [s for s in settings.REQUIRED_SLOTS if self.files.get(s) is None]

    @property
    def run_ready(self) -> bool:
        return not self.missing_required


class RunService:
    """Orchestrates datasets, engine runs, and persisted results."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        runs_dir: Optional[Path] = None,
        runner: Optional[JobRunner] = None,
    ):
        self.data_dir = Path(data_dir) if data_dir else settings.DATA_DIR
        self.runs_dir = Path(runs_dir) if runs_dir else settings.RUNS_DIR
        self.runner = runner or JobRunner()

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------

    def list_years(self) -> List[YearDataset]:
        datasets = []
        if self.data_dir.is_dir():
            for year_dir in sorted(self.data_dir.iterdir()):
                if not (year_dir.is_dir() and year_dir.name.isdigit()):
                    continue
                year = int(year_dir.name)
                files = {
                    slot: (year_dir / name if (year_dir / name).is_file() else None)
                    for slot, name in settings.SLOT_FILES.items()
                }
                ds = YearDataset(year=year, files=files)
                if files["positions_start"] is None:
                    prev = self._positions_end_of(year - 1)
                    ds.notes.append(
                        f"pozice na začátku roku: použije se konec roku {year - 1}"
                        if prev else
                        "pozice na začátku roku: prázdné (účet bez pozic na začátku roku)"
                    )
                if files["corp_actions"] is None:
                    ds.notes.append("korporátní akce: žádné (prázdný soubor)")
                datasets.append(ds)
        return datasets

    def get_year(self, year: int) -> Optional[YearDataset]:
        return next((d for d in self.list_years() if d.year == year), None)

    def save_upload(self, year: int, slot: str, content: bytes) -> Path:
        if slot not in settings.SLOT_FILES:
            raise ValueError(f"Neznámý typ souboru: {slot}")
        year_dir = self.data_dir / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        target = year_dir / settings.SLOT_FILES[slot]
        target.write_bytes(content)
        logger.info(f"Uploaded {slot} for {year} -> {target}")
        return target

    def _positions_end_of(self, year: int) -> Optional[Path]:
        p = self.data_dir / str(year) / settings.SLOT_FILES["positions_end"]
        return p if p.is_file() else None

    # ------------------------------------------------------------------
    # Input assembly
    # ------------------------------------------------------------------

    def _merge_years(self, slot: str, tax_year: int, target: Path) -> Optional[Path]:
        """Concatenate a slot's files across all dataset years <= tax_year."""
        sources = []
        for ds in self.list_years():
            if ds.year <= tax_year and ds.files.get(slot):
                sources.append((ds.year, ds.files[slot]))
        if not sources:
            return None

        header = None
        with open(target, "w", encoding="utf-8", newline="") as out:
            for year, src in sources:
                with open(src, encoding="utf-8-sig", newline="") as fh:
                    lines = fh.readlines()
                if not lines:
                    continue
                if header is None:
                    header = lines[0].strip()
                    out.write(lines[0] if lines[0].endswith("\n") else lines[0] + "\n")
                elif lines[0].strip() != header:
                    raise ValueError(
                        f"Soubor {src.name} pro rok {year} má jinou hlavičku než "
                        f"předchozí roky — soubory nelze sloučit. Vygenerujte "
                        f"všechny roky stejnou Flex Query šablonou."
                    )
                for line in lines[1:]:
                    if line.strip():
                        out.write(line if line.endswith("\n") else line + "\n")
        return target

    def _prepare_inputs(self, run_dir: Path, tax_year: int) -> Dict[str, Path]:
        ds = self.get_year(tax_year)
        if ds is None:
            raise ValueError(f"Pro rok {tax_year} nejsou nahraná žádná data.")
        if not ds.run_ready:
            missing = ", ".join(settings.SLOT_LABELS[s] for s in ds.missing_required)
            raise ValueError(f"Pro rok {tax_year} chybí: {missing}")

        inputs_dir = run_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)

        trades = self._merge_years("trades", tax_year, inputs_dir / "trades.csv")

        cash = inputs_dir / "cash_transactions.csv"
        shutil.copyfile(ds.files["cash"], cash)

        pos_end = inputs_dir / "positions_end.csv"
        shutil.copyfile(ds.files["positions_end"], pos_end)

        pos_start = inputs_dir / "positions_start.csv"
        if ds.files["positions_start"]:
            shutil.copyfile(ds.files["positions_start"], pos_start)
        else:
            prev = self._positions_end_of(tax_year - 1)
            if prev:
                shutil.copyfile(prev, pos_start)
            else:
                pos_start.write_text(settings.POSITIONS_HEADER, encoding="utf-8")

        corp = self._merge_years("corp_actions", tax_year, inputs_dir / "corporate_actions.csv")
        if corp is None:
            corp = inputs_dir / "corporate_actions.csv"
            corp.write_text(settings.CORP_ACTIONS_HEADER, encoding="utf-8")

        return {
            "trades": trades,
            "cash": cash,
            "positions_start": pos_start,
            "positions_end": pos_end,
            "corp_actions": corp,
        }

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(self, tax_year: int, fx_mode: str) -> Tuple[str, str]:
        """Submit a run to the single-worker executor; returns (job_id, run_id).

        Dataset readiness is validated HERE, before submitting — the user gets
        the error immediately instead of a job that fails a poll later.
        """
        if fx_mode not in FX_MODES:
            raise ValueError(f"Neznámý kurzový režim: {fx_mode}")
        ds = self.get_year(tax_year)
        if ds is None:
            raise ValueError(f"Pro rok {tax_year} nejsou nahraná žádná data.")
        if not ds.run_ready:
            missing = ", ".join(settings.SLOT_LABELS[s] for s in ds.missing_required)
            raise ValueError(f"Pro rok {tax_year} chybí: {missing}")
        run_id = f"{tax_year}-{datetime.now():%Y%m%d-%H%M%S}"
        job_id = self.runner.submit(
            f"Výpočet {tax_year} ({fx_mode})",
            self._execute_run, run_id, tax_year, fx_mode,
        )
        return job_id, run_id

    def _execute_run(
        self,
        run_id: str,
        tax_year: int,
        fx_mode: str,
        ecb_provider=None,
        cz_fx_provider=None,
    ) -> Dict[str, Any]:
        """Runs the full pipeline + CZ aggregation and persists everything.

        Executed on the JobRunner worker thread (decimal context set there);
        the provider overrides exist for offline tests.
        """
        started = time.monotonic()
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        inputs = self._prepare_inputs(run_dir, tax_year)

        with engine_file_lock():
            processing = run_core_processing_pipeline(
                trades_file_path=str(inputs["trades"]),
                cash_transactions_file_path=str(inputs["cash"]),
                positions_start_file_path=str(inputs["positions_start"]),
                positions_end_file_path=str(inputs["positions_end"]),
                corporate_actions_file_path=str(inputs["corp_actions"]),
                interactive_classification_mode=False,
                tax_year_to_process=tax_year,
                custom_rate_provider=ecb_provider,
                country_code="cz",
            )

            compare_lines: List[str] = []
            if fx_mode == "compare":
                comparison = run_cz_compare(processing, tax_year)
                compare_lines = list(comparison.render_lines())
                mode_results = [("daily", comparison.daily), ("uniform", comparison.uniform)]
            else:
                result = run_cz_aggregation(
                    processing, tax_year, fx_mode, fx_provider=cz_fx_provider
                )
                mode_results = [(fx_mode, result)]

        from src.countries.cz.exporters.json_exporter import export_cz_to_json
        from src.countries.cz.exporters.xlsx_exporter import export_cz_to_xlsx

        summary: Dict[str, Dict[str, Any]] = {}
        for mode, result in mode_results:
            export_cz_to_json(result, output=str(run_dir / f"result.{mode}.json"))
            export_cz_to_xlsx(result, output=str(run_dir / f"result.{mode}.xlsx"))

            cr = result.country_result or {}
            form_mapping = cr.get("form_mapping")
            if form_mapping is not None:
                dump_json(form_mapping.to_dict(), run_dir / f"form.{mode}.json")

            exported = load_json(run_dir / f"result.{mode}.json")
            liability = exported["sections"].get("cz_tax_liability", {}).get("line_items", {})
            summary[mode] = {
                "combined_base_czk": liability.get("combined_taxable_base_czk"),
                "final_tax_czk": liability.get("final_czech_tax_after_credit_czk"),
                "pending_review_count": exported.get("warnings", {}).get("pending_review_count", 0),
            }

        meta = {
            "run_id": run_id,
            "tax_year": tax_year,
            "fx_mode": fx_mode,
            "modes": [m for m, _ in mode_results],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(time.monotonic() - started, 1),
            "eoy_mismatch_error_count": processing.eoy_mismatch_error_count,
            "summary": summary,
            "compare_lines": compare_lines,
        }
        dump_json(meta, run_dir / "meta.json")
        logger.info(f"Run {run_id} finished in {meta['duration_s']} s.")
        return meta

    # ------------------------------------------------------------------
    # Reading persisted runs
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> Optional[JobState]:
        return self.runner.get(job_id)

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        metas = []
        if self.runs_dir.is_dir():
            for run_dir in self.runs_dir.iterdir():
                meta_path = run_dir / "meta.json"
                if meta_path.is_file():
                    try:
                        metas.append(load_json(meta_path))
                    except Exception:
                        logger.warning(f"Unreadable meta.json in {run_dir}")
        metas.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return metas[:limit]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        meta_path = self.runs_dir / run_id / "meta.json"
        return load_json(meta_path) if meta_path.is_file() else None

    def load_result(self, run_id: str, mode: str) -> Optional[Dict[str, Any]]:
        path = self.runs_dir / run_id / f"result.{mode}.json"
        return load_json(path) if path.is_file() else None

    def load_form(self, run_id: str, mode: str) -> Optional[Dict[str, Any]]:
        path = self.runs_dir / run_id / f"form.{mode}.json"
        return load_json(path) if path.is_file() else None

    def export_path(self, run_id: str, mode: str, fmt: str) -> Optional[Path]:
        if fmt not in ("json", "xlsx"):
            return None
        path = self.runs_dir / run_id / f"result.{mode}.{fmt}"
        return path if path.is_file() else None
