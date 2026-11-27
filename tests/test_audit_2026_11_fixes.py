# tests/test_audit_2026_11_fixes.py
"""
Regression tests for the findings of the 2026-11 audit that were fixed:

* W21 — per-year run lookups only saw the 100 newest runs across all years.
* V05 — a ČNB sheet requested for "today" before the afternoon fixing was
  cached under today's date for good, although it was the previous day's.
* W01 — a year's statements were only ever downloaded while it was running,
  so after New Year the closed year stayed cut off at its last download, and
  a download requested for it then fetched the NEW year's rows over it.

Everything here is synthetic: no real statement, rate or account is used.
"""
import json
import os
import shutil
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import src.config as config
from src.utils.cnb_exchange_rate_provider import CNBExchangeRateProvider
from src.webapp.ibkr_flex import FlexFetchError
from src.webapp.services import _FINGERPRINT_VERSION, RunService
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider

SYNTHETIC = Path(__file__).resolve().parent.parent / "data" / "synthetic_2024"

# synthetic file name -> canonical slot file name
SYNTHETIC_MAP = {
    "trades.csv": "trades.csv",
    "cash_transactions.csv": "cash_transactions.csv",
    "positions_start_of_year.csv": "positions_start.csv",
    "positions_end_of_year.csv": "positions_end.csv",
    "corporate_actions.csv": "corporate_actions.csv",
}


@pytest.fixture
def service(tmp_path, monkeypatch):
    # The engine keeps its asset classifications in one file under cache/.
    # A run here must not write the synthetic assets into the real one.
    monkeypatch.setattr(config, "CLASSIFICATION_CACHE_FILE_PATH",
                        str(tmp_path / "classifications.json"))
    svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    yield svc
    svc.runner.shutdown(wait=False)


def _settle_classifications(svc: RunService, tax_year: int = 2024) -> None:
    """Run the year once so the classification file exists.

    The first run of a dataset writes that file, and the file is part of the
    input fingerprint - so only from the second run on does a run match the
    fingerprint computed after it. Tests about reusing a run start here.
    """
    svc._execute_run(
        f"{tax_year}-first-ever", tax_year, "daily",
        ecb_provider=GoldenEcbProvider(),
        cz_fx_provider=GoldenCnbProvider(),
    )


def _seed_synthetic_year(svc: RunService, year: int = 2024) -> None:
    year_dir = svc.data_dir / str(year)
    year_dir.mkdir(parents=True)
    for src_name, dst_name in SYNTHETIC_MAP.items():
        shutil.copyfile(SYNTHETIC / src_name, year_dir / dst_name)


def _seed_run_meta(svc: RunService, run_id: str, tax_year: int,
                   created_at: str,
                   fingerprint_version: str = _FINGERPRINT_VERSION) -> None:
    """Persist a run's meta.json the way ``_execute_run`` writes it."""
    run_dir = svc.runs_dir / run_id
    run_dir.mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "tax_year": tax_year,
        "fx_mode": "daily",
        "pairing_method": "fifo",
        "modes": ["daily"],
        "created_at": created_at,
        "duration_s": 1.0,
        "eoy_mismatch_error_count": 0,
        "summary": {"daily": {"combined_base_czk": "0.00",
                              "final_tax_czk": "0.00",
                              "pending_review_count": 0}},
        "compare_lines": [],
        "notes": [],
        "account_ids": ["U1234567"],
        "input_fingerprint": f"fp-{run_id}",
        "fingerprint_version": fingerprint_version,
    }
    if fingerprint_version is None:
        del meta["fingerprint_version"]   # a run stored before the stamp existed
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _local_day(iso_instant: str) -> str:
    """The calendar day of an instant as the app prints it ("30. 12. 2026").

    A run's creation time is stored in UTC and shown in local time, so the
    day it falls on depends on the machine's zone; no instant is the same
    day everywhere.
    """
    local = datetime.fromisoformat(iso_instant).astimezone()
    return f"{local.day}. {local.month}. {local.year}"


def _seed_many_newer_runs(svc: RunService, tax_year: int, count: int) -> None:
    for i in range(count):
        _seed_run_meta(svc, f"{tax_year}-newer-{i:03d}", tax_year,
                       f"2099-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00")


# ---------------------------------------------------------------------------
# W21 — a year with many runs must not hide the other years
# ---------------------------------------------------------------------------

class TestRunLookupsSeeEveryRun:
    """The running year is recomputed almost daily, so it soon holds more than
    a hundred runs. Lookups that sliced the listing to the 100 newest before
    filtering by year then lost every closed year: no dashboard card, no
    result for the MCP year tools, and a recompute instead of a cache hit.
    """

    def test_latest_run_of_a_year_is_found_behind_101_newer_runs(self, service):
        _seed_run_meta(service, "2025-only", 2025, "2026-03-01T10:00:00+00:00")
        _seed_many_newer_runs(service, 2026, 101)

        assert service.latest_run_id(2025) == "2025-only"

    def test_dashboard_keeps_a_card_for_a_year_behind_101_newer_runs(self, service):
        _seed_run_meta(service, "2025-only", 2025, "2026-03-01T10:00:00+00:00")
        _seed_many_newer_runs(service, 2026, 101)

        overview = service.dashboard_overview()

        assert [c["tax_year"] for c in overview["year_cards"]] == [2026, 2025]
        assert overview["latest"]["run_id"] == "2026-newer-100"

    def test_identical_inputs_reuse_a_run_behind_101_newer_runs(self, service):
        _seed_synthetic_year(service)
        _settle_classifications(service)
        service._execute_run(
            "2024-cache", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )
        _seed_many_newer_runs(service, 2026, 101)

        assert service.find_cached_run(2024, "daily", "fifo") == "2024-cache"

    def test_the_run_list_page_still_shows_only_the_newest_runs(self, service):
        _seed_many_newer_runs(service, 2026, 25)

        listed = service.list_runs()

        assert len(listed) == 20
        assert listed[0]["run_id"] == "2026-newer-024"


