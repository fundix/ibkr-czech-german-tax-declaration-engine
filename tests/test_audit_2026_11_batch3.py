# tests/test_audit_2026_11_batch3.py
"""
Regression tests for the option and corporate-action findings of the 2026-11
audit:

* V08 — an option on an ETF lost its premium when it was assigned or
  exercised: only stocks were accepted as the delivery leg.
* V09 — deliveries were matched to exercises/assignments without the side,
  so two opposite events on one day swapped their premiums depending on the
  order of the CSV rows.
* X14 — an exercise or assignment that delivers no shares (a cash-settled
  index option) ended the run with an exception, and its settlement amount
  was dropped.
* X15 — a stock-for-stock merger that names the acquirer by its ISIN created
  a phantom asset instead of finding the acquirer.

Everything here is synthetic: invented symbols, ISINs, conids and amounts.
Expected figures are worked out by hand in each test, in EUR so that no
exchange rate takes part.
"""
from decimal import Decimal

import pytest

from src.classification.asset_classifier import AssetClassifier
from src.domain.assets import Option, Stock
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import (
    CorpActionMergerStock, OptionAssignmentEvent, OptionExerciseEvent, TradeEvent,
)
from src.identification.asset_resolver import AssetResolver
from src.parsers.domain_event_factory import DomainEventFactory
from src.parsers.raw_models import RawCorporateActionRecord
from src.processing.option_trade_linker import OptionTradeLinker
from tests.support.base import FifoTestCaseBase
from tests.support.mock_providers import MockECBExchangeRateProvider

ACCOUNT = "U1234567"
TAX_YEAR = 2025


def _trade_row(*, asset_class, sub_category, symbol, description, isin, conid,
               trade_date, quantity, price, buy_sell, open_close, tx_id,
               commission="-1.00", notes="", currency="EUR",
               strike=None, expiry=None, put_call=None,
               underlying_symbol="", underlying_conid="", multiplier="1"):
    """One Trades row, in the column order of ``TRADES_FILE_HEADERS``."""
    return [
        ACCOUNT, currency, asset_class, sub_category, symbol, description, isin,
        strike, expiry, put_call, trade_date, quantity, price, commission,
        currency, buy_sell, tx_id, notes, underlying_symbol, conid,
        underlying_conid, multiplier, open_close,
    ]


# A synthetic index ETF: a STK row whose SubCategory makes it a fund.
ETF = dict(asset_class="STK", sub_category="ETF", symbol="SYNE",
           description="SYNTHETIC INDEX ETF", isin="US0000000101",
           conid="900000101")

STOCK = dict(asset_class="STK", sub_category="COMMON", symbol="SYNA",
             description="SYNTHETIC ALPHA INC", isin="US0000000201",
             conid="900000201")

# An index: options are written on it, but it never appears as a row itself.
INDEX = dict(symbol="SYNX", conid="900000301")


def _option_on(underlying, *, put_call, strike, expiry, conid):
    """Row fields of an option on *underlying* (100 shares per contract)."""
    yymmdd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    return dict(
        asset_class="OPT", sub_category=put_call,
        symbol=f"{underlying['symbol']}  {yymmdd}{put_call}{int(strike):05d}000",
        description=f"{underlying['symbol']} {expiry} {strike} {put_call}",
        isin="", conid=conid, strike=strike, expiry=expiry, put_call=put_call,
        underlying_symbol=underlying["symbol"],
        underlying_conid=underlying["conid"], multiplier="100",
    )


def _one_to_one():
    return MockECBExchangeRateProvider(foreign_to_eur_init_value=Decimal("1.0"))


