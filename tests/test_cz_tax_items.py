# tests/test_cz_tax_items.py
"""
Tests for CZ tax item building and bucket classification.

Covers:
1. Dividend with linked withholding tax
2. Interest income
3. Stock sale (security disposal)
4. Option close (sell-to-close)
5. Worthless option expiration
6. Item audit metadata completeness
7. WHT linking logic
8. to_dict() serialisation
"""
import os
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

import pytest

from src.countries.cz.config import CzTaxConfig
from src.countries.cz.enums import CzTaxSection
from src.countries.cz.fx_policy import CzCurrencyConverter, CzFxPolicyConfig, FxConversionRecord
from src.countries.cz.item_builder import build_tax_items
from src.countries.cz.plugin import CzechTaxAggregator, CzechTaxPlugin
from src.countries.cz.tax_items import CzTaxItem, CzTaxItemType, CzWhtRecord
from src.domain.enums import AssetCategory, FinancialEventType, RealizationType
from src.domain.events import CashFlowEvent, FinancialEvent, WithholdingTaxEvent
from src.domain.results import RealizedGainLoss
from src.identification.asset_resolver import AssetResolver
from src.classification.asset_classifier import AssetClassifier
from src.domain.assets import Asset, Stock, Option
from src.utils.cnb_exchange_rate_provider import CNBExchangeRateProvider


# ---------------------------------------------------------------------------
# Mock CNB provider
# ---------------------------------------------------------------------------

SAMPLE_CNB = """\
25.03.2025 #59
země|měna|množství|kód|kurz
EMU|euro|1|EUR|24,320
USA|dolar|1|USD|22,345
Velká Británie|libra|1|GBP|28,910
"""


class MockCNBProvider(CNBExchangeRateProvider):
    def __init__(self, responses=None, **kw):
        self._mock_responses = responses or {}
        if "cache_file_path" not in kw:
            kw["cache_file_path"] = os.path.join(tempfile.mkdtemp(), "m.json")
        super().__init__(**kw)

    def _fetch_rates_for_date(self, query_date):
        # Fall back to the sample rate for any date not explicitly mocked, so
        # acquisition-date conversions (cost basis) resolve like the real ČNB
        # provider would instead of spuriously failing.
        text = self._mock_responses.get(query_date) or SAMPLE_CNB
        return self._parse_cnb_text(text, query_date) if text else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolver() -> AssetResolver:
    class Dummy(AssetClassifier):
        def __init__(self):
            super().__init__(cache_file_path="dummy.json")
        def save_classifications(self):
            pass
    return AssetResolver(asset_classifier=Dummy())


def _fx() -> CzCurrencyConverter:
    provider = MockCNBProvider(responses={date(2025, 3, 25): SAMPLE_CNB})
    return CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())


_STOCK_ID = uuid.uuid4()
_OPTION_ID = uuid.uuid4()


def _register_stock(resolver: AssetResolver) -> Stock:
    s = Stock(asset_category=AssetCategory.STOCK, description="AAPL")
    s.internal_asset_id = _STOCK_ID
    s.ibkr_symbol = "AAPL"
    s.ibkr_isin = "US0378331005"
    resolver.assets_by_internal_id[_STOCK_ID] = s
    return s


def _register_option(resolver: AssetResolver) -> Option:
    o = Option(asset_category=AssetCategory.OPTION, description="AAPL 150 C 2025-06")
    o.internal_asset_id = _OPTION_ID
    o.ibkr_symbol = "AAPL 250620C00150000"
    resolver.assets_by_internal_id[_OPTION_ID] = o
    return o