# ---------------------------------------------------------------------------
# V05 — a ČNB sheet is cached only under a date it is the sheet of
# ---------------------------------------------------------------------------

def _sheet(header: date, usd: str, eur: str) -> str:
    """A ČNB daily sheet as the text endpoint serves it (synthetic rates)."""
    return (
        f"{header:%d.%m.%Y} #1\n"
        "země|měna|množství|kód|kurz\n"
        f"EMU|euro|1|EUR|{eur}\n"
        f"USA|dolar|1|USD|{usd}\n"
    )


class _ScriptedCnb(CNBExchangeRateProvider):
    """ČNB as it answers on one fixed day: a canned sheet per query date.

    ``today`` is the Prague calendar date the provider believes it is —
    the afternoon fixing of that day is what may or may not be out yet.
    """

    def __init__(self, responses, today, cache_file_path, **kwargs):
        self._responses = responses
        self._today = today
        self.fetch_log = []
        super().__init__(cache_file_path=str(cache_file_path), **kwargs)

    def _fetch_rates_for_date(self, query_date):
        self.fetch_log.append(query_date)
        text = self._responses.get(query_date)
        return None if text is None else self._parse_cnb_text(text, query_date)

    def _publishing_today(self):
        return self._today


THU = date(2026, 11, 19)
FRI = date(2026, 11, 20)
SUN = date(2026, 11, 15)
PREV_FRI = date(2026, 11, 13)

# What ČNB printed on each sheet (CZK per 1 unit), and the provider's
# normalised form of it: foreign units per 1 CZK.
THU_SHEET = _sheet(THU, usd="21,000", eur="24,000")
FRI_SHEET = _sheet(FRI, usd="22,000", eur="25,000")
PREV_FRI_SHEET = _sheet(PREV_FRI, usd="20,000", eur="23,000")
USD_THU = Decimal(1) / Decimal("21.000")
USD_FRI = Decimal(1) / Decimal("22.000")
USD_PREV_FRI = Decimal(1) / Decimal("20.000")


