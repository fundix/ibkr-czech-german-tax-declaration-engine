# tests/test_soy_holding_period_basis.py
"""
IBKR's two lot dates are not interchangeable, and only one of them is a Czech
acquisition date.

``OpenDateTime`` is when the lot was opened. ``HoldingPeriodDateTime`` is IBKR's
holding basis under **US** rules: pushed forward by wash sales, which Czech law
has no equivalent of, and carried back over IRC §368 reorganisations, which the
Czech treatment classifies as a taxable disposal rather than a deferral. So it is
wrong for BOTH Czech questions — it is neither the acquisition date (which
selects the applicable regime) nor a Czech holding-period start.

The seeding used to fall back to it silently. This module pins that it no longer
does, and that the two situations where it carries information are surfaced
rather than trusted.
"""
import datetime
from decimal import Decimal

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzTaxReviewStatus
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.time_test import evaluate_time_test
from src.domain.assets import SoyPositionLot, Stock
from src.parsers.parsing_orchestrator import ParsingOrchestrator
from tests.test_audit_fixes import _make_ledger

import uuid


class _RawPos:
    def __init__(self, open_dt=None, holding_dt=None):
        self.open_date_time = open_dt
        self.holding_period_date_time = holding_dt


class TestLotDatesAreKeptApart:
    def test_both_present_and_equal_is_the_ordinary_case(self):
        got = ParsingOrchestrator._lot_dates(
            _RawPos("2025-12-29;113656", "2025-12-29;113656"))
        assert got == ("2025-12-29", "2025-12-29", False)

    def test_a_divergent_holding_basis_is_recorded_not_used(self):
        """IBKR says the holding is older than the lot. Under Czech rules the
        lot open date still governs, so the acquisition date must be it."""
        open_date, holding_date, from_basis = ParsingOrchestrator._lot_dates(
            _RawPos("2016-05-05;120000", "2013-04-02;093000"))

        assert open_date == "2016-05-05"        # NOT the 2013 US basis
        assert holding_date == "2013-04-02"     # kept, so it can be surfaced
        assert from_basis is False

    def test_a_missing_open_date_falls_back_but_says_so(self):
        open_date, holding_date, from_basis = ParsingOrchestrator._lot_dates(
            _RawPos(None, "2013-04-02;093000"))

        assert open_date == "2013-04-02"
        assert from_basis is True               # the caller must mark it estimated

    def test_neither_date_gives_nothing(self):
        assert ParsingOrchestrator._lot_dates(_RawPos(None, None)) == (None, None, False)

    def test_compact_ibkr_format_is_parsed(self):
        got = ParsingOrchestrator._lot_dates(_RawPos("20251229;113656", None))
        assert got[0] == "2025-12-29"


class TestSeedingFlagsWhatItCannotTrust:
    def _asset(self, lots, qty="10"):
        stock = Stock(ibkr_symbol="ABC")
        stock.soy_quantity = Decimal(qty)
        stock.soy_cost_basis_amount = Decimal("100")
        stock.soy_cost_basis_currency = "EUR"
        stock.soy_lots = lots
        return stock

    def _seed(self, lots, qty="10"):
        ledger = _make_ledger()
        asset = self._asset(lots, qty)
        ok = ledger._create_lots_from_soy_snapshot(
            asset, Decimal(qty), tax_year=2026, long_side=True)
        return ledger, ok

    def test_ordinary_lot_is_seeded_unflagged(self):
        """Do not regress the lot-level snapshot: these dates are real."""
        ledger, ok = self._seed([SoyPositionLot(
            open_date="2025-06-01", quantity=Decimal("10"),
            cost_basis_amount=Decimal("100"), cost_basis_currency="EUR",
            holding_period_date="2025-06-01",
        )])

        assert ok is True
        lot = ledger.lots[0]
        assert lot.acquisition_date == "2025-06-01"
        assert lot.holding_period_start == "2025-06-01"
        assert lot.acquisition_date_estimated is False
        assert lot.holding_period_start_estimated is False

    def test_a_divergent_basis_flags_the_holding_start(self):
        """The gap is a US adjustment with no Czech counterpart, so whether the
        Czech holding start is the lot date or something earlier is unknown."""
        ledger, ok = self._seed([SoyPositionLot(
            open_date="2016-05-05", quantity=Decimal("10"),
            cost_basis_amount=Decimal("100"), cost_basis_currency="EUR",
            holding_period_date="2013-04-02",
        )])

        assert ok is True
        lot = ledger.lots[0]
        assert lot.acquisition_date == "2016-05-05"
        # The US basis is NOT imported as the start...
        assert lot.holding_period_start == "2016-05-05"
        # ...and the acquisition date itself is real, so only the start is flagged.
        assert lot.acquisition_date_estimated is False
        assert lot.holding_period_start_estimated is True

    def test_a_holding_basis_used_as_acquisition_flags_both(self):
        """With no OpenDateTime the acquisition is not established either — and
        it is the acquisition date that picks the pre-2014 regime."""
        ledger, ok = self._seed([SoyPositionLot(
            open_date="2013-04-02", quantity=Decimal("10"),
            cost_basis_amount=Decimal("100"), cost_basis_currency="EUR",
            holding_period_date="2013-04-02",
            open_date_is_holding_basis=True,
        )])

        assert ok is True
        lot = ledger.lots[0]
        assert lot.acquisition_date_estimated is True
        assert lot.holding_period_start_estimated is True


