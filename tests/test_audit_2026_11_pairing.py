# tests/test_audit_2026_11_pairing.py
"""
Regression tests for the lot-pairing findings of the 2026-11 audit:

* V04 — the start-of-year lots were rebuilt by replaying EVERY earlier year
  with the method of the current run, not with the method that year was
  filed with; an ``optimal`` year was replayed as FIFO.
* W05 — after ``optimal`` the ledger (portfolio, simulator) kept FIFO's lots.
* W06 — the optimal solver minimised the gain in EUR while the CZK base is
  what is filed.
* W07 — the solver compared dates only, so a lot bought later the same day
  could fill an earlier sale.
* W08 — the weighted average was taken in EUR and converted at the rate of
  the lot FIFO picked.

Everything is synthetic: an invented symbol, account U1234567, round EUR
amounts. Expected figures are worked out by hand in each test.
"""
import json
import uuid
from datetime import date
from decimal import Decimal

import pytest

import src.config as global_config
from src.domain.enums import AssetCategory
from src.engine.fifo_manager import FifoLedger
from src.engine.pairing import PairingHistory, PairingMethod
from src.pipeline_runner import run_core_processing_pipeline
from src.utils.currency_converter import CurrencyConverter
from tests.support.csv_creators import (
    create_cash_transactions_csv_string, create_corporate_actions_csv_string,
    create_positions_csv_string, create_trades_csv_string,
)
from tests.support.mock_providers import MockECBExchangeRateProvider

ACCOUNT = "U1234567"

PAIRA = dict(asset_class="STK", sub_category="COMMON", symbol="PAIRA",
             description="PAIRING ALPHA INC", isin="US0000000901",
             conid="900000901")


def _trade(security, trade_date, quantity, price, tx_id, *, buy_sell=None,
           open_close=None, commission="0"):
    """One Trades row (order of ``TRADES_FILE_HEADERS``), in EUR."""
    qty = Decimal(quantity)
    buy_sell = buy_sell or ("BUY" if qty > 0 else "SELL")
    open_close = open_close or ("O" if qty > 0 else "C")
    return [
        ACCOUNT, "EUR", security["asset_class"], security["sub_category"],
        security["symbol"], security["description"], security["isin"],
        None, None, None, trade_date, quantity, price, commission, "EUR",
        buy_sell, tx_id, "", "", security["conid"], "", "1", open_close,
    ]


def _position(security, quantity, cost_basis="0", mark_price="1"):
    """One Positions row (order of ``POSITIONS_FILE_HEADERS``), in EUR."""
    return [
        ACCOUNT, "EUR", security["asset_class"], security["sub_category"],
        security["symbol"], security["description"], security["isin"], quantity,
        str(Decimal(quantity) * Decimal(mark_price)), mark_price, cost_basis,
        None, security["conid"], None, "1",
    ]


def _one_to_one():
    return MockECBExchangeRateProvider(foreign_to_eur_init_value=Decimal("1.0"))


@pytest.fixture
def run(mock_config_paths, monkeypatch):
    """Run the core pipeline on synthetic rows; returns the ProcessingOutput."""
    monkeypatch.setattr(global_config, "IS_INTERACTIVE_CLASSIFICATION", False)
    paths = mock_config_paths

    def _run(trades, *, tax_year, soy=(), eoy=(), method=PairingMethod.FIFO,
             history=None, country="cz", **kwargs):
        monkeypatch.setattr(global_config, "TAX_YEAR", tax_year)
        files = {
            paths["trades"]: create_trades_csv_string(list(trades)),
            paths["pos_start"]: create_positions_csv_string(list(soy)),
            paths["pos_end"]: create_positions_csv_string(list(eoy)),
            paths["cash"]: create_cash_transactions_csv_string([]),
            paths["corp_actions"]: create_corporate_actions_csv_string([]),
        }
        for path, text in files.items():
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(text)
        return run_core_processing_pipeline(
            trades_file_path=paths["trades"],
            cash_transactions_file_path=paths["cash"],
            positions_start_file_path=paths["pos_start"],
            positions_end_file_path=paths["pos_end"],
            corporate_actions_file_path=paths["corp_actions"],
            interactive_classification_mode=False,
            tax_year_to_process=tax_year,
            custom_rate_provider=kwargs.pop("rates", None) or _one_to_one(),
            country_code=country,
            pairing_method=method,
            pairing_history=history,
            **kwargs,
        )
    return _run


def _rgl_view(rgls):
    """What a disposal row says, without the per-run ids."""
    return sorted(
        (r.acquisition_date, r.realization_date, r.quantity_realized,
         r.total_cost_basis_eur, r.total_realization_value_eur,
         r.gross_gain_loss_eur, r.holding_period_start,
         r.is_acquisition_estimated, r.holding_period_start_estimated)
        for r in rgls
    )


def _lots_view(output):
    rows = []
    for ledger in output.fifo_ledgers_by_asset_id.values():
        for lot in ledger.lots:
            rows.append((lot.acquisition_date, lot.quantity,
                         lot.total_cost_basis_eur, lot.source_transaction_id,
                         lot.acquisition_date_estimated))
        for lot in ledger.short_lots:
            rows.append((lot.opening_date, lot.quantity_shorted,
                         lot.total_sale_proceeds_eur, lot.source_transaction_id,
                         lot.opening_date_estimated))
    return sorted(rows)


# ---------------------------------------------------------------------------
# V04 — earlier years are replayed with the method they were filed with
# ---------------------------------------------------------------------------

# Year 1 (2024): buy 10 @ 100 in January, 10 @ 130 in March, sell 10 @ 150 in
# June. Year 2 (2025): sell the remaining 10 @ 160.
YEAR1 = [
    _trade(PAIRA, "2024-01-10", "10", "100", "101"),
    _trade(PAIRA, "2024-03-11", "10", "130", "102"),
    _trade(PAIRA, "2024-06-10", "-10", "150", "103"),
]
YEAR2 = [_trade(PAIRA, "2025-06-10", "-10", "160", "104")]


def _year2(run, *, method=PairingMethod.FIFO, history=None, soy_qty="10",
           sale_qty=None, soy_cost="1000"):
    trades = YEAR1 + ([_trade(PAIRA, "2025-06-10", f"-{sale_qty}", "160", "104")]
                      if sale_qty else YEAR2)
    return run(trades, tax_year=2025, soy=[_position(PAIRA, soy_qty, soy_cost)],
               method=method, history=history)


