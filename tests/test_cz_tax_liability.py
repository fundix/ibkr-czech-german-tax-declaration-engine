# tests/test_cz_tax_liability.py
"""
Tests for Czech tax liability computation (§16 rate application + §38f FTC finalization).

Covers:
1. Dividends only → tax at base rate
2. §10 net securities → included in tax base
3. Securities + options → combined tax base
4. Preliminary FTC < Czech tax on foreign income → full FTC usable
5. Preliminary FTC > Czech tax on foreign income → FTC capped
6. Zero taxable base → zero Czech tax, no crash
7. Config-driven higher-rate threshold
8. Summary contains all liability totals
9. Exporters still work with updated TaxResult
"""
import uuid
from decimal import Decimal
from typing import List

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.foreign_tax_credit import CzForeignTaxCreditSummary, CzCountryCreditAggregate
from src.countries.cz.loss_offsetting import CzLossOffsettingResult, CzSectionNetting
from src.countries.cz.tax_liability import CzTaxLiabilitySummary, compute_tax_liability

ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _netting(sec_gains=ZERO, sec_losses=ZERO, opt_gains=ZERO, opt_losses=ZERO):
    n = CzLossOffsettingResult()
    n.securities.taxable_gains = sec_gains
    n.securities.taxable_losses = sec_losses
    n.options.taxable_gains = opt_gains
    n.options.taxable_losses = opt_losses
    n.compute_combined()
    return n


def _ftc(paid=ZERO, creditable=ZERO, foreign_income=ZERO):
    s = CzForeignTaxCreditSummary()
    s.foreign_tax_paid_total_czk = paid
    s.foreign_tax_creditable_total_czk = creditable
    s.foreign_income_total_czk = foreign_income
    return s


# =========================================================================
# Test 1: Dividends only → base rate
# =========================================================================

class TestDividendsOnly:
    def test_dividends_at_15_percent(self):
        cfg = CzTaxConfig()
        netting = _netting()
        ftc = _ftc()

        result = compute_tax_liability(
            taxable_dividends=Decimal("10000"),
            taxable_interest=ZERO,
            netting=netting,
            ftc_summary=ftc,
            config=cfg,
        )

        assert result.taxable_dividends == Decimal("10000")
        assert result.combined_taxable_base == Decimal("10000")
        assert result.gross_czech_tax == Decimal("1500.00")
        assert result.final_czech_tax_after_credit == Decimal("1500.00")


# =========================================================================
# Test 2: §10 net securities in tax base
# =========================================================================

class TestSecuritiesNet:
    def test_securities_gain_in_base(self):
        cfg = CzTaxConfig()
        netting = _netting(sec_gains=Decimal("50000"), sec_losses=Decimal("10000"))
        ftc = _ftc()

        result = compute_tax_liability(
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            netting=netting,
            ftc_summary=ftc,
            config=cfg,
        )

        assert result.taxable_securities_net == Decimal("40000")
        assert result.combined_taxable_base == Decimal("40000")
        assert result.gross_czech_tax == Decimal("6000.00")

    def test_securities_net_loss_floored_to_zero(self):
        cfg = CzTaxConfig()
        netting = _netting(sec_gains=Decimal("5000"), sec_losses=Decimal("20000"))
        ftc = _ftc()

        result = compute_tax_liability(
            taxable_dividends=ZERO,
            taxable_interest=ZERO,
            netting=netting,
            ftc_summary=ftc,
            config=cfg,
        )

        assert result.taxable_securities_net == ZERO
        assert result.combined_taxable_base == ZERO
        assert result.gross_czech_tax == ZERO
        assert any("floored to 0" in n for n in result.limitation_notes)


# =========================================================================
# Test 3: Securities + options combined
# =========================================================================

class TestCombined:
    def test_combined_base(self):
        cfg = CzTaxConfig()
        netting = _netting(
            sec_gains=Decimal("30000"), sec_losses=Decimal("5000"),
            opt_gains=Decimal("10000"), opt_losses=Decimal("2000"),
        )
        ftc = _ftc()

        result = compute_tax_liability(
            taxable_dividends=Decimal("5000"),
            taxable_interest=Decimal("1000"),
            netting=netting,
            ftc_summary=ftc,
            config=cfg,
        )

        # 5000 + 1000 + 25000 + 8000 = 39000
        assert result.combined_taxable_base == Decimal("39000")
        assert result.gross_czech_tax == Decimal("5850.00")


# =========================================================================
# Test 4: FTC lower than CZ tax on foreign income → full usable
# =========================================================================

