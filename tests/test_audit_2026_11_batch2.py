# tests/test_audit_2026_11_batch2.py
"""
Regression tests for the second batch of 2026-11 audit findings, all in the
Czech tax-liability / foreign-tax-credit path:

* X04 — the §16a separate-base scenario moved only foreign dividends out of
  the general base and left foreign interest behind, although the election
  covers all foreign §8 income or none of it.
* W26 — that scenario divided the §38f ratio by the tax base already rounded
  down to hundreds, credited more than the general base would, and reported
  the difference as a saving below the 23 % threshold, where both variants
  tax everything at 15 %. (The unrounded divisor is the audit's proposal and
  the owner's decision; the return's annex for that base reads differently -
  see the test class.)
* W27 — South Korea had no treaty cap, so Korean dividends were credited at
  the 15 % default instead of the treaty's 5 %.
* X27 — with two income rows of the same size on one asset and day, the
  income row a tax row was linked to depended on a per-run random event id.
* X20 — the summary listed only preliminary per-state credit amounts, which
  do not add up to the credit on Příloha č. 3.

Everything here is synthetic: invented amounts and states, no real statement,
rate or account (U1234567 where a stored run needs one). Expected figures are
worked out by hand in the docstrings, never taken from the code under test.
"""
import io
import uuid
from datetime import date
from decimal import Decimal

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.exporters.xlsx_exporter import export_cz_to_xlsx
from src.countries.cz.foreign_tax_credit import evaluate_foreign_tax_credit
from src.countries.cz.form_mapping import build_form_mapping
from src.countries.cz.fx_policy import CzCurrencyConverter
from src.countries.cz.loss_offsetting import CzLossOffsettingResult
from src.countries.cz.plugin import CzechTaxAggregator
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzWhtRecord
from src.countries.cz.tax_liability import compute_tax_liability
from src.domain.enums import FinancialEventType
from src.domain.events import CashFlowEvent, WithholdingTaxEvent
from src.processing.withholding_tax_linker import WithholdingTaxLinker
from tests.support.golden_fx import GoldenCnbProvider

ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _income(item_type: CzTaxItemType, country: str, amount_czk: str,
            wht_czk: str = "0") -> CzTaxItem:
    """One foreign §8 income item, with the tax its source state withheld."""
    section = (CzTaxSection.CZ_8_INTEREST if item_type == CzTaxItemType.INTEREST
               else CzTaxSection.CZ_8_DIVIDENDS)
    item = CzTaxItem(
        item_type=item_type,
        section=section,
        source_event_id=uuid.uuid4(),
        event_date="2025-06-16",
        amount_czk=Decimal(amount_czk),
        source_country=country,
    )
    if Decimal(wht_czk) != ZERO:
        item.wht_records.append(CzWhtRecord(
            wht_event_id=uuid.uuid4(),
            event_date="2025-06-16",
            original_amount=Decimal(wht_czk),
            original_currency="CZK",
            amount_czk=Decimal(wht_czk),
            source_country=country,
        ))
    return item


def _dividend(country: str, amount_czk: str, wht_czk: str = "0") -> CzTaxItem:
    return _income(CzTaxItemType.DIVIDEND, country, amount_czk, wht_czk)


def _interest(country: str, amount_czk: str, wht_czk: str = "0") -> CzTaxItem:
    return _income(CzTaxItemType.INTEREST, country, amount_czk, wht_czk)


def _section_total(items, section: CzTaxSection) -> Decimal:
    return sum((it.amount_czk for it in items if it.section == section), ZERO)


def _netting(securities_net: str = "0") -> CzLossOffsettingResult:
    netting = CzLossOffsettingResult()
    netting.securities.taxable_gains = Decimal(securities_net)
    netting.compute_combined()
    return netting


def _liability(items, securities_net: str = "0", config: CzTaxConfig = None,
               tax_year: int = 2025):
    """Run the real §38f evaluator and the liability over *items*.

    The same two calls the plugin makes, in the same order, so the credit a
    scenario works with is the one the evaluator really hands it.
    """
    cfg = config or CzTaxConfig()
    ftc = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
    return compute_tax_liability(
        taxable_dividends=_section_total(items, CzTaxSection.CZ_8_DIVIDENDS),
        taxable_interest=_section_total(items, CzTaxSection.CZ_8_INTEREST),
        netting=_netting(securities_net),
        ftc_summary=ftc,
        config=cfg,
        tax_year=tax_year,
    )


# A 23 % threshold of CZK 100 000 keeps the arithmetic below checkable by
# hand; the statutory one is far higher and changes nothing about the logic.
_LOW_THRESHOLD = Decimal("100000")


