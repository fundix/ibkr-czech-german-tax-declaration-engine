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
from src.countries.cz.loss_offsetting import ZERO, CzLossOffsettingResult
from src.engine.currency_ledger import MovementDateField
from src.countries.cz.currency_gains import (
    CzCurrencyGains,
    CzCurrencyRecognition,
    compute_currency_gains,
)
from src.parsers.statement_of_funds_parser import parse_statement_of_funds_csv

FIXTURE = str(Path(__file__).parent / "fixtures" / "statement_of_funds_currency_fifo.csv")

# Trade date and its settlement date carry the same rate, so these
# expectations test the §10 verdicts rather than the date basis (which
# test_currency_ledger covers).
RATES = {
    date(2026, 1, 10): Decimal("21.00"), date(2026, 1, 12): Decimal("21.00"),
    date(2026, 2, 10): Decimal("23.00"), date(2026, 2, 12): Decimal("23.00"),
    date(2026, 3, 10): Decimal("22.50"), date(2026, 3, 12): Decimal("22.50"),
    date(2026, 4, 10): Decimal("22.00"), date(2026, 4, 14): Decimal("22.00"),
    date(2026, 5, 10): Decimal("24.00"), date(2026, 5, 12): Decimal("24.00"),
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
        # Repaying borrowed currency stays a disputed category of its own.
        assert cfg.currency_short_fx_in_tax_base is False
        # Both blocking questions were answered on 2026-08-15, so the figures
        # now reach the base: settlement date, and netting floored at zero.
        assert cfg.currency_gains_in_tax_base is True
        assert cfg.currency_movement_date is MovementDateField.SETTLE_DATE
        assert cfg.currency_occasional_exempt_limit_czk == Decimal("50000")


class TestNetting:
    """§10 odst. 5 calls an FX loss an expense; odst. 4 says that where a
    kind's expenses exceed its income the difference is disregarded."""

    def _netted(self, gains, losses, threshold=Decimal("50000"), enabled=True):
        result = CzLossOffsettingResult()
        result.currency.gains = gains
        result.currency.losses = losses
        result.currency_exemption_enabled = enabled
        result.currency_exemption_threshold = threshold
        result.compute_combined()
        return result

    def test_losses_net_against_gains_inside_the_kind(self):
        r = self._netted(Decimal("80000"), Decimal("30000"))
        assert r.currency.raw_net == Decimal("50000")
        assert r.currency.net_taxable == Decimal("50000")

    def test_a_net_loss_floors_at_zero_and_is_disregarded(self):
        r = self._netted(Decimal("60000"), Decimal("90000"))
        assert r.currency.raw_net == Decimal("-30000")
        assert r.currency.net_taxable == Decimal("0")
        assert r.currency.unutilized_loss == Decimal("30000")

    def test_an_unused_loss_never_reduces_another_kind(self):
        r = self._netted(Decimal("60000"), Decimal("90000"))
        r.securities.taxable_gains = Decimal("10000")
        r.compute_combined()
        assert r.combined_net_taxable == Decimal("10000")

    def test_the_exemption_is_tested_on_gross_gains_not_the_net(self):
        """A loss neither lowers the income being tested nor earns the
        exemption: 60k of gains against 20k of losses nets to 40k, which is
        under the threshold, yet the gains that are tested are 60k."""
        r = self._netted(Decimal("60000"), Decimal("20000"))
        assert r.currency.exempt_occasional is False
        assert r.currency.net_taxable == Decimal("40000")

    def test_below_the_threshold_the_whole_amount_is_exempt(self):
        r = self._netted(Decimal("40000"), Decimal("5000"))
        assert r.currency.exempt_occasional is True
        assert r.currency.net_taxable == Decimal("0")

    def test_the_threshold_is_a_cliff(self):
        assert self._netted(Decimal("50000"), ZERO).currency.exempt_occasional is True
        assert self._netted(Decimal("50001"), ZERO).currency.exempt_occasional is False


class TestSectionNote:
    """The note is the only place the reader meets these figures, and it is
    built outside aggregate()'s local scope — a NameError there reaches a real
    run while every unit test around it still passes. So exercise it."""

    def _note(self, gains, losses, short_fx=ZERO, year=2026, threshold=Decimal("50000")):
        from src.countries.cz.plugin import CzechTaxAggregator

        netting = CzLossOffsettingResult()
        netting.currency.gains = gains
        netting.currency.losses = losses
        netting.currency_exemption_enabled = True
        netting.currency_exemption_threshold = threshold
        netting.compute_combined()

        class _Gains(CzCurrencyGains):
            def total(self, recognition, year=None):
                return gains - losses

            def short_fx_total(self, year=None):
                return short_fx

            def undetermined(self, year=None):
                return []

        agg = CzechTaxAggregator(config=CzTaxConfig())
        return agg._currency_note(13, _Gains(), year, netting)

    def test_it_reports_the_exemption_rather_than_crying_review(self):
        note = self._note(Decimal("52.77"), Decimal("413.93"))
        assert "EXEMPT" in note
        assert "50000.00" in note
        assert "REVIEW REQUIRED" not in note

    def test_a_disregarded_loss_says_so(self):
        note = self._note(Decimal("60000"), Decimal("90000"))
        assert "Into the base: 0 CZK" in note
        assert "§10 odst. 4" in note

    def test_a_taxable_net_is_stated_plainly(self):
        note = self._note(Decimal("80000"), Decimal("30000"))
        assert "Into the base: 50000.00 CZK" in note

    def test_short_fx_is_flagged_for_review_when_it_exists(self):
        note = self._note(Decimal("52.77"), Decimal("413.93"),
                          short_fx=Decimal("-5201.17"))
        assert "REVIEW REQUIRED" in note
        assert "never netted" in note

    def test_a_missing_ledger_says_which_slot_to_download(self):
        from src.countries.cz.plugin import CzechTaxAggregator

        note = CzechTaxAggregator(config=CzTaxConfig())._currency_note(13, None, 2026)
        assert "Statement of Funds" in note and "REVIEW REQUIRED" in note

    def test_a_ledger_that_does_not_reconcile_blocks_every_figure(self):
        from src.countries.cz.plugin import CzechTaxAggregator

        gains = CzCurrencyGains(ledger_problems=["USD 2026-01-02: replay gives 1"])
        note = CzechTaxAggregator(config=CzTaxConfig())._currency_note(13, gains, 2026)
        assert "does not replay" in note
        assert "REVIEW REQUIRED" in note

    def test_the_2026_letter_caveat_is_year_scoped(self):
        assert "360/2025" in self._note(Decimal("1"), ZERO, year=2026)
        assert "360/2025" not in self._note(Decimal("1"), ZERO, year=2025)