def _cache_on_disk(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class TestCnbSheetIsCachedOnlyUnderItsOwnDay:
    """ČNB publishes a day's sheet around 14:30. Asked for today before that,
    it answers with the previous sheet — and the provider used to file that
    under today's date, where every later run found it as a cache hit.
    """

    def test_before_the_fixing_today_gets_yesterdays_rate_with_its_real_date(
            self, tmp_path):
        provider = _ScriptedCnb({FRI: THU_SHEET}, today=FRI,
                                cache_file_path=tmp_path / "cnb.json")

        assert provider.get_rate_info(FRI, "USD") == (USD_THU, THU)

    def test_before_the_fixing_nothing_is_cached_under_todays_date(self, tmp_path):
        cache = tmp_path / "cnb.json"
        provider = _ScriptedCnb({FRI: THU_SHEET}, today=FRI, cache_file_path=cache)

        provider.get_rate_info(FRI, "USD")

        assert "2026-11-20" not in _cache_on_disk(cache)

    def test_the_real_sheet_is_used_once_it_is_published(self, tmp_path):
        cache = tmp_path / "cnb.json"
        morning = _ScriptedCnb({FRI: THU_SHEET}, today=FRI, cache_file_path=cache)
        morning.get_rate_info(FRI, "USD")

        afternoon = _ScriptedCnb({FRI: FRI_SHEET}, today=FRI, cache_file_path=cache)

        assert afternoon.get_rate_info(FRI, "USD") == (USD_FRI, FRI)

    def test_the_early_sheet_is_kept_under_the_day_it_belongs_to(self, tmp_path):
        cache = tmp_path / "cnb.json"
        morning = _ScriptedCnb({FRI: THU_SHEET}, today=FRI, cache_file_path=cache)
        morning.get_rate_info(FRI, "USD")

        offline = _ScriptedCnb({}, today=FRI, cache_file_path=cache)

        assert offline.get_rate_info(THU, "USD") == (USD_THU, THU)
        assert offline.fetch_log == []

    def test_before_the_fixing_cnb_is_asked_once_however_much_is_converted(
            self, tmp_path):
        provider = _ScriptedCnb({FRI: THU_SHEET}, today=FRI,
                                cache_file_path=tmp_path / "cnb.json")

        # A portfolio page converts every position at today's rate.
        provider.get_rate_info(FRI, "USD")
        provider.get_rate_info(FRI, "EUR")
        provider.get_rate_info(FRI, "USD")

        assert provider.fetch_log == [FRI]

    def test_a_sheet_remembered_in_the_morning_is_not_filed_after_midnight(
            self, tmp_path):
        cache = tmp_path / "cnb.json"
        provider = _ScriptedCnb({FRI: THU_SHEET}, today=FRI, cache_file_path=cache)
        assert provider.get_rate_info(FRI, "USD") == (USD_THU, THU)

        # The same provider is still alive on Saturday; Friday's own sheet
        # has been out since Friday afternoon.
        provider._today = date(2026, 11, 21)
        provider._responses[FRI] = FRI_SHEET

        assert provider.get_rate_info(FRI, "USD") == (USD_FRI, FRI)
        assert Decimal(_cache_on_disk(cache)["2026-11-20"]["USD"]) == USD_FRI

    def test_today_is_the_calendar_day_in_prague(self, tmp_path):
        provider = CNBExchangeRateProvider(cache_file_path=str(tmp_path / "cnb.json"))
        late_evening_utc = datetime(2026, 11, 27, 23, 30, tzinfo=timezone.utc)

        # 23:30 UTC is already half past midnight in Prague.
        assert provider._publishing_today(now=late_evening_utc) == date(2026, 11, 28)

    def test_an_early_sheet_outside_the_fallback_window_is_not_used(self, tmp_path):
        provider = _ScriptedCnb({FRI: THU_SHEET}, today=FRI,
                                cache_file_path=tmp_path / "cnb.json",
                                max_fallback_days_override=0)

        # No lookback allowed: a sheet one day old is not Friday's rate.
        assert provider.get_rate_info(FRI, "USD") is None

    def test_a_weekend_date_still_caches_the_last_sheet_before_it(self, tmp_path):
        provider = _ScriptedCnb({SUN: PREV_FRI_SHEET}, today=FRI,
                                cache_file_path=tmp_path / "cnb.json")

        first = provider.get_rate_info(SUN, "USD")
        second = provider.get_rate_info(SUN, "USD")

        assert first == second == (USD_PREV_FRI, SUN)
        assert provider.fetch_log == [SUN]

    def test_a_sheet_with_an_unreadable_header_is_not_cached_for_today(
            self, tmp_path):
        cache = tmp_path / "cnb.json"
        headless = "???\n" + FRI_SHEET.split("\n", 1)[1]
        provider = _ScriptedCnb({FRI: headless, THU: THU_SHEET}, today=FRI,
                                cache_file_path=cache)

        # Nothing proves the headless sheet is Friday's, so Thursday's own
        # sheet — a past day, final — is what the lookup falls back to.
        assert provider.get_rate_info(FRI, "USD") == (USD_THU, THU)
        assert "2026-11-20" not in _cache_on_disk(cache)

    def test_prefetch_does_not_file_yesterdays_sheet_under_today(self, tmp_path):
        cache = tmp_path / "cnb.json"
        provider = _ScriptedCnb({THU: THU_SHEET, FRI: THU_SHEET}, today=FRI,
                                cache_file_path=cache)

        provider.prefetch_rates(THU, FRI, {"USD"})

        stored = _cache_on_disk(cache)
        assert "2026-11-20" not in stored
        assert Decimal(stored["2026-11-19"]["USD"]) == USD_THU


class TestCnbEntriesFromBeforeTheFixAreRechecked:
    """A cache written by the old code cannot say which sheet an entry holds,
    so an entry may be the previous day's sheet. Each such entry is compared
    with ČNB the first time it is used — the past is final there — and from
    then on it is trusted like any other.
    """

    def _legacy_cache(self, tmp_path, entries: dict):
        cache = tmp_path / "cnb.json"
        cache.write_text(json.dumps(entries), encoding="utf-8")
        return cache

    def test_an_entry_holding_the_previous_days_sheet_is_corrected(self, tmp_path):
        # Filed under Thursday while ČNB still served Wednesday's sheet.
        cache = self._legacy_cache(
            tmp_path, {"2026-11-19": {"USD": str(USD_PREV_FRI), "EUR": "0.04"}})
        provider = _ScriptedCnb({THU: THU_SHEET}, today=FRI, cache_file_path=cache)

        assert provider.get_rate_info(THU, "USD") == (USD_THU, THU)
        assert Decimal(_cache_on_disk(cache)["2026-11-19"]["USD"]) == USD_THU

    def test_a_rechecked_entry_is_not_fetched_again(self, tmp_path):
        cache = self._legacy_cache(
            tmp_path, {"2026-11-19": {"USD": str(USD_PREV_FRI)}})
        _ScriptedCnb({THU: THU_SHEET}, today=FRI,
                     cache_file_path=cache).get_rate_info(THU, "USD")

        later = _ScriptedCnb({}, today=FRI, cache_file_path=cache)

        assert later.get_rate_info(THU, "USD") == (USD_THU, THU)
        assert later.fetch_log == []

    def test_an_old_entry_is_still_used_when_cnb_cannot_be_reached(self, tmp_path):
        cache = self._legacy_cache(
            tmp_path, {"2026-11-19": {"USD": str(USD_THU)}})
        provider = _ScriptedCnb({}, today=FRI, cache_file_path=cache)

        assert provider.get_rate_info(THU, "USD") == (USD_THU, THU)

    def test_an_unreachable_cnb_is_asked_only_once_per_provider(self, tmp_path):
        cache = self._legacy_cache(tmp_path, {
            "2026-11-19": {"USD": str(USD_THU)},
            "2026-11-13": {"USD": str(USD_PREV_FRI)},
        })
        provider = _ScriptedCnb({}, today=FRI, cache_file_path=cache)

        provider.get_rate_info(THU, "USD")
        provider.get_rate_info(PREV_FRI, "USD")

        assert provider.fetch_log == [THU]

    def test_dropping_an_old_entry_for_today_costs_one_request(self, tmp_path):
        cache = self._legacy_cache(
            tmp_path, {"2026-11-20": {"USD": str(USD_THU)}})
        provider = _ScriptedCnb({FRI: THU_SHEET}, today=FRI, cache_file_path=cache)

        provider.get_rate_info(FRI, "USD")

        assert provider.fetch_log == [FRI]

    def test_an_old_entry_is_kept_when_cnbs_answer_carries_no_date(self, tmp_path):
        cache = self._legacy_cache(
            tmp_path, {"2026-11-19": {"USD": str(USD_PREV_FRI)}})
        headless = "???\n" + THU_SHEET.split("\n", 1)[1]
        provider = _ScriptedCnb({THU: headless}, today=FRI, cache_file_path=cache)

        # An answer that cannot say which day it is proves nothing either way.
        assert provider.get_rate_info(THU, "USD") == (USD_PREV_FRI, THU)
        assert provider.get_rate_info(THU, "USD") == (USD_PREV_FRI, THU)
        assert provider.fetch_log == [THU]

    def test_an_entry_of_failure_markers_does_not_stop_the_rechecks(self, tmp_path):
        cache = self._legacy_cache(tmp_path, {
            "2026-11-19": {"USD": None},
            "2026-11-13": {"USD": str(USD_THU)},
        })
        provider = _ScriptedCnb({PREV_FRI: PREV_FRI_SHEET}, today=FRI,
                                cache_file_path=cache)

        provider.get_rate_info(THU, "USD")   # nothing there to compare

        assert provider.get_rate_info(PREV_FRI, "USD") == (USD_PREV_FRI, PREV_FRI)

    def test_a_rate_that_could_not_be_checked_is_reported(self, tmp_path):
        cache = self._legacy_cache(
            tmp_path, {"2026-11-19": {"USD": str(USD_THU)}})
        provider = _ScriptedCnb({}, today=FRI, cache_file_path=cache)

        provider.get_rate_info(THU, "USD")

        assert provider.unverified_dates_used == {"2026-11-19"}

    def test_a_checked_rate_is_not_reported_as_unverified(self, tmp_path):
        cache = self._legacy_cache(
            tmp_path, {"2026-11-19": {"USD": str(USD_PREV_FRI)}})
        provider = _ScriptedCnb({THU: THU_SHEET}, today=FRI, cache_file_path=cache)

        provider.get_rate_info(THU, "USD")

        assert provider.unverified_dates_used == set()

    def test_an_old_entry_for_today_is_dropped_before_the_fixing(self, tmp_path):
        # Written this morning by the old code: Thursday's sheet as "Friday".
        cache = self._legacy_cache(
            tmp_path, {"2026-11-20": {"USD": str(USD_THU)}})
        provider = _ScriptedCnb({FRI: THU_SHEET}, today=FRI, cache_file_path=cache)

        assert provider.get_rate_info(FRI, "USD") == (USD_THU, THU)
        assert "2026-11-20" not in _cache_on_disk(cache)


# ---------------------------------------------------------------------------
# W01 — a closed year is downloaded for its whole calendar year
# ---------------------------------------------------------------------------

FLEX_QUERIES = {"trades": "11", "cash": "22", "positions": "33"}
FLEX_FILES = ("trades.csv", "cash_transactions.csv", "positions_end.csv")


def _statement(svc: RunService, year: int, name: str, written: datetime,
               content: bytes = b"old\n") -> Path:
    """A stored statement file that was downloaded at ``written``."""
    path = svc.data_dir / str(year) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (written.timestamp(), written.timestamp()))
    return path