class TestV04PairingHistoryReplay:

    def test_lifo_filed_year_then_fifo_year(self, run):
        """Year 1 filed LIFO consumed the March lot (cost 1 300, gain 200), so
        the January lot is what is left: year 2 gains 1 600 - 1 000 = 600 and
        the lot's date is January. Before the fix the FIFO run replayed year 1
        as FIFO and sold the March lot a second time (gain 300)."""
        year1 = run(YEAR1, tax_year=2024, method=PairingMethod.LIFO)
        (r1,) = year1.realized_gains_losses
        assert r1.gross_gain_loss_eur == Decimal("200")
        assert r1.total_cost_basis_eur == Decimal("1300")

        year2 = _year2(run, history=PairingHistory(
            methods={2024: PairingMethod.LIFO}))
        (r2,) = year2.realized_gains_losses
        assert r2.gross_gain_loss_eur == Decimal("600")
        assert r2.acquisition_date == "2024-01-10"
        # Every purchase is claimed exactly once over the two years.
        assert r1.total_cost_basis_eur + r2.total_cost_basis_eur == Decimal("2300")
        assert year2.soy_unverified_symbols == []

    def test_no_history_means_earlier_years_are_fifo(self, run):
        """A LIFO run without a history replays the earlier year as FIFO (it
        was filed FIFO), not as LIFO: the March lot is what is left."""
        out = _year2(run, method=PairingMethod.LIFO)
        (r,) = out.realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("300")
        assert r.acquisition_date == "2024-03-11"

    def test_fifo_filed_year_then_lifo_year(self, run):
        out = _year2(run, method=PairingMethod.LIFO, history=PairingHistory(
            methods={2024: PairingMethod.FIFO}))
        (r,) = out.realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("300")

    def test_weighted_average_filed_year_then_fifo_year(self, run):
        """Average 115: year 1 gains 1 500 - 1 150 = 350; the March lot is left,
        re-priced to 10 x 115; year 2 gains 1 600 - 1 150 = 450."""
        year1 = run(YEAR1, tax_year=2024, method=PairingMethod.WEIGHTED_AVERAGE)
        (r1,) = year1.realized_gains_losses
        assert r1.gross_gain_loss_eur == Decimal("350")

        year2 = _year2(run, history=PairingHistory(
            methods={2024: PairingMethod.WEIGHTED_AVERAGE}))
        (r2,) = year2.realized_gains_losses
        assert r2.gross_gain_loss_eur == Decimal("450")
        assert r2.acquisition_date == "2024-03-11"
        assert r1.total_cost_basis_eur + r2.total_cost_basis_eur == Decimal("2300")

    def test_fifo_invariance(self, run):
        """No history, an all-FIFO history and an empty history give the same
        disposals and the same open lots."""
        trades = YEAR1 + [
            _trade(PAIRA, "2025-02-03", "5", "140", "105"),
            _trade(PAIRA, "2025-06-10", "-12", "160", "104"),
        ]
        soy = [_position(PAIRA, "10", "1300")]
        eoy = [_position(PAIRA, "3", "420")]
        outs = [
            run(trades, tax_year=2025, soy=soy, eoy=eoy, history=h)
            for h in (None, PairingHistory(),
                      PairingHistory(methods={2024: PairingMethod.FIFO}))
        ]
        views = {tuple(_rgl_view(o.realized_gains_losses)) for o in outs}
        lots = {tuple(_lots_view(o)) for o in outs}
        assert len(views) == 1 and len(lots) == 1
        # And it is the FIFO answer: 10 March @ 130 + 2 February @ 140.
        assert sum(r.gross_gain_loss_eur for r in outs[0].realized_gains_losses) \
            == Decimal("1920") - Decimal("1300") - Decimal("280")

    def test_optimal_filed_year_replays_its_plan(self, run):
        """Year 1 filed ``optimal`` sold the March lot (tx 102); the plan says
        so, and replaying it leaves the January lot."""
        history = PairingHistory(
            methods={2024: PairingMethod.OPTIMAL},
            plans={2024: {"103": (("102", Decimal("10")),)}},
        )
        (r,) = _year2(run, history=history).realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("600")
        assert r.acquisition_date == "2024-01-10"

    def test_optimal_year_without_plan_entry_is_fifo(self, run):
        history = PairingHistory(methods={2024: PairingMethod.OPTIMAL}, plans={})
        (r,) = _year2(run, history=history).realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("300")

    def test_unusable_plan_falls_back_and_is_flagged(self, run):
        """A plan naming a lot that does not exist cannot be replayed: the
        opening lots come from the fallback, dated estimated, and the symbol
        is listed for review."""
        history = PairingHistory(
            methods={2024: PairingMethod.OPTIMAL},
            plans={2024: {"103": (("999", Decimal("10")),)}},
        )
        out = _year2(run, history=history)
        (r,) = out.realized_gains_losses
        assert r.is_acquisition_estimated
        assert r.holding_period_start_estimated
        assert out.soy_unverified_symbols == ["PAIRA"]

    def test_non_fifo_history_with_fallback_lots_is_flagged(self, run):
        """The broker reports fewer shares than the replay leaves (5 vs 10):
        the surviving lots are trimmed by the broker's own FIFO rule, which
        need not match a LIFO filing, so they are marked for review."""
        out = _year2(run, history=PairingHistory(
            methods={2024: PairingMethod.LIFO}), soy_qty="5", sale_qty="5",
            soy_cost="500")
        (r,) = out.realized_gains_losses
        assert r.is_acquisition_estimated and r.holding_period_start_estimated
        assert out.soy_unverified_symbols == ["PAIRA"]

    def test_fifo_history_with_fallback_lots_is_not_flagged(self, run):
        out = _year2(run, soy_qty="5", sale_qty="5", soy_cost="500")
        (r,) = out.realized_gains_losses
        assert not r.is_acquisition_estimated
        assert out.soy_unverified_symbols == []


def test_option_ledger_ignores_the_history():
    provider = _one_to_one()
    ledger = FifoLedger(
        asset_internal_id=uuid.uuid4(), asset_category=AssetCategory.OPTION,
        asset_multiplier_from_asset=Decimal("100"),
        currency_converter=CurrencyConverter(rate_provider=provider),
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
        pairing_history=PairingHistory(methods={2024: PairingMethod.LIFO}),
    )
    assert ledger.pairing_history is None


class TestPairingHistory:

    def test_defaults_and_lookups(self):
        h = PairingHistory(
            methods={2024: PairingMethod.LIFO},
            plans={2025: {"7": (("1", Decimal("2")),)}},
        )
        assert h.method_for(2024) == PairingMethod.LIFO
        assert h.method_for(2023) == PairingMethod.FIFO
        assert h.plan_for(2025, "7") == (("1", Decimal("2")),)
        assert h.plan_for(2025, "8") is None
        assert h.plan_for(2025, None) is None

    def test_fingerprint_is_stable_and_sensitive(self):
        a = PairingHistory(methods={2025: PairingMethod.FIFO,
                                    2024: PairingMethod.LIFO})
        b = PairingHistory(methods={2024: "lifo", 2025: "fifo"})
        assert a.fingerprint() == b.fingerprint()
        c = PairingHistory(methods={2024: PairingMethod.LIFO,
                                    2025: PairingMethod.FIFO},
                           plans={2025: {"7": (("1", Decimal("2.0")),)}})
        d = PairingHistory(methods={2024: PairingMethod.LIFO,
                                    2025: PairingMethod.FIFO},
                           plans={2025: {"7": (("1", Decimal("2")),)}})
        assert c.fingerprint() != a.fingerprint()
        assert c.fingerprint() == d.fingerprint()


