# tests/test_historical_prices.py
"""
HistoricalPriceProvider — the §19 closing-price look-back and its disk cache.

Lives in src/utils because the engine needs it (src/engine must not import
src/webapp); the live-quote service in src/webapp/quotes.py re-exports it.

Offline: every test injects a fetcher, so no test touches the network. The
recorded windows mirror real Yahoo behaviour observed against the live API:
only traded days come back, weekends and holidays are simply absent.
"""
import datetime
import json
from decimal import Decimal

import pytest

from src.utils.historical_price_provider import HistoricalPriceProvider

D = datetime.date


@pytest.fixture
def provider_factory(tmp_path):
    """Build a provider with an injected fetcher and an isolated cache file."""
    def _make(closes, currency="USD", max_lookback_days=30, overrides=None):
        calls = []

        def fetcher(symbol, start, end):
            calls.append((symbol, start, end))
            if closes is None:
                return None
            return {d: Decimal(v) for d, v in closes.items()}, currency

        overrides_path = tmp_path / "symbol_map.json"
        if overrides is not None:
            overrides_path.write_text(json.dumps(overrides), encoding="utf-8")

        p = HistoricalPriceProvider(
            fetcher=fetcher,
            overrides_path=overrides_path,
            cache_file_path=tmp_path / "closes.json",
            max_lookback_days=max_lookback_days,
        )
        return p, calls
    return _make


class TestCloseOnTheDate:
    def test_close_on_the_requested_trading_day(self, provider_factory):
        # NU's real closes around the 2025-07-23 flip trade.
        p, _ = provider_factory({
            D(2025, 7, 22): "12.79",
            D(2025, 7, 23): "12.96",
            D(2025, 7, 24): "12.75",
        })
        hit = p.get_close("NU", "USD", D(2025, 7, 23))

        assert hit is not None
        assert hit.price == Decimal("12.96")
        assert hit.price_date == D(2025, 7, 23)
        assert hit.requested_date == D(2025, 7, 23)
        assert hit.used_lookback is False
        assert hit.currency == "USD"
        assert hit.yahoo_symbol == "NU"
        assert hit.source == "yahoo:chart"

    def test_symbol_override_is_honoured(self, provider_factory):
        p, calls = provider_factory(
            {D(2025, 7, 23): "8.03"}, currency="EUR",
            overrides={"TUI1x": "TUI1.DE"},
        )
        hit = p.get_close("TUI1x", "EUR", D(2025, 7, 23))

        assert hit is not None and hit.yahoo_symbol == "TUI1.DE"
        assert calls[0][0] == "TUI1.DE"


class TestLookBack:
    def test_weekend_falls_back_to_the_previous_close(self, provider_factory):
        """2025-07-26 was a Saturday — the venue has no bar for it."""
        p, _ = provider_factory({
            D(2025, 7, 24): "12.75",
            D(2025, 7, 25): "12.73",
        })
        hit = p.get_close("NU", "USD", D(2025, 7, 26))

        assert hit is not None
        assert hit.price == Decimal("12.73")
        assert hit.price_date == D(2025, 7, 25)   # Friday
        assert hit.requested_date == D(2025, 7, 26)
        assert hit.used_lookback is True

    def test_takes_the_newest_close_not_the_oldest(self, provider_factory):
        p, _ = provider_factory({
            D(2025, 7, 1): "10.00",
            D(2025, 7, 18): "13.02",
        })
        hit = p.get_close("NU", "USD", D(2025, 7, 20))

        assert hit is not None
        assert hit.price == Decimal("13.02")
        assert hit.price_date == D(2025, 7, 18)

    def test_nothing_within_the_window_returns_none(self, provider_factory):
        """A gap longer than the look-back must refuse, not reach further."""
        p, _ = provider_factory({D(2025, 5, 2): "9.00"}, max_lookback_days=30)
        assert p.get_close("NU", "USD", D(2025, 7, 23)) is None

    def test_window_edge_is_inclusive(self, provider_factory):
        p, _ = provider_factory({D(2025, 6, 23): "11.11"}, max_lookback_days=30)
        hit = p.get_close("NU", "USD", D(2025, 7, 23))

        assert hit is not None and hit.price_date == D(2025, 6, 23)

    def test_failed_fetch_returns_none(self, provider_factory):
        p, _ = provider_factory(None)
        assert p.get_close("NU", "USD", D(2025, 7, 23)) is None


