# tests/test_golden_scenarios_cz.py
"""
Extended golden end-to-end scenarios for the CZ plugin (tax year 2024).

Complements ``test_golden_e2e_cz.py`` with the mechanics fixed in the
2026-07 calculation audit that the base golden scenario does not exercise:

  S1  short put ×2: one contract assigned into stock (premium folded into
      the stock basis), the other expires worthless — pins the M5 pro-rata
      premium allocation across partial consumption AND the current
      M17/M18 mixed-FX-date behaviour of the premium component
  S2  weekend dividend: settle date on Saturday — pins the L9 ČNB fallback
      audit trail (fx_date_used = Friday fixing, conversion_note set)
  S3  forward split 2:1 between buy and sell — split must scale quantity,
      preserve total cost AND the original acquisition date (time test)
  S4  cash merger (TC "FOR USD ... PER SHARE") — pins L6 disposal semantics
  S5  "C;O" flip: one SELL closes the long position and opens a short with
      per-unit pro-rata proceeds (M19); the short stays open at EOY
  S6  closing a long option where commission exceeds gross proceeds — the
      negative net proceeds must keep their sign (L5)

All expected figures are HAND-COMPUTED from the pinned real ECB/ČNB rates
in ``tests/support/golden_fx.py`` (see scenario docstrings), independent of
engine output. FX legs follow NSS 2 Afs 4/2019-35: each cash flow at the
rate of its own date; short/option-premium legs at the opening date.

Because each scenario runs in isolation, the security disposals in S3–S5
have annual proceeds UNDER the 100k CZK limit and come out exempt
(all-or-nothing §4/1/w rule) — deliberately kept that way, so these
scenarios also pin the under-limit exemption branch, complementing the
over-limit branch pinned by ``test_golden_e2e_cz.py`` (and by S1 here,
whose stock proceeds of 215,753 CZK exceed the limit).
"""
from decimal import Decimal

from src.countries.cz.config import CzTaxConfig
from src.countries.registry import get_tax_plugin
from tests.support.base import FifoTestCaseBase
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider

TAX_YEAR = 2024
TWO = Decimal("0.01")
ACC = "U1234567"


def q2(value) -> Decimal:
    return Decimal(value).quantize(TWO)


class _GoldenScenarioBase(FifoTestCaseBase):
    """Runs the core pipeline + CZ aggregation with the pinned golden rates."""

    def _run_cz(self, trades=None, soy=None, eoy=None, cash=None, corp=None):
        results = self._run_pipeline(
            trades_data=trades,
            positions_start_data=soy,
            positions_end_data=eoy,
            cash_transactions_data=cash,
            corporate_actions_data=corp,
            custom_rate_provider=GoldenEcbProvider(),
            tax_year=TAX_YEAR,
        )
        plugin = get_tax_plugin(
            "cz", config=CzTaxConfig(), fx_provider=GoldenCnbProvider()
        )
        cz_result = plugin.get_tax_aggregator().aggregate(
            realized_gains_losses=results.realized_gains_losses,
            financial_events=results.processed_income_events,
            asset_resolver=results.asset_resolver,
            tax_year=TAX_YEAR,
        )
        return results, cz_result

    @staticmethod
    def _disposal_items(cz_result):
        return {
            it.asset_symbol: it
            for it in cz_result.country_result["items"]
            if it.cost_basis_czk is not None or it.proceeds_czk is not None
        }


# ---------------------------------------------------------------------------
# S1 — partial assignment + partial expiry of a 2-contract short put
# ---------------------------------------------------------------------------

S1_OPT_SYMBOL = "P UND2 20240315 90 M"
S1_OPT_DESC = "UND2 15MAR24 90 P"

S1_TRADES = [
    # sell 2 puts @ 3.00 x 100, commission -1  -> net premium 599 USD
    [ACC, "USD", "OPT", "P", S1_OPT_SYMBOL, S1_OPT_DESC, None,
     "90", "2024-03-15", "P", "2024-02-12", "-2", "3", "-1", "USD", "SELL",
     "5001", None, "UND2", "777001", "777000", "100", "O"],
    # assignment of 1 contract: option close @ 0 ...
    [ACC, "USD", "OPT", "P", S1_OPT_SYMBOL, S1_OPT_DESC, None,
     "90", "2024-03-15", "P", "2024-03-05", "1", "0", "0", "USD", "BUY",
     "5002", "A", "UND2", "777001", "777000", "100", "C"],
    # ... linked stock delivery: buy 100 @ strike 90, no commission
    [ACC, "USD", "STK", "COMMON", "UND2", "UND2 COMMON STOCK", "US7777777777",
     None, None, None, "2024-03-05", "100", "90", "0", "USD", "BUY",
     "5003", "A", None, "777000", None, "1", "O"],
    # the second contract expires worthless
    [ACC, "USD", "OPT", "P", S1_OPT_SYMBOL, S1_OPT_DESC, None,
     "90", "2024-03-15", "P", "2024-03-15", "1", "0", "0", "USD", "BUY",
     "5004", "Ep", "UND2", "777001", "777000", "100", "C"],
    # sell the delivered stock
    [ACC, "USD", "STK", "COMMON", "UND2", "UND2 COMMON STOCK", "US7777777777",
     None, None, None, "2024-09-10", "-100", "95", "-1", "USD", "SELL",
     "5005", None, None, "777000", None, "1", "C"],
]