# ---------------------------------------------------------------------------
# X04 — electing the separate base moves ALL foreign §8 income, interest too
# ---------------------------------------------------------------------------

class TestX04SeparateBaseTakesForeignInterestToo:
    """§8 odst. 8 ZDP lets foreign §8 income be taxed in the separate base
    of §16a, and adds that once such income is included, all of it is. The
    scenario kept foreign interest in the general base regardless.

    The example, with the 23 % rate starting at 100 000:

        foreign dividends, state US   80 000   withheld 12 000 (15 %)
        foreign interest,  state CA   40 000   withheld  4 000 (10 %)
        §10 securities, net           90 000

    Variant A - everything in the general base:
        base    80 000 + 40 000 + 90 000              = 210 000
        tax     100 000 x 15 % + 110 000 x 23 %       =  40 300
        §38f    US limit 40 300 x 80/210 = 15 352.38  -> credit 12 000
                CA limit 40 300 x 40/210 =  7 676.19  -> credit  4 000
        tax after credit  40 300 - 16 000             =  24 300

    Variant B - the separate base is elected:
        separate base   80 000 + 40 000               = 120 000
        tax             120 000 x 15 %                =  18 000
        §38f    US limit 18 000 x 80/120 = 12 000     -> credit 12 000
                CA limit 18 000 x 40/120 =  6 000     -> credit  4 000
        separate base after credit  18 000 - 16 000   =   2 000
        general base    90 000 (securities only)      -> 13 500, no credit
        total           2 000 + 13 500                =  15 500
        saving          24 300 - 15 500               =   8 800
                        (8 % of the 110 000 that sat above the threshold)

    With the interest left behind the general base was 130 000, taxed
    21 900 less the 4 000 credit, and variant B came to 17 900: 2 400 too
    much, 8 % of the 30 000 of interest still above the threshold.
    """

    def _result(self):
        return _liability(
            [_dividend("US", "80000", "12000"), _interest("CA", "40000", "4000")],
            securities_net="90000",
            config=CzTaxConfig(elevated_rate_threshold_czk=_LOW_THRESHOLD),
        )

    def test_variant_a_is_what_the_arithmetic_says(self):
        result = self._result()

        assert result.combined_taxable_base == Decimal("210000")
        assert result.gross_czech_tax == Decimal("40300.00")
        assert result.final_creditable_ftc == Decimal("16000.00")
        assert result.final_czech_tax_after_credit == Decimal("24300")

    def test_interest_moves_into_the_separate_base_with_the_dividends(self):
        cmp = self._result().dividend_separate_base

        assert cmp.dividends == Decimal("80000")
        assert cmp.interest == Decimal("40000")
        assert cmp.separate_dividend_base == Decimal("120000")
        assert cmp.separate_dividend_base_rounded == Decimal("120000")
        assert cmp.separate_dividend_gross_tax == Decimal("18000.00")

    def test_general_base_of_variant_b_keeps_no_foreign_interest(self):
        cmp = self._result().dividend_separate_base

        assert cmp.separate_general_base == Decimal("90000")
        assert cmp.separate_general_base_tax == Decimal("13500.00")

    def test_credit_for_the_interest_moves_with_it(self):
        cmp = self._result().dividend_separate_base

        # Both states are credited against the separate base ...
        assert cmp.separate_dividend_ftc == Decimal("16000.00")
        assert cmp.separate_dividend_net_tax == Decimal("2000.00")
        # ... and nothing is left to credit against the general one.
        assert cmp.separate_general_base_ftc == ZERO
        assert cmp.separate_general_base_net_tax == Decimal("13500.00")

    def test_totals_and_recommendation(self):
        result = self._result()
        cmp = result.dividend_separate_base

        assert cmp.general_base_total_tax == Decimal("24300")
        assert cmp.separate_base_total_tax == Decimal("15500")
        assert cmp.saving == Decimal("8800")
        assert cmp.recommended_mode == "separate"
        assert result.recommended_final_tax == Decimal("15500")

    def test_a_year_with_foreign_interest_only_still_gets_the_comparison(self):
        """Interest alone is foreign §8 income the election covers.

            foreign interest, no tax withheld   150 000
            variant A   100 000 x 15 % + 50 000 x 23 %   = 26 500
            variant B   150 000 x 15 %                   = 22 500
            saving                                       =  4 000
        """
        result = _liability(
            [_interest("CA", "150000")],
            config=CzTaxConfig(elevated_rate_threshold_czk=_LOW_THRESHOLD),
        )
        cmp = result.dividend_separate_base

        assert cmp is not None and cmp.available
        assert cmp.dividends == ZERO
        assert cmp.interest == Decimal("150000")
        assert cmp.general_base_total_tax == Decimal("26500")
        assert cmp.separate_base_total_tax == Decimal("22500")
        assert cmp.saving == Decimal("4000")
        assert cmp.recommended_mode == "separate"

    def test_line_items_show_the_interest_that_moved(self):
        line_items = self._result().to_line_items("CZK")

        assert line_items["sep_dividends_czk"] == Decimal("80000.00")
        assert line_items["sep_interest_czk"] == Decimal("40000.00")
        assert line_items["sep_dividend_base_czk"] == Decimal("120000.00")

    def test_texts_shown_to_the_taxpayer_name_the_interest(self):
        items = [_dividend("US", "80000", "12000"), _interest("CA", "40000", "4000")]
        cfg = CzTaxConfig(elevated_rate_threshold_czk=_LOW_THRESHOLD)
        ftc = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        netting = _netting("90000")
        liability = compute_tax_liability(
            Decimal("80000"), Decimal("40000"), netting, ftc, cfg, tax_year=2025)
        mapping = build_form_mapping(
            liability=liability, netting=netting, ftc_summary=ftc,
            taxable_dividends=Decimal("80000"), taxable_interest=Decimal("40000"),
            currency="CZK", items=items,
        )

        base_line = mapping.get_line("CZ_DAP_16A_SEPARATE_BASE")
        assert base_line.value == Decimal("120000.00")
        assert "úrok" in base_line.label.lower()

        section = mapping.get_section("CZ_FORM_DIVIDEND_SEPARATE_BASE")
        assert "úrok" in section.label.lower()
        assert any("úrok" in note.lower() for note in section.notes)

        advice = [n for n in liability.limitation_notes if "§16a" in n]
        assert advice and all("úrok" in n.lower() for n in advice)


