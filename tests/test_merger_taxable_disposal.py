# tests/test_merger_taxable_disposal.py
"""
The TAXABLE_DISPOSAL half of a stock-for-stock merger.

This is the path an ordinary US merger takes — outside §23b/§23c the exchange is
a disposal of the old shares at the fair value of what was received, and the new
shares start a fresh basis and a fresh holding period. Since the advisor's answer
puts most of this portfolio's mergers here, it is the practically relevant half.

Fair value comes from the injected price provider (a past close, §19 zákona o
oceňování majetku with its 30-day look-back) converted at that date. Both are
refused rather than guessed: a merger valued at the wrong price is a wrong §10
gain, and the disposal is not visible anywhere else to catch it.
"""
import datetime
import json
import uuid
from decimal import Decimal

import pytest

from src.countries.cz.merger_treatment import CzMergerPolicy, MergerTreatmentStore
from src.domain.events import CorpActionMergerStock
from src.engine.event_processors.corporate_action_processor import MergerStockProcessor
from src.engine.fifo_manager import FifoLot, ShortFifoLot
from src.utils.historical_price_provider import HistoricalPrice
from tests.test_audit_fixes import _make_ledger

MERGER_DATE = "2025-06-10"


def _lot(acq, qty, total, tx, hps=None, hps_est=False, acq_est=False):
    q, t = Decimal(qty), Decimal(total)
    return FifoLot(
        acquisition_date=acq, quantity=q,
        unit_cost_basis_eur=t / q, total_cost_basis_eur=t,
        source_transaction_id=tx,
        holding_period_start=hps, holding_period_start_estimated=hps_est,
        acquisition_date_estimated=acq_est,
    )


class _Asset:
    def __init__(self, symbol, currency="USD"):
        self.ibkr_symbol = symbol
        self.currency = currency


class _Resolver:
    def __init__(self, m):
        self._m = m

    def get_asset_by_id(self, aid):
        return self._m.get(aid)


class _Prices:
    """Injected price provider: returns a fixed close, or nothing."""

    def __init__(self, price=None, currency="USD", price_date=None, lookback=False):
        self.price = None if price is None else Decimal(price)
        self.currency = currency
        self.price_date = price_date or datetime.date(2025, 6, 10)
        self.lookback = lookback
        self.calls = []

    def get_close(self, symbol, currency, on_date):
        self.calls.append((symbol, currency, on_date))
        if self.price is None:
            return None
        return HistoricalPrice(
            ibkr_symbol=symbol, yahoo_symbol=symbol,
            requested_date=on_date, price_date=self.price_date,
            price=self.price, currency=self.currency, source="test",
        )


class _Converter:
    """EUR per unit of the quote currency; None to simulate a failure."""

    def __init__(self, rate="1"):
        self.rate = None if rate is None else Decimal(rate)
        self.calls = []

    def convert_to_eur(self, original_amount, original_currency, date_of_conversion):
        self.calls.append((original_amount, original_currency, date_of_conversion))
        if self.rate is None:
            return None
        return original_amount * self.rate


def _setup(tmp_path, *, ratio="1", source_lots=None, short_lots=None,
           prices=None, converter=None, regime="outside_safe_harbor"):
    old_id, new_id = uuid.uuid4(), uuid.uuid4()

    source = _make_ledger()
    source.asset_internal_id = old_id
    for l in (source_lots or []):
        source.lots.append(l)
    for l in (short_lots or []):
        source.short_lots.append(l)

    target = _make_ledger()
    target.asset_internal_id = new_id

    event = CorpActionMergerStock(
        asset_internal_id=old_id, event_date=MERGER_DATE,
        new_asset_internal_id=new_id,
        new_shares_received_per_old=Decimal(ratio),
        ca_action_id_ibkr="A1",
    )

    path = tmp_path / "mergers.json"
    path.write_text(json.dumps({f"A1|{MERGER_DATE}|OLDCO->NEWCO": regime}),
                    encoding="utf-8")

    ledgers = {old_id: source, new_id: target}
    ctx = {
        "asset_resolver": _Resolver({old_id: _Asset("OLDCO"),
                                     new_id: _Asset("NEWCO")}),
        "merger_policy": CzMergerPolicy(
            store=MergerTreatmentStore(cache_file_path=path)),
        "ledger_for": lambda aid: ledgers[aid],
        "applied_merger_keys": set(),
        "security_price_provider": prices if prices is not None else _Prices("50"),
        "currency_converter": converter if converter is not None else _Converter("1"),
    }
    return event, source, target, ctx


