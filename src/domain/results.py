# src/domain/results.py
from dataclasses import dataclass, field, KW_ONLY
from decimal import Decimal
import uuid
from typing import Optional, Dict
from collections import defaultdict

import logging

from .enums import AssetCategory, TaxReportingCategory, InvestmentFundType, RealizationType

logger = logging.getLogger(__name__)


@dataclass
class LossOffsettingResult:
    form_line_values: Dict[TaxReportingCategory | str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    conceptual_net_stocks: Decimal = Decimal('0')
    conceptual_net_other_income: Decimal = Decimal('0')
    conceptual_net_derivatives_uncapped: Decimal = Decimal('0')
    conceptual_net_derivatives_capped: Decimal = Decimal('0')
    conceptual_net_p23_estg: Decimal = Decimal('0')
    conceptual_fund_income_net_taxable: Decimal = Decimal('0')


@dataclass
class RealizedGainLoss:
    originating_event_id: uuid.UUID
    asset_internal_id: uuid.UUID
    asset_category_at_realization: AssetCategory
    acquisition_date: str
    realization_date: str

    realization_type: RealizationType
    quantity_realized: Decimal

    unit_cost_basis_eur: Decimal
    unit_realization_value_eur: Decimal

    total_cost_basis_eur: Decimal
    total_realization_value_eur: Decimal

    gross_gain_loss_eur: Decimal

    _: KW_ONLY
    holding_period_days: Optional[int] = None
    # True when acquisition_date comes from a synthetic SOY fallback lot
    # (31 Dec of the prior year) — the real purchase date is unknown, so
    # holding-period tests must not treat the date as real.
    is_acquisition_estimated: bool = False
    is_within_speculation_period: bool = False
    is_taxable_under_section_23: bool = False # Changed default to False

    tax_reporting_category: Optional[TaxReportingCategory] = None

    fund_type_at_sale: Optional[InvestmentFundType] = None
    teilfreistellung_rate_applied: Optional[Decimal] = None
    teilfreistellung_amount_eur: Optional[Decimal] = None
    net_gain_loss_after_teilfreistellung_eur: Optional[Decimal] = None

    is_stillhalter_income: bool = False

    def __post_init__(self):
        if not isinstance(self.asset_category_at_realization, AssetCategory):
            raise TypeError(f"RealizedGainLoss.asset_category_at_realization must be an AssetCategory, got {type(self.asset_category_at_realization)}")
        if not isinstance(self.realization_type, RealizationType):
            raise TypeError(f"RealizedGainLoss.realization_type must be a RealizationType, got {type(self.realization_type)}")
        if self.tax_reporting_category is not None and not isinstance(self.tax_reporting_category, TaxReportingCategory):
            raise TypeError(f"RealizedGainLoss.tax_reporting_category must be a TaxReportingCategory, got {type(self.tax_reporting_category)}")
        if self.fund_type_at_sale is not None and not isinstance(self.fund_type_at_sale, InvestmentFundType):
            raise TypeError(f"RealizedGainLoss.fund_type_at_sale must be an InvestmentFundType, got {type(self.fund_type_at_sale)}")
        if not isinstance(self.quantity_realized, Decimal) or self.quantity_realized < Decimal(0):
            raise ValueError(f"RealizedGainLoss.quantity_realized must be a non-negative Decimal, got {self.quantity_realized}")
        # Country-specific fields (tax_reporting_category, teilfreistellung_*,
        # is_taxable_under_section_23, is_stillhalter_income, etc.) are populated
        # by the injected TaxClassifier — NOT auto-calculated here.


@dataclass
class VorabpauschaleData:
    asset_internal_id: uuid.UUID
    tax_year: int

    fund_value_start_year_eur: Decimal
    fund_value_end_year_eur: Decimal
    distributions_during_year_eur: Decimal
    base_return_rate: Decimal
    basiszins: Decimal

    calculated_base_return_eur: Decimal

    gross_vorabpauschale_eur: Decimal

    fund_type: InvestmentFundType
    teilfreistellung_rate_applied: Decimal
    teilfreistellung_amount_eur: Decimal

    net_taxable_vorabpauschale_eur: Decimal

    tax_reporting_category_gross: Optional[TaxReportingCategory] = None
