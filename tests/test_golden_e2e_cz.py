# tests/test_golden_e2e_cz.py
"""
Golden end-to-end regression test for the CZ plugin (tax year 2024).

Runs the FULL pipeline (CSV parsing -> enrichment -> FIFO engine -> CZ
aggregation) on a synthetic dataset with independently HAND-COMPUTED expected
values. The expected figures were derived on 2026-07-02 from the tax rules
alone (per-leg FX per NSS 2 Afs 4/2019-35, §4/1/w time test, 100k annual
limit, §16/2 base rounding, §38f/8 per-state FTC cap, §146/1 DR tax rounding)
using real ECB/ČNB rates fetched directly from the providers' public APIs —
NOT from engine output. The engine matched all figures on first run
(HEAD 06ed6f1); this test pins that behaviour offline.

Scenario (all USD):
  1. ALPHA  - buy 100 @ 50 (2024-03-05), sell @ 60 (2024-09-10),
              1 USD commission per trade -> taxable §10 securities gain
  2. OLDCO  - buy 50 @ 20 (2020-06-15, historical trade + SOY position),
              sell @ 40 (2024-05-20) -> time-test exempt (1435 days > 3y)
  3. short put "P UNDR 20240315 95 M" - premium 200 USD - 1 commission
              (2024-02-12), expires worthless "Ep" (2024-03-15)
              -> §10 options gain; premium leg converted at the OPEN date
  4. DIVCO  - dividend 100 USD + 15 USD US WHT (2024-04-15); 100 shares in
              SOY and EOY with no trades (SOY fallback lot, never disposed)
              -> §8 income; FTC designed to sit EXACTLY at the 15% treaty
              cap, which the §16/2 base rounding then trims to 356.33

Annual 100k limit: taxable disposal proceeds (ALPHA only) = 136,256.86 CZK
> 100,000 -> deliberately NOT applied.
"""
from decimal import Decimal

from src.countries.cz.config import CzTaxConfig
from src.countries.registry import get_tax_plugin
from tests.support.base import FifoTestCaseBase
from tests.support.golden_fx import GoldenCnbProvider, GoldenEcbProvider

TAX_YEAR = 2024
TWO = Decimal("0.01")


def q2(value) -> Decimal:
    return Decimal(value).quantize(TWO)


# ---------------------------------------------------------------------------
# Synthetic input rows (mirror of the local data/synthetic_2024 CSVs)
# ---------------------------------------------------------------------------

ACC = "U1234567"
OPT_SYMBOL = "P UNDR 20240315 95 M"
OPT_DESC = "UNDR 15MAR24 95 P"

TRADES = [
    # ClientAccountID, Currency, AssetClass, SubCategory, Symbol, Description,
    # ISIN, Strike, Expiry, Put/Call, TradeDate, Quantity, TradePrice,
    # IBCommission, IBCommissionCurrency, Buy/Sell, TransactionID, Notes/Codes,
    # UnderlyingSymbol, Conid, UnderlyingConid, Multiplier, Open/CloseIndicator
    [ACC, "USD", "STK", "COMMON", "OLDCO", "OLDCO COMMON STOCK", "US1111111111",
     None, None, None, "2020-06-15", "50", "20", "-1", "USD", "BUY", "1001",
     None, None, "111111", None, "1", "O"],
    [ACC, "USD", "OPT", "P", OPT_SYMBOL, OPT_DESC, None,
     "95", "2024-03-15", "P", "2024-02-12", "-1", "2", "-1", "USD", "SELL", "3001",
     None, "UNDR", "333333", "555555", "100", "O"],
    [ACC, "USD", "STK", "COMMON", "ALPHA", "ALPHA COMMON STOCK", "US2222222222",
     None, None, None, "2024-03-05", "100", "50", "-1", "USD", "BUY", "2001",
     None, None, "222222", None, "1", "O"],
    [ACC, "USD", "OPT", "P", OPT_SYMBOL, OPT_DESC, None,
     "95", "2024-03-15", "P", "2024-03-15", "1", "0", "0", "USD", "BUY", "3002",
     "Ep", "UNDR", "333333", "555555", "100", "C"],
    [ACC, "USD", "STK", "COMMON", "OLDCO", "OLDCO COMMON STOCK", "US1111111111",
     None, None, None, "2024-05-20", "-50", "40", "-1", "USD", "SELL", "2002",
     None, None, "111111", None, "1", "C"],
    [ACC, "USD", "STK", "COMMON", "ALPHA", "ALPHA COMMON STOCK", "US2222222222",
     None, None, None, "2024-09-10", "-100", "60", "-1", "USD", "SELL", "2003",
     None, None, "222222", None, "1", "C"],
]