class TestTheDisposal:
    def test_gain_is_fair_value_minus_the_lot_basis(self, tmp_path):
        # 10 old shares, basis 100 EUR. Ratio 1, FV 50 USD @ 1.0 -> 500 EUR.
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        out = MergerStockProcessor().process(event, source, ctx)

        assert len(out) == 1
        rgl = out[0]
        assert rgl.total_realization_value_eur == Decimal("500")
        assert rgl.total_cost_basis_eur == Decimal("100.00")
        assert rgl.gross_gain_loss_eur == Decimal("400.00")
        assert rgl.realization_date == MERGER_DATE

    def test_a_loss_is_reported_as_a_loss(self, tmp_path):
        """An acquirer trading below the old basis realises a real loss."""
        lots = [_lot("2020-01-10", "10", "900.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        out = MergerStockProcessor().process(event, source, ctx)
        assert out[0].gross_gain_loss_eur == Decimal("-400.00")

    def test_one_rgl_per_lot_each_keeping_its_own_dates(self, tmp_path):
        """The holding-period test applies to the OLD shares, per lot — a
        blended disposal would force one verdict onto three holdings."""
        lots = [
            _lot("2020-01-10", "10", "100.00", "t1"),
            _lot("2021-02-11", "10", "200.00", "t2"),
            _lot("2022-03-12", "10", "300.00", "t3"),
        ]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        out = MergerStockProcessor().process(event, source, ctx)

        assert len(out) == 3
        assert sorted(r.acquisition_date for r in out) == [
            "2020-01-10", "2021-02-11", "2022-03-12"]

    def test_holding_period_comes_from_the_old_shares_holding_start(self, tmp_path):
        """A previously carried holding still counts toward the time test on
        THIS disposal."""
        lots = [_lot("2023-05-01", "10", "100.00", "t1", hps="2013-04-02")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        rgl = MergerStockProcessor().process(event, source, ctx)[0]

        assert rgl.holding_period_start == "2013-04-02"
        assert rgl.acquisition_date == "2023-05-01"
        assert rgl.holding_period_days > 4000

    def test_estimated_flags_reach_the_disposal(self, tmp_path):
        lots = [_lot("2024-12-31", "10", "100.00", "SOY_FALLBACK_x")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        rgl = MergerStockProcessor().process(event, source, ctx)[0]
        assert rgl.is_acquisition_estimated is True
        assert rgl.holding_period_start_estimated is True

    def test_proceeds_scale_with_the_exchange_ratio(self, tmp_path):
        """Two new shares per old at 50 EUR each is 100 EUR per old share."""
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="2", source_lots=lots)

        rgl = MergerStockProcessor().process(event, source, ctx)[0]
        assert rgl.total_realization_value_eur == Decimal("1000")

    def test_the_quote_is_taken_for_the_new_share_at_the_merger_date(self, tmp_path):
        prices = _Prices("50")
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots, prices=prices)

        MergerStockProcessor().process(event, source, ctx)

        assert prices.calls == [("NEWCO", "USD", datetime.date(2025, 6, 10))]

    def test_the_fx_date_is_the_close_date_not_the_requested_date(self, tmp_path):
        """When the §19 look-back ran, the price is from an earlier day and the
        conversion has to use that day's rate, not the merger date's."""
        prices = _Prices("50", price_date=datetime.date(2025, 6, 6), lookback=True)
        conv = _Converter("1")
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots,
                                            prices=prices, converter=conv)

        MergerStockProcessor().process(event, source, ctx)

        assert conv.calls[0][2] == datetime.date(2025, 6, 6)


class TestTheReplacementLots:
    def test_new_lots_start_a_fresh_basis_and_holding_period(self, tmp_path):
        """Nothing carries over — this was a realisation, not a deferral."""
        lots = [_lot("2013-04-02", "10", "100.00", "t1", hps="2013-04-02")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        MergerStockProcessor().process(event, source, ctx)

        assert len(target.lots) == 1
        t = target.lots[0]
        assert t.acquisition_date == MERGER_DATE
        assert t.holding_period_start == MERGER_DATE      # NOT 2013
        assert t.holding_period_start_estimated is False
        assert t.unit_cost_basis_eur == Decimal("50")
        assert t.total_cost_basis_eur == Decimal("500")

    def test_the_new_basis_equals_the_proceeds_just_reported(self, tmp_path):
        """Otherwise the same value is taxed twice, or once and never."""
        lots = [
            _lot("2020-01-10", "10", "100.00", "t1"),
            _lot("2021-02-11", "5", "300.00", "t2"),
        ]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        out = MergerStockProcessor().process(event, source, ctx)

        proceeds = sum(r.total_realization_value_eur for r in out)
        new_basis = sum(l.total_cost_basis_eur for l in target.lots)
        assert proceeds == new_basis

    def test_source_ledger_is_emptied(self, tmp_path):
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, source_lots=lots)

        MergerStockProcessor().process(event, source, ctx)
        assert source.lots == []

    def test_credited_quantity_matches_the_ratio(self, tmp_path):
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="2", source_lots=lots)

        MergerStockProcessor().process(event, source, ctx)
        assert sum(l.quantity for l in target.lots) == Decimal("20.00000000")


