# tests/test_time_test_deadline.py
"""
Pure §4/1/w deadline helper — single source of the holding-period
arithmetic shared by the in-place evaluator and the portfolio countdown.
"""
import datetime

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.time_test import time_test_deadline


CFG = CzTaxConfig()


class TestStandardThreeYearTest:
    def test_plain_anniversary(self):
        assert time_test_deadline(datetime.date(2024, 3, 5), CFG) == datetime.date(2027, 3, 5)

    def test_feb_29_clamps_to_feb_28(self):
        # 2024-02-29 + 3y → 2027 has no Feb 29 → period ends on the last
        # day of the month (§33 daňového řádu)
        assert time_test_deadline(datetime.date(2024, 2, 29), CFG) == datetime.date(2027, 2, 28)

    def test_disposal_on_deadline_not_exempt_semantics(self):
        # The evaluator exempts only disposals strictly AFTER the deadline —
        # document the boundary here so the portfolio "osvobozeno od" date
        # (deadline + 1 day) stays consistent with evaluate_time_test.
        acq = datetime.date(2022, 6, 15)
        deadline = time_test_deadline(acq, CFG)
        assert deadline == datetime.date(2025, 6, 15)
        # sale on 2025-06-15 → taxable; 2025-06-16 → exempt


class TestPre2014Regime:
    def test_pre_2014_uses_six_months(self):
        assert time_test_deadline(datetime.date(2013, 3, 10), CFG) == datetime.date(2013, 9, 10)

    def test_pre_2014_month_end_clamp(self):
        # Aug 31 + 6 months → Feb has no 31st → last day of February
        assert time_test_deadline(datetime.date(2013, 8, 31), CFG) == datetime.date(2014, 2, 28)

    def test_cutoff_day_uses_three_years(self):
        # Acquired exactly on 2014-01-01 → NEW regime
        assert time_test_deadline(datetime.date(2014, 1, 1), CFG) == datetime.date(2017, 1, 1)

    def test_pre_2014_rule_can_be_disabled(self):
        cfg = CzTaxConfig(pre_2014_rule_enabled=False)
        assert time_test_deadline(datetime.date(2013, 3, 10), cfg) == datetime.date(2016, 3, 10)