def _year_of_statements(svc: RunService, year: int, written: datetime) -> None:
    for name in FLEX_FILES:
        _statement(svc, year, name, written)


class _FakeIbkr:
    """Records every statement request and answers with its own parameters."""

    def __init__(self, failing_window_end: str = "", failing_query: str = ""):
        self.calls = []
        self._failing_window_end = failing_window_end
        self._failing_query = failing_query

    def __call__(self, token, query_id, from_date=None, to_date=None, report=None):
        if (to_date and to_date == self._failing_window_end
                and self._failing_query in ("", query_id)):
            raise FlexFetchError("statement not available", code="1003")
        self.calls.append((query_id, from_date, to_date))
        return f"q{query_id}:{from_date}-{to_date}\n".encode()


@pytest.fixture
def flex_service(service, monkeypatch):
    """A service with IBKR Flex set up; the engine run itself is stubbed out."""
    service.save_flex_settings("tok", dict(FLEX_QUERIES))
    captured = {}
    monkeypatch.setattr(
        service, "_execute_run",
        lambda run_id, year, fx_mode, **kw: captured.update(kw) or {"run_id": run_id},
    )
    service.captured_run_kwargs = captured
    return service


def _fetch(svc: RunService, tax_year: int, today: date, ibkr: _FakeIbkr,
           monkeypatch) -> dict:
    monkeypatch.setattr(svc, "_today", lambda: today)
    return svc._fetch_and_run(f"{tax_year}-x", tax_year, "daily",
                              fetch=ibkr, pause=lambda _s: None)