# ---------------------------------------------------------------------------
# V04 — the web service: filed years, notes, fingerprint, routes, MCP
# ---------------------------------------------------------------------------

from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider  # noqa: E402


class PairingCnb(GoldenCnbProvider):
    """Synthetic CZK per EUR for the dates of the two-year scenario."""

    CZK_PER_UNIT = {
        ("EUR", date(2024, 1, 10)): Decimal("25"),
        ("EUR", date(2024, 3, 11)): Decimal("24"),
        ("EUR", date(2024, 6, 10)): Decimal("25"),
        ("EUR", date(2025, 2, 3)): Decimal("25"),
        ("EUR", date(2025, 6, 10)): Decimal("25"),
    }


def _write_dataset(svc, year, *, trades, eoy=(), soy=None):
    year_dir = svc.data_dir / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    (year_dir / "trades.csv").write_text(create_trades_csv_string(list(trades)))
    (year_dir / "cash_transactions.csv").write_text(
        create_cash_transactions_csv_string([]))
    (year_dir / "positions_end.csv").write_text(
        create_positions_csv_string(list(eoy)))
    if soy is not None:
        (year_dir / "positions_start.csv").write_text(
            create_positions_csv_string(list(soy)))


@pytest.fixture
def two_years(tmp_path):
    """A service with the two-year scenario: 2024 and 2025 are closed."""
    from src.webapp.services import RunService
    svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    svc._today = lambda: date(2026, 3, 1)
    _write_dataset(svc, 2024, trades=YEAR1, eoy=[_position(PAIRA, "10", "1000")])
    _write_dataset(svc, 2025, trades=YEAR2)
    yield svc
    svc.runner.shutdown(wait=False)


def _execute(svc, run_id, year, method="fifo", fx="daily"):
    return svc._execute_run(run_id, year, fx, ecb_provider=GoldenEcbProvider(),
                            cz_fx_provider=PairingCnb(), pairing_method=method)


def _disposals(svc, run_id, mode="daily"):
    result = svc.load_result(run_id, mode)
    return [it for it in result["items"]
            if it.get("item_type") == "SECURITY_DISPOSAL"]


class TestV04Service:

    def test_marked_lifo_year_drives_the_next_year(self, two_years):
        svc = two_years
        _execute(svc, "2024-lifo", 2024, "lifo")
        entry = svc.mark_run_filed("2024-lifo")
        assert entry["pairing_method"] == "lifo" and entry["fx_mode"] == "daily"
        assert svc.load_filed_years()["years"]["2024"]["run_id"] == "2024-lifo"

        meta = _execute(svc, "2025-fifo", 2025)
        (item,) = _disposals(svc, "2025-fifo")
        assert Decimal(item["cost_basis_eur"]) == Decimal("1000")
        assert item["acquisition_date"] == "2024-01-10"
        assert meta["prior_year_pairing"] == {"2024": "lifo"}
        assert meta["prior_year_pairing_assumed"] == []
        assert any("2024 – LIFO" in n for n in meta["notes"])
        assert not any("nejsou označené" in n for n in meta["notes"])

    def test_unmarked_fifo_world_has_no_warning(self, two_years):
        meta = _execute(two_years, "2025-fifo", 2025)
        assert meta["prior_year_pairing"] == {"2024": "fifo"}
        assert meta["prior_year_pairing_assumed"] == [2024]
        assert not any("nejsou označené" in n for n in meta["notes"])
        (item,) = _disposals(two_years, "2025-fifo")
        assert Decimal(item["cost_basis_eur"]) == Decimal("1300")

    def test_non_fifo_run_warns_about_unmarked_years(self, two_years):
        meta = _execute(two_years, "2025-lifo", 2025, "lifo")
        assert any("Roky 2024 nejsou označené" in n for n in meta["notes"])

    def test_unmarked_year_with_a_non_fifo_run_warns(self, two_years):
        _execute(two_years, "2024-lifo", 2024, "lifo")
        meta = _execute(two_years, "2025-fifo", 2025)
        assert any("Roky 2024 nejsou označené" in n for n in meta["notes"])

    def test_marking_fifo_keeps_a_run_without_warning_current(self, two_years):
        svc = two_years
        _execute(svc, "2024-fifo", 2024)
        _execute(svc, "2025-fifo", 2025)
        assert svc.find_cached_run(2025, "daily", "fifo") == "2025-fifo"
        svc.mark_run_filed("2024-fifo")
        assert svc.find_cached_run(2025, "daily", "fifo") == "2025-fifo"
        assert not svc.get_run("2025-fifo").get("pairing_history_changed")

    def test_marking_another_method_makes_later_runs_stale(self, two_years):
        svc = two_years
        _execute(svc, "2025-fifo", 2025)
        _execute(svc, "2024-lifo", 2024, "lifo")
        svc.mark_run_filed("2024-lifo")
        assert svc.find_cached_run(2025, "daily", "fifo") is None
        meta = svc.get_run("2025-fifo")
        assert meta["pairing_history_changed"] is True
        assert any("přepočítejte" in n for n in meta["notes"])
        # Unmarking restores what the run was computed with.
        assert svc.unmark_filed_year(2024) is True
        assert not svc.get_run("2025-fifo").get("pairing_history_changed")
        assert svc.unmark_filed_year(2024) is False

    def test_read_time_flags(self, two_years):
        svc = two_years
        _execute(svc, "2024-fifo", 2024)
        meta = svc.get_run("2024-fifo")
        assert meta["can_mark_filed"] is True and meta["filed"] is False
        svc.mark_run_filed("2024-fifo")
        meta = svc.get_run("2024-fifo")
        assert meta["filed"] is True and meta["filed_mode"] == "daily"
        assert svc.get_year(2024).filed["run_id"] == "2024-fifo"
        assert svc.get_year(2025).filed is None

    def test_mark_refuses_open_year_and_unknown_run(self, two_years):
        svc = two_years
        _execute(svc, "2024-fifo", 2024)
        with pytest.raises(ValueError, match="neexistuje"):
            svc.mark_run_filed("nope")
        svc._today = lambda: date(2024, 12, 1)
        with pytest.raises(ValueError, match="neskončil"):
            svc.mark_run_filed("2024-fifo")
        assert svc.get_run("2024-fifo")["can_mark_filed"] is False

    def test_optimal_run_without_stored_plan_cannot_be_marked(self, two_years):
        svc = two_years
        _execute(svc, "2024-opt", 2024, "optimal")
        plan = svc.runs_dir / "2024-opt" / "pairing_plan.daily.json"
        if plan.exists():
            plan.unlink()
        with pytest.raises(ValueError, match="přepočítejte"):
            svc.mark_run_filed("2024-opt")

    def test_optimal_with_compare_is_refused(self, two_years):
        svc = two_years
        with pytest.raises(ValueError, match="denní kurz"):
            svc.start_run(2025, "compare", "optimal")
        with pytest.raises(ValueError, match="denní kurz"):
            svc.run_pipeline_sync(2025, "compare", "optimal")
        with pytest.raises(ValueError, match="denní kurz"):
            _execute(svc, "2025-x", 2025, "optimal", fx="compare")


