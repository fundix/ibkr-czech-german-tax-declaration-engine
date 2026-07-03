# src/webapp/serializers.py
"""JSON-safe conversion helpers shared by the web routes and (later) MCP tools."""
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
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