# ---------------------------------------------------------------------------
# V08 — the delivery leg of an option may be a fund, not only a stock
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("country", ["cz", "de"])
class TestV08OptionOnAnEtfKeepsItsPremium(FifoTestCaseBase):
    """Options on an index ETF are assigned like options on a stock, but the
    ETF is classified as an investment fund. The factory, the linker and the
    trade processor each accepted stocks only, so the delivery was never
    linked: the option's lots were closed without a result and the premium
    reached neither the option nor the ETF.
    """

    def test_assigned_put_lowers_the_cost_of_the_delivered_etf(self, country):
        put = _option_on(ETF, put_call="P", strike="50", expiry="2025-03-21",
                         conid="900000111")
        trades = [
            # Written for 3.00: 300 received, 1 commission -> 299 kept.
            _trade_row(**put, trade_date="2025-03-03", quantity="-1", price="3.00",
                       buy_sell="SELL", open_close="O", tx_id="1001"),
            # Assigned: the option is closed at zero ...
            _trade_row(**put, trade_date="2025-03-21", quantity="1", price="0",
                       buy_sell="BUY", open_close="C", tx_id="1002",
                       commission="0", notes="A"),
            # ... and 100 ETF shares are delivered at the strike.
            _trade_row(**ETF, trade_date="2025-03-21", quantity="100", price="50",
                       buy_sell="BUY", open_close="O", tx_id="1003", notes="A"),
            _trade_row(**ETF, trade_date="2025-04-10", quantity="-100", price="56",
                       buy_sell="SELL", open_close="C", tx_id="1004"),
        ]

        out = self._run_pipeline(trades_data=trades, tax_year=TAX_YEAR,
                                 custom_rate_provider=_one_to_one(),
                                 country_code=country)

        (rgl,) = out.realized_gains_losses
        assert rgl.asset_category_at_realization is AssetCategory.INVESTMENT_FUND
        assert rgl.realization_type is RealizationType.LONG_POSITION_SALE
        # cost: 100 x 50 + 1 commission - 299 premium kept
        assert rgl.total_cost_basis_eur == Decimal("4702.00")
        # proceeds: 100 x 56 - 1 commission
        assert rgl.total_realization_value_eur == Decimal("5599.00")
        # The premium is in the result exactly once: 299 - 5001 + 5599.
        assert rgl.gross_gain_loss_eur == Decimal("897.00")

    def test_assigned_call_raises_the_proceeds_of_the_delivered_etf(self, country):
        call = _option_on(ETF, put_call="C", strike="45", expiry="2025-03-21",
                          conid="900000112")
        trades = [
            _trade_row(**ETF, trade_date="2025-02-03", quantity="100", price="40",
                       buy_sell="BUY", open_close="O", tx_id="1101"),
            # Covered call written for 2.00: 200 received, 1 commission.
            _trade_row(**call, trade_date="2025-03-03", quantity="-1", price="2.00",
                       buy_sell="SELL", open_close="O", tx_id="1102"),
            _trade_row(**call, trade_date="2025-03-21", quantity="1", price="0",
                       buy_sell="BUY", open_close="C", tx_id="1103",
                       commission="0", notes="A"),
            # Called away at the strike.
            _trade_row(**ETF, trade_date="2025-03-21", quantity="-100", price="45",
                       buy_sell="SELL", open_close="C", tx_id="1104", notes="A"),
        ]

        out = self._run_pipeline(trades_data=trades, tax_year=TAX_YEAR,
                                 custom_rate_provider=_one_to_one(),
                                 country_code=country)

        (rgl,) = out.realized_gains_losses
        assert rgl.asset_category_at_realization is AssetCategory.INVESTMENT_FUND
        # cost: 100 x 40 + 1 commission
        assert rgl.total_cost_basis_eur == Decimal("4001.00")
        # proceeds: 100 x 45 - 1 commission + 199 premium kept
        assert rgl.total_realization_value_eur == Decimal("4698.00")
        assert rgl.gross_gain_loss_eur == Decimal("697.00")


# ---------------------------------------------------------------------------
# V09 — a delivery is matched to its exercise/assignment on the side as well
# ---------------------------------------------------------------------------

