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
    conversions,
    movements,
    parse_statement_of_funds_csv,
    starting_balances,
)

FIXTURE = str(Path(__file__).parent / "fixtures" / "statement_of_funds_sample.csv")


class TestParsing:
    def test_every_row_is_read_including_balance_markers(self):
        assert len(parse_statement_of_funds_csv(FIXTURE)) == 19

    def test_a_missing_file_is_empty_not_an_error(self):
        assert parse_statement_of_funds_csv("/nope/missing.csv") == []


class TestBalanceRows:
    """LevelOfDetail is 'Currency' on every row, so it cannot tell the marker
    rows apart — the empty activity code plus the description can."""

    def test_markers_are_recognised(self):
        recs = parse_statement_of_funds_csv(FIXTURE)
        markers = [r for r in recs if r.is_balance_row]
        assert len(markers) == 10         # five currencies, open + close
        assert all(not (r.activity_code or "") for r in markers)
        assert all(r.amount == 0 for r in markers)

    def test_movements_exclude_the_markers(self):
        recs = parse_statement_of_funds_csv(FIXTURE)
        assert len(movements(recs)) == 9
        assert all(not r.is_balance_row for r in movements(recs))

    def test_starting_balances_are_the_point_of_the_whole_query(self):
        """Without these a currency FIFO has nothing to consume."""
        recs = parse_statement_of_funds_csv(FIXTURE)
        assert starting_balances(recs) == {
            "CHF": Decimal("0"),
            "EUR": Decimal("500.00"),
            "GBP": Decimal("250.00"),
            "USD": Decimal("-1000.00"),
            "CZK": Decimal("-395.02"),
        }

    def test_a_negative_opening_balance_is_kept_signed(self):
        """On a margin account that is borrowed currency, not an error —
        clamping it would hide a real FX exposure."""
        recs = parse_statement_of_funds_csv(FIXTURE)
        assert starting_balances(recs)["USD"] < 0


class TestConversions:
    """A conversion is two OR THREE rows sharing a trade id.

    The commission is booked as its own row with its own transaction id, so
    grouping on the transaction id splits it off as a bogus one-legged
    conversion — on the real book that turned 17 conversions into 26.
    """

    def _by_trade(self):
        return {c.trade_id: c for c in conversions(parse_statement_of_funds_csv(FIXTURE))}

    def test_trade_id_is_the_key_not_the_transaction_id(self):
        assert sorted(self._by_trade()) == ["7001", "7004"]

    def test_a_two_row_conversion_has_two_legs_and_no_charge(self):
        conv = self._by_trade()["7001"]
        assert conv.is_complete
        assert {l.currency_primary for l in conv.legs} == {"CHF", "CZK"}
        assert conv.charges == []

    def test_the_commission_row_is_not_mistaken_for_a_leg(self):
        conv = self._by_trade()["7004"]
        assert conv.is_complete
        assert {l.currency_primary for l in conv.legs} == {"CHF", "GBP"}
        # The fee lands in the billing currency and is not a currency disposal.
        assert [c.currency_primary for c in conv.charges] == ["CZK"]
        assert conv.charges[0].amount == Decimal("-4.19")

    def test_the_disposed_side_is_the_negative_one(self):
        """A gain is measured on what was given up, not what came in."""
        conv = self._by_trade()["7004"]
        assert conv.disposed.currency_primary == "GBP"
        assert conv.disposed.amount == Decimal("-100")
        assert conv.received.currency_primary == "CHF"

    def test_leg_lookup_by_currency(self):
        conv = self._by_trade()["7001"]
        assert conv.leg_for("czk").amount == Decimal("-7.35994")
        assert conv.leg_for("usd") is None

    def test_commission_sharing_the_legs_transaction_id_is_still_a_charge(self):
        """Real trade 1295097860: the commission row shares the legs'
        transaction id AND the disposed leg's currency, so neither id nor
        currency separates it — only the pair symbol does."""
        recs = parse_statement_of_funds_csv(FIXTURE)
        extra = [r for r in recs]  # copy; append a synthetic 3-row trade
        import copy
        template = next(r for r in recs if r.trade_id == "7001"
                        and r.currency_primary == "CZK")
        # legs: CZK (pair row, disposed) + USD; charge: CZK, same tx id
        pair = copy.deepcopy(template)
        pair.trade_id, pair.transaction_id = "7009", "9900"
        pair.symbol, pair.currency_primary = "USD.CZK", "CZK"
        pair.amount = Decimal("-11706.87")
        leg = copy.deepcopy(template)
        leg.trade_id, leg.transaction_id = "7009", "9900"
        leg.symbol, leg.currency_primary = "", "USD"
        leg.amount = Decimal("582.91")
        fee = copy.deepcopy(template)
        fee.trade_id, fee.transaction_id = "7009", "9900"
        fee.symbol, fee.currency_primary = "", "CZK"
        fee.amount = Decimal("-40.818")
        fee.activity_description = "Commission from Forex Trade"
        conv = next(c for c in conversions(extra + [pair, leg, fee])
                    if c.trade_id == "7009")
        assert conv.is_complete
        assert {l.currency_primary for l in conv.legs} == {"CZK", "USD"}
        assert [c.amount for c in conv.charges] == [Decimal("-40.818")]
        assert conv.disposed.amount == Decimal("-11706.87")

    def test_only_forex_rows_are_considered(self):
        for conv in conversions(parse_statement_of_funds_csv(FIXTURE)):
            for row in conv.legs + conv.charges:
                assert (row.activity_code or "").upper() == "FOREX"


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
