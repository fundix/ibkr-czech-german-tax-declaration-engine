"""Tests for §10 pairing methods (FIFO / LIFO / weighted average / optimal).



buy 1 unit each at 750, 730, 900, 850, then sell 1 unit at 880.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

import src.config as global_config
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import TradeEvent
from src.engine.fifo_manager import FifoLedger
from src.engine.pairing import PairingMethod, consumption_order_indices
from src.engine.pairing_solver import SaleDemand, SupplyLot, solve_optimal_matching
from src.utils.currency_converter import CurrencyConverter
from tests.support.base import FifoTestCaseBase
from tests.support.mock_providers import MockECBExchangeRateProvider


def _ledger(pairing_method, asset_category=AssetCategory.STOCK):
    provider = MockECBExchangeRateProvider(
        foreign_to_eur_init_value=Decimal("1.0"))
    converter = CurrencyConverter(rate_provider=provider)
    return FifoLedger(
        asset_internal_id=uuid.uuid4(),
        asset_category=asset_category,
        asset_multiplier_from_asset=None,
        currency_converter=converter,
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
        tax_classifier=None,
        pairing_method=pairing_method,
    )


def _buy(ledger, when, cost, tx_id):
    ev = TradeEvent(
        asset_internal_id=ledger.asset_internal_id,
        event_date=when,
        quantity=Decimal("1"),
        price_foreign_currency=Decimal(cost),
        event_type=FinancialEventType.TRADE_BUY_LONG,
        ibkr_transaction_id=tx_id,
    )
    ev.net_proceeds_or_cost_basis_eur = Decimal(cost)
    ledger.add_long_lot(ev)
    return ev


def _sell(ledger, when, qty, proceeds, tx_id):
    ev = TradeEvent(
        asset_internal_id=ledger.asset_internal_id,
        event_date=when,
        quantity=Decimal(qty),  # negative for a sale
        price_foreign_currency=Decimal(proceeds),
        event_type=FinancialEventType.TRADE_SELL_LONG,
        ibkr_transaction_id=tx_id,
    )
    ev.net_proceeds_or_cost_basis_eur = Decimal(proceeds)
    return ledger.consume_long_lots_for_sale(ev)


def _tax_book(ledger):
    _buy(ledger, "2023-01-10", "750", "1")
    _buy(ledger, "2023-02-10", "730", "2")
    _buy(ledger, "2023-04-10", "900", "3")
    _buy(ledger, "2023-06-10", "850", "4")


# --- Ledger-level pairing behaviour -----------------------------------------

@pytest.mark.parametrize(
    "method,expected_gain,expected_acq",
    [
        (PairingMethod.FIFO, Decimal("130"), "2023-01-10"),   # oldest (750)
        (PairingMethod.LIFO, Decimal("30"), "2023-06-10"),    # newest (850)
        (PairingMethod.WEIGHTED_AVERAGE, Decimal(
            "72.5"), "2023-01-10"),  # avg 807.5
    ],
)
def test_pairing_method_gain(method, expected_gain, expected_acq):
    ledger = _ledger(method)
    _tax_book(ledger)
    rgls = _sell(ledger, "2023-07-10", "-1", "880", "5")
    assert len(rgls) == 1
    assert rgls[0].gross_gain_loss_eur == expected_gain
    # WA keeps FIFO lot identity (dates) for the time test.
    assert rgls[0].acquisition_date == expected_acq


def test_weighted_average_moving_pool_is_consistent():
    """After a WA sale, surviving lots are re-priced so the next average is
    computed on a consistent (moving-average) inventory value."""
    ledger = _ledger(PairingMethod.WEIGHTED_AVERAGE)
    _tax_book(ledger)  # avg 807.5 across 4 units
    _sell(ledger, "2023-07-10", "-1", "880", "5")
    # 3 units remain, each re-priced to 807.5 (not their original costs).
    assert all(lot.unit_cost_basis_eur == Decimal("807.5")
               for lot in ledger.lots)
    # A second sale therefore also costs at 807.5.
    rgls2 = _sell(ledger, "2023-08-10", "-1", "820", "6")
    assert rgls2[0].gross_gain_loss_eur == Decimal("12.5")  # 820 - 807.5


def test_fifo_default_unchanged_when_method_omitted():
    provider = MockECBExchangeRateProvider(
        foreign_to_eur_init_value=Decimal("1.0"))
    converter = CurrencyConverter(rate_provider=provider)
    ledger = FifoLedger(
        asset_internal_id=uuid.uuid4(), asset_category=AssetCategory.STOCK,
        asset_multiplier_from_asset=None, currency_converter=converter,
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
    )
    assert ledger.pairing_method == PairingMethod.FIFO
    _tax_book(ledger)
    rgls = _sell(ledger, "2023-07-10", "-1", "880", "5")
    assert rgls[0].gross_gain_loss_eur == Decimal("130")


def test_consumption_order_indices():
    assert consumption_order_indices(3, PairingMethod.FIFO) == [0, 1, 2]
    assert consumption_order_indices(3, PairingMethod.LIFO) == [2, 1, 0]
    assert consumption_order_indices(
        3, PairingMethod.WEIGHTED_AVERAGE) == [0, 1, 2]
    assert consumption_order_indices(3, PairingMethod.OPTIMAL) == [0, 1, 2]


# --- Min-cost-flow optimal solver -------------------------------------------

def _never_exempt(lot, sale):
    return False


def test_solver_minimises_taxable_gain_via_high_cost_lot():
    """With no exemptions, the optimum matches the sale to the highest-cost
    lot (max loss / min gain) — the 'MaxLose' pick."""
    supplies = [
        SupplyLot(date(2023, 1, 10), Decimal("1"), Decimal("750"), "a"),
        SupplyLot(date(2023, 2, 10), Decimal("1"), Decimal("730"), "b"),
        SupplyLot(date(2023, 4, 10), Decimal("1"), Decimal("900"), "c"),
        SupplyLot(date(2023, 6, 10), Decimal("1"), Decimal("850"), "d"),
    ]
    demands = [SaleDemand(date(2023, 7, 10), Decimal("1"),
                          Decimal("880"), "S1")]
    asg = solve_optimal_matching(supplies, demands, _never_exempt)
    assert asg is not None
    # highest cost is 900 (index 2): gain = 880 - 900 = -20 (a loss).
    assert len(asg) == 1
    assert asg[0].supply_index == 2


def test_solver_routes_gain_to_exempt_lot():
    """A gain routed to a time-test-exempt lot drops out of the base entirely,
    which beats merely minimising the nominal gain."""
    supplies = [
        SupplyLot(date(2020, 1, 1), Decimal("1"),
                  Decimal("100"), "old"),   # exempt
        SupplyLot(date(2024, 1, 1), Decimal("1"),
                  Decimal("140"), "new"),   # taxable
    ]
    demands = [SaleDemand(date(2024, 6, 1), Decimal("1"),
                          Decimal("150"), "S1")]

    def exempt(lot, sale):
        return (sale.sale_date - lot.acq_date).days > 365 * 3 and not lot.estimated

    asg = solve_optimal_matching(supplies, demands, exempt)
    assert asg is not None
    # Route the sale to the EXEMPT old lot (arc cost 0) even though the new lot
    # would show a smaller nominal gain (10 vs 50): exempt beats taxable.
    assert asg[0].supply_index == 0


def test_solver_estimated_lot_never_exempt():
    supplies = [
        SupplyLot(date(2020, 1, 1), Decimal("1"), Decimal(
            "100"), "SOY_FALLBACK", estimated=True),
        SupplyLot(date(2024, 1, 1), Decimal("1"), Decimal("140"), "new"),
    ]
    demands = [SaleDemand(date(2024, 6, 1), Decimal("1"),
                          Decimal("150"), "S1")]

    def exempt(lot, sale):
        if lot.estimated:
            return False
        return (sale.sale_date - lot.acq_date).days > 365 * 3

    asg = solve_optimal_matching(supplies, demands, exempt)
    assert asg is not None
    # Neither lot is exempt (old one is estimated) → minimise gain → high cost 140.
    assert asg[0].supply_index == 1


def test_solver_infeasible_returns_none():
    supplies = [SupplyLot(date(2024, 8, 1), Decimal("1"),
                          Decimal("100"), "late")]
    # sale precedes the only lot's acquisition — cannot match.
    demands = [SaleDemand(date(2024, 6, 1), Decimal("1"),
                          Decimal("150"), "S1")]
    assert solve_optimal_matching(supplies, demands, _never_exempt) is None


# --- CzPairingComparison: cheapest-cell selection ---------------------------

def test_pairing_comparison_picks_cheapest_cell():
    from types import SimpleNamespace
    from src.countries.cz.pairing_compare import CzPairingComparison

    def _result(final_tax):
        return SimpleNamespace(sections={
            "cz_tax_liability": SimpleNamespace(
                line_items={
                    "final_czech_tax_after_credit_czk": Decimal(final_tax)}
            )
        })

    methods = [PairingMethod.FIFO, PairingMethod.LIFO, PairingMethod.OPTIMAL]
    grid = {
        ("daily", "fifo"): _result("3604"),
        ("daily", "lifo"): _result("3200"),
        ("daily", "optimal"): _result("2800"),
        ("uniform", "fifo"): _result("3822"),
        ("uniform", "lifo"): _result("3400"),
        ("uniform", "optimal"): _result("2900"),
    }
    cmp = CzPairingComparison(
        grid=grid, fx_modes=["daily", "uniform"], pairing_methods=methods)
    assert cmp.best_cell == ("daily", "optimal")
    assert cmp.final_tax_for("daily", PairingMethod.OPTIMAL) == Decimal("2800")
    text = "\n".join(cmp.render_lines())
    assert "Daňově optimální" in text
    assert "úspora" in text  # 3604 - 2800 saving vs FIFO/daily


# --- End-to-end: optimal solver over a real pipeline run --------------------

# STOCK with two lots and one partial sale; neither lot passes the 3y time
# test, so the optimum matches the sale to the HIGHER-cost lot (min gain).
_TWO_LOT_TRADES = [
    ["U1", "USD", "STK", "COMMON", "TWOLOT", "TWOLOT COMMON STOCK", "US9999999999",
     None, None, None, "2024-01-10", "10", "100", "0", "USD", "BUY", "10", None,
     None, "999999", None, "1", "O"],
    ["U1", "USD", "STK", "COMMON", "TWOLOT", "TWOLOT COMMON STOCK", "US9999999999",
     None, None, None, "2024-02-10", "10", "130", "0", "USD", "BUY", "11", None,
     None, "999999", None, "1", "O"],
    ["U1", "USD", "STK", "COMMON", "TWOLOT", "TWOLOT COMMON STOCK", "US9999999999",
     None, None, None, "2024-06-10", "-10", "120", "0", "USD", "SELL", "12", None,
     None, "999999", None, "1", "C"],
]
_TWO_LOT_EOY = [
    ["U1", "USD", "STK", "COMMON", "TWOLOT", "TWOLOT COMMON STOCK", "US9999999999",
     "10", "1300", "130", "1300", None, "999999", None, "1"],
]


class TestOptimalPairingE2E(FifoTestCaseBase):
    def _total_gain(self, pairing_method):
        results = self._run_pipeline(
            trades_data=_TWO_LOT_TRADES,
            positions_start_data=[],
            positions_end_data=_TWO_LOT_EOY,
            cash_transactions_data=[],
            corporate_actions_data=None,
            custom_rate_provider=MockECBExchangeRateProvider(
                foreign_to_eur_init_value=Decimal("1.0")),
            tax_year=2024,
            country_code="cz",
            pairing_method=pairing_method,
        )
        return sum((r.gross_gain_loss_eur for r in results.realized_gains_losses), Decimal(0))

    def test_optimal_beats_fifo_by_matching_high_cost_lot(self):
        fifo_gain = self._total_gain(PairingMethod.FIFO)
        optimal_gain = self._total_gain(PairingMethod.OPTIMAL)
        # FIFO consumes the 100-cost lot: gain = 10*(120-100) = +200.
        assert fifo_gain == Decimal("200")
        # Optimal consumes the 130-cost lot: gain = 10*(120-130) = -100 (loss).
        assert optimal_gain == Decimal("-100")
        assert optimal_gain < fifo_gain
