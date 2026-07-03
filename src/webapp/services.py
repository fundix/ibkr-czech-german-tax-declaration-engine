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
           ``result.<mode>.xlsx``, ``result.<mode>.pdf``, ``form.<mode>.json``

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
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.countries.cz.aggregation_service import run_cz_aggregation, run_cz_compare
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.time_test import time_test_deadline
from src.pipeline_runner import run_core_processing_pipeline
from src.utils.type_utils import parse_ibkr_date
from src.webapp import settings
from src.webapp.ibkr_flex import (
    FLEX_SLOTS,
    INTER_QUERY_DELAY_S,
    FlexConfig,
    fetch_statement,
    load_flex_config,
    save_flex_config,
)
from src.webapp.jobs import JobRunner, JobState, engine_file_lock
from src.webapp.serializers import dump_json, load_json

logger = logging.getLogger(__name__)

FX_MODES = ("daily", "uniform", "compare")


def _effective_fx_mode(fx_mode: str, tax_year: int, current_year: int):
    """Downgrade compare→daily for the RUNNING year.

    The GFŘ publishes the jednotný kurz only AFTER the year ends, so the
    uniform column of a running-year comparison cannot be computed (every
    conversion fails → nonsense totals + a wall of pending items)."""
    if fx_mode == "compare" and tax_year >= current_year:
        return "daily", [
            "Jednotný kurz pro běžící rok ještě neexistuje (GFŘ jej vyhlašuje "
            "až po konci roku) — spočítán pouze denní kurz ČNB."
        ]
    return fx_mode, []


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
        quote_service=None,
        converter_factory=None,
    ):
        self.data_dir = Path(data_dir) if data_dir else settings.DATA_DIR
        self.runs_dir = Path(runs_dir) if runs_dir else settings.RUNS_DIR
        self.runner = runner or JobRunner()
        from src.webapp.quotes import QuoteService
        self.quotes = quote_service or QuoteService(
            overrides_path=self.data_dir / "symbol_map.json"
        )
        # Factory (not instance): the CNB provider is built lazily on the
        # worker thread; tests inject a stub with fixed rates.
        self._converter_factory = converter_factory or self._cz_converter

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

        fx_mode, run_notes = _effective_fx_mode(fx_mode, tax_year, datetime.now().year)

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
        from src.countries.cz.exporters.pdf_exporter import export_cz_to_pdf
        from src.countries.cz.exporters.xlsx_exporter import export_cz_to_xlsx

        summary: Dict[str, Dict[str, Any]] = {}
        for mode, result in mode_results:
            export_cz_to_json(result, output=str(run_dir / f"result.{mode}.json"))
            export_cz_to_xlsx(result, output=str(run_dir / f"result.{mode}.xlsx"))
            export_cz_to_pdf(result, output=str(run_dir / f"result.{mode}.pdf"))

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

        dump_json(
            self._build_portfolio(processing, tax_year),
            run_dir / "portfolio.json",
        )

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
            "notes": run_notes,
        }
        dump_json(meta, run_dir / "meta.json")
        logger.info(f"Run {run_id} finished in {meta['duration_s']} s.")
        return meta

    # ------------------------------------------------------------------
    # IBKR Flex Web Service (automated statement download)
    # ------------------------------------------------------------------

    # slot -> canonical dataset file. Positions land as positions_end: for
    # the RUNNING year that means "state as of the last business day" — the
    # engine then validates FIFO against the current holdings and the tax
    # summary is a running estimate.
    _FLEX_SLOT_FILES = {
        "trades": "trades.csv",
        "cash": "cash_transactions.csv",
        "positions": "positions_end.csv",
        "corp_actions": "corporate_actions.csv",
    }

    @property
    def flex_config_path(self) -> Path:
        return self.data_dir / "ibkr_flex.json"

    def get_flex_config(self) -> FlexConfig:
        return load_flex_config(self.flex_config_path)

    def save_flex_settings(self, token: str, queries: Dict[str, str]) -> None:
        cfg = self.get_flex_config()
        if token.strip():
            cfg.token = token.strip()
        cfg.queries = {k: v.strip() for k, v in queries.items() if v.strip()}
        save_flex_config(self.flex_config_path, cfg)

    def dataset_age_hours(self, tax_year: int) -> Optional[float]:
        """Hours since the newest dataset file for the year; None if absent."""
        year_dir = self.data_dir / str(tax_year)
        mtimes = [f.stat().st_mtime for f in year_dir.glob("*.csv")] if year_dir.is_dir() else []
        if not mtimes:
            return None
        return (time.time() - max(mtimes)) / 3600

    def should_auto_fetch(self, tax_year: int, max_age_hours: float = 12.0) -> bool:
        if not self.get_flex_config().configured:
            return False
        age = self.dataset_age_hours(tax_year)
        return age is None or age > max_age_hours

    def start_fetch_and_run(self, tax_year: int, fx_mode: str = "compare") -> Tuple[str, str]:
        """Download fresh YTD statements from IBKR, then recompute — one job."""
        if not self.get_flex_config().configured:
            raise ValueError(
                "IBKR Flex Web Service není nastavená — vyplňte token a query ID "
                "na stránce Soubory."
            )
        run_id = f"{tax_year}-{datetime.now():%Y%m%d-%H%M%S}"
        job_id = self.runner.submit(
            f"Stažení z IBKR + výpočet {tax_year} ({fx_mode})",
            self._fetch_and_run, run_id, tax_year, fx_mode,
        )
        return job_id, run_id

    def fetch_and_run_sync(self, tax_year: int, fx_mode: str = "compare") -> Dict[str, Any]:
        """Synchronous variant for MCP tools."""
        if not self.get_flex_config().configured:
            raise ValueError("IBKR Flex Web Service is not configured "
                             "(token + query IDs in data/webapp/ibkr_flex.json).")
        run_id = f"{tax_year}-{datetime.now():%Y%m%d-%H%M%S}"
        return self.runner.run_sync(
            self._fetch_and_run, run_id, tax_year, fx_mode, timeout=900
        )

    def _fetch_and_run(
        self,
        run_id: str,
        tax_year: int,
        fx_mode: str,
        fetch=fetch_statement,
        pause=time.sleep,
    ) -> Dict[str, Any]:
        cfg = self.get_flex_config()
        year_dir = self.data_dir / str(tax_year)
        year_dir.mkdir(parents=True, exist_ok=True)
        fetched = []
        for slot in FLEX_SLOTS:
            query_id = cfg.queries.get(slot)
            if not query_id:
                continue
            if fetched:
                # IBKR throttles per-token bursts (error 1018) — space out
                # consecutive statement downloads.
                pause(INTER_QUERY_DELAY_S)
            logger.info(f"IBKR Flex: downloading {slot} (query {query_id})…")
            content = fetch(cfg.token, query_id)
            target = year_dir / self._FLEX_SLOT_FILES[slot]
            target.write_bytes(content)
            fetched.append(slot)
        if not fetched:
            raise ValueError("Žádná query ID nejsou nastavená.")
        logger.info(f"IBKR Flex: fetched {', '.join(fetched)} for {tax_year}.")
        meta = self._execute_run(run_id, tax_year, fx_mode)
        meta["fetched_slots"] = fetched
        return meta

    # ------------------------------------------------------------------
    # Portfolio (end-of-year open FIFO lots + time-test deadlines)
    # ------------------------------------------------------------------

    # §4/1/w applies to securities; derivatives never pass the time test.
    _TIME_TEST_CATEGORIES = {"STOCK", "BOND", "INVESTMENT_FUND"}

    def _build_portfolio(self, processing, tax_year: int) -> Dict[str, Any]:
        """Distill open FIFO lots into a JSON-safe portfolio snapshot.

        Valuation stays in the position's own currency (EOY mark price from
        the positions file); cost basis is EUR (engine-internal base). CZK
        conversion arrives with live quotes in a later phase.
        """
        cz_cfg = CzTaxConfig()
        positions = []
        for asset_id, ledger in (processing.fifo_ledgers_by_asset_id or {}).items():
            lots = getattr(ledger, "lots", [])
            short_lots = getattr(ledger, "short_lots", [])
            if not lots and not short_lots:
                continue
            asset = processing.asset_resolver.get_asset_by_id(asset_id)
            if asset is None:
                continue
            category = asset.asset_category.name if asset.asset_category else "UNKNOWN"
            time_test_applies = category in self._TIME_TEST_CATEGORIES

            lot_rows = []
            for lot in lots:
                estimated = str(lot.source_transaction_id).startswith("SOY_FALLBACK")
                acq = parse_ibkr_date(lot.acquisition_date)
                deadline = (
                    time_test_deadline(acq, cz_cfg)
                    if (time_test_applies and acq and not estimated) else None
                )
                lot_rows.append({
                    "acquisition_date": lot.acquisition_date,
                    "quantity": lot.quantity,
                    "unit_cost_eur": lot.unit_cost_basis_eur,
                    "total_cost_eur": lot.total_cost_basis_eur,
                    "acquisition_estimated": estimated,
                    # exempt when disposed of strictly AFTER the deadline
                    "time_test_deadline": deadline,
                })

            short_rows = [{
                "opening_date": s.opening_date,
                "quantity": s.quantity_shorted,
                "unit_proceeds_eur": s.unit_sale_proceeds_eur,
                "total_proceeds_eur": s.total_sale_proceeds_eur,
            } for s in short_lots]

            positions.append({
                "symbol": asset.ibkr_symbol,
                "isin": getattr(asset, "ibkr_isin", None),
                "description": asset.description,
                "category": category,
                "time_test_applicable": time_test_applies,
                "quantity_long": sum((l.quantity for l in lots), Decimal(0)),
                "quantity_short": sum((s.quantity_shorted for s in short_lots), Decimal(0)),
                "total_cost_eur": sum((l.total_cost_basis_eur for l in lots), Decimal(0)),
                "eoy_quantity": asset.eoy_quantity,
                "eoy_market_price": asset.eoy_market_price,
                "eoy_currency": asset.eoy_mark_price_currency,
                "eoy_position_value": asset.eoy_position_value,
                "lots": lot_rows,
                "short_lots": short_rows,
            })

        positions.sort(key=lambda p: (p["symbol"] or ""))
        return {
            "tax_year": tax_year,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "positions": positions,
        }

    def load_portfolio(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = self.runs_dir / run_id / "portfolio.json"
        return load_json(path) if path.is_file() else None

    # ------------------------------------------------------------------
    # Live valuation (quotes + today's CZK), sale simulator, snapshots
    # ------------------------------------------------------------------

    def _cz_converter(self):
        """Daily-ČNB converter for 'today' valuations (network-backed cache)."""
        from src.countries.cz.fx_policy import CzCurrencyConverter
        from src.utils.fx_provider_factory import create_fx_provider

        cfg = CzTaxConfig()
        provider = create_fx_provider(
            cfg.fx_policy.source, cache_file_path=cfg.cnb_cache_file_path
        )
        return CzCurrencyConverter(provider, cfg.fx_policy)

    def _to_czk(self, converter, amount: Decimal, currency: str, on_date) -> Optional[Decimal]:
        if converter is None or amount is None:
            return None
        try:
            rec = converter.convert_to_czk(Decimal(str(amount)), currency, on_date)
            return rec.converted_amount_czk if rec else None
        except Exception:
            return None

    def get_live_portfolio(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Portfolio snapshot augmented with live quotes + CZK valuation.

        Runs on the single-worker executor (shares FX caches with engine
        runs; worker has the decimal context)."""
        pf = self.load_portfolio(run_id)
        if pf is None:
            return None
        return self.runner.run_sync(self._compute_live_portfolio, pf, timeout=120)

    def _compute_live_portfolio(self, pf: Dict[str, Any]) -> Dict[str, Any]:
        from datetime import date as _date
        today = _date.today()
        converter = self._converter_factory()
        quotes_ok = 0
        total_value_czk = Decimal(0)
        total_cost_czk = Decimal(0)
        rows = []
        for pos in pf.get("positions", []):
            qty = Decimal(str(pos.get("quantity_long") or 0))
            if qty == 0:
                continue
            row = dict(pos)
            quote = None
            if pos.get("category") != "OPTION":
                quote = self.quotes.get_quote(pos.get("symbol") or "", pos.get("eoy_currency") or "USD")
            if quote is not None:
                price, currency, price_source = quote.price, quote.currency, "live"
                quotes_ok += 1
            elif pos.get("eoy_market_price"):
                price = Decimal(str(pos["eoy_market_price"]))
                currency = pos.get("eoy_currency") or "USD"
                price_source = "eoy"
            else:
                price, currency, price_source = None, None, "none"

            value_czk = cost_czk = unrealized = pct = None
            value_ccy = None
            if price is not None:
                value_ccy = qty * price
                value_czk = self._to_czk(converter, value_ccy, currency, today)
                cost_czk = self._to_czk(
                    converter, Decimal(str(pos.get("total_cost_eur") or 0)), "EUR", today
                )
                if value_czk is not None and cost_czk is not None:
                    unrealized = value_czk - cost_czk
                    pct = (unrealized / cost_czk * 100) if cost_czk else None
                    total_value_czk += value_czk
                    total_cost_czk += cost_czk
            row.update({
                "live_price": price, "live_currency": currency,
                "price_source": price_source, "value_ccy": value_ccy,
                "value_czk": value_czk, "cost_czk": cost_czk,
                "unrealized_czk": unrealized, "unrealized_pct": pct,
            })
            rows.append(row)

        rows.sort(key=lambda r: r.get("value_czk") or Decimal(0), reverse=True)
        result = {
            "as_of": today.isoformat(),
            "tax_year": pf.get("tax_year"),
            "positions": rows,
            "quotes_ok": quotes_ok,
            "quotes_total": sum(1 for r in rows if r.get("category") != "OPTION"),
            "total_value_czk": total_value_czk if total_value_czk else None,
            "total_cost_czk": total_cost_czk if total_cost_czk else None,
            "total_unrealized_czk": (total_value_czk - total_cost_czk) if total_value_czk else None,
        }
        if result["total_value_czk"]:
            self._maybe_save_snapshot(pf.get("tax_year"), result)
        return result

    # -- sale simulator -------------------------------------------------

    def simulate_sale(
        self,
        run_id: str,
        symbol: str,
        quantity: Decimal,
        price: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        pf = self.load_portfolio(run_id)
        if pf is None:
            raise ValueError("Pro tento běh není portfolio k dispozici.")
        pos = next((p for p in pf.get("positions", []) if p.get("symbol") == symbol), None)
        if pos is None:
            raise ValueError(f"Pozice {symbol} v portfoliu není.")
        meta = self.get_run(run_id) or {}
        result = self.load_result(run_id, (meta.get("modes") or ["daily"])[0]) or {}
        return self.runner.run_sync(
            self._compute_simulation, pos, quantity, price, result, timeout=120
        )

    def _compute_simulation(
        self,
        pos: Dict[str, Any],
        quantity: Decimal,
        price: Optional[Decimal],
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        from datetime import date as _date, timedelta as _timedelta
        today = _date.today()
        converter = self._converter_factory()
        currency = pos.get("eoy_currency") or "USD"

        price_source = "manual"
        if price is None:
            quote = None
            if pos.get("category") != "OPTION":
                quote = self.quotes.get_quote(pos.get("symbol") or "", currency)
            if quote is not None:
                price, currency, price_source = quote.price, quote.currency, "live"
            elif pos.get("eoy_market_price"):
                price = Decimal(str(pos["eoy_market_price"]))
                price_source = "eoy"
            else:
                raise ValueError("Cena není k dispozici — zadejte ji ručně.")

        available = Decimal(str(pos.get("quantity_long") or 0))
        qty = min(Decimal(str(quantity)), available)
        if qty <= 0:
            raise ValueError("Počet kusů musí být kladný.")

        time_test_applies = bool(pos.get("time_test_applicable"))
        remaining = qty
        consumed = []
        exempt_gain = Decimal(0)
        taxable_gain = Decimal(0)
        estimated_involved = False
        latest_deadline = None
        for lot in pos.get("lots", []):
            if remaining <= 0:
                break
            lot_qty = Decimal(str(lot["quantity"]))
            take = min(lot_qty, remaining)
            remaining -= take

            proceeds_czk = self._to_czk(converter, take * price, currency, today)
            cost_czk = self._to_czk(
                converter, take * Decimal(str(lot["unit_cost_eur"])), "EUR", today
            )
            gain = (proceeds_czk - cost_czk) if (proceeds_czk is not None and cost_czk is not None) else None

            deadline = lot.get("time_test_deadline")
            deadline_d = _date.fromisoformat(deadline) if deadline else None
            estimated = bool(lot.get("acquisition_estimated"))
            estimated_involved = estimated_involved or estimated
            exempt = bool(
                time_test_applies and deadline_d is not None and today > deadline_d
            )
            if gain is not None:
                if exempt:
                    exempt_gain += gain
                else:
                    taxable_gain += gain
            if deadline_d is not None and not exempt:
                latest_deadline = max(latest_deadline or deadline_d, deadline_d)

            consumed.append({
                "acquisition_date": lot["acquisition_date"],
                "quantity": take,
                "unit_cost_eur": lot["unit_cost_eur"],
                "cost_czk": cost_czk,
                "proceeds_czk": proceeds_czk,
                "gain_czk": gain,
                "exempt": exempt,
                "estimated": estimated,
                "exempt_from": (
                    (deadline_d + _timedelta(days=1)).isoformat() if deadline_d else None
                ),
            })

        proceeds_total_czk = self._to_czk(converter, qty * price, currency, today)

        # 100k annual limit interplay: simulated proceeds add to this year's
        # already-realized eligible proceeds.
        limit_items = (result.get("sections", {})
                       .get("cz_10_summary", {}).get("line_items", {}))
        existing = Decimal(str(limit_items.get("annual_limit_eligible_proceeds_czk") or 0))
        threshold = Decimal(str(limit_items.get("annual_limit_threshold_czk") or 100000))
        combined = existing + (proceeds_total_czk or Decimal(0))
        under_limit = time_test_applies and combined <= threshold

        tax = Decimal(0)
        if not under_limit and taxable_gain > 0:
            tax = (taxable_gain * Decimal("0.15")).quantize(Decimal("0.01"))

        return {
            "symbol": pos.get("symbol"),
            "description": pos.get("description"),
            "as_of": today.isoformat(),
            "quantity": qty,
            "available": available,
            "price": price,
            "currency": currency,
            "price_source": price_source,
            "proceeds_czk": proceeds_total_czk,
            "consumed": consumed,
            "exempt_gain_czk": exempt_gain,
            "taxable_gain_czk": taxable_gain,
            "estimated_involved": estimated_involved,
            "time_test_applicable": time_test_applies,
            "annual_limit": {
                "existing_czk": existing,
                "combined_czk": combined,
                "threshold_czk": threshold,
                "under_limit": under_limit,
            },
            "estimated_tax_czk": tax,
            "wait_until": (
                (latest_deadline + _timedelta(days=1)).isoformat()
                if latest_deadline else None
            ),
        }

    # -- portfolio value snapshots (SQLite) ------------------------------

    def _snapshot_db(self):
        import sqlite3
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.data_dir / "portfolio.db")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS snapshots ("
            " taken_at TEXT NOT NULL,"
            " tax_year INTEGER,"
            " total_value_czk TEXT NOT NULL,"
            " total_cost_czk TEXT,"
            " quotes_ok INTEGER)"
        )
        return conn

    def _maybe_save_snapshot(self, tax_year, live: Dict[str, Any]) -> None:
        """At most one automatic snapshot per day (manual saves unrestricted)."""
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            with self._snapshot_db() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE substr(taken_at, 1, 10) = ?",
                    (today,),
                ).fetchone()
                if row[0] == 0:
                    self._insert_snapshot(conn, tax_year, live)
        except Exception as exc:
            logger.warning(f"Snapshot save failed: {exc}")

    def save_snapshot(self, run_id: str) -> None:
        live = self.get_live_portfolio(run_id)
        if live and live.get("total_value_czk"):
            with self._snapshot_db() as conn:
                self._insert_snapshot(conn, live.get("tax_year"), live)

    def _insert_snapshot(self, conn, tax_year, live: Dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO snapshots (taken_at, tax_year, total_value_czk, total_cost_czk, quotes_ok)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                tax_year,
                str(live.get("total_value_czk")),
                str(live.get("total_cost_czk") or ""),
                live.get("quotes_ok") or 0,
            ),
        )

    def list_snapshots(self, limit: int = 365) -> List[Dict[str, Any]]:
        try:
            with self._snapshot_db() as conn:
                rows = conn.execute(
                    "SELECT taken_at, total_value_czk FROM snapshots ORDER BY taken_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {"taken_at": r[0], "total_value_czk": r[1]}
                for r in reversed(rows)
            ]
        except Exception:
            return []

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

    def latest_run_id(self, tax_year: int) -> Optional[str]:
        for meta in self.list_runs(limit=100):
            if meta.get("tax_year") == tax_year:
                return meta.get("run_id")
        return None

    def run_pipeline_sync(self, tax_year: int, fx_mode: str = "compare") -> Dict[str, Any]:
        """Run the full pipeline synchronously (MCP tools wait for the result)."""
        if fx_mode not in FX_MODES:
            raise ValueError(f"Neznámý kurzový režim: {fx_mode}")
        run_id = f"{tax_year}-{datetime.now():%Y%m%d-%H%M%S}"
        return self.runner.run_sync(
            self._execute_run, run_id, tax_year, fx_mode, timeout=600
        )

    def dividend_summary(self, run_id: str, mode: str) -> Optional[Dict[str, Any]]:
        """Per-asset and per-month dividend aggregation from a persisted run.

        Single source for the web dividends page AND the MCP tool."""
        result = self.load_result(run_id, mode)
        if result is None:
            return None
        by_asset: Dict[str, Dict[str, Any]] = {}
        by_month: Dict[str, Decimal] = {}
        total_czk = Decimal(0)
        total_wht = Decimal(0)
        for it in result.get("items", []):
            if it.get("item_type") not in ("DIVIDEND", "FUND_DISTRIBUTION"):
                continue
            sym = it.get("asset_symbol") or "?"
            a = by_asset.setdefault(sym, {
                "symbol": sym, "description": it.get("asset_description"),
                "country": it.get("source_country"), "count": 0,
                "gross_czk": Decimal(0), "wht_czk": Decimal(0),
            })
            gross = Decimal(it.get("amount_czk") or 0)
            wht = Decimal(it.get("wht_total_czk") or 0)
            a["count"] += 1
            a["gross_czk"] += gross
            a["wht_czk"] += wht
            total_czk += gross
            total_wht += wht
            month = (it.get("event_date") or "")[:7]
            by_month[month] = by_month.get(month, Decimal(0)) + gross
        TWO = Decimal("0.01")
        for a in by_asset.values():
            a["gross_czk"] = a["gross_czk"].quantize(TWO)
            a["wht_czk"] = a["wht_czk"].quantize(TWO)
        return {
            "assets": sorted(by_asset.values(), key=lambda a: a["gross_czk"], reverse=True),
            "months": [(m, v.quantize(TWO)) for m, v in sorted(by_month.items())],
            "total_gross_czk": total_czk.quantize(TWO),
            "total_wht_czk": total_wht.quantize(TWO),
        }

    def time_test_overview(self, run_id: str, symbol: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Per-lot §4/1/w countdown computed from the persisted portfolio."""
        from datetime import date as _date, timedelta as _timedelta
        pf = self.load_portfolio(run_id)
        if pf is None:
            return None
        today = _date.today()
        positions = []
        for pos in pf.get("positions", []):
            if symbol and pos.get("symbol") != symbol:
                continue
            lots = []
            for lot in pos.get("lots", []):
                deadline = lot.get("time_test_deadline")
                entry = {
                    "acquisition_date": lot.get("acquisition_date"),
                    "quantity": lot.get("quantity"),
                    "acquisition_estimated": bool(lot.get("acquisition_estimated")),
                }
                if not pos.get("time_test_applicable"):
                    entry["status"] = "not_applicable_derivative"
                elif deadline is None:
                    entry["status"] = "unknown_verify_manually"
                else:
                    d = _date.fromisoformat(deadline)
                    entry["exempt_from"] = (d + _timedelta(days=1)).isoformat()
                    days = (d - today).days + 1
                    entry["days_remaining"] = max(days, 0)
                    entry["status"] = "exempt_now" if days <= 0 else "running"
                lots.append(entry)
            positions.append({
                "symbol": pos.get("symbol"),
                "description": pos.get("description"),
                "category": pos.get("category"),
                "quantity_long": pos.get("quantity_long"),
                "time_test_applicable": pos.get("time_test_applicable"),
                "lots": lots,
            })
        return {"as_of": today.isoformat(), "tax_year": pf.get("tax_year"),
                "positions": positions}

    def load_result(self, run_id: str, mode: str) -> Optional[Dict[str, Any]]:
        path = self.runs_dir / run_id / f"result.{mode}.json"
        return load_json(path) if path.is_file() else None

    def load_form(self, run_id: str, mode: str) -> Optional[Dict[str, Any]]:
        path = self.runs_dir / run_id / f"form.{mode}.json"
        return load_json(path) if path.is_file() else None

    def export_path(self, run_id: str, mode: str, fmt: str) -> Optional[Path]:
        if fmt not in ("json", "xlsx", "pdf"):
            return None
        path = self.runs_dir / run_id / f"result.{mode}.{fmt}"
        return path if path.is_file() else None