class TestRefusals:
    def _lots(self):
        return [_lot("2020-01-10", "10", "100.00", "t1")]

    def test_no_price_available_refuses_and_says_what_to_do(self, tmp_path):
        """Valuing at zero would invent a total loss; skipping would drop a
        realised gain. Neither is visible anywhere else."""
        event, source, target, ctx = _setup(tmp_path, source_lots=self._lots(),
                                            prices=_Prices(None))
        with pytest.raises(ValueError, match="no price is available"):
            MergerStockProcessor().process(event, source, ctx)

    def test_no_price_provider_refuses(self, tmp_path):
        event, source, target, ctx = _setup(tmp_path, source_lots=self._lots())
        ctx["security_price_provider"] = None
        with pytest.raises(ValueError, match="no security price provider"):
            MergerStockProcessor().process(event, source, ctx)

    def test_failed_currency_conversion_refuses(self, tmp_path):
        event, source, target, ctx = _setup(tmp_path, source_lots=self._lots(),
                                            converter=_Converter(None))
        with pytest.raises(ValueError, match="cannot convert"):
            MergerStockProcessor().process(event, source, ctx)

    def test_no_converter_refuses(self, tmp_path):
        event, source, target, ctx = _setup(tmp_path, source_lots=self._lots())
        ctx["currency_converter"] = None
        with pytest.raises(ValueError, match="without a currency converter"):
            MergerStockProcessor().process(event, source, ctx)

    def test_empty_source_refuses(self, tmp_path):
        event, source, target, ctx = _setup(tmp_path, source_lots=[])
        with pytest.raises(ValueError, match="no long lots to realise"):
            MergerStockProcessor().process(event, source, ctx)

    def test_a_short_position_on_the_disposing_side_refuses(self, tmp_path):
        """Realising a short against the acquirer's price is not the same
        transaction as closing it, and the treatment is undecided."""
        shorts = [ShortFifoLot(
            opening_date="2024-03-01", quantity_shorted=Decimal("10"),
            unit_sale_proceeds_eur=Decimal("20"),
            total_sale_proceeds_eur=Decimal("200"),
            source_transaction_id="s1",
        )]
        event, source, target, ctx = _setup(tmp_path, source_lots=self._lots(),
                                            short_lots=shorts)
        with pytest.raises(ValueError, match="short lots"):
            MergerStockProcessor().process(event, source, ctx)

    def test_fractional_credited_share_still_refuses(self, tmp_path):
        """Same reasoning as the carry-over path: the cash-in-lieu disposal is
        not carried on this event."""
        lots = [_lot("2020-01-10", "100", "1000.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="0.6595",
                                            source_lots=lots)
        with pytest.raises(ValueError, match="cash in lieu"):
            MergerStockProcessor().process(event, source, ctx)

    def test_the_same_merger_twice_refuses(self, tmp_path):
        event, source, target, ctx = _setup(tmp_path, source_lots=self._lots())
        MergerStockProcessor().process(event, source, ctx)
        source.lots.append(_lot("2020-01-10", "10", "100.00", "t1"))

        with pytest.raises(ValueError, match="already applied"):
            MergerStockProcessor().process(event, source, ctx)

    def test_guards_are_shared_with_the_carry_over_path(self, tmp_path):
        """One guard set, so a refusal cannot exist on only one branch."""
        event, source, target, ctx = _setup(tmp_path, source_lots=self._lots())
        object.__setattr__(event, "new_shares_received_per_old", Decimal("0"))
        with pytest.raises(ValueError, match="unusable exchange ratio"):
            MergerStockProcessor().process(event, source, ctx)
