# tests/test_statement_of_funds_parser.py
"""Statement of Funds parsing.

The fixture mirrors the structure of a real IBKR export (verified against a
July 2026 run) with a fake account and rounded figures: multiple currency
blocks, a Starting/Ending Balance pair around each, a two-legged FOREX
conversion, a positive commission, and a withholding-tax correction whose
economic date is a month before its report date.
"""
from decimal import Decimal
from pathlib import Path

from src.parsers.statement_of_funds_parser import (
    conversion_legs,
    movements,
    parse_statement_of_funds_csv,
    starting_balances,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "statement_of_funds_sample.csv")


class TestParsing:
    def test_every_row_is_read_including_balance_markers(self):
        assert len(parse_statement_of_funds_csv(FIXTURE)) == 14

    def test_a_missing_file_is_empty_not_an_error(self):
        assert parse_statement_of_funds_csv("/nope/missing.csv") == []


class TestBalanceRows:
    """LevelOfDetail is 'Currency' on every row, so it cannot tell the marker
    rows apart — the empty activity code plus the description can."""

    def test_markers_are_recognised(self):
        recs = parse_statement_of_funds_csv(FIXTURE)
        markers = [r for r in recs if r.is_balance_row]
        assert len(markers) == 8          # four currencies, open + close
        assert all(not (r.activity_code or "") for r in markers)
        assert all(r.amount == 0 for r in markers)

    def test_movements_exclude_the_markers(self):
        recs = parse_statement_of_funds_csv(FIXTURE)
        assert len(movements(recs)) == 6
        assert all(not r.is_balance_row for r in movements(recs))

    def test_starting_balances_are_the_point_of_the_whole_query(self):
        """Without these a currency FIFO has nothing to consume."""
        recs = parse_statement_of_funds_csv(FIXTURE)
        assert starting_balances(recs) == {
            "CHF": Decimal("0"),
            "EUR": Decimal("500.00"),
            "USD": Decimal("-1000.00"),
            "CZK": Decimal("-395.02"),
        }

    def test_a_negative_opening_balance_is_kept_signed(self):
        """On a margin account that is borrowed currency, not an error —
        clamping it would hide a real FX exposure."""
        recs = parse_statement_of_funds_csv(FIXTURE)
        assert starting_balances(recs)["USD"] < 0


class TestConversionLegs:
    def test_both_legs_pair_through_the_ids(self):
        """The descriptions differ between legs ("Traded" vs "Trading"), so the
        text is not joinable — the ids are."""
        legs = conversion_legs(parse_statement_of_funds_csv(FIXTURE))
        assert list(legs) == ["9002"]
        [pair] = legs.values()
        assert {r.currency_primary for r in pair} == {"CHF", "CZK"}
        assert {r.trade_id for r in pair} == {"7001"}
        # One leg gains, the other gives up.
        assert sorted(r.amount for r in pair) == [Decimal("-7.35994"), Decimal("0.28")]

    def test_only_forex_rows_are_grouped(self):
        legs = conversion_legs(parse_statement_of_funds_csv(FIXTURE))
        assert all((r.activity_code or "").upper() == "FOREX"
                   for pair in legs.values() for r in pair)


class TestFieldSemantics:
    def _by_tx(self, tx):
        return next(r for r in parse_statement_of_funds_csv(FIXTURE)
                    if r.transaction_id == tx)

    def test_amount_equals_debit_plus_credit(self):
        for rec in movements(parse_statement_of_funds_csv(FIXTURE)):
            assert (rec.debit or Decimal(0)) + (rec.credit or Decimal(0)) == rec.amount

    def test_economic_date_can_precede_the_report_date(self):
        """A withholding-tax correction reported in July for a June dividend.
        The ČNB rate must follow `date`, not `report_date`."""
        rec = self._by_tx("9005")
        assert rec.report_date == "2026-07-30"
        assert rec.date == "2026-06-25"

    def test_a_positive_commission_keeps_its_sign(self):
        """IBKR rebates exist here too — the same trap as in the trades file."""
        assert self._by_tx("9004").trade_commission == Decimal("0.01053")

    def test_instrument_context_survives(self):
        rec = self._by_tx("9003")
        assert (rec.symbol, rec.isin, rec.asset_class) == (
            "RHM", "DE0007030009", "STK")
        assert rec.trade_gross == Decimal("-100.00")
