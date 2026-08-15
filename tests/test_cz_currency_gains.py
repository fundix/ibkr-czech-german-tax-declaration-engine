# tests/test_cz_currency_gains.py
"""Czech §10 verdicts over the cash ledger.

Same fixture as test_currency_ledger — the advisor's worked example — read
here through the CZ layer, which decides which of the ledger's realisations
§10 reaches.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.currency_gains import (
    CzCurrencyRecognition,
    compute_currency_gains,
)
from src.parsers.statement_of_funds_parser import parse_statement_of_funds_csv

FIXTURE = str(Path(__file__).parent / "fixtures" / "statement_of_funds_currency_fifo.csv")

RATES = {
    date(2026, 1, 10): Decimal("21.00"),
    date(2026, 2, 10): Decimal("23.00"),
    date(2026, 3, 10): Decimal("22.50"),
    date(2026, 4, 10): Decimal("22.00"),
    date(2026, 5, 10): Decimal("24.00"),
}


def stub_rate(on, currency):
    if currency == "CZK":
        return Decimal("1")
    return RATES.get(on)


@pytest.fixture
def gains():
    return compute_currency_gains(
        parse_statement_of_funds_csv(FIXTURE), rate_lookup=stub_rate,
    )


class TestRecognition:
    def test_narrow_counts_only_the_conversion(self, gains):
        """§10 speaks of an exchange of money from a foreign-currency account.
        Paying a share's purchase price is payment, not exchange."""
        assert gains.total(CzCurrencyRecognition.NARROW) == Decimal("-300.00")

    def test_broad_adds_the_share_purchase(self, gains):
        """1000 bought at 21.00 and 200 at 23.00, both given up at 22.50:
        1000 × 1.50 − 200 × 0.50 = 1400 on top of the conversion's −300."""
        assert gains.total(CzCurrencyRecognition.BROAD) == Decimal("1100.00")

    def test_both_readings_share_one_fifo(self, gains):
        """The readings differ by a predicate, never by the inventory. Were
        the purchase left out of the FIFO, the conversion would have met the
        January layer and shown +500 instead of −300."""
        narrow = [r for r in gains.realisations
                  if r.recognised_under(CzCurrencyRecognition.NARROW)]
        broad = [r for r in gains.realisations
                 if r.recognised_under(CzCurrencyRecognition.BROAD)]
        assert len(narrow) == 1 and len(broad) == 2
        assert narrow[0].realisation.layers[0].rate == Decimal("23.00")

    def test_the_conversion_leg_is_told_from_a_fee_by_identity(self, gains):
        """IBKR bills a conversion's commission under the legs' own id on some
        trades and its own on others, so a transaction id cannot separate
        them — record identity can."""
        legs = [r for r in gains.realisations if r.is_conversion_leg]
        assert len(legs) == 1
        assert legs[0].realisation.activity == "FOREX"


class TestShortFx:
    def test_debt_repayment_is_its_own_category(self, gains):
        """Never folded into either reading: §10 covers exchanging one's own
        money, not repaying a foreign-currency debt."""
        assert gains.short_fx_total() == Decimal("-400.00")
        assert all(not r.recognised_under(CzCurrencyRecognition.BROAD)
                   for r in gains.realisations
                   if r.realisation.kind.name == "DEBT_REPAYMENT")

    def test_a_short_loss_is_not_netted_against_the_gains(self, gains):
        """The −400 debt result must not quietly reduce either §10 total."""
        assert gains.total(CzCurrencyRecognition.NARROW) == Decimal("-300.00")
        assert gains.total(CzCurrencyRecognition.BROAD) == Decimal("1100.00")


class TestJoinAndIntegrity:
    def test_realisations_join_by_ibkr_transaction_id(self, gains):
        """The id on a FOREX row in trades.csv — which reaches
        CurrencyConversionEvent.ibkr_transaction_id — is the id the Statement
        of Funds puts on that conversion's legs."""
        found = gains.for_transaction("8004")
        assert len(found) == 1
        assert found[0].gain_czk == Decimal("-300.00")

    def test_an_unknown_transaction_matches_nothing(self, gains):
        assert gains.for_transaction("nope") == []
        assert gains.for_transaction("") == []

    def test_a_broken_ledger_is_reported_not_swallowed(self):
        records = [r for r in parse_statement_of_funds_csv(FIXTURE)
                   if r.transaction_id != "8002"]
        broken = compute_currency_gains(records, rate_lookup=stub_rate)
        assert broken.ledger_problems


class TestUndetermined:
    def test_undetermined_is_counted_apart_from_the_totals(self):
        """A gain that cannot be stated must not be summed in as a nil one."""
        gains = compute_currency_gains(parse_statement_of_funds_csv(FIXTURE),
                                       rate_lookup=lambda on, cur: None)
        assert gains.total(CzCurrencyRecognition.NARROW) == Decimal("0")
        assert len(gains.undetermined()) > 0


class TestConfigDefaults:
    def test_the_advisors_defaults_are_the_shipped_ones(self):
        cfg = CzTaxConfig()
        assert cfg.currency_recognition is CzCurrencyRecognition.NARROW
        assert cfg.currency_short_fx_in_tax_base is False
        # Two questions are still with the advisor — netting inside the
        # section, and trade vs settlement date — so nothing moves a tax
        # figure yet.
        assert cfg.currency_gains_in_tax_base is False