def _make_rgl(
    asset_id: uuid.UUID,
    cat: AssetCategory,
    gross: Decimal,
    realization_type: RealizationType = RealizationType.LONG_POSITION_SALE,
    realization_date: str = "2025-03-25",
    acquisition_date: str = "2024-06-15",
    holding_days: int = 283,
    cost_basis: Optional[Decimal] = None,
    proceeds: Optional[Decimal] = None,
) -> RealizedGainLoss:
    cb = cost_basis if cost_basis is not None else max(Decimal(0), -gross)
    pr = proceeds if proceeds is not None else max(Decimal(0), gross)
    return RealizedGainLoss(
        originating_event_id=uuid.uuid4(),
        asset_internal_id=asset_id,
        asset_category_at_realization=cat,
        acquisition_date=acquisition_date,
        realization_date=realization_date,
        realization_type=realization_type,
        quantity_realized=Decimal("10"),
        unit_cost_basis_eur=cb / Decimal("10"),
        unit_realization_value_eur=pr / Decimal("10"),
        total_cost_basis_eur=cb,
        total_realization_value_eur=pr,
        gross_gain_loss_eur=gross,
        holding_period_days=holding_days,
    )


def _make_dividend(
    asset_id: uuid.UUID, amount: Decimal, currency: str = "USD",
    event_date: str = "2025-03-25",
) -> CashFlowEvent:
    eur = amount / Decimal("1.1") if currency != "EUR" else amount
    return CashFlowEvent(
        asset_internal_id=asset_id,
        event_date=event_date,
        event_type=FinancialEventType.DIVIDEND_CASH,
        gross_amount_foreign_currency=amount,
        local_currency=currency,
        gross_amount_eur=eur,
    )


def _make_interest(
    asset_id: uuid.UUID, amount: Decimal, currency: str = "USD",
    event_date: str = "2025-03-25",
) -> CashFlowEvent:
    eur = amount / Decimal("1.1") if currency != "EUR" else amount
    return CashFlowEvent(
        asset_internal_id=asset_id,
        event_date=event_date,
        event_type=FinancialEventType.INTEREST_RECEIVED,
        gross_amount_foreign_currency=amount,
        local_currency=currency,
        gross_amount_eur=eur,
    )


def _make_wht(
    asset_id: uuid.UUID, amount: Decimal, currency: str = "USD",
    event_date: str = "2025-03-25",
    taxed_income_event_id: Optional[uuid.UUID] = None,
    source_country: str = "US",
) -> WithholdingTaxEvent:
    eur = amount / Decimal("1.1") if currency != "EUR" else amount
    return WithholdingTaxEvent(
        asset_internal_id=asset_id,
        event_date=event_date,
        gross_amount_foreign_currency=amount,
        local_currency=currency,
        gross_amount_eur=eur,
        taxed_income_event_id=taxed_income_event_id,
        source_country_code=source_country,
    )


# =========================================================================
# Test 1: Dividend with linked WHT
# =========================================================================

