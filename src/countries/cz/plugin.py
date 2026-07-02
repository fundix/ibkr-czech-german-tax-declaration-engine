# src/countries/cz/plugin.py
"""
Czech Republic TaxPlugin implementation.

Data flow:
1. Core pipeline produces ``RealizedGainLoss`` items and ``FinancialEvent`` items.
2. ``CzechTaxClassifier.classify()`` assigns a ``CzTaxSection`` bucket to each RGL.
3. ``CzechTaxAggregator.aggregate()`` converts each item to CZK via
   ``CzCurrencyConverter`` (direct foreign→CZK, per-event, daily ČNB rate)
   and sums the buckets into a ``TaxResult`` with audit metadata.
4. ``CzechOutputRenderer`` prints/exports the result.
"""
from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.countries.base import (
    OutputRenderer,
    TaxAggregator,
    TaxClassifier,
    TaxPlugin,
    TaxResult,
    TaxResultSection,
)
from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection, category_to_cz_section
from src.countries.cz.fx_policy import (
    CzCurrencyConverter,
    CzFxPolicyConfig,
    FxConversionRecord,
)
from src.countries.cz.annual_limit import evaluate_annual_limit
from src.countries.cz.foreign_tax_credit import CzForeignTaxCreditSummary, evaluate_foreign_tax_credit
from src.countries.cz.item_builder import build_tax_items
from src.countries.cz.loss_offsetting import CzLossOffsettingResult, compute_loss_offsetting
from src.countries.cz.form_mapping import CzFormMappingResult, build_form_mapping
from src.countries.cz.tax_liability import CzTaxLiabilitySummary, compute_tax_liability
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzTaxReviewStatus, CzWhtRecord
from src.countries.cz.time_test import evaluate_time_test
from src.domain.assets import Asset
from src.domain.enums import AssetCategory, FinancialEventType
from src.domain.events import (
    CashFlowEvent,
    FinancialEvent,
    WithholdingTaxEvent,
)
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver
from src.utils.exchange_rate_provider import ExchangeRateProvider
from src.utils.type_utils import parse_ibkr_date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CzechTaxClassifier
# ---------------------------------------------------------------------------

class CzechTaxClassifier:
    """
    Assigns a ``CzTaxSection`` to each ``RealizedGainLoss``.

    Sets ``rgl.cz_tax_section`` to the appropriate bucket (§8 or §10).
    Time-test exemption is applied **later** by ``evaluate_time_test()``
    on the ``CzTaxItem`` level, not here.
    """

    def __init__(self, config: Optional[CzTaxConfig] = None):
        self.config = config or CzTaxConfig()

    def classify(
        self,
        rgl: RealizedGainLoss,
        asset: Optional[Asset] = None,
    ) -> None:
        cat = rgl.asset_category_at_realization
        section = category_to_cz_section(cat.name)

        rgl.cz_tax_section = section  # type: ignore[attr-defined]

        # CZ has no Teilfreistellung — net = gross
        if rgl.gross_gain_loss_eur is not None:
            rgl.net_gain_loss_after_teilfreistellung_eur = rgl.gross_gain_loss_eur
        else:
            rgl.net_gain_loss_after_teilfreistellung_eur = None


# ---------------------------------------------------------------------------
# CzechTaxAggregator
# ---------------------------------------------------------------------------