class TestS1PartialAssignmentAndExpiry(_GoldenScenarioBase):
    """Hand-computed (net premium 599 USD, half per contract = 299.5):
      expiry RGL:  proceeds = 299.5/1.0773 EUR @ open date -> x25.215
                   = 7,010.02 CZK (gain, cost 0)
      stock RGL:   cost = (9000/1.0849 - 299.5/1.0773) EUR, converted
                   x25.355 @ 2024-03-05 = 203,288.42 CZK
                   (premium component keeps the option-open ECB rate but the
                   CZK conversion uses the stock date — documented M17/M18
                   behaviour; this pin must CHANGE when per-component dates land)
      proceeds = 9499/1.1031 x25.055 = 215,753.28 CZK; gain 12,464.86 CZK
    """

    def test_partial_assignment_premium_prorata_and_expiry(self):
        results, cz_result = self._run_cz(trades=S1_TRADES)
        assert results.eoy_mismatch_error_count == 0
        assert len(results.realized_gains_losses) == 2

        items = self._disposal_items(cz_result)

        option = items[S1_OPT_SYMBOL]
        assert q2(option.proceeds_czk) == Decimal("7010.02")
        assert q2(option.cost_basis_czk) == Decimal("0.00")
        assert q2(option.gain_loss_czk) == Decimal("7010.02")

        stock = items["UND2"]
        assert q2(stock.cost_basis_czk) == Decimal("203288.42")
        assert q2(stock.proceeds_czk) == Decimal("215753.28")
        assert q2(stock.gain_loss_czk) == Decimal("12464.86")
        assert stock.is_taxable  # 189 days < 3y


# ---------------------------------------------------------------------------
# S2 — weekend dividend (ČNB fallback audit trail, L9)
# ---------------------------------------------------------------------------

S2_CASH = [
    [ACC, "USD", "STK", "COMMON", "DIVX",
     "DIVX(US8888888888) CASH DIVIDEND USD 0.60 PER SHARE (Ordinary Dividend)",
     "2024-06-15", "60", "Dividends", "888000", None, "US8888888888", "US", "5101"],
    [ACC, "USD", "STK", "COMMON", "DIVX",
     "DIVX(US8888888888) CASH DIVIDEND USD 0.60 PER SHARE - US TAX",
     "2024-06-15", "-9", "Withholding Tax", "888000", None, "US8888888888", "US", "5102"],
]


class TestS2WeekendDividendFallback(_GoldenScenarioBase):
    """2024-06-15 is a Saturday; the ČNB fixing of Friday 2024-06-14
    (23.154 CZK/USD) applies: dividend 60 USD = 1,389.24 CZK, WHT 9 USD
    = 208.39 CZK (exactly the 15% treaty cap). The audit trail must show
    the REAL rate date and a fallback note (L9)."""

    def test_weekend_dividend_uses_friday_rate_with_audit_note(self):
        _, cz_result = self._run_cz(cash=S2_CASH)

        dividends = [
            it for it in cz_result.country_result["items"]
            if it.asset_symbol == "DIVX" and it.item_type.name == "DIVIDEND"
        ]
        assert len(dividends) == 1
        div = dividends[0]
        assert q2(div.amount_czk) == Decimal("1389.24")
        assert div.fx is not None
        assert div.fx.fx_date_used == "2024-06-14"
        assert div.fx.event_date == "2024-06-15"
        assert div.fx.conversion_note and "fallback" in div.fx.conversion_note.lower()

        assert len(div.wht_records) == 1
        assert q2(div.wht_records[0].amount_czk) == Decimal("208.39")

        line = cz_result.sections["cz_ftc_summary"].line_items
        assert q2(line["ftc_us_creditable_czk"]) == Decimal("208.39")


# ---------------------------------------------------------------------------
# S3 — forward split 2:1 between acquisition and sale
# ---------------------------------------------------------------------------