class TestClosedYearIsDownloadedInFull:
    """The Flex queries are saved as Year to Date. That is the right period
    while a year runs and the wrong one the moment it ends: its last days
    were never downloaded, and "this year" at IBKR is by then the next one.
    """

    def test_a_closed_year_is_requested_for_its_calendar_year(
            self, flex_service, monkeypatch):
        ibkr = _FakeIbkr()

        _fetch(flex_service, 2026, date(2027, 1, 20), ibkr, monkeypatch)

        assert ibkr.calls == [(q, "20260101", "20261231") for q in ("11", "22", "33")]

    def test_the_year_just_closed_is_downloaded_again_with_the_new_year(
            self, flex_service, monkeypatch):
        _year_of_statements(flex_service, 2026, written=datetime(2026, 12, 29, 22, 0))
        ibkr = _FakeIbkr()

        _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        assert ibkr.calls == (
            [(q, "20260101", "20261231") for q in ("11", "22", "33")]
            + [(q, None, None) for q in ("11", "22", "33")]
        )
        assert (flex_service.data_dir / "2026" / "trades.csv").read_bytes() \
            == b"q11:20260101-20261231\n"

    def test_the_run_says_which_closed_year_was_downloaded_again(
            self, flex_service, monkeypatch):
        _year_of_statements(flex_service, 2026, written=datetime(2026, 12, 29, 22, 0))

        meta = _fetch(flex_service, 2027, date(2027, 1, 20), _FakeIbkr(), monkeypatch)

        assert meta["refreshed_years"] == [2026]
        assert "bootstrapped_years" not in meta     # nothing was missing
        notes = flex_service.captured_run_kwargs["extra_notes"]
        assert any("2026" in note and "31. 12." in note for note in notes)

    def test_the_replaced_statement_is_kept_beside_the_new_one(
            self, flex_service, monkeypatch):
        _year_of_statements(flex_service, 2026, written=datetime(2026, 12, 29, 22, 0))

        _fetch(flex_service, 2027, date(2027, 1, 20), _FakeIbkr(), monkeypatch)

        backups = sorted((flex_service.data_dir / "2026").glob("trades.csv.*.bak"))
        assert [b.read_bytes() for b in backups] == [b"old\n"]

    def test_a_second_replacement_does_not_cost_the_first_backup(
            self, flex_service, monkeypatch):
        # Downloaded twice in the first week of January, then once more: the
        # statement the year was computed from in December must survive all.
        _year_of_statements(flex_service, 2026, written=datetime(2026, 12, 29, 22, 0))
        _fetch(flex_service, 2027, date(2027, 1, 20), _FakeIbkr(), monkeypatch)
        for name in FLEX_FILES:   # as if that download had been made on 3 Jan
            path = flex_service.data_dir / "2026" / name
            early = datetime(2027, 1, 3, 9, 0).timestamp()
            os.utime(path, (early, early))

        _fetch(flex_service, 2027, date(2027, 1, 20), _FakeIbkr(), monkeypatch)

        backups = sorted((flex_service.data_dir / "2026").glob("trades.csv.*.bak"))
        assert b"old\n" in [b.read_bytes() for b in backups]
        assert len(backups) == 2

    def test_a_download_from_the_first_days_of_january_is_repeated(
            self, flex_service, monkeypatch):
        # IBKR had not finished the last trading day when this was fetched.
        _year_of_statements(flex_service, 2026, written=datetime(2027, 1, 3, 9, 0))
        ibkr = _FakeIbkr()

        _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        assert ("11", "20260101", "20261231") in ibkr.calls

    def test_a_year_downloaded_after_it_settled_is_left_alone(
            self, flex_service, monkeypatch):
        _year_of_statements(flex_service, 2025, written=datetime(2026, 7, 3, 22, 40))
        _year_of_statements(flex_service, 2026, written=datetime(2027, 1, 15, 8, 0))
        ibkr = _FakeIbkr()

        _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        assert ibkr.calls == [(q, None, None) for q in ("11", "22", "33")]
        assert (flex_service.data_dir / "2026" / "trades.csv").read_bytes() == b"old\n"

    def test_a_year_before_the_first_ibkr_year_is_never_downloaded_over(
            self, flex_service, monkeypatch):
        flex_service.save_flex_settings("tok", dict(FLEX_QUERIES), first_year="2026")
        _year_of_statements(flex_service, 2025, written=datetime(2025, 12, 20, 9, 0))
        _year_of_statements(flex_service, 2026, written=datetime(2027, 1, 15, 8, 0))
        ibkr = _FakeIbkr()

        _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        assert not any(to_date == "20251231" for _q, _fd, to_date in ibkr.calls)
        assert (flex_service.data_dir / "2025" / "trades.csv").read_bytes() == b"old\n"

    def test_a_closed_year_ibkr_cannot_deliver_keeps_its_statements(
            self, flex_service, monkeypatch):
        _year_of_statements(flex_service, 2026, written=datetime(2026, 12, 29, 22, 0))
        ibkr = _FakeIbkr(failing_window_end="20261231")

        meta = _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        assert (flex_service.data_dir / "2026" / "trades.csv").read_bytes() == b"old\n"
        assert "refreshed_years" not in meta
        notes = flex_service.captured_run_kwargs["extra_notes"]
        assert any("uzavřeného roku 2026" in note and "selhalo" in note
                   for note in notes)
        # The new year's own download still went through.
        assert ibkr.calls == [(q, None, None) for q in ("11", "22", "33")]

    def test_a_closed_year_is_replaced_whole_or_not_at_all(
            self, flex_service, monkeypatch):
        # Full-year trades next to December's positions would be a dataset
        # that never existed at IBKR; better the consistent older one.
        _year_of_statements(flex_service, 2026, written=datetime(2026, 12, 29, 22, 0))
        ibkr = _FakeIbkr(failing_window_end="20261231", failing_query="22")

        meta = _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        year_dir = flex_service.data_dir / "2026"
        assert [(year_dir / name).read_bytes() for name in FLEX_FILES] \
            == [b"old\n"] * 3
        assert list(year_dir.glob("*.bak")) == []
        assert "refreshed_years" not in meta

    def test_a_statement_ibkr_will_not_deliver_does_not_block_the_refresh(
            self, flex_service, monkeypatch):
        # 2026 has December's trades and positions and never had a cash
        # statement; IBKR keeps refusing that one. The two that CAN be
        # replaced by complete ones must not wait for it for ever.
        flex_service.save_flex_settings("tok", dict(FLEX_QUERIES), first_year="2026")
        for name in ("trades.csv", "positions_end.csv"):
            _statement(flex_service, 2026, name, datetime(2026, 12, 29, 22, 0))
        ibkr = _FakeIbkr(failing_window_end="20261231", failing_query="22")

        meta = _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        year_dir = flex_service.data_dir / "2026"
        assert (year_dir / "trades.csv").read_bytes() == b"q11:20260101-20261231\n"
        assert (year_dir / "positions_end.csv").read_bytes() \
            == b"q33:20260101-20261231\n"
        assert not (year_dir / "cash_transactions.csv").exists()
        assert meta["refreshed_years"] == [2026]
        notes = flex_service.captured_run_kwargs["extra_notes"]
        assert any("Doplnění roku 2026" in note and "selhalo" in note
                   for note in notes)

    def test_a_failed_refresh_does_not_stop_the_missing_statements(
            self, flex_service, monkeypatch):
        # The other way round: IBKR refuses the trades of the closed year,
        # but the cash statement that was never there can still be fetched.
        flex_service.save_flex_settings("tok", dict(FLEX_QUERIES), first_year="2026")
        for name in ("trades.csv", "positions_end.csv"):
            _statement(flex_service, 2026, name, datetime(2026, 12, 29, 22, 0))
        ibkr = _FakeIbkr(failing_window_end="20261231", failing_query="11")

        meta = _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        year_dir = flex_service.data_dir / "2026"
        assert (year_dir / "trades.csv").read_bytes() == b"old\n"
        assert (year_dir / "cash_transactions.csv").read_bytes() \
            == b"q22:20260101-20261231\n"
        assert meta["bootstrapped_years"] == [2026]
        assert "refreshed_years" not in meta
        notes = flex_service.captured_run_kwargs["extra_notes"]
        assert any("uzavřeného roku 2026" in note and "selhalo" in note
                   for note in notes)

    def test_progress_counts_every_statement_of_a_year_fetched_in_two_parts(
            self, flex_service, monkeypatch):
        flex_service.save_flex_settings("tok", dict(FLEX_QUERIES), first_year="2026")
        for name in ("trades.csv", "positions_end.csv"):
            _statement(flex_service, 2026, name, datetime(2026, 12, 29, 22, 0))
        monkeypatch.setattr(flex_service, "_today", lambda: date(2027, 1, 20))
        seen = []

        flex_service._fetch_and_run("2027-x", 2027, "daily", fetch=_FakeIbkr(),
                                    pause=lambda _s: None,
                                    report=lambda **kw: seen.append(kw))

        steps = [kw["step"] for kw in seen if "step" in kw]
        # 3 statements of 2026 (2 replaced + 1 missing) + 3 of 2027 + the run
        assert {kw["total"] for kw in seen if "total" in kw} == {7}
        assert steps == sorted(steps)
        assert steps[-1] == 7
        assert max(b - a for a, b in zip(steps, steps[1:])) <= 1

    def test_a_requested_closed_year_is_replaced_whole_or_not_at_all(
            self, flex_service, monkeypatch):
        _year_of_statements(flex_service, 2026, written=datetime(2027, 1, 15, 8, 0))
        ibkr = _FakeIbkr(failing_window_end="20261231", failing_query="22")

        with pytest.raises(FlexFetchError):
            _fetch(flex_service, 2026, date(2027, 1, 20), ibkr, monkeypatch)

        year_dir = flex_service.data_dir / "2026"
        assert [(year_dir / name).read_bytes() for name in FLEX_FILES] \
            == [b"old\n"] * 3
        assert list(year_dir.glob("*.bak")) == []

    def test_without_a_first_year_only_last_year_is_downloaded_again(
            self, flex_service, monkeypatch):
        # How far back IBKR reaches is unknown here; an old year uploaded by
        # hand must not be asked for - and fail - on every download.
        _year_of_statements(flex_service, 2024, written=datetime(2024, 12, 20, 9, 0))
        _year_of_statements(flex_service, 2026, written=datetime(2026, 12, 29, 22, 0))
        ibkr = _FakeIbkr()

        _fetch(flex_service, 2027, date(2027, 1, 20), ibkr, monkeypatch)

        windows = {to_date for _q, _fd, to_date in ibkr.calls}
        assert windows == {"20261231", None}