class TestFtcFullUsable:
    def test_full_credit(self):
        cfg = CzTaxConfig()
        netting = _netting()
        # Dividend 10000, WHT paid 1500, all creditable (within 15% cap)
        ftc = _ftc(paid=Decimal("1500"), creditable=Decimal("1500"),
                    foreign_income=Decimal("10000"))

        result = compute_tax_liability(
            taxable_dividends=Decimal("10000"),
            taxable_interest=ZERO,
            netting=netting,
            ftc_summary=ftc,
            config=cfg,
        )

        # CZ tax = 10000 * 0.15 = 1500
        # CZ tax on foreign = 1500 (100% foreign)
        # FTC = min(1500, 1500) = 1500
        assert result.gross_czech_tax == Decimal("1500.00")
        assert result.czech_tax_on_foreign_income == Decimal("1500.00")
        assert result.final_creditable_ftc == Decimal("1500.00")
        assert result.final_czech_tax_after_credit == ZERO


# =========================================================================
# Test 5: FTC higher than CZ tax on foreign income → capped
# =========================================================================

class TestFtcCapped:
    def test_ftc_capped_by_cz_tax(self):
        cfg = CzTaxConfig()
        # Mix: 50000 dividends + 50000 securities = 100000 total
        netting = _netting(sec_gains=Decimal("50000"))
        ftc = _ftc(paid=Decimal("10000"), creditable=Decimal("7500"),
                    foreign_income=Decimal("50000"))

        result = compute_tax_liability(
            taxable_dividends=Decimal("50000"),
            taxable_interest=ZERO,
            netting=netting,
            ftc_summary=ftc,
            config=cfg,
        )

        # CZ tax = 100000 * 0.15 = 15000
        # Foreign ratio = 50000 / 100000 = 0.5
        # CZ tax on foreign = 15000 * 0.5 = 7500
        # FTC = min(7500, 7500) = 7500
        assert result.gross_czech_tax == Decimal("15000.00")
        assert result.czech_tax_on_foreign_income == Decimal("7500.00")
        assert result.final_creditable_ftc == Decimal("7500.00")
        assert result.final_czech_tax_after_credit == Decimal("7500.00")

    def test_ftc_capped_when_domestic_dominant(self):
        cfg = CzTaxConfig()
        # 10000 dividends (foreign) + 90000 securities (domestic) = 100000
        netting = _netting(sec_gains=Decimal("90000"))
        ftc = _ftc(paid=Decimal("3000"), creditable=Decimal("1500"),
                    foreign_income=Decimal("10000"))

        result = compute_tax_liability(
            taxable_dividends=Decimal("10000"),
            taxable_interest=ZERO,
            netting=netting,
            ftc_summary=ftc,
            config=cfg,
        )

        # CZ tax = 100000 * 0.15 = 15000
        # Foreign ratio = 10000 / 100000 = 0.1
        # CZ tax on foreign = 15000 * 0.1 = 1500
        # FTC = min(1500, 1500) = 1500
        assert result.czech_tax_on_foreign_income == Decimal("1500.00")
        assert result.final_creditable_ftc == Decimal("1500.00")
        assert result.non_creditable_ftc == Decimal("1500.00")  # 3000 paid - 1500 credited


# =========================================================================
# Test 5b: what "not credited" is made of — ř. 329 vs. money to reclaim
# =========================================================================