def test_unverified_opening_lots_are_named_in_the_notes(tmp_path):
    """2024 filed LIFO, but the broker reports 5 shares where the replay
    leaves 10: the trimmed lots cannot be trusted, and the run says so."""
    from src.webapp.services import RunService
    svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    svc._today = lambda: date(2026, 3, 1)
    try:
        _write_dataset(svc, 2024, trades=YEAR1, eoy=[_position(PAIRA, "5", "500")])
        _write_dataset(svc, 2025, trades=[
            _trade(PAIRA, "2025-06-10", "-5", "160", "104")])
        _execute(svc, "2024-lifo", 2024, "lifo")
        svc.mark_run_filed("2024-lifo")
        meta = _execute(svc, "2025-fifo", 2025)
        assert meta["soy_unverified_symbols"] == ["PAIRA"]
        assert any("U titulů PAIRA nelze ověřit" in n for n in meta["notes"])
    finally:
        svc.runner.shutdown(wait=False)


class TestV04Routes:

    @pytest.fixture
    def client(self, two_years):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from fastapi.testclient import TestClient
        from src.webapp.app import create_app
        _execute(two_years, "2024-lifo", 2024, "lifo")
        with TestClient(create_app(services=two_years)) as tc:
            yield tc, two_years

    def test_mark_and_unmark_through_the_routes(self, client):
        tc, svc = client
        page = tc.get("/results/2024-lifo").text
        assert 'action="/results/2024-lifo/filed"' in page

        r = tc.post("/results/2024-lifo/filed", data={"mode": "daily"},
                    follow_redirects=False)
        assert r.status_code == 303
        assert svc.load_filed_years()["years"]["2024"]["pairing_method"] == "lifo"

        page = tc.get("/results/2024-lifo").text
        assert ">Podáno</span>" in page
        assert 'action="/results/2024-lifo/filed"' not in page
        runs = tc.get("/runs").text
        assert "<th>Podáno</th>" in runs
        assert "Podáno · lifo" in runs

        r = tc.post("/runs/filed/2024/delete", follow_redirects=False)
        assert r.status_code == 303
        assert svc.load_filed_years()["years"] == {}

    def test_mark_error_is_shown(self, client):
        tc, svc = client
        svc._today = lambda: date(2024, 12, 1)
        r = tc.post("/results/2024-lifo/filed", data={"mode": "daily"})
        assert "neskončil" in r.text


def test_mcp_list_datasets_reports_filed(two_years):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _call, _payload
    from src.mcp_server.server import create_server
    _execute(two_years, "2024-fifo", 2024)
    two_years.mark_run_filed("2024-fifo")
    data = _payload(_call(create_server(two_years), "list_datasets"))
    years = {d["tax_year"]: d for d in data["datasets"]}
    assert years[2024]["filed"]["run_id"] == "2024-fifo"
    assert years[2025]["filed"] is None


# ---------------------------------------------------------------------------
# W08 — the weighted average is kept in EUR AND per payment date
# ---------------------------------------------------------------------------

from src.domain.enums import FinancialEventType  # noqa: E402
from src.domain.events import TradeEvent  # noqa: E402


def _ledger(method, category=AssetCategory.STOCK):
    provider = _one_to_one()
    return FifoLedger(
        asset_internal_id=uuid.uuid4(), asset_category=category,
        asset_multiplier_from_asset=None,
        currency_converter=CurrencyConverter(rate_provider=provider),
        exchange_rate_provider=provider,
        internal_working_precision=global_config.INTERNAL_CALCULATION_PRECISION,
        decimal_rounding_mode=global_config.DECIMAL_ROUNDING_MODE,
        pairing_method=method,
    )


def _ev(ledger, kind, when, qty, eur, tx):
    ev = TradeEvent(asset_internal_id=ledger.asset_internal_id, event_date=when,
                    event_type=kind, quantity=Decimal(qty),
                    price_foreign_currency=Decimal("1"), ibkr_transaction_id=tx)
    ev.net_proceeds_or_cost_basis_eur = Decimal(eur)
    return ev


def _buy(ledger, when, qty, eur, tx):
    ledger.add_long_lot(_ev(ledger, FinancialEventType.TRADE_BUY_LONG, when, qty, eur, tx))


def _sell(ledger, when, qty, eur, tx):
    return ledger.consume_long_lots_for_sale(
        _ev(ledger, FinancialEventType.TRADE_SELL_LONG, when, f"-{qty}", eur, tx))


def _sum(components):
    return sum((amount for _d, amount in components), Decimal(0))