class TestTheRegimeCannotBeChosenFromABrokerBasis:
    """The failure this change prevents, end to end.

    A share issued in a post-2014 merger whose IBKR holding basis is a pre-2014
    purchase used to be seeded with that pre-2014 date as its acquisition date,
    which selects the transitional SIX-MONTH test — so a disposal seven months
    later was reported exempt, with no merger event anywhere in the data to
    explain it.
    """

    def _item(self, acq, event_date, hps=None, acq_est=False, hps_est=False):
        return CzTaxItem(
            item_type=CzTaxItemType.SECURITY_DISPOSAL,
            section=CzTaxSection.CZ_10_SECURITIES,
            source_event_id=uuid.uuid4(),
            event_date=event_date,
            acquisition_date=acq,
            holding_period_start=hps,
            acquisition_date_estimated=acq_est,
            holding_period_start_estimated=hps_est,
            original_amount=Decimal("500"), original_currency="EUR",
            amount_eur=Decimal("500"), proceeds_czk=Decimal("50000"),
            asset_category="STOCK",
        )

    def test_the_old_behaviour_would_have_exempted_this(self):
        """Documents the wrong answer: a 2013 acquisition date earns the
        six-month test, so a sale seven months later is exempt."""
        item = self._item("2013-04-02", "2013-11-15")
        evaluate_time_test([item], CzTaxConfig(), tax_year=2013)

        assert item.is_exempt is True     # this is what must not be reachable
        assert "months" in item.tax_review_note

    def test_a_lot_seeded_from_a_broker_basis_is_reviewed_not_exempted(self):
        """With the acquisition date marked estimated, the test is not decided
        at all — the item is conservatively taxable and flagged."""
        item = self._item("2013-04-02", "2013-11-15", acq_est=True)
        evaluate_time_test([item], CzTaxConfig(), tax_year=2013)

        assert item.is_exempt is False
        assert item.is_taxable is True
        assert item.tax_review_status is CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        assert "not established" in item.tax_review_note

    def test_a_divergent_basis_also_blocks_a_confident_verdict(self):
        item = self._item("2016-05-05", "2026-06-01", hps_est=True)
        evaluate_time_test([item], CzTaxConfig(), tax_year=2026)

        # Ten years of holding would otherwise be a clear exemption.
        assert item.is_exempt is False
        assert item.tax_review_status is CzTaxReviewStatus.PENDING_MANUAL_REVIEW

    def test_an_unflagged_lot_still_gets_a_normal_verdict(self):
        """The flags must not become a blanket veto on the time test."""
        item = self._item("2016-05-05", "2026-06-01")
        evaluate_time_test([item], CzTaxConfig(), tax_year=2026)

        assert item.is_exempt is True
        assert item.tax_review_status is CzTaxReviewStatus.RESOLVED
