"""Small hardening fixes ported from the German upstream (Fsaupe fork).

Each class names the upstream commit it follows. None of them moves a figure
on today's real data; each closes a path where a plausible input would.
"""
from decimal import Decimal

from src.domain.events import TradeEvent
from src.parsers.raw_models import RawTradeRecord


def _trade_events(**overrides):
    from src.classification.asset_classifier import AssetClassifier
    from src.identification.asset_resolver import AssetResolver
    from src.parsers.domain_event_factory import DomainEventFactory

    payload = {
        "ClientAccountID": "U1", "CurrencyPrimary": "USD", "AssetClass": "STK",
        "Symbol": "SYN", "Description": "SYNTHETIC INC", "ISIN": "US0000000001",
        "Conid": "700000002", "TradeDate": "2025-12-30", "Quantity": "10",
        "TradePrice": "5", "IBCommission": "-1", "IBCommissionCurrency": "USD",
        "Buy/Sell": "BUY", "TransactionID": "9100", "Open/CloseIndicator": "O",
    }
    payload.update(overrides)
    factory = DomainEventFactory(AssetResolver(AssetClassifier()))
    events, _, _ = factory.create_events_from_trades([RawTradeRecord(**payload)])
    return [e for e in events if isinstance(e, TradeEvent)]


class TestTradeIsDatedByItsContractDate:
    """Upstream 82733e8: a trade is booked on its TradeDate by rule.

    The old helper preferred SettleDateTarget, so adding that column to the
    Flex query would silently move a 30 December purchase into the next tax
    year and onto another day's exchange rate.
    """

    def test_settlement_date_does_not_override_the_trade_date(self):
        (evt,) = _trade_events(SettleDateTarget="2026-01-02")
        assert evt.event_date == "2025-12-30"

    def test_report_date_does_not_override_the_trade_date(self):
        (evt,) = _trade_events(ReportDate="2026-01-05")
        assert evt.event_date == "2025-12-30"

    def test_trade_time_keeps_the_trade_date(self):
        (evt,) = _trade_events(TradeTime="23:59:59")
        assert evt.event_date == "2025-12-30"


class TestSortKeyIsDeterministic:
    """Upstream 80cf74e: the last tie-break of the event sort key was
    event_id, a uuid4 drawn afresh each run -- two events tying on everything
    else came out in a different order on every run."""

    def _two_tied_trades(self):
        import uuid
        from src.classification.asset_classifier import AssetClassifier
        from src.domain.enums import FinancialEventType
        from src.identification.asset_resolver import AssetResolver
        from src.parsers.domain_event_factory import DomainEventFactory

        resolver = AssetResolver(AssetClassifier())
        factory = DomainEventFactory(resolver)
        payload = {
            "ClientAccountID": "U1", "CurrencyPrimary": "USD", "AssetClass": "STK",
            "Symbol": "SYN", "Description": "SYNTHETIC INC", "ISIN": "US0000000001",
            "Conid": "700000002", "TradeDate": "2025-12-30", "Quantity": "10",
            "TradePrice": "5", "Buy/Sell": "BUY", "TransactionID": "9100",
            "Open/CloseIndicator": "O",
        }
        (seed, *_), _, _ = factory.create_events_from_trades([RawTradeRecord(**payload)])

        def _tied(event_id):
            return TradeEvent(
                asset_internal_id=seed.asset_internal_id, event_date="2025-12-30",
                event_type=FinancialEventType.TRADE_BUY_LONG, quantity=Decimal("1"),
                price_foreign_currency=Decimal("1"), event_id=event_id,
            )
        # Built first, but with the LARGER uuid: a uuid tie-break reverses them.
        first = _tied(uuid.UUID(int=2))
        second = _tied(uuid.UUID(int=1))
        return resolver, first, second

    def test_tied_events_keep_construction_order(self):
        from src.utils.sorting_utils import get_event_sort_key

        resolver, first, second = self._two_tied_trades()
        ordered = sorted([second, first], key=lambda e: get_event_sort_key(e, resolver))
        assert ordered == [first, second]

    def test_the_sort_key_carries_no_per_run_identifier(self):
        from src.utils.sorting_utils import get_event_sort_key

        resolver, first, _ = self._two_tied_trades()
        key = get_event_sort_key(first, resolver)
        assert first.event_id not in key[1]


class TestEoyMismatchBlocksFiling:
    """Upstream 1c4c981 refuses to declare from a ledger that does not
    reconcile to the broker's year-end positions. Here the run still completes
    (a running-year estimate must not die on a moving snapshot), but the
    result carries a blocking review note naming every position, so it
    reaches the review page, the MCP checklist, the PDF and the XLSX."""

    MISMATCH = {"symbol": "SYN", "description": "SYNTHETIC INC",
                "calculated": Decimal("20"), "reported": Decimal("10")}

    def test_the_note_blocks_filing_and_names_each_position(self):
        from src.countries.cz.aggregation_service import eoy_reconciliation_section

        section = eoy_reconciliation_section([self.MISMATCH])
        (note,) = section.notes
        assert "REVIEW REQUIRED" in note and "DO NOT FILE" in note
        assert "SYN" in note and "20" in note and "10" in note

    def test_a_reconciled_ledger_adds_nothing(self):
        from src.countries.cz.aggregation_service import eoy_reconciliation_section

        assert eoy_reconciliation_section([]) is None

    def test_the_aggregation_attaches_the_section(self):
        from types import SimpleNamespace
        from src.classification.asset_classifier import AssetClassifier
        from src.countries.cz.aggregation_service import run_cz_aggregation
        from src.identification.asset_resolver import AssetResolver

        class _NoRates:
            def get_rate(self, *_a, **_k):
                return None

        processing = SimpleNamespace(
            realized_gains_losses=[], processed_income_events=[],
            asset_resolver=AssetResolver(AssetClassifier()),
            eoy_mismatch_details=[self.MISMATCH],
        )
        result = run_cz_aggregation(processing, 2025, "daily", fx_provider=_NoRates())
        assert "eoy_reconciliation" in result.sections
        assert "DO NOT FILE" in result.sections["eoy_reconciliation"].notes[0]


class TestShortSaleWhoseCostsExceedItsPrice:
    """Upstream ccd01ed / 3541de7: a short opened for less than its own
    commission (writing a near-worthless option) has negative net proceeds.
    add_short_lot already keeps the sign; ShortFifoLot then crashed on it."""

    def test_the_short_lot_keeps_negative_proceeds_and_the_cover_books_the_loss(self):
        import uuid
        from src.domain.enums import FinancialEventType
        from tests.test_audit_2026_07_fixes import _trade
        from tests.test_audit_fixes import _make_ledger

        ledger = _make_ledger()
        aid = uuid.uuid4()
        # Sold for 1.00, paid 1.10 commission: brought in -0.10.
        ledger.add_short_lot(_trade(
            aid, "2026-01-10", Decimal("-1"), Decimal("-0.10"), "1",
            event_type=FinancialEventType.TRADE_SELL_SHORT_OPEN,
        ))
        assert ledger.short_lots[0].total_sale_proceeds_eur == Decimal("-0.10")

        cover = _trade(aid, "2026-02-10", Decimal("1"), Decimal("0.50"), "2",
                       event_type=FinancialEventType.TRADE_BUY_SHORT_COVER)
        (rgl,) = ledger.consume_short_lots_for_cover(cover)
        assert rgl.gross_gain_loss_eur == Decimal("-0.60")