class TestW08CostComponents:

    def test_weighted_average_sale_carries_dated_components(self):
        ledger = _ledger(PairingMethod.WEIGHTED_AVERAGE)
        _buy(ledger, "2024-01-10", "10", "1000", "101")
        _buy(ledger, "2024-03-11", "10", "1300", "102")
        (rgl,) = _sell(ledger, "2024-06-10", "10", "1500", "103")
        assert rgl.total_cost_basis_eur == Decimal("1150")
        assert rgl.cost_basis_components_eur == [
            ("2024-01-10", Decimal("500")), ("2024-03-11", Decimal("650"))]
        (lot,) = ledger.lots
        assert lot.total_cost_basis_eur == Decimal("1150")
        assert lot.cost_components == [
            ("2024-01-10", Decimal("500")), ("2024-03-11", Decimal("650"))]
        # A second average sale splits the pool's components again.
        (rgl2,) = _sell(ledger, "2024-07-10", "5", "800", "104")
        assert rgl2.cost_basis_components_eur == [
            ("2024-01-10", Decimal("250")), ("2024-03-11", Decimal("325"))]
        assert _sum(ledger.lots[0].cost_components) == ledger.lots[0].total_cost_basis_eur

    def test_components_always_add_up_exactly(self):
        ledger = _ledger(PairingMethod.WEIGHTED_AVERAGE)
        _buy(ledger, "2024-01-10", "3", "100", "101")
        _buy(ledger, "2024-02-10", "7", "233.33", "102")
        _buy(ledger, "2024-03-11", "11", "419.17", "103")
        rgls = _sell(ledger, "2024-06-10", "13", "900", "104")
        assert len(rgls) == 3  # FIFO lot identity: 3 + 7 + 3 of 11
        for rgl in rgls:
            assert _sum(rgl.cost_basis_components_eur) == rgl.total_cost_basis_eur
        for lot in ledger.lots:
            assert _sum(lot.cost_components) == lot.total_cost_basis_eur

    @pytest.mark.parametrize("method", [PairingMethod.FIFO, PairingMethod.LIFO])
    def test_fifo_and_lifo_rgls_carry_no_components(self, method):
        ledger = _ledger(method)
        _buy(ledger, "2024-01-10", "10", "1000", "101")
        _buy(ledger, "2024-03-11", "10", "1300", "102")
        rgls = _sell(ledger, "2024-06-10", "15", "2250", "103")
        assert all(r.cost_basis_components_eur is None for r in rgls)
        assert all(l.cost_components is None for l in ledger.lots)

    def test_fifo_sale_of_an_averaged_lot_splits_its_components(self):
        ledger = _ledger(PairingMethod.FIFO)
        _buy(ledger, "2024-01-10", "10", "1150", "101")
        ledger.lots[0].cost_components = [
            ("2024-01-10", Decimal("500")), ("2024-03-11", Decimal("650"))]
        (rgl,) = _sell(ledger, "2025-06-10", "4", "640", "104")
        assert rgl.cost_basis_components_eur == [
            ("2024-01-10", Decimal("200")), ("2024-03-11", Decimal("260"))]
        assert ledger.lots[0].cost_components == [
            ("2024-01-10", Decimal("300")), ("2024-03-11", Decimal("390"))]

    def test_capital_repayment_scales_the_components(self):
        ledger = _ledger(PairingMethod.FIFO)
        _buy(ledger, "2024-01-10", "10", "1150", "101")
        ledger.lots[0].cost_components = [
            ("2024-01-10", Decimal("500")), ("2024-03-11", Decimal("650"))]
        ledger.reduce_cost_basis_for_capital_repayment(Decimal("115"))
        lot = ledger.lots[0]
        assert lot.total_cost_basis_eur == Decimal("1035")
        assert lot.cost_components == [
            ("2024-01-10", Decimal("450")), ("2024-03-11", Decimal("585"))]

    def test_short_side_under_weighted_average_is_fifo(self):
        ledger = _ledger(PairingMethod.WEIGHTED_AVERAGE)
        ledger.add_short_lot(_ev(ledger, FinancialEventType.TRADE_SELL_SHORT_OPEN,
                                 "2024-01-10", "-1", "100", "201"))
        ledger.add_short_lot(_ev(ledger, FinancialEventType.TRADE_SELL_SHORT_OPEN,
                                 "2024-02-10", "-1", "120", "202"))
        (rgl,) = ledger.consume_short_lots_for_cover(
            _ev(ledger, FinancialEventType.TRADE_BUY_SHORT_COVER,
                "2024-03-11", "1", "90", "203"))
        assert rgl.total_realization_value_eur == Decimal("100")
        assert rgl.gross_gain_loss_eur == Decimal("10")
        (left,) = ledger.short_lots
        assert left.total_sale_proceeds_eur == Decimal("120")


class TestW08Czk:

    def test_weighted_average_cost_is_the_czk_average(self, two_years):
        """Rates 25 (January) and 24 (March). Year 1 sells half the pool at
        the average 115: 500 EUR of January money and 650 EUR of March money,
        28 100 CZK (not 1 150 x 25 = 28 750). Year 2 sells the other half,
        likewise 28 100; together 56 200 = 1 000 x 25 + 1 300 x 24."""
        svc = two_years
        _execute(svc, "2024-wa", 2024, "weighted_average")
        (y1,) = _disposals(svc, "2024-wa")
        assert Decimal(y1["cost_basis_czk"]) == Decimal("28100")
        svc.mark_run_filed("2024-wa")
        _execute(svc, "2025-fifo", 2025)
        (y2,) = _disposals(svc, "2025-fifo")
        assert Decimal(y2["cost_basis_eur"]) == Decimal("1150")
        assert Decimal(y2["cost_basis_czk"]) == Decimal("28100")
        assert Decimal(y1["cost_basis_czk"]) + Decimal(y2["cost_basis_czk"]) \
            == Decimal("56200")

    def test_fifo_czk_cost_is_unchanged(self, two_years):
        _execute(two_years, "2024-fifo", 2024)
        (item,) = _disposals(two_years, "2024-fifo")
        assert Decimal(item["cost_basis_czk"]) == Decimal("25000")


# ---------------------------------------------------------------------------
# W07 / W06 / W05 — optimal pairing: order within a day, CZK, the ledger
# ---------------------------------------------------------------------------

from src.countries.cz.config import CzTaxConfig  # noqa: E402
from src.countries.cz.fx_policy import CzCurrencyConverter  # noqa: E402
from src.engine.pairing_solver import (  # noqa: E402
    SaleDemand, SupplyLot, solve_optimal_matching,
)


class ObjectiveCnb(GoldenCnbProvider):
    """Lot A at 27, lot B at 24, the sale at 25 CZK per EUR."""

    CZK_PER_UNIT = {
        ("EUR", date(2025, 1, 6)): Decimal("27"),
        ("EUR", date(2025, 2, 3)): Decimal("24"),
        ("EUR", date(2025, 3, 3)): Decimal("25"),
    }


def _czk(provider):
    return CzCurrencyConverter(provider, CzTaxConfig().fx_policy)


# Lot A: 1 @ 100 EUR (January), lot B: 1 @ 104 EUR (February), sale 1 @ 110.
OBJECTIVE_TRADES = [
    _trade(PAIRA, "2025-01-06", "1", "100", "601"),
    _trade(PAIRA, "2025-02-03", "1", "104", "602"),
    _trade(PAIRA, "2025-03-03", "-1", "110", "603"),
]


class TestW07SameDay:

    def test_solver_respects_the_order_key(self):
        lots = [SupplyLot(date(2025, 3, 3), Decimal("1"), Decimal("100"), "501",
                          order_key=1),
                SupplyLot(date(2025, 3, 3), Decimal("1"), Decimal("200"), "506",
                          order_key=3)]
        sale = [SaleDemand(date(2025, 3, 3), Decimal("1"), Decimal("110"), "S",
                           order_key=2)]
        (a,) = solve_optimal_matching(lots, sale, lambda l, s: False)
        assert a.supply_index == 0
        # Without the keys the same-day later lot is taken (the old rule).
        for x in (*lots, *sale):
            x.order_key = None
        (a,) = solve_optimal_matching(lots, sale, lambda l, s: False)
        assert a.supply_index == 1

    def test_a_lot_bought_later_the_same_day_is_not_paired(self, run):
        trades = [
            _trade(PAIRA, "2025-03-03", "10", "100", "501"),
            _trade(PAIRA, "2025-03-03", "-10", "110", "505"),
            _trade(PAIRA, "2025-03-03", "10", "200", "506"),
        ]
        out = run(trades, tax_year=2025, eoy=[_position(PAIRA, "10", "2000")],
                  method=PairingMethod.OPTIMAL)
        (r,) = out.realized_gains_losses
        assert r.gross_gain_loss_eur == Decimal("100")
        (lot,) = next(iter(
            l for l in out.fifo_ledgers_by_asset_id.values() if l.lots)).lots
        assert lot.source_transaction_id == "506"