# ---------------------------------------------------------------------------
# W26 — the §38f limit of the separate base uses the UNROUNDED base, and a
# difference that is rounding alone is not reported as a saving
# ---------------------------------------------------------------------------

class TestW26NoSavingFromRoundingAlone:
    """The §38f limit of a state is tax x (income of that state / tax base).
    The tax comes from the base rounded down to hundreds, the ratio from the
    base as it stands (Příloha č. 3, rows 324 and 325). The separate-base
    scenario divided by the ROUNDED base instead, which made a state's limit
    the full 15 % of its income, credited more than the general base would,
    and variant B came out a few crowns "cheaper" below the 23 % threshold,
    where both variants tax everything at 15 %.

    Both variants now divide by the base before rounding. That is the audit's
    proposal and the owner's decision of 2026-11-27: one convention for both,
    and the stricter of the two. It is NOT what the annex for the separate
    base says on its face - Příloha č. 4 (form 25 5405/P4), row 413, allows
    the tax paid abroad "maximálně do výše 15 % z částky uvedené na ř. 411",
    the flat rate on the income, which is what the old divisor gave. So the
    scenario credits a few crowns less than that annex would, never more.

    The example (statutory threshold, far away):

        foreign dividends, state US   6 030   withheld 904.50 (15 %)
        foreign interest,  state GB   4 020   nothing withheld
        §10 securities, net           2 000

    Variant A - everything in the general base:
        base    6 030 + 4 020 + 2 000 = 12 050  -> rounded down 12 000
        tax     12 000 x 15 %                             = 1 800.00
        §38f    US limit 1 800 x 6 030 / 12 050 = 900.75  -> credit 900.75
        tax     1 800.00 - 900.75 = 899.25  -> rounded up      900

    Variant B - the separate base is elected:
        separate base  6 030 + 4 020 = 10 050  -> rounded down 10 000
        tax            10 000 x 15 %                      = 1 500.00
        §38f    US limit 1 500 x 6 030 / 10 050 = 900.00  -> credit 900.00
        separate base after credit                        =   600.00
        general base   2 000 x 15 %                       =   300.00
        total          900.00                             ->      900

    Same tax either way. Dividing by the rounded 10 000 gave the US a limit
    of 1 500 x 6 030 / 10 000 = 904.50, the whole withholding was credited,
    variant B came to 895.50 -> 896 and the page advised the election for a
    "saving" of 4.
    """

    _ITEMS_ARGS = (("US", "6030", "904.50"), ("GB", "4020"))

    def _result(self, securities_net: str):
        return _liability(
            [_dividend(*self._ITEMS_ARGS[0]), _interest(*self._ITEMS_ARGS[1])],
            securities_net=securities_net,
        )

    def test_general_base_divides_by_the_base_before_rounding(self):
        result = self._result("2000")

        assert result.combined_taxable_base == Decimal("12050")
        assert result.combined_taxable_base_rounded == Decimal("12000")
        assert result.gross_czech_tax == Decimal("1800.00")
        assert result.final_creditable_ftc == Decimal("900.75")
        assert result.final_czech_tax_after_credit == Decimal("900")

    def test_separate_base_divides_by_the_base_before_rounding_too(self):
        cmp = self._result("2000").dividend_separate_base

        assert cmp.separate_dividend_base == Decimal("10050")
        assert cmp.separate_dividend_base_rounded == Decimal("10000")
        assert cmp.separate_dividend_gross_tax == Decimal("1500.00")
        assert cmp.separate_dividend_ftc == Decimal("900.00")
        assert cmp.separate_dividend_net_tax == Decimal("600.00")

    def test_both_variants_cost_the_same_below_the_threshold(self):
        result = self._result("2000")
        cmp = result.dividend_separate_base

        assert cmp.general_base_total_tax == Decimal("900")
        assert cmp.separate_base_total_tax == Decimal("900")
        assert cmp.saving == ZERO
        assert cmp.recommended_mode == "general"
        assert not any("DOPORUČENÍ" in n for n in result.limitation_notes)

    def test_two_bases_rounded_down_instead_of_one_is_not_a_saving(self):
        """Variant B rounds two bases down to hundreds where A rounds one, and
        that alone can make it a few crowns lower. Securities of 2 060 instead
        of 2 000 show it:

        Variant A:
            base    12 110 -> 12 100, tax 1 815.00
            §38f    US limit 1 815 x 6 030 / 12 110 = 903.75 -> credit 903.75
            tax     1 815.00 - 903.75 = 911.25  -> 912
        Variant B:
            separate base as above                    = 600.00
            general base 2 060 -> 2 000, x 15 %       = 300.00
            total                                     -> 900

        The 12 between them is rounding and nothing else: 110 of base lost to
        rounding instead of 10 (15 less tax), 3.75 less credit, 0.75 from the
        final rounding up. Every crown is taxed at 15 % in both variants, so
        there is nothing for the election to save and it is not advised.
        """
        result = self._result("2060")
        cmp = result.dividend_separate_base

        # The figures of each variant stay what was computed for it ...
        assert cmp.general_base_total_tax == Decimal("912")
        assert cmp.separate_base_total_tax == Decimal("900")
        # ... but the difference is not offered as a saving.
        assert cmp.saving == ZERO
        assert cmp.recommended_mode == "general"
        assert result.recommended_final_tax == Decimal("912")
        assert not any("DOPORUČENÍ" in n for n in result.limitation_notes)

        line_items = result.to_line_items("CZK")
        assert line_items["sep_recommended_separate"] == ZERO
        assert line_items["sep_saving_czk"] == ZERO

    def test_form_explains_a_rounding_difference_instead_of_advising(self):
        items = [_dividend(*self._ITEMS_ARGS[0]), _interest(*self._ITEMS_ARGS[1])]
        cfg = CzTaxConfig()
        ftc = evaluate_foreign_tax_credit(items, cfg, has_fx=True)
        netting = _netting("2060")
        liability = compute_tax_liability(
            Decimal("6030"), Decimal("4020"), netting, ftc, cfg, tax_year=2025)
        mapping = build_form_mapping(
            liability=liability, netting=netting, ftc_summary=ftc,
            taxable_dividends=Decimal("6030"), taxable_interest=Decimal("4020"),
            currency="CZK", items=items,
        )

        section = mapping.get_section("CZ_FORM_DIVIDEND_SEPARATE_BASE")
        assert mapping.get_line("CZ_DAP_16A_GENERAL_TOTAL").value == Decimal("912.00")
        assert mapping.get_line("CZ_DAP_16A_SEPARATE_TOTAL").value == Decimal("900.00")
        assert mapping.get_line("CZ_DAP_16A_SAVING").value == ZERO
        assert not any("DOPORUČENÍ" in n for n in section.notes)
        assert any("zaokrouhl" in n.lower() for n in section.notes)

    def test_a_real_saving_above_the_threshold_is_still_reported(self):
        """The guard must not swallow what the election is for.

            foreign dividends, state US   150 030   withheld 22 504.50 (15 %)
            23 % rate from 100 000

        Variant A:
            base    150 030 -> 150 000
            tax     100 000 x 15 % + 50 000 x 23 %       = 26 500.00
            §38f    US limit 26 500 x 150 030 / 150 030  -> credit 22 504.50
            tax     26 500.00 - 22 504.50 = 3 995.50     -> 3 996
        Variant B:
            separate base 150 030 -> 150 000, x 15 %     = 22 500.00
            §38f    US limit 22 500 x 150 030 / 150 030  -> credit 22 500.00
            total                                        -> 0
            saving  3 996
        """
        result = _liability(
            [_dividend("US", "150030", "22504.50")],
            config=CzTaxConfig(elevated_rate_threshold_czk=_LOW_THRESHOLD),
        )
        cmp = result.dividend_separate_base

        assert cmp.general_base_total_tax == Decimal("3996")
        assert cmp.separate_dividend_ftc == Decimal("22500.00")
        assert cmp.separate_base_total_tax == ZERO
        assert cmp.saving == Decimal("3996")
        assert cmp.recommended_mode == "separate"

    def test_a_difference_is_called_rounding_only_when_it_can_be_nothing_else(self):
        """The general base of the separate scenario does not carry a taxable
        §10 currency result (a separate, open finding of the same audit), so
        with one the two variants do not hold the same income and what is
        between them is not rounding. The comparison must not say it is.
        """
        items = [_dividend(*self._ITEMS_ARGS[0]), _interest(*self._ITEMS_ARGS[1])]
        cfg = CzTaxConfig()
        ftc = evaluate_foreign_tax_credit(items, cfg, has_fx=True)

        plain = compute_tax_liability(
            Decimal("6030"), Decimal("4020"), _netting("2000"), ftc, cfg,
            tax_year=2025)
        assert plain.dividend_separate_base.same_tax_before_rounding is True

        with_currency = _netting("2000")
        with_currency.currency.gains = Decimal("60000")
        with_currency.currency_exemption_enabled = True
        with_currency.currency_exemption_threshold = Decimal("50000")
        with_currency.compute_combined()
        assert with_currency.currency.net_taxable == Decimal("60000")
        mixed = compute_tax_liability(
            Decimal("6030"), Decimal("4020"), with_currency, ftc, cfg,
            tax_year=2025)
        assert mixed.dividend_separate_base.same_tax_before_rounding is False


