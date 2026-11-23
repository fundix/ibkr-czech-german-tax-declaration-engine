"""A stock trade's transaction tax (stamp duty) belongs to its cost or proceeds.

Hong Kong charges stamp duty on both sides of a share trade, Korea a
transaction tax on the sale. IBKR reports it in the Trades export's ``Taxes``
column -- which the user's Flex query may not include -- and always in the
Statement of Funds' ``TradeTax`` column. Ignoring it carried a purchase too
cheap and a sale too rich, so the §10 gain was overstated by the tax on both
legs. Ported in spirit from upstream ccb81f0 (Fsaupe fork); the currency side
of that commit does not apply here, the Czech FX ledger replays the Statement
of Funds' ``Amount``, which already includes the tax.
"""
from decimal import Decimal

import pytest

from src.domain.enums import FinancialEventType
from src.domain.events import TradeEvent
from src.parsers.raw_models import RawStatementOfFundsRecord, RawTradeRecord


def _raw_trade(taxes=None, buy_sell="BUY", tx_id="9000001"):
    # Synthetic HKD trade: 100 x 50, commission 18.
    payload = {
        "ClientAccountID": "U1", "CurrencyPrimary": "HKD", "AssetClass": "STK",
        "Symbol": "SYNH", "Description": "SYNTHETIC CO LTD-H",
        "ISIN": "HK0000000001", "Conid": "700000001",
        "TradeDate": "2025-07-15",
        "Quantity": "100" if buy_sell == "BUY" else "-100",
        "TradePrice": "50", "IBCommission": "-18",
        "IBCommissionCurrency": "HKD", "Buy/Sell": buy_sell,
        "TransactionID": tx_id,
        "Open/CloseIndicator": "O" if buy_sell == "BUY" else "C",
    }
    if taxes is not None:
        payload["Taxes"] = taxes
    return RawTradeRecord(**payload)


def _trade_event(taxes=None, buy_sell="BUY", tx_id="9000001") -> TradeEvent:
    from src.classification.asset_classifier import AssetClassifier
    from src.identification.asset_resolver import AssetResolver
    from src.parsers.domain_event_factory import DomainEventFactory

    factory = DomainEventFactory(AssetResolver(AssetClassifier()))
    events, _, _ = factory.create_events_from_trades([_raw_trade(taxes, buy_sell, tx_id)])
    trades = [e for e in events if isinstance(e, TradeEvent)]
    assert len(trades) == 1
    return trades[0]


def _eur_trade(event_type, gross, commission, tax) -> TradeEvent:
    """A trade in EUR, so the enriched net is readable without an FX table."""
    import uuid
    qty = Decimal("1") if event_type in (FinancialEventType.TRADE_BUY_LONG,
                                         FinancialEventType.TRADE_BUY_SHORT_COVER) else Decimal("-1")
    return TradeEvent(
        asset_internal_id=uuid.uuid4(), event_date="2026-03-20",
        event_type=event_type, quantity=qty, price_foreign_currency=gross,
        commission_foreign_currency=commission, commission_currency="EUR",
        transaction_tax_foreign=tax,
        local_currency="EUR", gross_amount_foreign_currency=gross,
    )


def _enrich(evt):
    from src.processing.enrichment import enrich_financial_events

    class _NoFx:
        def convert_to_eur(self, amount, currency, on):
            raise AssertionError("an EUR trade needs no conversion")

    enrich_financial_events([evt], _NoFx(), 28, "ROUND_HALF_UP")
    return evt


class TestFactoryReadsTheTaxesColumn:
    def test_a_charged_tax_is_stored_as_a_positive_cost(self):
        evt = _trade_event(taxes="-7.5")
        assert evt.transaction_tax_foreign == Decimal("7.5")

    def test_no_column_means_no_tax(self):
        assert _trade_event(taxes=None).transaction_tax_foreign == Decimal("0")

    def test_a_blank_cell_means_no_tax(self):
        assert _trade_event(taxes="").transaction_tax_foreign == Decimal("0")