class TestW06CzkObjective:

    def test_eur_objective_without_converter(self, run):
        out = run(OBJECTIVE_TRADES, tax_year=2025, eoy=[_position(PAIRA, "1", "100")],
                  method=PairingMethod.OPTIMAL)
        (r,) = out.realized_gains_losses
        assert r.acquisition_date == "2025-02-03"   # B: EUR gain 6 < 10
        assert out.pairing_plan == {"603": [("602", Decimal("1"))]}

    def test_czk_objective_with_converter(self, run):
        """In CZK, A gains 110 x 25 - 100 x 27 = 50 and B gains
        2 750 - 104 x 24 = 254: A is the optimum (and here also FIFO)."""
        out = run(OBJECTIVE_TRADES, tax_year=2025, eoy=[_position(PAIRA, "1", "104")],
                  method=PairingMethod.OPTIMAL,
                  pairing_objective_converter=_czk(ObjectiveCnb()))
        (r,) = out.realized_gains_losses
        assert r.acquisition_date == "2025-01-06"
        assert out.pairing_plan == {"603": [("601", Decimal("1"))]}

    def test_unpriceable_arc_keeps_fifo(self, run):
        class Missing(GoldenCnbProvider):
            CZK_PER_UNIT = {("EUR", date(2025, 3, 3)): Decimal("25")}
        trades = [
            _trade(PAIRA, "2024-01-10", "1", "100", "601"),
            _trade(PAIRA, "2024-06-03", "1", "130", "602"),
            _trade(PAIRA, "2025-03-03", "-1", "110", "603"),
        ]
        out = run(trades, tax_year=2025, soy=[_position(PAIRA, "2", "230")],
                  eoy=[_position(PAIRA, "1", "130")], method=PairingMethod.OPTIMAL,
                  pairing_objective_converter=_czk(Missing()))
        (r,) = out.realized_gains_losses
        assert r.acquisition_date == "2024-01-10"   # FIFO, not the 130 lot
        assert out.pairing_plan == {}


class TestW05LedgerAfterOptimal:

    def test_open_lots_are_what_the_assignment_left(self, run):
        out = run(OBJECTIVE_TRADES, tax_year=2025, eoy=[_position(PAIRA, "1", "100")],
                  method=PairingMethod.OPTIMAL)
        (ledger,) = [l for l in out.fifo_ledgers_by_asset_id.values() if l.lots]
        (lot,) = ledger.lots
        # The solver sold B, so A is held -- FIFO would have kept B.
        assert lot.source_transaction_id == "601"
        assert lot.acquisition_date == "2025-01-06"
        assert lot.total_cost_basis_eur == Decimal("100")

    def test_partial_lot_after_optimal(self, run):
        trades = [
            _trade(PAIRA, "2025-01-06", "10", "100", "601"),
            _trade(PAIRA, "2025-02-03", "10", "130", "602"),
            _trade(PAIRA, "2025-03-03", "-4", "120", "603"),
        ]
        out = run(trades, tax_year=2025, eoy=[_position(PAIRA, "16", "1900")],
                  method=PairingMethod.OPTIMAL)
        (ledger,) = [l for l in out.fifo_ledgers_by_asset_id.values() if l.lots]
        held = sorted((l.source_transaction_id, l.quantity, l.total_cost_basis_eur)
                      for l in ledger.lots)
        assert held == [("601", Decimal("10"), Decimal("1000")),
                        ("602", Decimal("6"), Decimal("780"))]

    def test_portfolio_shows_the_optimal_lots(self, two_years):
        """2024 optimal in CZK: the March lot (24 CZK/EUR, 3 120 CZK a share)
        beats January (2 500): it is sold, January is held."""
        svc = two_years
        _execute(svc, "2024-opt", 2024, "optimal")
        pf = svc.load_portfolio("2024-opt")
        (pos,) = pf["positions"]
        assert [l["acquisition_date"] for l in pos["lots"]] == ["2024-01-10"]


def test_pairing_matrix_runs_optimal_once_per_fx_mode():
    from src.countries.cz.aggregation_service import run_cz_pairing_matrix
    calls = []

    def _runner(method, objective_converter=None):
        calls.append((method, objective_converter is not None))
        raise _Stop()

    class _Stop(Exception):
        pass

    with pytest.raises(_Stop):
        run_cz_pairing_matrix(_runner, 2025, fx_modes=("daily",),
                              pairing_methods=(PairingMethod.OPTIMAL,),
                              fx_providers={"daily": ObjectiveCnb()})
    assert calls == [(PairingMethod.OPTIMAL, True)]