class CzechTaxAggregator:
    """
    Builds ``CzTaxItem`` objects from core outputs, then aggregates
    them into CZ ``TaxResult`` sections.

    Every amount is converted to CZK **per-event** via ``CzCurrencyConverter``.
    Individual ``CzTaxItem``s (with full audit trail) are stored in
    ``TaxResult.country_result["items"]`` for downstream use (time test,
    WHT credit, JSON/XLSX export).

    PLACEHOLDER: expense deduction rules (§10/4 ZDP) not applied.
    """

    def __init__(
        self,
        config: Optional[CzTaxConfig] = None,
        fx_converter: Optional[CzCurrencyConverter] = None,
    ):
        self.config = config or CzTaxConfig()
        self._fx = fx_converter

    def aggregate(
        self,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
        tax_year: int,
    ) -> TaxResult:
        TWO = Decimal("0.01")
        ZERO = Decimal(0)

        # --- Phase 1: Build individual tax items with FX + WHT linking ---
        items, fx_records = build_tax_items(
            realized_gains_losses=realized_gains_losses,
            financial_events=financial_events,
            asset_resolver=asset_resolver,
            fx=self._fx,
        )

        # --- Phase 2: Apply time test (sets taxability fields in-place) ---
        evaluate_time_test(items, self.config)

        # --- Phase 3: Apply annual exempt limit (after time test) ---
        has_fx = self._fx is not None
        annual_limit_proceeds = evaluate_annual_limit(items, self.config, has_fx)

        # --- Phase 4: Compute §10 loss offsetting ---
        netting = compute_loss_offsetting(items, has_fx)
        netting.annual_limit_eligible_proceeds = annual_limit_proceeds
        netting.annual_limit_threshold = self.config.annual_exempt_limit_czk
        netting.annual_limit_applied = (
            self.config.annual_exempt_limit_enabled
            and annual_limit_proceeds <= self.config.annual_exempt_limit_czk
            and annual_limit_proceeds > ZERO
        )

        # --- Phase 5: Aggregate §8 income (dividends, interest) ---
        div_taxable = ZERO
        div_wht = ZERO
        div_count = 0
        int_taxable = ZERO
        int_count = 0

        for it in items:
            if it.section == CzTaxSection.CZ_8_DIVIDENDS:
                if it.item_type == CzTaxItemType.OTHER:
                    # Unlinked WHT standalone item — count WHT only, not as income
                    div_wht += it.total_wht_czk() if has_fx else sum(
                        (r.original_amount for r in it.wht_records), ZERO
                    )
                    continue
                div_count += 1
                if it.included_in_tax_base:
                    div_taxable += (it.amount_czk if has_fx else it.amount_eur) or ZERO
                    div_wht += it.total_wht_czk() if has_fx else sum(
                        (r.original_amount for r in it.wht_records), ZERO
                    )
            elif it.section == CzTaxSection.CZ_8_INTEREST:
                int_count += 1
                if it.included_in_tax_base:
                    int_taxable += (it.amount_czk if has_fx else it.amount_eur) or ZERO

        cur = "CZK" if has_fx else "EUR"
        c = cur.lower()

        # --- Phase 6: Build TaxResult sections ---
        sections: Dict[str, TaxResultSection] = {}

        sections["cz_8_dividends"] = TaxResultSection(
            section_key="cz_8_dividends",
            label=self.config.section_labels.get("cz_8_dividends", "§8 – Dividendy"),
            line_items={
                f"gross_dividends_{c}": div_taxable.quantize(TWO),
                f"wht_paid_{c}": div_wht.quantize(TWO),
                "item_count": Decimal(div_count),
            },
            notes=([] if has_fx else ["no FX converter — amounts in EUR"]),
        )

        sections["cz_8_interest"] = TaxResultSection(
            section_key="cz_8_interest",
            label=self.config.section_labels.get("cz_8_interest", "§8 – Úroky"),
            line_items={
                f"gross_interest_{c}": int_taxable.quantize(TWO),
                "item_count": Decimal(int_count),
            },
            notes=([] if has_fx else ["no FX converter — amounts in EUR"]),
        )

        # --- Phase 5.5: Foreign tax credit (§38f ZDP) ---
        ftc_summary = evaluate_foreign_tax_credit(items, self.config, has_fx)

        # §10 netting summary (from loss_offsetting module)
        netting_items = netting.to_line_items(cur)
        sections["cz_10_summary"] = TaxResultSection(
            section_key="cz_10_summary",
            label="§10 ZDP – Souhrnný přehled",
            line_items=netting_items,
            notes=["PLACEHOLDER: expense deduction rules (§10/4 ZDP) not applied"],
        )

        # Foreign tax credit summary (preliminary per-item caps)
        ftc_items = ftc_summary.to_line_items(cur)
        sections["cz_ftc_summary"] = TaxResultSection(
            section_key="cz_ftc_summary",
            label="§38f ZDP – Zápočet zahraniční daně (per-item caps)",
            line_items=ftc_items,
            notes=["Per-item treaty/default cap applied; final credit in liability summary"],
        )

        # --- Phase 7: Tax liability computation ---
        liability = compute_tax_liability(
            taxable_dividends=div_taxable,
            taxable_interest=int_taxable,
            netting=netting,
            ftc_summary=ftc_summary,
            config=self.config,
            tax_year=tax_year,
            has_fx=has_fx,
        )

        liability_items = liability.to_line_items(cur)
        sections["cz_tax_liability"] = TaxResultSection(
            section_key="cz_tax_liability",
            label="Daňová povinnost (§16 + §38f ZDP)",
            line_items=liability_items,
            notes=liability.limitation_notes,
        )

        # --- Phase 8: Form mapping (DAP-oriented output) ---
        form_mapping = build_form_mapping(
            liability=liability,
            netting=netting,
            ftc_summary=ftc_summary,
            taxable_dividends=div_taxable,
            taxable_interest=int_taxable,
            currency=cur,
            items=items,
        )

        return TaxResult(
            country_code="cz",
            tax_year=tax_year,
            sections=sections,
            country_result={
                "items": items,
                "netting": netting,
                "ftc_summary": ftc_summary,
                "liability": liability,
                "form_mapping": form_mapping,
                "fx_conversion_records": fx_records,
                "fx_policy": self.config.fx_policy,
                "currency": cur,
            },
        )