class TestEnrichmentFoldsTheTax:
    def test_tax_raises_a_purchase_cost(self):
        evt = _enrich(_eur_trade(FinancialEventType.TRADE_BUY_LONG,
                                 Decimal("1000"), Decimal("3"), Decimal("2")))
        assert evt.transaction_tax_eur == Decimal("2")
        assert evt.net_proceeds_or_cost_basis_eur == Decimal("1005")

    def test_tax_lowers_sale_proceeds(self):
        evt = _enrich(_eur_trade(FinancialEventType.TRADE_SELL_LONG,
                                 Decimal("1000"), Decimal("3"), Decimal("2")))
        assert evt.net_proceeds_or_cost_basis_eur == Decimal("995")

    def test_tax_lowers_short_sale_proceeds_and_raises_cover_cost(self):
        short = _enrich(_eur_trade(FinancialEventType.TRADE_SELL_SHORT_OPEN,
                                   Decimal("1000"), Decimal("0"), Decimal("2")))
        cover = _enrich(_eur_trade(FinancialEventType.TRADE_BUY_SHORT_COVER,
                                   Decimal("1000"), Decimal("0"), Decimal("2")))
        assert short.net_proceeds_or_cost_basis_eur == Decimal("998")
        assert cover.net_proceeds_or_cost_basis_eur == Decimal("1002")

    def test_untaxed_trade_is_unchanged(self):
        evt = _enrich(_eur_trade(FinancialEventType.TRADE_BUY_LONG,
                                 Decimal("1000"), Decimal("3"), Decimal("0")))
        assert evt.net_proceeds_or_cost_basis_eur == Decimal("1003")

    def test_foreign_tax_converts_on_the_trade_date(self):
        from src.processing.enrichment import enrich_financial_events

        seen = []

        class _Fx:
            def convert_to_eur(self, amount, currency, on):
                seen.append((amount, currency, str(on)))
                return amount / Decimal("10")

        evt = _trade_event(taxes="-20")
        enrich_financial_events([evt], _Fx(), 28, "ROUND_HALF_UP")
        assert (Decimal("20"), "HKD", "2025-07-15") in seen
        # (5000 + 18 + 20) / 10
        assert evt.net_proceeds_or_cost_basis_eur == Decimal("503.8")


class TestOriginalCostIncludesTheTax:
    def test_lot_original_cost_matches_the_eur_formula(self):
        from src.engine.fifo_manager import _original_cost_of_buy

        evt = _trade_event(taxes="-7.5")
        total, currency = _original_cost_of_buy(evt)
        assert currency == "HKD"
        assert total == Decimal("5000") + Decimal("18") + Decimal("7.5")


class TestPositionFlipSplitsTheTax:
    def test_both_halves_carry_their_share(self):
        from src.engine.fifo_manager import split_position_flip_event

        evt = _eur_trade(FinancialEventType.TRADE_SELL_LONG,
                         Decimal("1000"), Decimal("0"), Decimal("4"))
        evt.quantity = Decimal("-4")
        evt.allows_position_flip = True
        close, open_ = split_position_flip_event(evt, Decimal("1"), Decimal("0"))
        assert close.transaction_tax_foreign == Decimal("1")
        assert open_.transaction_tax_foreign == Decimal("3")


def _sof(tx_id, tax, currency="HKD", asset_class="STK"):
    return RawStatementOfFundsRecord(**{
        "CurrencyPrimary": currency, "AssetClass": asset_class,
        "ActivityCode": "BUY", "Date": "2025-07-15",
        "TradeTax": tax, "Amount": "-5025.5",
        "TransactionID": tx_id, "LevelOfDetail": "Currency",
    })


class TestBackfillFromStatementOfFunds:
    """The Trades query of the real book has no Taxes column; the Statement
    of Funds carries the same figure under the trade's own TransactionID."""

    def test_tax_is_taken_from_the_matching_row(self):
        from src.processing.trade_tax_backfill import apply_statement_of_funds_trade_taxes

        evt = _trade_event(taxes=None)
        applied = apply_statement_of_funds_trade_taxes(
            [evt], [_sof("9000001", "-7.5")])
        assert applied == 1
        assert evt.transaction_tax_foreign == Decimal("7.5")

    def test_a_tax_from_the_trades_export_is_not_overwritten(self):
        from src.processing.trade_tax_backfill import apply_statement_of_funds_trade_taxes

        evt = _trade_event(taxes="-7.5")
        applied = apply_statement_of_funds_trade_taxes(
            [evt], [_sof("9000001", "-99")])
        assert applied == 0
        assert evt.transaction_tax_foreign == Decimal("7.5")

    def test_rows_without_tax_or_without_a_match_change_nothing(self):
        from src.processing.trade_tax_backfill import apply_statement_of_funds_trade_taxes

        evt = _trade_event(taxes=None)
        applied = apply_statement_of_funds_trade_taxes(
            [evt], [_sof("9000001", "0"), _sof("other", "-5")])
        assert applied == 0
        assert evt.transaction_tax_foreign == Decimal("0")

    def test_a_currency_mismatch_is_refused(self):
        from src.processing.trade_tax_backfill import apply_statement_of_funds_trade_taxes

        evt = _trade_event(taxes=None)
        with pytest.raises(ValueError, match="currency"):
            apply_statement_of_funds_trade_taxes(
                [evt], [_sof("9000001", "-7.5", currency="USD")])

    def test_two_tax_rows_for_one_trade_are_refused(self):
        from src.processing.trade_tax_backfill import apply_statement_of_funds_trade_taxes

        evt = _trade_event(taxes=None)
        with pytest.raises(ValueError, match="9000001"):
            apply_statement_of_funds_trade_taxes(
                [evt], [_sof("9000001", "-1"), _sof("9000001", "-2")])