S3_TRADES = [
    [ACC, "USD", "STK", "COMMON", "SPLTCO", "SPLTCO COMMON STOCK", "US5555555555",
     None, None, None, "2024-02-12", "100", "10", "-1", "USD", "BUY",
     "6001", None, None, "555000", None, "1", "O"],
    [ACC, "USD", "STK", "COMMON", "SPLTCO", "SPLTCO COMMON STOCK", "US5555555555",
     None, None, None, "2024-09-10", "-200", "7", "-1", "USD", "SELL",
     "6002", None, None, "555000", None, "1", "C"],
]

S3_CORP_ACTIONS = [
    # ClientAccountID, Symbol, Description, ISIN, Report Date, Code, Type,
    # ActionID, Conid, UnderlyingConid, UnderlyingSymbol, CurrencyPrimary,
    # Amount, Proceeds, Value, Quantity
    [ACC, "SPLTCO",
     "SPLTCO(US5555555555) SPLIT 2 FOR 1 (SPLTCO, SPLTCO COMMON STOCK, US5555555555)",
     "US5555555555", "2024-05-20", None, "FS", "900001", "555000", None, None,
     "USD", "0", "0", "0", "100"],
]


class TestS3ForwardSplit(_GoldenScenarioBase):
    """Buy 100 @ 10 (cost 1001 USD, 2024-02-12), split 2:1, sell 200 @ 7
    (proceeds 1399 USD, 2024-09-10). Hand-computed:
      cost = 1001/1.0773 x25.215 = 23,429.14 CZK (acquisition date KEPT)
      proceeds = 1399/1.1031 x25.055 = 31,775.85 CZK; gain 8,346.71 CZK
    Proceeds 31,775.85 <= 100k -> exempt via the annual limit."""

    def test_split_scales_quantity_and_preserves_acquisition_date(self):
        results, cz_result = self._run_cz(
            trades=S3_TRADES, corp=S3_CORP_ACTIONS
        )
        assert results.eoy_mismatch_error_count == 0
        assert len(results.realized_gains_losses) == 1
        rgl = results.realized_gains_losses[0]
        assert rgl.quantity_realized == Decimal("200")

        item = self._disposal_items(cz_result)["SPLTCO"]
        assert item.acquisition_date == "2024-02-12"
        assert item.holding_period_days == 211
        assert q2(item.cost_basis_czk) == Decimal("23429.14")
        assert q2(item.proceeds_czk) == Decimal("31775.85")
        assert q2(item.gain_loss_czk) == Decimal("8346.71")
        assert item.is_exempt and item.exempt_due_to_annual_limit
        assert item.exemption_reason.name == "ANNUAL_LIMIT_NOT_EXCEEDED"


# ---------------------------------------------------------------------------
# S4 — cash merger (TC)
# ---------------------------------------------------------------------------

S4_TRADES = [
    [ACC, "USD", "STK", "COMMON", "MRGCO", "MRGCO CORP", "US6666666666",
     None, None, None, "2024-03-05", "100", "30", "-1", "USD", "BUY",
     "7001", None, None, "666000", None, "1", "O"],
]

S4_CORP_ACTIONS = [
    [ACC, "MRGCO",
     "MRGCO(US6666666666) MERGED(Acquisition) FOR USD 35 PER SHARE CASH "
     "(MRGCO, MRGCO CORP, US6666666666)",
     "US6666666666", "2024-09-10", None, "TC", "900002", "666000", None, None,
     "USD", "0", "3500", "-3001", "-100"],
]


class TestS4CashMerger(_GoldenScenarioBase):
    """Buy 100 @ 30 (cost 3001 USD, 2024-03-05); cash-out at 35 USD/share on
    2024-09-10 (proceeds 3500 USD). Hand-computed:
      cost = 3001/1.0849 x25.355 = 70,135.82 CZK
      proceeds = 3500/1.1031 x25.055 = 79,496.42 CZK; gain 9,360.60 CZK
    Proceeds 79,496.42 <= 100k -> exempt via the annual limit."""

    def test_cash_merger_realizes_full_position(self):
        results, cz_result = self._run_cz(
            trades=S4_TRADES, corp=S4_CORP_ACTIONS
        )
        assert results.eoy_mismatch_error_count == 0
        assert len(results.realized_gains_losses) == 1
        assert results.realized_gains_losses[0].quantity_realized == Decimal("100")

        item = self._disposal_items(cz_result)["MRGCO"]
        assert q2(item.cost_basis_czk) == Decimal("70135.82")
        assert q2(item.proceeds_czk) == Decimal("79496.42")
        assert q2(item.gain_loss_czk) == Decimal("9360.60")
        assert item.is_exempt and item.exempt_due_to_annual_limit
        assert item.exemption_reason.name == "ANNUAL_LIMIT_NOT_EXCEEDED"