class TestCache:
    def test_second_lookup_does_not_refetch(self, provider_factory):
        p, calls = provider_factory({D(2025, 7, 23): "12.96"})
        p.get_close("NU", "USD", D(2025, 7, 23))
        p.get_close("NU", "USD", D(2025, 7, 23))

        assert len(calls) == 1

    def test_non_trading_days_inside_a_covered_window_never_refetch(
            self, provider_factory):
        """Once a window is fetched, its weekend days resolve from cache.

        Only a date the window never reached forces another call (covered by
        test_a_later_date_refetches_instead_of_reusing_an_older_close).
        """
        p, calls = provider_factory({D(2025, 7, 25): "12.73"})

        p.get_close("NU", "USD", D(2025, 7, 27))   # Sunday — fetches through 27
        assert len(calls) == 1

        # Both are inside the fetched window: Saturday, then the same Sunday.
        assert p.get_close("NU", "USD", D(2025, 7, 26)).price_date == D(2025, 7, 25)
        assert p.get_close("NU", "USD", D(2025, 7, 27)).price_date == D(2025, 7, 25)
        assert len(calls) == 1

    def test_a_later_date_refetches_instead_of_reusing_an_older_close(self, tmp_path):
        """An unfetched day must not be treated as a day that did not trade.

        Asking for the 23rd fetches a window ending there. Asking for the 26th
        (a Saturday) then walks back over 25/24 — days no request has covered.
        Skipping them would return the 23rd's close and silently ignore
        Friday the 25th, which is the correct §19 fallback.
        """
        windows = []

        def fetcher(symbol, start, end):
            windows.append((start, end))
            data = {
                D(2025, 7, 23): Decimal("12.96"),
                D(2025, 7, 24): Decimal("12.75"),
                D(2025, 7, 25): Decimal("12.73"),
            }
            return {d: v for d, v in data.items() if start <= d <= end}, "USD"

        p = HistoricalPriceProvider(
            fetcher=fetcher,
            overrides_path=tmp_path / "symbol_map.json",
            cache_file_path=tmp_path / "closes.json",
        )

        first = p.get_close("NU", "USD", D(2025, 7, 23))
        assert first is not None and first.price_date == D(2025, 7, 23)

        second = p.get_close("NU", "USD", D(2025, 7, 26))   # Saturday
        assert second is not None
        assert second.price_date == D(2025, 7, 25)          # Friday, not the 23rd
        assert second.price == Decimal("12.73")
        assert len(windows) == 2                            # the gap forced a refetch

    def test_cache_survives_a_new_provider_instance(self, tmp_path, provider_factory):
        p1, calls1 = provider_factory({D(2025, 7, 23): "12.96"})
        p1.get_close("NU", "USD", D(2025, 7, 23))
        assert len(calls1) == 1

        # A fresh provider over the same file must not call out again.
        calls2 = []

        def dead_fetcher(symbol, start, end):
            calls2.append(symbol)
            return None

        p2 = HistoricalPriceProvider(
            fetcher=dead_fetcher,
            overrides_path=tmp_path / "symbol_map.json",
            cache_file_path=tmp_path / "closes.json",
        )
        hit = p2.get_close("NU", "USD", D(2025, 7, 23))

        assert hit is not None and hit.price == Decimal("12.96")
        assert calls2 == []

    def test_unreadable_cache_starts_empty_rather_than_raising(
            self, tmp_path, provider_factory):
        (tmp_path / "closes.json").write_text("{ not json", encoding="utf-8")
        p, _ = provider_factory({D(2025, 7, 23): "12.96"})
        assert p.get_close("NU", "USD", D(2025, 7, 23)).price == Decimal("12.96")


