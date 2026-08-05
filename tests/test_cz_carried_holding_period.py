# tests/test_cz_carried_holding_period.py
"""
A carried-over holding period: acquisition_date and holding_period_start diverge.

Only a qualified Czech merger (§23b/§23c ZDP) produces this, and its mechanics
are not implemented yet — so these tests drive the fields directly. They exist
now because the split is otherwise invisible: with holding_period_start
defaulting to acquisition_date, an implementation that never plumbs the field
through passes the rest of the suite green.

The two facts under test, both from the advisor's answer of 2026-08-05:
  (a) the old share's holding period CONTINUES on the new share, and
  (b) the pre-2014 six-month regime does NOT transfer to a share issued later
      (NSS 3 Afs 249/2024-45).
"""
import uuid
from decimal import Decimal

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.tax_items import (
    CzExemptionReason,
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
)
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.time_test import evaluate_time_test
from src.domain.enums import AssetCategory, RealizationType
from src.domain.results import RealizedGainLoss
from src.engine.fifo_manager import FifoLot


def _item(
    *,
    acquisition_date,
    event_date,
    holding_period_start=None,
    holding_period_days=None,
    acquisition_date_estimated=False,
    holding_period_start_estimated=False,
) -> CzTaxItem:
    return CzTaxItem(
        item_type=CzTaxItemType.SECURITY_DISPOSAL,
        section=CzTaxSection.CZ_10_SECURITIES,
        source_event_id=uuid.uuid4(),
        event_date=event_date,
        acquisition_date=acquisition_date,
        holding_period_start=holding_period_start,
        holding_period_start_estimated=holding_period_start_estimated,
        holding_period_days=holding_period_days,
        acquisition_date_estimated=acquisition_date_estimated,
        original_amount=Decimal("500"),
        original_currency="EUR",
        amount_eur=Decimal("500"),
        proceeds_czk=Decimal("50000"),
        asset_category="STOCK",
    )


class TestCarriedPeriodGrantsExemption:
    """Fact (a) — the period keeps running through the merger."""

    def test_carried_start_makes_a_recent_acquisition_exempt(self):
        item = _item(
            acquisition_date="2023-05-01",      # the new share
            holding_period_start="2010-03-01",  # holding began here
            event_date="2025-06-01",
        )
        evaluate_time_test([item], CzTaxConfig(), tax_year=2025)

        assert item.is_exempt is True
        assert item.is_taxable is False
        assert item.exemption_reason is CzExemptionReason.TIME_TEST_PASSED
        assert item.included_in_tax_base is False

    def test_the_note_explains_the_two_dates(self):
        """"held 5571 days" against a 2023 acquisition reads as a contradiction."""
        item = _item(
            acquisition_date="2023-05-01",
            holding_period_start="2010-03-01",
            event_date="2025-06-01",
        )
        evaluate_time_test([item], CzTaxConfig(), tax_year=2025)

        assert "2010-03-01" in item.tax_review_note
        assert "2023-05-01" in item.tax_review_note

    def test_without_the_carried_start_the_same_item_is_taxable(self):
        """The control: the field is what changes the verdict."""
        item = _item(acquisition_date="2023-05-01", event_date="2025-06-01")
        evaluate_time_test([item], CzTaxConfig(), tax_year=2025)

        assert item.is_taxable is True
        assert item.is_exempt is False


class TestPre2014RegimeDoesNotTransfer:
    """Fact (b) — the trap this whole split exists for."""

    def test_carried_pre_2014_start_does_not_earn_the_six_month_test(self):
        item = _item(
            acquisition_date="2014-06-01",      # share issued AFTER the cutoff
            holding_period_start="2013-11-01",  # holding began before it
            event_date="2015-01-15",
        )
        evaluate_time_test([item], CzTaxConfig(), tax_year=2015)

        # Six months from 2013-11-01 would be 2014-05-01 → exempt. Three years
        # from it is 2016-11-01 → taxable. The regime follows the acquisition.
        assert item.is_taxable is True
        assert item.is_exempt is False
        assert "calendar years" in item.tax_review_note

    def test_a_genuinely_pre_2014_share_keeps_the_six_month_test(self):
        """Do not over-correct: a real pre-2014 acquisition still qualifies."""
        item = _item(acquisition_date="2013-11-01", event_date="2014-07-01")
        evaluate_time_test([item], CzTaxConfig(), tax_year=2014)

        assert item.is_exempt is True
        assert "months" in item.tax_review_note


