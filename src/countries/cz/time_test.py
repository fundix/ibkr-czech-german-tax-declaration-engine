# src/countries/cz/time_test.py
"""
Czech holding-period time test evaluator (§4 odst. 1 písm. w ZDP).

Sets taxability fields on ``CzTaxItem`` objects:
- ``is_taxable`` / ``is_exempt`` / ``exemption_reason``
- ``included_in_tax_base``
- ``tax_review_status`` / ``tax_review_note``

Rules applied:
- **SECURITY_DISPOSAL** (stocks, bonds, funds): if held > threshold → exempt.
  Securities acquired on/after 2014-01-01 use the 3-calendar-year test;
  securities acquired BEFORE 2014-01-01 use the pre-2014 6-month test
  (přechodné ustanovení čl. II bod 5 zák. opatření č. 344/2013 Sb.),
  assuming a direct issuer share ≤ 5 % (noted on the item).
- **DIVIDEND / INTEREST**: always taxable (time test not applicable).
- **OPTION_CLOSE / OPTION_EXPIRY_WORTHLESS**: time test NOT applied
  (options are derivative instruments, not securities under §4/1/w).
- If ``acquisition_date`` is missing on a disposal → ``PENDING_MANUAL_REVIEW``.

The CZK 100k annual exempt limit lives in ``annual_limit.py`` (run AFTER
this evaluator).
"""
from __future__ import annotations

import calendar
import datetime
import logging
from typing import List

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.tax_items import (
    CzExemptionReason,
    CzTaxItem,
    CzTaxItemType,
    CzTaxReviewStatus,
)
from src.utils.type_utils import parse_ibkr_date

logger = logging.getLogger(__name__)


def _add_years(d: datetime.date, years: int) -> datetime.date:
    """Anniversary of *d* after *years* calendar years (§33 daňového řádu).

    Feb 29 anniversaries in a non-leap year fall on Feb 28 (the period ends
    on the last day of the month when the numerically matching day does not
    exist).
    """
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _add_months(d: datetime.date, months: int) -> datetime.date:
    """Date *months* calendar months after *d* (§33 daňového řádu).

    When the numerically matching day does not exist in the target month
    (e.g. Aug 31 + 6 months), the period ends on the last day of that month.
    """
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    try:
        return d.replace(year=year, month=month)
    except ValueError:
        return datetime.date(year, month, calendar.monthrange(year, month)[1])


# Securities acquired before this date fall under the pre-2014 exemption
# regime (6-month test) per the transitional provision of 344/2013 Sb.
_PRE_2014_CUTOFF = datetime.date(2014, 1, 1)


def time_test_deadline(
    acquisition_date: datetime.date,
    config: CzTaxConfig,
) -> datetime.date:
    """Last day of the §4/1/w holding period for a security acquired on
    *acquisition_date* — a disposal strictly AFTER this date is exempt.

    Single source of the deadline arithmetic (3-year test, pre-2014 6-month
    test, §33 daňového řádu month-end clamping), shared by the in-place
    evaluator below and by portfolio/countdown views.
    """
    if config.pre_2014_rule_enabled and acquisition_date < _PRE_2014_CUTOFF:
        return _add_months(acquisition_date, config.pre_2014_holding_test_months)
    return _add_years(acquisition_date, config.holding_test_years)

# Item types subject to the holding-period time test
_TIME_TEST_ITEM_TYPES = {
    CzTaxItemType.SECURITY_DISPOSAL,
}

# Item types where time test is explicitly NOT applicable
_NO_TIME_TEST_ITEM_TYPES = {
    CzTaxItemType.DIVIDEND,
    CzTaxItemType.FUND_DISTRIBUTION,
    CzTaxItemType.INTEREST,
    CzTaxItemType.OPTION_CLOSE,
    CzTaxItemType.OPTION_EXPIRY_WORTHLESS,
    CzTaxItemType.OPTION_EXERCISE_ASSIGNMENT,
    CzTaxItemType.OTHER,
}