def test_service_optimal_minimises_the_czk_base(tmp_path):
    """Through the web service the objective is the run's own CZK base: lot A
    (50 CZK gain) is sold, not lot B (the EUR optimum, 254 CZK)."""
    from src.webapp.services import RunService
    svc = RunService(data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
    svc._today = lambda: date(2026, 3, 1)
    try:
        _write_dataset(svc, 2025, trades=OBJECTIVE_TRADES,
                       eoy=[_position(PAIRA, "1", "104")])
        svc._execute_run("2025-opt", 2025, "daily", ecb_provider=GoldenEcbProvider(),
                         cz_fx_provider=ObjectiveCnb(), pairing_method="optimal")
        (item,) = _disposals(svc, "2025-opt")
        assert item["acquisition_date"] == "2025-01-06"
        assert Decimal(item["gain_loss_czk"]) == Decimal("50")
    finally:
        svc.runner.shutdown(wait=False)


# ---------------------------------------------------------------------------
# V04 + optimal — the filed assignment is stored and replayed
# ---------------------------------------------------------------------------

class TestOptimalAcrossYears:

    def test_plan_is_stored_marked_and_replayed(self, two_years):
        svc = two_years
        _execute(svc, "2024-opt", 2024, "optimal")
        stored = json.loads(
            (svc.runs_dir / "2024-opt" / "pairing_plan.daily.json").read_text())
        assert stored["plan"] == {"103": [["102", "10"]]}
        entry = svc.mark_run_filed("2024-opt")
        assert entry["pairing_plan"] == {"103": [["102", "10"]]}

        meta = _execute(svc, "2025-fifo", 2025)
        (item,) = _disposals(svc, "2025-fifo")
        # 2024 sold the March lot, so January is what 2025 sells.
        assert item["acquisition_date"] == "2024-01-10"
        assert Decimal(item["cost_basis_eur"]) == Decimal("1000")
        assert meta["prior_year_pairing"] == {"2024": "optimal"}
        assert not meta.get("soy_unverified_symbols")

    def test_unmarked_optimal_year_is_assumed_fifo_with_a_warning(self, two_years):
        svc = two_years
        _execute(svc, "2024-opt", 2024, "optimal")
        meta = _execute(svc, "2025-fifo", 2025)
        (item,) = _disposals(svc, "2025-fifo")
        assert Decimal(item["cost_basis_eur"]) == Decimal("1300")
        assert any("Roky 2024 nejsou označené" in n for n in meta["notes"])

    def test_a_plan_that_does_not_fit_goes_to_review(self, two_years):
        svc = two_years
        _execute(svc, "2024-opt", 2024, "optimal")
        svc.mark_run_filed("2024-opt")

        def _break(store):
            store["years"]["2024"]["pairing_plan"] = {"103": [["999", "10"]]}
        svc._write_filed_years(_break)
        meta = _execute(svc, "2025-fifo", 2025)
        assert meta["soy_unverified_symbols"] == ["PAIRA"]
        (item,) = _disposals(svc, "2025-fifo")
        assert item["acquisition_date_estimated"] is True

    def test_fifo_run_writes_no_plan(self, two_years):
        _execute(two_years, "2024-fifo", 2024)
        assert not list((two_years.runs_dir / "2024-fifo").glob("pairing_plan.*"))


# ---------------------------------------------------------------------------
# Review follow-ups (F1-F6)
# ---------------------------------------------------------------------------

from src.engine.fifo_manager import FifoLot, plan_key  # noqa: E402


def _carried_lot(qty, total, hps, carried_from, src="M1"):
    return FifoLot(
        acquisition_date="2023-11-01", quantity=Decimal(qty),
        unit_cost_basis_eur=Decimal(total) / Decimal(qty),
        total_cost_basis_eur=Decimal(total), source_transaction_id=src,
        holding_period_start=hps, carried_from=carried_from)


def _merger_ledger(carried=("OLD:11", "OLD:12")):
    ledger = _ledger(PairingMethod.OPTIMAL)
    ledger.lots = [
        _carried_lot("10", "1000", "2023-01-10", carried[0]),
        _carried_lot("10", "2000", "2023-06-12", carried[1]),
    ]
    ledger._reset_bookkeeping()
    return ledger


class TestF1PlanKeys:

    def test_carried_lots_replay_to_the_lot_the_filed_run_sold(self):
        from src.countries.cz.optimal_pairing import rematch_cz_optimal
        filed = _merger_ledger()
        rgls = _sell(filed, "2024-06-10", "10", "15000", "103")
        out = rematch_cz_optimal(rgls, {filed.asset_internal_id: filed}, [], 2024)
        # The optimum sells the 2 000-cost lot (a loss) ...
        (r,) = out.realized_gains_losses
        assert r.total_cost_basis_eur == Decimal("2000")
        assert out.plan == {"103": [("OLD:12", Decimal("10"))]}
        assert plan_key(filed.lots[0]) == "OLD:11"

        replay = _merger_ledger()
        replay.consume_long_lots_for_sale(
            _ev(replay, FinancialEventType.TRADE_SELL_LONG, "2024-06-10",
                "-10", "15000", "103"),
            is_historical_simulation=True, method=PairingMethod.OPTIMAL,
            plan=tuple(out.plan["103"]))
        # ... so the replay keeps the 1 000-cost lot, like the filed run.
        assert [l.total_cost_basis_eur for l in replay.lots] == [Decimal("1000")]
        assert [l.total_cost_basis_eur for l in filed.lots] == [Decimal("1000")]

    def test_lots_sharing_a_key_are_ambiguous(self):
        ledger = _merger_ledger(carried=(None, None))
        with pytest.raises(UserWarning, match="ambiguous|nejednozn"):
            ledger.consume_long_lots_for_sale(
                _ev(ledger, FinancialEventType.TRADE_SELL_LONG, "2024-06-10",
                    "-10", "15000", "103"),
                is_historical_simulation=True, method=PairingMethod.OPTIMAL,
                plan=(("M1", Decimal("10")),))
        assert ledger._replayed_non_fifo_disposal is True

    def test_ambiguous_plan_ends_in_the_flagged_fallback(self, run):
        trades = [
            _trade(PAIRA, "2024-01-10", "10", "100", "101"),
            _trade(PAIRA, "2024-03-11", "10", "130", "101"),
            _trade(PAIRA, "2024-06-10", "-10", "150", "103"),
            _trade(PAIRA, "2025-06-10", "-10", "160", "104"),
        ]
        history = PairingHistory(
            methods={2024: PairingMethod.OPTIMAL},
            plans={2024: {"103": (("101", Decimal("10")),)}})
        out = run(trades, tax_year=2025, soy=[_position(PAIRA, "10", "1000")],
                  history=history)
        (r,) = out.realized_gains_losses
        assert r.is_acquisition_estimated
        assert out.soy_unverified_symbols == ["PAIRA"]


def test_f2_cli_warns_that_earlier_years_are_fifo(caplog):
    from src.main import warn_cli_pairing_history
    with caplog.at_level("WARNING"):
        warn_cli_pairing_history("lifo")
    assert any("FIFO" in r.message and r.levelname == "WARNING" for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING"):
        warn_cli_pairing_history("compare")
    assert caplog.records
    caplog.clear()
    with caplog.at_level("WARNING"):
        warn_cli_pairing_history("fifo")
    assert not caplog.records


def test_f3_sale_without_transaction_id_keeps_fifo():
    from src.countries.cz.optimal_pairing import rematch_cz_optimal
    ledger = _ledger(PairingMethod.OPTIMAL)
    _buy(ledger, "2024-01-10", "10", "1000", "101")
    _buy(ledger, "2024-03-11", "10", "1300", "102")
    sale = _ev(ledger, FinancialEventType.TRADE_SELL_LONG, "2024-06-10",
               "-10", "1500", None)
    rgls = ledger.consume_long_lots_for_sale(sale)
    out = rematch_cz_optimal(rgls, {ledger.asset_internal_id: ledger}, [], 2024)
    assert out.rematched_asset_ids == set()
    assert out.realized_gains_losses == rgls
    assert [l.source_transaction_id for l in ledger.lots] == ["102"]


class TestF4NormalisedHistory:

    def test_fifo_years_do_not_change_the_fingerprint(self):
        assert PairingHistory(methods={2023: PairingMethod.FIFO}).fingerprint() \
            == PairingHistory().fingerprint()
        assert PairingHistory(methods={2023: PairingMethod.LIFO}).fingerprint() \
            != PairingHistory().fingerprint()

    def test_adding_an_older_year_does_not_flag_runs(self, two_years):
        svc = two_years
        _execute(svc, "2025-fifo", 2025)
        _write_dataset(svc, 2023, trades=[], eoy=[])
        assert not svc.get_run("2025-fifo").get("pairing_history_changed")
        _execute(svc, "2024-lifo", 2024, "lifo")
        svc.mark_run_filed("2024-lifo")
        assert svc.get_run("2025-fifo").get("pairing_history_changed") is True


def test_f5_components_add_up_exactly_after_awkward_sales():
    ledger = _ledger(PairingMethod.WEIGHTED_AVERAGE)
    _buy(ledger, "2024-01-10", "3", "100.01", "101")
    _buy(ledger, "2024-02-12", "7", "233.37", "102")
    _buy(ledger, "2024-03-11", "11", "419.13", "103")
    rgls = []
    for n, qty in enumerate(("1.3333", "2.7", "0.07", "4.1", "3.33333333")):
        rgls += _sell(ledger, f"2024-0{n + 4}-10", qty, "77.7", f"20{n}")
        _buy(ledger, f"2024-0{n + 4}-11", "0.9", "31.17", f"30{n}")
    for r in rgls:
        assert sum((a for _d, a in r.cost_basis_components_eur), Decimal(0)) \
            == r.total_cost_basis_eur
    for lot in ledger.lots:
        if lot.cost_components is not None:
            assert sum((a for _d, a in lot.cost_components), Decimal(0)) \
                == lot.total_cost_basis_eur


def test_f6_mark_error_is_a_full_page(two_years):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from src.webapp.app import create_app
    _execute(two_years, "2024-lifo", 2024, "lifo")
    two_years._today = lambda: date(2024, 12, 1)
    with TestClient(create_app(services=two_years)) as tc:
        r = tc.post("/results/2024-lifo/filed", data={"mode": "daily"})
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert "neskončil" in r.text


def test_f5_scaled_components_sum_to_the_target_to_the_last_digit():
    """A case where proportional parts at 28 digits plus a remainder used to
    miss the target in the last digit (found by a random search)."""
    import random
    from decimal import Context
    from src.engine.fifo_manager import _scale_components
    ctx = Context(prec=28)
    target = Decimal("2315.130951050763493281567505")
    parts = _scale_components(
        [("2024-01-10", Decimal("2511.0")), ("2024-02-12", Decimal("3852.6159"))],
        target, ctx)
    assert sum((a for _d, a in parts), Decimal(0)) == target
    rng = random.Random(7)
    for _ in range(500):
        comps = [(str(k), Decimal(str(round(rng.uniform(0.001, 5000), rng.randint(0, 6)))))
                 for k in range(rng.randint(2, 5))]
        target = ctx.divide(Decimal(str(rng.uniform(1, 9999))),
                            Decimal(str(rng.uniform(0.3, 7))))
        parts = _scale_components(comps, target, ctx)
        assert sum((a for _d, a in parts), Decimal(0)) == target


# ---------------------------------------------------------------------------
# Second review pass: guards around the re-matching, the registry in listings
# ---------------------------------------------------------------------------

def _lot_rows(ledger):
    return [(lot.source_transaction_id, lot.quantity, lot.total_cost_basis_eur)
            for lot in ledger.lots]


def test_history_fingerprint_follows_the_default_method():
    """What differs from the DEFAULT counts: under a LIFO default a year
    listed as FIFO is a statement, a year listed as LIFO is not."""
    lifo_default = PairingHistory(default=PairingMethod.LIFO)
    fifo_year = PairingHistory(default=PairingMethod.LIFO,
                               methods={2024: PairingMethod.FIFO})
    lifo_year = PairingHistory(default=PairingMethod.LIFO,
                               methods={2024: PairingMethod.LIFO})
    assert fifo_year.fingerprint() != lifo_default.fingerprint()
    assert lifo_year.fingerprint() == lifo_default.fingerprint()


def test_rematch_refuses_lots_it_cannot_tell_apart():
    """Two open lots that carry no ledger id of their own would be read as one
    lot and their costs blended. The asset stays on FIFO instead."""
    from src.countries.cz.optimal_pairing import rematch_cz_optimal
    from src.engine.fifo_manager import FifoLot
    ledger = _ledger(PairingMethod.OPTIMAL)
    _buy(ledger, "2024-01-10", "10", "1000", "101")
    _buy(ledger, "2024-03-11", "10", "1300", "102")
    for day, unit, total, tx in (("2024-04-10", "140", "1400", "X1"),
                                 ("2024-04-11", "150", "1500", "X2")):
        ledger.lots.append(FifoLot(          # booked without the ledger's stamp
            acquisition_date=day, quantity=Decimal("10"),
            unit_cost_basis_eur=Decimal(unit), total_cost_basis_eur=Decimal(total),
            source_transaction_id=tx))
    rgls = _sell(ledger, "2024-06-10", "10", "1500", "103")
    out = rematch_cz_optimal(rgls, {ledger.asset_internal_id: ledger}, [], 2024)
    assert out.rematched_asset_ids == set()
    assert out.realized_gains_losses == rgls
    assert sorted(_lot_rows(ledger)) == [
        ("102", Decimal("10"), Decimal("1300")),
        ("X1", Decimal("10"), Decimal("1400")),
        ("X2", Decimal("10"), Decimal("1500")),
    ]


def test_rematching_twice_changes_nothing_more():
    """After a re-matching the ledger's journal says what the re-matching
    consumed, so a second pass starts from the same lots and ends the same."""
    from src.countries.cz.optimal_pairing import rematch_cz_optimal
    ledger = _ledger(PairingMethod.OPTIMAL)
    _buy(ledger, "2024-01-10", "10", "1000", "101")
    _buy(ledger, "2024-03-11", "10", "1300", "102")
    rgls = _sell(ledger, "2024-06-10", "4", "600", "103")
    ledgers = {ledger.asset_internal_id: ledger}
    first = rematch_cz_optimal(rgls, ledgers, [], 2024)
    # Nothing is exempt, so the dearer March lot is sold: 10 x 100 and 6 x 130 stay.
    assert _lot_rows(ledger) == [("101", Decimal("10"), Decimal("1000")),
                                 ("102", Decimal("6"), Decimal("780"))]
    second = rematch_cz_optimal(first.realized_gains_losses, ledgers, [], 2024)
    assert _lot_rows(ledger) == [("101", Decimal("10"), Decimal("1000")),
                                 ("102", Decimal("6"), Decimal("780"))]
    assert second.plan == first.plan


def test_dataset_rows_do_not_carry_the_stored_plan(two_years):
    """The year row shown on the web page and to MCP names the filed run; the
    assignment itself (one entry per sale) stays in the registry."""
    svc = two_years
    _execute(svc, "2024-opt", 2024, "optimal")
    svc.mark_run_filed("2024-opt")
    filed = svc.get_year(2024).filed
    assert (filed["pairing_method"], filed["run_id"]) == ("optimal", "2024-opt")
    assert "pairing_plan" not in filed
    assert svc.load_filed_years()["years"]["2024"]["pairing_plan"]


def test_listing_runs_reads_the_registry_once(two_years, monkeypatch):
    """The running year is recomputed almost daily; a listing must not read
    the registry and scan the datasets again for every stored run."""
    svc = two_years
    for index in range(4):
        _execute(svc, f"2025-r{index}", 2025)
    reads = {"count": 0}
    original = svc.load_filed_years

    def counting():
        reads["count"] += 1
        return original()

    monkeypatch.setattr(svc, "load_filed_years", counting)
    runs = svc.list_runs(limit=None)
    assert len(runs) == 4
    assert all(r["can_mark_filed"] for r in runs)
    assert reads["count"] == 1