# ---------------------------------------------------------------------------
# W27 — South Korea has a treaty cap of its own
# ---------------------------------------------------------------------------

class TestW27KoreaTreatyCap:
    """Korea was missing from the treaty caps, so tax withheld on a Korean
    dividend was credited up to the 15 % default. Article 10(2) of the 2018
    treaty (1/2020 Sb. m. s.) limits the source state to 5 % of the gross
    dividend - one rate, no split by size of holding - for income paid from
    1 January 2020.

        dividend, state KR   10 000   withheld 2 200 (22 %)
        creditable           10 000 x 5 %   =   500
        above the treaty     2 200 - 500    = 1 700   (to reclaim in Korea)

        tax       10 000 x 15 %             = 1 500
        credit    min(500, 1 500)           =   500
        tax after credit                    = 1 000

    With the default cap 1 500 was credited and the Czech tax came to 0.
    """

    def test_korea_is_in_the_treaty_table_at_five_percent(self):
        assert CzTaxConfig().country_credit_caps["KR"] == Decimal("0.05")

    def test_korean_withholding_is_credited_up_to_the_treaty_rate_only(self):
        item = _dividend("KR", "10000", "2200")
        summary = evaluate_foreign_tax_credit([item], CzTaxConfig(), has_fx=True)

        record = item.ftc_record
        assert record.configured_cap_rate == Decimal("0.05")
        assert record.cap_rate_defaulted is False
        assert record.actual_creditable_czk == Decimal("500.00")
        assert record.non_creditable_czk == Decimal("1700.00")
        assert summary.per_country["KR"].creditable_czk == Decimal("500.00")

    def test_czech_tax_on_a_korean_dividend(self):
        result = _liability([_dividend("KR", "10000", "2200")])

        assert result.gross_czech_tax == Decimal("1500.00")
        assert result.final_creditable_ftc == Decimal("500.00")
        assert result.treaty_excess_ftc == Decimal("1700.00")
        assert result.uncredited_ftc == ZERO
        assert result.final_czech_tax_after_credit == Decimal("1000")


