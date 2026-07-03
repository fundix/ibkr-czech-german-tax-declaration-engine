# src/webapp/settings.py
"""Paths and constants for the local web GUI."""
from pathlib import Path

import src.config as config

PROJECT_ROOT = Path(config.__file__).resolve().parent.parent

# Per-year input datasets live here: data/webapp/<year>/<canonical name>
DATA_DIR = PROJECT_ROOT / "data" / "webapp"

# Every run persists its inputs + exports here: out/webapp_runs/<run_id>/
RUNS_DIR = PROJECT_ROOT / "out" / "webapp_runs"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321

# Canonical file names inside a year dataset directory, keyed by slot.
SLOT_FILES = {
    "trades": "trades.csv",
    "cash": "cash_transactions.csv",
    "positions_start": "positions_start.csv",
    "positions_end": "positions_end.csv",
    "corp_actions": "corporate_actions.csv",
}

SLOT_LABELS = {
    "trades": "Obchody (Trades)",
    "cash": "Cash transakce (dividendy, úroky, WHT)",
    "positions_start": "Pozice na začátku roku",
    "positions_end": "Pozice na konci roku",
    "corp_actions": "Korporátní akce",
}

# A run needs these; the rest can be derived (positions_start from the
# previous year's positions_end) or generated header-only (corp_actions).
REQUIRED_SLOTS = ("trades", "cash", "positions_end")

# Header-only fallbacks for optional inputs (the parsers accept zero rows).
POSITIONS_HEADER = (
    '"ClientAccountID","CurrencyPrimary","AssetClass","SubCategory","Symbol",'
    '"Description","Conid","ISIN","UnderlyingSymbol","Multiplier","Quantity",'
    '"MarkPrice","PositionValue","CostBasisMoney","UnderlyingConid"\n'
)
CORP_ACTIONS_HEADER = (
    '"ClientAccountID","CurrencyPrimary","AssetClass","Symbol","Description",'
    '"Conid","ISIN","UnderlyingConid","UnderlyingSymbol","Report Date",'
    '"Amount","Proceeds","Value","Quantity","Code","Type","ActionID"\n'
)
