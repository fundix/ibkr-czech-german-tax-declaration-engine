# tests/test_currency_ledger.py
"""FIFO inventory over cash — the long/debt two-queue ledger.

The fixture is the tax advisor's own worked example, made concrete: a book
that acquires dollars at two different rates, spends some on a share, converts
more than it still holds (so the conversion both disposes and borrows), and
finally receives dollars that repay the borrowing before any of them become a
holding again.

Rates are stubbed, so every expected figure below is hand-computable.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.engine.currency_ledger import (
    CurrencyLedger,
    MovementDateField,
    RealisationKind,
    verify_against_statement,
)
from src.parsers.statement_of_funds_parser import (
    movements,
    parse_statement_of_funds_csv,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "statement_of_funds_currency_fifo.csv")

# CZK per 1 USD on the fixture's dates.
RATES = {
    date(2026, 1, 10): Decimal("21.00"),
    date(2026, 2, 10): Decimal("23.00"),
    date(2026, 3, 10): Decimal("22.50"),
    date(2026, 4, 10): Decimal("22.00"),
    date(2026, 5, 10): Decimal("24.00"),
}


def stub_rate(on, currency):
    return RATES.get(on) if currency == "USD" else None


@pytest.fixture
def records():
    return parse_statement_of_funds_csv(FIXTURE)


@pytest.fixture
def realisations(records):
    return CurrencyLedger(rate_lookup=stub_rate).replay(records)


class TestStatementInvariant:
    def test_replaying_the_amounts_reproduces_ibkrs_own_balances(self, records):
        """The statement proves the reconstruction: a dropped, duplicated or
        misordered row is exactly what makes a currency FIFO wrong, and all
        three break this check."""
        assert verify_against_statement(records) == []

    def test_a_dropped_row_is_caught(self, records):
        without_the_sale = [r for r in records if r.transaction_id != "8002"]
        assert verify_against_statement(without_the_sale) != []


class TestSharedFifo:
    """The advisor's central point: a share purchase is not itself a currency
    event, but it MUST still consume layers. If it did not, the conversion
    that follows would be measured against dollars that were already spent."""

    def test_the_purchase_consumes_layers_without_realising_anything(self, realisations):
        buys = [r for r in realisations if r.activity == "BUY"]
        assert len(buys) == 1
        # 1200 spent: all 1000 of the January layer, then 200 of February's.
        assert buys[0].quantity == Decimal("1200.00")
        assert [l.quantity for l in buys[0].layers] == [Decimal("1000.00"),
                                                        Decimal("200.00")]
        assert [l.opened_on for l in buys[0].layers] == [date(2026, 1, 10),
                                                         date(2026, 2, 10)]

    def test_the_conversion_meets_the_layer_the_purchase_left_behind(self, realisations):
        """Had the purchase not consumed anything, the 300 would have come
        from the January layer at 21.00 and shown a 500 CZK gain instead of a
        300 CZK loss. This assertion IS the advisor's worked example."""
        conv = next(r for r in realisations if r.activity == "FOREX")
        assert conv.kind is RealisationKind.LONG_DISPOSAL
        assert conv.quantity == Decimal("300.00")          # not 500 — see below
        assert [l.rate for l in conv.layers] == [Decimal("23.00")]
        # 300 acquired at 23.00, given up at 22.00.
        assert conv.gain == Decimal("-300.00")