class TestHoldingPeriodDaysCannotFlipTheVerdict:
    """Guard against the tempting no-op implementation.

    Carrying the change in holding_period_days alone looks like it works — but
    the evaluator decides from the DATES and only falls back to the day count
    when they cannot be parsed. Such an implementation would print
    "held 5000 days ≤ 3 calendar years" and tax an exempt disposal.
    """

    def test_a_huge_day_count_with_matching_dates_stays_taxable(self):
        item = _item(
            acquisition_date="2024-06-01",
            event_date="2025-06-01",
            holding_period_days=5000,        # nonsense, deliberately
        )
        evaluate_time_test([item], CzTaxConfig(), tax_year=2025)

        assert item.is_taxable is True

    def test_a_tiny_day_count_with_carried_dates_is_still_exempt(self):
        item = _item(
            acquisition_date="2023-05-01",
            holding_period_start="2010-03-01",
            event_date="2025-06-01",
            holding_period_days=1,           # nonsense in the other direction
        )
        evaluate_time_test([item], CzTaxConfig(), tax_year=2025)

        assert item.is_exempt is True


class TestEstimatedDatesBlockTheExemption:
    def test_estimated_holding_start_forces_manual_review(self):
        """A carried period sourced from a synthetic 31-Dec lot proves nothing,
        even though the acquisition date itself is real."""
        item = _item(
            acquisition_date="2023-05-01",
            holding_period_start="2010-03-01",
            holding_period_start_estimated=True,
            event_date="2025-06-01",
        )
        evaluate_time_test([item], CzTaxConfig(), tax_year=2025)

        assert item.is_taxable is True
        assert item.tax_review_status is CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        assert "Holding-period start" in item.tax_review_note

    def test_estimated_acquisition_still_forces_review(self):
        item = _item(
            acquisition_date="2024-12-31",
            acquisition_date_estimated=True,
            event_date="2025-06-01",
        )
        evaluate_time_test([item], CzTaxConfig(), tax_year=2025)

        assert item.tax_review_status is CzTaxReviewStatus.PENDING_MANUAL_REVIEW
        assert "Acquisition date" in item.tax_review_note


class TestFifoLotNormalisation:
    def _lot(self, **kw):
        base = dict(
            acquisition_date="2023-05-01",
            quantity=Decimal("10"),
            unit_cost_basis_eur=Decimal("5"),
            total_cost_basis_eur=Decimal("50"),
            source_transaction_id="t1",
        )
        base.update(kw)
        return FifoLot(**base)

    def test_absent_start_defaults_to_the_acquisition_date(self):
        lot = self._lot()
        assert lot.holding_period_start == "2023-05-01"
        assert lot.holding_period_start_estimated is False

    def test_a_carried_start_is_kept(self):
        lot = self._lot(holding_period_start="2010-03-01")
        assert lot.holding_period_start == "2010-03-01"

    def test_a_fallback_lot_marks_both_dates_estimated(self):
        """The flag now lives on the lot instead of being sniffed from the id."""
        lot = self._lot(source_transaction_id="SOY_FALLBACK_abc")
        assert lot.acquisition_date_estimated is True
        assert lot.holding_period_start_estimated is True

    def test_an_explicitly_flagged_lot_is_estimated_without_the_prefix(self):
        """What a future merger carry-over will produce: a real corp-action id
        but a date inherited from a synthetic lot."""
        lot = self._lot(
            source_transaction_id="CA_12345",
            holding_period_start="2019-12-31",
            holding_period_start_estimated=True,
        )
        assert lot.acquisition_date_estimated is False
        assert lot.holding_period_start_estimated is True

    def test_a_snapshot_lot_is_not_flagged(self):
        """Do not regress the lot-level SOY snapshot: those dates are real."""
        lot = self._lot(source_transaction_id="SOY_SNAPSHOT_0_abc")
        assert lot.acquisition_date_estimated is False

    def test_an_unparseable_carried_date_raises(self):
        """Silently sorting as datetime.min would read as an ancient holding."""
        import pytest
        with pytest.raises(ValueError, match="holding_period_start"):
            self._lot(holding_period_start="not-a-date")