class TestNonCreditableSplit:
    """§38f odst. 5 counts foreign tax only up to the treaty rate.

    Anything withheld above it never enters ř. 323, so it cannot leave
    through ř. 329 either — it is a refund claim against the source state,
    not a Czech carry-forward under §24 odst. 2 písm. ch).
    """

    def test_treaty_excess_is_not_line_329(self):
        cfg = CzTaxConfig()
        # NL dividend 1000 gross, 150 withheld, treaty rate 10% → 100 eligible.
        # Czech tax is 150 and all income is foreign, so the whole 100 credits:
        # nothing is lost to §38f, and ř. 329 must be zero.
        ftc = _ftc(paid=Decimal("150"), creditable=Decimal("100"),
                   foreign_income=Decimal("1000"))
        result = compute_tax_liability(
            taxable_dividends=Decimal("1000"), taxable_interest=ZERO,
            netting=_netting(), ftc_summary=ftc, config=cfg,
        )

        assert result.preliminary_ftc == Decimal("100")       # ř. 323
        assert result.final_creditable_ftc == Decimal("100")  # ř. 328
        assert result.uncredited_ftc == ZERO                  # ř. 329 = 323 - 328
        # The 50 over the treaty rate is the Dutch tax office's to refund.
        assert result.treaty_excess_ftc == Decimal("50")

    def test_capped_credit_is_line_329(self):
        cfg = CzTaxConfig()
        # Same 150 withheld, but domestic income dominates, so §38f/1 lets
        # only part of the treaty-eligible 100 through. That shortfall IS 329.
        ftc = _ftc(paid=Decimal("150"), creditable=Decimal("100"),
                   foreign_income=Decimal("1000"))
        result = compute_tax_liability(
            taxable_dividends=Decimal("1000"), taxable_interest=ZERO,
            netting=_netting(sec_gains=Decimal("9000")), ftc_summary=ftc,
            config=cfg,
        )

        # CZ tax 1500, foreign ratio 0.1 → 150 head-room, credit = min(100,150)
        assert result.final_creditable_ftc == Decimal("100")
        assert result.uncredited_ftc == ZERO
        assert result.treaty_excess_ftc == Decimal("50")

    def test_zero_czech_tax_loses_the_whole_credit(self):
        cfg = CzTaxConfig()
        ftc = _ftc(paid=Decimal("150"), creditable=Decimal("100"),
                   foreign_income=ZERO)
        result = compute_tax_liability(ZERO, ZERO, _netting(), ftc, cfg)

        # No Czech tax to credit against: the full treaty-eligible 100 is lost
        # to §38f and belongs on ř. 329; the 50 excess still does not.
        assert result.final_creditable_ftc == ZERO
        assert result.uncredited_ftc == Decimal("100")
        assert result.treaty_excess_ftc == Decimal("50")

    def test_the_two_parts_still_add_up_to_the_total(self):
        cfg = CzTaxConfig()
        ftc = _ftc(paid=Decimal("3000"), creditable=Decimal("1500"),
                   foreign_income=Decimal("10000"))
        result = compute_tax_liability(
            taxable_dividends=Decimal("10000"), taxable_interest=ZERO,
            netting=_netting(sec_gains=Decimal("90000")), ftc_summary=ftc,
            config=cfg,
        )
        assert (result.treaty_excess_ftc + result.uncredited_ftc
                == result.non_creditable_ftc)

    def test_per_state_credits_sum_to_the_final_credit(self):
        """The separate Příloha 3 sheets must reconcile against ř. 328.

        The per-country FTC aggregate holds the PRELIMINARY per-item cap; what a
        state actually gets is decided later by §38f/8, so the sheets could add
        up to more than the total they belong to.
        """
        from src.countries.cz.foreign_tax_credit import CzCountryCreditAggregate

        cfg = CzTaxConfig()
        ftc = _ftc(paid=Decimal("235.12"), creditable=Decimal("217.43"),
                   foreign_income=Decimal("3311.40"))
        ftc.per_country = {
            "US": CzCountryCreditAggregate(
                country="US", gross_income_czk=Decimal("3000"),
                foreign_tax_paid_czk=Decimal("187.16"),
                creditable_czk=Decimal("169.47")),
            "IE": CzCountryCreditAggregate(
                country="IE", gross_income_czk=Decimal("311.40"),
                foreign_tax_paid_czk=Decimal("47.96"),
                creditable_czk=Decimal("47.96")),
        }
        result = compute_tax_liability(
            taxable_dividends=Decimal("3311.40"), taxable_interest=ZERO,
            netting=_netting(), ftc_summary=ftc, config=cfg,
        )

        assert sum(result.per_state_credit.values()) == result.final_creditable_ftc
        # IE is the state the per-state cap bit into, not US.
        assert result.per_state_credit["US"] == Decimal("169.47")
        assert result.per_state_credit["IE"] < Decimal("47.96")

    def test_the_aggregate_cap_scales_the_states_down_exactly(self):
        """When §38f/1 bites on top of §38f/8 the states are scaled to match —
        rounding residue included, or the sheets drift by a heller."""
        from src.countries.cz.foreign_tax_credit import CzCountryCreditAggregate

        cfg = CzTaxConfig()
        # Domestic income dominates, so CZ tax on foreign income is tiny and
        # the aggregate cap binds well below the sum of the per-state caps.
        ftc = _ftc(paid=Decimal("900"), creditable=Decimal("900"),
                   foreign_income=Decimal("3000"))
        ftc.per_country = {
            c: CzCountryCreditAggregate(
                country=c, gross_income_czk=Decimal("1000"),
                foreign_tax_paid_czk=Decimal("300"),
                creditable_czk=Decimal("300"))
            for c in ("US", "IE", "DE")
        }
        result = compute_tax_liability(
            taxable_dividends=Decimal("3000"), taxable_interest=ZERO,
            netting=_netting(sec_gains=Decimal("97000")), ftc_summary=ftc,
            config=cfg,
        )

        assert result.final_creditable_ftc < Decimal("900")
        assert sum(result.per_state_credit.values()) == result.final_creditable_ftc
        assert all(v >= ZERO for v in result.per_state_credit.values())

    def test_both_causes_at_once(self):
        """The shape the real 2026 run hit, where ř. 329 read 18.94.

        There ř. 323 was 217.43 and ř. 328 was 216.18, so the line overstated
        itself by the 17.69 withheld above treaty rates. Both causes act here
        too: a per-state §38f/8 cap eats part of the eligible credit, and
        17.69 was over-withheld. Only the first belongs on ř. 329.
        """
        from src.countries.cz.foreign_tax_credit import CzCountryCreditAggregate

        cfg = CzTaxConfig()
        ftc = _ftc(paid=Decimal("235.12"), creditable=Decimal("217.43"),
                   foreign_income=Decimal("3311.40"))
        ftc.per_country = {
            # Head-room to spare: cap 450, so all of it credits.
            "US": CzCountryCreditAggregate(
                country="US", gross_income_czk=Decimal("3000"),
                foreign_tax_paid_czk=Decimal("187.16"),
                creditable_czk=Decimal("169.47")),
            # Little income, lots of tax: cap 46.71 bites into 47.96.
            "IE": CzCountryCreditAggregate(
                country="IE", gross_income_czk=Decimal("311.40"),
                foreign_tax_paid_czk=Decimal("47.96"),
                creditable_czk=Decimal("47.96")),
        }
        result = compute_tax_liability(
            taxable_dividends=Decimal("3311.40"), taxable_interest=ZERO,
            netting=_netting(), ftc_summary=ftc, config=cfg,
        )
        # Base rounds to 3300 (§16 odst. 2), so gross tax is 495.00 and the IE
        # per-state cap lands at 46.55, taking 1.41 off the eligible 217.43.
        assert result.preliminary_ftc == Decimal("217.43")      # ř. 323
        assert result.final_creditable_ftc == Decimal("216.02")  # ř. 328
        assert result.uncredited_ftc == Decimal("1.41")          # ř. 329 = 323 - 328
        # The old formula would have put 235.12 - 216.02 = 19.10 on ř. 329,
        # smuggling the over-withheld 17.69 into a Czech carry-forward.
        assert result.treaty_excess_ftc == Decimal("17.69")      # reclaim abroad
        assert result.non_creditable_ftc == Decimal("19.10")