# ---------------------------------------------------------------------------
# CzechOutputRenderer
# ---------------------------------------------------------------------------

class CzechOutputRenderer:
    """
    Renders a CZ ``TaxResult`` to console / PDF.

    PLACEHOLDER: only console output implemented (simple dump).
    PDF generation is a future step.
    """

    def render_console(
        self,
        tax_result: TaxResult,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
    ) -> None:
        cr = tax_result.country_result or {}
        cur = cr.get("currency", "EUR")
        policy = cr.get("fx_policy")
        policy_desc = f" | FX: {policy.source} / {policy.mode.name.lower()}" if policy else ""

        print(f"\n--- Přehled pro daňové přiznání ČR za rok {tax_result.tax_year}{policy_desc} ---")
        print(f"--- Částky v {cur} ---\n")

        for key, section in tax_result.sections.items():
            print(f"  {section.label}")
            for item_key, value in section.line_items.items():
                print(f"    {item_key}: {value}")
            for note in section.notes:
                print(f"    ⚠ {note}")
            print()

        fx_records = cr.get("fx_conversion_records", [])
        if fx_records:
            print(f"  FX konverzí provedeno: {len(fx_records)}")

        print("--- Konec přehledu ---\n")

    def render_pdf(
        self,
        tax_result: TaxResult,
        realized_gains_losses: List[RealizedGainLoss],
        financial_events: List[FinancialEvent],
        asset_resolver: AssetResolver,
        output_path: str,
    ) -> None:
        logger.warning(
            f"CZ PDF report generation is not implemented yet. "
            f"Output path '{output_path}' ignored."
        )


# ---------------------------------------------------------------------------
# CzechTaxPlugin
# ---------------------------------------------------------------------------

class CzechTaxPlugin:
    """
    Czech Republic tax plugin implementing the ``TaxPlugin`` Protocol.

    Usage::

        plugin = CzechTaxPlugin()
        classifier = plugin.get_tax_classifier()
        aggregator = plugin.get_tax_aggregator()
        renderer  = plugin.get_output_renderer()

    To enable CZK conversion, supply an ``ExchangeRateProvider``
    (typically ``CNBExchangeRateProvider``) or let the plugin
    create one from the configured ``fx_policy.source``::

        from src.utils.fx_provider_factory import create_fx_provider
        provider = create_fx_provider("cnb")
        plugin = CzechTaxPlugin(fx_provider=provider)
    """

    def __init__(
        self,
        config: Optional[CzTaxConfig] = None,
        fx_provider: Optional[ExchangeRateProvider] = None,
        **kwargs,
    ):
        self._config = config or CzTaxConfig()
        self._fx_provider = fx_provider
        # Accept and ignore unknown kwargs for registry compatibility
        if kwargs:
            logger.debug(f"CzechTaxPlugin ignoring unknown kwargs: {list(kwargs.keys())}")

    @property
    def country_code(self) -> str:
        return "cz"

    @property
    def config(self) -> CzTaxConfig:
        return self._config

    def get_tax_classifier(self) -> CzechTaxClassifier:
        return CzechTaxClassifier(config=self._config)

    def get_tax_aggregator(self) -> CzechTaxAggregator:
        fx_converter: Optional[CzCurrencyConverter] = None
        if self._fx_provider is not None:
            fx_converter = CzCurrencyConverter(
                provider=self._fx_provider,
                policy=self._config.fx_policy,
            )
        return CzechTaxAggregator(config=self._config, fx_converter=fx_converter)

    def get_output_renderer(self) -> CzechOutputRenderer:
        return CzechOutputRenderer()