# ---------------------------------------------------------------------------
# X27 — which income row a withholding row is linked to must not depend on
# the event ids, which are drawn afresh on every run
# ---------------------------------------------------------------------------

# Either order of the two income events' ids. The ids are random in a real
# run; fixing them both ways round makes "the id decided" visible as a test
# that passes one way and fails the other.
_ID_ORDERS = pytest.mark.parametrize("first_id, second_id", [(1, 2), (2, 1)])


class TestX27WithholdingLinkDoesNotDependOnEventIds:
    """A dividend and a payment in lieu of the same amount, on the same asset
    and day, each followed by its own tax row:

        4001  dividend            100.00
        4002  tax                  15.00
        4003  payment in lieu     100.00
        4004  tax                  15.00

    Row 4002 is unambiguous. Row 4004 follows BOTH income rows closely enough
    to count as "sequential" for either, both candidates score the same
    confidence and imply the same 15 % rate, and the choice fell to the last
    element of the key - the candidate's event id, a uuid4 that differs on
    every run. Whenever the dividend drew the larger id, both tax rows landed
    on it and the payment in lieu looked untaxed.
    """

    ASSET = uuid.UUID(int=77)
    DAY = "2025-06-16"

    def _income(self, tx_id, description, event_id):
        return CashFlowEvent(
            asset_internal_id=self.ASSET,
            event_date=self.DAY,
            event_type=FinancialEventType.DIVIDEND_CASH,
            event_id=uuid.UUID(int=event_id),
            gross_amount_foreign_currency=Decimal("100.00"),
            local_currency="USD",
            ibkr_transaction_id=tx_id,
            ibkr_activity_description=description,
        )

    def _tax(self, tx_id):
        return WithholdingTaxEvent(
            asset_internal_id=self.ASSET,
            event_date=self.DAY,
            source_country_code="US",
            gross_amount_foreign_currency=Decimal("15.00"),
            local_currency="USD",
            ibkr_transaction_id=tx_id,
            ibkr_activity_description="ABC(US0000000001) CASH DIVIDEND - US TAX",
        )

    def _rows(self, first_id, second_id, tx_ids):
        dividend = self._income(
            tx_ids[0], "ABC(US0000000001) CASH DIVIDEND USD 1.00 PER SHARE",
            first_id)
        tax_on_dividend = self._tax(tx_ids[1])
        in_lieu = self._income(
            tx_ids[2], "ABC(US0000000001) PAYMENT IN LIEU OF DIVIDEND",
            second_id)
        tax_on_in_lieu = self._tax(tx_ids[3])
        WithholdingTaxLinker().link_withholding_tax_events(
            [dividend, tax_on_dividend, in_lieu, tax_on_in_lieu])
        return dividend, tax_on_dividend, in_lieu, tax_on_in_lieu

    @_ID_ORDERS
    def test_each_tax_row_goes_to_the_income_row_it_follows(self, first_id, second_id):
        dividend, tax_on_dividend, in_lieu, tax_on_in_lieu = self._rows(
            first_id, second_id, ("4001", "4002", "4003", "4004"))

        assert tax_on_dividend.taxed_income_event_id == dividend.event_id
        assert tax_on_in_lieu.taxed_income_event_id == in_lieu.event_id

    @_ID_ORDERS
    def test_nearest_transaction_id_decides_outside_the_sequential_window(
            self, first_id, second_id):
        """Ten apart is too far to count as "sequential", so all four
        pairings score the same; the nearer TransactionID still tells them
        apart (10 against 3 990)."""
        dividend, tax_on_dividend, in_lieu, tax_on_in_lieu = self._rows(
            first_id, second_id, ("1000", "1010", "5000", "5010"))

        assert tax_on_dividend.taxed_income_event_id == dividend.event_id
        assert tax_on_in_lieu.taxed_income_event_id == in_lieu.event_id

    @_ID_ORDERS
    def test_a_row_booked_before_the_tax_row_wins_over_a_nearer_later_one(
            self, first_id, second_id):
        """A tax row follows the income row it belongs to.

            1000  dividend
            1020  tax        20 after the dividend, 10 BEFORE the in-lieu row
            1030  payment in lieu
            1040  tax

        By plain distance row 1020 would go to the payment in lieu, which was
        not booked yet when the tax was."""
        dividend, tax_on_dividend, in_lieu, tax_on_in_lieu = self._rows(
            first_id, second_id, ("1000", "1020", "1030", "1040"))

        assert tax_on_dividend.taxed_income_event_id == dividend.event_id
        assert tax_on_in_lieu.taxed_income_event_id == in_lieu.event_id

    def test_rows_without_transaction_ids_link_the_same_way_on_every_run(self):
        """With no TransactionID there is nothing to tell the two income rows
        apart, but the answer still has to be the same answer every run."""
        def linked_positions(first_id, second_id):
            dividend, tax_a, in_lieu, tax_b = self._rows(
                first_id, second_id, (None, None, None, None))
            position = {dividend.event_id: "dividend", in_lieu.event_id: "in lieu"}
            return (position[tax_a.taxed_income_event_id],
                    position[tax_b.taxed_income_event_id])

        assert linked_positions(1, 2) == linked_positions(2, 1)


