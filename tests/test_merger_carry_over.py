# tests/test_merger_carry_over.py
"""
The §23b/§23c CARRY_OVER lot transfer.

Each test is named by the wrong figure it prevents. The two that matter most:

* the aggregate transferred quantity must hit its target EXACTLY — a 1e-8
  shortfall makes the next full-position sale abort the entire run, so the tax
  year produces nothing at all;
* the total cost basis must be preserved verbatim, never scaled by the ratio,
  or the §10 gain is wrong by a factor of the ratio.
"""
import json
import uuid
from decimal import Decimal

import pytest

from src.countries.cz.merger_treatment import CzMergerPolicy, MergerTreatmentStore
from src.domain.events import CorpActionMergerStock
from src.engine.event_processors.corporate_action_processor import MergerStockProcessor
from src.engine.fifo_manager import FifoLot, ShortFifoLot, _allocate_rescaled_quantities
from tests.test_audit_fixes import _make_ledger


def _lot(acq, qty, total, tx, hps=None, hps_est=False):
    q = Decimal(qty)
    t = Decimal(total)
    return FifoLot(
        acquisition_date=acq,
        quantity=q,
        unit_cost_basis_eur=t / q,
        total_cost_basis_eur=t,
        source_transaction_id=tx,
        holding_period_start=hps,
        holding_period_start_estimated=hps_est,
    )


def _short_lot(open_date, qty, total, tx, est=False):
    q = Decimal(qty)
    t = Decimal(total)
    return ShortFifoLot(
        opening_date=open_date,
        quantity_shorted=q,
        unit_sale_proceeds_eur=t / q,
        total_sale_proceeds_eur=t,
        source_transaction_id=tx,
        opening_date_estimated=est,
    )


class _Asset:
    def __init__(self, symbol):
        self.ibkr_symbol = symbol


class _Resolver:
    def __init__(self, mapping):
        self._m = mapping

    def get_asset_by_id(self, asset_id):
        return self._m.get(asset_id)


def _setup(tmp_path, *, ratio="2.5", regime="qualified_23c", source_lots=None,
           short_lots=None, target_lots=None, merger_date="2025-06-10",
           quantity_exchanged=None, same_asset=False):
    """Wire a source ledger, a target ledger and a decided policy."""
    old_id = uuid.uuid4()
    new_id = old_id if same_asset else uuid.uuid4()

    source = _make_ledger()
    source.asset_internal_id = old_id
    for lot in (source_lots or []):
        source.lots.append(lot)
    for lot in (short_lots or []):
        source.short_lots.append(lot)

    target = _make_ledger()
    target.asset_internal_id = new_id
    for lot in (target_lots or []):
        target.lots.append(lot)

    event = CorpActionMergerStock(
        asset_internal_id=old_id,
        event_date=merger_date,
        new_asset_internal_id=new_id,
        new_shares_received_per_old=None if ratio is None else Decimal(ratio),
        quantity_exchanged=quantity_exchanged,
        ca_action_id_ibkr="A1",
    )

    path = tmp_path / "mergers.json"
    key_guess = f"A1|{merger_date}|OLDCO->NEWCO" if not same_asset else f"A1|{merger_date}|OLDCO->OLDCO"
    path.write_text(json.dumps({key_guess: regime}), encoding="utf-8")
    policy = CzMergerPolicy(store=MergerTreatmentStore(cache_file_path=path))

    ledgers = {old_id: source, new_id: target}
    context = {
        "asset_resolver": _Resolver({
            old_id: _Asset("OLDCO"),
            new_id: _Asset("OLDCO" if same_asset else "NEWCO"),
        }),
        "merger_policy": policy,
        "ledger_for": lambda aid: ledgers[aid],
        "applied_merger_keys": set(),
    }
    return event, source, target, context


