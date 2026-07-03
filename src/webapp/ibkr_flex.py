# src/webapp/ibkr_flex.py
"""
IBKR Flex Web Service client — automated download of Flex Query statements.

Protocol (v3): two GET requests.
1. ``SendRequest?t=<token>&q=<query_id>&v=3`` → XML with a ReferenceCode
   (statement generation starts server-side),
2. ``GetStatement?t=<token>&q=<reference_code>&v=3`` → the statement body
   (CSV, per the query's configured format). While generation is still
   running IBKR answers with an XML error (code 1019) — poll with backoff.

Configuration lives in ``data/webapp/ibkr_flex.json`` (gitignored — the
token is a secret; it also expires, max one year, so expiry errors must be
surfaced clearly):

    {"token": "...", "queries": {"trades": "123456", "cash": "...",
     "positions": "...", "corp_actions": "..."}}

The HTTP getter is injectable for tests.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_STATEMENT_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
_UA = {"User-Agent": "ibkr-tax-engine (local)"}

# Statement slots we know how to consume, in fetch order.
FLEX_SLOTS = ("trades", "cash", "positions", "corp_actions")

# Friendly messages for the documented Flex error codes.
_ERROR_HINTS = {
    "1012": "Token vypršel — vygenerujte v Client Portalu nový (Flex Web Service).",
    "1013": "IP adresa není povolená — zkontrolujte IP omezení tokenu.",
    "1015": "Neplatný token.",
    "1018": "Příliš mnoho požadavků — počkejte chvíli a zkuste znovu.",
    "1019": "Výpis se ještě generuje.",
    "1020": "Neplatné Query ID.",
}

_POLL_ATTEMPTS = 12
_POLL_DELAY_S = 5.0

# IBKR throttles bursts of requests per token (error 1018): pause between
# consecutive statement downloads and back off + retry when 1018 appears.
INTER_QUERY_DELAY_S = 10.0
_RATE_LIMIT_RETRIES = 5
_RATE_LIMIT_DELAY_S = 30.0


class FlexFetchError(RuntimeError):
    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


@dataclass
class FlexConfig:
    token: str = ""
    queries: Dict[str, str] = field(default_factory=dict)  # slot -> query id
    # Optional bootstrap queries with the "Last Calendar Year" period —
    # fetched once into the PREVIOUS year's dataset when it is missing, so
    # a fresh install fills the running + previous year purely via the API.
    prev_year_queries: Dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.token and any(self.queries.get(s) for s in FLEX_SLOTS))

    def masked_token(self) -> str:
        if len(self.token) <= 6:
            return "•" * len(self.token)
        return f"{self.token[:3]}…{self.token[-3:]}"


def load_flex_config(path: Path) -> FlexConfig:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return FlexConfig(
                token=str(data.get("token") or ""),
                queries={k: str(v) for k, v in (data.get("queries") or {}).items() if v},
                prev_year_queries={k: str(v) for k, v in
                                   (data.get("prev_year_queries") or {}).items() if v},
            )
    except Exception as exc:
        logger.warning(f"Unreadable ibkr_flex.json: {exc}")
    return FlexConfig()


def save_flex_config(path: Path, config: FlexConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"token": config.token, "queries": config.queries,
                    "prev_year_queries": config.prev_year_queries},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _http_get(url: str, params: Dict[str, str]) -> bytes:
    resp = requests.get(url, params=params, headers=_UA, timeout=30)
    resp.raise_for_status()
    return resp.content


def _parse_error(body: str) -> Optional[tuple]:
    """Return (code, message) when the body is a Flex XML error/status."""
    code = re.search(r"<ErrorCode>(\d+)</ErrorCode>", body)
    msg = re.search(r"<ErrorMessage>(.*?)</ErrorMessage>", body, re.S)
    if code:
        c = code.group(1)
        hint = _ERROR_HINTS.get(c, "")
        text = (msg.group(1).strip() if msg else "")
        return c, f"IBKR Flex chyba {c}: {text} {hint}".strip()
    return None


def fetch_statement(
    token: str,
    query_id: str,
    http_get: Callable[[str, Dict[str, str]], bytes] = _http_get,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Download one Flex Query statement (CSV bytes). Raises FlexFetchError.

    IBKR throttles per-token request bursts (error 1018) — both protocol
    steps treat 1018 as RETRYABLE with a longer backoff instead of failing
    the whole download.
    """
    ref = None
    for attempt in range(_RATE_LIMIT_RETRIES):
        body = http_get(SEND_REQUEST_URL, {"t": token, "q": query_id, "v": "3"}).decode(
            "utf-8", errors="replace"
        )
        err = _parse_error(body)
        if err and err[0] == "1018":  # rate limited — back off and retry
            logger.info(
                f"Flex query {query_id}: rate limited (attempt {attempt + 1}), "
                f"waiting {_RATE_LIMIT_DELAY_S:.0f} s…"
            )
            sleep(_RATE_LIMIT_DELAY_S)
            continue
        if err:
            raise FlexFetchError(err[1], code=err[0])
        match = re.search(r"<ReferenceCode>(\w+)</ReferenceCode>", body)
        if not match:
            raise FlexFetchError(f"Neočekávaná odpověď SendRequest: {body[:200]}")
        ref = match.group(1)
        break
    if ref is None:
        raise FlexFetchError(
            "IBKR stále hlásí příliš mnoho požadavků (1018) — zkuste to za pár minut.",
            code="1018",
        )

    for attempt in range(_POLL_ATTEMPTS):
        statement = http_get(GET_STATEMENT_URL, {"t": token, "q": ref, "v": "3"})
        head = statement[:500].decode("utf-8", errors="replace")
        if "<FlexStatementResponse" in head or "<ErrorCode>" in head:
            err = _parse_error(head)
            if err and err[0] in ("1019", "1018"):  # generating / rate limited
                delay = _RATE_LIMIT_DELAY_S if err[0] == "1018" else _POLL_DELAY_S
                logger.info(
                    f"Flex statement {query_id} not ready "
                    f"(code {err[0]}, attempt {attempt + 1}), waiting {delay:.0f} s…"
                )
                sleep(delay)
                continue
            raise FlexFetchError(
                err[1] if err else f"Neočekávaná odpověď GetStatement: {head[:200]}"
            )
        if not statement.strip():
            raise FlexFetchError("IBKR vrátil prázdný výpis.")
        return statement

    raise FlexFetchError(
        "Výpis se negeneroval ani po opakovaných pokusech — zkuste to později.",
        code="1019",
    )