# ---------------------------------------------------------------------------
# X20 — the summary carries the FINAL credit of each state, not only the
# preliminary per-item caps
# ---------------------------------------------------------------------------

class _NoAssets:
    def get_asset_by_id(self, _id):
        return None


def _aggregate_dividends(rows):
    """Run the real aggregator over dividend rows ``(state, amount, withheld)``.

    Every row is in CZK, so no exchange rate is looked up; ``withheld`` is
    None for a row nothing was withheld on (a negative amount is a reversal).
    """
    events = []
    for number, (country, amount, withheld) in enumerate(rows):
        asset = uuid.UUID(int=100 + number)
        dividend = CashFlowEvent(
            asset_internal_id=asset,
            event_date="2025-06-16",
            event_type=FinancialEventType.DIVIDEND_CASH,
            gross_amount_foreign_currency=Decimal(amount),
            local_currency="CZK",
            ibkr_transaction_id=str(7000 + 10 * number),
            source_country_code=country,
        )
        events.append(dividend)
        if withheld is not None:
            events.append(WithholdingTaxEvent(
                asset_internal_id=asset,
                event_date="2025-06-16",
                gross_amount_foreign_currency=Decimal(withheld),
                local_currency="CZK",
                ibkr_transaction_id=str(7001 + 10 * number),
                taxed_income_event_id=dividend.event_id,
                source_country_code=country,
            ))
    cfg = CzTaxConfig()
    aggregator = CzechTaxAggregator(
        config=cfg,
        fx_converter=CzCurrencyConverter(
            provider=GoldenCnbProvider(), policy=cfg.fx_policy),
    )
    return aggregator.aggregate([], events, _NoAssets(), 2025)


