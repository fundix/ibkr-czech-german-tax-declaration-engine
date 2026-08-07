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

import csv
import hashlib
import logging
import re
import shutil
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from dataclasses import dataclass, field
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import src.config as config
from src.countries.cz.aggregation_service import run_cz_aggregation, run_cz_compare
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.time_test import time_test_deadline
from src.engine.pairing import PairingMethod, coerce as coerce_pairing_method
from src.pipeline_runner import run_core_processing_pipeline
from src.utils.decimal_context import setup_decimal_context
from src.utils.type_utils import parse_ibkr_date
from src.webapp import settings
from src.webapp.ibkr_flex import (
    FLEX_SLOTS,
    INTER_QUERY_DELAY_S,
    FlexConfig,
    FlexFetchError,
    fetch_statement,
    load_flex_config,
    save_flex_config,
)
from src.webapp.jobs import JobRunner, JobState, engine_file_lock
from src.webapp.serializers import dump_json, load_json

logger = logging.getLogger(__name__)

FX_MODES = ("daily", "uniform", "compare")
# Single-method pairing choices offered in the web GUI (the full FX×method
# matrix / 'compare' lives on the CLI — `--cz-pairing-method compare`).
PAIRING_METHODS = ("fifo", "lifo", "weighted_average", "optimal")

# Result items that represent a realized §10 disposal (carry gain/loss legs).
DISPOSAL_ITEM_TYPES = ("SECURITY_DISPOSAL", "OPTION_CLOSE", "OPTION_EXPIRY_WORTHLESS")

# Initial ordering of the per-symbol disposal rows. "gain_desc" is the default
# because the question being asked is "what did I earn on" — ordering by
# magnitude answers a different one, putting the worst loss above every
# moderate win. The table is click-sortable, so this only sets the first view.
DISPOSAL_SORTS = {
    "gain_desc": (lambda a: a["gain_loss_czk"], True),
    "gain_asc": (lambda a: a["gain_loss_czk"], False),
    "abs_desc": (lambda a: abs(a["gain_loss_czk"]), True),
    "proceeds_desc": (lambda a: a["proceeds_czk"], True),
    "symbol": (lambda a: a["symbol"], False),
}
DEFAULT_DISPOSAL_SORT = "gain_desc"


def symbol_matches(sym: str, category: Optional[str],
                   description: Optional[str], want: str) -> bool:
    """Whether a result row belongs to the ticker the user typed.

    A stock symbol also claims the options written on it, in both key styles
    (OCC ``PYPL  260731C00061000`` and marker-first ``C TUI  20260619 9 M``),
    plus a description-prefix fallback for listings whose stock ticker differs
    from the option key's underlying token (stock ``TUI1`` vs option
    ``C TUI …`` — IBKR's option description opens with the stock ticker,
    "TUI1 19DEC25 6.4 C").

    Shared by the disposals ranking and the items list so typing a ticker
    means the same thing on both.
    """
    if not want:
        return True
    up = sym.upper()
    if up == want:
        return True
    if category != "OPTION":
        return False
    if _OCC_KEY_TAIL.search(up):               # OCC: underlying first
        return up.startswith(want + " ")
    if (up[:2] in ("C ", "P ")                 # marker-first key style
            and up[2:].lstrip().startswith(want + " ")):
        return True
    return (description or "").upper().startswith(want + " ")


def item_matches(it: Dict[str, Any], *, category: Optional[str] = None,
                 date_from: Optional[str] = None,
                 date_to: Optional[str] = None) -> bool:
    """Category and date-window filter shared by the disposals and items views.

    Category keys on ``asset_category``, never ``item_type``: a CFD is emitted
    as ``OPTION_CLOSE`` (item_builder), so an item_type filter would silently
    count CFDs among the options.

    Both date bounds are inclusive and expect a full ISO ``YYYY-MM-DD``, which
    is what ``<input type="date">`` submits and which compares correctly as a
    string. A row with no date falls outside any window that is set.
    """
    if category and (it.get("asset_category") or "") != category:
        return False
    if date_from or date_to:
        when = it.get("event_date") or ""
        if date_from and when < date_from:
            return False
        if date_to and when > date_to:
            return False
    return True


def _qty_display(value: Any) -> str:
    """FIFO quantities carry eight tail zeros; "5.00000000" reads as noise.

    ``format`` rather than a bare ``normalize()``, which renders Decimal("100")
    as "1E+2".
    """
    if value in (None, ""):
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)

# Sell-target ladder ("prodejní zóny") — user state, not a run artifact.
SELL_TARGETS_FILE = "sell_targets.json"
SELL_TARGETS_LOCK = "sell_targets.lock"
SELL_TARGETS_VERSION = 1
MAX_ZONES_PER_SYMBOL = 10
MAX_TARGET_SYMBOLS = 200
MAX_IMPORT_LINES = 500

# Live quotes are one HTTP round trip per symbol (~200 ms), fetched inline by
# the sell-zone ladder before it can render. Measured on the real portfolio:
# 24 holdings took 4.5 s serially. Pure I/O, so a small pool collapses that to
# roughly one round trip; kept modest because it is all one host.
QUOTE_FETCH_WORKERS = 8

# Slices the allocation doughnut draws before folding the rest into "ostatní".
# 12 keeps the legend readable; the book runs to 36 rows, so the fold matters.
ALLOCATION_SLICES = 12

# Fuzzy description→symbol matching for the spreadsheet import. Deliberately
# strict: a WRONG symbol in a sell plan is far worse than an unresolved row the
# user picks from a dropdown. Measured on the real sheet, 0.72 keeps the good
# matches (KWEB .95, BABA .89, FLXK .88) and rejects the dangerous near-miss
# ("Duolingo Inc" scored .58 against PYPL). MARGIN forces a clear winner.
_MATCH_THRESHOLD = 0.72
_MATCH_MARGIN = 0.08

# Thousands separators a Czech spreadsheet paste can carry: plain space,
# no-break, narrow no-break, thin.
_DEC_JUNK = str.maketrans("", "", "    ")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lot_is_exempt(lot: Dict[str, Any], time_test_applies: bool,
                   today: date) -> Optional[bool]:
    """Is this lot past the §4/1/u holding test?

    Returns ``None`` for "cannot tell" — ``time_test_deadline`` is absent
    whenever an acquisition date was estimated, and such a lot must never be
    silently counted as exempt OR as taxable. Callers that need a hard bool
    (the sale simulator) treat ``None`` as not-exempt, which is the
    conservative direction.

    Single source of truth so the simulator and the sell-zone overview cannot
    drift apart on what "exempt" means.
    """
    if not time_test_applies:
        return False
    deadline = lot.get("time_test_deadline")
    if not deadline:
        return None
    return today > date.fromisoformat(deadline)


def _norm_text(value: Any) -> str:
    """Diacritic-free, punctuation-free, lowercase, single-spaced.

    Used for both header detection and description matching, so "Wix.Com Ltd"
    and "WIX.COM LTD" collapse to the same key.
    """
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _split_import_line(line: str) -> List[str]:
    """Split one pasted line into fields.

    Tab first (a Sheets/Excel paste is TSV), then semicolon (Czech Excel CSV).
    Plain comma is NEVER a delimiter — it collides with the Czech decimal
    comma, and "1 234,50" would silently become two columns.
    """
    if "\t" in line:
        parts = line.split("\t")
    elif ";" in line:
        parts = line.split(";")
    else:
        parts = re.split(r"\s{2,}", line)
    return [p.strip() for p in parts]


def _parse_decimal_cz(text: Any, field: str) -> Decimal:
    """Parse a Czech-formatted decimal ("1 234,50", "70", "12,5 %") → Decimal.

    Shared by the form path and the bulk-import path so the two can never
    drift apart. Raises ValueError with a Czech message the routes surface
    verbatim as a flash.
    """
    raw = str(text or "").strip()
    cleaned = raw.translate(_DEC_JUNK)
    for junk in ("Kč", "CZK", "$", "%"):
        cleaned = cleaned.replace(junk, "")
    cleaned = cleaned.replace(",", ".")
    if not cleaned:
        raise ValueError(f"Chybí {field}.")
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ArithmeticError):
        raise ValueError(f"{field.capitalize()} není číslo: {raw!r}.") from None
    if not value.is_finite():
        raise ValueError(f"{field.capitalize()} není konečné číslo: {raw!r}.")
    return value

# OCC option keys end with yymmdd + C/P + 8-digit strike ("SOFI  260417P00020000").
_OCC_KEY_TAIL = re.compile(r"\d{6}[CP]\d{8}$")

# Placeholder account label until real multi-account support lands. Runs are
# tagged with the IBKR account id(s) found in their inputs; a run with no
# account column falls back to this so the dashboard can always group by one.
DEFAULT_ACCOUNT = "default"
# IBKR statements name the account column differently per report; trades/cash/
# corp actions use ClientAccountID, positions use AccountId in some exports.
_ACCOUNT_COLUMNS = ("ClientAccountID", "AccountId", "AccountAlias")
# Bump when the fingerprint recipe changes, OR when an engine change makes the
# same inputs produce a different result — the hash covers inputs only, so
# without a bump the cache happily serves a run computed by the old code.
# v2: §4 odst. 1 letter corrected (w → u/t) and time-test-exempt proceeds now
#     count toward the 100k limit sum.
# v3: the holding period is measured from holding_period_start, so the frozen
#     time_test_deadline in portfolio.json and the CZK cost basis of a carried
#     lot both change.
# v4: a stock-for-stock merger is now applied (carry-over or taxable disposal)
#     instead of aborting the run.
_FINGERPRINT_VERSION = "v4"