class TestClosedYearFromAnInYearDownloadIsFlagged:
    """Without a download after New Year nothing repairs the closed year, and
    its result looks complete: the positions snapshot is as stale as the
    trades, so the two reconcile. The run has to say so itself.
    """

    def _run(self, service, monkeypatch, today: date) -> dict:
        monkeypatch.setattr(service, "_today", lambda: today)
        return service._execute_run(
            "2024-stale", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )

    def _touch_year(self, service, written: datetime) -> None:
        for path in (service.data_dir / "2024").iterdir():
            os.utime(path, (written.timestamp(), written.timestamp()))

    def test_statements_written_before_year_end_put_a_warning_on_the_run(
            self, service, monkeypatch):
        _seed_synthetic_year(service)
        self._touch_year(service, datetime(2024, 12, 20, 18, 0))

        meta = self._run(service, monkeypatch, today=date(2025, 2, 10))

        assert meta["unsettled_statements_from"] == "2024-12-20"
        assert any("20. 12. 2024" in note and "před koncem roku" in note
                   for note in meta["notes"])

    def test_statements_written_after_the_year_settled_carry_no_warning(
            self, service, monkeypatch):
        _seed_synthetic_year(service)
        self._touch_year(service, datetime(2025, 1, 20, 18, 0))

        meta = self._run(service, monkeypatch, today=date(2025, 2, 10))

        assert "unsettled_statements_from" not in meta
        assert not any("stáhněte rok" in note for note in meta["notes"])

    def test_the_running_year_carries_no_warning(self, service, monkeypatch):
        _seed_synthetic_year(service)
        self._touch_year(service, datetime(2024, 6, 20, 18, 0))

        meta = self._run(service, monkeypatch, today=date(2024, 6, 21))

        assert "unsettled_statements_from" not in meta
        assert not any("stáhněte rok" in note for note in meta["notes"])

    def test_a_run_cached_while_the_year_ran_is_not_reused_after_it_closed(
            self, service, monkeypatch):
        _seed_synthetic_year(service)
        _settle_classifications(service)
        self._touch_year(service, datetime(2024, 12, 20, 18, 0))
        self._run(service, monkeypatch, today=date(2024, 12, 21))
        assert service.find_cached_run(2024, "daily", "fifo") == "2024-stale"

        # Same files, but the year is over now: the cached result carries no
        # warning, so handing it back would hide that the data is cut short.
        monkeypatch.setattr(service, "_today", lambda: date(2025, 2, 10))

        assert service.find_cached_run(2024, "daily", "fifo") is None

    def test_the_warning_dates_from_the_earliest_statement(self, service, monkeypatch):
        _seed_synthetic_year(service)
        self._touch_year(service, datetime(2025, 1, 3, 9, 0))
        early = datetime(2024, 12, 20, 18, 0).timestamp()
        os.utime(service.data_dir / "2024" / "positions_end.csv", (early, early))

        meta = self._run(service, monkeypatch, today=date(2025, 2, 10))

        assert meta["unsettled_statements_from"] == "2024-12-20"
        assert any("před koncem roku" in note for note in meta["notes"])

    def test_a_download_in_the_first_days_of_january_is_flagged_too(
            self, service, monkeypatch):
        _seed_synthetic_year(service)
        self._touch_year(service, datetime(2025, 1, 3, 9, 0))

        meta = self._run(service, monkeypatch, today=date(2025, 2, 10))

        assert meta["unsettled_statements_from"] == "2025-01-03"
        assert any("hned po konci roku" in note and "8. 1. 2025" in note
                   for note in meta["notes"])

    def test_the_warning_names_the_statements_it_is_about(self, service, monkeypatch):
        _seed_synthetic_year(service)
        self._touch_year(service, datetime(2025, 1, 20, 18, 0))
        early = datetime(2024, 12, 20, 18, 0).timestamp()
        os.utime(service.data_dir / "2024" / "positions_end.csv", (early, early))

        meta = self._run(service, monkeypatch, today=date(2025, 2, 10))

        assert any(note.startswith("Výpisy roku 2024 (pozice) byly staženy")
                   for note in meta["notes"])