class TestV09SpreadWithBothLegsInTheMoney(FifoTestCaseBase):
    """A call spread that expires with both legs in the money is exercised
    (shares bought at the lower strike) and assigned (shares sold at the
    higher one) on the same day, for the same quantity of the same stock.
    Date, underlying and quantity cannot tell the two deliveries apart; the
    side can. Matched without it, the first delivery row in the file took the
    first option event, and the two premiums changed places.
    """

    LONG_CALL = _option_on(STOCK, put_call="C", strike="40", expiry="2025-02-21",
                           conid="900000211")
    SHORT_CALL = _option_on(STOCK, put_call="C", strike="45", expiry="2025-02-21",
                            conid="900000212")

    @pytest.mark.parametrize("delivery_listed_first", ["buy", "sell"])
    def test_the_result_does_not_depend_on_the_order_of_the_rows(
            self, delivery_listed_first):
        bought = _trade_row(**STOCK, trade_date="2025-02-21", quantity="100",
                            price="40", buy_sell="BUY", open_close="O",
                            tx_id="2005", notes="Ex")
        sold = _trade_row(**STOCK, trade_date="2025-02-21", quantity="-100",
                          price="45", buy_sell="SELL", open_close="C",
                          tx_id="2006", notes="A")
        deliveries = [bought, sold] if delivery_listed_first == "buy" else [sold, bought]
        trades = [
            # Long call bought for 6.00: 600 paid + 1 commission = 601.
            _trade_row(**self.LONG_CALL, trade_date="2025-02-03", quantity="1",
                       price="6.00", buy_sell="BUY", open_close="O", tx_id="2001"),
            # Short call written for 2.50: 250 received - 1 commission = 249.
            _trade_row(**self.SHORT_CALL, trade_date="2025-02-03", quantity="-1",
                       price="2.50", buy_sell="SELL", open_close="O", tx_id="2002"),
            _trade_row(**self.LONG_CALL, trade_date="2025-02-21", quantity="-1",
                       price="0", buy_sell="SELL", open_close="C", tx_id="2003",
                       commission="0", notes="Ex"),
            _trade_row(**self.SHORT_CALL, trade_date="2025-02-21", quantity="1",
                       price="0", buy_sell="BUY", open_close="C", tx_id="2004",
                       commission="0", notes="A"),
            *deliveries,
        ]

        out = self._run_pipeline(trades_data=trades, tax_year=TAX_YEAR,
                                 custom_rate_provider=_one_to_one(),
                                 country_code="cz")

        (rgl,) = out.realized_gains_losses
        # bought by exercise: 100 x 40 + 1 commission + 601 paid for the call
        assert rgl.total_cost_basis_eur == Decimal("4602.00")
        # sold by assignment: 100 x 45 - 1 commission + 249 kept from the call
        assert rgl.total_realization_value_eur == Decimal("4748.00")
        # = the 500 the spread is worth, less 352 net premium and 2 commissions
        assert rgl.gross_gain_loss_eur == Decimal("146.00")


class _MapResolver:
    def __init__(self, *assets):
        self._by_id = {a.internal_asset_id: a for a in assets}

    def get_asset_by_id(self, asset_id):
        return self._by_id.get(asset_id)


class TestV09DeliverySideFollowsFromTheOptionAndTheEvent:
    """The holder of a call buys and the holder of a put sells; the writer
    who is assigned takes the other side."""

    @pytest.mark.parametrize("event_class, put_call, delivered_by", [
        (OptionExerciseEvent, "C", "buy"),
        (OptionAssignmentEvent, "P", "buy"),
        (OptionExerciseEvent, "P", "sell"),
        (OptionAssignmentEvent, "C", "sell"),
    ])
    def test_only_the_trade_on_the_expected_side_is_linked(
            self, event_class, put_call, delivered_by):
        stock = Stock(ibkr_symbol="SYNA", ibkr_conid="900000201")
        option = Option(option_type=put_call, ibkr_symbol=f"SYNA {put_call}",
                        underlying_ibkr_conid="900000201",
                        multiplier=Decimal("100"))
        event = event_class(asset_internal_id=option.internal_asset_id,
                            event_date="2025-02-21",
                            quantity_contracts=Decimal("1"),
                            ibkr_transaction_id="2100")

        def delivery(side, tx_id):
            buying = side == "buy"
            return TradeEvent(
                asset_internal_id=stock.internal_asset_id, event_date="2025-02-21",
                quantity=Decimal("100") if buying else Decimal("-100"),
                price_foreign_currency=Decimal("40"),
                event_type=(FinancialEventType.TRADE_BUY_LONG if buying
                            else FinancialEventType.TRADE_SELL_LONG),
                ibkr_transaction_id=tx_id,
            )

        other_side = "sell" if delivered_by == "buy" else "buy"
        wrong = delivery(other_side, "2101")     # listed first
        right = delivery(delivered_by, "2102")

        linker = OptionTradeLinker(_MapResolver(stock, option))
        linker.link_trades([wrong, right], linker._build_option_event_lookup([event]))

        assert wrong.related_option_event_id is None
        assert right.related_option_event_id == event.event_id


# ---------------------------------------------------------------------------
# X14 — an exercise/assignment without a delivery is a closing trade
# ---------------------------------------------------------------------------