class TestBasisAndQuantity:
    def test_total_basis_is_preserved_across_the_rescale(self, tmp_path):
        """Scaling the total would multiply the acquisition cost by the ratio."""
        lots = [
            _lot("2020-01-10", "100", "1000.00", "t1"),
            _lot("2021-02-11", "100", "1100.00", "t2"),
            _lot("2022-03-12", "100", "900.00", "t3"),
        ]
        event, source, target, ctx = _setup(tmp_path, ratio="0.3333333333333333333333333333",
                                            source_lots=lots)
        assert MergerStockProcessor().process(event, source, ctx) == []

        assert sum(l.total_cost_basis_eur for l in target.lots) == Decimal("3000.00")

    def test_unit_cost_is_rederived_from_the_preserved_total(self, tmp_path):
        """A stale unit mis-costs the sale AND overwrites the preserved total on
        the next partial disposal."""
        lots = [_lot("2020-01-10", "100", "1000.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="2.5", source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        for l in target.lots:
            assert abs(l.quantity * l.unit_cost_basis_eur - l.total_cost_basis_eur) < Decimal("1e-20")

    def test_transferred_quantity_sums_to_the_target_exactly(self, tmp_path):
        """A 1e-8 shortfall aborts the whole run at the next full sale."""
        lots = [_lot("2020-01-0%d" % (i + 1), "100", "1000.00", f"t{i}") for i in range(3)]
        event, source, target, ctx = _setup(tmp_path, ratio="0.3333333333333333333333333333",
                                            source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        assert sum(l.quantity for l in target.lots) == Decimal("100.00000000")

    def test_no_overshoot_either(self, tmp_path):
        """An overshoot leaves a 1e-8 ghost lot that trips EOY reconciliation."""
        lots = [
            _lot("2020-01-01", "100", "1000.00", "t1"),
            _lot("2020-01-02", "100", "1000.00", "t2"),
            _lot("2020-01-03", "80", "800.00", "t3"),
        ]
        ratio = Decimal(3) / Decimal(7)
        event, source, target, ctx = _setup(tmp_path, ratio=str(ratio), source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        total = sum(l.quantity for l in target.lots)
        assert total == Decimal("120.00000000"), total

    def test_the_residual_lands_on_the_oldest_holding(self, tmp_path):
        """Otherwise the extra 1e-8 moves between runs of identical input."""
        keys = [(Decimal(2), 2), (Decimal(0), 0), (Decimal(1), 1)]
        out = _allocate_rescaled_quantities(
            [Decimal("100")] * 3,
            Decimal(1) / Decimal(3),
            keys,
            _make_ledger().ctx,
        )
        # index 1 has the earliest order key, so it absorbs the residual
        assert out[1] > out[0] and out[1] > out[2]

    def test_one_target_lot_per_source_lot_no_coalescing(self, tmp_path):
        """A blended lot forces one exemption verdict, one estimated flag and one
        FX date onto shares that have three."""
        lots = [
            _lot("2020-01-10", "10", "100.00", "t1", hps="2013-01-10"),
            _lot("2021-02-11", "10", "200.00", "t2", hps="2016-02-11"),
            _lot("2022-03-12", "10", "300.00", "t3", hps="2019-03-12"),
        ]
        event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        assert len(target.lots) == 3
        assert sorted(l.holding_period_start for l in target.lots) == [
            "2013-01-10", "2016-02-11", "2019-03-12"]


class TestDatesAndFlags:
    def test_acquisition_date_is_the_merger_date_not_the_source_date(self, tmp_path):
        """Copying the source date hands a post-2014 share the pre-2014
        six-month regime."""
        lots = [_lot("2013-04-02", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        assert target.lots[0].acquisition_date == "2025-06-10"
        assert target.lots[0].holding_period_start == "2013-04-02"

    def test_holding_period_start_survives_two_chained_mergers(self, tmp_path):
        """Reading the source's acquisition_date instead loses the origin at the
        SECOND link: 2013 buy -> 2016 merger -> 2022 merger."""
        first = [_lot("2013-04-02", "10", "100.00", "t1")]
        e1, s1, t1, c1 = _setup(tmp_path, ratio="1", source_lots=first,
                                merger_date="2016-05-05")
        MergerStockProcessor().process(e1, s1, c1)
        assert t1.lots[0].holding_period_start == "2013-04-02"

        # Second merger, whose source is the lot produced by the first.
        second_dir = tmp_path / "b"
        second_dir.mkdir()
        e2, s2, t2, c2 = _setup(second_dir, ratio="1", source_lots=list(t1.lots),
                                merger_date="2022-07-07")
        MergerStockProcessor().process(e2, s2, c2)

        assert t2.lots[0].acquisition_date == "2022-07-07"
        assert t2.lots[0].holding_period_start == "2013-04-02"

    def test_holding_period_start_estimated_is_carried(self, tmp_path):
        """Otherwise a synthetic 31-Dec start reads as real and the preparer is
        never told the date was invented."""
        lots = [_lot("2020-01-10", "10", "100.00", "t1",
                     hps="2019-12-31", hps_est=True)]
        event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        assert target.lots[0].holding_period_start_estimated is True

    def test_acquisition_date_estimated_is_false_on_the_target(self, tmp_path):
        """The merger date is real — footnoting it as an estimate buries the one
        genuinely unproven flag."""
        lots = [_lot("2024-12-31", "10", "100.00", "SOY_FALLBACK_x")]
        assert lots[0].acquisition_date_estimated is True   # source is synthetic

        event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        assert target.lots[0].acquisition_date_estimated is False
        # ...but the carried start keeps the truth about itself
        assert target.lots[0].holding_period_start_estimated is True

    def test_source_id_is_the_broker_action_id_and_stable(self, tmp_path):
        """A per-run uuid4 in the FIFO tie-break makes two identical runs order
        lots differently, with nothing to explain the delta."""
        ids = []
        for _ in range(2):
            lots = [_lot("2020-01-10", "10", "100.00", "t1")]
            event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)
            MergerStockProcessor().process(event, source, ctx)
            ids.append(target.lots[0].source_transaction_id)
        assert ids[0] == ids[1] == "A1"

    def test_provenance_records_the_funding_purchase(self, tmp_path):
        lots = [_lot("2020-01-10", "10", "100.00", "1234567890")]
        event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        assert target.lots[0].carried_from == "OLDCO:1234567890"


class TestShortLots:
    def test_short_lots_transfer_with_their_opening_date(self, tmp_path):
        """Leaving them behind makes the eventual cover hit an empty short list
        and raise; the proceeds figure IS the tax attribute."""
        shorts = [_short_lot("2024-03-01", "10", "200.00", "s1")]
        event, source, target, ctx = _setup(tmp_path, ratio="2", short_lots=shorts)
        MergerStockProcessor().process(event, source, ctx)

        assert len(target.short_lots) == 1
        st = target.short_lots[0]
        assert st.opening_date == "2024-03-01"       # not the merger date
        assert st.quantity_shorted == Decimal("20.00000000")
        assert st.total_sale_proceeds_eur == Decimal("200.00")

    def test_opening_date_estimated_is_carried(self, tmp_path):
        shorts = [_short_lot("2024-12-31", "10", "200.00", "SOY_FALLBACK_SHORT_x")]
        assert shorts[0].opening_date_estimated is True

        event, source, target, ctx = _setup(tmp_path, ratio="1", short_lots=shorts)
        MergerStockProcessor().process(event, source, ctx)

        assert target.short_lots[0].opening_date_estimated is True


class TestLedgerState:
    def test_source_is_emptied_and_target_holds_everything(self, tmp_path):
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)
        MergerStockProcessor().process(event, source, ctx)

        assert source.lots == [] and source.short_lots == []
        assert len(target.lots) == 1

    def test_target_keeps_its_own_lots_and_stays_sorted(self, tmp_path):
        """The acquirer's directly-bought lots must survive and order correctly."""
        own = [_lot("2019-01-01", "5", "50.00", "own1"),
               _lot("2026-01-01", "5", "50.00", "own2")]
        carried = [_lot("2020-01-10", "10", "100.00", "t1", hps="2013-01-10")]
        event, source, target, ctx = _setup(tmp_path, ratio="1",
                                            source_lots=carried, target_lots=own)
        MergerStockProcessor().process(event, source, ctx)

        dates = [l.acquisition_date for l in target.lots]
        assert dates == sorted(dates)
        assert len(target.lots) == 3

    def test_a_deferral_returns_no_realized_gain(self, tmp_path):
        """A synthetic zero-gain RGL would consume the whole 100k limit and,
        because the two legs convert on different dates, invent an FX gain."""
        lots = [_lot("2020-01-10", "10", "100.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="1", source_lots=lots)

        assert MergerStockProcessor().process(event, source, ctx) == []


class TestRefusals:
    def _lots(self):
        return [_lot("2020-01-10", "10", "100.00", "t1")]

    def test_no_ledger_accessor(self, tmp_path):
        event, source, _, ctx = _setup(tmp_path, source_lots=self._lots())
        del ctx["ledger_for"]
        with pytest.raises(ValueError, match="wiring error"):
            MergerStockProcessor().process(event, source, ctx)

    def test_self_merger(self, tmp_path):
        event, source, _, ctx = _setup(tmp_path, source_lots=self._lots(),
                                       same_asset=True)
        with pytest.raises(ValueError, match="same asset"):
            MergerStockProcessor().process(event, source, ctx)

    def test_receiving_leg_positive_quantity(self, tmp_path):
        event, source, _, ctx = _setup(tmp_path, source_lots=self._lots(),
                                       quantity_exchanged=Decimal("10"))
        with pytest.raises(ValueError, match="receiving leg"):
            MergerStockProcessor().process(event, source, ctx)

    def test_the_same_merger_twice(self, tmp_path):
        event, source, target, ctx = _setup(tmp_path, ratio="1",
                                            source_lots=self._lots())
        MergerStockProcessor().process(event, source, ctx)
        source.lots.append(_lot("2020-01-10", "10", "100.00", "t1"))

        with pytest.raises(ValueError, match="already applied"):
            MergerStockProcessor().process(event, source, ctx)

    def test_empty_source_ledger(self, tmp_path):
        event, source, _, ctx = _setup(tmp_path, source_lots=[])
        with pytest.raises(ValueError, match="no lots to carry"):
            MergerStockProcessor().process(event, source, ctx)

    @pytest.mark.parametrize("ratio", [None, "0", "-1"])
    def test_unusable_ratio(self, tmp_path, ratio):
        event, source, _, ctx = _setup(tmp_path, ratio=ratio,
                                       source_lots=self._lots())
        with pytest.raises(ValueError, match="unusable exchange ratio"):
            MergerStockProcessor().process(event, source, ctx)

    def test_unparseable_merger_date(self, tmp_path):
        event, source, _, ctx = _setup(tmp_path, source_lots=self._lots(),
                                       merger_date="not-a-date")
        with pytest.raises(ValueError, match="unparseable date"):
            MergerStockProcessor().process(event, source, ctx)

    def test_fractional_credited_share_refuses_and_names_cash_in_lieu(self, tmp_path):
        """Rounding the fraction away destroys real basis; inventing a
        zero-proceeds disposal invents a loss."""
        lots = [_lot("2020-01-10", "100", "1000.00", "t1")]
        event, source, target, ctx = _setup(tmp_path, ratio="0.6595", source_lots=lots)

        with pytest.raises(ValueError, match="cash in lieu"):
            MergerStockProcessor().process(event, source, ctx)

    def test_a_refused_transfer_leaves_both_ledgers_untouched(self, tmp_path):
        """Rollback fidelity: the source must come out byte-identical, not
        re-sorted from the sort key."""
        lots = [
            _lot("2020-01-10", "100", "1000.00", "10000000001"),
            _lot("2020-01-10", "100", "1000.00", "9999999999"),
        ]
        event, source, target, ctx = _setup(tmp_path, ratio="0.6595",
                                            source_lots=lots)
        before_ids = [l.source_transaction_id for l in source.lots]
        before_qty = [l.quantity for l in source.lots]

        with pytest.raises(ValueError):
            MergerStockProcessor().process(event, source, ctx)

        assert [l.source_transaction_id for l in source.lots] == before_ids
        assert [l.quantity for l in source.lots] == before_qty
        assert target.lots == []


class TestSortKeyIsANoOpForUncarriedLots:
    def test_same_date_lots_keep_their_order(self):
        from src.engine.fifo_manager import _long_lot_sort_key

        lots = [
            _lot("2020-01-10", "10", "100.00", "10000000001"),
            _lot("2020-01-10", "10", "100.00", "9999999999"),
        ]
        # holding_period_start == acquisition_date for both, so the second key
        # component cannot change the outcome; the numeric id break decides.
        ordered = sorted(lots, key=_long_lot_sort_key)
        assert [l.source_transaction_id for l in ordered] == ["9999999999", "10000000001"]