class TestRunComputedWhileItsYearWasOpenIsFlaggedWhenRead:
    """A run stays the "latest" of its year until someone recomputes it. The
    one computed on 30 December is what the dashboard card and the MCP year
    tools serve all through filing season - even after the statements were
    downloaded again - so it has to say what it is whenever it is read.
    """

    def test_a_december_run_carries_a_warning_once_the_year_is_over(
            self, service, monkeypatch):
        _seed_run_meta(service, "2026-dec", 2026, "2026-12-30T12:00:00+00:00")
        monkeypatch.setattr(service, "_today", lambda: date(2027, 2, 10))

        meta = service.get_run("2026-dec")

        assert meta["computed_before_year_settled"] is True
        made = _local_day("2026-12-30T12:00:00+00:00")
        assert any(made in note and "přepočítejte" in note
                   for note in meta["notes"])

    def test_the_dashboard_card_of_that_year_carries_the_flag(
            self, service, monkeypatch):
        _seed_run_meta(service, "2026-dec", 2026, "2026-12-30T12:00:00+00:00")
        monkeypatch.setattr(service, "_today", lambda: date(2027, 2, 10))

        card = service.dashboard_overview()["year_cards"][0]

        assert card["computed_before_year_settled"] is True

    def test_a_run_made_after_the_year_settled_is_left_as_it_is(
            self, service, monkeypatch):
        _seed_run_meta(service, "2026-feb", 2026, "2027-02-01T09:00:00+00:00")
        monkeypatch.setattr(service, "_today", lambda: date(2027, 2, 10))

        meta = service.get_run("2026-feb")

        assert "computed_before_year_settled" not in meta
        assert meta["notes"] == []

    def test_a_run_of_the_running_year_is_left_as_it_is(self, service, monkeypatch):
        _seed_run_meta(service, "2026-nov", 2026, "2026-11-20T09:00:00+00:00")
        monkeypatch.setattr(service, "_today", lambda: date(2026, 11, 27))

        assert "computed_before_year_settled" not in service.get_run("2026-nov")


class TestRunOnUnverifiedRatesIsNotReused:
    """With CNB unreachable, cache entries written by the old code are used
    unchecked - any of them may be the previous day's sheet. Such a run is
    still better than none, but it must not be handed back later as if it
    had been computed from checked rates.
    """

    class _UnverifiedGolden(GoldenCnbProvider):
        unverified_dates_used = {"2024-03-05"}

    def _run(self, service, fx_mode: str) -> dict:
        _seed_synthetic_year(service)
        _settle_classifications(service)   # or nothing would be reused anyway
        return service._execute_run(
            "2024-unverified", 2024, fx_mode,
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=self._UnverifiedGolden(),
        )

    def test_the_run_says_which_rates_could_not_be_checked(self, service):
        meta = self._run(service, "daily")

        assert meta["unverified_rate_dates"] == ["2024-03-05"]
        assert any("nepodařilo ověřit" in note for note in meta["notes"])

    def test_the_run_is_not_served_from_the_compute_cache(self, service):
        meta = self._run(service, "daily")

        assert service.find_cached_run(2024, "daily", "fifo") is None
        # It still records what it was computed from, for comparing runs.
        assert len(meta["input_fingerprint"]) == 64

    def test_a_run_that_builds_its_own_provider_asks_it_as_well(
            self, service, monkeypatch):
        # No provider injected: this is how every real run is made.
        from src.countries.cz.config import CzTaxConfig

        asked = []

        def fake_factory(name, **kwargs):
            asked.append((name, kwargs))
            return self._UnverifiedGolden()

        monkeypatch.setattr("src.utils.fx_provider_factory.create_fx_provider",
                            fake_factory)
        _seed_synthetic_year(service)
        _settle_classifications(service)

        meta = service._execute_run("2024-own-provider", 2024, "daily",
                                    ecb_provider=GoldenEcbProvider())

        assert asked == [("cnb", {"cache_file_path": CzTaxConfig().cnb_cache_file_path})]
        assert meta["unverified_rate_dates"] == ["2024-03-05"]
        assert service.find_cached_run(2024, "daily", "fifo") is None

    def test_rates_behind_the_portfolio_snapshot_count_as_well(self, tmp_path,
                                                               monkeypatch):
        # Lots bought in earlier years are converted only for the snapshot,
        # by the service's own converter - not by the tax run's provider.
        from src.countries.cz.config import CzTaxConfig
        from src.countries.cz.fx_policy import CzCurrencyConverter

        monkeypatch.setattr(config, "CLASSIFICATION_CACHE_FILE_PATH",
                            str(tmp_path / "classifications.json"))
        svc = RunService(
            data_dir=tmp_path / "data", runs_dir=tmp_path / "runs",
            converter_factory=lambda: CzCurrencyConverter(
                self._UnverifiedGolden(), CzTaxConfig().fx_policy),
        )
        try:
            _seed_synthetic_year(svc)
            _settle_classifications(svc)
            meta = svc._execute_run(
                "2024-snapshot", 2024, "daily",
                ecb_provider=GoldenEcbProvider(),
                cz_fx_provider=GoldenCnbProvider(),
            )
        finally:
            svc.runner.shutdown(wait=False)

        assert meta["unverified_rate_dates"] == ["2024-03-05"]
        assert svc.find_cached_run(2024, "daily", "fifo") is None

    def test_the_daily_side_of_a_comparison_is_covered_as_well(self, service):
        meta = self._run(service, "compare")

        assert meta["unverified_rate_dates"] == ["2024-03-05"]
        assert service.find_cached_run(2024, "compare", "fifo") is None