def evaluate_time_test(
    items: List[CzTaxItem],
    config: CzTaxConfig,
) -> None:
    """
    Evaluate the Czech holding-period time test on *items* **in-place**.

    If ``config.time_test_enabled`` is ``False``, all items are marked
    taxable (no exemption applied).
    """
    for item in items:
        if getattr(item, "fx_conversion_failed", False):
            # Foreign→CZK conversion failed for this item. Its CZK amounts are
            # None (never the raw foreign amount), so it must not silently enter
            # the tax base with a bogus figure. Flag it for manual review and
            # keep it conservatively taxable.
            item.is_taxable = True
            item.is_exempt = False
            item.exemption_reason = None
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
            note = item.tax_review_note or ""
            item.tax_review_note = (
                f"{note + '; ' if note else ''}"
                "FX→CZK conversion failed — CZK amount unavailable; "
                "manual review required (item kept in tax base as conservative default)."
            )
            continue

        if item.item_type in _NO_TIME_TEST_ITEM_TYPES:
            # Income items and options — always taxable, no time test
            item.is_taxable = True
            item.is_exempt = False
            item.exemption_reason = None
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.RESOLVED
            continue

        if item.item_type not in _TIME_TEST_ITEM_TYPES:
            # Unknown type — taxable by default
            item.is_taxable = True
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.RESOLVED
            continue

        # --- SECURITY_DISPOSAL: apply time test ---

        if item.category_needs_review:
            # PRIVATE_SALE_ASSET / unknown category: may not be a security,
            # so the §4/1/w exemptions must not be granted silently.
            item.is_taxable = True
            item.is_exempt = False
            item.exemption_reason = None
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
            item.tax_review_note = (
                f"Asset category '{item.asset_category}' may not be a security "
                "— verify whether the §4/1/w time test and the 100k annual "
                "limit apply. Item kept taxable as conservative default."
            )
            continue

        if item.is_short_position:
            # Short positions can never pass the time test: the security is
            # not held between acquisition and transfer (the sale precedes
            # the purchase), and acquisition_date is the short OPENING date.
            item.is_taxable = True
            item.is_exempt = False
            item.exemption_reason = None
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.RESOLVED
            item.tax_review_note = (
                "Short position (sale precedes purchase) — §4/1/w time test "
                "not applicable; item is taxable."
            )
            continue

        if not config.time_test_enabled:
            item.is_taxable = True
            item.is_exempt = False
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.RESOLVED
            item.tax_review_note = "Time test disabled in config"
            continue

        # Synthetic acquisition date (SOY fallback lot, 31 Dec of the prior
        # year): the real purchase date is unknown, so the time test cannot
        # be evaluated reliably — keep taxable and flag for manual review
        # (the position may actually be exempt if held > 3 years).
        if item.acquisition_date_estimated:
            item.is_taxable = True
            item.is_exempt = False
            item.exemption_reason = None
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
            item.tax_review_note = (
                "Acquisition date is a synthetic SOY fallback (31 Dec) — the "
                "real purchase date is unknown; time test not evaluated. Item "
                "kept taxable as conservative default; review manually."
            )
            continue

        # Check for missing acquisition_date
        if not item.acquisition_date:
            item.is_taxable = True  # conservative default
            item.is_exempt = False
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
            item.tax_review_note = (
                "Missing acquisition_date — cannot evaluate time test. "
                "Item included in tax base as conservative default."
            )
            continue

        # Compute holding period from dates if not already set
        holding_days = item.holding_period_days
        if holding_days is None:
            acq = parse_ibkr_date(item.acquisition_date)
            evt = parse_ibkr_date(item.event_date)
            if acq is not None and evt is not None and evt >= acq:
                holding_days = (evt - acq).days
                item.holding_period_days = holding_days
            else:
                item.is_taxable = True
                item.is_exempt = False
                item.included_in_tax_base = True
                item.tax_review_status = CzTaxReviewStatus.PENDING_MANUAL_REVIEW
                item.tax_review_note = (
                    f"Cannot compute holding period from "
                    f"acquisition_date='{item.acquisition_date}', "
                    f"event_date='{item.event_date}'. "
                    "Item included in tax base as conservative default."
                )
                continue

        # Apply the time test (§4/1/w ZDP): exempt only if the holding period
        # EXCEEDS the threshold — time counted per §33 daňového řádu (the
        # period ends on the day of the anniversary). A fixed day-count
        # (years × 365) misfires whenever the window contains Feb 29, so the
        # dates take precedence; the day threshold is only a fallback when
        # the dates cannot be parsed.
        #
        # Securities acquired BEFORE 2014-01-01 keep the pre-2014 regime:
        # a 6-MONTH test (přechodné ustanovení čl. II bod 5 zák. opatření
        # č. 344/2013 Sb.), under the ≤5% direct-share assumption documented
        # in the config.
        acq_d = parse_ibkr_date(item.acquisition_date)
        evt_d = parse_ibkr_date(item.event_date)
        pre_2014 = (
            config.pre_2014_rule_enabled
            and acq_d is not None
            and acq_d < _PRE_2014_CUTOFF
        )
        if acq_d is not None and evt_d is not None:
            threshold_date = time_test_deadline(acq_d, config)
            is_exempt = evt_d > threshold_date
        else:
            is_exempt = holding_days > config.holding_test_days

        if pre_2014:
            rule_desc = (
                f"{config.pre_2014_holding_test_months} months — pre-2014 "
                "acquisition, čl. II bod 5 zák. opatření č. 344/2013 Sb. "
                "(assumes direct issuer share ≤ 5 % in the 24 months before "
                "the sale)"
            )
        else:
            rule_desc = f"{config.holding_test_years} calendar years (§4/1/w ZDP)"

        if is_exempt:
            item.is_taxable = False
            item.is_exempt = True
            item.exemption_reason = CzExemptionReason.TIME_TEST_PASSED
            item.included_in_tax_base = False
            item.tax_review_status = CzTaxReviewStatus.RESOLVED
            item.tax_review_note = (
                f"Exempt: held {holding_days} days > {rule_desc}"
            )
        else:
            item.is_taxable = True
            item.is_exempt = False
            item.exemption_reason = None
            item.included_in_tax_base = True
            item.tax_review_status = CzTaxReviewStatus.RESOLVED
            item.tax_review_note = (
                f"Taxable: held {holding_days} days ≤ {rule_desc}"
            )
