# src/webapp/serializers.py
"""JSON-safe conversion helpers shared by the web routes and (later) MCP tools."""
import json
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from enum import Enum
from typing import Any


def json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.name
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def dump_json(data: Any, path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=json_default)


def load_json(path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def format_czk(value: Any) -> str:
    """Czech number formatting: 12 345,67 (non-breaking thousands space)."""
    if value is None or value == "":
        return "–"
    try:
        dec = Decimal(str(value))
    except Exception:
        return str(value)
    formatted = f"{dec:,.2f}"
    return formatted.replace(",", " ").replace(".", ",")


def asset_url(static_dir: Path, name: str) -> str:
    """``/static/<name>?v=<mtime>`` — a link that changes when the file does.

    Templates reload live under a running server, the browser's copy of
    style.css does not: after a ``git pull`` the new markup would sit on the
    old stylesheet until a hard reload. The version is the file's mtime read
    at render time (one stat), so it follows a checkout with no restart. A
    missing file links with ``v=0`` rather than failing the whole page.
    """
    try:
        version = int(os.stat(Path(static_dir) / name).st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


def format_cs_date(value: Any) -> str:
    """ISO date → Czech "4. 9. 2026". None/empty → "–"; anything else as given,
    so a stray label shows rather than blanking the card."""
    if value is None or value == "":
        return "–"
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{d.day}. {d.month}. {d.year}"


def format_quantity(value: Any) -> str:
    """A share count without the FIFO tail: "10.00000000" -> "10".

    Fractional holdings are real (IBKR sells them, and a corporate action can
    leave one behind), so decimals are dropped only when they are ALL zero —
    the point is to remove eight zeros of noise, never to round a position away.

    ``format`` rather than a bare ``normalize()``, which renders
    ``Decimal("100")`` as "1E+2".
    """
    if value is None or value == "":
        return "–"
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except Exception:
        return str(value)