# Czech gloss for the shared AssetClassifier dialog labels (German origin).
# Keyed on the exact label from AssetClassifier.classification_options() —
# unmapped labels fall back to the original string.
CLASSIFY_LABELS_CZ = {
    "Aktienfonds (KAP-INV)": "Akciový fond",
    "Mischfonds (KAP-INV)": "Smíšený fond",
    "Immobilienfonds (KAP-INV)": "Nemovitostní fond",
    "Auslands-Immobilienfonds (KAP-INV)": "Zahraniční nemovitostní fond",
    "Sonstige Investmentfonds (KAP-INV)": "Ostatní investiční fond",
    "§23 EStG / Anlage SO (z.B. Gold-ETC, Krypto-ETP)": "Ostatní majetek §10 (zlato/krypto ETC/ETP)",
    "Aktie (Anlage KAP)": "Akcie",
    "Anleihe (Anlage KAP)": "Dluhopis",
    "Option/Termingeschäft (Anlage KAP)": "Opce / termínový obchod",
    "CFD (Anlage KAP)": "CFD",
    "Cash / Währungssaldo (ECHT)": "Hotovost / měnový zůstatek",
    "Devisenhandelspaar (z.B. EUR.USD) - wird als UNKNOWN klassifiziert":
        "Měnový pár (např. EUR.USD) — neznámé",
    "Sonstiges (Standard Anlage KAP)": "Ostatní (výchozí — akcie)",
}


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

    def delete_year_dataset(self, year: int) -> Path:
        """Soft-delete a year's input dataset: move it to ``data_dir/_trash``.

        Deliberately NOT a hard delete — the manually exported statements may
        be irreplaceable (the Flex Web Service reaches only ~1 year back).
        Persisted runs keep their own input copies, so history stays intact.
        Returns the trash path the dataset was moved to.
        """
        year_dir = self.data_dir / str(year)
        if not year_dir.is_dir():
            raise ValueError(f"Pro rok {year} nejsou nahraná žádná data.")
        trash_dir = self.data_dir / "_trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = trash_dir / f"{year}-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.move(str(year_dir), str(target))
        logger.info(f"Dataset {year} moved to trash: {target}")
        return target

    def _positions_end_of(self, year: int) -> Optional[Path]:
        p = self.data_dir / str(year) / settings.SLOT_FILES["positions_end"]
        return p if p.is_file() else None

    # ------------------------------------------------------------------
    # Input assembly
    # ------------------------------------------------------------------

    # ID columns used to drop duplicate rows when dataset years overlap
    # (e.g. a rolling-window Flex query period instead of Year to Date).
    _DEDUP_ID_COLUMNS = ("TransactionID", "ActionID")

    def _merge_years(
        self,
        slot: str,
        tax_year: int,
        target: Path,
        notes: Optional[List[str]] = None,
    ) -> Optional[Path]:
        """Concatenate a slot's files across all dataset years <= tax_year.

        Byte-identical rows sharing the same TransactionID/ActionID are
        written only once: overlapping Flex query periods export the same
        trades into two year files, and duplicated trades corrupt the SOY
        FIFO reconstruction. Rows are never dropped on the ID alone —
        distinct rows may legitimately share an ActionID (multi-leg
        corporate actions). Files without an ID column are kept verbatim.

        Header rows repeated mid-file are dropped: IBKR inserts them between
        export sections (multi-account) and hand-concatenated uploads carry
        one per source file. Parsed as data they would reach the parsers —
        and corporate-action rows validate even with non-numeric decimals
        (safe_decimal -> 0), creating a phantom asset named "Symbol".
        """
        sources = []
        for ds in self.list_years():
            if ds.year <= tax_year and ds.files.get(slot):
                sources.append((ds.year, ds.files[slot]))
        if not sources:
            return None

        header = None
        id_index: Optional[int] = None
        seen: set = set()
        dropped = 0
        repeated_headers = 0
        with open(target, "w", encoding="utf-8", newline="") as out:
            for year, src in sources:
                with open(src, encoding="utf-8-sig", newline="") as fh:
                    lines = fh.readlines()
                if not lines:
                    continue
                if header is None:
                    header = lines[0].strip()
                    out.write(lines[0] if lines[0].endswith("\n") else lines[0] + "\n")
                    columns = next(csv.reader([header]))
                    id_index = next(
                        (columns.index(c) for c in self._DEDUP_ID_COLUMNS
                         if c in columns),
                        None,
                    )
                elif lines[0].strip() != header:
                    raise ValueError(
                        f"Soubor {src.name} pro rok {year} má jinou hlavičku než "
                        f"předchozí roky — soubory nelze sloučit. Vygenerujte "
                        f"všechny roky stejnou Flex Query šablonou."
                    )
                for line in lines[1:]:
                    if not line.strip():
                        continue
                    if line.strip() == header:
                        repeated_headers += 1
                        continue
                    if id_index is not None:
                        row = next(csv.reader([line]), [])
                        row_id = row[id_index] if id_index < len(row) else ""
                        if row_id:
                            key = (row_id, line.strip())
                            if key in seen:
                                dropped += 1
                                continue
                            seen.add(key)
                    out.write(line if line.endswith("\n") else line + "\n")

        if repeated_headers:
            logger.info(
                "Sloučení souborů (%s): odstraněno %d opakovaných hlaviček "
                "uvnitř souborů (IBKR je vkládá mezi sekce exportu).",
                settings.SLOT_LABELS[slot], repeated_headers,
            )
        if dropped:
            note = (
                f"Sloučení souborů ({settings.SLOT_LABELS[slot]}): odstraněno "
                f"{dropped} duplicitních řádků — období Flex queries se "
                f"překrývají, zkontrolujte nastavení Year to Date."
            )
            logger.warning(note)
            if notes is not None:
                notes.append(note)
        return target

    @staticmethod
    def _copy_csv(src: Path, target: Path) -> Path:
        """Copy a slot file into the run's inputs, dropping header rows
        repeated mid-file (same rationale as in _merge_years). Everything
        else is kept byte-verbatim; the stored dataset file is not touched.
        """
        with open(src, encoding="utf-8-sig", newline="") as fh:
            lines = fh.readlines()
        with open(target, "w", encoding="utf-8", newline="") as out:
            if not lines:
                return target
            header = lines[0].strip()
            out.write(lines[0] if lines[0].endswith("\n") else lines[0] + "\n")
            for line in lines[1:]:
                if line.strip() == header:
                    continue
                out.write(line)
        return target

    def _prepare_inputs(
        self,
        run_dir: Path,
        tax_year: int,
        notes: Optional[List[str]] = None,
    ) -> Dict[str, Path]:
        ds = self.get_year(tax_year)
        if ds is None:
            raise ValueError(f"Pro rok {tax_year} nejsou nahraná žádná data.")
        if not ds.run_ready:
            missing = ", ".join(settings.SLOT_LABELS[s] for s in ds.missing_required)
            raise ValueError(f"Pro rok {tax_year} chybí: {missing}")

        inputs_dir = run_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)

        trades = self._merge_years("trades", tax_year, inputs_dir / "trades.csv",
                                   notes=notes)

        cash = inputs_dir / "cash_transactions.csv"
        self._copy_csv(ds.files["cash"], cash)

        pos_end = inputs_dir / "positions_end.csv"
        self._copy_csv(ds.files["positions_end"], pos_end)

        pos_start = inputs_dir / "positions_start.csv"
        if ds.files["positions_start"]:
            self._copy_csv(ds.files["positions_start"], pos_start)
        else:
            prev = self._positions_end_of(tax_year - 1)
            if prev:
                self._copy_csv(prev, pos_start)
            else:
                pos_start.write_text(settings.POSITIONS_HEADER, encoding="utf-8")

        corp = self._merge_years("corp_actions", tax_year,
                                 inputs_dir / "corporate_actions.csv", notes=notes)
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
    # Compute cache (input fingerprint → reuse an identical past run)
    # ------------------------------------------------------------------

    def _dataset_source_files(self, tax_year: int) -> List[Path]:
        """Every source file that can feed a run for ``tax_year``.

        Conservative on purpose: hashes all dataset files of years <= tax_year
        (trades/corp actions are merged across history). Touching an unrelated
        earlier-year file only forces a harmless recompute — it can never make
        a stale result be reused.
        """
        paths: List[Path] = []
        for ds in self.list_years():
            if ds.year > tax_year:
                continue
            for p in ds.files.values():
                if p is not None:
                    paths.append(p)
        return sorted(set(paths))

    def _input_fingerprint(self, tax_year: int, fx_mode: str, pairing_method: str) -> str:
        """SHA-256 over the inputs + params that determine a run's output.

        Includes the effective fx_mode (running-year compare→daily), pairing
        method, all source CSVs, and the classification cache (which reshapes
        results). ECB rates are deliberately excluded: for a closed year they
        are stable, and a running year changes its data (→ new hash) anyway.
        """
        eff_mode, _ = _effective_fx_mode(fx_mode, tax_year, datetime.now().year)
        h = hashlib.sha256()
        h.update(
            f"{_FINGERPRINT_VERSION}|year={tax_year}|fx={eff_mode}"
            f"|pairing={pairing_method}".encode()
        )
        for p in self._dataset_source_files(tax_year):
            try:
                data = p.read_bytes()
            except OSError:
                continue
            h.update(str(p).encode())
            h.update(str(len(data)).encode())
            h.update(data)
        try:
            cache_path = Path(config.CLASSIFICATION_CACHE_FILE_PATH)
            if cache_path.is_file():
                h.update(b"classify:")
                h.update(cache_path.read_bytes())
        except Exception:  # noqa: BLE001 — a missing/unreadable cache just omits it
            pass
        return h.hexdigest()

    def find_cached_run(
        self, tax_year: int, fx_mode: str, pairing_method: str
    ) -> Optional[str]:
        """Return the run_id of an existing run with identical inputs, if any."""
        fp = self._input_fingerprint(tax_year, fx_mode, pairing_method)
        for meta in self.list_runs(limit=100):
            if meta.get("input_fingerprint") != fp:
                continue
            run_id = meta.get("run_id")
            modes = meta.get("modes") or []
            if not run_id or not modes:
                continue
            # The reused result must still be on disk (runs can be pruned).
            if (self.runs_dir / run_id / f"result.{modes[0]}.json").is_file():
                return run_id
        return None

    def _extract_account_ids(self, inputs: Dict[str, Path]) -> List[str]:
        """Distinct IBKR account id(s) present in a run's input files."""
        accounts: set = set()
        for key in ("positions_end", "trades", "cash"):
            p = inputs.get(key)
            if not p or not Path(p).is_file():
                continue
            try:
                with open(p, encoding="utf-8-sig", newline="") as fh:
                    reader = csv.DictReader(fh)
                    fields = reader.fieldnames or []
                    col = next((c for c in _ACCOUNT_COLUMNS if c in fields), None)
                    if not col:
                        continue
                    for row in reader:
                        val = (row.get(col) or "").strip()
                        if val:
                            accounts.add(val)
            except Exception:  # noqa: BLE001 — never let account tagging fail a run
                continue
        return sorted(accounts) or [DEFAULT_ACCOUNT]

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(
        self,
        tax_year: int,
        fx_mode: str,
        pairing_method: str = "fifo",
        force: bool = False,
    ) -> Tuple[Optional[str], str]:
        """Submit a run to the single-worker executor; returns (job_id, run_id).

        Dataset readiness is validated HERE, before submitting — the user gets
        the error immediately instead of a job that fails a poll later. When an
        identical run already exists and ``force`` is False, no job is queued:
        returns ``(None, cached_run_id)`` so the caller can jump straight to it.
        """
        if fx_mode not in FX_MODES:
            raise ValueError(f"Neznámý kurzový režim: {fx_mode}")
        if pairing_method not in PAIRING_METHODS:
            raise ValueError(f"Neznámá párovací metoda: {pairing_method}")
        ds = self.get_year(tax_year)
        if ds is None:
            raise ValueError(f"Pro rok {tax_year} nejsou nahraná žádná data.")
        if not ds.run_ready:
            missing = ", ".join(settings.SLOT_LABELS[s] for s in ds.missing_required)
            raise ValueError(f"Pro rok {tax_year} chybí: {missing}")
        if not force:
            cached = self.find_cached_run(tax_year, fx_mode, pairing_method)
            if cached:
                logger.info(
                    f"Cache hit for {tax_year}/{fx_mode}/{pairing_method} → {cached}"
                )
                return None, cached
        run_id = f"{tax_year}-{datetime.now():%Y%m%d-%H%M%S}"
        job_id = self.runner.submit(
            f"Výpočet {tax_year} ({fx_mode}/{pairing_method})",
            self._execute_run, run_id, tax_year, fx_mode, None, None, None, pairing_method,
        )
        return job_id, run_id

    def _execute_run(
        self,
        run_id: str,
        tax_year: int,
        fx_mode: str,
        ecb_provider=None,
        cz_fx_provider=None,
        extra_notes: Optional[List[str]] = None,
        pairing_method: str = "fifo",
    ) -> Dict[str, Any]:
        """Runs the full pipeline + CZ aggregation and persists everything.

        Executed on the JobRunner worker thread (decimal context set there);
        the provider overrides exist for offline tests.
        """
        started = time.monotonic()
        requested_fx_mode = fx_mode  # before compare→daily downgrade, for the fingerprint
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        fx_mode, run_notes = _effective_fx_mode(fx_mode, tax_year, datetime.now().year)
        if extra_notes:
            run_notes.extend(extra_notes)

        inputs = self._prepare_inputs(run_dir, tax_year, notes=run_notes)
        account_ids = self._extract_account_ids(inputs)
        input_fingerprint = self._input_fingerprint(
            tax_year, requested_fx_mode, pairing_method
        )

        pairing = coerce_pairing_method(pairing_method)
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
                pairing_method=pairing,
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
            self._build_portfolio(processing, tax_year, account_ids=account_ids),
            run_dir / "portfolio.json",
        )

        meta = {
            "run_id": run_id,
            "tax_year": tax_year,
            "fx_mode": fx_mode,
            "pairing_method": pairing.value,
            "modes": [m for m, _ in mode_results],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(time.monotonic() - started, 1),
            "eoy_mismatch_error_count": processing.eoy_mismatch_error_count,
            "summary": summary,
            "compare_lines": compare_lines,
            "notes": run_notes,
            "account_ids": account_ids,
            "input_fingerprint": input_fingerprint,
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

    def save_flex_settings(
        self,
        token: str,
        queries: Dict[str, str],
        first_year: Optional[str] = None,
    ) -> None:
        cfg = self.get_flex_config()
        if token.strip():
            cfg.token = token.strip()
        cfg.queries = {k: v.strip() for k, v in queries.items() if v.strip()}
        if first_year is not None:
            text = str(first_year).strip()
            cfg.first_year = int(text) if text.isdigit() else None
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

        def _download(year: int, delay_first: bool,
                      from_date: Optional[str] = None,
                      to_date: Optional[str] = None) -> List[str]:
            year_dir = self.data_dir / str(year)
            year_dir.mkdir(parents=True, exist_ok=True)
            done: List[str] = []
            for slot in FLEX_SLOTS:
                query_id = cfg.queries.get(slot)
                if not query_id:
                    continue
                if done or delay_first:
                    # IBKR throttles per-token bursts (error 1018) — space out
                    # consecutive statement downloads.
                    pause(INTER_QUERY_DELAY_S)
                logger.info(f"IBKR Flex: downloading {slot} for {year} (query {query_id})…")
                content = fetch(cfg.token, query_id,
                                from_date=from_date, to_date=to_date)
                (year_dir / self._FLEX_SLOT_FILES[slot]).write_bytes(content)
                done.append(slot)
            return done

        # Historical bootstrap FIRST (oldest year first): every missing
        # dataset year from first_year up to tax_year-1 is fetched via the
        # fd/td period override on the SAME queries — one calendar year per
        # request (365 days limits the window span, not how far back it
        # starts). Positions then arrive as the 31 Dec snapshot of the year.
        extra_notes: List[str] = []
        bootstrapped: List[int] = []
        delay_next = False
        if cfg.first_year:
            for year in range(cfg.first_year, tax_year):
                ds = self.get_year(year)
                if ds is not None and ds.run_ready:
                    continue
                try:
                    done = _download(year, delay_first=delay_next,
                                     from_date=f"{year}0101", to_date=f"{year}1231")
                except FlexFetchError as exc:
                    # A year IBKR cannot deliver must not sink the whole job.
                    logger.warning(f"IBKR Flex: bootstrap of {year} failed: {exc}")
                    extra_notes.append(f"Doplnění roku {year} z IBKR selhalo: {exc}")
                    delay_next = True
                    continue
                delay_next = delay_next or bool(done)
                if done:
                    bootstrapped.append(year)
                    logger.info(f"IBKR Flex: bootstrapped missing {year} dataset.")
        if bootstrapped:
            extra_notes.append(
                "Chybějící datasety doplněny z IBKR (fd/td období přes "
                f"stávající queries): {', '.join(map(str, bootstrapped))}."
            )

        fetched = _download(tax_year, delay_first=delay_next)
        if not fetched:
            raise ValueError("Žádná query ID nejsou nastavená.")
        logger.info(f"IBKR Flex: fetched {', '.join(fetched)} for {tax_year}.")

        meta = self._execute_run(run_id, tax_year, fx_mode, extra_notes=extra_notes)
        meta["fetched_slots"] = fetched
        if bootstrapped:
            meta["bootstrapped_years"] = bootstrapped
        return meta

    # ------------------------------------------------------------------
    # Portfolio (end-of-year open FIFO lots + time-test deadlines)
    # ------------------------------------------------------------------

    # §4/1/u applies to securities; derivatives never pass the time test.
    _TIME_TEST_CATEGORIES = {"STOCK", "BOND", "INVESTMENT_FUND"}

    def _build_portfolio(
        self, processing, tax_year: int, account_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Distill open FIFO lots into a JSON-safe portfolio snapshot.

        Valuation stays in the position's own currency (EOY mark price from
        the positions file); cost basis is EUR (engine-internal base). CZK
        conversion arrives with live quotes in a later phase.

        ``account_ids`` tags the snapshot for future multi-account filtering; a
        single-account run stamps every position with that account.
        """
        account_ids = account_ids or [DEFAULT_ACCOUNT]
        single_account = account_ids[0] if len(account_ids) == 1 else None
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
                # The lot carries the flag now — sniffing the id prefix would
                # miss a carried-over lot minted under a corporate-action id.
                estimated = bool(lot.acquisition_date_estimated)
                holding_estimated = bool(lot.holding_period_start_estimated)
                acq = parse_ibkr_date(lot.acquisition_date)
                hps = parse_ibkr_date(lot.holding_period_start or lot.acquisition_date)
                deadline = (
                    # Regime from the acquisition, period from the holding start.
                    time_test_deadline(
                        acquisition_date=acq, holding_period_start=hps, config=cz_cfg)
                    if (time_test_applies and acq and hps
                        and not estimated and not holding_estimated) else None
                )
                lot_rows.append({
                    "acquisition_date": lot.acquisition_date,
                    "holding_period_start": lot.holding_period_start or lot.acquisition_date,
                    "quantity": lot.quantity,
                    "unit_cost_eur": lot.unit_cost_basis_eur,
                    "total_cost_eur": lot.total_cost_basis_eur,
                    "acquisition_estimated": estimated,
                    "holding_period_start_estimated": holding_estimated,
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
                "account": single_account,
                "time_test_applicable": time_test_applies,
                "quantity_long": sum((l.quantity for l in lots), Decimal(0)),
                "quantity_short": sum((s.quantity_shorted for s in short_lots), Decimal(0)),
                "total_cost_eur": sum((l.total_cost_basis_eur for l in lots), Decimal(0)),
                "eoy_quantity": asset.eoy_quantity,
                "eoy_market_price": asset.eoy_market_price,
                "eoy_currency": asset.eoy_mark_price_currency,
                "eoy_position_value": asset.eoy_position_value,
                # Option contract metadata (None for non-options) — powers the
                # dashboard expiry overview.
                "option_type": getattr(asset, "option_type", None),
                "strike_price": getattr(asset, "strike_price", None),
                "expiry_date": getattr(asset, "expiry_date", None),
                "multiplier": getattr(asset, "multiplier", None),
                "underlying_symbol": getattr(asset, "underlying_ibkr_symbol", None),
                "lots": lot_rows,
                "short_lots": short_rows,
            })

        positions.sort(key=lambda p: (p["symbol"] or ""))
        return {
            "tax_year": tax_year,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "account_ids": account_ids,
            "positions": positions,
        }

    def load_portfolio(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = self.runs_dir / run_id / "portfolio.json"
        return load_json(path) if path.is_file() else None

    def options_overview(self, run_id: str) -> Dict[str, Any]:
        """Open option contracts from a run's portfolio, with days-to-expiry.

        Includes both long (bought) and short (written) contracts — writing
        options is exactly where the expiry matters most — so it reads the raw
        portfolio rather than the live valuation (which drops short-only rows).
        Sorted by expiry ascending (soonest first; undated last).

        ``days_to_expiry`` counts from the snapshot's reference date, not today:
        the FIFO book is a point-in-time snapshot at the tax-year end, so for a
        closed year an option that was alive on 31 Dec must not read as
        "expired" just because that date has since passed. Reference =
        ``min(today, 31 Dec of the tax year)`` — today for the live current
        year, the year-end for closed years. ``options`` is empty for old runs
        whose ``portfolio.json`` predates option metadata (re-run to populate).
        """
        pf = self.load_portfolio(run_id)
        if pf is None:
            return {"as_of": None, "tax_year": None, "options": []}
        today = date.today()
        tax_year = pf.get("tax_year")
        ref = today
        if tax_year:
            try:
                ref = min(today, date(int(tax_year), 12, 31))
            except (ValueError, TypeError):
                ref = today
        rows: List[Dict[str, Any]] = []
        for pos in pf.get("positions", []):
            if pos.get("category") != "OPTION":
                continue
            long_q = Decimal(str(pos.get("quantity_long") or 0))
            short_q = Decimal(str(pos.get("quantity_short") or 0))
            if long_q == 0 and short_q == 0:
                continue
            expiry = pos.get("expiry_date")
            days = None
            if expiry:
                try:
                    days = (date.fromisoformat(expiry) - ref).days
                except ValueError:
                    days = None
            net = long_q - short_q
            rows.append({
                "symbol": pos.get("symbol"),
                "description": pos.get("description"),
                "underlying": pos.get("underlying_symbol"),
                "option_type": pos.get("option_type"),
                "strike_price": pos.get("strike_price"),
                "multiplier": pos.get("multiplier"),
                "currency": pos.get("eoy_currency"),
                "expiry_date": expiry,
                "days_to_expiry": days,
                "expired": days is not None and days < 0,
                "quantity_long": long_q,
                "quantity_short": short_q,
                "net_quantity": net,
                # Contract counts are whole numbers — drop the FIFO tail zeros.
                "net_display": format(net.normalize(), "f"),
            })
        rows.sort(key=lambda r: (r["expiry_date"] is None, r["expiry_date"] or ""))
        return {"as_of": ref.isoformat(), "tax_year": tax_year, "options": rows}

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

    @staticmethod
    def _net_quantity(pos: Dict[str, Any]) -> Decimal:
        """Signed size of a holding: long lots minus written (short) ones.

        A written option is a liability — IBKR reports it as a negative
        ``PositionValue`` — so a short-only row belongs in the net worth just
        as much as a long one, with the opposite sign.
        """
        return (Decimal(str(pos.get("quantity_long") or 0))
                - Decimal(str(pos.get("quantity_short") or 0)))

    @staticmethod
    def _short_premium_eur(pos: Dict[str, Any]) -> Decimal:
        """Premium already received for the written lots.

        This is a short leg's cost side with the sign flipped — a credit, not
        an outlay — which is what makes ``value - cost`` the P/L for long and
        short legs alike: buying back cheaper than you sold is a gain.
        """
        return sum((Decimal(str(s.get("total_proceeds_eur") or 0))
                    for s in pos.get("short_lots") or []), Decimal(0))

    @staticmethod
    def _is_quotable(pos: Dict[str, Any]) -> bool:
        """Whether a live price exists for this instrument at all.

        Not for options: there is no option chain behind ``QuoteService``,
        only a last price per plain ticker, so a contract always falls back to
        its year-end mark.
        """
        return pos.get("category") != "OPTION"

    def _needs_quote(self, pos: Dict[str, Any]) -> bool:
        """Whether this row is worth an HTTP round trip — quotable and not flat.

        Both the prefetch and the valuation loop ask through here. They used to
        carry the same condition twice, and once they drifted apart a row would
        silently land on the EOY fallback with no way to tell.
        """
        return self._is_quotable(pos) and self._net_quantity(pos) != 0

    @staticmethod
    def _contract_size(pos: Dict[str, Any]) -> Optional[Decimal]:
        """Shares per unit held: 1 for stock, ``multiplier`` for an option.

        An option is quoted per underlying share but held in contracts of 100,
        and its FIFO cost is already per contract — so leaving this out puts
        the two legs 100x apart. ``None`` means an option whose run never
        recorded the multiplier: refuse it rather than publish a number that
        is 100x light (re-run the year to populate the metadata).
        """
        mult = pos.get("multiplier")
        if mult:
            return Decimal(str(mult))
        return None if pos.get("category") == "OPTION" else Decimal(1)

    def _fetch_quotes(self, pf: Dict[str, Any]) -> Dict[Tuple[str, str], Any]:
        """Every holding's live quote, fetched concurrently. ``{}`` values may
        be ``None`` — a missing quote is the caller's EOY-fallback case.

        Keyed by (symbol, currency) because the currency is what picks the
        Yahoo exchange suffix, and deduplicated so one symbol held twice costs
        one request.

        Its own pool, deliberately NOT ``self.runner``: this method already
        runs ON the runner's single worker, so scheduling there would deadlock.
        Threads get the engine's decimal context through ``initializer``, the
        same way ``jobs.py`` arms its worker.
        """
        wanted = {
            (pos.get("symbol") or "", pos.get("eoy_currency") or "USD")
            for pos in pf.get("positions", [])
            if self._needs_quote(pos)
        }
        keys = sorted(wanted)
        if not keys:
            return {}
        if len(keys) == 1:                      # no pool for a single holding
            return {keys[0]: self.quotes.get_quote(*keys[0])}
        with ThreadPoolExecutor(max_workers=min(QUOTE_FETCH_WORKERS, len(keys)),
                                thread_name_prefix="quote",
                                initializer=setup_decimal_context) as pool:
            futures = {key: pool.submit(self.quotes.get_quote, *key) for key in keys}
        # .result() re-raises, so a broken quote service still surfaces here
        # rather than turning into a silent EOY fallback.
        return {key: future.result() for key, future in futures.items()}

    @staticmethod
    def allocation_slices(positions: List[Dict[str, Any]],
                         limit: int = ALLOCATION_SLICES) -> Dict[str, Any]:
        """Doughnut slices: the biggest ``limit`` holdings plus one "ostatní".

        Chart.js normalises whatever it is handed to a full circle, so handing
        it a bare top-N drew the tail as if it did not exist and inflated every
        slice that survived — a real 5% holding rendered as 6-7% of the ring
        with nothing to say the picture was partial.

        Written positions are a liability, not a share of the portfolio, and a
        negative wedge renders broken; they are left out and counted, so the
        caller can disclose both what was folded and what was dropped.
        """
        priced = [(p.get("symbol") or "?", Decimal(str(p["value_czk"])))
                  for p in positions if p.get("value_czk") is not None]
        longs = sorted(((s, v) for s, v in priced if v > 0),
                       key=lambda sv: sv[1], reverse=True)
        shorts = [(s, v) for s, v in priced if v < 0]

        head, tail = longs[:limit], longs[limit:]
        slices = [{"label": s, "value": str(v)} for s, v in head]
        if tail:
            slices.append({
                "label": f"ostatní ({len(tail)})",
                "value": str(sum((v for _, v in tail), Decimal(0))),
            })
        return {
            "slices": slices,
            "folded": len(tail),
            "short_excluded": len(shorts),
            "short_value_czk": sum((v for _, v in shorts), Decimal(0)) or None,
        }

    @staticmethod
    def portfolio_breakdown(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Long-side split by asset class and by trading currency.

        The denominator is the LONG value alone. A written position is a
        liability, not a share of what you own, so counting it would push the
        weights past 100% — and the same reasoning already keeps it out of the
        allocation doughnut.

        Sets ``weight_pct`` on every row as a side effect, so the table can
        sort by it. Rows are mutated in place because they are the same dicts
        the caller is about to render.
        """
        long_total = sum((r["value_czk"] for r in rows
                          if r.get("value_czk") and r["value_czk"] > 0),
                         Decimal(0))
        by_category: Dict[str, Decimal] = {}
        by_currency: Dict[str, Decimal] = {}
        for r in rows:
            value = r.get("value_czk")
            if not value or value <= 0:
                r["weight_pct"] = None
                continue
            r["weight_pct"] = (value / long_total * 100) if long_total else None
            cat = r.get("category") or "?"
            cur = r.get("live_currency") or r.get("eoy_currency") or "?"
            by_category[cat] = by_category.get(cat, Decimal(0)) + value
            by_currency[cur] = by_currency.get(cur, Decimal(0)) + value

        def _slices(totals: Dict[str, Decimal]) -> List[Dict[str, Any]]:
            # value stays an exact string, pct is a float: it is a display
            # share, never summed back into money, and these slices go
            # through tojson into Chart.js, which has no Decimal.
            return [{"label": k, "value": str(v),
                     "pct": float(v / long_total * 100) if long_total else None}
                    for k, v in sorted(totals.items(), key=lambda kv: kv[1],
                                       reverse=True)]

        return {
            "total_czk": long_total or None,
            "by_category": _slices(by_category),
            "by_currency": _slices(by_currency),
        }

    def _compute_live_portfolio(self, pf: Dict[str, Any]) -> Dict[str, Any]:
        from datetime import date as _date
        today = _date.today()
        converter = self._converter_factory()
        quotes = self._fetch_quotes(pf)
        quotes_ok = 0
        options_at_eoy = 0
        total_value_czk = Decimal(0)
        total_cost_czk = Decimal(0)
        rows = []
        for pos in pf.get("positions", []):
            qty = self._net_quantity(pos)
            if qty == 0:
                continue
            row = dict(pos)
            quote = None
            if self._needs_quote(pos):
                quote = quotes.get((pos.get("symbol") or "",
                                    pos.get("eoy_currency") or "USD"))
            size = self._contract_size(pos)
            if quote is not None:
                price, currency, price_source = quote.price, quote.currency, "live"
                quotes_ok += 1
            elif pos.get("eoy_market_price") and size is not None:
                price = Decimal(str(pos["eoy_market_price"]))
                currency = pos.get("eoy_currency") or "USD"
                price_source = "eoy"
                if pos.get("category") == "OPTION":
                    options_at_eoy += 1
            else:
                price, currency, price_source = None, None, "none"

            value_czk = cost_czk = unrealized = pct = None
            value_ccy = None
            if price is not None:
                value_ccy = qty * price * size
                value_czk = self._to_czk(converter, value_ccy, currency, today)
                cost_czk = self._to_czk(
                    converter,
                    Decimal(str(pos.get("total_cost_eur") or 0))
                    - self._short_premium_eur(pos),
                    "EUR", today,
                )
                if value_czk is not None and cost_czk is not None:
                    unrealized = value_czk - cost_czk
                    # abs(): a written leg's "cost" is the premium received, a
                    # credit, so a plain ratio would flip the sign of a win.
                    pct = (unrealized / abs(cost_czk) * 100) if cost_czk else None
                    total_value_czk += value_czk
                    total_cost_czk += cost_czk
            row.update({
                "net_quantity": qty,
                # Contract counts are whole numbers — drop the FIFO tail zeros.
                "net_display": format(qty.normalize(), "f"),
                "live_price": price, "live_currency": currency,
                "price_source": price_source, "value_ccy": value_ccy,
                "value_czk": value_czk, "cost_czk": cost_czk,
                "unrealized_czk": unrealized, "unrealized_pct": pct,
            })
            rows.append(row)

        rows.sort(key=lambda r: r.get("value_czk") or Decimal(0), reverse=True)
        breakdown = self.portfolio_breakdown(rows)     # also sets weight_pct
        result = {
            "breakdown": breakdown,
            "as_of": today.isoformat(),
            "tax_year": pf.get("tax_year"),
            "positions": rows,
            "quotes_ok": quotes_ok,
            "quotes_total": sum(1 for r in rows if r.get("category") != "OPTION"),
            # Options are absent from the ratio above because they are never
            # quotable; without this count the header would read "5/5 live"
            # while a book of contracts sits on year-end marks.
            "options_at_eoy": options_at_eoy,
            "total_value_czk": total_value_czk if total_value_czk else None,
            "total_cost_czk": total_cost_czk if total_cost_czk else None,
            "total_unrealized_czk": (total_value_czk - total_cost_czk) if total_value_czk else None,
        }
        # The one place where every position already carries a fresh quote.
        # Both entry points (/dashboard/valuation and the portfolio page) pass
        # through here, so opening either one arms the alerts.
        result["sell_targets"] = self._evaluate_sell_targets(rows, today)
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
        skip_quantity: Decimal = Decimal(0),
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
            self._compute_simulation, pos, quantity, price, result, skip_quantity,
            timeout=120,
        )

    @staticmethod
    def _lots_after_skip(lots: List[Dict[str, Any]], skip: Decimal):
        """Drop the first ``skip`` shares in FIFO order.

        Lets a caller ask "what if I sell the LATER lots" — a sell zone pinned
        to a specific purchase, or a ladder rung whose predecessors already
        spoke for the older shares. Without it the simulator always starts at
        lot 0 and would price a different set of shares than the zone claims.
        Returns ``(pairs, actually_skipped)`` where pairs are
        ``(lot, available_quantity)``.
        """
        pairs, skipped = [], Decimal(0)
        for lot in lots:
            qty = Decimal(str(lot.get("quantity") or 0))
            if skip > 0:
                drop = min(qty, skip)
                skip -= drop
                skipped += drop
                qty -= drop
                if qty <= 0:
                    continue
            pairs.append((lot, qty))
        return pairs, skipped

    def _compute_simulation(
        self,
        pos: Dict[str, Any],
        quantity: Decimal,
        price: Optional[Decimal],
        result: Dict[str, Any],
        skip_quantity: Decimal = Decimal(0),
    ) -> Dict[str, Any]:
        from datetime import date as _date, timedelta as _timedelta
        today = _date.today()
        converter = self._converter_factory()
        currency = pos.get("eoy_currency") or "USD"

        size = self._contract_size(pos)
        if size is None:
            raise ValueError(
                "U tohoto kontraktu chybí velikost kontraktu (multiplier) — "
                "přepočítejte rok."
            )

        price_source = "manual"
        if price is None:
            quote = None
            if self._is_quotable(pos):
                quote = self.quotes.get_quote(pos.get("symbol") or "", currency)
            if quote is not None:
                price, currency, price_source = quote.price, quote.currency, "live"
            elif pos.get("eoy_market_price"):
                price = Decimal(str(pos["eoy_market_price"]))
                price_source = "eoy"
            else:
                raise ValueError("Cena není k dispozici — zadejte ji ručně.")

        lot_pairs, skipped = self._lots_after_skip(
            pos.get("lots", []), Decimal(str(skip_quantity or 0)))
        available = max(
            Decimal(0), Decimal(str(pos.get("quantity_long") or 0)) - skipped)
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
        for lot, lot_qty in lot_pairs:
            if remaining <= 0:
                break
            take = min(lot_qty, remaining)
            remaining -= take

            # ``size`` on the proceeds only: a lot's unit cost is already per
            # contract, the quoted price is per underlying share.
            proceeds_czk = self._to_czk(converter, take * price * size, currency, today)
            cost_czk = self._to_czk(
                converter, take * Decimal(str(lot["unit_cost_eur"])), "EUR", today
            )
            gain = (proceeds_czk - cost_czk) if (proceeds_czk is not None and cost_czk is not None) else None

            deadline = lot.get("time_test_deadline")
            deadline_d = _date.fromisoformat(deadline) if deadline else None
            estimated = bool(lot.get("acquisition_estimated"))
            estimated_involved = estimated_involved or estimated
            # `is True` keeps the historical behaviour: an unknown deadline
            # (estimated acquisition) counts as NOT exempt here.
            exempt = _lot_is_exempt(lot, time_test_applies, today) is True
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

        proceeds_total_czk = self._to_czk(converter, qty * price * size, currency, today)

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
    # Asset classification (non-interactive cache editor)
    # ------------------------------------------------------------------
    #
    # The engine's interactive dialog blocks on input(); the web always runs
    # non-interactive over a pre-filled cache. These methods let the user
    # inspect and fill that cache (``cache/user_classifications.json``) BEFORE
    # a run, without triggering the non-interactive auto-default (UNKNOWN→STOCK
    # written straight to cache) that a full pipeline would.

    def _new_classifier(self):
        from src.classification.asset_classifier import AssetClassifier
        import src.config as app_config
        return AssetClassifier(cache_file_path=app_config.CLASSIFICATION_CACHE_FILE_PATH)

    def classification_choices(self) -> List[Dict[str, str]]:
        """Category choices for the web form, deduped by (category, fund_type).

        Values are ``CATEGORY:FUND_TYPE`` (enum names) so the cache written
        matches exactly what the CLI dialog would write; labels are Czech.
        """
        from src.classification.asset_classifier import AssetClassifier
        seen: set = set()
        choices: List[Dict[str, str]] = []
        for label, cat, ft in AssetClassifier.classification_options():
            value = f"{cat.name}:{ft.name}"
            if value in seen:
                continue
            seen.add(value)
            choices.append({"value": value, "label": CLASSIFY_LABELS_CZ.get(label, label)})
        return choices

    def _classification_from_choice(self, choice: str):
        """Validate a ``CATEGORY:FUND_TYPE`` choice against the shared options."""
        from src.classification.asset_classifier import AssetClassifier
        from src.domain.enums import AssetCategory, InvestmentFundType
        allowed = {f"{c.name}:{f.name}" for _, c, f in AssetClassifier.classification_options()}
        if choice not in allowed:
            raise ValueError(f"Neplatná klasifikace: {choice}")
        cat_name, ft_name = choice.split(":", 1)
        cat = AssetCategory[cat_name]
        ft = InvestmentFundType[ft_name] if cat == AssetCategory.INVESTMENT_FUND else InvestmentFundType.NONE
        return cat, ft

    def scan_unclassified_assets(self, tax_year: int) -> Dict[str, Any]:
        """Discover a year's assets WITHOUT side effects.

        Runs only the parser/discovery stages (never
        ``finalize_asset_classifications``), so it neither writes the cache nor
        auto-defaults UNKNOWN→STOCK. Returns assets the user still has to
        decide on (``pending``) and previously auto-defaulted ones worth a
        second look (``review``)."""
        return self.runner.run_sync(self._scan_unclassified, tax_year, timeout=300)

    def _scan_unclassified(self, tax_year: int) -> Dict[str, Any]:
        import tempfile
        from src.classification.asset_classifier import AssetClassifier  # noqa: F401
        from src.domain.assets import InvestmentFund
        from src.domain.enums import AssetCategory, InvestmentFundType
        from src.identification.asset_resolver import AssetResolver
        from src.parsers.parsing_orchestrator import ParsingOrchestrator

        classifier = self._new_classifier()
        resolver = AssetResolver(asset_classifier=classifier)
        orchestrator = ParsingOrchestrator(
            asset_resolver=resolver,
            asset_classifier=classifier,
            interactive_classification=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self._prepare_inputs(Path(tmp), tax_year)
            orchestrator.load_all_raw_data(
                trades_file=str(inputs["trades"]),
                cash_transactions_file=str(inputs["cash"]),
                positions_start_file=str(inputs["positions_start"]),
                positions_end_file=str(inputs["positions_end"]),
                corporate_actions_file=str(inputs["corp_actions"]),
            )
            # Discovery only — deliberately NOT finalize_asset_classifications()
            # (that mutates the cache and auto-defaults UNKNOWN→STOCK).
            orchestrator.process_positions()
            orchestrator.discover_assets_from_transactions()
            resolver.link_derivatives()

        cache = classifier.classifications_cache
        pending: List[Dict[str, Any]] = []
        review: List[Dict[str, Any]] = []
        classified = 0
        for asset in resolver.assets_by_internal_id.values():
            try:
                key = asset.get_classification_key()
            except ValueError:
                continue  # no stable key — cannot be cached anyway
            fund_type = (
                asset.fund_type.name
                if isinstance(asset, InvestmentFund) and asset.fund_type
                else InvestmentFundType.NONE.name
            )
            row = {
                "key": key,
                "symbol": asset.ibkr_symbol,
                "isin": asset.ibkr_isin,
                "conid": asset.ibkr_conid,
                "description": asset.description,
                "ibkr_class": asset.ibkr_asset_class_raw,
                "ibkr_sub": asset.ibkr_sub_category_raw,
                "suggested_category": asset.asset_category.name,
                "suggested_value": f"{asset.asset_category.name}:{fund_type}",
                # The cases the interactive dialog would have prompted for.
                "needs_attention": (
                    classifier._is_potentially_special(asset)
                    or asset.asset_category == AssetCategory.UNKNOWN
                ),
            }
            cached = cache.get(key)
            if cached is None:
                pending.append(row)
            else:
                classified += 1
                notes = cached[2] or ""
                row["cached_value"] = f"{cached[0]}:{cached[1]}"
                row["cached_category"] = cached[0]
                row["cached_notes"] = notes
                # Only the genuinely-uncertain fallback (UNKNOWN→STOCK /
                # →CASH_BALANCE) warrants a second look. Confident heuristic
                # hits ("Auto-classified based on heuristics", e.g. STK→STOCK)
                # are trusted and stay out of the review list.
                if "Auto-defaulted" in notes:
                    review.append(row)

        # Surface funds / gold / crypto / FX pairs / unknowns first.
        pending.sort(key=lambda r: (not r["needs_attention"], r["symbol"] or ""))
        review.sort(key=lambda r: r["symbol"] or "")
        return {
            "tax_year": tax_year,
            "pending": pending,
            "review": review,
            "pending_attention": sum(1 for r in pending if r["needs_attention"]),
            "classified_count": classified,
        }

    def save_classification(self, key: str, choice: str, notes: str = "") -> None:
        if not key or not key.strip():
            raise ValueError("Chybí identifikátor aktiva.")
        cat, ft = self._classification_from_choice(choice)
        self.runner.run_sync(
            self._write_classification, key.strip(), cat.name, ft.name, notes or "",
            timeout=30,
        )

    def _write_classification(self, key: str, cat_name: str, ft_name: str, notes: str) -> None:
        with engine_file_lock():
            classifier = self._new_classifier()
            classifier.classifications_cache[key] = (cat_name, ft_name, notes)
            classifier.save_classifications()
        logger.info(f"Classification saved: {key} -> {cat_name}/{ft_name}")

    def delete_classification(self, key: str) -> None:
        self.runner.run_sync(self._delete_classification, key, timeout=30)

    def _delete_classification(self, key: str) -> None:
        with engine_file_lock():
            classifier = self._new_classifier()
            classifier.classifications_cache.pop(key, None)
            classifier.save_classifications()
        logger.info(f"Classification deleted: {key}")

    # ------------------------------------------------------------------
    # Sell-target ladder (prodejní zóny) — user state about the FUTURE, so it
    # lives next to the other user stores and must survive re-running the
    # pipeline. Keyed by symbol: portfolio.json persists no conid/asset_id and
    # isin is null for options, so symbol is the only reliable join key.
    # ------------------------------------------------------------------

    @property
    def sell_targets_path(self) -> Path:
        """Store location, derived from ``data_dir`` — deliberately NOT a
        module constant in settings.py: the ``service(tmp_path)`` fixture
        injects ``data_dir``, so tests get isolation for free. A settings
        constant would make them write the developer's real file (which is
        why the classification tests must monkeypatch their path)."""
        return self.data_dir / SELL_TARGETS_FILE

    @staticmethod
    def _empty_sell_targets() -> Dict[str, Any]:
        return {"version": SELL_TARGETS_VERSION, "updated_at": None, "targets": {}}

    def load_sell_targets(self) -> Dict[str, Any]:
        """Read the store. Never raises — a missing or corrupt file yields the
        empty store plus a warning, mirroring ``QuoteService._overrides``. A
        hand-edit typo must not take the whole page down."""
        path = self.sell_targets_path
        if not path.is_file():
            return self._empty_sell_targets()
        try:
            data = load_json(path)
        except Exception:  # noqa: BLE001 — corrupt store degrades, never 500s
            logger.warning(f"Unreadable sell-target store {path} — ignoring it.")
            return self._empty_sell_targets()
        if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
            logger.warning(f"Malformed sell-target store {path} — ignoring it.")
            return self._empty_sell_targets()
        data.setdefault("version", SELL_TARGETS_VERSION)
        data.setdefault("updated_at", None)
        return data

    @staticmethod
    def _sorted_zones(zones: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ladder order is price-ascending, enforced on write. A sell ladder is
        monotone by construction, so an explicit order field could contradict
        price order and make "nearest unmet zone" ambiguous — editing a price
        IS the reorder."""
        def _key(z):
            try:
                return Decimal(str(z.get("price") or 0))
            except (InvalidOperation, ArithmeticError):
                return Decimal(0)
        return sorted(zones, key=_key)

    def list_sell_targets(self) -> List[Dict[str, Any]]:
        """UI-ordered view of the store: symbols A→Z, zones price-ascending.
        No live data — that join is the responsibility of the live pass."""
        store = self.load_sell_targets()
        rows = []
        for symbol, entry in sorted(store.get("targets", {}).items()):
            zones = self._sorted_zones(list(entry.get("zones") or []))
            rows.append({
                "symbol": symbol,
                "note": entry.get("note") or "",
                "isin": entry.get("isin"),
                "zones": zones,
                "open_zones": [z for z in zones if not z.get("done_at")],
            })
        return rows

    @staticmethod
    def _optional_quantity(quantity: Any) -> Optional[str]:
        """Quantity is OPTIONAL — the price is what a sell zone is about.

        The user's own sheet admits its share counts are unreliable, so a
        blank or unparseable quantity must never cost him the price. ``None``
        means "unspecified"; downstream that reads as the whole holding.
        """
        raw = str(quantity or "").strip()
        if not raw:
            return None
        qty = _parse_decimal_cz(raw, "počet kusů")
        if qty <= 0:
            raise ValueError("Počet kusů musí být kladný.")
        return str(qty)

    @staticmethod
    def _optional_lot_date(value: Any) -> Optional[str]:
        """Validate an acquisition date used to pin a zone to a purchase.

        Keyed on the DATE alone, not on a lot id: portfolio.json persists no
        lot identity, and "the batch I bought on the dip in February" is how
        the user thinks about it. Same-day fills are aggregated, which is the
        right answer when IBKR split one purchase across several executions.
        """
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            raise ValueError(f"Datum nákupu není platné: {raw!r}.") from None

    def save_sell_zone(self, symbol: str, price: Any, quantity: Any = None,
                       zone_id: Optional[str] = None, note: str = "",
                       currency: Optional[str] = None,
                       isin: Optional[str] = None,
                       lot_acquired: Any = None) -> str:
        """Upsert one rung of a symbol's ladder. Returns the zone id.

        Validates on the caller's thread, then hands the write to the worker
        (same shape as ``save_classification``). ``zone_id=None`` creates.
        """
        sym = (symbol or "").strip()
        if not sym:
            raise ValueError("Chybí symbol titulu.")
        if len(sym) > 40:
            raise ValueError("Symbol je příliš dlouhý (max 40 znaků).")
        # No .upper(): option keys carry spaces and case. No membership check
        # against the run either — a target may precede the purchase.
        price_d = _parse_decimal_cz(price, "cílová cena")
        if price_d <= 0:
            raise ValueError("Cílová cena musí být kladné číslo.")
        qty_s = self._optional_quantity(quantity)
        lot_s = self._optional_lot_date(lot_acquired)
        zid = zone_id or uuid.uuid4().hex[:8]
        self.runner.run_sync(
            self._write_sell_zone, sym, str(price_d), qty_s, zid,
            (note or "").strip(), (currency or "").strip().upper() or None,
            (isin or "").strip() or None, lot_s,
            timeout=30,
        )
        return zid

    def _write_sell_zone(self, symbol: str, price: str, quantity: Optional[str],
                         zone_id: str, note: str, currency: Optional[str],
                         isin: Optional[str], lot_acquired: Optional[str] = None
                         ) -> None:
        def _mutate(store: Dict[str, Any]) -> None:
            targets = store["targets"]
            if symbol not in targets and len(targets) >= MAX_TARGET_SYMBOLS:
                raise ValueError(f"Maximálně {MAX_TARGET_SYMBOLS} titulů.")
            entry = targets.setdefault(symbol, {"note": "", "isin": isin, "zones": []})
            if isin and not entry.get("isin"):
                entry["isin"] = isin
            zones = entry.setdefault("zones", [])
            existing = next((z for z in zones if z.get("id") == zone_id), None)
            for other in zones:
                if other is existing:
                    continue
                if str(other.get("price")) == price:
                    raise ValueError(
                        f"Zóna s cenou {price} u {symbol} už existuje."
                    )
            if existing is None and len(zones) >= MAX_ZONES_PER_SYMBOL:
                raise ValueError(f"Maximálně {MAX_ZONES_PER_SYMBOL} zón na titul.")
            if existing is None:
                zones.append({
                    "id": zone_id, "price": price, "quantity": quantity,
                    "currency": currency, "note": note,
                    "lot_acquired": lot_acquired,
                    "created_at": _utc_now_iso(),
                    # Alert state is three timestamps, never a status string —
                    # a hand-edit then cannot invent an inconsistent state.
                    "reached_at": None, "reached_price": None,
                    "acknowledged_at": None, "done_at": None,
                })
            else:
                existing.update({"price": price, "quantity": quantity,
                                 "note": note, "lot_acquired": lot_acquired})
                if currency:
                    existing["currency"] = currency

            entry["zones"] = self._sorted_zones(zones)

        self._write_sell_targets(_mutate)
        logger.info(f"Sell zone saved: {symbol} @ {price} × {quantity}")

    def delete_sell_zone(self, symbol: str, zone_id: str) -> None:
        self.runner.run_sync(self._delete_sell_zone, symbol, zone_id, timeout=30)

    def _delete_sell_zone(self, symbol: str, zone_id: str) -> None:
        def _mutate(store: Dict[str, Any]) -> None:
            entry = store["targets"].get(symbol)
            if not entry:
                return
            entry["zones"] = [z for z in entry.get("zones", [])
                              if z.get("id") != zone_id]
            if not entry["zones"]:
                store["targets"].pop(symbol, None)

        self._write_sell_targets(_mutate)
        logger.info(f"Sell zone deleted: {symbol}/{zone_id}")

    def delete_sell_target(self, symbol: str) -> None:
        self.runner.run_sync(self._delete_sell_target, symbol, timeout=30)

    def _delete_sell_target(self, symbol: str) -> None:
        self._write_sell_targets(lambda s: s["targets"].pop(symbol, None))
        logger.info(f"Sell target deleted: {symbol}")

    _ZONE_ACTIONS = ("acknowledge", "done", "undone", "rearm")

    def set_zone_state(self, symbol: str, zone_id: str, action: str) -> None:
        """Advance a zone through pending → reached → acknowledged → done.

        One method (and one route) instead of four near-identical endpoints.
        ``rearm`` clears the reached latch so the zone can fire again.
        """
        if action not in self._ZONE_ACTIONS:
            raise ValueError(f"Neznámá akce: {action}.")
        self.runner.run_sync(self._set_zone_state, symbol, zone_id, action, timeout=30)

    def _set_zone_state(self, symbol: str, zone_id: str, action: str) -> None:
        def _mutate(store: Dict[str, Any]) -> None:
            entry = store["targets"].get(symbol) or {}
            zone = next((z for z in entry.get("zones", [])
                         if z.get("id") == zone_id), None)
            if zone is None:
                raise ValueError(f"Zóna {zone_id} u {symbol} neexistuje.")
            now = _utc_now_iso()
            if action == "acknowledge":
                zone["acknowledged_at"] = now
            elif action == "done":
                zone["done_at"] = now
                zone.setdefault("acknowledged_at", None)
                zone["acknowledged_at"] = zone["acknowledged_at"] or now
            elif action == "undone":
                zone["done_at"] = None
            elif action == "rearm":
                zone["reached_at"] = None
                zone["reached_price"] = None
                zone["acknowledged_at"] = None

        self._write_sell_targets(_mutate)
        logger.info(f"Sell zone {action}: {symbol}/{zone_id}")

    # ---- alerts ---------------------------------------------------------

    @staticmethod
    def _zone_is_alerting(zone: Dict[str, Any]) -> bool:
        """Reached, and the user has neither dismissed it nor sold."""
        return bool(zone.get("reached_at") and not zone.get("acknowledged_at")
                    and not zone.get("done_at"))

    def sell_alert_count(self) -> int:
        """Number of zones waiting to be acknowledged.

        Store read only — no quotes, no run load, so the nav badge on every
        page costs a single small file read. It therefore reflects the last
        EVALUATION, not a fresh price check; that is the honest consequence of
        having no background poller.
        """
        try:
            store = self.load_sell_targets()
        except Exception:  # noqa: BLE001 — a badge must never break a page
            return 0
        return sum(
            1
            for entry in store.get("targets", {}).values()
            for zone in entry.get("zones", [])
            if self._zone_is_alerting(zone)
        )

    def _evaluate_sell_targets(self, rows: List[Dict[str, Any]],
                               today: date) -> Optional[Dict[str, Any]]:
        """Latch zones whose live price has reached the target.

        WORKER-THREAD ONLY. This runs inside ``_compute_live_portfolio``,
        which is already executing on the single-worker JobRunner — calling
        ``runner.run_sync`` from here would deadlock until the 120 s timeout.
        The write goes straight through ``_write_sell_targets``.

        Deliberately cheap: a price comparison and, at most, one file write.
        The rich join (lots, time test, proceeds) belongs to
        ``sell_targets_overview`` and only runs on the /targets page.
        """
        try:
            if not self.sell_targets_path.is_file():
                return None            # feature unused → cost is one stat()
            store = self.load_sell_targets()
            targets = store.get("targets") or {}
            if not targets:
                return None

            by_symbol = {r.get("symbol"): r for r in rows}
            dirty = False
            alerts: List[Dict[str, Any]] = []
            now = _utc_now_iso()

            for symbol, entry in targets.items():
                pos = by_symbol.get(symbol)
                # Only a genuine live quote may fire an alert. An EOY mark
                # would make every closed-year run scream permanently, and a
                # position we no longer hold is a watchlist, not a trigger.
                live_ok = bool(pos and pos.get("price_source") == "live"
                               and pos.get("category") != "OPTION")
                live_price = Decimal(str(pos["live_price"])) if live_ok and pos.get("live_price") else None
                live_currency = (pos or {}).get("live_currency")

                for zone in entry.get("zones", []):
                    if zone.get("done_at"):
                        continue
                    if (live_price is not None
                            and not (zone.get("currency") and live_currency
                                     and zone["currency"] != live_currency)
                            and live_price >= Decimal(str(zone["price"]))
                            and not zone.get("reached_at")):
                        zone["reached_at"] = now
                        zone["reached_price"] = str(live_price)
                        dirty = True
                    if self._zone_is_alerting(zone):
                        alerts.append({
                            "symbol": symbol, "zone_id": zone["id"],
                            "price": zone["price"],
                            "currency": zone.get("currency") or live_currency,
                            "quantity": zone.get("quantity"),
                            "reached_at": zone["reached_at"],
                            "reached_price": zone.get("reached_price"),
                            "description": (pos or {}).get("description"),
                        })

            if dirty:
                self._write_sell_targets(lambda s: s.update({"targets": targets}))
            alerts.sort(key=lambda a: a["reached_at"] or "", reverse=True)
            return {"alert_count": len(alerts), "alerts": alerts}
        except Exception:  # noqa: BLE001 — never break the net-worth card
            logger.warning("Sell-target evaluation failed", exc_info=True)
            return None

    # ---- live overview -------------------------------------------------

    @staticmethod
    def group_lots_by_date(pos: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Open lots of a position, aggregated per acquisition date.

        Same-day fills become one purchase, which is how a human refers to a
        lot ("what I bought on the dip on 6 February"). Ordered oldest-first,
        i.e. the order FIFO would consume them in.
        """
        buckets: Dict[str, Dict[str, Any]] = {}
        for lot in (pos or {}).get("lots", []):
            key = lot.get("acquisition_date") or ""
            qty = Decimal(str(lot.get("quantity") or 0))
            if qty <= 0:
                continue
            b = buckets.setdefault(key, {
                "acquired": key or None, "quantity": Decimal(0),
                "cost_eur": Decimal(0), "time_test_deadline": None,
                "estimated": False,
            })
            b["quantity"] += qty
            b["cost_eur"] += qty * Decimal(str(lot.get("unit_cost_eur") or 0))
            b["estimated"] = b["estimated"] or bool(lot.get("acquisition_estimated"))
            deadline = lot.get("time_test_deadline")
            if deadline:
                # Conservative: the latest deadline of the merged fills.
                current = b["time_test_deadline"]
                b["time_test_deadline"] = max(current, deadline) if current else deadline
            else:
                b["time_test_deadline"] = b["time_test_deadline"]
        out = []
        for b in buckets.values():
            b["unit_cost_eur"] = (b["cost_eur"] / b["quantity"]) if b["quantity"] else None
            out.append(b)
        out.sort(key=lambda b: b["acquired"] or "")
        return out

    def position_lots(self, run_id: str, symbol: str) -> List[Dict[str, Any]]:
        """Lot buckets of one holding — feeds the zone editor's lot picker."""
        pf = self.load_portfolio(run_id) or {}
        pos = next((p for p in pf.get("positions", []) if p.get("symbol") == symbol), None)
        if pos is None:
            return []
        today = date.today()
        applies = bool(pos.get("time_test_applicable"))
        out = []
        for b in self.group_lots_by_date(pos):
            exempt = _lot_is_exempt({"time_test_deadline": b["time_test_deadline"]},
                                    applies, today)
            out.append({**b, "exempt": exempt})
        return out

    def sell_targets_overview(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Sell zones joined with live prices, lots and the §4/1/u time test.

        Runs on the request thread (``get_live_portfolio`` does its own
        ``run_sync``), so it must NOT be called from inside the live-portfolio
        worker — that would nest ``run_sync`` on a single-worker pool and hang.
        """
        if run_id is None:
            latest = (self.dashboard_overview() or {}).get("latest") or {}
            run_id = latest.get("run_id")
        # Live valuation FIRST: it is what latches newly reached zones, so
        # reading the store before it would render a stale ladder.
        live = self.get_live_portfolio(run_id) if run_id else None
        targets = self.list_sell_targets()
        by_symbol = {p["symbol"]: p for p in (live or {}).get("positions", [])}
        today = date.today()
        converter = self._converter_factory()

        rows = [self._build_target_row(t, by_symbol.get(t["symbol"]), today, converter)
                for t in targets]
        rows.sort(key=lambda r: (
            r["next_zone"]["distance_pct"] if r.get("next_zone")
            and r["next_zone"].get("distance_pct") is not None else Decimal("9999")
        ))
        return {
            "run_id": run_id, "as_of": (live or {}).get("as_of") or today.isoformat(),
            "tax_year": (live or {}).get("tax_year"),
            "rows": rows,
            "reached_count": sum(r["reached_open"] for r in rows),
            "alerts": ((live or {}).get("sell_targets") or {}).get("alerts") or [],
            "quotes_ok": (live or {}).get("quotes_ok"),
            "quotes_total": (live or {}).get("quotes_total"),
        }

    def _build_target_row(self, target: Dict[str, Any], pos: Optional[Dict[str, Any]],
                          today: date, converter) -> Dict[str, Any]:
        symbol = target["symbol"]
        live_price = pos.get("live_price") if pos else None
        live_price = Decimal(str(live_price)) if live_price is not None else None
        live_currency = (pos or {}).get("live_currency")
        price_source = (pos or {}).get("price_source") or "none"
        held = Decimal(str((pos or {}).get("quantity_long") or 0))
        is_option = (pos or {}).get("category") == "OPTION"

        if pos is None:
            status = "not_held"
        elif is_option:
            status = "option"          # options are never quoted
        elif live_price is None or live_price <= 0:
            status = "no_price"
        else:
            status = "ok"

        # A FIFO cursor over the lot buckets so a ladder cannot sell the same
        # shares twice: zone 2 gets what zone 1 left behind. Lot-pinned zones
        # take their own purchase out of the pool first.
        pool = [dict(b) for b in self.group_lots_by_date(pos)]
        applies = bool((pos or {}).get("time_test_applicable"))
        open_zones = [z for z in target["zones"] if not z.get("done_at")]
        # Three passes, in decreasing order of how firmly a rung is committed:
        #   1. pinned to a purchase  → reserves that lot before anyone else
        #   2. explicit quantity     → consumes the remaining pool FIFO
        #   3. neither               → reports what is LEFT without consuming
        # Pass 3 must not consume: two rungs that both say "sell everything"
        # are an unfinished plan, not a claim on disjoint shares, and letting
        # the first one eat the position left the next showing a bogus 0 ks.
        pinned = [z for z in open_zones if z.get("lot_acquired")]
        sized = [z for z in open_zones if not z.get("lot_acquired") and z.get("quantity")]
        unsized = [z for z in open_zones
                   if not z.get("lot_acquired") and not z.get("quantity")]

        views: Dict[str, Dict[str, Any]] = {}
        for zone in pinned + sized:
            views[zone["id"]] = self._zone_view(
                zone, pool, applies, today, live_price, live_currency,
                converter, consume=True)
        for zone in unsized:
            views[zone["id"]] = self._zone_view(
                zone, pool, applies, today, live_price, live_currency,
                converter, consume=False)
        for zone in target["zones"]:
            if zone.get("done_at"):
                views[zone["id"]] = self._zone_view(
                    zone, [], applies, today, live_price, live_currency,
                    converter, consume=False)

        zone_views = [views[z["id"]] for z in target["zones"]]
        open_views = [v for v in zone_views if not v["done_at"]]
        unreached = [v for v in open_views if not v["reached"]]
        next_zone = min(
            (v for v in unreached if v["distance_pct"] is not None),
            key=lambda v: v["distance_pct"], default=None)
        if next_zone is None and unreached:
            next_zone = unreached[0]
        if next_zone is not None:
            next_zone["is_next"] = True

        return {
            "symbol": symbol, "note": target.get("note") or "",
            "description": (pos or {}).get("description"),
            "status": status, "in_portfolio": pos is not None,
            "held_quantity": held if pos else None,
            "live_price": live_price, "live_currency": live_currency,
            "price_source": price_source,
            "zones": zone_views, "open_zones": open_views, "next_zone": next_zone,
            "reached_open": sum(1 for v in open_views if v["reached"]),
            # What the plan ASKS for, from committed rungs only (a lot or an
            # explicit count). Summing `sellable` instead would hide
            # over-allocation, because the cursor already clamps it to what is
            # left; "sell everything" rungs are excluded so an unfinished plan
            # cannot fire a bogus warning.
            "planned_quantity": sum((v["wanted"] for v in open_views
                                     if v["consumes"]), Decimal(0)),
            "over_allocated": bool(
                pos and sum((v["wanted"] for v in open_views
                             if v["consumes"]), Decimal(0)) > held),
            "proceeds_czk": sum((v["proceeds_czk"] for v in open_views
                                 if v["proceeds_czk"] is not None), Decimal(0)) or None,
        }

    def _zone_view(self, zone: Dict[str, Any], pool: List[Dict[str, Any]],
                   applies: bool, today: date, live_price: Optional[Decimal],
                   live_currency: Optional[str], converter,
                   consume: bool = True) -> Dict[str, Any]:
        price = Decimal(str(zone["price"]))
        pinned_to = zone.get("lot_acquired")

        # How many shares this rung is about: an explicit count, else the
        # pinned lot's size, else whatever the ladder has not spoken for.
        if zone.get("quantity"):
            wanted = Decimal(str(zone["quantity"]))
        elif pinned_to:
            wanted = sum((b["quantity"] for b in pool
                          if b["acquired"] == pinned_to), Decimal(0))
        else:
            wanted = sum((b["quantity"] for b in pool), Decimal(0))

        take_from = ([b for b in pool if b["acquired"] == pinned_to] if pinned_to
                     else list(pool))
        remaining = wanted
        consumed, exempt_qty, unknown_qty = Decimal(0), Decimal(0), Decimal(0)
        wait_until = None
        for bucket in take_from:
            if remaining <= 0:
                break
            take = min(bucket["quantity"], remaining)
            if take <= 0:
                continue
            if consume:
                bucket["quantity"] -= take      # the cursor: later rungs see less
            remaining -= take
            consumed += take
            state = _lot_is_exempt({"time_test_deadline": bucket["time_test_deadline"]},
                                   applies, today)
            if state is True:
                exempt_qty += take
            elif state is None:
                unknown_qty += take             # estimated date — never guess
            else:
                deadline = bucket["time_test_deadline"]
                if deadline:
                    wait_until = max(wait_until or deadline, deadline)

        distance_pct = None
        currency_mismatch = bool(zone.get("currency") and live_currency
                                 and zone["currency"] != live_currency)
        if live_price and live_price > 0 and not currency_mismatch:
            distance_pct = (price - live_price) / live_price * Decimal(100)

        proceeds_ccy = consumed * price if consumed else None
        proceeds_czk = (self._to_czk(converter, proceeds_ccy,
                                     zone.get("currency") or live_currency or "USD", today)
                        if proceeds_ccy else None)
        return {
            **zone,
            "price_d": price,
            "sellable": consumed,
            "wanted": wanted,
            "consumes": consume,
            "unspecified": not consume,
            "short_of_plan": bool(consume and consumed < wanted),
            "lot_missing": bool(pinned_to and consumed == 0),
            "distance_pct": distance_pct,
            "currency_mismatch": currency_mismatch,
            "reached": bool(live_price and live_price >= price and not currency_mismatch),
            "exempt_quantity": exempt_qty,
            "unknown_quantity": unknown_qty,
            "taxable_quantity": consumed - exempt_qty - unknown_qty,
            "exempt_from": (date.fromisoformat(wait_until) + timedelta(days=1)).isoformat()
                           if wait_until else None,
            "days_remaining": ((date.fromisoformat(wait_until) - today).days + 1)
                              if wait_until else None,
            "proceeds_ccy": proceeds_ccy,
            "proceeds_czk": proceeds_czk,
            "is_next": False,
        }

    def zone_tax_impact(self, run_id: Optional[str], symbol: str,
                        zone_id: str) -> Dict[str, Any]:
        """What selling this rung at its target price would mean, tax-wise.

        On demand only — one click, one simulation. Never for every row on
        page load: each call builds a fresh ČNB converter (which loads a
        ~267 kB cache and may hit the network) and takes a turn on the
        single-worker executor, so 25 of them would queue behind any running
        pipeline.

        The zone decides both the size and WHICH shares: a pinned rung skips
        everything acquired earlier, an ordinary rung skips what the cheaper
        committed rungs already spoke for. Returns the ``simulate_sale``
        payload plus the zone context the template needs.
        """
        if not run_id:
            raise ValueError("Zatím není žádný výpočet — spusť ho na stránce Výpočty.")
        overview_row = next(
            (r for r in self.sell_targets_overview(run_id)["rows"]
             if r["symbol"] == symbol), None)
        if overview_row is None:
            raise ValueError(f"Titul {symbol} v plánu není.")
        zone = next((z for z in overview_row["zones"] if z["id"] == zone_id), None)
        if zone is None:
            raise ValueError("Zóna neexistuje.")
        if not overview_row["in_portfolio"]:
            raise ValueError(f"{symbol} není mezi otevřenými pozicemi — nelze simulovat.")

        qty = zone["sellable"]
        if qty <= 0:
            raise ValueError(
                "Na tuhle zónu nezbývají žádné kusy — dřívější zóny žebříku je "
                "už rozebraly, nebo navázaný nákup v portfoliu není."
            )

        skip = Decimal(0)
        pf = self.load_portfolio(run_id) or {}
        pos = next((p for p in pf.get("positions", []) if p.get("symbol") == symbol), None)
        buckets = self.group_lots_by_date(pos)
        if zone.get("lot_acquired"):
            skip = sum((b["quantity"] for b in buckets
                        if (b["acquired"] or "") < zone["lot_acquired"]), Decimal(0))
        else:
            skip = sum(
                (z["sellable"] for z in overview_row["open_zones"]
                 if z["consumes"] and not z.get("lot_acquired")
                 and Decimal(str(z["price"])) < Decimal(str(zone["price"]))),
                Decimal(0),
            )
        sim = self.simulate_sale(run_id, symbol, quantity=qty,
                                 price=Decimal(str(zone["price"])),
                                 skip_quantity=skip)
        return {"sim": sim, "zone": zone, "symbol": symbol,
                "skip_quantity": skip,
                "pairing_method": (self.get_run(run_id) or {}).get("pairing_method")}

    # ---- spreadsheet import -------------------------------------------

    # Header keywords, matched against _norm_text of the header cell.
    _HDR_SYMBOL = ("titul", "symbol", "ticker", "nazev", "akcie", "instrument")
    _HDR_PRICE = ("cena", "zona", "sell", "target", "cil")
    _HDR_QTY = ("ks", "kus", "pocet", "mnozstvi", "quantity", "qty")

    @staticmethod
    def _sellable_positions(pf: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Non-option positions of a run — the universe a pasted name may
        resolve to. Options are excluded: their key embeds expiry and strike,
        so a ladder on one goes stale with the contract."""
        return [p for p in (pf or {}).get("positions", [])
                if p.get("category") != "OPTION"]

    def resolve_target_symbol(self, value: str,
                              positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Map a pasted cell to an IBKR symbol.

        His sheet's "Titul" column holds company NAMES, often truncated by the
        column width ("Franklin FTSE Korea UCITS ET"), so an exact symbol match
        is the exception, not the rule. Ladder of strategies, most trustworthy
        first; anything not clearly resolved comes back with ``symbol=None``
        and candidates for the user to pick in the preview.
        """
        raw = (value or "").strip()
        out = {"symbol": None, "how": "", "candidates": []}
        if not raw:
            out["how"] = "prázdné"
            return out

        by_symbol = {p["symbol"]: p for p in positions}
        if raw in by_symbol:
            return {"symbol": raw, "how": "symbol", "candidates": []}
        for sym in by_symbol:
            if sym.lower() == raw.lower():
                return {"symbol": sym, "how": "symbol", "candidates": []}

        needle = _norm_text(raw)
        if not needle:
            out["how"] = "prázdné"
            return out

        exact = [p["symbol"] for p in positions
                 if _norm_text(p.get("description")) == needle]
        if len(exact) == 1:
            return {"symbol": exact[0], "how": "název", "candidates": []}

        # Prefix both ways: the sheet value may be truncated, or it may carry
        # a suffix the description lacks ("Evolution AB (publ)" vs "EVOLUTION AB").
        prefix = []
        for p in positions:
            desc = _norm_text(p.get("description"))
            if desc and (desc.startswith(needle) or needle.startswith(desc)):
                prefix.append(p["symbol"])
        if len(prefix) == 1:
            return {"symbol": prefix[0], "how": "částečný název", "candidates": []}
        if len(prefix) > 1:
            return {"symbol": None, "how": "víc shod", "candidates": sorted(prefix)}

        scored = sorted(
            ((SequenceMatcher(None, needle, _norm_text(p.get("description"))).ratio(),
              p["symbol"]) for p in positions),
            key=lambda t: (-t[0], t[1]),
        )
        if scored:
            best_score, best_sym = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            if best_score >= _MATCH_THRESHOLD and (best_score - runner_up) >= _MATCH_MARGIN:
                return {"symbol": best_sym, "how": f"podobnost {best_score:.0%}",
                        "candidates": []}
            out["candidates"] = [s for _, s in scored[:5]]
            out["how"] = f"nejisté (nejblíž {best_sym}, {best_score:.0%})"
        else:
            out["how"] = "žádné pozice k porovnání"
        return out

    def parse_sell_targets(self, text: str,
                           positions: Optional[List[Dict[str, Any]]] = None
                           ) -> Dict[str, Any]:
        """Parse a pasted sheet into rows. Pure — never writes.

        Returns ``{"rows": [...], "skipped": [...], "header_used": bool}``.
        Every row carries what was understood so the preview can show it and
        the user can correct the symbol before anything is stored.
        """
        positions = positions if positions is not None else []
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if len(lines) > MAX_IMPORT_LINES:
            raise ValueError(
                f"Příliš mnoho řádků ({len(lines)}), maximum je {MAX_IMPORT_LINES}."
            )

        rows: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        cols: Optional[Dict[str, int]] = None

        first = _split_import_line(lines[0]) if lines else []
        if first and self._looks_like_header(first):
            cols = self._map_header(first)
            lines = lines[1:]

        for offset, line in enumerate(lines, start=2 if cols else 1):
            fields = _split_import_line(line)
            try:
                raw_symbol, raw_price, raw_qty = self._pick_fields(fields, cols)
                price = _parse_decimal_cz(raw_price, "cílová cena")
                if price <= 0:
                    raise ValueError("Cílová cena musí být kladné číslo.")
            except ValueError as exc:
                # Only a missing/broken PRICE loses the row — that is the one
                # thing a sell zone cannot do without.
                skipped.append({"line": offset, "raw": line.strip(), "error": str(exc)})
                continue
            try:
                qty = self._optional_quantity(raw_qty)
                qty_note = ""
            except ValueError:
                # Share counts in the source sheet are known to be unreliable;
                # keep the price and flag the quantity rather than dropping it.
                qty, qty_note = None, f"počet kusů nepřečten ({raw_qty.strip()[:20]})"
            match = self.resolve_target_symbol(raw_symbol, positions)
            rows.append({
                "line": offset, "raw_symbol": raw_symbol.strip(),
                "symbol": match["symbol"], "match": match["how"],
                "candidates": match["candidates"],
                "price": str(price), "quantity": qty, "quantity_note": qty_note,
            })
        return {"rows": rows, "skipped": skipped, "header_used": cols is not None}

    def _looks_like_header(self, fields: List[str]) -> bool:
        """A header row names columns and carries no parseable number."""
        joined = " ".join(_norm_text(f) for f in fields)
        if not self._header_matches(joined, self._HDR_SYMBOL + self._HDR_PRICE):
            return False
        for f in fields:
            try:
                _parse_decimal_cz(f, "x")
                return False       # a number ⇒ this is data, not a header
            except ValueError:
                continue
        return True

    @staticmethod
    def _header_matches(name: str, keywords) -> bool:
        """Token-prefix match: a keyword must start a word of the header.

        Prefix rather than equality so Czech case endings still hit ("Kusů" →
        "kusu" matches "kus"); anchored to a word start rather than a bare
        substring so "ks" cannot fire inside an unrelated word.
        """
        return any(tok.startswith(k) for tok in name.split() for k in keywords)

    def _map_header(self, fields: List[str]) -> Dict[str, int]:
        cols: Dict[str, int] = {}
        for idx, field in enumerate(fields):
            name = _norm_text(field)
            if not name:
                continue
            # "% k prodejní zóně" is a computed column — never let it win the
            # price slot just because it mentions the zone.
            is_pct = "%" in field or name.startswith("procent")
            if "symbol" not in cols and self._header_matches(name, self._HDR_SYMBOL):
                cols["symbol"] = idx
            elif (not is_pct and "price" not in cols
                    and self._header_matches(name, self._HDR_PRICE)):
                cols["price"] = idx
            elif "quantity" not in cols and self._header_matches(name, self._HDR_QTY):
                cols["quantity"] = idx
        return cols

    @staticmethod
    def _pick_fields(fields: List[str], cols: Optional[Dict[str, int]]):
        def at(i):
            return fields[i] if 0 <= i < len(fields) else ""
        if cols:
            # Quantity is optional: a sheet with only names and prices is a
            # perfectly good sell plan.
            missing = [n for n in ("symbol", "price") if n not in cols]
            if missing:
                raise ValueError(
                    "V hlavičce chybí sloupec: " + ", ".join(missing) + "."
                )
            return (at(cols["symbol"]), at(cols["price"]),
                    at(cols["quantity"]) if "quantity" in cols else "")
        if len(fields) < 2:
            raise ValueError("Řádek musí mít aspoň 2 sloupce: symbol a cenu.")
        return fields[0], fields[1], fields[2] if len(fields) > 2 else ""

    def import_sell_targets(self, rows: List[Dict[str, Any]],
                            replace: bool = False) -> Dict[str, Any]:
        """Write reviewed rows into the store.

        Takes the rows the user CONFIRMED in the preview, not raw text — so
        what gets written is exactly what was on screen, with no re-parse in
        between. Per symbol the paste replaces the OPEN zones and keeps the
        ones already marked sold; otherwise re-importing the sheet would
        duplicate every rung. ``replace=True`` wipes the store first.
        """
        clean: List[Dict[str, Any]] = []
        for row in rows:
            symbol = (row.get("symbol") or "").strip()
            if not symbol:
                continue
            price = _parse_decimal_cz(row.get("price"), "cílová cena")
            if price <= 0:
                raise ValueError(f"Neplatná cílová cena u {symbol}.")
            clean.append({"symbol": symbol, "price": str(price),
                          "quantity": self._optional_quantity(row.get("quantity")),
                          "note": (row.get("note") or "").strip()})
        if not clean:
            raise ValueError("Není co importovat — vyber aspoň jeden řádek.")

        self.runner.run_sync(self._write_import, clean, bool(replace), timeout=60)
        return {"imported": len(clean),
                "symbols": sorted({r["symbol"] for r in clean})}

    def _write_import(self, rows: List[Dict[str, Any]], replace: bool) -> None:
        def _mutate(store: Dict[str, Any]) -> None:
            if replace:
                store["targets"] = {}
            targets = store["targets"]
            touched = {r["symbol"] for r in rows}
            for symbol in touched:
                entry = targets.setdefault(symbol, {"note": "", "isin": None, "zones": []})
                # Keep sold rungs — they are history, not plan.
                entry["zones"] = [z for z in entry.get("zones", []) if z.get("done_at")]
            for r in rows:
                entry = targets[r["symbol"]]
                if len(entry["zones"]) >= MAX_ZONES_PER_SYMBOL:
                    continue
                if any(str(z.get("price")) == r["price"] for z in entry["zones"]):
                    continue
                entry["zones"].append({
                    "id": uuid.uuid4().hex[:8], "price": r["price"],
                    "quantity": r["quantity"], "currency": None, "note": r["note"],
                    "created_at": _utc_now_iso(), "reached_at": None,
                    "reached_price": None, "acknowledged_at": None, "done_at": None,
                })
            for symbol in touched:
                targets[symbol]["zones"] = self._sorted_zones(targets[symbol]["zones"])
                if not targets[symbol]["zones"]:
                    targets.pop(symbol, None)

        self._write_sell_targets(_mutate)
        logger.info(f"Sell targets imported: {len(rows)} zones")

    def _write_sell_targets(self, mutator) -> None:
        """Read-modify-write under a DEDICATED lock.

        Not the global ``engine_file_lock()``: that one exists because the
        pipeline reads ``user_classifications.json``. Nothing but this feature
        touches the sell-target store, so sharing the engine lock would make
        saving a zone block behind a multi-minute run — and block the run.
        """
        with engine_file_lock(lock_file=self.data_dir / SELL_TARGETS_LOCK):
            store = self.load_sell_targets()
            mutator(store)
            store["version"] = SELL_TARGETS_VERSION
            store["updated_at"] = _utc_now_iso()
            self.sell_targets_path.parent.mkdir(parents=True, exist_ok=True)
            dump_json(store, self.sell_targets_path)

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

    def dashboard_overview(self) -> Dict[str, Any]:
        """Home-page summary: current-year run + newest run per year.

        ``latest`` drives the live net-worth + options card, so it tracks the
        highest (most current) tax year rather than the most recently executed
        run — re-running a closed year to tweak a filing must not flip the "net
        worth today" view back to that year's 31 Dec snapshot. Grouped by
        account so a second account later is a data change, not a UI rewrite.
        """
        runs = self.list_runs(limit=100)  # newest first
        per_year: Dict[int, Dict[str, Any]] = {}
        accounts: set = set()
        for meta in runs:
            year = meta.get("tax_year")
            if year is not None and year not in per_year:
                per_year[year] = meta  # newest run of that year (runs are newest first)
            for acc in (meta.get("account_ids") or []):
                accounts.add(acc)
        # Newest run of the highest tax year; fall back to newest run overall
        # when a run carries no tax year.
        latest = per_year[max(per_year)] if per_year else (runs[0] if runs else None)
        return {
            "has_runs": bool(runs),
            "latest": latest,
            "year_cards": [per_year[y] for y in sorted(per_year, reverse=True)],
            "accounts": sorted(accounts),
        }

    def get_dashboard_valuation(self) -> Optional[Dict[str, Any]]:
        """Live CZK valuation of the latest run for the net-worth card.

        Returns None when there is no run yet or quoting/valuation yields
        nothing. Later this will aggregate the latest run per account.
        """
        overview = self.dashboard_overview()
        latest = overview.get("latest")
        if not latest:
            return None
        run_id = latest.get("run_id")
        live = self.get_live_portfolio(run_id)
        if live is None:
            return None
        return {"run_id": run_id, "meta": latest, "live": live}

    def run_pipeline_sync(
        self, tax_year: int, fx_mode: str = "compare", pairing_method: str = "fifo",
        force: bool = False,
    ) -> Dict[str, Any]:
        """Run the full pipeline synchronously (MCP tools wait for the result).

        Reuses an identical past run when ``force`` is False — unchanged data
        returns instantly instead of recomputing.
        """
        if fx_mode not in FX_MODES:
            raise ValueError(f"Neznámý kurzový režim: {fx_mode}")
        if pairing_method not in PAIRING_METHODS:
            raise ValueError(f"Neznámá párovací metoda: {pairing_method}")
        if not force:
            cached = self.find_cached_run(tax_year, fx_mode, pairing_method)
            if cached:
                meta = self.get_run(cached)
                if meta is not None:
                    return meta
        run_id = f"{tax_year}-{datetime.now():%Y%m%d-%H%M%S}"
        return self.runner.run_sync(
            self._execute_run, run_id, tax_year, fx_mode, None, None, None, pairing_method,
            timeout=600,
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
            # Backfill: setdefault only looks at the first payment of the year,
            # so a January payout that carried no country left the symbol blank
            # for all twelve months.
            if not a["country"] and it.get("source_country"):
                a["country"] = it["source_country"]
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
        """Per-lot §4/1/u countdown computed from the persisted portfolio."""
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

    def disposal_summary(
        self, run_id: str, mode: str, symbol: Optional[str] = None,
        include_lots: bool = False, sort: str = DEFAULT_DISPOSAL_SORT,
        category: Optional[str] = None, date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Realized §10 disposals from a persisted run.

        Per-symbol aggregation always; per-lot pairing rows (which acquisition
        each sale consumed) when ``symbol`` is given or ``include_lots`` is
        True — kept opt-in so a busy year doesn't flood the MCP client.

        Single source for the web page AND the MCP tool. ``symbol`` matches
        the exact asset symbol and the options written on that underlying
        (see ``symbol_matches``); ``category`` and the date window come from
        ``item_matches``. The per-lot rows always show exact symbols.
        """
        result = self.load_result(run_id, mode)
        if result is None:
            return None
        TWO = Decimal("0.01")
        want = (symbol or "").strip().upper()

        by_symbol: Dict[str, Dict[str, Any]] = {}
        lots: List[Dict[str, Any]] = []
        totals = {
            "count": 0, "proceeds_czk": Decimal(0), "cost_basis_czk": Decimal(0),
            "gain_loss_czk": Decimal(0), "taxable_gain_loss_czk": Decimal(0),
            "exempt_gain_loss_czk": Decimal(0), "fx_failed_count": 0,
        }

        def _q2(value) -> Optional[Decimal]:
            return None if value is None else Decimal(value).quantize(TWO)

        for it in result.get("items", []):
            if it.get("item_type") not in DISPOSAL_ITEM_TYPES:
                continue
            sym = it.get("asset_symbol") or "?"
            if not symbol_matches(sym, it.get("asset_category"),
                                  it.get("asset_description"), want):
                continue
            if not item_matches(it, category=category, date_from=date_from,
                                date_to=date_to):
                continue
            gain = Decimal(it.get("gain_loss_czk") or 0)
            proceeds = Decimal(it.get("proceeds_czk") or 0)
            cost = Decimal(it.get("cost_basis_czk") or 0)
            # A null CZK leg means the FX conversion FAILED (item_builder
            # contract) — the money is unknown, not zero. Sums below coerce
            # nulls to 0, so flag every affected item loudly.
            fx_failed = bool(it.get("fx_conversion_failed")) or any(
                it.get(k) is None for k in
                ("proceeds_czk", "cost_basis_czk", "gain_loss_czk"))
            a = by_symbol.setdefault(sym, {
                "symbol": sym, "description": it.get("asset_description"),
                "category": it.get("asset_category"), "count": 0,
                "quantity_sold": Decimal(0), "proceeds_czk": Decimal(0),
                "cost_basis_czk": Decimal(0), "gain_loss_czk": Decimal(0),
                "taxable_gain_loss_czk": Decimal(0),
                "exempt_gain_loss_czk": Decimal(0), "fx_failed_count": 0,
                "first_sale": it.get("event_date"),
                "last_sale": it.get("event_date"),
            })
            if fx_failed:
                a["fx_failed_count"] += 1
                totals["fx_failed_count"] += 1
            a["count"] += 1
            a["quantity_sold"] += Decimal(it.get("quantity") or 0)
            a["proceeds_czk"] += proceeds
            a["cost_basis_czk"] += cost
            a["gain_loss_czk"] += gain
            totals["count"] += 1
            totals["proceeds_czk"] += proceeds
            totals["cost_basis_czk"] += cost
            totals["gain_loss_czk"] += gain
            if it.get("is_exempt"):
                a["exempt_gain_loss_czk"] += gain
                totals["exempt_gain_loss_czk"] += gain
            elif it.get("is_taxable"):
                a["taxable_gain_loss_czk"] += gain
                totals["taxable_gain_loss_czk"] += gain
            d = it.get("event_date")
            if d:
                a["first_sale"] = min(a["first_sale"] or d, d)
                a["last_sale"] = max(a["last_sale"] or d, d)
            if want or include_lots:
                lots.append({
                    "symbol": sym, "item_type": it.get("item_type"),
                    "sale_date": it.get("event_date"),
                    "acquisition_date": it.get("acquisition_date"),
                    "holding_period_days": it.get("holding_period_days"),
                    "quantity": it.get("quantity"),
                    "quantity_display": _qty_display(it.get("quantity")),
                    "proceeds_czk": _q2(it.get("proceeds_czk")),
                    "cost_basis_czk": _q2(it.get("cost_basis_czk")),
                    "gain_loss_czk": _q2(it.get("gain_loss_czk")),
                    "is_taxable": it.get("is_taxable"),
                    "is_exempt": it.get("is_exempt"),
                    "exemption_reason": it.get("exemption_reason"),
                    "tax_review_status": it.get("tax_review_status"),
                })

        for a in by_symbol.values():
            for key in ("proceeds_czk", "cost_basis_czk", "gain_loss_czk",
                        "taxable_gain_loss_czk", "exempt_gain_loss_czk"):
                a[key] = a[key].quantize(TWO)
            # FIFO quantities carry eight tail zeros. format() rather than
            # plain normalize(), which turns Decimal("100") into "1E+2".
            a["quantity_display"] = _qty_display(a["quantity_sold"])
        for key in ("proceeds_czk", "cost_basis_czk", "gain_loss_czk",
                    "taxable_gain_loss_czk", "exempt_gain_loss_czk"):
            totals[key] = totals[key].quantize(TWO)

        sort_key, descending = DISPOSAL_SORTS.get(
            sort, DISPOSAL_SORTS[DEFAULT_DISPOSAL_SORT])
        out: Dict[str, Any] = {
            "symbol_filter": symbol, "totals": totals, "sort": sort,
            "filters": {"symbol": symbol or None, "category": category or None,
                        "date_from": date_from or None, "date_to": date_to or None},
            "by_symbol": sorted(by_symbol.values(), key=sort_key,
                                reverse=descending),
            "lots": sorted(lots, key=lambda l: (l["sale_date"] or "",
                                                l["symbol"])),
            "note": ("Gross per-item aggregation; the §10 taxable netting "
                     "(cross-symbol loss offset, annual limit) is in "
                     "get_tax_summary → cz_10_summary."),
        }
        if not lots and not (want or include_lots):
            out["lot_detail"] = ("Pass symbol=... or include_lots=true for "
                                 "per-lot pairing rows.")
        if totals["fx_failed_count"]:
            out["fx_warning"] = (
                f"WARNING: {totals['fx_failed_count']} disposal(s) have a "
                f"failed FX→CZK conversion — their null money legs count as 0 "
                f"in these sums, so the affected gains are UNKNOWN, not zero. "
                f"See per-lot rows / get_pending_review_items."
            )
        return out

    # Headline liability lines diffed by compare_runs (order = display order).
    _COMPARE_LIABILITY_KEYS = (
        "taxable_dividends_czk", "taxable_interest_czk",
        "taxable_securities_net_czk", "taxable_options_net_czk",
        "combined_taxable_base_czk", "gross_czech_tax_czk",
        "final_creditable_ftc_czk", "final_czech_tax_after_credit_czk",
    )

    def compare_runs(
        self, run_id_a: str, run_id_b: str, mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Diff two persisted runs: headline liability lines plus per-symbol
        realized-gain deltas decomposed into proceeds vs cost-basis legs.

        Built for pairing-method / FX-mode what-ifs (FIFO vs LIFO runs of the
        same year). All deltas are ``b − a``. Raises ValueError on unknown
        run ids or when the runs share no FX mode.
        """
        meta_a = self.get_run(run_id_a)
        if meta_a is None:
            raise ValueError(f"Run '{run_id_a}' not found — list_datasets shows recent run ids.")
        meta_b = self.get_run(run_id_b)
        if meta_b is None:
            raise ValueError(f"Run '{run_id_b}' not found — list_datasets shows recent run ids.")
        modes_a = meta_a.get("modes") or ["daily"]
        modes_b = meta_b.get("modes") or ["daily"]
        if mode is None:
            shared = [m for m in modes_a if m in modes_b]
            if not shared:
                raise ValueError(
                    f"No shared FX mode: {run_id_a} has {modes_a}, "
                    f"{run_id_b} has {modes_b}."
                )
            mode = "daily" if "daily" in shared else shared[0]
        elif mode not in modes_a or mode not in modes_b:
            raise ValueError(
                f"FX mode '{mode}' not available in both runs: {run_id_a} "
                f"has {modes_a}, {run_id_b} has {modes_b}. Omit fx_mode to "
                f"compare on a mode both runs share."
            )
        result_a = self.load_result(run_id_a, mode)
        if result_a is None:
            raise ValueError(
                f"Run '{run_id_a}' has no '{mode}' result file (meta lists "
                f"modes {modes_a}) — re-run run_pipeline."
            )
        result_b = self.load_result(run_id_b, mode)
        if result_b is None:
            raise ValueError(
                f"Run '{run_id_b}' has no '{mode}' result file (meta lists "
                f"modes {modes_b}) — re-run run_pipeline."
            )
        TWO = Decimal("0.01")

        def _delta(b: Decimal, a: Decimal) -> Decimal:
            d = (b - a).quantize(TWO)
            return d if d else Decimal("0.00")  # normalize -0.00

        def _brief(meta: Dict[str, Any]) -> Dict[str, Any]:
            return {k: meta.get(k) for k in
                    ("run_id", "tax_year", "fx_mode", "pairing_method",
                     "created_at", "summary")}

        def _liability(result: Dict[str, Any]) -> Dict[str, Any]:
            return ((result.get("sections") or {})
                    .get("cz_tax_liability", {}).get("line_items", {}))

        li_a, li_b = _liability(result_a), _liability(result_b)
        liability = []
        for key in self._COMPARE_LIABILITY_KEYS:
            va, vb = li_a.get(key), li_b.get(key)
            if va is None and vb is None:
                continue
            da = Decimal(va) if va is not None else Decimal(0)
            db = Decimal(vb) if vb is not None else Decimal(0)
            liability.append({"line": key, "a": va, "b": vb,
                              "delta": _delta(db, da)})

        def _gains(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
            agg: Dict[str, Dict[str, Any]] = {}
            for it in result.get("items", []):
                if it.get("item_type") not in DISPOSAL_ITEM_TYPES:
                    continue
                sym = it.get("asset_symbol") or "?"
                a = agg.setdefault(sym, {
                    "count": 0, "proceeds_czk": Decimal(0),
                    "cost_basis_czk": Decimal(0), "gain_loss_czk": Decimal(0),
                    "taxable_gain_loss_czk": Decimal(0), "fx_failed": 0,
                })
                gain = Decimal(it.get("gain_loss_czk") or 0)
                a["count"] += 1
                a["proceeds_czk"] += Decimal(it.get("proceeds_czk") or 0)
                a["cost_basis_czk"] += Decimal(it.get("cost_basis_czk") or 0)
                a["gain_loss_czk"] += gain
                if it.get("is_taxable") and not it.get("is_exempt"):
                    a["taxable_gain_loss_czk"] += gain
                if bool(it.get("fx_conversion_failed")) or any(
                        it.get(k) is None for k in
                        ("proceeds_czk", "cost_basis_czk", "gain_loss_czk")):
                    a["fx_failed"] += 1
            return agg

        gains_a, gains_b = _gains(result_a), _gains(result_b)
        ZERO_ROW = {"count": 0, "proceeds_czk": Decimal(0),
                    "cost_basis_czk": Decimal(0), "gain_loss_czk": Decimal(0),
                    "taxable_gain_loss_czk": Decimal(0), "fx_failed": 0}
        changed, unchanged = [], []
        for sym in sorted(set(gains_a) | set(gains_b)):
            ra = gains_a.get(sym, ZERO_ROW)
            rb = gains_b.get(sym, ZERO_ROW)
            # Quantize the endpoints first and derive the delta from them so
            # gain_b − gain_a always equals gain_delta exactly in the output.
            # Deltas come from the RAW sums, so the decomposition is exact:
            # raw gain ≡ raw proceeds − raw cost per item, hence
            # gain_delta == proceeds_delta − cost_delta after rounding.
            # The displayed gain_b is then derived from gain_a + the delta
            # (rather than rounded independently) so the row's other identity,
            # gain_b − gain_a == gain_delta, holds too. Rounding all three
            # endpoints independently cannot satisfy both at once, and a
            # haléř gap in either reads as an arithmetic error.
            gain_delta = _delta(rb["gain_loss_czk"], ra["gain_loss_czk"])
            proceeds_delta = _delta(rb["proceeds_czk"], ra["proceeds_czk"])
            cost_delta = _delta(rb["cost_basis_czk"], ra["cost_basis_czk"])
            gain_a_q = ra["gain_loss_czk"].quantize(TWO)
            gain_b_q = gain_a_q + gain_delta
            # The gross figures can be pairing-invariant while the §4/1/u
            # exemption flips (a different lot fails the time test) — the
            # taxable split must participate in the changed/unchanged call.
            # From raw sums like the gain, so an all-taxable symbol never
            # shows the two deltas disagreeing by a haléř.
            taxable_delta = _delta(rb["taxable_gain_loss_czk"],
                                   ra["taxable_gain_loss_czk"])
            fx_a, fx_b = ra["fx_failed"], rb["fx_failed"]
            if (gain_delta == 0 and proceeds_delta == 0 and cost_delta == 0
                    and taxable_delta == 0 and fx_a == fx_b):
                unchanged.append(sym)
                continue
            row = {
                "symbol": sym,
                "gain_a_czk": gain_a_q,
                "gain_b_czk": gain_b_q,
                "gain_delta_czk": gain_delta,
                "taxable_gain_delta_czk": taxable_delta,
                "proceeds_delta_czk": proceeds_delta,
                "cost_basis_delta_czk": cost_delta,
                "count_a": ra["count"], "count_b": rb["count"],
            }
            if fx_a or fx_b:
                row["fx_failed_a"] = fx_a
                row["fx_failed_b"] = fx_b
            changed.append(row)
        changed.sort(key=lambda r: max(abs(r["gain_delta_czk"]),
                                       abs(r["taxable_gain_delta_czk"])),
                     reverse=True)

        notes = ["All deltas are b − a."]
        fx_total = (sum(r["fx_failed"] for r in gains_a.values())
                    + sum(r["fx_failed"] for r in gains_b.values()))
        if fx_total:
            notes.append(
                "Some items have failed FX→CZK conversions (null money legs "
                "counted as 0) — deltas on symbols flagged fx_failed_a/b may "
                "reflect FX-rate coverage, not the settings change."
            )
        if meta_a.get("tax_year") != meta_b.get("tax_year"):
            notes.append(
                f"Runs cover different tax years ({meta_a.get('tax_year')} vs "
                f"{meta_b.get('tax_year')}) — per-symbol deltas compare "
                f"different periods."
            )
        # The fingerprint mixes dataset content with the requested fx mode and
        # pairing method — only when those settings match does a fingerprint
        # difference imply the *data* changed. Without these guards the note
        # would fire on every FIFO-vs-LIFO comparison, i.e. the headline use
        # case. (meta fx_mode is the EFFECTIVE mode, so a compare→daily
        # downgrade can still leave a false positive — erring loud is fine.)
        if (meta_a.get("input_fingerprint") and meta_b.get("input_fingerprint")
                and meta_a.get("tax_year") == meta_b.get("tax_year")
                and meta_a.get("fx_mode") == meta_b.get("fx_mode")
                and meta_a.get("pairing_method") == meta_b.get("pairing_method")
                and meta_a["input_fingerprint"] != meta_b["input_fingerprint"]):
            notes.append("Input data changed between the runs — deltas may "
                         "reflect new statements, not settings.")
        return {
            "mode": mode,
            "runs": {"a": _brief(meta_a), "b": _brief(meta_b)},
            "liability": liability,
            "by_symbol": changed,
            "unchanged_symbols": unchanged,
            "notes": notes,
        }

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
