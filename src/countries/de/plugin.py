# src/countries/de/plugin.py
"""
German TaxPlugin implementation.

Wraps the existing German-specific modules (loss_offsetting, reporting)
behind the TaxPlugin / TaxClassifier / TaxAggregator / OutputRenderer
Protocols defined in ``src.countries.base``.

During the transition period **no logic is moved** — this module delegates
to the original locations so that all existing tests keep passing.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional
import uuid

from src.countries.base import (
    OutputRenderer,
    TaxAggregator,
    TaxClassifier,
    TaxPlugin,
    TaxResult,
    TaxResultSection,
)
from src.domain.assets import Asset, InvestmentFund
from src.domain.enums import (
    AssetCategory,
    InvestmentFundType,
    TaxReportingCategory,
)
from src.domain.events import FinancialEvent
from src.domain.results import RealizedGainLoss, VorabpauschaleData, LossOffsettingResult
from src.engine.loss_offsetting import LossOffsettingEngine
from src.identification.asset_resolver import AssetResolver
from src.utils.tax_utils import get_teilfreistellung_rate_for_fund_type
from src.domain.enums import RealizationType
import src.config as global_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GermanTaxClassifier
# ---------------------------------------------------------------------------

class GermanTaxClassifier:
    """
    Assigns German ``TaxReportingCategory`` to a ``RealizedGainLoss``.

    Currently the classification is done **inside** ``fifo_manager.py``
    (the 3× duplicated block).  This class provides the *same* logic as
    a standalone callable so that new code paths can use it, and the
    existing ``fifo_manager`` blocks can be replaced incrementally in a
    later step.

    This class satisfies the ``TaxClassifier`` Protocol.
    """

    def classify(
        self,
        rgl: RealizedGainLoss,
        asset: Optional[Asset] = None,
    ) -> None:
        """Classify *rgl* in-place by setting ``tax_reporting_category``
        and all German-specific optional fields.

        Sets: ``tax_reporting_category``, ``is_taxable_under_section_23``,
        ``is_within_speculation_period``, ``is_stillhalter_income``,
        ``fund_type_at_sale``, ``teilfreistellung_rate_applied``,
        ``teilfreistellung_amount_eur``, ``net_gain_loss_after_teilfreistellung_eur``.
        """
        cat = rgl.asset_category_at_realization
        gl = rgl.gross_gain_loss_eur

        # --- Tax category assignment ---

        if cat == AssetCategory.STOCK:
            rgl.tax_reporting_category = (
                TaxReportingCategory.ANLAGE_KAP_AKTIEN_GEWINN
                if gl >= Decimal(0)
                else TaxReportingCategory.ANLAGE_KAP_AKTIEN_VERLUST
            )

        elif cat == AssetCategory.BOND:
            rgl.tax_reporting_category = (
                TaxReportingCategory.ANLAGE_KAP_SONSTIGE_KAPITALERTRAEGE
                if gl >= Decimal(0)
                else TaxReportingCategory.ANLAGE_KAP_SONSTIGE_VERLUSTE
            )

        elif cat in (AssetCategory.OPTION, AssetCategory.CFD):
            rgl.tax_reporting_category = (
                TaxReportingCategory.ANLAGE_KAP_TERMIN_GEWINN
                if gl >= Decimal(0)
                else TaxReportingCategory.ANLAGE_KAP_TERMIN_VERLUST
            )
            # Stillhalter: short option cover with gain, or short option expired worthless with gain
            if cat == AssetCategory.OPTION and gl >= Decimal(0):
                if rgl.realization_type in (
                    RealizationType.SHORT_POSITION_COVER,
                    RealizationType.OPTION_TRADE_CLOSE_SHORT,
                    RealizationType.OPTION_EXPIRED_SHORT,
                ):
                    rgl.is_stillhalter_income = True

        elif cat == AssetCategory.INVESTMENT_FUND:
            fund_type = rgl.fund_type_at_sale
            if fund_type is None:
                fund_type = InvestmentFundType.NONE
            rgl.fund_type_at_sale = fund_type

            tf_rate = get_teilfreistellung_rate_for_fund_type(fund_type)
            rgl.teilfreistellung_rate_applied = tf_rate
            rgl.tax_reporting_category = _kap_inv_gain_category(fund_type)

            # Teilfreistellung amount and net gain/loss
            if gl is not None and tf_rate is not None:
                rgl.teilfreistellung_amount_eur = (
                    gl.copy_abs() * tf_rate
                ).quantize(
                    global_config.OUTPUT_PRECISION_AMOUNTS,
                    rounding=global_config.DECIMAL_ROUNDING_MODE,
                )
            else:
                rgl.teilfreistellung_amount_eur = Decimal("0.00")

            if gl is not None:
                tf_amt = rgl.teilfreistellung_amount_eur
                if gl >= Decimal(0):
                    rgl.net_gain_loss_after_teilfreistellung_eur = gl - tf_amt
                else:
                    rgl.net_gain_loss_after_teilfreistellung_eur = gl + tf_amt
            else:
                rgl.net_gain_loss_after_teilfreistellung_eur = None

        elif cat == AssetCategory.PRIVATE_SALE_ASSET:
            rgl.is_within_speculation_period = True
            if rgl.holding_period_days is not None and rgl.holding_period_days <= 365:
                rgl.is_taxable_under_section_23 = True
                rgl.tax_reporting_category = (
                    TaxReportingCategory.SECTION_23_ESTG_TAXABLE_GAIN
                    if gl >= Decimal(0)
                    else TaxReportingCategory.SECTION_23_ESTG_TAXABLE_LOSS
                )
            else:
                rgl.is_taxable_under_section_23 = False
                rgl.tax_reporting_category = (
                    TaxReportingCategory.SECTION_23_ESTG_EXEMPT_HOLDING_PERIOD_MET
                )

        # --- net_gain_loss_after_teilfreistellung_eur fallback for non-funds ---
        if cat != AssetCategory.INVESTMENT_FUND:
            if gl is not None:
                rgl.net_gain_loss_after_teilfreistellung_eur = gl
            else:
                rgl.net_gain_loss_after_teilfreistellung_eur = None


def _kap_inv_gain_category(fund_type: InvestmentFundType) -> TaxReportingCategory:
    """Map ``InvestmentFundType`` → KAP-INV gain TaxReportingCategory."""
    _MAP = {
        InvestmentFundType.AKTIENFONDS: TaxReportingCategory.ANLAGE_KAP_INV_AKTIENFONDS_GEWINN_GROSS,
        InvestmentFundType.MISCHFONDS: TaxReportingCategory.ANLAGE_KAP_INV_MISCHFONDS_GEWINN_GROSS,
        InvestmentFundType.IMMOBILIENFONDS: TaxReportingCategory.ANLAGE_KAP_INV_IMMOBILIENFONDS_GEWINN_GROSS,
        InvestmentFundType.AUSLANDS_IMMOBILIENFONDS: TaxReportingCategory.ANLAGE_KAP_INV_AUSLANDS_IMMOBILIENFONDS_GEWINN_GROSS,
        InvestmentFundType.SONSTIGE_FONDS: TaxReportingCategory.ANLAGE_KAP_INV_SONSTIGE_FONDS_GEWINN_GROSS,
        InvestmentFundType.NONE: TaxReportingCategory.ANLAGE_KAP_INV_SONSTIGE_FONDS_GEWINN_GROSS,
    }
    return _MAP.get(fund_type, TaxReportingCategory.ANLAGE_KAP_INV_SONSTIGE_FONDS_GEWINN_GROSS)


# ---------------------------------------------------------------------------
# GermanTaxAggregator
# ---------------------------------------------------------------------------

class GermanTaxAggregator:
    """
    Wraps the existing ``LossOffsettingEngine`` behind the
    ``TaxAggregator`` Protocol.

    Accepts the same extra parameters (vorabpauschale_items,
    apply_conceptual_derivative_loss_capping) as the original engine
    via its constructor.

    This class satisfies the ``TaxAggregator`` Protocol.
    """

    def __init__(
        self,
        vorabpauschale_items: Optional[List[VorabpauschaleData]] = None,
        apply_conceptual_derivative_loss_capping: bool = global_config.APPLY_CONCEPTUAL_DERIVATIVE_LOSS_CAPPING,
    ):
        self._vorabpauschale_items = vorabpauschale_items or []
        self._apply_capping = apply_conceptual_derivative_loss_capping

    def aggregate(
        self,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
        tax_year: int,
    ) -> TaxResult:
        engine = LossOffsettingEngine(
            realized_gains_losses=realized_gains_losses,
            vorabpauschale_items=self._vorabpauschale_items,
            current_year_financial_events=financial_events,
            asset_resolver=asset_resolver,
            tax_year=tax_year,
            apply_conceptual_derivative_loss_capping=self._apply_capping,
        )
        lo_result: LossOffsettingResult = engine.calculate_reporting_figures()

        # Build generic TaxResult wrapping the original DE result
        sections = _loss_offsetting_to_sections(lo_result)
        return TaxResult(
            country_code="de",
            tax_year=tax_year,
            sections=sections,
            country_result=lo_result,
        )


def _loss_offsetting_to_sections(lo: LossOffsettingResult) -> Dict[str, TaxResultSection]:
    """Convert ``LossOffsettingResult`` into generic ``TaxResultSection`` dict."""
    kap_items: Dict[str, Decimal] = {}
    kap_inv_items: Dict[str, Decimal] = {}
    so_items: Dict[str, Decimal] = {}

    for key, value in lo.form_line_values.items():
        label = key.name if isinstance(key, TaxReportingCategory) else str(key)
        if "KAP_INV" in label:
            kap_inv_items[label] = value
        elif "SECTION_23" in label or "ANLAGE_SO" in label:
            so_items[label] = value
        else:
            kap_items[label] = value

    sections: Dict[str, TaxResultSection] = {}
    if kap_items:
        sections["anlage_kap"] = TaxResultSection(
            section_key="anlage_kap",
            label="Anlage KAP",
            line_items=kap_items,
        )
    if kap_inv_items:
        sections["anlage_kap_inv"] = TaxResultSection(
            section_key="anlage_kap_inv",
            label="Anlage KAP-INV",
            line_items=kap_inv_items,
        )
    if so_items:
        sections["anlage_so"] = TaxResultSection(
            section_key="anlage_so",
            label="Anlage SO",
            line_items=so_items,
        )
    return sections


# ---------------------------------------------------------------------------
# GermanOutputRenderer
# ---------------------------------------------------------------------------

class GermanOutputRenderer:
    """
    Delegates to the existing ``console_reporter`` and ``pdf_generator``.

    This class satisfies the ``OutputRenderer`` Protocol.
    """

    def __init__(
        self,
        vorabpauschale_items: Optional[List[VorabpauschaleData]] = None,
        eoy_mismatch_count: int = 0,
    ):
        self._vorabpauschale_items = vorabpauschale_items or []
        self._eoy_mismatch_count = eoy_mismatch_count

    def render_console(
        self,
        tax_result: TaxResult,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
    ) -> None:
        from src.reporting.console_reporter import generate_console_tax_report

        lo_result = tax_result.country_result
        if not isinstance(lo_result, LossOffsettingResult):
            logger.error("GermanOutputRenderer.render_console requires a LossOffsettingResult in tax_result.country_result")
            return

        generate_console_tax_report(
            realized_gains_losses=realized_gains_losses,
            vorabpauschale_items=self._vorabpauschale_items,
            all_financial_events=financial_events,
            asset_resolver=asset_resolver,
            tax_year=tax_result.tax_year,
            eoy_mismatch_count=self._eoy_mismatch_count,
            loss_offsetting_summary=lo_result,
        )

    def render_pdf(
        self,
        tax_result: TaxResult,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
        output_path: str,
    ) -> None:
        from src.reporting.pdf_generator import PdfReportGenerator

        lo_result = tax_result.country_result
        if not isinstance(lo_result, LossOffsettingResult):
            logger.error("GermanOutputRenderer.render_pdf requires a LossOffsettingResult in tax_result.country_result")
            return

        pdf_gen = PdfReportGenerator(
            loss_offsetting_result=lo_result,
            all_financial_events=financial_events,
            realized_gains_losses=realized_gains_losses,
            vorabpauschale_items=self._vorabpauschale_items,
            assets_by_id=asset_resolver.assets_by_internal_id,
            tax_year=tax_result.tax_year,
            eoy_mismatch_details=[],
        )
        pdf_gen.generate_report(output_path)


# ---------------------------------------------------------------------------
# GermanTaxPlugin — top-level façade
# ---------------------------------------------------------------------------

class GermanTaxPlugin:
    """
    German tax plugin implementing the ``TaxPlugin`` Protocol.

    Usage::

        plugin = GermanTaxPlugin(vorabpauschale_items=vp_items)
        classifier = plugin.get_tax_classifier()
        aggregator = plugin.get_tax_aggregator()
        renderer  = plugin.get_output_renderer()
    """

    def __init__(
        self,
        vorabpauschale_items: Optional[List[VorabpauschaleData]] = None,
        apply_conceptual_derivative_loss_capping: bool = global_config.APPLY_CONCEPTUAL_DERIVATIVE_LOSS_CAPPING,
        eoy_mismatch_count: int = 0,
    ):
        self._vorabpauschale_items = vorabpauschale_items or []
        self._apply_capping = apply_conceptual_derivative_loss_capping
        self._eoy_mismatch_count = eoy_mismatch_count

    @property
    def country_code(self) -> str:
        return "de"

    def get_merger_policy(self) -> None:
        """No German merger rule is on file in this fork.

        Upstream treats a stock-for-stock merger as tax-neutral (§20 Abs 4a
        EStG) and transfers the lots. That rule is deliberately NOT asserted
        here: it was never ported, and inventing it would put an unverified
        treatment into someone's return. Returning None makes the engine refuse
        such an event, which is the honest state — see
        ``src/engine/merger_policy.py``.
        """
        return None

    def get_tax_classifier(self) -> GermanTaxClassifier:
        return GermanTaxClassifier()

    def get_tax_aggregator(self) -> GermanTaxAggregator:
        return GermanTaxAggregator(
            vorabpauschale_items=self._vorabpauschale_items,
            apply_conceptual_derivative_loss_capping=self._apply_capping,
        )

    def get_output_renderer(self) -> GermanOutputRenderer:
        return GermanOutputRenderer(
            vorabpauschale_items=self._vorabpauschale_items,
            eoy_mismatch_count=self._eoy_mismatch_count,
        )