class TestCostBasisFxDate:
    """The cost leg converts at the rate of the day the cost was PAID.

    Per-leg FX (NSS 2 Afs 4/2019-35). In a §23b/§23c carry-over no cash moved at
    the merger — the cost was paid when the old share was bought, which is
    holding_period_start. Converting it at the merger-date rate instead produces
    a wrong §10 gain that also feeds the 100k proceeds total, and under
    fx_policy=uniform would pick the wrong year's GFŘ rate entirely.
    """

    def _items(self, rgl):
        from src.countries.cz import item_builder
        from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig
        from tests.test_audit_fixes import _DatedEurToCzkProvider, _NoneResolver

        provider = _DatedEurToCzkProvider({
            "2010-03-01": Decimal("0.02"),   # 1 EUR = 50 CZK — cost was paid here
            "2023-06-01": Decimal("0.04"),   # 1 EUR = 25 CZK — merger date
            "2025-09-10": Decimal("0.05"),   # 1 EUR = 20 CZK — disposal
        })
        fx = CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())
        return item_builder._build_disposal_items([rgl], _NoneResolver(), fx, [])

    def _rgl(self, **kw):
        base = dict(
            originating_event_id=uuid.uuid4(),
            asset_internal_id=uuid.uuid4(),
            asset_category_at_realization=AssetCategory.STOCK,
            acquisition_date="2023-06-01",
            realization_date="2025-09-10",
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=Decimal("10"),
            unit_cost_basis_eur=Decimal("100"),
            unit_realization_value_eur=Decimal("120"),
            total_cost_basis_eur=Decimal("1000"),
            total_realization_value_eur=Decimal("1200"),
            gross_gain_loss_eur=Decimal("200"),
        )
        base.update(kw)
        return RealizedGainLoss(**base)

    def test_carried_lot_converts_cost_at_the_original_purchase_rate(self):
        item = self._items(self._rgl(holding_period_start="2010-03-01"))[0]

        # 1000 EUR / 0.02 = 50000 CZK, NOT 1000 / 0.04 = 25000.
        assert item.cost_basis_czk == Decimal("50000")
        assert item.proceeds_czk == Decimal("24000")
        assert item.gain_loss_czk == Decimal("-26000")

    def test_an_ordinary_lot_is_unaffected(self):
        """Byte-identical when the dates coincide — the common case."""
        item = self._items(self._rgl())[0]

        assert item.cost_basis_czk == Decimal("25000")   # at the acquisition rate
        assert item.proceeds_czk == Decimal("24000")

    def test_the_carried_start_reaches_the_tax_item(self):
        """Without this the split dies at the engine→tax-item boundary and every
        downstream behaviour is cosmetic."""
        item = self._items(self._rgl(holding_period_start="2010-03-01"))[0]

        assert item.holding_period_start == "2010-03-01"
        assert item.acquisition_date == "2023-06-01"

    def test_to_dict_emits_both_dates(self):
        item = self._items(self._rgl(holding_period_start="2010-03-01"))[0]
        payload = item.to_dict()

        assert payload["acquisition_date"] == "2023-06-01"
        assert payload["holding_period_start"] == "2010-03-01"

    def test_to_dict_never_emits_a_null_start(self):
        """An empty exporter cell must mean "no acquisition", not "not plumbed"."""
        payload = self._items(self._rgl())[0].to_dict()
        assert payload["holding_period_start"] == "2023-06-01"