# ---------------------------------------------------------------------------
# S5 — "C;O" flip: close long 100, open short 50 in one trade
# ---------------------------------------------------------------------------

S5_TRADES = [
    [ACC, "USD", "STK", "COMMON", "FLIPCO", "FLIPCO INC", "US9999999999",
     None, None, None, "2024-03-05", "100", "20", "-1", "USD", "BUY",
     "8001", None, None, "999000", None, "1", "O"],
    # one SELL 150 @ 22 (net 3299 USD): closes 100, opens short 50
    [ACC, "USD", "STK", "COMMON", "FLIPCO", "FLIPCO INC", "US9999999999",
     None, None, None, "2024-05-20", "-150", "22", "-1", "USD", "SELL",
     "8002", None, None, "999000", None, "1", "C;O"],
]

S5_EOY = [
    [ACC, "USD", "STK", "COMMON", "FLIPCO", "FLIPCO INC", "US9999999999",
     "-50", "-1100", "22", "-1100", None, "999000", None, "1"],
]


class TestS5FlipIndicator(_GoldenScenarioBase):
    """Hold 100 @ 20 (cost 2001 USD); SELL 150 @ 22 with "C;O" (net 3299 USD).
    Hand-computed for the CLOSED 100 (per-unit pro-rata, M19):
      proceeds = 3299 x (100/150) / 1.0861 x24.745 = 50,108.19 CZK
      cost = 2001/1.0849 x25.355 = 46,765.01 CZK; gain 3,343.18 CZK
    The remaining 50 open a SHORT position that survives to EOY (no RGL).
    Proceeds 50,108.19 <= 100k -> exempt via the annual limit."""

    def test_flip_closes_long_and_opens_short(self):
        results, cz_result = self._run_cz(trades=S5_TRADES, eoy=S5_EOY)
        assert results.eoy_mismatch_error_count == 0
        assert len(results.realized_gains_losses) == 1
        rgl = results.realized_gains_losses[0]
        assert rgl.quantity_realized == Decimal("100")

        item = self._disposal_items(cz_result)["FLIPCO"]
        assert q2(item.cost_basis_czk) == Decimal("46765.01")
        assert q2(item.proceeds_czk) == Decimal("50108.19")
        assert q2(item.gain_loss_czk) == Decimal("3343.18")
        assert item.is_exempt and item.exempt_due_to_annual_limit
        assert item.exemption_reason.name == "ANNUAL_LIMIT_NOT_EXCEEDED"


# ---------------------------------------------------------------------------
# S6 — negative net proceeds on an option close (L5)
# ---------------------------------------------------------------------------

S6_OPT_SYMBOL = "C NEGO 20240621 50 M"
S6_OPT_DESC = "NEGO 21JUN24 50 C"

S6_TRADES = [
    # buy 1 call @ 3.00 x 100 + 1 commission -> cost 301 USD
    [ACC, "USD", "OPT", "C", S6_OPT_SYMBOL, S6_OPT_DESC, None,
     "50", "2024-06-21", "C", "2024-02-12", "1", "3", "-1", "USD", "BUY",
     "9001", None, "NEGO", "444001", "444000", "100", "O"],
    # sell @ 0.005 x 100 = 0.50 gross, commission -1 -> net proceeds -0.50 USD
    [ACC, "USD", "OPT", "C", S6_OPT_SYMBOL, S6_OPT_DESC, None,
     "50", "2024-06-21", "C", "2024-03-05", "-1", "0.005", "-1", "USD", "SELL",
     "9002", None, "NEGO", "444001", "444000", "100", "C"],
]


class TestS6NegativeNetProceeds(_GoldenScenarioBase):
    """Closing a nearly worthless long call where commission (1.00) exceeds
    gross proceeds (0.50). Hand-computed:
      cost = 301/1.0773 x25.215 = 7,045.13 CZK
      proceeds = -0.50/1.0849 x25.355 = -11.69 CZK (sign preserved, L5)
      loss = -7,056.81 CZK; the options net loss is floored to 0 in the
      liability base (no carryforward)."""

    def test_negative_proceeds_keep_sign(self):
        results, cz_result = self._run_cz(trades=S6_TRADES)
        assert results.eoy_mismatch_error_count == 0
        assert len(results.realized_gains_losses) == 1

        item = self._disposal_items(cz_result)[S6_OPT_SYMBOL]
        assert item.proceeds_czk < 0  # the L5 regression would flip this
        assert q2(item.proceeds_czk) == Decimal("-11.69")
        assert q2(item.cost_basis_czk) == Decimal("7045.13")
        assert q2(item.gain_loss_czk) == Decimal("-7056.81")

        line = cz_result.sections["cz_tax_liability"].line_items
        assert q2(line["taxable_options_net_czk"]) == Decimal("0.00")
