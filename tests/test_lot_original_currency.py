# tests/test_lot_original_currency.py
"""A lot's cost in the currency it was actually paid in.

The engine computes everything in EUR — it began as a German tool — so a USD
holding reported a cost the owner never paid, in a currency they never touched:
AAPL bought at 304.28 USD showed as 263.79 EUR. The original is now carried
alongside, for display and audit only.

The invariant these tests exist to protect: the two costs on a lot are the SAME
amount in two currencies, never two different amounts. That holds by
construction at the buy (the original mirrors `enrichment`'s formula term for
term) and has to be maintained at every site that touches the basis afterwards —
which is where a silent divergence would live.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

import src.config as global_config
from src.domain.enums import AssetCategory, FinancialEventType
from src.domain.events import CorpActionSplitForward, TradeEvent
from src.engine.fifo_manager import FifoLedger, FifoLot
from src.utils.currency_converter import CurrencyConverter
from tests.support.mock_providers import MockECBExchangeRateProvider

# A rate that is not 1, so a mirrored figure cannot pass by coincidence.
USD_TO_EUR = Decimal("0.8")


def _ledger(asset_category=AssetCategory.STOCK, multiplier=None):
    provider = MockECBExchangeRateProvider(foreign_to_eur_init_value=USD_TO_EUR)
    return FifoLedger(
        asset_internal_id=uuid.uuid4(),
        asset_category=asset_category,
        asset_multiplier_from_asset=multiplier,
        currency_converter=CurrencyConverter(rate_provider=provider),
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
        tax_classifier=None,
    )


def _buy(ledger, *, quantity, gross, commission=Decimal(0), currency="USD",
         commission_currency=None, when="2024-01-05", tx_id="T1"):
    """A BUY carrying both the original amounts and the EUR ones.

    `gross_amount_foreign_currency` is set explicitly because that is what the
    parser does — and for an option it already includes the multiplier, which is
    exactly why the lot must not re-apply it.
    """
    ev = TradeEvent(
        asset_internal_id=ledger.asset_internal_id,
        event_date=when,
        quantity=Decimal(str(quantity)),
        price_foreign_currency=Decimal(str(gross)) / Decimal(str(quantity)),
        event_type=FinancialEventType.TRADE_BUY_LONG,
        ibkr_transaction_id=tx_id,
        gross_amount_foreign_currency=Decimal(str(gross)),
        local_currency=currency,
    )
    ev.commission_foreign_currency = Decimal(str(commission))
    ev.commission_currency = commission_currency or (
        currency if commission else None)
    # What enrichment would have produced: the same sum, converted.
    total_original = Decimal(str(gross)) + Decimal(str(commission))
    ev.net_proceeds_or_cost_basis_eur = total_original * USD_TO_EUR
    ledger.add_long_lot(ev)
    return ledger.lots[-1]


class TestTheFieldPair:
    def test_an_amount_without_its_currency_is_refused(self):
        """Half the pair is not a cost: the amount would render as if it were in
        whatever currency the reader assumes, and a currency with no amount
        reads as zero."""
        with pytest.raises(ValueError, match="together"):
            FifoLot(acquisition_date="2024-01-05", quantity=Decimal("1"),
                    unit_cost_basis_eur=Decimal("1"),
                    total_cost_basis_eur=Decimal("1"),
                    source_transaction_id="T",
                    total_cost_original=Decimal("2"))

    def test_a_currency_without_an_amount_is_refused(self):
        with pytest.raises(ValueError, match="together"):
            FifoLot(acquisition_date="2024-01-05", quantity=Decimal("1"),
                    unit_cost_basis_eur=Decimal("1"),
                    total_cost_basis_eur=Decimal("1"),
                    source_transaction_id="T",
                    cost_currency="USD")

    def test_neither_is_the_normal_case(self):
        lot = FifoLot(acquisition_date="2024-01-05", quantity=Decimal("1"),
                      unit_cost_basis_eur=Decimal("1"),
                      total_cost_basis_eur=Decimal("1"),
                      source_transaction_id="T")
        assert lot.total_cost_original is None
        assert lot.unit_cost_original is None


class TestBuy:
    def test_the_original_is_gross_plus_commission_in_the_trade_currency(self):
        """The real case: 2 AAPL at 303.78 plus 1.00 of commission is 608.56 USD
        — 304.28 per share, which is the number the owner recognises."""
        ledger = _ledger()
        lot = _buy(ledger, quantity=2, gross="607.56", commission="1.00")
        assert lot.cost_currency == "USD"
        assert lot.total_cost_original == Decimal("608.56")
        assert lot.unit_cost_original == Decimal("304.28")

    def test_the_two_costs_are_one_amount_in_two_currencies(self):
        """Not two independently-derived figures: their ratio must be exactly
        the rate the conversion used, or one of them is wrong."""
        ledger = _ledger()
        lot = _buy(ledger, quantity=2, gross="607.56", commission="1.00")
        assert (lot.total_cost_basis_eur
                == lot.total_cost_original * USD_TO_EUR)

    def test_a_rebate_lowers_the_original_just_as_it_lowers_the_eur_basis(self):
        """IBKR pays commission back sometimes. The commission is signed as a
        cost, so a negative one has to reduce the basis on both sides."""
        ledger = _ledger()
        lot = _buy(ledger, quantity=1, gross="100", commission="-0.5")
        assert lot.total_cost_original == Decimal("99.5")
        assert lot.total_cost_basis_eur == Decimal("99.5") * USD_TO_EUR

    def test_a_eur_trade_reports_eur(self):
        ledger = _ledger()
        lot = _buy(ledger, quantity=1, gross="100", currency="EUR")
        assert lot.cost_currency == "EUR"
        assert lot.total_cost_original == Decimal("100")

    def test_a_commission_billed_in_a_third_currency_voids_the_original(self):
        """Adding it would sum two currencies into one number; dropping it would
        disagree with the EUR basis by the fee. Neither is worth showing."""
        ledger = _ledger()
        lot = _buy(ledger, quantity=1, gross="100", commission="2",
                   currency="USD", commission_currency="GBP")
        assert lot.total_cost_original is None
        assert lot.cost_currency is None

    def test_a_zero_commission_in_a_third_currency_is_harmless(self):
        """Zero adds nothing, so the currency mismatch cannot corrupt the sum."""
        ledger = _ledger()
        lot = _buy(ledger, quantity=1, gross="100", commission="0",
                   currency="USD", commission_currency="GBP")
        assert lot.total_cost_original == Decimal("100")

    def test_an_option_is_not_multiplied_twice(self):
        """The parser already put the multiplier into the gross, so the lot's
        original is per CONTRACT — directly comparable to the EUR basis, which
        is also per contract. Re-applying it here would put the display 100x
        above the figure beside it."""
        ledger = _ledger(asset_category=AssetCategory.OPTION,
                         multiplier=Decimal("100"))
        # 2 contracts at 3.50 per share => gross 700 as the parser reports it
        lot = _buy(ledger, quantity=2, gross="700", commission="1.30")
        assert lot.total_cost_original == Decimal("701.30")
        assert lot.unit_cost_original == Decimal("350.65")   # per contract
        assert (lot.total_cost_basis_eur
                == lot.total_cost_original * USD_TO_EUR)


class TestItStaysInStepWithTheEurBasis:
    """Every site that changes the EUR basis has to leave the pair consistent."""

    @staticmethod
    def _assert_consistent(lot):
        if lot.total_cost_original is None:
            return
        assert (lot.total_cost_basis_eur
                == pytest.approx(lot.total_cost_original * USD_TO_EUR,
                                 rel=Decimal("1e-20")))

    def _sell(self, ledger, quantity, price, when="2024-06-05", tx_id="S1"):
        ev = TradeEvent(
            asset_internal_id=ledger.asset_internal_id,
            event_date=when, quantity=-Decimal(str(quantity)),
            price_foreign_currency=Decimal(str(price)),
            event_type=FinancialEventType.TRADE_SELL_LONG,
            ibkr_transaction_id=tx_id,
            gross_amount_foreign_currency=Decimal(str(quantity)) * Decimal(str(price)),
            local_currency="USD",
        )
        ev.commission_foreign_currency = Decimal(0)
        ev.commission_currency = "USD"
        ev.net_proceeds_or_cost_basis_eur = (
            Decimal(str(quantity)) * Decimal(str(price)) * USD_TO_EUR)
        return ledger.consume_long_lots_for_sale(ev)

    def test_a_partial_sale_shrinks_both_costs_by_the_same_ratio(self):
        ledger = _ledger()
        _buy(ledger, quantity=10, gross="1000", commission="2")
        self._sell(ledger, 4, "150")
        [lot] = ledger.lots
        assert lot.quantity == Decimal("6")
        # 1002 USD for 10 shares -> 601.20 for the 6 that remain
        assert lot.total_cost_original == Decimal("601.2")
        self._assert_consistent(lot)

    def test_repeated_partial_sales_do_not_drift(self):
        """The ratio is applied to the CURRENT total each time, so three bites
        must land on the same figure as one bite of the same size."""
        ledger = _ledger()
        _buy(ledger, quantity=10, gross="1000", commission="0")
        for i, q in enumerate((2, 2, 2)):
            self._sell(ledger, q, "150", tx_id=f"S{i}")
        [lot] = ledger.lots
        assert lot.quantity == Decimal("4")
        assert lot.total_cost_original == Decimal("400")
        self._assert_consistent(lot)

    def test_a_split_keeps_the_total_and_rescales_the_unit(self):
        """The reason the TOTAL is the stored field: a split changes how many
        shares the same money bought, so nothing has to be mirrored at all."""
        ledger = _ledger()
        _buy(ledger, quantity=10, gross="1000", commission="0")
        ledger.adjust_lots_for_split(CorpActionSplitForward(
            asset_internal_id=ledger.asset_internal_id,
            event_date="2024-06-05", new_shares_per_old_share=Decimal("2")))
        [lot] = ledger.lots
        assert lot.quantity == Decimal("20")
        assert lot.total_cost_original == Decimal("1000")
        assert lot.unit_cost_original == Decimal("50")
        self._assert_consistent(lot)

    def test_a_capital_repayment_drops_the_original(self):
        """The repayment is an EUR amount. Subtracting it from a USD figure
        needs a rate this code has no business choosing, and scaling by the EUR
        ratio would invent one — so the pair is dropped and the EUR basis, which
        does reflect the repayment, stands alone."""
        ledger = _ledger()
        _buy(ledger, quantity=10, gross="1000", commission="0")
        before = ledger.lots[0].total_cost_basis_eur
        ledger.reduce_cost_basis_for_capital_repayment(Decimal("100"))
        [lot] = ledger.lots
        assert lot.total_cost_basis_eur < before      # the EUR basis did move
        assert lot.total_cost_original is None
        assert lot.cost_currency is None
