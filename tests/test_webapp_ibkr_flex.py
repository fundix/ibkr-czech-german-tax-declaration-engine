# tests/test_webapp_ibkr_flex.py
"""
IBKR Flex Web Service integration — client protocol (SendRequest →
ReferenceCode → GetStatement with 1019 polling), config handling, and the
fetch-then-recompute service flow. All offline via injected fetchers.
"""
from pathlib import Path

import pytest

from src.webapp.ibkr_flex import (
    FlexConfig,
    FlexFetchError,
    fetch_statement,
    load_flex_config,
    save_flex_config,
)
from src.webapp.services import RunService, _effective_fx_mode

SEND_OK = b"<FlexStatementResponse><Status>Success</Status><ReferenceCode>REF123</ReferenceCode></FlexStatementResponse>"
GENERATING = b"<FlexStatementResponse><ErrorCode>1019</ErrorCode><ErrorMessage>Statement generation in progress</ErrorMessage></FlexStatementResponse>"
TOKEN_EXPIRED = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
RATE_LIMITED = b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1018</ErrorCode><ErrorMessage>Too many requests have been made from this token.</ErrorMessage></FlexStatementResponse>"
CSV_BODY = b'"ClientAccountID","Symbol"\n"U1","AAPL"\n'


class TestFetchStatement:
    def test_success_two_step(self):
        calls = []

        def http_get(url, params):
            calls.append((url.rsplit("/", 1)[-1], dict(params)))
            return SEND_OK if "SendRequest" in url else CSV_BODY

        body = fetch_statement("tok", "42", http_get=http_get)
        assert body == CSV_BODY
        assert calls[0] == ("SendRequest", {"t": "tok", "q": "42", "v": "3"})
        assert calls[1] == ("GetStatement", {"t": "tok", "q": "REF123", "v": "3"})

    def test_period_override_adds_fd_td_to_send_request_only(self):
        calls = []

        def http_get(url, params):
            calls.append((url.rsplit("/", 1)[-1], dict(params)))
            return SEND_OK if "SendRequest" in url else CSV_BODY

        fetch_statement("tok", "42", http_get=http_get,
                        from_date="20250101", to_date="20251231")
        assert calls[0] == ("SendRequest", {"t": "tok", "q": "42", "v": "3",
                                            "fd": "20250101", "td": "20251231"})
        # GetStatement polls the reference code — no period params there
        assert calls[1] == ("GetStatement", {"t": "tok", "q": "REF123", "v": "3"})

    def test_polls_while_generating(self):
        responses = iter([SEND_OK, GENERATING, GENERATING, CSV_BODY])
        slept = []
        body = fetch_statement(
            "tok", "42",
            http_get=lambda url, params: next(responses),
            sleep=slept.append,
        )
        assert body == CSV_BODY
        assert len(slept) == 2

    def test_expired_token_raises_with_hint(self):
        with pytest.raises(FlexFetchError, match="Token vypršel"):
            fetch_statement("tok", "42", http_get=lambda u, p: TOKEN_EXPIRED)

    def test_unexpected_response_raises(self):
        with pytest.raises(FlexFetchError, match="Neočekávaná"):
            fetch_statement("tok", "42", http_get=lambda u, p: b"<html>login</html>")

    def test_gives_up_after_max_polls(self):
        responses = iter([SEND_OK] + [GENERATING] * 20)
        with pytest.raises(FlexFetchError, match="negeneroval"):
            fetch_statement("tok", "42",
                            http_get=lambda u, p: next(responses),
                            sleep=lambda s: None)

    def test_rate_limit_on_send_request_backs_off_and_retries(self):
        # Regression: 1018 must NOT fail the download — back off and retry.
        responses = iter([RATE_LIMITED, RATE_LIMITED, SEND_OK, CSV_BODY])
        slept = []
        body = fetch_statement("tok", "42",
                               http_get=lambda u, p: next(responses),
                               sleep=slept.append)
        assert body == CSV_BODY
        assert len(slept) == 2
        assert all(s >= 30 for s in slept)  # longer backoff than 1019 polling

    def test_rate_limit_during_polling_retries_with_long_delay(self):
        responses = iter([SEND_OK, RATE_LIMITED, CSV_BODY])
        slept = []
        body = fetch_statement("tok", "42",
                               http_get=lambda u, p: next(responses),
                               sleep=slept.append)
        assert body == CSV_BODY
        assert slept == [30.0]

    def test_persistent_rate_limit_gives_up_with_hint(self):
        with pytest.raises(FlexFetchError, match="příliš mnoho požadavků") as exc:
            fetch_statement("tok", "42",
                            http_get=lambda u, p: RATE_LIMITED,
                            sleep=lambda s: None)
        assert exc.value.code == "1018"