class TestDebt:
    def test_converting_more_than_is_held_borrows_the_rest(self, realisations, records):
        """500 disposed against 300 held: the 200 shortfall is the broker's
        money, so it realises nothing and opens a debt instead."""
        conv = next(r for r in realisations if r.activity == "FOREX")
        assert conv.quantity == Decimal("300.00")
        ledger = CurrencyLedger(rate_lookup=stub_rate)
        ledger.replay([r for r in records
                       if (r.date or "") <= "2026-04-10" or r.is_balance_row])
        debts = ledger.open_debts("USD")
        assert [d.quantity for d in debts] == [Decimal("200.00")]
        assert debts[0].rate == Decimal("22.00")

    def test_incoming_cash_repays_the_debt_before_it_becomes_a_holding(self, realisations):
        repayment = next(r for r in realisations
                         if r.kind is RealisationKind.DEBT_REPAYMENT)
        assert repayment.quantity == Decimal("200.00")
        # Mirrored: drawn when a dollar cost 22.00, repaid when it cost 24.00.
        # The debt grew more expensive, so the holder is 400 CZK down.
        assert repayment.gain == Decimal("-400.00")

    def test_only_the_surplus_above_zero_opens_a_new_lot(self, records):
        ledger = CurrencyLedger(rate_lookup=stub_rate)
        ledger.replay(records)
        lots = ledger.open_lots("USD")
        assert [l.quantity for l in lots] == [Decimal("600.00")]
        assert lots[0].opened_on == date(2026, 5, 10)
        assert ledger.open_debts("USD") == []

    def test_the_ledger_position_matches_the_brokers_closing_balance(self, records):
        ledger = CurrencyLedger(rate_lookup=stub_rate)
        ledger.replay(records)
        assert ledger.position("USD") == Decimal("600.00")
        assert ledger.balances["USD"] == Decimal("600.00")


class TestHomeCurrency:
    def test_the_home_currency_never_realises_anything(self, realisations):
        """Koruna measured against the koruna cannot move, so the CZK leg of
        the conversion is inventory movement and nothing else."""
        assert all(r.currency != "CZK" for r in realisations)


class TestUndeterminedRates:
    def test_a_missing_rate_reports_undetermined_not_zero(self, records):
        """The advisor was explicit: without the acquisition rate the output
        must say it cannot be determined. A zero would read as 'no gain'."""
        ledger = CurrencyLedger(rate_lookup=lambda on, cur: None)
        out = ledger.replay(records)
        disposals = [r for r in out if r.currency == "USD"]
        assert disposals
        assert all(not r.is_determined and r.gain is None for r in disposals)

    def test_a_seeded_opening_balance_carries_no_rate(self):
        """An opening balance says how much is held, never when it was
        acquired. Valuing it at the 1 January rate would invent a cost the
        holder never paid, so the layer carries no rate and anything
        consuming it is undetermined."""
        recs = parse_statement_of_funds_csv(FIXTURE)
        opening = next(r for r in recs if r.is_starting_balance)
        object.__setattr__(opening, "balance", Decimal("100"))
        ledger = CurrencyLedger(rate_lookup=stub_rate)
        ledger.seed_opening_balance(opening)
        assert [l.rate for l in ledger.open_lots("USD")] == [None]


class TestDateField:
    def test_the_date_field_is_a_switch_not_a_hardcode(self, records):
        """Date and SettleDate disagree on most rows of a real book, and
        which one governs is a question for the taxpayer's advisor."""
        by_trade = CurrencyLedger(rate_lookup=stub_rate,
                                  date_field=MovementDateField.DATE)
        by_settle = CurrencyLedger(rate_lookup=stub_rate,
                                   date_field=MovementDateField.SETTLE_DATE)
        by_trade.replay(records)
        by_settle.replay(records)
        assert by_trade.open_lots("USD")[0].opened_on == date(2026, 5, 10)
        assert by_settle.open_lots("USD")[0].opened_on == date(2026, 5, 12)


class TestOrdering:
    def test_movements_are_applied_in_the_order_given(self, records):
        """File order is canonical — IBKR's own running balance proves it —
        so the ledger must not re-sort.

        The closing position is a plain sum and so cannot show this; what
        order decides is which layer each disposal meets, and therefore every
        gain. Reversing the rows must change the results, not silently
        produce the same ones.
        """
        forward = CurrencyLedger(rate_lookup=stub_rate)
        forward_out = forward.replay(records)
        backward = CurrencyLedger(rate_lookup=stub_rate)
        backward_out = backward.replay(list(reversed(movements(records))))

        assert forward.position("USD") == backward.position("USD")
        forward_gains = [r.gain for r in forward_out if r.currency == "USD"]
        backward_gains = [r.gain for r in backward_out if r.currency == "USD"]
        assert forward_gains != backward_gains