class TestX14ExerciseOrAssignmentWithoutDelivery(FifoTestCaseBase):
    """An index option is settled in cash: nothing is delivered, and the
    option's own row carries the settlement amount. The processors expected a
    delivery to hand the premium to, raised when the option had no underlying
    in the inputs, and the factory had already replaced the row's amount by
    zero. Such a row is a closing trade of the option at that amount.
    """

    def test_exercised_index_call_is_sold_at_its_settlement_amount(self):
        call = _option_on(INDEX, put_call="C", strike="5000", expiry="2025-05-16",
                          conid="900000311")
        trades = [
            # Bought for 20.00: 2000 paid + 1.50 commission.
            _trade_row(**call, trade_date="2025-05-05", quantity="1", price="20.00",
                       buy_sell="BUY", open_close="O", tx_id="3001",
                       commission="-1.50"),
            # Settled in cash at 35.25: 3525 received - 0.50 charged on the row.
            _trade_row(**call, trade_date="2025-05-16", quantity="-1", price="35.25",
                       buy_sell="SELL", open_close="C", tx_id="3002",
                       commission="-0.50", notes="C;Ex"),
        ]

        out = self._run_pipeline(trades_data=trades, tax_year=TAX_YEAR,
                                 custom_rate_provider=_one_to_one(),
                                 country_code="cz")

        (rgl,) = out.realized_gains_losses
        assert rgl.realization_type is RealizationType.OPTION_TRADE_CLOSE_LONG
        assert rgl.quantity_realized == Decimal("1")
        assert rgl.acquisition_date == "2025-05-05"
        assert rgl.realization_date == "2025-05-16"
        assert rgl.total_cost_basis_eur == Decimal("2001.50")
        assert rgl.total_realization_value_eur == Decimal("3524.50")
        assert rgl.gross_gain_loss_eur == Decimal("1523.00")

    def test_assigned_index_put_is_bought_back_at_its_settlement_amount(self):
        put = _option_on(INDEX, put_call="P", strike="5100", expiry="2025-05-16",
                         conid="900000312")
        trades = [
            # Written for 30.00: 3000 received - 1.50 commission.
            _trade_row(**put, trade_date="2025-05-05", quantity="-1", price="30.00",
                       buy_sell="SELL", open_close="O", tx_id="3101",
                       commission="-1.50"),
            # Settled in cash at 42.00: 4200 paid.
            _trade_row(**put, trade_date="2025-05-16", quantity="1", price="42.00",
                       buy_sell="BUY", open_close="C", tx_id="3102",
                       commission="0", notes="A;C"),
        ]

        out = self._run_pipeline(trades_data=trades, tax_year=TAX_YEAR,
                                 custom_rate_provider=_one_to_one(),
                                 country_code="cz")

        (rgl,) = out.realized_gains_losses
        assert rgl.realization_type is RealizationType.OPTION_TRADE_CLOSE_SHORT
        assert rgl.quantity_realized == Decimal("1")
        assert rgl.total_cost_basis_eur == Decimal("4200.00")
        assert rgl.total_realization_value_eur == Decimal("2998.50")
        assert rgl.gross_gain_loss_eur == Decimal("-1201.50")

    def test_premium_is_not_lost_when_the_delivery_row_is_missing(self):
        """The underlying is known and shares should have come, but no
        delivery row does. The premium used to wait for it and then vanish;
        it is realised on the option, which the row closes at zero."""
        put = _option_on(STOCK, put_call="P", strike="50", expiry="2025-03-21",
                         conid="900000213")
        trades = [
            _trade_row(**STOCK, trade_date="2025-01-10", quantity="10", price="48",
                       buy_sell="BUY", open_close="O", tx_id="3201"),
            # Written for 3.00: 300 received - 1 commission.
            _trade_row(**put, trade_date="2025-03-03", quantity="-1", price="3.00",
                       buy_sell="SELL", open_close="O", tx_id="3202"),
            _trade_row(**put, trade_date="2025-03-21", quantity="1", price="0",
                       buy_sell="BUY", open_close="C", tx_id="3203",
                       commission="0", notes="A"),
        ]
        held = [[ACCOUNT, "EUR", "STK", "COMMON", "SYNA", "SYNTHETIC ALPHA INC",
                 "US0000000201", "10", "480", "48", "481", None, "900000201",
                 None, "1"]]

        out = self._run_pipeline(trades_data=trades, positions_end_data=held,
                                 tax_year=TAX_YEAR,
                                 custom_rate_provider=_one_to_one(),
                                 country_code="cz")

        gains = [(r.realization_type, r.gross_gain_loss_eur)
                 for r in out.realized_gains_losses]
        assert gains == [(RealizationType.OPTION_TRADE_CLOSE_SHORT, Decimal("299.00"))]