class TestRunsThatTheComputeCacheMustNotHandBack:
    """Identical inputs normally mean "reuse the stored run". Two kinds of
    stored run would carry a warning that no longer applies - or hide one
    that does - if they were handed back in place of a new computation.
    """

    def _run(self, service, monkeypatch, today: date, run_id: str) -> dict:
        monkeypatch.setattr(service, "_today", lambda: today)
        return service._execute_run(
            run_id, 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )

    def _touch_year(self, service, written: datetime) -> None:
        for path in (service.data_dir / "2024").iterdir():
            os.utime(path, (written.timestamp(), written.timestamp()))

    def test_a_december_run_is_not_reused_once_its_year_has_closed(
            self, service, monkeypatch):
        # Nothing happened in the last days of the year, so the statements
        # downloaded again in January are byte for byte the December ones.
        _seed_synthetic_year(service)
        _settle_classifications(service)
        self._touch_year(service, datetime(2024, 12, 20, 18, 0))
        self._run(service, monkeypatch, date(2024, 12, 21), "2024-december")
        meta_path = service.runs_dir / "2024-december" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["created_at"] = "2024-12-21T12:00:00+00:00"
        meta_path.write_text(json.dumps(meta))
        assert service.find_cached_run(2024, "daily", "fifo") == "2024-december"

        self._touch_year(service, datetime(2025, 1, 20, 18, 0))
        monkeypatch.setattr(service, "_today", lambda: date(2025, 2, 10))

        # Handed back, it would say "recompute me" for ever.
        assert service.find_cached_run(2024, "daily", "fifo") is None

    def test_a_run_warning_about_early_statements_is_not_reused_after_they_settle(
            self, service, monkeypatch):
        _seed_synthetic_year(service)
        _settle_classifications(service)
        self._touch_year(service, datetime(2024, 12, 20, 18, 0))
        self._run(service, monkeypatch, date(2025, 2, 10), "2024-early")
        assert service.find_cached_run(2024, "daily", "fifo") == "2024-early"

        # The same bytes, downloaded again after the year settled.
        self._touch_year(service, datetime(2025, 2, 11, 9, 0))

        assert service.find_cached_run(2024, "daily", "fifo") is None


class TestRunStoredByAnOlderVersionIsFlaggedWhenRead:
    """Bumping the fingerprint version stops the compute cache from handing an
    old run back. It does nothing for the places that simply show the newest
    stored run of a year - the dashboard card, the results pages, the MCP
    year tools - and those went on presenting figures the current code would
    no longer compute (for this fix: figures from the wrong day's rates).
    """

    def test_a_run_without_the_current_version_carries_a_warning(
            self, service, monkeypatch):
        _seed_run_meta(service, "2025-old", 2025, "2026-08-08T10:00:00+00:00",
                       fingerprint_version="v6")
        monkeypatch.setattr(service, "_today", lambda: date(2026, 11, 27))

        meta = service.get_run("2025-old")

        assert meta["computed_by_older_version"] is True
        assert any("starší verzi" in note and "přepočítejte" in note
                   for note in meta["notes"])

    def test_a_run_from_before_the_stamp_existed_counts_as_older(
            self, service, monkeypatch):
        _seed_run_meta(service, "2025-unstamped", 2025, "2026-08-08T10:00:00+00:00",
                       fingerprint_version=None)
        monkeypatch.setattr(service, "_today", lambda: date(2026, 11, 27))

        assert service.get_run("2025-unstamped")["computed_by_older_version"] is True

    def test_the_dashboard_card_carries_that_flag(self, service, monkeypatch):
        _seed_run_meta(service, "2025-old", 2025, "2026-08-08T10:00:00+00:00",
                       fingerprint_version="v6")
        monkeypatch.setattr(service, "_today", lambda: date(2026, 11, 27))

        card = service.dashboard_overview()["year_cards"][0]

        assert card["computed_by_older_version"] is True

    def test_a_run_made_by_this_version_is_left_as_it_is(self, service, monkeypatch):
        _seed_synthetic_year(service)
        made = service._execute_run(
            "2024-current", 2024, "daily",
            ecb_provider=GoldenEcbProvider(),
            cz_fx_provider=GoldenCnbProvider(),
        )

        read = service.get_run("2024-current")

        assert "computed_by_older_version" not in read
        assert read["notes"] == made["notes"]