# =========================================================================
# Test 6: Zero taxable base
# =========================================================================

class TestZeroBase:
    def test_zero_everything(self):
        cfg = CzTaxConfig()
        result = compute_tax_liability(ZERO, ZERO, _netting(), _ftc(), cfg)

        assert result.combined_taxable_base == ZERO
        assert result.gross_czech_tax == ZERO
        assert result.final_czech_tax_after_credit == ZERO
        assert result.final_creditable_ftc == ZERO

    def test_all_exempt_no_crash(self):
        """If all items were exempt, bases are zero."""
        cfg = CzTaxConfig()
        netting = _netting()  # all zeros
        ftc = _ftc(paid=Decimal("100"), creditable=Decimal("100"),
                    foreign_income=ZERO)

        result = compute_tax_liability(ZERO, ZERO, netting, ftc, cfg)
        assert result.gross_czech_tax == ZERO
        # FTC: CZ tax on foreign = 0 → credit = 0
        assert result.final_creditable_ftc == ZERO
        assert result.non_creditable_ftc == Decimal("100")


# =========================================================================
# Test 7: Higher rate threshold
# =========================================================================

class TestHigherRate:
    def test_above_threshold(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("100000"))
        netting = _netting(sec_gains=Decimal("150000"))
        ftc = _ftc()

        result = compute_tax_liability(ZERO, ZERO, netting, ftc, cfg)

        assert result.combined_taxable_base == Decimal("150000")
        assert result.base_for_base_rate == Decimal("100000")
        assert result.base_for_elevated_rate == Decimal("50000")
        assert result.tax_at_base_rate == Decimal("15000.00")
        assert result.tax_at_elevated_rate == Decimal("11500.00")
        assert result.gross_czech_tax == Decimal("26500.00")

    def test_below_threshold_only_base_rate(self):
        cfg = CzTaxConfig(elevated_rate_threshold_czk=Decimal("2000000"))
        netting = _netting(sec_gains=Decimal("100000"))
        ftc = _ftc()

        result = compute_tax_liability(ZERO, ZERO, netting, ftc, cfg)

        assert result.base_for_base_rate == Decimal("100000")
        assert result.base_for_elevated_rate == ZERO
        assert result.tax_at_elevated_rate == ZERO
        assert result.gross_czech_tax == Decimal("15000.00")

    def test_custom_rates(self):
        cfg = CzTaxConfig(
            base_tax_rate=Decimal("0.20"),
            elevated_tax_rate=Decimal("0.30"),
            elevated_rate_threshold_czk=Decimal("50000"),
        )
        netting = _netting(sec_gains=Decimal("80000"))

        result = compute_tax_liability(ZERO, ZERO, netting, _ftc(), cfg)

        assert result.tax_at_base_rate == Decimal("10000.00")   # 50000 * 0.20
        assert result.tax_at_elevated_rate == Decimal("9000.00")  # 30000 * 0.30
        assert result.gross_czech_tax == Decimal("19000.00")