# ---------------------------------------------------------------------------
# X15 — a merger names the acquirer by ISIN
# ---------------------------------------------------------------------------

def _resolver(tmp_path):
    """A resolver whose classification cache lives in the test's temp dir."""
    return AssetResolver(AssetClassifier(
        cache_file_path=str(tmp_path / "classifications.json")))


def _corporate_action(**fields):
    row = {"ClientAccountID": ACCOUNT, "CurrencyPrimary": "USD",
           "AssetClass": "STK", "Proceeds": "0", "Value": "0"}
    row.update(fields)
    return RawCorporateActionRecord(**row)


class TestX15MergerNamesTheAcquirerByIsin:
    """IBKR writes a stock-for-stock merger as
    "OLD(isin) MERGED(Acquisition) WITH <ISIN of the acquirer> n FOR m (...)".
    The token after WITH was taken for a symbol, so a new asset with that
    "symbol" and neither ISIN nor conid was created, and both rows of the
    merger pointed at it instead of at the acquirer already in the book.
    """

    MERGER = ("OLDC(US0000000401) MERGED(Acquisition) WITH US0000000402 1 FOR 2 "
              "({symbol}, {name}, {isin})")

    def test_both_rows_resolve_to_the_acquirer_already_in_the_book(self, tmp_path):
        resolver = _resolver(tmp_path)
        old = resolver.get_or_create_asset(
            raw_isin="US0000000401", raw_conid="900000401", raw_symbol="OLDC",
            raw_currency="USD", raw_ibkr_asset_class="STK",
            raw_description="OLDCO INC", description_source_type="trade")
        acquirer = resolver.get_or_create_asset(
            raw_isin="US0000000402", raw_conid="900000402", raw_symbol="NEWC",
            raw_currency="USD", raw_ibkr_asset_class="STK",
            raw_description="NEWCO CORP", description_source_type="position")
        rows = [
            _corporate_action(
                Symbol="OLDC", ISIN="US0000000401", Conid="900000401",
                Description=self.MERGER.format(symbol="OLDC", name="OLDCO INC",
                                               isin="US0000000401"),
                Type="TC", ActionID="7001", Quantity="-100",
                **{"Report Date": "2025-06-10"}),
            _corporate_action(
                Symbol="NEWC", ISIN="US0000000402", Conid="900000402",
                Description=self.MERGER.format(symbol="NEWC", name="NEWCO CORP",
                                               isin="US0000000402"),
                Type="TC", ActionID="7001", Quantity="50",
                **{"Report Date": "2025-06-10"}),
        ]

        events = DomainEventFactory(resolver).create_events_from_corporate_actions(rows)

        # One merger: the disposing row. The receiving row names the same
        # asset on both sides and is dropped as before.
        mergers = [(e.asset_internal_id, e.new_asset_internal_id,
                    e.new_shares_received_per_old)
                   for e in events if isinstance(e, CorpActionMergerStock)]
        assert mergers == [(old.internal_asset_id, acquirer.internal_asset_id,
                            Decimal("0.5"))]
        # ... and no asset was invented on the way.
        assert set(resolver.assets_by_internal_id) == {
            old.internal_asset_id, acquirer.internal_asset_id}

    def test_an_acquirer_seen_for_the_first_time_is_created_under_its_isin(self, tmp_path):
        resolver = _resolver(tmp_path)
        rows = [_corporate_action(
            Symbol="OLDC", ISIN="US0000000401", Conid="900000401",
            Description=self.MERGER.format(symbol="OLDC", name="OLDCO INC",
                                           isin="US0000000401"),
            Type="TC", ActionID="7001", Quantity="-100",
            **{"Report Date": "2025-06-10"})]

        (merger,) = [e for e in DomainEventFactory(resolver)
                     .create_events_from_corporate_actions(rows)
                     if isinstance(e, CorpActionMergerStock)]

        new_asset = resolver.get_asset_by_id(merger.new_asset_internal_id)
        assert new_asset.ibkr_isin == "US0000000402"
        # A later row that carries the ISIN finds this asset, not a second one.
        assert resolver.get_asset_by_alias("ISIN:US0000000402") is new_asset