class TestFlexConfig:
    def test_roundtrip_and_masking(self, tmp_path):
        path = tmp_path / "flex.json"
        save_flex_config(path, FlexConfig(token="abcdef123456",
                                          queries={"trades": "1", "cash": "2"}))
        cfg = load_flex_config(path)
        assert cfg.configured
        assert cfg.queries == {"trades": "1", "cash": "2"}
        assert cfg.masked_token() == "abc…456"
        assert "abcdef123456" not in cfg.masked_token()

    def test_missing_file_is_unconfigured(self, tmp_path):
        cfg = load_flex_config(tmp_path / "nope.json")
        assert not cfg.configured


class TestEffectiveFxMode:
    def test_compare_downgrades_to_daily_for_running_year(self):
        # GFŘ publishes the jednotný kurz only after the year ends — a
        # running-year "uniform" column would be nonsense (all pending).
        mode, notes = _effective_fx_mode("compare", 2026, 2026)
        assert mode == "daily"
        assert notes and "Jednotný kurz" in notes[0]

    def test_compare_kept_for_closed_year(self):
        assert _effective_fx_mode("compare", 2025, 2026) == ("compare", [])

    def test_explicit_modes_untouched(self):
        assert _effective_fx_mode("daily", 2026, 2026) == ("daily", [])
        # explicit uniform for a running year is the user's own choice
        assert _effective_fx_mode("uniform", 2026, 2026) == ("uniform", [])


