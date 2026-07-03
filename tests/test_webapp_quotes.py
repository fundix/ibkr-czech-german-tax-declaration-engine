# tests/test_webapp_quotes.py
"""Quote service: IBKR→Yahoo symbol mapping, TTL cache, GBp handling."""
from decimal import Decimal

from src.webapp.quotes import QuoteService, map_symbol


class TestSymbolMapping:
    def test_usd_passthrough(self):
        assert map_symbol("BABA", "USD") == "BABA"

    def test_trailing_lowercase_venue_marker_stripped(self):
        assert map_symbol("COPNz", "CHF") == "COPN.SW"
        assert map_symbol("EVOs", "SEK") == "EVO.ST"

    def test_currency_suffixes(self):
        assert map_symbol("BOSS", "EUR") == "BOSS.DE"
        assert map_symbol("HSBA", "GBP") == "HSBA.L"
        assert map_symbol("CEZ", "CZK") == "CEZ.PR"

    def test_hkd_numeric_zero_padded(self):
        assert map_symbol("700", "HKD") == "0700.HK"

    def test_override_wins(self):
        assert map_symbol("AMV0", "EUR", {"AMV0": "AMV.DE"}) == "AMV.DE"


class TestQuoteService:
    def test_ttl_cache_avoids_refetch_and_caches_failures(self, tmp_path):
        calls = []

        def fake_fetch(symbol):
            calls.append(symbol)
            if symbol == "GONE":
                return None
            return Decimal("11.5"), "USD"

        svc = QuoteService(fetcher=fake_fetch, overrides_path=tmp_path / "m.json")
        q1 = svc.get_quote("BABA", "USD")
        q2 = svc.get_quote("BABA", "USD")
        assert q1.price == Decimal("11.5")
        assert q2 is q1
        assert calls == ["BABA"]

        assert svc.get_quote("GONE", "USD") is None
        assert svc.get_quote("GONE", "USD") is None
        assert calls == ["BABA", "GONE"]  # failure cached too

    def test_ttl_expiry_triggers_refetch(self, tmp_path):
        calls = []

        def fake_fetch(symbol):
            calls.append(symbol)
            return Decimal("1"), "USD"

        svc = QuoteService(fetcher=fake_fetch, overrides_path=tmp_path / "m.json",
                           ttl_seconds=0)
        svc.get_quote("BABA", "USD")
        svc.get_quote("BABA", "USD")
        assert calls == ["BABA", "BABA"]

    def test_overrides_file_used(self, tmp_path):
        seen = []
        (tmp_path / "m.json").write_text('{"AMV0": "AMV.DE"}')

        def fake_fetch(symbol):
            seen.append(symbol)
            return Decimal("40"), "EUR"

        svc = QuoteService(fetcher=fake_fetch, overrides_path=tmp_path / "m.json")
        q = svc.get_quote("AMV0", "EUR")
        assert seen == ["AMV.DE"]
        assert q.yahoo_symbol == "AMV.DE"
