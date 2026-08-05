# tests/test_time_test_deadline.py
"""
Pure §4/1/u deadline helper — single source of the holding-period
arithmetic shared by the in-place evaluator, optimal pairing and the
portfolio countdown.

Two dates go in: the acquisition date selects the REGIME (pre-2014 six months
vs three calendar years), the holding-period start is what the period is
MEASURED from. They coincide for everything except a holding carried over by a
qualified merger (§23b/§23c ZDP), so the classes below keep them equal and
``TestCarriedOverHolding`` covers the case where they diverge.
"""
import datetime

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.time_test import time_test_deadline, time_test_exempt


CFG = CzTaxConfig()


def _deadline(date, config=CFG):
    """Both dates equal — the ordinary, non-carried case."""
    return time_test_deadline(
        acquisition_date=date, holding_period_start=date, config=config
    )


class TestStandardThreeYearTest:
    def test_plain_anniversary(self):
        assert _deadline(datetime.date(2024, 3, 5)) == datetime.date(2027, 3, 5)

    def test_feb_29_clamps_to_feb_28(self):
        # 2024-02-29 + 3y → 2027 has no Feb 29 → period ends on the last
        # day of the month (§33 daňového řádu)
        assert _deadline(datetime.date(2024, 2, 29)) == datetime.date(2027, 2, 28)

    def test_disposal_on_deadline_not_exempt_semantics(self):
        # The evaluator exempts only disposals strictly AFTER the deadline —
        # document the boundary here so the portfolio "osvobozeno od" date
        # (deadline + 1 day) stays consistent with evaluate_time_test.
        acq = datetime.date(2022, 6, 15)
        deadline = _deadline(acq)
        assert deadline == datetime.date(2025, 6, 15)
        # sale on 2025-06-15 → taxable; 2025-06-16 → exempt
        assert not time_test_exempt(
            acquisition_date=acq, holding_period_start=acq,
            sale_date=deadline, config=CFG)
        assert time_test_exempt(
            acquisition_date=acq, holding_period_start=acq,
            sale_date=deadline + datetime.timedelta(days=1), config=CFG)


class TestPre2014Regime:
    def test_pre_2014_uses_six_months(self):
        assert _deadline(datetime.date(2013, 3, 10)) == datetime.date(2013, 9, 10)

    def test_pre_2014_month_end_clamp(self):
        # Aug 31 + 6 months → Feb has no 31st → last day of February
        assert _deadline(datetime.date(2013, 8, 31)) == datetime.date(2014, 2, 28)

    def test_cutoff_day_uses_three_years(self):
        # Acquired exactly on 2014-01-01 → NEW regime
        assert _deadline(datetime.date(2014, 1, 1)) == datetime.date(2017, 1, 1)

    def test_pre_2014_rule_can_be_disabled(self):
        cfg = CzTaxConfig(pre_2014_rule_enabled=False)
        assert _deadline(datetime.date(2013, 3, 10), cfg) == datetime.date(2016, 3, 10)


class TestCarriedOverHolding:
    """Where the two dates diverge — a qualified §23b/§23c merger."""

    def test_period_is_measured_from_the_carried_start(self):
        """Acquired 2023 but holding running since 2010 → long since exempt."""
        assert time_test_deadline(
            acquisition_date=datetime.date(2023, 5, 1),
            holding_period_start=datetime.date(2010, 3, 1),
            config=CFG,
        ) == datetime.date(2013, 3, 1)

    def test_pre_2014_regime_does_not_transfer_to_a_later_share(self):
        """The trap this split exists for (NSS 3 Afs 249/2024-45).

        The holding began in 2013, but the share being sold was issued in
        2014-06. Selecting the regime from the carried date would apply the
        six-month test; it must apply three years, measured from 2013-11-01.
        """
        deadline = time_test_deadline(
            acquisition_date=datetime.date(2014, 6, 1),
            holding_period_start=datetime.date(2013, 11, 1),
            config=CFG,
        )
        assert deadline == datetime.date(2016, 11, 1)          # 3 years
        assert deadline != datetime.date(2014, 5, 1)           # NOT 6 months

        # A sale in early 2015 is therefore taxable, not exempt.
        assert not time_test_exempt(
            acquisition_date=datetime.date(2014, 6, 1),
            holding_period_start=datetime.date(2013, 11, 1),
            sale_date=datetime.date(2015, 1, 15),
            config=CFG,
        )

    def test_a_genuinely_pre_2014_share_still_gets_six_months(self):
        """The mirror of the above — do not over-correct."""
        assert time_test_deadline(
            acquisition_date=datetime.date(2013, 11, 1),
            holding_period_start=datetime.date(2013, 11, 1),
            config=CFG,
        ) == datetime.date(2014, 5, 1)

    def test_carried_start_uses_the_month_end_clamp_too(self):
        assert time_test_deadline(
            acquisition_date=datetime.date(2025, 1, 10),
            holding_period_start=datetime.date(2024, 2, 29),
            config=CFG,
        ) == datetime.date(2027, 2, 28)


class TestSignatureIsDeliberatelyStrict:
    def test_both_dates_are_required_keywords(self):
        """No positional or defaulted call is allowed.

        A default would let a caller silently measure from the wrong date, and
        the distinction is invisible in the common case where the dates match —
        so the signature has to force every call site to state both.
        """
        with pytest.raises(TypeError):
            time_test_deadline(datetime.date(2024, 3, 5), CFG)   # old positional
        with pytest.raises(TypeError):
            time_test_deadline(acquisition_date=datetime.date(2024, 3, 5), config=CFG)
