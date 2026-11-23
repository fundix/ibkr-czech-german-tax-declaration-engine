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