CASH_TRANSACTIONS = [
    # ClientAccountID, Currency, AssetClass, SubCategory, Symbol, Description,
    # SettleDate, Amount, Type, Conid, UnderlyingConid, ISIN,
    # IssuerCountryCode, TransactionID
    [ACC, "USD", "STK", "COMMON", "DIVCO",
     "DIVCO(US4444444444) CASH DIVIDEND USD 1.00 PER SHARE (Ordinary Dividend)",
     "2024-04-15", "100", "Dividends", "444444", None, "US4444444444", "US", "4001"],
    [ACC, "USD", "STK", "COMMON", "DIVCO",
     "DIVCO(US4444444444) CASH DIVIDEND USD 1.00 PER SHARE - US TAX",
     "2024-04-15", "-15", "Withholding Tax", "444444", None, "US4444444444", "US", "4002"],
]

POSITIONS_SOY = [
    # ClientAccountID, Currency, AssetClass, SubCategory, Symbol, Description,
    # ISIN, Quantity, PositionValue, MarkPrice, CostBasisMoney,
    # UnderlyingSymbol, Conid, UnderlyingConid, Multiplier
    [ACC, "USD", "STK", "COMMON", "OLDCO", "OLDCO COMMON STOCK", "US1111111111",
     "50", "1750", "35", "1001", None, "111111", None, "1"],
    [ACC, "USD", "STK", "COMMON", "DIVCO", "DIVCO COMMON STOCK", "US4444444444",
     "100", "3000", "30", "2500", None, "444444", None, "1"],
]

POSITIONS_EOY = [
    [ACC, "USD", "STK", "COMMON", "DIVCO", "DIVCO COMMON STOCK", "US4444444444",
     "100", "3200", "32", "2500", None, "444444", None, "1"],
]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