class TestDividendWithWht:
    def test_dividend_becomes_cz_8_dividends(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        wht = _make_wht(
            _STOCK_ID, Decimal("15"), "USD",
            taxed_income_event_id=div.event_id,
            source_country="US",
        )
        events: List[FinancialEvent] = [div, wht]

        items, fx_recs = build_tax_items([], events, resolver, fx)

        div_items = [i for i in items if i.item_type == CzTaxItemType.DIVIDEND]
        assert len(div_items) == 1
        item = div_items[0]

        assert item.section == CzTaxSection.CZ_8_DIVIDENDS
        assert item.original_amount == Decimal("100")
        assert item.original_currency == "USD"
        assert item.amount_czk is not None
        # 100 USD * 22.345 CZK/USD ≈ 2234.5 CZK
        assert abs(item.amount_czk - Decimal("2234.5")) < Decimal("1")
        assert item.asset_symbol == "AAPL"
        assert item.asset_isin == "US0378331005"
        assert item.event_date == "2025-03-25"

    def test_wht_linked_to_dividend(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        wht = _make_wht(
            _STOCK_ID, Decimal("15"), "USD",
            taxed_income_event_id=div.event_id,
            source_country="US",
        )

        items, _ = build_tax_items([], [div, wht], resolver, fx)
        div_item = [i for i in items if i.item_type == CzTaxItemType.DIVIDEND][0]

        assert len(div_item.wht_records) == 1
        wht_rec = div_item.wht_records[0]
        assert wht_rec.original_amount == Decimal("15")
        assert wht_rec.original_currency == "USD"
        assert wht_rec.amount_czk is not None
        # 15 USD * 22.345 ≈ 335 CZK
        assert abs(wht_rec.amount_czk - Decimal("335")) < Decimal("2")
        assert wht_rec.source_country == "US"

    def test_wht_total_czk(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        wht = _make_wht(_STOCK_ID, Decimal("15"), "USD", taxed_income_event_id=div.event_id)

        items, _ = build_tax_items([], [div, wht], resolver, fx)
        div_item = [i for i in items if i.item_type == CzTaxItemType.DIVIDEND][0]
        assert div_item.total_wht_czk() > Decimal("330")

    def test_wht_fx_metadata(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        wht = _make_wht(_STOCK_ID, Decimal("15"), "USD", taxed_income_event_id=div.event_id)

        items, _ = build_tax_items([], [div, wht], resolver, fx)
        wht_rec = items[0].wht_records[0]
        assert wht_rec.fx is not None
        assert wht_rec.fx.fx_source == "cnb"
        assert wht_rec.fx.original_currency == "USD"


# =========================================================================
# Test 2: Interest income
# =========================================================================

class TestInterestIncome:
    def test_interest_becomes_cz_8_interest(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        intr = _make_interest(_STOCK_ID, Decimal("50"), "USD")
        items, _ = build_tax_items([], [intr], resolver, fx)

        int_items = [i for i in items if i.item_type == CzTaxItemType.INTEREST]
        assert len(int_items) == 1
        item = int_items[0]
        assert item.section == CzTaxSection.CZ_8_INTEREST
        assert item.original_amount == Decimal("50")
        assert item.amount_czk is not None
        # 50 USD * 22.345 ≈ 1117 CZK
        assert abs(item.amount_czk - Decimal("1117")) < Decimal("2")


# =========================================================================
# Test 3: Stock sale (security disposal)
# =========================================================================

class TestStockSale:
    def test_stock_sale_becomes_cz_10_securities(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        rgl = _make_rgl(
            _STOCK_ID, AssetCategory.STOCK, Decimal("500"),
            cost_basis=Decimal("1000"), proceeds=Decimal("1500"),
        )

        items, _ = build_tax_items([rgl], [], resolver, fx)
        disp_items = [i for i in items if i.item_type == CzTaxItemType.SECURITY_DISPOSAL]
        assert len(disp_items) == 1
        item = disp_items[0]

        assert item.section == CzTaxSection.CZ_10_SECURITIES
        assert item.gain_loss_eur == Decimal("500")
        assert item.cost_basis_eur == Decimal("1000")
        assert item.proceeds_eur == Decimal("1500")
        assert item.gain_loss_czk is not None
        assert item.cost_basis_czk is not None
        assert item.proceeds_czk is not None
        assert item.quantity == Decimal("10")
        assert item.asset_symbol == "AAPL"
        assert item.acquisition_date == "2024-06-15"
        assert item.holding_period_days == 283

    def test_stock_sale_czk_amounts(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        rgl = _make_rgl(
            _STOCK_ID, AssetCategory.STOCK, Decimal("500"),
            cost_basis=Decimal("1000"), proceeds=Decimal("1500"),
        )

        items, _ = build_tax_items([rgl], [], resolver, fx)
        item = items[0]

        # 500 EUR * 24.320 CZK/EUR ≈ 12160 CZK
        assert abs(item.gain_loss_czk - Decimal("12160")) < Decimal("10")
        # 1000 EUR * 24.320 ≈ 24320 CZK
        assert abs(item.cost_basis_czk - Decimal("24320")) < Decimal("10")
        # 1500 EUR * 24.320 ≈ 36480 CZK
        assert abs(item.proceeds_czk - Decimal("36480")) < Decimal("10")

    def test_stock_sale_fx_audit(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        rgl = _make_rgl(_STOCK_ID, AssetCategory.STOCK, Decimal("500"),
                        cost_basis=Decimal("1000"), proceeds=Decimal("1500"))
        items, _ = build_tax_items([rgl], [], resolver, fx)
        item = items[0]

        assert item.fx is not None
        assert item.fx.fx_source == "cnb"
        assert item.fx_cost_basis is not None
        assert item.fx_proceeds is not None


# =========================================================================
# Test 4: Option close (sell-to-close)
# =========================================================================

class TestOptionClose:
    def test_option_close_becomes_cz_10_options(self):
        resolver = _resolver()
        _register_option(resolver)
        fx = _fx()

        rgl = _make_rgl(
            _OPTION_ID, AssetCategory.OPTION, Decimal("300"),
            realization_type=RealizationType.OPTION_TRADE_CLOSE_LONG,
            cost_basis=Decimal("200"), proceeds=Decimal("500"),
        )

        items, _ = build_tax_items([rgl], [], resolver, fx)
        opt_items = [i for i in items if i.item_type == CzTaxItemType.OPTION_CLOSE]
        assert len(opt_items) == 1
        item = opt_items[0]

        assert item.section == CzTaxSection.CZ_10_OPTIONS
        assert item.gain_loss_eur == Decimal("300")
        assert item.asset_symbol == "AAPL 250620C00150000"


# =========================================================================
# Test 5: Worthless option expiration
# =========================================================================

class TestWorthlessExpiration:
    def test_expired_long_becomes_cz_10_options(self):
        resolver = _resolver()
        _register_option(resolver)
        fx = _fx()

        rgl = _make_rgl(
            _OPTION_ID, AssetCategory.OPTION, Decimal("-200"),
            realization_type=RealizationType.OPTION_EXPIRED_LONG,
            cost_basis=Decimal("200"), proceeds=Decimal("0"),
        )

        items, _ = build_tax_items([rgl], [], resolver, fx)
        exp_items = [i for i in items if i.item_type == CzTaxItemType.OPTION_EXPIRY_WORTHLESS]
        assert len(exp_items) == 1
        item = exp_items[0]

        assert item.section == CzTaxSection.CZ_10_OPTIONS
        assert item.gain_loss_eur == Decimal("-200")
        assert item.gain_loss_czk is not None
        assert item.gain_loss_czk < Decimal(0)

    def test_expired_short_gain(self):
        resolver = _resolver()
        _register_option(resolver)
        fx = _fx()

        rgl = _make_rgl(
            _OPTION_ID, AssetCategory.OPTION, Decimal("150"),
            realization_type=RealizationType.OPTION_EXPIRED_SHORT,
            cost_basis=Decimal("0"), proceeds=Decimal("150"),
        )

        items, _ = build_tax_items([rgl], [], resolver, fx)
        item = items[0]
        assert item.item_type == CzTaxItemType.OPTION_EXPIRY_WORTHLESS
        assert item.section == CzTaxSection.CZ_10_OPTIONS
        assert item.gain_loss_eur == Decimal("150")
        assert item.gain_loss_czk > Decimal(0)


# =========================================================================
# Test 6: Audit metadata completeness
# =========================================================================

class TestAuditMetadata:
    def test_disposal_item_has_all_fields(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        rgl = _make_rgl(_STOCK_ID, AssetCategory.STOCK, Decimal("500"),
                        cost_basis=Decimal("1000"), proceeds=Decimal("1500"))
        items, _ = build_tax_items([rgl], [], resolver, fx)
        item = items[0]

        assert item.source_event_id is not None
        assert item.asset_id == _STOCK_ID
        assert item.asset_symbol == "AAPL"
        assert item.asset_isin == "US0378331005"
        assert item.asset_category == "STOCK"
        assert item.event_date == "2025-03-25"
        assert item.acquisition_date == "2024-06-15"
        assert item.holding_period_days == 283
        assert item.original_currency == "EUR"
        assert item.fx is not None
        assert item.fx.fx_source == "cnb"
        assert item.fx.fx_policy == "daily"

    def test_income_item_has_all_fields(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        items, _ = build_tax_items([], [div], resolver, fx)
        item = items[0]

        assert item.source_event_id == div.event_id
        assert item.asset_id == _STOCK_ID
        assert item.asset_symbol == "AAPL"
        assert item.original_amount == Decimal("100")
        assert item.original_currency == "USD"
        assert item.fx is not None
        assert item.fx.original_currency == "USD"


# =========================================================================
# Test 7: to_dict() serialisation
# =========================================================================

class TestSerialization:
    def test_disposal_to_dict(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        rgl = _make_rgl(_STOCK_ID, AssetCategory.STOCK, Decimal("500"),
                        cost_basis=Decimal("1000"), proceeds=Decimal("1500"))
        items, _ = build_tax_items([rgl], [], resolver, fx)
        d = items[0].to_dict()

        assert d["item_type"] == "SECURITY_DISPOSAL"
        assert d["section"] == "CZ_10_SECURITIES"
        assert d["asset_symbol"] == "AAPL"
        assert d["gain_loss_eur"] is not None
        assert d["gain_loss_czk"] is not None
        assert d["fx_source"] == "cnb"
        assert d["fx_policy"] == "daily"

    def test_dividend_with_wht_to_dict(self):
        resolver = _resolver()
        _register_stock(resolver)
        fx = _fx()

        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        wht = _make_wht(_STOCK_ID, Decimal("15"), "USD", taxed_income_event_id=div.event_id)
        items, _ = build_tax_items([], [div, wht], resolver, fx)
        d = items[0].to_dict()

        assert d["item_type"] == "DIVIDEND"
        assert len(d["wht_records"]) == 1
        assert d["wht_records"][0]["original_amount"] == "15"
        assert d["wht_records"][0]["source_country"] == "US"
        assert float(d["wht_total_czk"]) > 0


# =========================================================================
# Test 8: Aggregator integration with items
# =========================================================================

class TestAggregatorWithItems:
    def test_items_in_country_result(self):
        resolver = _resolver()
        _register_stock(resolver)
        provider = MockCNBProvider(responses={date(2025, 3, 25): SAMPLE_CNB})
        plugin = CzechTaxPlugin(fx_provider=provider)
        aggregator = plugin.get_tax_aggregator()

        rgl = _make_rgl(_STOCK_ID, AssetCategory.STOCK, Decimal("500"),
                        cost_basis=Decimal("1000"), proceeds=Decimal("1500"))
        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        wht = _make_wht(_STOCK_ID, Decimal("15"), "USD", taxed_income_event_id=div.event_id)

        result = aggregator.aggregate([rgl], [div, wht], resolver, 2025)

        cr = result.country_result
        assert "items" in cr
        items = cr["items"]
        assert len(items) == 2  # 1 dividend + 1 disposal
        assert all(isinstance(i, CzTaxItem) for i in items)

    def test_dividend_wht_in_section_totals(self):
        resolver = _resolver()
        _register_stock(resolver)
        provider = MockCNBProvider(responses={date(2025, 3, 25): SAMPLE_CNB})

        converter = CzCurrencyConverter(provider=provider, policy=CzFxPolicyConfig())
        aggregator = CzechTaxAggregator(fx_converter=converter)

        div = _make_dividend(_STOCK_ID, Decimal("100"), "USD")
        wht = _make_wht(_STOCK_ID, Decimal("15"), "USD", taxed_income_event_id=div.event_id)

        result = aggregator.aggregate([], [div, wht], resolver, 2025)
        sec = result.sections["cz_8_dividends"]

        assert sec.line_items["gross_dividends_czk"] > Decimal("2200")
        assert sec.line_items["wht_paid_czk"] > Decimal("330")