def _credited_rows(result):
    ftc = result.sections["cz_ftc_summary"].line_items
    return {k: v for k, v in ftc.items() if k.endswith("_credited_czk")}


class TestX20FinalCreditPerState:
    """Příloha č. 3 is filled in per state, and what a state is finally
    credited is decided by the §38f limits. The summary listed only the
    preliminary figure per state (the per-item treaty caps added up), so the
    per-state amounts on the XLSX Summary sheet did not add up to the credit
    on the form.

    The example (all rows in CZK, so no exchange rate is involved):

        dividend, state US   6 030   withheld 904.50 (15 %)
        dividend, state DE   3 015   withheld 795.21 (26.375 %)
        dividend, state GB   1 005   nothing withheld

        preliminary  US 904.50 | DE 3 015 x 15 % = 452.25 | GB 0   = 1 356.75

        base    10 050 -> rounded down 10 000, tax 1 500.00
        §38f    US limit 1 500 x 6 030 / 10 050 = 900.00  -> credited 900.00
                DE limit 1 500 x 3 015 / 10 050 = 450.00  -> credited 450.00
                GB                                        -> credited   0.00
        credit  900.00 + 450.00                           = 1 350.00
        tax     1 500.00 - 1 350.00                       =   150
    """

    def _result(self):
        return _aggregate_dividends((
            ("US", "6030", "904.50"),
            ("DE", "3015", "795.21"),
            ("GB", "1005", None),
        ))

    def test_the_example_is_what_the_arithmetic_says(self):
        liability = self._result().sections["cz_tax_liability"].line_items

        assert liability["gross_czech_tax_czk"] == Decimal("1500.00")
        assert liability["preliminary_ftc_czk"] == Decimal("1356.75")
        assert liability["final_creditable_ftc_czk"] == Decimal("1350.00")
        assert liability["final_czech_tax_after_credit_czk"] == Decimal("150.00")

    def test_summary_has_one_final_credited_row_per_state(self):
        ftc = self._result().sections["cz_ftc_summary"].line_items

        assert ftc["ftc_us_credited_czk"] == Decimal("900.00")
        assert ftc["ftc_de_credited_czk"] == Decimal("450.00")
        # A state nothing was withheld in still gets its row: one per state.
        assert ftc["ftc_gb_credited_czk"] == Decimal("0.00")
        # The preliminary figures stay, under their own name.
        assert ftc["ftc_us_creditable_czk"] == Decimal("904.50")
        assert ftc["ftc_de_creditable_czk"] == Decimal("452.25")

    def test_final_rows_add_up_to_the_credit_on_the_form(self):
        result = self._result()
        liability = result.sections["cz_tax_liability"].line_items

        credited = _credited_rows(result)
        assert len(credited) == 3
        assert sum(credited.values()) == liability["final_creditable_ftc_czk"]

    def test_xlsx_summary_sheet_shows_the_final_rows(self):
        from openpyxl import load_workbook

        buffer = io.BytesIO()
        export_cz_to_xlsx(self._result(), buffer)
        buffer.seek(0)
        sheet = load_workbook(buffer, read_only=True)["Summary"]
        cells = {row[0]: row[1] for row in sheet.iter_rows(values_only=True)
                 if row and row[0] and str(row[0]).startswith(("ftc_", "final_"))}

        assert cells["ftc_us_credited_czk"] == 900.00
        assert cells["ftc_de_credited_czk"] == 450.00
        assert cells["ftc_gb_credited_czk"] == 0
        assert (cells["ftc_us_credited_czk"] + cells["ftc_de_credited_czk"]
                + cells["ftc_gb_credited_czk"]) == cells["final_creditable_ftc_czk"]

    def test_rows_add_up_to_the_heller_when_the_credits_do_not_end_on_one(self):
        """Converted withholding seldom ends on a whole heller, and rows
        rounded one by one then miss the total.

            three states, each: dividend 1 000, withheld 100.005
            tax     3 000 x 15 % = 450.00; each state's limit is 150.00
            credit  3 x 100.005 = 300.015                 -> printed 300.02

        Printed on their own the three rows would be 100.01 each, 300.03 in
        all. Two of them carry 100.01 and one 100.00.
        """
        result = _aggregate_dividends((
            ("US", "1000", "100.005"),
            ("CA", "1000", "100.005"),
            ("GB", "1000", "100.005"),
        ))
        liability = result.sections["cz_tax_liability"].line_items
        credited = _credited_rows(result)

        assert liability["final_creditable_ftc_czk"] == Decimal("300.02")
        assert sorted(credited.values()) == [
            Decimal("100.00"), Decimal("100.01"), Decimal("100.01")]
        assert sum(credited.values()) == liability["final_creditable_ftc_czk"]

    def test_a_state_credited_nothing_shows_zero_on_the_form_too(self):
        """A state whose income nets to nothing gets no credit although its
        per-item caps add up to something. Every place that shows the state
        has to say the same.

            state US   dividend 1 000, withheld 150; reversal -1 200
                       income -200 -> nothing to credit (preliminary 150)
            state DE   dividend 10 000, withheld 1 500
            base    9 800, tax 1 470.00
            §38f    DE limit 1 470 x min(10 000 / 9 800, 1) = 1 470.00
            credit  1 470.00
        """
        result = _aggregate_dividends((
            ("US", "1000", "150"),
            ("US", "-1200", None),
            ("DE", "10000", "1500"),
        ))
        ftc = result.sections["cz_ftc_summary"].line_items
        mapping = result.country_result["form_mapping"]

        assert ftc["ftc_us_creditable_czk"] == Decimal("150.00")
        assert ftc["ftc_us_credited_czk"] == Decimal("0.00")
        assert ftc["ftc_de_credited_czk"] == Decimal("1470.00")

        assert mapping.get_line("CZ_DAP_FTC_COUNTRY_US").value == Decimal("0.00")
        assert mapping.get_line("CZ_DAP_FTC_COUNTRY_DE").value == Decimal("1470.00")
        assert mapping.get_line("CZ_DAP_FTC_FINAL").value == Decimal("1470.00")


# ---------------------------------------------------------------------------
# The fixes above change what the same statements compute to, so a run stored
# before them must not pass for a current one
# ---------------------------------------------------------------------------

class TestRunsFromBeforeThisBatchCountAsOlder:
    def test_a_run_stamped_with_the_previous_version_is_flagged(
            self, tmp_path, monkeypatch):
        services = pytest.importorskip("src.webapp.services")
        from tests.test_audit_2026_11_fixes import _seed_run_meta

        service = services.RunService(
            data_dir=tmp_path / "data", runs_dir=tmp_path / "runs")
        try:
            # "v9" was current on main when X04, W26, W27, X27 and X20 were
            # still open (the options and corporate-actions batch landed
            # first), so a run stamped v9 predates these corrections.
            _seed_run_meta(service, "2025-before", 2025,
                           "2026-11-20T10:00:00+00:00", fingerprint_version="v9")
            monkeypatch.setattr(service, "_today", lambda: date(2026, 11, 27))

            meta = service.get_run("2025-before")
            assert meta.get("computed_by_older_version") is True
        finally:
            service.runner.shutdown(wait=False)