class TestFetcherParsing:
    """yahoo_fetch_closes against recorded payloads — shape, tz and pence."""

    def _payload(self, currency, gmtoffset, bars):
        return {"chart": {"result": [{
            "meta": {"currency": currency, "gmtoffset": gmtoffset},
            "timestamp": [ts for ts, _ in bars],
            "indicators": {"quote": [{"close": [c for _, c in bars]}]},
        }]}}

    def _fetch(self, monkeypatch, payload, start, end, symbol="X"):
        from src.utils import historical_price_provider as quotes

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return payload

        monkeypatch.setattr(quotes.requests, "get", lambda *a, **k: FakeResp())
        return quotes.yahoo_fetch_closes(symbol, start, end)

    def test_bar_is_dated_in_the_exchange_timezone(self, monkeypatch):
        """NYSE opens 13:30 UTC; with gmtoffset -14400 that is still the 23rd."""
        open_utc = int(datetime.datetime(
            2025, 7, 23, 13, 30, tzinfo=datetime.timezone.utc).timestamp())
        got = self._fetch(
            monkeypatch,
            self._payload("USD", -14400, [(open_utc, 12.96)]),
            D(2025, 7, 20), D(2025, 7, 24),
        )
        assert got is not None
        prices, currency = got
        assert currency == "USD"
        assert prices == {D(2025, 7, 23): Decimal("12.96")}

    def test_far_east_bar_is_not_misdated_to_the_previous_day(self, monkeypatch):
        """Sydney opens 10:00 AEDT = 23:00 UTC the day BEFORE.

        Reading the timestamp as UTC would file this bar under the 22nd and
        the look-back would then serve a stale close for the 23rd.
        """
        open_utc = int(datetime.datetime(
            2025, 7, 22, 23, 0, tzinfo=datetime.timezone.utc).timestamp())
        got = self._fetch(
            monkeypatch,
            self._payload("AUD", 39600, [(open_utc, 41.5)]),   # +11h
            D(2025, 7, 20), D(2025, 7, 24),
        )
        assert got is not None
        prices, _ = got
        assert prices == {D(2025, 7, 23): Decimal("41.5")}

    def test_pence_quotes_are_converted_to_pounds(self, monkeypatch):
        open_utc = int(datetime.datetime(
            2025, 7, 23, 8, 0, tzinfo=datetime.timezone.utc).timestamp())
        got = self._fetch(
            monkeypatch,
            self._payload("GBp", 3600, [(open_utc, 83.16)]),
            D(2025, 7, 22), D(2025, 7, 24),
        )
        assert got is not None
        prices, currency = got
        assert currency == "GBP"
        assert prices == {D(2025, 7, 23): Decimal("0.8316")}

    def test_null_bars_and_out_of_range_bars_are_dropped(self, monkeypatch):
        def utc(day, hour=13):
            return int(datetime.datetime(
                2025, 7, day, hour, tzinfo=datetime.timezone.utc).timestamp())
        got = self._fetch(
            monkeypatch,
            self._payload("USD", 0, [
                (utc(21), None),    # halted bar
                (utc(22), 12.79),
                (utc(30), 12.00),   # past the requested end
            ]),
            D(2025, 7, 20), D(2025, 7, 23),
        )
        assert got is not None
        prices, _ = got
        assert prices == {D(2025, 7, 22): Decimal("12.79")}

    def test_float32_artefact_is_rounded_back_to_the_quoted_close(self, monkeypatch):
        """Yahoo sends 12.73 as 12.729999542236328 — a tax figure must not."""
        open_utc = int(datetime.datetime(
            2025, 7, 25, 13, 30, tzinfo=datetime.timezone.utc).timestamp())
        got = self._fetch(
            monkeypatch,
            self._payload("USD", -14400, [(open_utc, 12.729999542236328)]),
            D(2025, 7, 24), D(2025, 7, 26),
        )
        assert got is not None
        prices, _ = got
        assert prices[D(2025, 7, 25)] == Decimal("12.73")

    def test_http_failure_returns_none(self, monkeypatch):
        from src.utils import historical_price_provider as quotes

        def boom(*a, **k):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(quotes.requests, "get", boom)
        assert quotes.yahoo_fetch_closes("X", D(2025, 7, 1), D(2025, 7, 2)) is None
