# tests/test_cz_margin_interest.py
"""
Margin/debit interest paid to the broker ("Broker Interest Paid") is a cost,
not negative interest income. It must not reduce the CZ §8 interest sum and
must not enter the FTC foreign-income base.

Regression for the 2026-07 real-statement finding: 31 margin-interest rows
were mapped to INTEREST_RECEIVED with the negative amount kept, entered §8
as negative income (reducing the tax base) and inflated the FTC item count.
"""
import uuid
from decimal import Decimal

from src.classification.asset_classifier import AssetClassifier
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.plugin import CzechTaxAggregator
from src.domain.enums import FinancialEventType
from src.domain.events import CashFlowEvent
from src.identification.asset_resolver import AssetResolver
from src.parsers.domain_event_factory import DomainEventFactory
from src.parsers.raw_models import RawCashTransactionRecord


# ---------------------------------------------------------------------------
# Parser mapping
# ---------------------------------------------------------------------------

def _cash_event_for(amount: Decimal,
                    type_str: str = "Broker Interest Paid",
                    description: str = "CZK DEBIT INT FOR JUN-2025",
                    currency: str = "CZK"):
    factory = DomainEventFactory(AssetResolver(AssetClassifier()))
    rct = RawCashTransactionRecord(
        CurrencyPrimary=currency,
        Description=description,
        SettleDate="2025-07-03",
        Type=type_str,
        Amount=amount,
        TransactionID="4288434986",
        ReportDate="2025-07-03",
    )
    events = factory.create_events_from_cash_transactions([rct])
    assert len(events) == 1
    return events[0]


class TestParserMapping:
    def test_broker_interest_paid_maps_to_debit_type_stored_positive(self):
        evt = _cash_event_for(Decimal("-26.06"))
        assert evt.event_type == FinancialEventType.INTEREST_PAID_DEBIT
        assert evt.gross_amount_foreign_currency == Decimal("26.06")

    def test_broker_interest_paid_refund_stored_negative(self):
        # A positive "Broker Interest Paid" row is a refund/reversal of a
        # prior charge — it must net against the charge, not become income.
        evt = _cash_event_for(Decimal("5"))
        assert evt.event_type == FinancialEventType.INTEREST_PAID_DEBIT
        assert evt.gross_amount_foreign_currency == Decimal("-5")

    def test_broker_interest_received_still_income(self):
        evt = _cash_event_for(
            Decimal("3.21"),
            type_str="Broker Interest Received",
            description="USD CREDIT INT FOR JUN-2025",
            currency="USD",
        )
        assert evt.event_type == FinancialEventType.INTEREST_RECEIVED
        assert evt.gross_amount_foreign_currency == Decimal("3.21")

    def test_debit_interest_description_routing(self):
        # Some exports carry the direction only in the description.
        evt = _cash_event_for(
            Decimal("-1.5"),
            type_str="Broker Interest",
            description="DEBIT INTEREST FOR JUN-2025",
        )
        assert evt.event_type == FinancialEventType.INTEREST_PAID_DEBIT
        assert evt.gross_amount_foreign_currency == Decimal("1.5")


# ---------------------------------------------------------------------------
# CZ aggregation (EUR mode, no FX provider needed)
# ---------------------------------------------------------------------------

def _resolver():
    class D(AssetClassifier):
        def __init__(self): super().__init__(cache_file_path="d.json")
        def save_classifications(self): pass
    return AssetResolver(asset_classifier=D())


def _interest_event(event_type: FinancialEventType, amount: Decimal, event_date: str):
    return CashFlowEvent(
        asset_internal_id=uuid.uuid4(),
        event_date=event_date,
        event_type=event_type,
        gross_amount_foreign_currency=amount,
        local_currency="EUR",
        gross_amount_eur=amount,
    )


def _aggregate(events):
    cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
    aggregator = CzechTaxAggregator(config=cfg)
    return aggregator.aggregate([], events, _resolver(), 2025)


class TestCzAggregation:
    def _events(self):
        return [
            _interest_event(FinancialEventType.INTEREST_RECEIVED, Decimal("25"), "2025-06-15"),
            _interest_event(FinancialEventType.INTEREST_PAID_DEBIT, Decimal("10"), "2025-07-03"),
            _interest_event(FinancialEventType.INTEREST_PAID_DEBIT, Decimal("5"), "2025-08-05"),
        ]

    def test_debit_interest_excluded_from_section_8(self):
        sec = _aggregate(self._events()).sections["cz_8_interest"]
        assert sec.line_items["gross_interest_eur"] == Decimal("25.00")
        assert sec.line_items["item_count"] == Decimal("1")

    def test_debit_interest_excluded_from_ftc(self):
        ftc = _aggregate(self._events()).sections["cz_ftc_summary"]
        assert ftc.line_items["ftc_item_count"] == Decimal("1")

    def test_exclusion_note_present(self):
        sec = _aggregate(self._events()).sections["cz_8_interest"]
        notes = " | ".join(sec.notes)
        assert "2 margin/debit interest charge(s)" in notes
        assert "15.00 EUR" in notes

    def test_no_note_without_debit_interest(self):
        events = [_interest_event(FinancialEventType.INTEREST_RECEIVED, Decimal("25"), "2025-06-15")]
        sec = _aggregate(events).sections["cz_8_interest"]
        assert not any("margin/debit" in n for n in sec.notes)

    def test_liability_base_uses_income_only(self):
        tl = _aggregate(self._events()).sections["cz_tax_liability"]
        assert tl.line_items["taxable_interest_eur"] == Decimal("25.00")
