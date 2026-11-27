# tests/test_audit_2026_11_batch3.py
"""
Regression tests for the option and corporate-action findings of the 2026-11
audit:

* V08 — an option on an ETF lost its premium when it was assigned or
  exercised: only stocks were accepted as the delivery leg.

Everything here is synthetic: invented symbols, ISINs, conids and amounts.
Expected figures are worked out by hand in each test, in EUR so that no
exchange rate takes part.
"""
from decimal import Decimal

import pytest

from src.domain.enums import AssetCategory, RealizationType
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
