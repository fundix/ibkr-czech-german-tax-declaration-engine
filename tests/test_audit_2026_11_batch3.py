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
* V23 — a split reported as two rows (old and new ISIN, one ActionID) became
  two events: the ratio was applied twice, or the old asset was rescaled
  while the new one stayed empty.

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


def _ca_row(*, symbol, description, isin, conid, report_date, ca_type,
            action_id, quantity, proceeds="0", value="0", currency="EUR"):
    """One Corporate Actions row, in the order of ``CORPORATE_ACTIONS_HEADERS``."""
    return [
        ACCOUNT, symbol, description, isin, report_date, None, ca_type, action_id,
        conid, None, None, currency, "0", proceeds, value, quantity,
    ]


def _position_row(security, quantity, mark_price="1", currency="EUR"):
    """One Positions row, in the order of ``POSITIONS_FILE_HEADERS``."""
    return [
        ACCOUNT, currency, security["asset_class"], security["sub_category"],
        security["symbol"], security["description"], security["isin"], quantity,
        str(Decimal(quantity) * Decimal(mark_price)), mark_price, "0", None,
        security["conid"], None, "1",
    ]


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


# ---------------------------------------------------------------------------
# V23 — a split reported as two rows is one event
# ---------------------------------------------------------------------------

class TestV23SplitReportedAsTwoRows(FifoTestCaseBase):
    """A split that comes with a new ISIN is reported as two rows under one
    ActionID: the old security with a negative quantity, the new one with a
    positive quantity. Each row became a split event of its own.
    """

    SPLIT = "SYNR(US0000000501) SPLIT 1 FOR 5 ({symbol}, SYNTHETIC RHO INC, {isin})"
    OLD = dict(asset_class="STK", sub_category="COMMON", symbol="SYNR",
               description="SYNTHETIC RHO INC", isin="US0000000501",
               conid="900000501")

    def _split_rows(self, *, old_symbol, new):
        return [
            _ca_row(symbol=old_symbol, isin="US0000000501", conid="900000501",
                    description=self.SPLIT.format(symbol=old_symbol,
                                                  isin="US0000000501"),
                    report_date="2025-06-02", ca_type="RS", action_id="8001",
                    quantity="-100"),
            _ca_row(symbol=new["symbol"], isin=new["isin"], conid=new["conid"],
                    description=self.SPLIT.format(symbol=new["symbol"],
                                                  isin=new["isin"]),
                    report_date="2025-06-02", ca_type="RS", action_id="8001",
                    quantity="20"),
        ]

    def test_ratio_is_applied_once_when_both_rows_are_the_same_asset(self):
        """IBKR's usual shape: the old row is renamed "SYNR.OLD", the new
        security keeps the ticker, so both rows resolve to one asset -- and
        the 1-for-5 ratio was applied twice, leaving 4 shares instead of 20.
        """
        new = dict(self.OLD, isin="US0000000502", conid="900000502")
        trades = [
            # 100 bought at 2.00 + 1 commission = 201.
            _trade_row(**self.OLD, trade_date="2025-01-15", quantity="100",
                       price="2.00", buy_sell="BUY", open_close="O", tx_id="4001"),
            # The 20 shares left after the split, sold at 12.00 - 1 commission.
            _trade_row(**new, trade_date="2025-09-10", quantity="-20",
                       price="12.00", buy_sell="SELL", open_close="C", tx_id="4002"),
        ]

        out = self._run_pipeline(
            trades_data=trades,
            corporate_actions_data=self._split_rows(old_symbol="SYNR.OLD", new=new),
            tax_year=TAX_YEAR, custom_rate_provider=_one_to_one(), country_code="cz")

        (rgl,) = out.realized_gains_losses
        assert rgl.quantity_realized == Decimal("20")
        assert rgl.acquisition_date == "2025-01-15"
        assert rgl.total_cost_basis_eur == Decimal("201.00")
        assert rgl.total_realization_value_eur == Decimal("239.00")
        assert rgl.gross_gain_loss_eur == Decimal("38.00")
        assert out.eoy_mismatch_error_count == 0

    def test_lots_move_to_the_new_asset_when_the_rows_are_two_assets(self):
        """The new security has its own ticker, so the rows are two assets.
        The old one was rescaled and kept its shares; the new one had none,
        and selling them ended the run."""
        new = dict(asset_class="STK", sub_category="COMMON", symbol="SYNN",
                   description="SYNTHETIC RHO NEW INC", isin="US0000000502",
                   conid="900000502")
        trades = [
            # 100 bought at 2.00 + 1 commission = 201.
            _trade_row(**self.OLD, trade_date="2025-01-15", quantity="100",
                       price="2.00", buy_sell="BUY", open_close="O", tx_id="4101"),
            # 5 of the 20 new shares sold at 12.00 - 1 commission = 59.
            _trade_row(**new, trade_date="2025-09-10", quantity="-5",
                       price="12.00", buy_sell="SELL", open_close="C", tx_id="4102"),
        ]

        out = self._run_pipeline(
            trades_data=trades,
            corporate_actions_data=self._split_rows(old_symbol="SYNR", new=new),
            positions_end_data=[_position_row(new, "15", mark_price="12")],
            tax_year=TAX_YEAR, custom_rate_provider=_one_to_one(), country_code="cz")

        resolver = out.asset_resolver
        old_asset = resolver.get_asset_by_alias("ISIN:US0000000501")
        new_asset = resolver.get_asset_by_alias("ISIN:US0000000502")
        assert old_asset is not new_asset

        (rgl,) = out.realized_gains_losses
        assert rgl.asset_internal_id == new_asset.internal_asset_id
        assert rgl.quantity_realized == Decimal("5")
        # The purchase date and the cost travel with the shares.
        assert rgl.acquisition_date == "2025-01-15"
        # 201 for 20 shares -> 50.25 for 5
        assert rgl.total_cost_basis_eur == Decimal("50.25")
        assert rgl.total_realization_value_eur == Decimal("59.00")
        assert rgl.gross_gain_loss_eur == Decimal("8.75")

        ledgers = out.fifo_ledgers_by_asset_id
        assert ledgers[old_asset.internal_asset_id].lots == []
        (lot,) = ledgers[new_asset.internal_asset_id].lots
        assert lot.quantity == Decimal("15")
        assert lot.total_cost_basis_eur == Decimal("150.75")
        assert lot.acquisition_date == "2025-01-15"
        assert out.eoy_mismatch_error_count == 0

    def test_a_ratio_without_a_finite_decimal_still_pairs_the_rows(self):
        """1 for 3: the ratio is 0.333..., and 300 times that is not exactly
        100 -- the pair must still be recognised."""
        split = "SYNR(US0000000501) SPLIT 1 FOR 3 ({symbol}, {name}, {isin})"
        new = dict(asset_class="STK", sub_category="COMMON", symbol="SYNN",
                   description="SYNTHETIC RHO NEW INC", isin="US0000000502",
                   conid="900000502")
        trades = [
            # 300 bought at 2.00 + 1 commission = 601.
            _trade_row(**self.OLD, trade_date="2025-01-15", quantity="300",
                       price="2.00", buy_sell="BUY", open_close="O", tx_id="4501"),
            # The 100 shares after the split sold at 7.00 - 1 commission = 699.
            _trade_row(**new, trade_date="2025-09-10", quantity="-100",
                       price="7.00", buy_sell="SELL", open_close="C", tx_id="4502"),
        ]
        rows = [
            _ca_row(symbol="SYNR", isin="US0000000501", conid="900000501",
                    description=split.format(symbol="SYNR", name="SYNTHETIC RHO INC",
                                             isin="US0000000501"),
                    report_date="2025-06-02", ca_type="RS", action_id="8004",
                    quantity="-300"),
            _ca_row(symbol="SYNN", isin="US0000000502", conid="900000502",
                    description=split.format(symbol="SYNN",
                                             name="SYNTHETIC RHO NEW INC",
                                             isin="US0000000502"),
                    report_date="2025-06-02", ca_type="RS", action_id="8004",
                    quantity="100"),
        ]

        out = self._run_pipeline(
            trades_data=trades, corporate_actions_data=rows, tax_year=TAX_YEAR,
            custom_rate_provider=_one_to_one(), country_code="cz")

        (rgl,) = out.realized_gains_losses
        assert rgl.quantity_realized == Decimal("100")
        assert rgl.acquisition_date == "2025-01-15"
        assert rgl.total_cost_basis_eur == Decimal("601.00")
        assert rgl.gross_gain_loss_eur == Decimal("98.00")
        assert out.eoy_mismatch_error_count == 0

    @pytest.mark.parametrize("holding", ["long", "short"])
    def test_a_one_for_one_exchange_moves_the_holding_once(self, holding):
        """With a ratio of 1 the two rows carry the same number and only the
        sign tells them apart -- and the signs are the other way round for a
        short holding. Either row could be read as the old one; taking both
        readings cancelled the event altogether."""
        split = "SYNR(US0000000501) SPLIT 1 FOR 1 ({symbol}, {name}, {isin})"
        new = dict(asset_class="STK", sub_category="COMMON", symbol="SYNN",
                   description="SYNTHETIC RHO NEW INC", isin="US0000000502",
                   conid="900000502")
        long = holding == "long"
        trades = [
            # 100 at 2.00: bought for 201, or sold short for 199.
            _trade_row(**self.OLD, trade_date="2025-01-15",
                       quantity="100" if long else "-100", price="2.00",
                       buy_sell="BUY" if long else "SELL", open_close="O",
                       tx_id="4601"),
            # Closed at 3.00 under the new security: 299 received, or 301 paid.
            _trade_row(**new, trade_date="2025-09-10",
                       quantity="-100" if long else "100", price="3.00",
                       buy_sell="SELL" if long else "BUY", open_close="C",
                       tx_id="4602"),
        ]
        rows = [
            _ca_row(symbol="SYNR", isin="US0000000501", conid="900000501",
                    description=split.format(symbol="SYNR", name="SYNTHETIC RHO INC",
                                             isin="US0000000501"),
                    report_date="2025-06-02", ca_type="FS", action_id="8005",
                    quantity="-100" if long else "100"),
            _ca_row(symbol="SYNN", isin="US0000000502", conid="900000502",
                    description=split.format(symbol="SYNN",
                                             name="SYNTHETIC RHO NEW INC",
                                             isin="US0000000502"),
                    report_date="2025-06-02", ca_type="FS", action_id="8005",
                    quantity="100" if long else "-100"),
        ]

        out = self._run_pipeline(
            trades_data=trades, corporate_actions_data=rows, tax_year=TAX_YEAR,
            custom_rate_provider=_one_to_one(), country_code="cz")

        (rgl,) = out.realized_gains_losses
        assert rgl.quantity_realized == Decimal("100")
        assert rgl.acquisition_date == "2025-01-15"
        # long: 299 - 201; short: 199 - 301
        assert rgl.gross_gain_loss_eur == (Decimal("98.00") if long else Decimal("-102.00"))
        assert out.eoy_mismatch_error_count == 0

    def test_short_position_follows_the_split_to_the_new_asset(self):
        """For a short holding the rows' signs are the other way round: the
        old security comes with a positive quantity (the short is taken off
        it) and the new one with a negative quantity."""
        new = dict(asset_class="STK", sub_category="COMMON", symbol="SYNN",
                   description="SYNTHETIC RHO NEW INC", isin="US0000000502",
                   conid="900000502")
        trades = [
            # 100 sold short at 2.00 - 1 commission = 199 received.
            _trade_row(**self.OLD, trade_date="2025-01-15", quantity="-100",
                       price="2.00", buy_sell="SELL", open_close="O", tx_id="4201"),
            # The 20 shares owed after the split bought back at 9.00 + 1 = 181.
            _trade_row(**new, trade_date="2025-09-10", quantity="20",
                       price="9.00", buy_sell="BUY", open_close="C", tx_id="4202"),
        ]
        rows = [
            _ca_row(symbol="SYNR", isin="US0000000501", conid="900000501",
                    description=self.SPLIT.format(symbol="SYNR", isin="US0000000501"),
                    report_date="2025-06-02", ca_type="RS", action_id="8001",
                    quantity="100"),
            _ca_row(symbol="SYNN", isin="US0000000502", conid="900000502",
                    description=self.SPLIT.format(symbol="SYNN", isin="US0000000502"),
                    report_date="2025-06-02", ca_type="RS", action_id="8001",
                    quantity="-20"),
        ]

        out = self._run_pipeline(
            trades_data=trades, corporate_actions_data=rows, tax_year=TAX_YEAR,
            custom_rate_provider=_one_to_one(), country_code="cz")

        (rgl,) = out.realized_gains_losses
        assert rgl.realization_type is RealizationType.SHORT_POSITION_COVER
        assert rgl.quantity_realized == Decimal("20")
        assert rgl.total_realization_value_eur == Decimal("199.00")
        assert rgl.total_cost_basis_eur == Decimal("181.00")
        assert rgl.gross_gain_loss_eur == Decimal("18.00")
        assert out.eoy_mismatch_error_count == 0

    def test_option_rows_of_the_same_action_do_not_break_up_the_stock_split(self):
        """The action's ActionID is also on the rows that adjust the option
        contracts on the stock. They are other securities and must not turn
        the stock's two rows back into two events."""
        forward = "SYNR(US0000000501) SPLIT 2 FOR 1 ({symbol}, {name}, {isin})"
        new = dict(self.OLD, isin="US0000000502", conid="900000502")
        old_call = _option_on(self.OLD, put_call="C", strike="12",
                              expiry="2025-12-19", conid="900000511")
        new_call = _option_on(self.OLD, put_call="C", strike="6",
                              expiry="2025-12-19", conid="900000512")
        trades = [
            # 100 bought at 10 + 1 commission = 1001.
            _trade_row(**self.OLD, trade_date="2025-01-15", quantity="100",
                       price="10", buy_sell="BUY", open_close="O", tx_id="4301"),
            _trade_row(**old_call, trade_date="2025-02-03", quantity="1",
                       price="1.00", buy_sell="BUY", open_close="O", tx_id="4302"),
            # All 200 shares held after the split sold at 6 - 1 commission.
            _trade_row(**new, trade_date="2025-09-10", quantity="-200",
                       price="6", buy_sell="SELL", open_close="C", tx_id="4303"),
        ]
        rows = [
            _ca_row(symbol="SYNR.OLD", isin="US0000000501", conid="900000501",
                    description=forward.format(symbol="SYNR.OLD",
                                               name="SYNTHETIC RHO INC",
                                               isin="US0000000501"),
                    report_date="2025-06-02", ca_type="FS", action_id="8002",
                    quantity="-100"),
            _ca_row(symbol="SYNR", isin="US0000000502", conid="900000502",
                    description=forward.format(symbol="SYNR",
                                               name="SYNTHETIC RHO INC",
                                               isin="US0000000502"),
                    report_date="2025-06-02", ca_type="FS", action_id="8002",
                    quantity="200"),
            _ca_row(symbol=old_call["symbol"], isin="", conid="900000511",
                    description=forward.format(symbol=old_call["symbol"],
                                               name=old_call["description"], isin=""),
                    report_date="2025-06-02", ca_type="FS", action_id="8002",
                    quantity="-1"),
            _ca_row(symbol=new_call["symbol"], isin="", conid="900000512",
                    description=forward.format(symbol=new_call["symbol"],
                                               name=new_call["description"], isin=""),
                    report_date="2025-06-02", ca_type="FS", action_id="8002",
                    quantity="2"),
        ]

        out = self._run_pipeline(
            trades_data=trades, corporate_actions_data=rows, tax_year=TAX_YEAR,
            custom_rate_provider=_one_to_one(), country_code="cz")

        stock = out.asset_resolver.get_asset_by_alias("ISIN:US0000000501")
        (rgl,) = [r for r in out.realized_gains_losses
                  if r.asset_internal_id == stock.internal_asset_id]
        assert rgl.quantity_realized == Decimal("200")
        assert rgl.total_cost_basis_eur == Decimal("1001.00")
        assert rgl.total_realization_value_eur == Decimal("1199.00")
        assert out.fifo_ledgers_by_asset_id[stock.internal_asset_id].lots == []

    def test_written_option_follows_its_adjusted_contract(self):
        """A 2-for-1 split turns one written put into two at half the strike,
        under a new contract: +1 on the old contract (the short is taken off
        it), -2 on the new one. They are a pair of rows of their own, next to
        the single row of the ETF itself."""
        forward = "SYNE(US0000000101) SPLIT 2 FOR 1 ({symbol}, {name}, {isin})"
        old_put = _option_on(ETF, put_call="P", strike="50", expiry="2025-12-19",
                             conid="900000121")
        new_put = _option_on(ETF, put_call="P", strike="25", expiry="2025-12-19",
                             conid="900000122")
        trades = [
            # 100 ETF shares at 40 + 1 commission = 4001.
            _trade_row(**ETF, trade_date="2025-01-15", quantity="100", price="40",
                       buy_sell="BUY", open_close="O", tx_id="4401"),
            # One put written for 3.00: 300 - 1 commission = 299 received.
            _trade_row(**old_put, trade_date="2025-02-03", quantity="-1",
                       price="3.00", buy_sell="SELL", open_close="O", tx_id="4402"),
            # The two adjusted puts bought back at 1.00: 200 + 1 commission.
            _trade_row(**new_put, trade_date="2025-09-10", quantity="2",
                       price="1.00", buy_sell="BUY", open_close="C", tx_id="4403"),
        ]
        rows = [
            _ca_row(symbol="SYNE", isin="US0000000101", conid="900000101",
                    description=forward.format(symbol="SYNE",
                                               name="SYNTHETIC INDEX ETF",
                                               isin="US0000000101"),
                    report_date="2025-06-02", ca_type="FS", action_id="8003",
                    quantity="100"),
            _ca_row(symbol=old_put["symbol"], isin="", conid="900000121",
                    description=forward.format(symbol=old_put["symbol"],
                                               name=old_put["description"], isin=""),
                    report_date="2025-06-02", ca_type="FS", action_id="8003",
                    quantity="1"),
            _ca_row(symbol=new_put["symbol"], isin="", conid="900000122",
                    description=forward.format(symbol=new_put["symbol"],
                                               name=new_put["description"], isin=""),
                    report_date="2025-06-02", ca_type="FS", action_id="8003",
                    quantity="-2"),
        ]

        out = self._run_pipeline(
            trades_data=trades, corporate_actions_data=rows,
            positions_end_data=[_position_row(ETF, "200", mark_price="20")],
            tax_year=TAX_YEAR, custom_rate_provider=_one_to_one(), country_code="cz")

        (rgl,) = out.realized_gains_losses
        assert rgl.realization_type is RealizationType.OPTION_TRADE_CLOSE_SHORT
        assert rgl.quantity_realized == Decimal("2")
        assert rgl.acquisition_date == "2025-02-03"
        assert rgl.total_realization_value_eur == Decimal("299.00")
        assert rgl.total_cost_basis_eur == Decimal("201.00")
        assert rgl.gross_gain_loss_eur == Decimal("98.00")

        etf = out.asset_resolver.get_asset_by_alias("ISIN:US0000000101")
        (lot,) = out.fifo_ledgers_by_asset_id[etf.internal_asset_id].lots
        assert lot.quantity == Decimal("200")
        assert lot.total_cost_basis_eur == Decimal("4001.00")
        assert out.eoy_mismatch_error_count == 0