class TestServiceFetchFlow:
    @pytest.fixture
    def service(self, tmp_path):
        svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
        yield svc
        svc.runner.shutdown(wait=False)

    def test_should_auto_fetch_logic(self, service):
        assert service.should_auto_fetch(2026) is False  # not configured
        service.save_flex_settings("tok", {"trades": "1"})
        assert service.should_auto_fetch(2026) is True   # configured, no data
        service.save_upload(2026, "trades", b"h\n1\n")   # fresh file
        assert service.should_auto_fetch(2026) is False

    def test_fetch_and_run_saves_canonical_files_then_recomputes(self, service, monkeypatch):
        service.save_flex_settings("tok", {
            "trades": "11", "cash": "22", "positions": "33", "corp_actions": "44",
        })
        executed = {}
        monkeypatch.setattr(
            service, "_execute_run",
            lambda run_id, year, fx_mode, **kw: executed.update(
                run_id=run_id, year=year, fx_mode=fx_mode) or {"run_id": run_id},
        )
        fetched_queries = []

        def fake_fetch(token, query_id, from_date=None, to_date=None):
            fetched_queries.append((token, query_id))
            return f"data-{query_id}".encode()

        pauses = []
        meta = service._fetch_and_run("2026-x", 2026, "compare",
                                      fetch=fake_fetch, pause=pauses.append)
        year_dir = service.data_dir / "2026"
        assert (year_dir / "trades.csv").read_bytes() == b"data-11"
        assert (year_dir / "cash_transactions.csv").read_bytes() == b"data-22"
        # positions land as positions_end (= state as of last business day)
        assert (year_dir / "positions_end.csv").read_bytes() == b"data-33"
        assert (year_dir / "corporate_actions.csv").read_bytes() == b"data-44"
        assert [q for _, q in fetched_queries] == ["11", "22", "33", "44"]
        assert executed == {"run_id": "2026-x", "year": 2026, "fx_mode": "compare"}
        assert meta["fetched_slots"] == ["trades", "cash", "positions", "corp_actions"]
        # IBKR throttles per-token bursts — a pause between each download
        assert len(pauses) == 3

    def test_bootstrap_fetches_all_missing_years_via_fd_td(self, service, monkeypatch):
        service.save_flex_settings(
            "tok",
            {"trades": "11", "cash": "22", "positions": "33", "corp_actions": "44"},
            first_year="2024",
        )
        captured = {}
        monkeypatch.setattr(
            service, "_execute_run",
            lambda run_id, year, fx_mode, **kw: captured.update(kw) or {"run_id": run_id},
        )
        fetch_calls = []

        def fake_fetch(token, query_id, from_date=None, to_date=None):
            fetch_calls.append((query_id, from_date, to_date))
            return f"data-{query_id}-{to_date}".encode()

        pauses = []
        meta = service._fetch_and_run("2026-x", 2026, "daily",
                                      fetch=fake_fetch, pause=pauses.append)

        # Missing 2024 + 2025 bootstrapped oldest-first with full-year fd/td;
        # the current year keeps the queries' saved (YTD) period.
        assert fetch_calls[:4] == [(q, "20240101", "20241231")
                                   for q in ("11", "22", "33", "44")]
        assert fetch_calls[4:8] == [(q, "20250101", "20251231")
                                    for q in ("11", "22", "33", "44")]
        assert fetch_calls[8:] == [(q, None, None) for q in ("11", "22", "33", "44")]

        # Positions land as positions_end (31 Dec snapshot for old years)
        assert (service.data_dir / "2024" / "positions_end.csv").read_bytes() \
            == b"data-33-20241231"
        assert (service.data_dir / "2025" / "trades.csv").read_bytes() \
            == b"data-11-20251231"
        assert meta["bootstrapped_years"] == [2024, 2025]
        assert any("2024, 2025" in n for n in captured["extra_notes"])
        # A pause between every consecutive download: 12 requests -> 11 pauses
        assert len(pauses) == 11

    def test_bootstrap_skips_existing_years_and_survives_a_failed_one(self, service, monkeypatch):
        service.save_flex_settings(
            "tok",
            {"trades": "11", "cash": "22", "positions": "33", "corp_actions": "44"},
            first_year="2024",
        )
        # A run-ready 2025 dataset already exists — must NOT be overwritten
        for slot in ("trades", "cash", "positions_end"):
            service.save_upload(2025, slot, b"existing\n")
        captured = {}
        monkeypatch.setattr(
            service, "_execute_run",
            lambda run_id, year, fx_mode, **kw: captured.update(kw) or {"run_id": run_id},
        )

        def fake_fetch(token, query_id, from_date=None, to_date=None):
            if to_date == "20241231":  # IBKR cannot deliver 2024
                raise FlexFetchError("stará data nejsou", code="1020")
            return f"data-{query_id}".encode()

        meta = service._fetch_and_run("2026-x", 2026, "daily",
                                      fetch=fake_fetch, pause=lambda s: None)

        # 2024 failed but the job carried on; 2025 untouched; 2026 fetched
        assert "bootstrapped_years" not in meta
        assert any("2024" in n and "selhalo" in n for n in captured["extra_notes"])
        assert (service.data_dir / "2025" / "trades.csv").read_bytes() == b"existing\n"
        assert (service.data_dir / "2026" / "trades.csv").read_bytes() == b"data-11"

    def test_no_bootstrap_without_first_year(self, service, monkeypatch):
        service.save_flex_settings("tok", {"trades": "11"})
        monkeypatch.setattr(service, "_execute_run",
                            lambda run_id, year, fx_mode, **kw: {"run_id": run_id})
        fetch_calls = []
        service._fetch_and_run(
            "2026-x", 2026, "daily",
            fetch=lambda t, q, from_date=None, to_date=None:
                fetch_calls.append((q, from_date)) or b"data",
            pause=lambda s: None)
        assert fetch_calls == [("11", None)]

    def test_flex_config_first_year_roundtrip(self, tmp_path):
        path = tmp_path / "flex.json"
        save_flex_config(path, FlexConfig(
            token="abcdef123456", queries={"trades": "1"}, first_year=2024,
        ))
        cfg = load_flex_config(path)
        assert cfg.first_year == 2024
        # And absent stays absent
        save_flex_config(path, FlexConfig(token="abcdef123456", queries={"trades": "1"}))
        assert load_flex_config(path).first_year is None

    def test_start_fetch_requires_configuration(self, service):
        with pytest.raises(ValueError, match="není nastavená"):
            service.start_fetch_and_run(2026)