class TestGoldenE2ECz(FifoTestCaseBase):
    """Pins the hand-computed golden figures for the synthetic 2024 scenario."""

    def _run_golden_pipeline(self):
        results = self._run_pipeline(
            trades_data=TRADES,
            positions_start_data=POSITIONS_SOY,
            positions_end_data=POSITIONS_EOY,
            cash_transactions_data=CASH_TRANSACTIONS,
            corporate_actions_data=None,
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

    def test_pipeline_produces_expected_rgls_and_no_eoy_mismatch(self):
        results, _ = self._run_golden_pipeline()
        assert results.eoy_mismatch_error_count == 0
        assert len(results.realized_gains_losses) == 3  # ALPHA, OLDCO, PUTX

    def test_disposal_items_match_hand_computed_czk_legs(self):
        _, cz_result = self._run_golden_pipeline()
        items = cz_result.country_result["items"]
        by_symbol = {}
        for it in items:
            if it.cost_basis_czk is not None or it.proceeds_czk is not None:
                by_symbol[it.asset_symbol] = it

        alpha = by_symbol["ALPHA"]
        assert q2(alpha.cost_basis_czk) == Decimal("116877.46")
        assert q2(alpha.proceeds_czk) == Decimal("136256.86")
        assert q2(alpha.gain_loss_czk) == Decimal("19379.40")
        assert alpha.is_taxable

        oldco = by_symbol["OLDCO"]
        assert q2(oldco.cost_basis_czk) == Decimal("23732.94")
        assert q2(oldco.proceeds_czk) == Decimal("45543.92")
        assert q2(oldco.gain_loss_czk) == Decimal("21810.98")
        assert not oldco.is_taxable
        assert oldco.holding_period_days == 1435
        assert oldco.exemption_reason is not None
        assert "TIME_TEST" in str(oldco.exemption_reason)

        putx = by_symbol[OPT_SYMBOL]
        # Short position: the premium (proceeds) leg carries the OPEN-date
        # rate (2024-02-12), the zero cost leg the expiry-date rate.
        assert q2(putx.cost_basis_czk) == Decimal("0.00")
        assert q2(putx.proceeds_czk) == Decimal("4657.74")
        assert q2(putx.gain_loss_czk) == Decimal("4657.74")
        assert putx.is_taxable

    def test_section_10_summary_matches_hand_computed_figures(self):
        _, cz_result = self._run_golden_pipeline()
        line = cz_result.sections["cz_10_summary"].line_items
        assert q2(line["sec_net_taxable_czk"]) == Decimal("19379.40")
        assert q2(line["sec_exempt_time_test_czk"]) == Decimal("21810.98")
        assert q2(line["opt_net_taxable_czk"]) == Decimal("4657.74")
        assert q2(line["combined_net_taxable_czk"]) == Decimal("24037.15")
        # 136,256.86 CZK taxable proceeds > 100k threshold -> limit NOT applied
        assert q2(line["annual_limit_eligible_proceeds_czk"]) == Decimal("136256.86")
        assert Decimal(line["annual_limit_applied"]) == 0

    def test_section_8_dividends_and_wht(self):
        _, cz_result = self._run_golden_pipeline()
        line = cz_result.sections["cz_8_dividends"].line_items
        assert q2(line["gross_dividends_czk"]) == Decimal("2376.80")
        assert q2(line["wht_paid_czk"]) == Decimal("356.52")

    def test_tax_liability_and_ftc_cap_edge(self):
        """The WHT sits exactly at the 15% treaty cap; §16/2 rounding of the
        base then pushes the §38f/8 per-state cap 0.19 CZK BELOW the paid WHT
        — the credit must come out capped, not the full WHT."""
        _, cz_result = self._run_golden_pipeline()
        line = cz_result.sections["cz_tax_liability"].line_items
        assert q2(line["taxable_dividends_czk"]) == Decimal("2376.80")
        assert q2(line["taxable_securities_net_czk"]) == Decimal("19379.40")
        assert q2(line["taxable_options_net_czk"]) == Decimal("4657.74")
        assert q2(line["combined_taxable_base_czk"]) == Decimal("26413.95")
        assert q2(line["base_for_base_rate_czk"]) == Decimal("26400.00")
        assert q2(line["gross_czech_tax_czk"]) == Decimal("3960.00")
        assert q2(line["preliminary_ftc_czk"]) == Decimal("356.52")
        assert q2(line["czech_tax_on_foreign_income_czk"]) == Decimal("356.33")
        assert q2(line["final_creditable_ftc_czk"]) == Decimal("356.33")
        assert q2(line["non_creditable_ftc_czk"]) == Decimal("0.19")
        assert q2(line["final_czech_tax_after_credit_czk"]) == Decimal("3604.00")

    def test_ftc_per_country_breakdown(self):
        _, cz_result = self._run_golden_pipeline()
        line = cz_result.sections["cz_ftc_summary"].line_items
        assert q2(line["ftc_foreign_income_total_czk"]) == Decimal("2376.80")
        assert q2(line["ftc_us_paid_czk"]) == Decimal("356.52")
        assert q2(line["ftc_us_creditable_czk"]) == Decimal("356.52")