# =========================================================================
# Test 8: Summary contains all liability totals
# =========================================================================

class TestSummaryStructure:
    def test_line_items_keys(self):
        cfg = CzTaxConfig()
        netting = _netting(sec_gains=Decimal("50000"))
        ftc = _ftc(paid=Decimal("1000"), creditable=Decimal("1000"),
                    foreign_income=Decimal("10000"))

        result = compute_tax_liability(
            Decimal("10000"), Decimal("5000"), netting, ftc, cfg,
        )
        li = result.to_line_items("CZK")

        expected_keys = [
            "taxable_dividends_czk", "taxable_interest_czk",
            "taxable_securities_net_czk", "taxable_options_net_czk",
            "combined_taxable_base_czk",
            "base_for_base_rate_czk", "tax_at_base_rate_czk",
            "base_for_elevated_rate_czk", "tax_at_elevated_rate_czk",
            "gross_czech_tax_czk",
            "foreign_income_total_czk", "preliminary_ftc_czk",
            "czech_tax_on_foreign_income_czk",
            "final_creditable_ftc_czk", "non_creditable_ftc_czk",
            "final_czech_tax_after_credit_czk",
        ]
        for key in expected_keys:
            assert key in li, f"Missing key: {key}"

    def test_limitation_notes_present(self):
        cfg = CzTaxConfig()
        result = compute_tax_liability(Decimal("10000"), ZERO, _netting(), _ftc(), cfg)
        assert any("LIMITATION" in n for n in result.limitation_notes)


# =========================================================================
# Test 9: Exporters still work
# =========================================================================

class TestExporterCompatibility:
    def test_json_export_with_liability(self):
        import json
        from src.countries.cz.exporters.json_exporter import export_cz_to_json
        from src.countries.cz.plugin import CzechTaxAggregator, CzechTaxClassifier
        from src.identification.asset_resolver import AssetResolver
        from src.classification.asset_classifier import AssetClassifier
        from src.domain.events import CashFlowEvent
        from src.domain.enums import FinancialEventType

        class D(AssetClassifier):
            def __init__(self): super().__init__(cache_file_path="d.json")
            def save_classifications(self): pass
        resolver = AssetResolver(asset_classifier=D())

        div = CashFlowEvent(
            asset_internal_id=uuid.uuid4(),
            event_date="2025-06-15",
            event_type=FinancialEventType.DIVIDEND_CASH,
            gross_amount_foreign_currency=Decimal("100"),
            local_currency="EUR",
            gross_amount_eur=Decimal("100"),
        )

        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        aggregator = CzechTaxAggregator(config=cfg)
        result = aggregator.aggregate([], [div], resolver, 2025)

        # JSON export should not crash and should include liability section
        json_str = export_cz_to_json(result)
        data = json.loads(json_str)
        assert "cz_tax_liability" in data["sections"]
        li = data["sections"]["cz_tax_liability"]["line_items"]
        assert "gross_czech_tax_eur" in li
        assert "final_czech_tax_after_credit_eur" in li

    def test_xlsx_export_with_liability(self):
        import io
        from src.countries.cz.exporters.xlsx_exporter import export_cz_to_xlsx
        from src.countries.cz.plugin import CzechTaxAggregator
        from src.identification.asset_resolver import AssetResolver
        from src.classification.asset_classifier import AssetClassifier

        class D(AssetClassifier):
            def __init__(self): super().__init__(cache_file_path="d.json")
            def save_classifications(self): pass
        resolver = AssetResolver(asset_classifier=D())

        cfg = CzTaxConfig(annual_exempt_limit_enabled=False)
        aggregator = CzechTaxAggregator(config=cfg)
        result = aggregator.aggregate([], [], resolver, 2025)

        buf = io.BytesIO()
        export_cz_to_xlsx(result, buf)
        buf.seek(0)
        assert buf.read(4) == b"PK\x03\x04"  # valid ZIP