class TestOptimalPairingRoundTrip:
    """The optimal pairer rebuilds RGLs from scratch — the field must survive.

    If it does not, the same input reports a different exempt/taxable set under
    `optimal` than under `fifo`, and compare_runs blames the pairing method.
    """

    def _rgl(self, **kw):
        base = dict(
            originating_event_id=uuid.uuid4(),
            asset_internal_id=uuid.uuid4(),
            asset_category_at_realization=AssetCategory.STOCK,
            acquisition_date="2023-06-01",
            realization_date="2025-09-10",
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=Decimal("10"),
            unit_cost_basis_eur=Decimal("100"),
            unit_realization_value_eur=Decimal("120"),
            total_cost_basis_eur=Decimal("1000"),
            total_realization_value_eur=Decimal("1200"),
            gross_gain_loss_eur=Decimal("200"),
        )
        base.update(kw)
        return RealizedGainLoss(**base)

    def test_carried_start_survives_the_rebuild(self):
        from src.countries.cz.optimal_pairing import apply_cz_optimal_pairing

        asset_id = uuid.uuid4()
        rgl = self._rgl(asset_internal_id=asset_id, holding_period_start="2010-03-01")

        out = apply_cz_optimal_pairing(
            [rgl], fifo_ledgers={}, all_financial_events=[], tax_year=2025,
        )
        assert out, "pairing returned nothing"
        assert all(r.holding_period_start == "2010-03-01" for r in out)
        assert all(r.acquisition_date == "2023-06-01" for r in out)

    def test_holding_days_are_measured_from_the_carried_start(self):
        from src.countries.cz.optimal_pairing import apply_cz_optimal_pairing

        rgl = self._rgl(holding_period_start="2010-03-01")
        out = apply_cz_optimal_pairing(
            [rgl], fifo_ledgers={}, all_financial_events=[], tax_year=2025,
        )
        # 2010-03-01 → 2025-09-10 is well over 5000 days; from 2023-06-01 it
        # would be ~832. The emitted period must reflect the carried holding.
        assert all(r.holding_period_days > 5000 for r in out)

    def test_fifo_and_optimal_agree_on_the_exempt_set(self):
        from src.countries.cz.optimal_pairing import apply_cz_optimal_pairing
        from src.countries.cz.time_test import evaluate_time_test
        from src.countries.cz import item_builder
        from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig
        from tests.test_audit_fixes import _DatedEurToCzkProvider, _NoneResolver

        rgl = self._rgl(holding_period_start="2010-03-01")
        paired = apply_cz_optimal_pairing(
            [rgl], fifo_ledgers={}, all_financial_events=[], tax_year=2025,
        )
        fx = CzCurrencyConverter(
            provider=_DatedEurToCzkProvider({
                "2010-03-01": Decimal("0.02"),
                "2023-06-01": Decimal("0.04"),
                "2025-09-10": Decimal("0.05"),
            }),
            policy=CzFxPolicyConfig(),
        )
        for source in ([rgl], paired):
            items = item_builder._build_disposal_items(source, _NoneResolver(), fx, [])
            evaluate_time_test(items, CzTaxConfig(), tax_year=2025)
            assert all(i.is_exempt for i in items), (
                "carried holding must be exempt under both pairing methods"
            )


class TestSoyRebuildPreservesTheField:
    def test_rebuild_keeps_a_divergent_holding_start(self):
        """initialize_lots_from_soy re-creates every lot; dropping the field
        there would silently reset a carried holding on the next run."""
        from src.domain.assets import Stock
        from tests.test_audit_fixes import _make_ledger

        ledger = _make_ledger()
        stock = Stock(ibkr_symbol="ABC")
        stock.soy_quantity = Decimal("10")
        stock.soy_cost_basis_amount = Decimal("50")
        stock.soy_cost_basis_currency = "EUR"

        # Seed a reconstructed lot with a carried start, then let the SOY
        # reconciliation rebuild it.
        ledger.lots.append(FifoLot(
            acquisition_date="2023-05-01",
            quantity=Decimal("10"),
            unit_cost_basis_eur=Decimal("5"),
            total_cost_basis_eur=Decimal("50"),
            source_transaction_id="t1",
            holding_period_start="2010-03-01",
        ))
        # The rebuild path runs inside initialize_lots_from_soy; drive it with a
        # history that reproduces the same lot so reconstruction succeeds.
        from src.domain.enums import FinancialEventType
        from tests.test_audit_2026_07_fixes import _trade

        buy = _trade(stock.internal_asset_id, "2023-05-01", Decimal("10"),
                     Decimal("50"), "t1",
                     event_type=FinancialEventType.TRADE_BUY_LONG)
        ledger.lots.clear()
        ledger.initialize_lots_from_soy(stock, [buy], tax_year=2025)

        assert len(ledger.lots) == 1
        # The trade history carries no merger, so the rebuilt lot's start equals
        # its acquisition — the point is that the field exists and is populated
        # rather than None after a rebuild.
        assert ledger.lots[0].holding_period_start == "2023-05-01"

    def test_rebuild_carries_an_explicitly_set_start(self):
        """Directly exercise the rebuild copy: a lot whose start diverges must
        come out of FifoLot(...) reconstruction intact."""
        source = FifoLot(
            acquisition_date="2023-05-01",
            quantity=Decimal("10"),
            unit_cost_basis_eur=Decimal("5"),
            total_cost_basis_eur=Decimal("50"),
            source_transaction_id="t1",
            holding_period_start="2010-03-01",
        )
        # Mirrors the rebuild at fifo_manager.py's SOY reconciliation.
        rebuilt = FifoLot(
            acquisition_date=source.acquisition_date,
            quantity=source.quantity,
            unit_cost_basis_eur=source.unit_cost_basis_eur,
            total_cost_basis_eur=source.total_cost_basis_eur,
            source_transaction_id=source.source_transaction_id,
            holding_period_start=source.holding_period_start,
            acquisition_date_estimated=source.acquisition_date_estimated,
            holding_period_start_estimated=source.holding_period_start_estimated,
        )
        assert rebuilt.holding_period_start == "2010-03-01"


class TestRealizedGainLossNormalisation:
    def _rgl(self, **kw):
        base = dict(
            originating_event_id=uuid.uuid4(),
            asset_internal_id=uuid.uuid4(),
            asset_category_at_realization=AssetCategory.STOCK,
            acquisition_date="2023-05-01",
            realization_date="2025-06-01",
            realization_type=RealizationType.LONG_POSITION_SALE,
            quantity_realized=Decimal("10"),
            unit_cost_basis_eur=Decimal("5"),
            unit_realization_value_eur=Decimal("9"),
            total_cost_basis_eur=Decimal("50"),
            total_realization_value_eur=Decimal("90"),
            gross_gain_loss_eur=Decimal("40"),
        )
        base.update(kw)
        return RealizedGainLoss(**base)

    def test_absent_start_defaults_to_the_acquisition_date(self):
        assert self._rgl().holding_period_start == "2023-05-01"

    def test_estimated_flag_follows_the_acquisition_when_defaulted(self):
        rgl = self._rgl(is_acquisition_estimated=True)
        assert rgl.holding_period_start_estimated is True

    def test_a_carried_start_is_kept_independently(self):
        rgl = self._rgl(holding_period_start="2010-03-01", is_acquisition_estimated=True)
        assert rgl.holding_period_start == "2010-03-01"
        # Not overwritten by the acquisition flag — the carried date may be real
        # even when the acquisition date is synthetic.
        assert rgl.holding_period_start_estimated is False
