import logging
from dataclasses import dataclass, field, replace as dc_replace
from decimal import Decimal, Context, ROUND_DOWN, getcontext as get_global_context
from typing import Any, List, Optional, Tuple
import uuid
from datetime import date as date_obj, datetime

from src.domain.assets import Asset, Option
from src.domain.events import (
    FinancialEvent, TradeEvent, CorpActionSplitForward, CorpActionMergerCash,
    CorpActionStockDividend, OptionAssignmentEvent, OptionExerciseEvent,
    OptionExpirationWorthlessEvent, OptionLifecycleEvent,
)
from src.domain.results import RealizedGainLoss
from src.domain.enums import AssetCategory, FinancialEventType, TaxReportingCategory, RealizationType, InvestmentFundType
from src.utils.currency_converter import CurrencyConverter
from src.utils.exchange_rate_provider import ECBExchangeRateProvider
from src.utils.type_utils import parse_ibkr_date, safe_decimal, numeric_tx_sort_key
from src.utils.tax_utils import get_teilfreistellung_rate_for_fund_type
from src.engine.pairing import (
    PairingHistory, PairingMethod, coerce as coerce_pairing_method,
    consumption_order_indices, uses_pool_average_cost,
)
import src.config as global_config

# Type alias for the optional tax classifier callback.
# Any callable with signature (RealizedGainLoss, Optional[Asset]) -> None works.
from typing import Callable
_TaxClassifierCallable = Optional[Callable[[RealizedGainLoss], None]]

logger = logging.getLogger(__name__)

# Source-transaction-id prefixes for lots the engine invented because the trade
# history could not be reconstructed. Both are matched by prefix, so the short
# marker is covered by the long one.
SOY_FALLBACK_PREFIX = "SOY_FALLBACK"
SOY_SNAPSHOT_PREFIX = "SOY_SNAPSHOT"


def _original_cost_of_buy(trade_event) -> Tuple[Optional[Decimal], Optional[str]]:
    """What a BUY cost in its own currency, or ``(None, None)``.

    Mirrors the EUR formula in ``enrichment`` term for term — gross plus the
    commission, the commission being signed as a cost (positive when charged, so
    a rebate lowers the basis) — so the two costs on a lot can only ever be the
    same amount in two currencies, never two different amounts. The gross already
    carries an option's multiplier: ``domain_event_factory`` applies it when it
    computes ``gross_amount_foreign_currency``, which is why the price is not
    re-multiplied here.

    Gives up rather than guessing when:

    * the gross or the trade currency is missing;
    * the commission was billed in a **different** currency than the trade.
      Adding it would silently mix two currencies into one figure; dropping it
      would produce a cost that disagrees with the EUR one by the fee. Neither is
      a number worth showing, so the lot reports no original cost and callers
      fall back to EUR.
    """
    gross = getattr(trade_event, "gross_amount_foreign_currency", None)
    currency = getattr(trade_event, "local_currency", None)
    if gross is None or not currency:
        return None, None

    commission = getattr(trade_event, "commission_foreign_currency", None) or Decimal(0)
    commission_currency = getattr(trade_event, "commission_currency", None)
    if commission != Decimal(0) and (commission_currency or "").upper() != currency.upper():
        return None, None

    # The transaction tax is always in the trade's own currency.
    tax = getattr(trade_event, "transaction_tax_foreign", None) or Decimal(0)
    return gross.copy_abs() + commission + tax, currency.upper()


def _long_lot_sort_key(lot: "FifoLot"):
    """Consumption order for long lots.

    ``acquisition_date`` first — that is the FIFO queue. ``holding_period_start``
    second, because every lot carried in by one merger shares a single
    acquisition date, which makes the old two-part key degenerate across the
    whole carried block and leaves their relative order resting on sort
    stability, which the next unrelated insertion silently rebuilds. A no-op for
    any lot that was actually bought: the two dates are equal unless carried.
    """
    return (
        parse_ibkr_date(lot.acquisition_date) or datetime.min.date(),
        parse_ibkr_date(lot.holding_period_start) or datetime.min.date(),
        numeric_tx_sort_key(lot.source_transaction_id),
    )


def plan_key(lot: "FifoLot") -> str:
    """How a filed pairing plan names a lot.

    The IBKR TransactionID of its purchase -- except for a lot carried in by
    a merger, which carries the merger's id like every other lot of that
    merger; its ``carried_from`` (old asset + the original purchase) tells
    it apart. Used where a plan is written and where it is replayed, so the
    two cannot name lots differently.
    """
    return str(lot.carried_from or lot.source_transaction_id)


def _lot_cost_date(lot: "FifoLot") -> str:
    """The day a lot's cost was paid: where its holding began."""
    return lot.holding_period_start or lot.acquisition_date


def _components_of(lot: "FifoLot") -> List[Tuple[str, Decimal]]:
    """A lot's dated cost components; one component on its cost date if none."""
    if lot.cost_components is not None:
        return list(lot.cost_components)
    return [(_lot_cost_date(lot), lot.total_cost_basis_eur)]


def _merge_components(parts) -> List[Tuple[str, Decimal]]:
    """Add components of the same date together, ordered by date."""
    merged: dict = {}
    for day, amount in parts:
        merged[day] = merged.get(day, Decimal(0)) + amount
    return sorted(merged.items())


def _scale_components(components: List[Tuple[str, Decimal]], target: Decimal,
                      ctx: Context) -> List[Tuple[str, Decimal]]:
    """*components* rescaled so they add up EXACTLY to *target*.

    Proportional to their amounts; the last component takes the remainder,
    so rounding never leaves the parts disagreeing with the whole.
    """
    if not components:
        return []
    # Every part but the last is put on a grid no finer than the target's
    # own last digit, so the parts, their running sum and the remainder all
    # fit the working precision without rounding: the last part is then the
    # exact remainder and the parts add up to the target to the last digit.
    wide = Context(prec=2 * ctx.prec + 10)
    grid = Decimal(1).scaleb(min(target.as_tuple().exponent, -12))
    base = sum((amount for _d, amount in components), Decimal(0))
    scaled: List[Tuple[str, Decimal]] = []
    running = Decimal(0)
    for day, amount in components[:-1]:
        part = (wide.divide(wide.multiply(amount, target), base)
                if base != Decimal(0) else Decimal(0))
        part = part.quantize(grid, context=wide)
        scaled.append((day, part))
        running = wide.add(running, part)
    scaled.append((components[-1][0], wide.subtract(target, running)))
    return scaled


def _allocate_rescaled_quantities(
    quantities: List[Decimal],
    ratio: Decimal,
    order_keys: List[Any],
    ctx: Context,
) -> List[Decimal]:
    """Rescale *quantities* by *ratio*, on the 1e-8 grid, summing exactly.

    Quantising each lot independently drifts from the true total in both
    directions — three 100-share lots at ratio 1/3 floor to 99.99999999, and
    100+100+80 at 3/7 overshoot to 120.00000001. A shortfall is the dangerous
    one: the next full-position sale finds less than it needs, and
    ``consume_long_lots_for_sale`` tolerates 1e-10 before raising, which aborts
    the whole run.

    So the target is computed once for the transfer as a whole, each lot takes
    its floor, and the remaining 1e-8 units are handed out by largest fractional
    remainder — ties, and therefore the residual, going to the oldest holding so
    the result is deterministic rather than dependent on dict order.
    """
    pq = global_config.PRECISION_QUANTITY
    target = ctx.multiply(sum(quantities, Decimal(0)), ratio).quantize(pq, context=ctx)
    if target != target.to_integral_value():
        # A broker credits whole shares and pays cash in lieu of the fraction,
        # which is a separate disposal of the fractional claim — a figure this
        # event does not carry (its gross is pinned to zero at parse time).
        # Rounding the fraction away would destroy real basis; inventing a
        # zero-proceeds disposal would invent a loss. Refuse and say why.
        # Tested on the SUM, never per lot: two 3-share lots at ratio 2.5 give
        # 7.5 + 7.5 while the account is credited 15 whole shares.
        raise ValueError(
            f"Carrying {sum(quantities, Decimal(0))} share(s) at ratio {ratio} "
            f"credits {target} shares — not a whole number. The broker credits "
            "whole shares and pays cash in lieu of the fraction; that fraction is "
            "a separate disposal of the fractional claim and is not implemented. "
            "Record it manually — see docs/cz-tax-policy.md (Mergers)."
        )

    floors = [ctx.multiply(q, ratio).quantize(pq, rounding=ROUND_DOWN) for q in quantities]
    remainders = [
        ctx.multiply(q, ratio) - f for q, f in zip(quantities, floors)
    ]
    deficit = target - sum(floors, Decimal(0))

    max_deficit = pq * len(quantities)
    if deficit < Decimal(0) or deficit > max_deficit:
        raise ValueError(
            f"Rescaling {len(quantities)} lot(s) by ratio {ratio} left a residual "
            f"of {deficit}, outside the expected [0, {max_deficit}] — refusing to "
            f"guess how to distribute it."
        )

    # Largest remainder first; the oldest holding wins a tie.
    units = int((deficit / pq).to_integral_value())
    ranked = sorted(
        range(len(quantities)),
        key=lambda i: (-remainders[i], order_keys[i]),
    )
    result = list(floors)
    for i in ranked[:units]:
        result[i] = result[i] + pq
    return result


def split_position_flip_event(event: TradeEvent,
                             available_long_qty: Decimal,
                             available_short_qty: Decimal) -> List[TradeEvent]:
    """Split a "C;O" flip trade into its close and open halves.

    One IBKR trade both closes the existing position and opens the opposite
    one. ``consume_long_lots_for_sale`` / ``consume_short_lots_for_cover``
    handle that inline for the tax year, but only there: in historical
    replay the remainder raises UserWarning, which marks the whole
    reconstruction inconsistent and discards it. Splitting the event up
    front against the ledger's current quantities lets replay apply both
    halves with the real trade date and cost.

    Monetary amounts are split proportionally by quantity; the per-unit
    price is unchanged. Returns the event unchanged when it is not a flip
    or when the split cannot be decided.

    Ported from upstream f5228fd (uebber/ibkr-german-tax-declaration-engine).
    """
    if not getattr(event, "allows_position_flip", False):
        return [event]
    if event.quantity is None or event.quantity == Decimal(0):
        return [event]

    abs_qty = event.quantity.copy_abs()

    if event.event_type == FinancialEventType.TRADE_SELL_LONG:
        close_qty = min(abs_qty, available_long_qty)
        close_type = FinancialEventType.TRADE_SELL_LONG
        open_type = FinancialEventType.TRADE_SELL_SHORT_OPEN
    elif event.event_type == FinancialEventType.TRADE_BUY_SHORT_COVER:
        close_qty = min(abs_qty, available_short_qty)
        close_type = FinancialEventType.TRADE_BUY_SHORT_COVER
        open_type = FinancialEventType.TRADE_BUY_LONG
    else:
        logger.warning(
            f"Position flip event {event.event_id} has unexpected type "
            f"{event.event_type.name}. Processing as-is."
        )
        return [event]

    if close_qty < Decimal(0):
        close_qty = Decimal(0)
    open_qty = abs_qty - close_qty

    def _scaled(value: Optional[Decimal], ratio: Decimal) -> Optional[Decimal]:
        return value * ratio if value is not None else None

    def _make_sub_event(sub_type: FinancialEventType,
                        sub_abs_qty: Decimal) -> TradeEvent:
        ratio = sub_abs_qty / abs_qty
        # Sign convention: buys positive, sells negative.
        if sub_type in (FinancialEventType.TRADE_BUY_LONG,
                        FinancialEventType.TRADE_BUY_SHORT_COVER):
            signed_qty = sub_abs_qty
        else:
            signed_qty = -sub_abs_qty
        return TradeEvent(
            asset_internal_id=event.asset_internal_id,
            event_date=event.event_date,
            event_type=sub_type,
            quantity=signed_qty,
            price_foreign_currency=event.price_foreign_currency,
            commission_foreign_currency=_scaled(event.commission_foreign_currency, ratio),
            commission_currency=event.commission_currency,
            commission_eur=_scaled(event.commission_eur, ratio),
            transaction_tax_foreign=_scaled(event.transaction_tax_foreign, ratio),
            transaction_tax_eur=_scaled(event.transaction_tax_eur, ratio),
            net_proceeds_or_cost_basis_eur=_scaled(event.net_proceeds_or_cost_basis_eur, ratio),
            related_option_event_id=None,  # a flip never arises from an option exercise
            local_currency=event.local_currency,
            gross_amount_foreign_currency=_scaled(event.gross_amount_foreign_currency, ratio),
            gross_amount_eur=_scaled(event.gross_amount_eur, ratio),
            ibkr_transaction_id=event.ibkr_transaction_id,
            ibkr_activity_description=event.ibkr_activity_description,
            ibkr_notes_codes=event.ibkr_notes_codes,
        )

    results: List[TradeEvent] = []
    if close_qty > Decimal(0):
        results.append(_make_sub_event(close_type, close_qty))
    if open_qty > Decimal(0):
        results.append(_make_sub_event(open_type, open_qty))

    if not results:
        logger.warning(
            f"Position flip event {event.event_id}: both close and open "
            f"quantities are zero. Skipping."
        )
        return []

    logger.info(
        f"Split position flip {event.ibkr_transaction_id or event.event_id}: "
        f"{close_type.name}({close_qty}) + {open_type.name}({open_qty}) "
        f"from total {abs_qty}."
    )
    return results


@dataclass
class FifoLot:
    acquisition_date: str  # YYYY-MM-DD — when THIS security was acquired
    quantity: Decimal # Represents shares/units OR contracts for options
    unit_cost_basis_eur: Decimal # Renamed from cost_basis_eur_per_unit
    total_cost_basis_eur: Decimal # Stored with high precision
    source_transaction_id: str # IBKR Transaction ID (or fallback string like "SOY_FALLBACK")

    # When the holding period began, which is NOT always the acquisition date.
    # A qualified Czech merger (§23b/§23c) hands the new share the old share's
    # running holding period, while the regime that period is measured under is
    # still chosen by when THIS share was acquired — the pre-2014 six-month test
    # does not transfer to a share issued later (NSS 3 Afs 249/2024-45). One
    # field cannot answer both questions, so there are two.
    #
    # None means "same as acquisition_date"; __post_init__ fills it in, so every
    # consumer can read it without remembering an `or`.
    holding_period_start: Optional[str] = None

    # Whether each date is real or synthetic. Derived from the source id today,
    # but stored rather than re-sniffed: the moment a merger mints a lot under a
    # corporate-action id, every `startswith("SOY_FALLBACK")` check elsewhere
    # would stop recognising an estimated date and silently grant an exemption.
    acquisition_date_estimated: bool = False
    holding_period_start_estimated: bool = False

    # Free-text origin of a lot that arrived by transfer rather than purchase,
    # e.g. "OLDCO:1234567890". Satisfies the requirement to preserve each lot's
    # virtual origin without smuggling anything into source_transaction_id,
    # which the FIFO tie-break compares. Read by nothing that computes a figure.
    carried_from: Optional[str] = None

    # What the lot cost in the currency it was actually bought in. The engine
    # computes everything in EUR (it began as a German tool), so a USD holding
    # otherwise reports a cost the owner never paid, in a currency they never
    # touched. The original is carried alongside for display and audit only —
    # NOTHING that computes a tax figure may read these two fields.
    #
    # The TOTAL is stored rather than a unit price, deliberately: it is what the
    # EUR side treats as authoritative too (`unit` is derived as total /
    # quantity), so every proportional change — a partial sale, a split rescale,
    # a carry-over — keeps the two in step by construction, instead of needing a
    # mirrored edit at each of the dozen sites that touch the basis.
    #
    # ``None`` wherever no single original amount exists: a weighted-average
    # re-pricing, a lot valued in EUR by a taxable merger, a zero-cost stock
    # dividend, or a trade whose commission was billed in a third currency.
    # Callers fall back to the EUR figure; a wrong number would be worse.
    total_cost_original: Optional[Decimal] = None
    cost_currency: Optional[str] = None

    # When the money behind total_cost_basis_eur was paid: ((YYYY-MM-DD, EUR),
    # ...) adding up exactly to the total. None means all of it was paid on
    # the lot's cost date (holding_period_start, else acquisition_date) --
    # every bought lot. A weighted-average sale re-prices the surviving lots to
    # a blend of purchases made on different days; a country that converts
    # the cost leg at the rate of the day it was paid (CZ §10) needs to know
    # which money is whose, or it converts the whole average at one lot's
    # rate (audit W08).
    cost_components: Optional[List[Tuple[str, Decimal]]] = None

    # Bookkeeping of the ledger that holds the lot, not part of its value:
    # lot_id names the lot within its ledger (it survives partial sales, so
    # a consumed part can be traced back to the lot), sequence is the
    # ledger's operation counter when the lot was booked -- the order of
    # trades within one day, which a date cannot give (audit W07). Both are
    # 0 for a lot no tax-year bookkeeping has stamped (start-of-year lots
    # carry sequence 0 and their own ids).
    lot_id: int = field(default=0, compare=False, repr=False)
    sequence: int = field(default=0, compare=False, repr=False)

    @property
    def unit_cost_original(self) -> Optional[Decimal]:
        """Per-unit original cost, or None when the total is unknown."""
        if self.total_cost_original is None or not self.quantity:
            return None
        return self.total_cost_original / self.quantity

    def rescaled_original(self, new_quantity: Decimal) -> Optional[Decimal]:
        """The original total belonging to ``new_quantity`` of this lot.

        Used when a lot is partially consumed: the EUR total is recomputed as
        unit x remaining, so the original has to shrink by the same ratio or the
        two costs drift apart on every partial sale.
        """
        if self.total_cost_original is None or not self.quantity:
            return None
        return self.total_cost_original * (new_quantity / self.quantity)

    def __post_init__(self):
        if (self.total_cost_original is not None) != bool(self.cost_currency):
            raise ValueError(
                "FifoLot.total_cost_original and cost_currency must be set "
                f"together — got {self.total_cost_original!r} / "
                f"{self.cost_currency!r}. An amount without its currency is "
                "not a cost, and a currency without an amount reads as zero."
            )
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity <= Decimal(0):
            raise ValueError(f"FifoLot quantity must be a positive finite Decimal: {self.quantity} (type: {type(self.quantity)})")
        if not isinstance(self.unit_cost_basis_eur, Decimal) or not self.unit_cost_basis_eur.is_finite() or self.unit_cost_basis_eur < Decimal(0): # Renamed
            raise ValueError(f"FifoLot unit_cost_basis_eur must be a non-negative finite Decimal: {self.unit_cost_basis_eur}") # Renamed
        if not isinstance(self.total_cost_basis_eur, Decimal) or not self.total_cost_basis_eur.is_finite() or self.total_cost_basis_eur < Decimal(0):
            raise ValueError(f"FifoLot total_cost_basis_eur must be a non-negative finite Decimal: {self.total_cost_basis_eur}")
        if not self.source_transaction_id:
             raise ValueError(f"FifoLot requires a non-empty source_transaction_id.")

        # A synthetic SOY fallback lot (31 Dec of the prior year) has no real
        # purchase date. Derive the flag here so it is carried on the lot from
        # now on instead of being re-sniffed from the id at every reader.
        if str(self.source_transaction_id).startswith(SOY_FALLBACK_PREFIX):
            self.acquisition_date_estimated = True

        if not self.holding_period_start:
            self.holding_period_start = self.acquisition_date
            self.holding_period_start_estimated = self.acquisition_date_estimated
        elif parse_ibkr_date(self.holding_period_start) is None:
            # The acquisition date gets this guard at every consumption site; an
            # unparseable carried date must fail here too rather than silently
            # sorting as datetime.min and reading as an ancient holding.
            raise ValueError(
                f"FifoLot {self.source_transaction_id}: unparseable "
                f"holding_period_start '{self.holding_period_start}'."
            )

        ctx_check = Context(prec=get_global_context().prec)
        expected_total = ctx_check.multiply(self.quantity, self.unit_cost_basis_eur) # Renamed

        places_total = abs(global_config.OUTPUT_PRECISION_AMOUNTS.as_tuple().exponent) # Renamed
        places_unit = abs(global_config.OUTPUT_PRECISION_PER_SHARE.as_tuple().exponent) # Renamed
        tolerance_exponent = min(places_total, places_unit) - 1
        tolerance = Decimal('1e-' + str(tolerance_exponent))

        if abs(self.total_cost_basis_eur - expected_total) > tolerance and expected_total != Decimal(0):
             logger.warning(
                 f"FifoLot {self.source_transaction_id}: total_cost_basis_eur {self.total_cost_basis_eur} "
                 f"differs significantly from (quantity {self.quantity} * unit_cost_basis_eur {self.unit_cost_basis_eur} = {expected_total}). " # Renamed
                 f"Difference: {self.total_cost_basis_eur - expected_total}. Using provided total_cost_basis_eur."
             )

@dataclass
class ShortFifoLot:
    opening_date: str  # YYYY-MM-DD
    quantity_shorted: Decimal # Represents shares/units OR contracts for options (always positive)
    unit_sale_proceeds_eur: Decimal # Renamed from sale_proceeds_eur_per_unit
    total_sale_proceeds_eur: Decimal # Total sale proceeds when shorted
    source_transaction_id: str # IBKR Transaction ID (or fallback string like "SOY_FALLBACK_SHORT")

    # No second date here on purpose: a short position can never pass the
    # holding-period test (the sale precedes the purchase), so evaluate_time_test
    # short-circuits on is_short_position before any date is read. Only the
    # estimated flag is stored, to replace the prefix sniffing at the RGL site.
    opening_date_estimated: bool = False

    # Free-text origin of a lot that arrived by transfer rather than purchase,
    # e.g. "OLDCO:1234567890". Satisfies the requirement to preserve each lot's
    # virtual origin without smuggling anything into source_transaction_id,
    # which the FIFO tie-break compares. Read by nothing that computes a figure.
    carried_from: Optional[str] = None

    def __post_init__(self):
        if str(self.source_transaction_id).startswith(SOY_FALLBACK_PREFIX):
            self.opening_date_estimated = True
        if not isinstance(self.quantity_shorted, Decimal) or not self.quantity_shorted.is_finite() or self.quantity_shorted <= Decimal(0):
            raise ValueError(f"ShortFifoLot quantity_shorted must be a positive finite Decimal: {self.quantity_shorted}")
        # Net proceeds may be negative once the sale's own costs exceed its
        # price (writing a near-worthless option); the cover must keep that
        # loss. Quantity stays a positive magnitude; proceeds do not
        # (upstream 3541de7).
        if not isinstance(self.unit_sale_proceeds_eur, Decimal) or not self.unit_sale_proceeds_eur.is_finite():
            raise ValueError(f"ShortFifoLot unit_sale_proceeds_eur must be a finite Decimal: {self.unit_sale_proceeds_eur}")
        if not isinstance(self.total_sale_proceeds_eur, Decimal) or not self.total_sale_proceeds_eur.is_finite():
            raise ValueError(f"ShortFifoLot total_sale_proceeds_eur must be a finite Decimal: {self.total_sale_proceeds_eur}")
        if not self.source_transaction_id:
            raise ValueError(f"ShortFifoLot requires a non-empty source_transaction_id.")

        ctx_check = Context(prec=get_global_context().prec)
        expected_total = ctx_check.multiply(self.quantity_shorted, self.unit_sale_proceeds_eur) # Renamed

        places_total = abs(global_config.OUTPUT_PRECISION_AMOUNTS.as_tuple().exponent) # Renamed
        places_unit = abs(global_config.OUTPUT_PRECISION_PER_SHARE.as_tuple().exponent) # Renamed
        tolerance_exponent = min(places_total, places_unit) - 1
        tolerance = Decimal('1e-' + str(tolerance_exponent))

        if abs(self.total_sale_proceeds_eur - expected_total) > tolerance and expected_total != Decimal(0):
            logger.warning(
                f"ShortFifoLot {self.source_transaction_id}: total_sale_proceeds_eur {self.total_sale_proceeds_eur} "
                f"differs significantly from (quantity {self.quantity_shorted} * unit_sale_proceeds_eur {self.unit_sale_proceeds_eur} = {expected_total}). " # Renamed
                f"Difference: {self.total_sale_proceeds_eur - expected_total}. Using provided total_sale_proceeds_eur."
            )

@dataclass
class TaxYearSale:
    """A long sale of the tax year as the ledger booked it (journal entry)."""
    event_id: Any
    ibkr_transaction_id: Optional[str]
    sequence: int
    sale_date: str
    quantity: Decimal
    unit_proceeds_eur: Decimal


@dataclass
class ConsumedPortion:
    """Exactly the part of a lot one tax-year sale consumed (journal entry).

    ``lot`` is a snapshot of the lot holding the consumed quantity only, with
    the lot's own id, sequence, dates and flags and its cost scaled to that
    quantity -- its OWN cost, not a weighted average.
    """
    sale_sequence: int
    lot: "FifoLot"


@dataclass
class ConsumedLotDetail:
    consumed_quantity: Decimal
    value_per_unit_eur: Decimal # Cost basis per unit for long, proceeds per unit for short
    original_lot_date: str # Acquisition date for long, opening date for short
    original_lot_source_tx_id: str


class FifoLedger:
    def __init__(self,
                 asset_internal_id: uuid.UUID,
                 asset_category: AssetCategory,
                 asset_multiplier_from_asset: Optional[Decimal],
                 currency_converter: CurrencyConverter,
                 exchange_rate_provider: ECBExchangeRateProvider,
                 internal_working_precision: int, # Will be renamed internal_calculation_precision where called
                 decimal_rounding_mode: str,
                 fund_type: Optional[InvestmentFundType] = None,
                 tax_classifier: _TaxClassifierCallable = None,
                 pairing_method: PairingMethod = PairingMethod.FIFO,
                 pairing_history: Optional[PairingHistory] = None):
        self.asset_internal_id: uuid.UUID = asset_internal_id
        self.asset_category: AssetCategory = asset_category
        self.fund_type: Optional[InvestmentFundType] = fund_type
        # Lot-matching strategy for disposals. FIFO (default) preserves the
        # existing DE + CZ behaviour; LIFO / weighted-average vary the CZ §10
        # cost basis / time-test outcome. OPTIMAL behaves as FIFO here (the
        # global solver produces its RGLs separately).
        self.pairing_method: PairingMethod = coerce_pairing_method(pairing_method)
        # How each earlier year was filed, for the start-of-year replay. None
        # means every earlier year was filed FIFO. Option contracts are always
        # matched FIFO, whatever a year was filed with, so their ledger never
        # reads a history.
        self.pairing_history: Optional[PairingHistory] = (
            None if asset_category == AssetCategory.OPTION else pairing_history
        )
        # Set while replaying history when a disposal of an earlier year was
        # matched by anything other than FIFO. The broker's lot snapshot and
        # every other fallback follow the broker's own FIFO matching, so after
        # such a year a fallback cannot be trusted to name the right lots.
        self._replayed_non_fifo_disposal: bool = False
        # True when the start-of-year lots came from a fallback after a
        # non-FIFO year: their dates and costs are flagged for review.
        self.soy_lots_unverified: bool = False
        # Tax-year bookkeeping (audit W05/W07). Every lot booked and every
        # long sale gets the next value of one operation counter, so the
        # order of the day survives; the journals record each tax-year sale
        # and exactly which part of which lot it consumed, so a country layer
        # can re-match the sales and rebuild the open lots from its answer.
        self._operation_counter: int = 0
        self._last_lot_id: int = 0
        self.tax_year_sales: List[TaxYearSale] = []
        self.consumed_portions: List[ConsumedPortion] = []
        # Trades excluded from FIFO because enrichment left their EUR value
        # None (missing FX rate). Each exclusion silently removes a taxable
        # trade from the results — the engine surfaces the total at the end.
        self.dropped_unenriched_events: int = 0

        if self.asset_category == AssetCategory.INVESTMENT_FUND and self.fund_type is None:
            logger.warning(f"FifoLedger for Investment Fund {asset_internal_id} initialized without a specific fund_type. Defaulting to InvestmentFundType.NONE. This may impact tax calculations if not intended.")
            self.fund_type = InvestmentFundType.NONE


        self.asset_multiplier_info: Optional[Decimal] = None
        if asset_multiplier_from_asset is not None:
            multiplier_dec = safe_decimal(asset_multiplier_from_asset)
            if multiplier_dec is not None and multiplier_dec > Decimal(0):
                self.asset_multiplier_info = multiplier_dec
            elif self.asset_category == AssetCategory.OPTION:
                 logger.warning(f"FifoLedger for Option asset {asset_internal_id} initialized with invalid asset_multiplier_from_asset ({asset_multiplier_from_asset}). Storing as is, but typically should be > 0.")
                 self.asset_multiplier_info = multiplier_dec if multiplier_dec is not None else Decimal(100)

        self.lots: List[FifoLot] = []
        self.short_lots: List[ShortFifoLot] = []
        self.currency_converter: CurrencyConverter = currency_converter
        self.exchange_rate_provider: ECBExchangeRateProvider = exchange_rate_provider

        self.ctx = Context(prec=internal_working_precision, rounding=decimal_rounding_mode)
        self._tax_classifier: _TaxClassifierCallable = tax_classifier
        self.soy_fallback_lot_source_tx_id = f"SOY_FALLBACK_{asset_internal_id}"
        self.soy_fallback_short_lot_source_tx_id = f"SOY_FALLBACK_SHORT_{asset_internal_id}"


    def initialize_lots_from_soy(self,
                                 asset: Asset,
                                 all_historical_events_for_asset: List[FinancialEvent],
                                 tax_year: int):

        if self.asset_category == AssetCategory.INVESTMENT_FUND:
            asset_fund_type = getattr(asset, 'fund_type', None)
            if isinstance(asset_fund_type, InvestmentFundType) and asset_fund_type != InvestmentFundType.NONE:
                 if self.fund_type == InvestmentFundType.NONE:
                     logger.info(f"Updating FifoLedger fund_type for {self.asset_internal_id} from SOY asset object to {asset_fund_type}.")
                     self.fund_type = asset_fund_type
            elif self.fund_type is None:
                 logger.warning(f"FifoLedger for Investment Fund {self.asset_internal_id} still has no specific fund_type after asset load for SOY. Using InvestmentFundType.NONE.")
                 self.fund_type = InvestmentFundType.NONE

        self.lots.clear()
        self.short_lots.clear()
        historical_simulation_inconsistent = False
        self._replayed_non_fifo_disposal = False
        self.soy_lots_unverified = False
        # Whether the opening lots came from somewhere other than a clean
        # replay (a fallback, or a replay trimmed to the broker's quantity).
        soy_lots_from_fallback = False

        logger.info(f"Asset {asset.get_classification_key()} (ID: {asset.internal_asset_id}): Initializing SOY. "
                    f"Processing {len(all_historical_events_for_asset)} historical events for simulation.")

        for hist_event in all_historical_events_for_asset:
            event_date_obj = parse_ibkr_date(hist_event.event_date)
            if not event_date_obj or event_date_obj >= date_obj(tax_year, 1, 1):
                logger.warning(f"Historical event {hist_event.event_id} for asset {asset.internal_asset_id} "
                               f"has date {hist_event.event_date} which is not before tax year {tax_year}. Skipping for SOY init.")
                continue

            try:
                if isinstance(hist_event, TradeEvent):
                    # A "C;O" flip is split against the ledger's current
                    # quantities: the inline remainder handling in the consume
                    # methods is disabled during replay, so without this the
                    # flip would raise UserWarning and discard the whole
                    # reconstruction.
                    sub_events = split_position_flip_event(
                        hist_event,
                        sum((lot.quantity for lot in self.lots), Decimal(0)),
                        sum((lot.quantity_shorted for lot in self.short_lots), Decimal(0)),
                    )
                    # Each earlier year is replayed with the method it was
                    # FILED with, not with this run's method (V04).
                    year_method = self._filed_method_for(event_date_obj.year)
                    for sub_event in sub_events:
                        if sub_event.event_type == FinancialEventType.TRADE_BUY_LONG:
                            self.add_long_lot(sub_event)
                        elif sub_event.event_type == FinancialEventType.TRADE_SELL_LONG:
                            plan = None
                            if year_method == PairingMethod.OPTIMAL and self.pairing_history is not None:
                                plan = self.pairing_history.plan_for(
                                    event_date_obj.year, sub_event.ibkr_transaction_id)
                            self.consume_long_lots_for_sale(
                                sub_event, is_historical_simulation=True,
                                method=year_method, plan=plan)
                        elif sub_event.event_type == FinancialEventType.TRADE_SELL_SHORT_OPEN:
                            self.add_short_lot(sub_event)
                        elif sub_event.event_type == FinancialEventType.TRADE_BUY_SHORT_COVER:
                            self.consume_short_lots_for_cover(
                                sub_event, is_historical_simulation=True,
                                method=year_method)
                elif isinstance(hist_event, CorpActionSplitForward):
                    self.adjust_lots_for_split(hist_event)
                elif isinstance(hist_event, CorpActionStockDividend):
                     self.add_lot_for_stock_dividend(hist_event)
                elif isinstance(hist_event, OptionLifecycleEvent) and self.asset_category == AssetCategory.OPTION:
                    # Exercised/assigned/expired contracts left the position
                    # before SOY — without consuming them the reconstructed
                    # option quantity is overstated and falls back needlessly.
                    try:
                        qty = hist_event.quantity_contracts
                        if isinstance(hist_event, OptionExerciseEvent):
                            self.consume_long_option_get_cost(qty)
                        elif isinstance(hist_event, OptionAssignmentEvent):
                            self.consume_short_option_get_proceeds(qty)
                        elif isinstance(hist_event, OptionExpirationWorthlessEvent):
                            if sum(l.quantity for l in self.lots) >= qty:
                                self.consume_long_option_get_cost(qty)
                            elif sum(l.quantity_shorted for l in self.short_lots) >= qty:
                                self.consume_short_option_get_proceeds(qty)
                            else:
                                raise ValueError(
                                    f"insufficient option lots for historical expiration of {qty} contracts"
                                )
                    except ValueError as ve:
                        logger.warning(
                            f"Historical simulation: option lifecycle event {hist_event.event_id} "
                            f"could not be applied: {ve}"
                        )
                        historical_simulation_inconsistent = True
            except UserWarning as uw:
                logger.warning(f"Historical simulation warning for asset {asset.internal_asset_id} processing event {hist_event.event_id}: {uw}")
                historical_simulation_inconsistent = True


        reconstructed_long_lots_snapshot = list(self.lots)
        reconstructed_short_lots_snapshot = list(self.short_lots)
        self.lots.clear()
        self.short_lots.clear()

        reconstructed_total_long_qty = sum(lot.quantity for lot in reconstructed_long_lots_snapshot)
        reconstructed_total_short_qty_abs = sum(lot.quantity_shorted for lot in reconstructed_short_lots_snapshot)
        reconstructed_net_qty = self.ctx.subtract(reconstructed_total_long_qty, reconstructed_total_short_qty_abs)

        reported_soy_qty = asset.soy_quantity # Renamed from initial_quantity_soy
        if reported_soy_qty is None:
            logger.warning(f"Asset {asset.get_classification_key()}: SOY quantity from positions report is None. Assuming 0 for ledger initialization.")
            reported_soy_qty = Decimal(0)
        else:
            reported_soy_qty = reported_soy_qty.quantize(global_config.PRECISION_QUANTITY, context=self.ctx)

        logger.info(f"Asset {asset.get_classification_key()}: Reconstructed SOY Qty: {reconstructed_net_qty}. Reported SOY Qty: {reported_soy_qty}. Historical Sim Inconsistent: {historical_simulation_inconsistent}")

        if reported_soy_qty == Decimal(0):
            logger.info(f"Asset {asset.get_classification_key()}: Reported SOY quantity is 0. Initializing with no lots.")
            self._reset_bookkeeping()
            return

        use_fallback = historical_simulation_inconsistent

        if not use_fallback:
            if reported_soy_qty > Decimal(0):
                if reconstructed_total_long_qty >= reported_soy_qty and reconstructed_total_short_qty_abs == Decimal(0):
                    logger.info(f"Asset {asset.get_classification_key()}: Using reconstructed FIFO long lots and costs.")
                    qty_to_assign = reported_soy_qty
                    lots_source = reconstructed_long_lots_snapshot
                    excess_qty = reconstructed_total_long_qty - reported_soy_qty
                    if excess_qty > Decimal(0):
                        # More reconstructed history than the reported SOY position:
                        # an unreported historical disposal likely consumed lots —
                        # per FIFO it would have consumed the OLDEST ones, so the
                        # surviving position consists of the NEWEST lots. Assign
                        # from the end (the final sort restores chronology).
                        logger.warning(
                            f"Asset {asset.get_classification_key()}: reconstructed long qty "
                            f"{reconstructed_total_long_qty} exceeds reported SOY {reported_soy_qty} "
                            f"by {excess_qty} — an unreported historical disposal is likely. "
                            "Keeping the NEWEST lots per FIFO; review acquisition dates/costs."
                        )
                        lots_source = list(reversed(reconstructed_long_lots_snapshot))
                        soy_lots_from_fallback = True
                    for lot in lots_source:
                        if qty_to_assign <= Decimal(0): break
                        qty_from_this_lot = min(lot.quantity, qty_to_assign)
                        final_lot = FifoLot(
                            acquisition_date=lot.acquisition_date, quantity=qty_from_this_lot,
                            unit_cost_basis_eur=lot.unit_cost_basis_eur, # Renamed
                            total_cost_basis_eur=self.ctx.multiply(qty_from_this_lot, lot.unit_cost_basis_eur), # Renamed
                            source_transaction_id=lot.source_transaction_id,
                            # Carry both dates and their flags: the rebuild
                            # re-creates the lot, so dropping them here would
                            # silently reset a carried-over holding.
                            holding_period_start=lot.holding_period_start,
                            acquisition_date_estimated=lot.acquisition_date_estimated,
                            holding_period_start_estimated=lot.holding_period_start_estimated,
                            # Same proportional trim the EUR total gets above.
                            # Dropping it here is what left 27 of the real book's
                            # 91 lots with no original cost while every unit test
                            # passed: this rebuild, not add_long_lot, is what
                            # produces the surviving lots of a prior year.
                            total_cost_original=lot.rescaled_original(qty_from_this_lot),
                            cost_currency=(lot.cost_currency
                                           if lot.total_cost_original is not None
                                           else None),
                        )
                        if lot.cost_components is not None:
                            final_lot.cost_components = _scale_components(
                                lot.cost_components, final_lot.total_cost_basis_eur, self.ctx)
                        self.lots.append(final_lot)
                        qty_to_assign -= qty_from_this_lot
                    if qty_to_assign.copy_abs() > Decimal('1e-8'):
                         logger.error(f"Asset {asset.get_classification_key()}: Mismatch after assigning sufficient long lots. Rem: {qty_to_assign}")
                         use_fallback = True
                else:
                    use_fallback = True

            elif reported_soy_qty < Decimal(0):
                reported_soy_qty_abs = reported_soy_qty.copy_abs()
                if reconstructed_total_short_qty_abs >= reported_soy_qty_abs and reconstructed_total_long_qty == Decimal(0):
                    logger.info(f"Asset {asset.get_classification_key()}: Using reconstructed FIFO short lots and proceeds.")
                    qty_to_assign = reported_soy_qty_abs
                    short_lots_source = reconstructed_short_lots_snapshot
                    excess_short_qty = reconstructed_total_short_qty_abs - reported_soy_qty_abs
                    if excess_short_qty > Decimal(0):
                        # Same reasoning as the long branch: an unreported cover
                        # would have closed the OLDEST short lots first.
                        logger.warning(
                            f"Asset {asset.get_classification_key()}: reconstructed short qty "
                            f"{reconstructed_total_short_qty_abs} exceeds reported SOY {reported_soy_qty_abs} "
                            f"by {excess_short_qty} — an unreported historical cover is likely. "
                            "Keeping the NEWEST short lots per FIFO; review opening dates/proceeds."
                        )
                        short_lots_source = list(reversed(reconstructed_short_lots_snapshot))
                        soy_lots_from_fallback = True
                    for lot in short_lots_source:
                        if qty_to_assign <= Decimal(0): break
                        qty_from_this_lot = min(lot.quantity_shorted, qty_to_assign)
                        final_short_lot = ShortFifoLot(
                            opening_date=lot.opening_date, quantity_shorted=qty_from_this_lot,
                            unit_sale_proceeds_eur=lot.unit_sale_proceeds_eur, # Renamed
                            total_sale_proceeds_eur=self.ctx.multiply(qty_from_this_lot, lot.unit_sale_proceeds_eur), # Renamed
                            source_transaction_id=lot.source_transaction_id
                        )
                        self.short_lots.append(final_short_lot)
                        qty_to_assign -= qty_from_this_lot
                    if qty_to_assign.copy_abs() > Decimal('1e-8'):
                         logger.error(f"Asset {asset.get_classification_key()}: Mismatch after assigning sufficient short lots. Rem: {qty_to_assign}")
                         use_fallback = True
                else:
                    use_fallback = True
            else:
                 use_fallback = True


        if use_fallback:
            soy_lots_from_fallback = True
            self.lots.clear()
            self.short_lots.clear()
            # Reconstruction from trade history alone was insufficient. Prefer
            # the lot-level SOY snapshot (real acquisition dates) when present
            # and complete; only if that is unavailable fall back to a single
            # estimated 31 Dec lot from the reported totals. The chosen branch
            # logs which path it took, so this message stays neutral.
            logger.warning(f"Asset {asset.get_classification_key()}: Historical FIFO reconstruction "
                           f"(Long: {reconstructed_total_long_qty}, Short: {reconstructed_total_short_qty_abs}, Inconsistent: {historical_simulation_inconsistent}) "
                           f"is insufficient or mismatched for reported SOY Qty ({reported_soy_qty}). "
                           f"Seeding SOY from the lot-level snapshot if available, else the reported totals.")
            if reported_soy_qty > Decimal(0):
                if not self._create_lots_from_soy_snapshot(asset, reported_soy_qty, tax_year, long_side=True):
                    self._create_fallback_long_lot(asset, reported_soy_qty, tax_year)
            elif reported_soy_qty < Decimal(0):
                if not self._create_lots_from_soy_snapshot(asset, reported_soy_qty.copy_abs(), tax_year, long_side=False):
                    self._create_fallback_short_lot(asset, reported_soy_qty.copy_abs(), tax_year)

        if soy_lots_from_fallback and self._replayed_non_fifo_disposal:
            # The fallbacks follow the broker's own matching, which is FIFO; an
            # earlier year filed another way may have left different lots. The
            # quantity is right, which lots they are is not established.
            for lot in self.lots:
                lot.acquisition_date_estimated = True
                lot.holding_period_start_estimated = True
            for short_lot in self.short_lots:
                short_lot.opening_date_estimated = True
            if self.lots or self.short_lots:
                self.soy_lots_unverified = True
                logger.warning(
                    f"Asset {asset.get_classification_key()}: an earlier year was filed "
                    "with a non-FIFO pairing method, and the start-of-year lots come from "
                    "a fallback that follows the broker's FIFO matching. Their dates and "
                    "costs are marked for review."
                )

        if self.lots:
            self.lots.sort(key=_long_lot_sort_key)
            if any((parse_ibkr_date(lot.acquisition_date) is None) for lot in self.lots):
                 raise ValueError(f"Unparseable acquisition date found in final SOY lots for asset {self.asset_internal_id}.")
        if self.short_lots:
            self.short_lots.sort(key=lambda lot: (parse_ibkr_date(lot.opening_date) or datetime.min.date(), numeric_tx_sort_key(lot.source_transaction_id)))
            if any((parse_ibkr_date(lot.opening_date) is None) for lot in self.short_lots):
                 raise ValueError(f"Unparseable opening date found in final SOY short lots for asset {self.asset_internal_id}.")
        self._reset_bookkeeping()

    def _reset_bookkeeping(self) -> None:
        """Start the tax year's bookkeeping: the opening lots get their own
        ids and sequence 0 (held before anything the year books), the
        journals start empty."""
        self._operation_counter = 0
        self._last_lot_id = 0
        for lot in self.lots:
            self._last_lot_id += 1
            lot.lot_id = self._last_lot_id
            lot.sequence = 0
        self.tax_year_sales = []
        self.consumed_portions = []

    def _create_lots_from_soy_snapshot(
        self,
        asset: Asset,
        quantity_abs: Decimal,
        tax_year: int,
        long_side: bool,
    ) -> bool:
        """Seed SOY lots from a lot-level positions snapshot (real dates).

        Used only when the trade-history reconstruction failed — snapshot
        lots carry the REAL acquisition dates (time test works), while the
        classic fallback collapses the position into one lot dated 31 Dec.
        Returns False (and seeds nothing) unless the snapshot is complete:
        every lot parseable, dated before the tax year, with a cost basis,
        and quantities summing exactly to the reported SOY position.
        """
        candidates = [
            lot for lot in getattr(asset, "soy_lots", [])
            if (lot.quantity > Decimal(0)) == long_side and lot.quantity != Decimal(0)
        ]
        if not candidates:
            return False

        side = "long" if long_side else "short"
        total = sum((lot.quantity.copy_abs() for lot in candidates), Decimal(0))
        if total.quantize(global_config.PRECISION_QUANTITY, context=self.ctx) != quantity_abs:
            logger.warning(
                f"Asset {asset.get_classification_key()}: SOY snapshot {side} lots sum to "
                f"{total}, reported SOY is {quantity_abs} — snapshot ignored, using single fallback lot."
            )
            return False

        prepared = []
        for lot in candidates:
            open_date_obj = parse_ibkr_date(lot.open_date)
            if open_date_obj is None or open_date_obj >= date_obj(tax_year, 1, 1):
                logger.warning(
                    f"Asset {asset.get_classification_key()}: SOY snapshot lot has invalid "
                    f"open date '{lot.open_date}' — snapshot ignored, using single fallback lot."
                )
                return False
            if lot.cost_basis_amount is None or not lot.cost_basis_currency:
                logger.warning(
                    f"Asset {asset.get_classification_key()}: SOY snapshot lot ({lot.open_date}) "
                    f"has no cost basis — snapshot ignored, using single fallback lot."
                )
                return False

            amount = self.ctx.create_decimal(lot.cost_basis_amount).copy_abs()
            if lot.cost_basis_currency.upper() == "EUR":
                amount_eur = amount
            else:
                # Convert at the lot's open date — the same rate a replayed
                # historical trade would have used; 1 Jan as a fallback.
                amount_eur = self.currency_converter.convert_to_eur(
                    original_amount=amount, original_currency=lot.cost_basis_currency,
                    date_of_conversion=open_date_obj,
                ) or self.currency_converter.convert_to_eur(
                    original_amount=amount, original_currency=lot.cost_basis_currency,
                    date_of_conversion=date_obj(tax_year, 1, 1),
                )
                if amount_eur is None:
                    logger.warning(
                        f"Asset {asset.get_classification_key()}: cannot convert SOY snapshot "
                        f"lot basis ({amount} {lot.cost_basis_currency}) — snapshot ignored."
                    )
                    return False
                amount_eur = self.ctx.create_decimal(amount_eur)
            prepared.append((lot, amount_eur))

        for index, (lot, amount_eur) in enumerate(prepared):
            qty = lot.quantity.copy_abs()
            unit = self.ctx.divide(amount_eur, qty) if qty != Decimal(0) else Decimal(0)
            source_id = f"SOY_SNAPSHOT_{index}_{self.asset_internal_id}"
            # IBKR's HoldingPeriodDateTime is a US-rules holding basis and is
            # never imported as a date. It still carries information: when it is
            # the only date available the acquisition is not established, and
            # when it disagrees with the lot open date the broker has applied an
            # adjustment (wash sale, §368 tacking) whose Czech counterpart is
            # unknown. Either way the lot is flagged rather than trusted, so the
            # time test routes it to manual review instead of deciding from a
            # date the data does not support.
            acq_estimated = bool(lot.open_date_is_holding_basis)
            basis_diverges = bool(
                lot.holding_period_date
                and not lot.open_date_is_holding_basis
                and lot.holding_period_date != lot.open_date
            )
            if long_side:
                self.lots.append(FifoLot(
                    acquisition_date=lot.open_date, quantity=qty,
                    unit_cost_basis_eur=unit, total_cost_basis_eur=amount_eur,
                    source_transaction_id=source_id,
                    acquisition_date_estimated=acq_estimated,
                    # The snapshot states the basis in its own currency, so this
                    # is the original itself, not a back-conversion. `amount_eur`
                    # above is what was derived FROM it.
                    total_cost_original=self.ctx.create_decimal(
                        lot.cost_basis_amount).copy_abs(),
                    cost_currency=lot.cost_basis_currency.upper(),
                    # Pass the start EXPLICITLY even though it equals the
                    # acquisition date: leaving it None takes the defaulting
                    # branch of __post_init__, which overwrites the flag below
                    # with acquisition_date_estimated and would silently drop a
                    # divergent-basis warning.
                    holding_period_start=lot.open_date,
                    holding_period_start_estimated=acq_estimated or basis_diverges,
                ))
            else:
                self.short_lots.append(ShortFifoLot(
                    opening_date=lot.open_date, quantity_shorted=qty,
                    unit_sale_proceeds_eur=unit, total_sale_proceeds_eur=amount_eur,
                    source_transaction_id=source_id,
                    # A short can never pass the time test, so the flag only
                    # matters for the cover RGL's audit trail.
                    opening_date_estimated=acq_estimated,
                ))
        logger.info(
            f"Asset {asset.get_classification_key()}: Seeded {len(prepared)} SOY {side} lot(s) "
            f"from the lot-level positions snapshot (real acquisition dates)."
        )
        return True

    def _create_fallback_long_lot(self, asset: Asset, quantity: Decimal, tax_year: int):
        if quantity <= Decimal(0): return
        total_cost_basis_eur: Decimal
        if asset.soy_cost_basis_amount is None or asset.soy_cost_basis_currency is None: # Renamed
            logger.error(f"Asset {asset.get_classification_key()} fallback SOY: Missing SOY cost basis. Creating zero-cost lot for Qty {quantity}.")
            total_cost_basis_eur = self.ctx.create_decimal(Decimal(0))
        else:
            total_cost_basis_soy_curr = self.ctx.create_decimal(asset.soy_cost_basis_amount) # Renamed
            cost_basis_currency = asset.soy_cost_basis_currency # Renamed
            if cost_basis_currency.upper() == "EUR":
                total_cost_basis_eur = total_cost_basis_soy_curr
            else:
                conversion_date_obj = date_obj(tax_year, 1, 1)
                converted_eur = self.currency_converter.convert_to_eur(
                    original_amount=total_cost_basis_soy_curr, original_currency=cost_basis_currency, date_of_conversion=conversion_date_obj
                )
                if converted_eur is None:
                    logger.error(f"Asset {asset.get_classification_key()} fallback SOY: Failed to convert SOY cost basis. Creating zero-cost lot for Qty {quantity}.")
                    total_cost_basis_eur = self.ctx.create_decimal(Decimal(0))
                else:
                    total_cost_basis_eur = self.ctx.create_decimal(converted_eur)
            if total_cost_basis_eur < Decimal(0):
                logger.warning(f"Asset {asset.get_classification_key()} fallback SOY: Reported total cost basis {total_cost_basis_eur} EUR is negative. Using 0 for Qty {quantity}.")
                total_cost_basis_eur = self.ctx.create_decimal(Decimal(0))
        cost_per_unit = self.ctx.divide(total_cost_basis_eur, quantity) if quantity != Decimal(0) else Decimal(0)
        acquisition_date_str = f"{tax_year-1}-12-31"
        # The statement's own currency and amount, when both survived the branches
        # above — a zeroed basis has no original to report. Tied to
        # total_cost_basis_eur being non-zero rather than to the inputs, so the
        # fallbacks that zero it out cannot leave a stale original behind.
        original_total = original_currency = None
        if (total_cost_basis_eur != Decimal(0)
                and asset.soy_cost_basis_amount is not None
                and asset.soy_cost_basis_currency):
            original_total = self.ctx.create_decimal(
                asset.soy_cost_basis_amount).copy_abs()
            original_currency = asset.soy_cost_basis_currency.upper()
        fallback_lot = FifoLot(
            acquisition_date=acquisition_date_str, quantity=quantity,
            unit_cost_basis_eur=cost_per_unit, total_cost_basis_eur=total_cost_basis_eur, # Renamed
            source_transaction_id=self.soy_fallback_lot_source_tx_id,
            total_cost_original=original_total,
            cost_currency=original_currency,
        )
        self.lots.append(fallback_lot)
        logger.info(
            f"Asset {asset.get_classification_key()}: Created fallback SOY long lot: "
            f"Qty: {fallback_lot.quantity}, Cost/Unit EUR: {fallback_lot.unit_cost_basis_eur}, Acq. Date: {fallback_lot.acquisition_date}" # Renamed
        )

    def _create_fallback_short_lot(self, asset: Asset, quantity_abs: Decimal, tax_year: int):
        if quantity_abs <= Decimal(0): return
        total_proceeds_eur: Decimal
        if asset.soy_cost_basis_amount is None or asset.soy_cost_basis_currency is None: # Renamed (using cost basis field for proceeds as per IBKR convention for short SOY)
            logger.error(f"Asset {asset.get_classification_key()} fallback SOY SHORT: Missing SOY proceeds. Creating zero-proceeds lot for Qty {quantity_abs}.")
            total_proceeds_eur = self.ctx.create_decimal(Decimal(0))
        else:
            total_proceeds_soy_curr = self.ctx.create_decimal(asset.soy_cost_basis_amount).copy_abs() # Renamed
            proceeds_currency = asset.soy_cost_basis_currency # Renamed
            if proceeds_currency.upper() == "EUR":
                total_proceeds_eur = total_proceeds_soy_curr
            else:
                conversion_date_obj = date_obj(tax_year, 1, 1)
                converted_eur = self.currency_converter.convert_to_eur(
                    original_amount=total_proceeds_soy_curr, original_currency=proceeds_currency, date_of_conversion=conversion_date_obj
                )
                if converted_eur is None:
                    logger.error(f"Asset {asset.get_classification_key()} fallback SOY SHORT: Failed to convert SOY proceeds. Creating zero-proceeds lot for Qty {quantity_abs}.")
                    total_proceeds_eur = self.ctx.create_decimal(Decimal(0))
                else:
                    total_proceeds_eur = self.ctx.create_decimal(converted_eur)
        proceeds_per_unit = self.ctx.divide(total_proceeds_eur, quantity_abs) if quantity_abs != Decimal(0) else Decimal(0)
        opening_date_str = f"{tax_year-1}-12-31"
        fallback_short_lot = ShortFifoLot(
            opening_date=opening_date_str, quantity_shorted=quantity_abs,
            unit_sale_proceeds_eur=proceeds_per_unit, total_sale_proceeds_eur=total_proceeds_eur, # Renamed
            source_transaction_id=self.soy_fallback_short_lot_source_tx_id
        )
        self.short_lots.append(fallback_short_lot)
        logger.info(
            f"Asset {asset.get_classification_key()}: Created fallback SOY short lot: "
            f"Qty Short: {fallback_short_lot.quantity_shorted}, Proceeds/Unit EUR: {fallback_short_lot.unit_sale_proceeds_eur}, Opening Date: {fallback_short_lot.opening_date}" # Renamed
        )

    # --- Stock-for-stock merger: carry lots across to the new asset ----------
    #
    # Only the mechanical half lives here. Whether a merger is a deferral at all
    # is a legal question answered by a MergerPolicy (src/engine/merger_policy.py)
    # and dispatched by MergerStockProcessor; this method is reached only once
    # that has resolved to CARRY_OVER.

    def drain_all_long_lots(self) -> List[FifoLot]:
        """Remove and return every long lot, in ledger order.

        Returns a copy, so the caller can hand it back to
        ``restore_drained_lots`` if the transfer fails.
        """
        drained = list(self.lots)
        self.lots.clear()
        return drained

    def drain_all_short_lots(self) -> List[ShortFifoLot]:
        """Remove and return every short lot, in ledger order."""
        drained = list(self.short_lots)
        self.short_lots.clear()
        return drained

    def restore_drained_lots(
        self,
        long_lots: List[FifoLot],
        short_lots: List[ShortFifoLot],
    ) -> None:
        """Put drained lots back exactly as they were.

        Slice assignment, deliberately: re-sorting here would rebuild the order
        from the sort key rather than restore it, and the ledger must come out of
        a failed transfer byte-identical to how it went in.
        """
        self.lots[:] = long_lots
        self.short_lots[:] = short_lots

    def take_over_lots(
        self,
        long_lots: List[FifoLot],
        short_lots: List[ShortFifoLot],
        carried_from_prefix: str,
    ) -> None:
        """Book lots that continue under this asset after an identifier change.

        A split that comes with a new ISIN or conid leaves the holding what it
        was -- the same purchases, at the same cost, on the same dates -- under
        another asset. The lots are taken as they are (the split has already
        rescaled them): nothing is re-derived, so the cost basis and the
        holding period run on unbroken. Only their origin is noted.
        """
        for lot in [*long_lots, *short_lots]:
            if not lot.carried_from:
                lot.carried_from = f"{carried_from_prefix}:{lot.source_transaction_id}"
        for lot in long_lots:
            self._stamp(lot)  # ids belong to the ledger that holds the lot
        self.lots.extend(long_lots)
        self.lots.sort(key=_long_lot_sort_key)
        self.short_lots.extend(short_lots)
        self.short_lots.sort(key=lambda lot: (
            parse_ibkr_date(lot.opening_date) or datetime.min.date(),
            numeric_tx_sort_key(lot.source_transaction_id),
        ))

    def receive_carried_lots_from_merger(
        self,
        *,
        long_lots: List[FifoLot],
        short_lots: List[ShortFifoLot],
        ratio: Decimal,
        merger_date: str,
        source_transaction_id: str,
        carried_from_prefix: str,
    ) -> Decimal:
        """Take drained lots from a merged asset, rescaled by *ratio*.

        Takes primitives rather than the event so the arithmetic is unit-testable
        and no event-shape knowledge leaks into the ledger.

        Per lot, and per the Czech treatment of a qualified §23b/§23c exchange:

        * ``total_cost_basis_eur`` is preserved **verbatim** and the unit cost
          re-derived from the new quantity. Scaling the total would multiply the
          acquisition cost by the ratio; carrying the unit would leave it stale,
          and the next partial disposal overwrites the total from the unit.
        * ``acquisition_date`` becomes the **merger date** — it selects the
          applicable regime, and the pre-2014 six-month test must not transfer to
          a share issued later (NSS 3 Afs 249/2024-45).
        * ``holding_period_start`` is copied from the source lot's own
          ``holding_period_start``, not its acquisition date, so a chained merger
          keeps the original purchase.
        * both estimated flags are set explicitly: the merger date is real, while
          a synthetic carried start must stay marked as such.

        One target lot per source lot — never coalesced, since each carries its
        own basis, holding start and estimated flag.

        Two phases. PREPARE constructs every lot, so any validation failure
        raises before either ledger is touched. COMMIT extends and sorts, which
        cannot fail. Returns the committed aggregate long quantity.
        """
        prepared_long: List[FifoLot] = []
        prepared_short: List[ShortFifoLot] = []

        if long_lots:
            new_quantities = _allocate_rescaled_quantities(
                [lot.quantity for lot in long_lots],
                ratio,
                [
                    (parse_ibkr_date(lot.holding_period_start) or datetime.min.date(), i)
                    for i, lot in enumerate(long_lots)
                ],
                self.ctx,
            )
            for lot, new_qty in zip(long_lots, new_quantities):
                prepared_long.append(FifoLot(
                    acquisition_date=merger_date,
                    quantity=new_qty,
                    unit_cost_basis_eur=self.ctx.divide(lot.total_cost_basis_eur, new_qty),
                    total_cost_basis_eur=lot.total_cost_basis_eur,
                    # Carried verbatim, like the EUR total: a carry-over changes
                    # how many shares the same money bought, not the money.
                    total_cost_original=lot.total_cost_original,
                    cost_currency=lot.cost_currency,
                    # The same money, paid on the same days.
                    cost_components=(list(lot.cost_components)
                                     if lot.cost_components is not None else None),
                    source_transaction_id=source_transaction_id,
                    holding_period_start=lot.holding_period_start,
                    holding_period_start_estimated=lot.holding_period_start_estimated,
                    # acquisition_date_estimated stays False: the merger date is real.
                    carried_from=f"{carried_from_prefix}:{lot.source_transaction_id}",
                ))

        if short_lots:
            new_quantities = _allocate_rescaled_quantities(
                [lot.quantity_shorted for lot in short_lots],
                ratio,
                [
                    (parse_ibkr_date(lot.opening_date) or datetime.min.date(), i)
                    for i, lot in enumerate(short_lots)
                ],
                self.ctx,
            )
            for lot, new_qty in zip(short_lots, new_quantities):
                prepared_short.append(ShortFifoLot(
                    # A short keeps its opening date: the proceeds were received
                    # then, and a short can never pass the holding-period test.
                    opening_date=lot.opening_date,
                    quantity_shorted=new_qty,
                    unit_sale_proceeds_eur=self.ctx.divide(
                        lot.total_sale_proceeds_eur, new_qty),
                    total_sale_proceeds_eur=lot.total_sale_proceeds_eur,
                    source_transaction_id=source_transaction_id,
                    opening_date_estimated=lot.opening_date_estimated,
                    carried_from=f"{carried_from_prefix}:{lot.source_transaction_id}",
                ))

        # COMMIT — cannot fail.
        for lot in prepared_long:
            self._stamp(lot)
        self.lots.extend(prepared_long)
        self.lots.sort(key=_long_lot_sort_key)
        self.short_lots.extend(prepared_short)
        self.short_lots.sort(key=lambda lot: (
            parse_ibkr_date(lot.opening_date) or datetime.min.date(),
            numeric_tx_sort_key(lot.source_transaction_id),
        ))
        return sum((lot.quantity for lot in prepared_long), Decimal(0))

    def realize_all_lots_for_merger(
        self,
        *,
        unit_value_eur: Decimal,
        ratio: Decimal,
        merger_date: str,
    ) -> Tuple[List[RealizedGainLoss], List[Decimal]]:
        """Close every long lot against the consideration's fair value.

        The non-deferral treatment: the old shares are disposed of, measured at
        what was received for them. One RGL per lot, so each keeps its own
        acquisition date and holding-period start — the holding-period test
        applies to the OLD shares being sold, per lot.

        *unit_value_eur* is the fair value of ONE new share. Each lot's proceeds
        are the shares credited for it times that value, using the same
        allocation as the carry-over path so the credited total is exact and
        whole.

        Returns ``(realized, credited_quantities)`` — the caller needs the
        second to open the replacement lots, and recomputing the allocation
        there would be a second chance to disagree.
        """
        if not self.lots:
            return [], []

        credited = _allocate_rescaled_quantities(
            [lot.quantity for lot in self.lots],
            ratio,
            [
                (parse_ibkr_date(lot.holding_period_start) or datetime.min.date(), i)
                for i, lot in enumerate(self.lots)
            ],
            self.ctx,
        )

        realized: List[RealizedGainLoss] = []
        real_date_obj = parse_ibkr_date(merger_date)
        for lot, new_qty in zip(self.lots, credited):
            proceeds = self.ctx.multiply(new_qty, unit_value_eur)
            cost = lot.total_cost_basis_eur
            hps_date_obj = parse_ibkr_date(lot.holding_period_start)
            holding_period_days: Optional[int] = None
            if hps_date_obj and real_date_obj and real_date_obj >= hps_date_obj:
                holding_period_days = (real_date_obj - hps_date_obj).days

            rgl = RealizedGainLoss(
                originating_event_id=uuid.uuid4(),
                asset_internal_id=self.asset_internal_id,
                asset_category_at_realization=self.asset_category,
                acquisition_date=lot.acquisition_date,
                realization_date=merger_date,
                realization_type=RealizationType.LONG_POSITION_SALE,
                quantity_realized=lot.quantity,
                unit_cost_basis_eur=lot.unit_cost_basis_eur,
                unit_realization_value_eur=(
                    self.ctx.divide(proceeds, lot.quantity)
                    if lot.quantity != Decimal(0) else Decimal(0)
                ),
                total_cost_basis_eur=cost,
                total_realization_value_eur=proceeds,
                gross_gain_loss_eur=self.ctx.subtract(proceeds, cost),
                holding_period_days=holding_period_days,
                holding_period_start=lot.holding_period_start,
                holding_period_start_estimated=lot.holding_period_start_estimated,
                is_acquisition_estimated=lot.acquisition_date_estimated,
                fund_type_at_sale=(
                    self.fund_type
                    if self.asset_category == AssetCategory.INVESTMENT_FUND else None
                ),
            )
            if lot.cost_components is not None:
                rgl.cost_basis_components_eur = list(lot.cost_components)
            if self._tax_classifier is not None:
                self._tax_classifier(rgl)
            realized.append(rgl)

        self.lots.clear()
        logger.info(
            f"Merger realised {len(realized)} lot(s) of asset "
            f"{self.asset_internal_id} at {unit_value_eur} EUR per new share."
        )
        return realized, credited

    def open_lots_at_merger_value(
        self,
        *,
        quantities: List[Decimal],
        unit_value_eur: Decimal,
        merger_date: str,
        source_transaction_id: str,
        carried_from_prefix: str,
    ) -> Decimal:
        """Open the replacement lots after a taxable merger.

        Nothing carries over here: the shares were just valued, so that value is
        their cost, and both dates are the merger date — the holding period
        restarts. One lot per realised source lot, matching the disposal rows.
        """
        for index, qty in enumerate(quantities):
            if qty <= Decimal(0):
                continue
            self.lots.append(self._stamp(FifoLot(
                acquisition_date=merger_date,
                quantity=qty,
                unit_cost_basis_eur=unit_value_eur,
                total_cost_basis_eur=self.ctx.multiply(qty, unit_value_eur),
                source_transaction_id=source_transaction_id,
                # holding_period_start defaults to the acquisition date: a
                # taxable exchange starts a new holding.
                carried_from=f"{carried_from_prefix}:taxable-merger[{index}]",
            )))
        self.lots.sort(key=_long_lot_sort_key)
        return sum((q for q in quantities if q > Decimal(0)), Decimal(0))

    def add_placeholder_lot(
        self,
        *,
        quantity: Decimal,
        acquisition_date: str,
        source_transaction_id: str,
    ) -> None:
        """Hold shares whose cost the inputs do not give, at zero cost.

        For shares credited by a corporate action the engine cannot apply (a
        spin-off): that they are held is a fact, what they cost is not on the
        row. Zero is not a valuation -- it is the one figure that cannot
        understate a later gain -- and both dates are marked estimated, so a
        disposal from this lot is never exempted on its holding period and
        reaches the country layer flagged for review. Without the lot a sale
        of the shares would find nothing to consume and end the run.
        """
        zero = self.ctx.create_decimal(0)
        self.lots.append(self._stamp(FifoLot(
            acquisition_date=acquisition_date,
            quantity=quantity.quantize(global_config.PRECISION_QUANTITY, context=self.ctx),
            unit_cost_basis_eur=zero,
            total_cost_basis_eur=zero,
            source_transaction_id=source_transaction_id,
            acquisition_date_estimated=True,
            holding_period_start_estimated=True,
        )))
        self.lots.sort(key=_long_lot_sort_key)

    def _next_sequence(self) -> int:
        self._operation_counter += 1
        return self._operation_counter

    def _stamp(self, lot: "FifoLot") -> "FifoLot":
        """Give a newly booked long lot its id and its place in the day."""
        self._last_lot_id += 1
        lot.lot_id = self._last_lot_id
        lot.sequence = self._next_sequence()
        return lot

    def replace_open_long_lots(self, lots: List["FifoLot"]) -> None:
        """Install *lots* as the open long lots (after a re-matching of the
        year's sales decided which of them are left)."""
        self.lots = sorted(lots, key=_long_lot_sort_key)

    def add_long_lot(self, trade_event: TradeEvent):
        if trade_event.event_type != FinancialEventType.TRADE_BUY_LONG: return
        if trade_event.quantity is None or trade_event.quantity <= Decimal(0): return
        if trade_event.net_proceeds_or_cost_basis_eur is None:
            self.dropped_unenriched_events += 1
            logger.error(
                f"BUY trade {trade_event.ibkr_transaction_id or trade_event.event_id} "
                f"({trade_event.event_date}, qty {trade_event.quantity}) for asset {self.asset_internal_id} "
                "has no EUR value (FX conversion failed) — lot NOT created and EXCLUDED from FIFO. "
                "A later sale of this quantity may fail or realize against wrong lots."
            )
            return
        if not trade_event.ibkr_transaction_id:
            raise ValueError(f"Missing ibkr_transaction_id for trade {trade_event.event_id} needed for FIFO lot creation.")

        total_cost_basis_eur = self.ctx.create_decimal(trade_event.net_proceeds_or_cost_basis_eur)
        lot_qty_contracts_or_units = trade_event.quantity.quantize(global_config.PRECISION_QUANTITY, context=self.ctx)

        if lot_qty_contracts_or_units == Decimal(0):
            logger.warning(f"TradeEvent {trade_event.ibkr_transaction_id} (BUY_LONG) has zero quantity after quantization, skipping lot creation.")
            return
        cost_basis_eur_per_unit = self.ctx.divide(total_cost_basis_eur, lot_qty_contracts_or_units)

        original_total, original_currency = _original_cost_of_buy(trade_event)

        new_lot = FifoLot(
            acquisition_date=trade_event.event_date, quantity=lot_qty_contracts_or_units,
            unit_cost_basis_eur=cost_basis_eur_per_unit, # Renamed
            total_cost_basis_eur=total_cost_basis_eur,
            source_transaction_id=trade_event.ibkr_transaction_id,
            total_cost_original=original_total,
            cost_currency=original_currency,
        )
        self.lots.append(self._stamp(new_lot))
        self.lots.sort(key=_long_lot_sort_key)
        if any((parse_ibkr_date(lot.acquisition_date) is None) for lot in self.lots):
             raise ValueError(f"Unparseable acquisition date found in FIFO lots for asset {self.asset_internal_id} after adding lot.")

    def add_short_lot(self, trade_event: TradeEvent):
        if trade_event.event_type != FinancialEventType.TRADE_SELL_SHORT_OPEN: return
        if trade_event.quantity is None or trade_event.quantity >= Decimal(0): return
        if trade_event.net_proceeds_or_cost_basis_eur is None:
            self.dropped_unenriched_events += 1
            logger.error(f"Cannot add short lot for trade {trade_event.ibkr_transaction_id} - net_proceeds_or_cost_basis_eur is None (FX conversion failed). Trade EXCLUDED from FIFO.")
            return
        if not trade_event.ibkr_transaction_id:
            raise ValueError(f"Missing ibkr_transaction_id for trade {trade_event.event_id} needed for Short FIFO lot creation.")

        # Keep the sign: net proceeds are negative when commissions exceed the
        # gross amount (e.g. closing a near-worthless option) — copy_abs()
        # would flip a real cost into fictitious proceeds.
        total_sale_proceeds_eur = self.ctx.create_decimal(trade_event.net_proceeds_or_cost_basis_eur)
        lot_qty_shorted_contracts_or_units = trade_event.quantity.copy_abs().quantize(global_config.PRECISION_QUANTITY, context=self.ctx)

        if lot_qty_shorted_contracts_or_units == Decimal(0):
            logger.warning(f"TradeEvent {trade_event.ibkr_transaction_id} (SELL_SHORT_OPEN) has zero quantity after quantization, skipping lot creation.")
            return
        sale_proceeds_eur_per_unit = self.ctx.divide(total_sale_proceeds_eur, lot_qty_shorted_contracts_or_units)

        new_short_lot = ShortFifoLot(
            opening_date=trade_event.event_date, quantity_shorted=lot_qty_shorted_contracts_or_units,
            unit_sale_proceeds_eur=sale_proceeds_eur_per_unit, # Renamed
            total_sale_proceeds_eur=total_sale_proceeds_eur,
            source_transaction_id=trade_event.ibkr_transaction_id
        )
        self.short_lots.append(new_short_lot)
        self.short_lots.sort(key=lambda lot: (parse_ibkr_date(lot.opening_date) or datetime.min.date(), numeric_tx_sort_key(lot.source_transaction_id)))
        if any((parse_ibkr_date(lot.opening_date) is None) for lot in self.short_lots):
             raise ValueError(f"Unparseable opening date found in Short FIFO lots for asset {self.asset_internal_id} after adding lot.")


    def _portion_of(self, lot: "FifoLot", quantity: Decimal) -> "FifoLot":
        """A copy of *lot* holding *quantity* of it, at the lot's own cost."""
        if quantity == lot.quantity:
            return dc_replace(lot)
        total = self.ctx.multiply(quantity, lot.unit_cost_basis_eur)
        original = lot.rescaled_original(quantity)
        return dc_replace(
            lot, quantity=quantity, total_cost_basis_eur=total,
            total_cost_original=original,
            cost_currency=lot.cost_currency if original is not None else None,
            cost_components=(_scale_components(lot.cost_components, total, self.ctx)
                             if lot.cost_components is not None else None),
        )

    def _filed_method_for(self, year: int) -> PairingMethod:
        """The method an earlier *year* was filed with (FIFO when unknown)."""
        if self.pairing_history is None:
            return PairingMethod.FIFO
        return self.pairing_history.method_for(year)

    def _plan_picks(self, plan, quantity_to_realize: Decimal, sale_event: TradeEvent) -> List[Tuple[int, Decimal]]:
        """Turn a filed plan entry into (lot index, quantity) picks.

        Raises UserWarning when the entry cannot be applied as filed -- a lot
        it names is not held, holds too little, or the entry does not add up
        to the sale -- so the replay is marked inconsistent instead of
        consuming something the filing did not.
        """
        label = sale_event.ibkr_transaction_id or sale_event.event_id
        total = sum((Decimal(q) for _lot, q in plan), Decimal(0))
        if (total - quantity_to_realize).copy_abs() > Decimal("1e-10"):
            raise UserWarning(
                f"Filed pairing plan for sale {label} covers {total}, the sale is "
                f"{quantity_to_realize}.")
        taken: dict = {}
        for lot_id, qty in plan:
            need = Decimal(qty)
            matching = [index for index, lot in enumerate(self.lots)
                        if plan_key(lot) == str(lot_id)]
            if len(matching) > 1:
                # Several lots answer to the name: picking one would be a
                # guess, and a wrong guess claims one cost twice.
                raise UserWarning(
                    f"Filed pairing plan for sale {label} names lot {lot_id}, which is "
                    f"ambiguous: {len(matching)} replayed lots carry that key.")
            for index in matching:
                lot = self.lots[index]
                free = lot.quantity - taken.get(index, Decimal(0))
                if free <= Decimal(0):
                    continue
                take = min(free, need)
                taken[index] = taken.get(index, Decimal(0)) + take
                need -= take
            if need > Decimal("1e-10"):
                raise UserWarning(
                    f"Filed pairing plan for sale {label} names lot {lot_id} for "
                    f"{qty}, which the replayed ledger does not hold.")
        return sorted(taken.items())

    def consume_long_lots_for_sale(self, sale_event: TradeEvent, is_historical_simulation: bool = False,
                                   *, method: Optional[PairingMethod] = None,
                                   plan=None) -> List[RealizedGainLoss]:
        """Realise a long sale against the open lots.

        *method* overrides the ledger's own method (the start-of-year replay
        passes the method each earlier year was filed with). *plan*, for an
        ``optimal`` year, names exactly which lots the filed sale consumed.
        """
        if sale_event.event_type != FinancialEventType.TRADE_SELL_LONG: return []
        if sale_event.quantity is None or sale_event.quantity >= Decimal(0): return []
        if sale_event.net_proceeds_or_cost_basis_eur is None:
            self.dropped_unenriched_events += 1
            logger.error(
                f"SELL trade {sale_event.ibkr_transaction_id or sale_event.event_id} "
                f"({sale_event.event_date}, qty {sale_event.quantity}) for asset {self.asset_internal_id} "
                "has no EUR value (FX conversion failed) — the TAXABLE DISPOSAL is EXCLUDED "
                "from FIFO results. Review input data / FX rates."
            )
            return []

        quantity_to_realize = sale_event.quantity.copy_abs().quantize(global_config.PRECISION_QUANTITY, context=self.ctx)
        # Keep the sign: net proceeds are negative when commissions exceed the
        # gross amount — copy_abs() overstated the proceeds by 2× commission.
        total_sale_proceeds_for_event = self.ctx.create_decimal(sale_event.net_proceeds_or_cost_basis_eur)

        if quantity_to_realize == Decimal(0): return []
        sale_proceeds_eur_per_unit_for_event = self.ctx.divide(total_sale_proceeds_for_event, quantity_to_realize)

        realized_gains_losses: List[RealizedGainLoss] = []
        quantity_remaining_to_realize = quantity_to_realize
        lots_to_remove_indices: List[int] = []
        current_available_qty_in_lots = sum(l.quantity for l in self.lots)

        # Weighted-average pairing: every disposed unit is costed at the blended
        # pool average (Σcost / Σqty) held at sale time, while the deemed-sold
        # lot identity (dates → time test) stays FIFO. Surviving lots are then
        # re-priced to the average so the next average stays consistent (moving
        # average). FIFO/LIFO leave the per-lot cost untouched.
        active_method = (coerce_pairing_method(method) if method is not None
                         else self.pairing_method)
        # A filed plan applies only to an optimal year; without an entry the
        # sale stayed on FIFO in the filed run (OPTIMAL orders as FIFO here).
        # Flagged before the plan is read: a plan that cannot be applied is
        # just as much a non-FIFO year.
        if is_historical_simulation and (
                active_method in (PairingMethod.LIFO, PairingMethod.WEIGHTED_AVERAGE)
                or (active_method == PairingMethod.OPTIMAL and plan is not None)):
            self._replayed_non_fifo_disposal = True
        picks: List[Tuple[int, Optional[Decimal]]]
        if plan is not None and active_method == PairingMethod.OPTIMAL:
            picks = list(self._plan_picks(plan, quantity_to_realize, sale_event))
        else:
            picks = [(i, None) for i in consumption_order_indices(len(self.lots), active_method)]

        pool_avg_unit_cost: Optional[Decimal] = None
        if uses_pool_average_cost(active_method) and self.lots:
            _pool_qty = sum((l.quantity for l in self.lots), Decimal(0))
            _pool_cost = sum((l.total_cost_basis_eur for l in self.lots), Decimal(0))
            if _pool_qty > Decimal(0):
                pool_avg_unit_cost = self.ctx.divide(_pool_cost, _pool_qty)
        # The average blends money paid on different days. Each disposed
        # unit -- and each surviving one -- gets the pool's dated components
        # in proportion, so a country converting the cost at the rate of the
        # day it was paid converts the average correctly (audit W08).
        pool_components: Optional[List[Tuple[str, Decimal]]] = None
        if pool_avg_unit_cost is not None:
            pool_components = _merge_components(
                part for l in self.lots for part in _components_of(l))

        realization_type_for_rgl: RealizationType
        if self.asset_category == AssetCategory.OPTION:
            realization_type_for_rgl = RealizationType.OPTION_TRADE_CLOSE_LONG
        else:
            realization_type_for_rgl = RealizationType.LONG_POSITION_SALE # Renamed

        # Tax-year sales are journaled: their place in the day, and exactly
        # which part of which lot each consumed (audit W05/W07).
        journal = not is_historical_simulation
        sale_sequence = self._next_sequence() if journal else 0

        for i, cap in picks:
            if quantity_remaining_to_realize <= Decimal(0): break
            current_lot = self.lots[i]
            lot_before = dc_replace(current_lot) if journal else None
            effective_unit_cost = pool_avg_unit_cost if pool_avg_unit_cost is not None else current_lot.unit_cost_basis_eur
            # Without a cap (every method but a filed plan) the lot gives as
            # much as the sale still needs, exactly as before.
            wanted = (quantity_remaining_to_realize if cap is None
                      else min(cap, quantity_remaining_to_realize))
            quantity_from_this_lot: Decimal
            lot_components = current_lot.cost_components
            if current_lot.quantity <= wanted:
                quantity_from_this_lot = current_lot.quantity
                lots_to_remove_indices.append(i)
            else:
                quantity_from_this_lot = wanted
                remaining_qty = self.ctx.subtract(current_lot.quantity, quantity_from_this_lot)
                # Rescale the display-only original before the quantity moves.
                current_lot.total_cost_original = current_lot.rescaled_original(remaining_qty)
                current_lot.quantity = remaining_qty
                current_lot.total_cost_basis_eur = self.ctx.multiply(current_lot.quantity, current_lot.unit_cost_basis_eur) # Renamed
                if lot_components is not None:
                    current_lot.cost_components = _scale_components(
                        lot_components, current_lot.total_cost_basis_eur, self.ctx)

            quantity_remaining_to_realize = self.ctx.subtract(quantity_remaining_to_realize, quantity_from_this_lot)
            if journal:
                self.consumed_portions.append(ConsumedPortion(
                    sale_sequence=sale_sequence,
                    lot=self._portion_of(lot_before, quantity_from_this_lot),
                ))

            if not is_historical_simulation:
                cost_basis_for_portion = self.ctx.multiply(quantity_from_this_lot, effective_unit_cost)
                realization_value_for_portion = self.ctx.multiply(quantity_from_this_lot, sale_proceeds_eur_per_unit_for_event)
                gross_gain_loss = self.ctx.subtract(realization_value_for_portion, cost_basis_for_portion)

                acq_date_obj = parse_ibkr_date(current_lot.acquisition_date)
                # Measured from where the holding began, not from when this
                # security was acquired — they differ for a carried-over lot.
                hps_date_obj = parse_ibkr_date(
                    current_lot.holding_period_start or current_lot.acquisition_date)
                real_date_obj = parse_ibkr_date(sale_event.event_date)
                holding_period_days: Optional[int] = None
                if hps_date_obj and real_date_obj and real_date_obj >= hps_date_obj :
                    holding_period_days = (real_date_obj - hps_date_obj).days

                rgl = RealizedGainLoss(
                    originating_event_id=sale_event.event_id, asset_internal_id=self.asset_internal_id,
                    asset_category_at_realization=self.asset_category, acquisition_date=current_lot.acquisition_date,
                    realization_date=sale_event.event_date,
                    realization_type=realization_type_for_rgl,
                    quantity_realized=quantity_from_this_lot,
                    unit_cost_basis_eur=effective_unit_cost,
                    unit_realization_value_eur=sale_proceeds_eur_per_unit_for_event,
                    total_cost_basis_eur=cost_basis_for_portion,
                    total_realization_value_eur=realization_value_for_portion,
                    gross_gain_loss_eur=gross_gain_loss, holding_period_days=holding_period_days,
                    fund_type_at_sale=self.fund_type if self.asset_category == AssetCategory.INVESTMENT_FUND else None,
                    holding_period_start=current_lot.holding_period_start,
                    holding_period_start_estimated=current_lot.holding_period_start_estimated,
                    is_acquisition_estimated=current_lot.acquisition_date_estimated,
                )
                # Only where the cost is not simply the lot's own, paid on its
                # cost date: a plain FIFO/LIFO disposal stays as it was.
                source_components = (pool_components if pool_components is not None
                                     else lot_components)
                if source_components is not None:
                    rgl.cost_basis_components_eur = _scale_components(
                        source_components, cost_basis_for_portion, self.ctx)
                if self._tax_classifier is not None:
                    self._tax_classifier(rgl)
                realized_gains_losses.append(rgl)

        for i in sorted(lots_to_remove_indices, reverse=True): del self.lots[i]

        if journal:
            self.tax_year_sales.append(TaxYearSale(
                event_id=sale_event.event_id,
                ibkr_transaction_id=sale_event.ibkr_transaction_id,
                sequence=sale_sequence,
                sale_date=sale_event.event_date,
                quantity=self.ctx.subtract(quantity_to_realize, quantity_remaining_to_realize),
                unit_proceeds_eur=sale_proceeds_eur_per_unit_for_event,
            ))

        if pool_avg_unit_cost is not None:
            for lot in self.lots:
                lot.unit_cost_basis_eur = pool_avg_unit_cost
                lot.total_cost_basis_eur = self.ctx.multiply(lot.quantity, pool_avg_unit_cost)
                lot.cost_components = _scale_components(
                    pool_components, lot.total_cost_basis_eur, self.ctx)
                # Re-priced to a blended pool average: no single original
                # purchase corresponds to it any more, and rescaling the old
                # figure would invent an exchange rate. Drop it and let the
                # caller show the EUR average it actually is.
                lot.total_cost_original = None
                lot.cost_currency = None

        small_tolerance_qty = Decimal('1e-10')
        if quantity_remaining_to_realize.copy_abs() > small_tolerance_qty:
            if getattr(sale_event, "allows_position_flip", False) and not is_historical_simulation:
                # "C;O" flip: the remainder beyond the closed long position
                # OPENS a short position at the same per-unit proceeds.
                flip_proceeds_total = self.ctx.multiply(
                    quantity_remaining_to_realize, sale_proceeds_eur_per_unit_for_event
                )
                flip_lot = ShortFifoLot(
                    opening_date=sale_event.event_date,
                    quantity_shorted=quantity_remaining_to_realize,
                    unit_sale_proceeds_eur=sale_proceeds_eur_per_unit_for_event,
                    total_sale_proceeds_eur=flip_proceeds_total,
                    source_transaction_id=sale_event.ibkr_transaction_id or str(sale_event.event_id),
                )
                self.short_lots.append(flip_lot)
                self.short_lots.sort(key=lambda lot: (parse_ibkr_date(lot.opening_date) or datetime.min.date(), numeric_tx_sort_key(lot.source_transaction_id)))
                logger.info(
                    f"Position FLIP (C;O) for sale {sale_event.ibkr_transaction_id or sale_event.event_id}: "
                    f"closed {quantity_to_realize - quantity_remaining_to_realize} long, "
                    f"opened SHORT {quantity_remaining_to_realize} @ {sale_proceeds_eur_per_unit_for_event} EUR/unit."
                )
                return realized_gains_losses

            msg = (f"Insufficient long lots for sale event {sale_event.ibkr_transaction_id or sale_event.event_id} "
                   f"for asset {self.asset_internal_id}. Required to sell: {quantity_to_realize}, "
                   f"Total available in lots before this sale: {current_available_qty_in_lots}, "
                   f"Remaining to sell after processing lots: {quantity_remaining_to_realize}.")
            if is_historical_simulation:
                logger.warning(f"Historical Simulation: {msg}")
                raise UserWarning(msg)
            else:
                raise ValueError(msg)
        return realized_gains_losses

    def consume_short_lots_for_cover(self, cover_event: TradeEvent, is_historical_simulation: bool = False,
                                     *, method: Optional[PairingMethod] = None) -> List[RealizedGainLoss]:
        if cover_event.event_type != FinancialEventType.TRADE_BUY_SHORT_COVER: return []
        if cover_event.quantity is None or cover_event.quantity <= Decimal(0): return []
        if cover_event.net_proceeds_or_cost_basis_eur is None:
            self.dropped_unenriched_events += 1
            logger.error(
                f"COVER trade {cover_event.ibkr_transaction_id or cover_event.event_id} "
                f"({cover_event.event_date}) for asset {self.asset_internal_id} "
                "has no EUR value (FX conversion failed) — the TAXABLE SHORT COVER is "
                "EXCLUDED from FIFO results. Review input data / FX rates."
            )
            return []

        quantity_to_realize = cover_event.quantity.quantize(global_config.PRECISION_QUANTITY, context=self.ctx)
        total_cost_for_cover_event = self.ctx.create_decimal(cover_event.net_proceeds_or_cost_basis_eur)

        if quantity_to_realize == Decimal(0): return []
        cost_eur_per_unit_for_cover_event = self.ctx.divide(total_cost_for_cover_event, quantity_to_realize)

        realized_gains_losses: List[RealizedGainLoss] = []
        quantity_remaining_to_realize = quantity_to_realize
        short_lots_to_remove_indices: List[int] = []
        current_available_qty_in_short_lots = sum(sl.quantity_shorted for sl in self.short_lots)

        # Short positions are covered FIFO at each lot's own proceeds under
        # every method but LIFO. The weighted average is an acquisition-cost
        # rule for held securities; blending the proceeds of written positions
        # opened on different days loses the day each amount was received,
        # the same defect the long side had (audit W08).
        active_method = (coerce_pairing_method(method) if method is not None
                         else self.pairing_method)
        if is_historical_simulation and active_method == PairingMethod.LIFO:
            self._replayed_non_fifo_disposal = True


        realization_type_for_rgl: RealizationType
        if self.asset_category == AssetCategory.OPTION:
            realization_type_for_rgl = RealizationType.OPTION_TRADE_CLOSE_SHORT
        else:
            realization_type_for_rgl = RealizationType.SHORT_POSITION_COVER # Renamed

        for i in consumption_order_indices(len(self.short_lots), active_method):
            if quantity_remaining_to_realize <= Decimal(0): break
            current_short_lot = self.short_lots[i]
            effective_unit_proceeds = current_short_lot.unit_sale_proceeds_eur
            quantity_covered_from_this_lot: Decimal
            if current_short_lot.quantity_shorted <= quantity_remaining_to_realize:
                quantity_covered_from_this_lot = current_short_lot.quantity_shorted
                short_lots_to_remove_indices.append(i)
            else:
                quantity_covered_from_this_lot = quantity_remaining_to_realize
                current_short_lot.quantity_shorted = self.ctx.subtract(current_short_lot.quantity_shorted, quantity_covered_from_this_lot)
                current_short_lot.total_sale_proceeds_eur = self.ctx.multiply(current_short_lot.quantity_shorted, current_short_lot.unit_sale_proceeds_eur) # Renamed

            quantity_remaining_to_realize = self.ctx.subtract(quantity_remaining_to_realize, quantity_covered_from_this_lot)

            if not is_historical_simulation:
                cost_basis_for_portion = self.ctx.multiply(quantity_covered_from_this_lot, cost_eur_per_unit_for_cover_event)
                realization_value_for_portion = self.ctx.multiply(quantity_covered_from_this_lot, effective_unit_proceeds)
                gross_gain_loss = self.ctx.subtract(realization_value_for_portion, cost_basis_for_portion)

                open_date_obj = parse_ibkr_date(current_short_lot.opening_date)
                cover_date_obj = parse_ibkr_date(cover_event.event_date)
                holding_period_days: Optional[int] = None
                if open_date_obj and cover_date_obj and cover_date_obj >= open_date_obj:
                    holding_period_days = (cover_date_obj - open_date_obj).days

                rgl = RealizedGainLoss(
                    originating_event_id=cover_event.event_id, asset_internal_id=self.asset_internal_id,
                    asset_category_at_realization=self.asset_category,
                    acquisition_date=current_short_lot.opening_date,
                    realization_date=cover_event.event_date,
                    realization_type=realization_type_for_rgl,
                    quantity_realized=quantity_covered_from_this_lot,
                    unit_cost_basis_eur=cost_eur_per_unit_for_cover_event,
                    unit_realization_value_eur=effective_unit_proceeds,
                    total_cost_basis_eur=cost_basis_for_portion,
                    total_realization_value_eur=realization_value_for_portion,
                    gross_gain_loss_eur=gross_gain_loss, holding_period_days=holding_period_days,
                    fund_type_at_sale=self.fund_type if self.asset_category == AssetCategory.INVESTMENT_FUND else None,
                    is_acquisition_estimated=current_short_lot.opening_date_estimated,
                )
                if self._tax_classifier is not None:
                    self._tax_classifier(rgl)
                realized_gains_losses.append(rgl)

        for i in sorted(short_lots_to_remove_indices, reverse=True): del self.short_lots[i]

        small_tolerance_qty = Decimal('1e-10')
        if quantity_remaining_to_realize.copy_abs() > small_tolerance_qty:
            if getattr(cover_event, "allows_position_flip", False) and not is_historical_simulation:
                # "C;O" flip: the remainder beyond the covered short position
                # OPENS a long position at the same per-unit cost.
                flip_cost_total = self.ctx.multiply(
                    quantity_remaining_to_realize, cost_eur_per_unit_for_cover_event
                )
                flip_lot = FifoLot(
                    acquisition_date=cover_event.event_date,
                    quantity=quantity_remaining_to_realize,
                    unit_cost_basis_eur=cost_eur_per_unit_for_cover_event,
                    total_cost_basis_eur=flip_cost_total,
                    source_transaction_id=cover_event.ibkr_transaction_id or str(cover_event.event_id),
                )
                self.lots.append(self._stamp(flip_lot))
                self.lots.sort(key=_long_lot_sort_key)
                logger.info(
                    f"Position FLIP (C;O) for cover {cover_event.ibkr_transaction_id or cover_event.event_id}: "
                    f"covered {quantity_to_realize - quantity_remaining_to_realize} short, "
                    f"opened LONG {quantity_remaining_to_realize} @ {cost_eur_per_unit_for_cover_event} EUR/unit."
                )
                return realized_gains_losses

            msg = (f"Insufficient short lots for cover event {cover_event.ibkr_transaction_id or cover_event.event_id} "
                   f"for asset {self.asset_internal_id}. Required to cover: {quantity_to_realize}, "
                   f"Total available in short lots before this cover: {current_available_qty_in_short_lots}, "
                   f"Remaining to cover after processing lots: {quantity_remaining_to_realize}.")
            if is_historical_simulation:
                logger.warning(f"Historical Simulation: {msg}")
                raise UserWarning(msg)
            else:
                raise ValueError(msg)
        return realized_gains_losses


    def adjust_lots_for_split(self, event: CorpActionSplitForward):
        split_ratio = event.new_shares_per_old_share
        if split_ratio <= Decimal(0):
            logger.warning(f"Split event {event.event_id} for asset {self.asset_internal_id} has invalid ratio {split_ratio}. No adjustment made.")
            return

        logger.info(f"Applying split ratio {split_ratio} to lots for asset {self.asset_internal_id} (Category: {self.asset_category.name}) from event {event.event_id}")

        for lot in self.lots:
            original_quantity = lot.quantity
            original_total_cost = lot.total_cost_basis_eur
            new_quantity = self.ctx.multiply(original_quantity, split_ratio).quantize(global_config.PRECISION_QUANTITY, context=self.ctx)
            if new_quantity == Decimal(0) and original_quantity != Decimal(0) :
                logger.warning(f"Lot (Src: {lot.source_transaction_id}) quantity became zero after split ratio {split_ratio}. Original Qty: {original_quantity}. Setting cost/unit to 0.")
                new_cost_per_unit = Decimal(0)
            elif new_quantity == Decimal(0) and original_quantity == Decimal(0) :
                 new_cost_per_unit = Decimal(0)
            else:
                new_cost_per_unit = self.ctx.divide(original_total_cost, new_quantity)

            lot.quantity = new_quantity
            lot.unit_cost_basis_eur = new_cost_per_unit # Renamed
            logger.debug(f"  Adjusted Lot (Src: {lot.source_transaction_id}): New Qty={lot.quantity}, New Cost/Unit={lot.unit_cost_basis_eur}, Total Cost (Unchanged)={lot.total_cost_basis_eur}") # Renamed

        for short_lot in self.short_lots:
            original_quantity = short_lot.quantity_shorted
            original_total_proceeds = short_lot.total_sale_proceeds_eur
            new_quantity = self.ctx.multiply(original_quantity, split_ratio).quantize(global_config.PRECISION_QUANTITY, context=self.ctx)
            if new_quantity == Decimal(0) and original_quantity != Decimal(0):
                logger.warning(f"Short Lot (Src: {short_lot.source_transaction_id}) quantity became zero after split ratio {split_ratio}. Original Qty: {original_quantity}. Setting proceeds/unit to 0.")
                new_proceeds_per_unit = Decimal(0)
            elif new_quantity == Decimal(0) and original_quantity == Decimal(0) :
                 new_proceeds_per_unit = Decimal(0)
            else:
                new_proceeds_per_unit = self.ctx.divide(original_total_proceeds, new_quantity)

            short_lot.quantity_shorted = new_quantity
            short_lot.unit_sale_proceeds_eur = new_proceeds_per_unit # Renamed
            logger.debug(f"  Adjusted Short Lot (Src: {short_lot.source_transaction_id}): New Qty={short_lot.quantity_shorted}, New Proceeds/Unit={short_lot.unit_sale_proceeds_eur}, Total Proceeds (Unchanged)={short_lot.total_sale_proceeds_eur}") # Renamed

    def consume_all_lots_for_cash_merger(self, event: CorpActionMergerCash) -> List[RealizedGainLoss]:
        if event.cash_per_share_eur is None:
             logger.error(f"Cash merger event {event.event_id} for asset {self.asset_internal_id} missing cash_per_share_eur. Cannot process.")
             return []
        if self.short_lots:
            logger.warning(
                f"Cash merger event {event.event_id} for asset {self.asset_internal_id}: "
                f"SHORT lots exist (qty {sum(l.quantity_shorted for l in self.short_lots)}) "
                "and are NOT handled by cash merger processing — review manually."
            )
        if not self.lots:
            logger.info(f"Cash merger event {event.event_id} for asset {self.asset_internal_id}, but no long lots to consume.")
            return []

        logger.info(f"Processing cash merger for asset {self.asset_internal_id} from event {event.event_id}, at {event.cash_per_share_eur} EUR per {'contract' if self.asset_category == AssetCategory.OPTION else 'unit'}.")

        realized_gains_losses: List[RealizedGainLoss] = []
        realization_value_eur_per_unit_for_event = event.cash_per_share_eur

        # For options, lot quantity is in CONTRACTS and cost basis is stored
        # per-contract (already reflecting the contract multiplier). cash_per_share_eur
        # is a per-underlying-share figure, so it must be scaled by the multiplier to
        # be on the same per-contract basis as the cost basis; otherwise the gain/loss
        # is off by the multiplier factor (e.g. 100x).
        if self.asset_category == AssetCategory.OPTION:
            option_multiplier = self.asset_multiplier_info if self.asset_multiplier_info is not None else Decimal(100)
            realization_value_eur_per_unit_for_event = self.ctx.multiply(
                event.cash_per_share_eur, option_multiplier
            )

        # Consume only the DISPOSED quantity (partial tenders / buybacks exist);
        # quantity_disposed ≤ 0 or exceeding the held quantity falls back to all.
        total_long_qty = sum(l.quantity for l in self.lots)
        qty_remaining = getattr(event, "quantity_disposed", None)
        if qty_remaining is None or qty_remaining <= Decimal(0):
            qty_remaining = total_long_qty
        elif qty_remaining > total_long_qty:
            logger.warning(
                f"Cash merger event {event.event_id}: quantity_disposed {qty_remaining} exceeds "
                f"held quantity {total_long_qty} — consuming all held lots."
            )
            qty_remaining = total_long_qty
        elif qty_remaining < total_long_qty:
            logger.info(
                f"Cash merger event {event.event_id}: PARTIAL disposal of {qty_remaining} "
                f"out of {total_long_qty} held — remaining lots are kept."
            )

        lots_to_remove_indices: List[int] = []
        for i, current_lot in enumerate(self.lots):
            if qty_remaining <= Decimal(0): break
            lot_components = current_lot.cost_components
            if current_lot.quantity <= qty_remaining:
                quantity_from_this_lot = current_lot.quantity
                cost_basis_for_portion = current_lot.total_cost_basis_eur
                lots_to_remove_indices.append(i)
            else:
                quantity_from_this_lot = qty_remaining
                cost_basis_for_portion = self.ctx.multiply(quantity_from_this_lot, current_lot.unit_cost_basis_eur)
                remaining_qty = self.ctx.subtract(current_lot.quantity, quantity_from_this_lot)
                # Shrink the display-only original by the same ratio, before the
                # quantity changes underneath it — otherwise the two costs on the
                # lot drift apart on every partial sale.
                current_lot.total_cost_original = current_lot.rescaled_original(remaining_qty)
                current_lot.quantity = remaining_qty
                current_lot.total_cost_basis_eur = self.ctx.multiply(current_lot.quantity, current_lot.unit_cost_basis_eur)
                if lot_components is not None:
                    current_lot.cost_components = _scale_components(
                        lot_components, current_lot.total_cost_basis_eur, self.ctx)
            qty_remaining = self.ctx.subtract(qty_remaining, quantity_from_this_lot)

            realization_value_for_portion = self.ctx.multiply(quantity_from_this_lot, realization_value_eur_per_unit_for_event)
            gross_gain_loss = self.ctx.subtract(realization_value_for_portion, cost_basis_for_portion)

            acq_date_obj = parse_ibkr_date(current_lot.acquisition_date)
            hps_date_obj = parse_ibkr_date(
                current_lot.holding_period_start or current_lot.acquisition_date)
            real_date_obj = parse_ibkr_date(event.event_date)
            holding_period_days: Optional[int] = None
            if hps_date_obj and real_date_obj and real_date_obj >= hps_date_obj :
                holding_period_days = (real_date_obj - hps_date_obj).days

            rgl = RealizedGainLoss(
                originating_event_id=event.event_id, asset_internal_id=self.asset_internal_id,
                asset_category_at_realization=self.asset_category, acquisition_date=current_lot.acquisition_date,
                realization_date=event.event_date,
                realization_type=RealizationType.CASH_MERGER_PROCEEDS,
                quantity_realized=quantity_from_this_lot,
                unit_cost_basis_eur=current_lot.unit_cost_basis_eur,
                unit_realization_value_eur=realization_value_eur_per_unit_for_event,
                total_cost_basis_eur=cost_basis_for_portion,
                total_realization_value_eur=realization_value_for_portion,
                gross_gain_loss_eur=gross_gain_loss, holding_period_days=holding_period_days,
                holding_period_start=current_lot.holding_period_start,
                holding_period_start_estimated=current_lot.holding_period_start_estimated,
                is_acquisition_estimated=current_lot.acquisition_date_estimated,
                fund_type_at_sale=self.fund_type if self.asset_category == AssetCategory.INVESTMENT_FUND else None,
            )
            if lot_components is not None:
                rgl.cost_basis_components_eur = _scale_components(
                    lot_components, cost_basis_for_portion, self.ctx)
            if self._tax_classifier is not None:
                self._tax_classifier(rgl)
            realized_gains_losses.append(rgl)
            logger.debug(f"  Generated RGL from cash merger for lot (Src: {current_lot.source_transaction_id}): Realized {quantity_from_this_lot}, Gross G/L={gross_gain_loss}")

        for i in sorted(lots_to_remove_indices, reverse=True): del self.lots[i]
        logger.info(
            f"Cash merger for asset {self.asset_internal_id} consumed "
            f"{len(realized_gains_losses)} lot portion(s); {len(self.lots)} lot(s) remain."
        )
        return realized_gains_losses

    def add_lot_for_stock_dividend(self, event: CorpActionStockDividend):
        if event.quantity_new_shares_received <= Decimal(0):
            logger.info(f"Stock dividend event {event.event_id} for asset {self.asset_internal_id} has zero or negative new shares ({event.quantity_new_shares_received}). No lot added.")
            return
        if event.fmv_per_new_share_eur is None:
            # Never lose quantity: a missing FMV must not make the shares
            # vanish from the ledger (a later sale would crash or realize
            # against wrong lots). Create a ZERO-cost lot and complain.
            logger.error(
                f"Stock dividend event {event.event_id} for asset {self.asset_internal_id} "
                f"missing fmv_per_new_share_eur — creating ZERO-cost lot for "
                f"{event.quantity_new_shares_received} shares. Review FMV data."
            )
            zero = self.ctx.create_decimal(0)
            fallback_lot = FifoLot(
                acquisition_date=event.event_date,
                quantity=event.quantity_new_shares_received.quantize(global_config.PRECISION_QUANTITY, context=self.ctx),
                unit_cost_basis_eur=zero,
                total_cost_basis_eur=zero,
                source_transaction_id=event.ca_action_id_ibkr or event.ibkr_transaction_id or f"STOCKDIV_{event.event_id}",
            )
            self.lots.append(self._stamp(fallback_lot))
            self.lots.sort(key=_long_lot_sort_key)
            return

        if self.asset_category == AssetCategory.OPTION:
            logger.warning(f"Stock dividend event {event.event_id} received for OPTION asset {self.asset_internal_id}. This is unusual. Treating quantity as contracts with FMV per contract if applicable, but verify CA terms.")
        elif self.asset_category != AssetCategory.STOCK and self.asset_category != AssetCategory.INVESTMENT_FUND :
            logger.warning(f"Stock dividend event {event.event_id} received for non-STOCK/non-FUND asset {self.asset_internal_id} (Category: {self.asset_category.name}). Adding lot based on shares/FMV, but verify asset classification and CA terms.")

        new_lot_quantity = event.quantity_new_shares_received.quantize(global_config.PRECISION_QUANTITY, context=self.ctx)
        new_lot_cost_per_unit = event.fmv_per_new_share_eur
        new_lot_total_cost = self.ctx.multiply(new_lot_quantity, new_lot_cost_per_unit)

        source_id = event.ca_action_id_ibkr or event.ibkr_transaction_id or f"STOCKDIV_{event.event_id}"

        new_lot = FifoLot(
            acquisition_date=event.event_date, quantity=new_lot_quantity,
            unit_cost_basis_eur=new_lot_cost_per_unit, # Renamed
            total_cost_basis_eur=new_lot_total_cost, source_transaction_id=source_id
        )
        self.lots.append(self._stamp(new_lot))
        self.lots.sort(key=_long_lot_sort_key)
        if any((parse_ibkr_date(lot.acquisition_date) is None) for lot in self.lots):
             raise ValueError(f"Unparseable acquisition date found after adding stock dividend lot for asset {self.asset_internal_id}.")

        logger.info(f"Added new lot for stock dividend event {event.event_id} for asset {self.asset_internal_id}: Qty={new_lot.quantity}, Cost/Unit={new_lot.unit_cost_basis_eur} (FMV)") # Renamed


    def consume_long_option_get_cost(self, quantity_contracts_to_consume: Decimal) -> List[ConsumedLotDetail]:
        if self.asset_category != AssetCategory.OPTION:
            raise TypeError(f"consume_long_option_get_cost called on non-option asset {self.asset_internal_id} (Category: {self.asset_category.name})")

        qty_to_consume = quantity_contracts_to_consume.quantize(global_config.PRECISION_QUANTITY, context=self.ctx)
        if qty_to_consume <= Decimal(0):
            logger.warning(f"Quantity to consume for long option cost must be positive. Got {qty_to_consume}. Asset ID: {self.asset_internal_id}. Returning empty list.")
            return []

        consumed_lot_details: List[ConsumedLotDetail] = []
        quantity_remaining_to_consume = qty_to_consume
        lots_to_remove_indices: List[int] = []

        logger.debug(f"Attempting to consume {qty_to_consume} long option contracts for asset {self.asset_internal_id}...")

        for i, current_lot in enumerate(self.lots):
            if quantity_remaining_to_consume <= Decimal(0): break
            qty_available_in_lot = current_lot.quantity

            qty_consumed_from_this_lot: Decimal
            if qty_available_in_lot <= quantity_remaining_to_consume:
                qty_consumed_from_this_lot = qty_available_in_lot
                lots_to_remove_indices.append(i)
                logger.debug(f"  Fully consuming long option lot (Src: {current_lot.source_transaction_id}, Acq: {current_lot.acquisition_date}) Qty Contracts: {qty_consumed_from_this_lot}")
            else:
                qty_consumed_from_this_lot = quantity_remaining_to_consume
                remaining_contracts = self.ctx.subtract(current_lot.quantity, qty_consumed_from_this_lot)
                # Rescale the display-only original before the quantity moves.
                current_lot.total_cost_original = current_lot.rescaled_original(remaining_contracts)
                current_lot.quantity = remaining_contracts
                current_lot.total_cost_basis_eur = self.ctx.multiply(current_lot.quantity, current_lot.unit_cost_basis_eur) # Renamed
                logger.debug(f"  Partially consuming long option lot (Src: {current_lot.source_transaction_id}, Acq: {current_lot.acquisition_date}) Qty Contracts: {qty_consumed_from_this_lot}. Remaining Qty Contracts: {current_lot.quantity}")

            consumed_lot_details.append(ConsumedLotDetail(
                consumed_quantity=qty_consumed_from_this_lot,
                value_per_unit_eur=current_lot.unit_cost_basis_eur, # Renamed
                original_lot_date=current_lot.acquisition_date,
                original_lot_source_tx_id=current_lot.source_transaction_id
            ))
            quantity_remaining_to_consume = self.ctx.subtract(quantity_remaining_to_consume, qty_consumed_from_this_lot)

        for i in sorted(lots_to_remove_indices, reverse=True):
            logger.debug(f"  Removing fully consumed long option lot index {i} (Src: {self.lots[i].source_transaction_id})")
            del self.lots[i]

        small_tolerance_qty = Decimal('1e-10')
        if quantity_remaining_to_consume.copy_abs() > small_tolerance_qty:
            current_total_qty_in_lots = sum(l.quantity for l in self.lots)
            available_before_this_op = current_total_qty_in_lots + (qty_to_consume - quantity_remaining_to_consume)
            raise ValueError(f"Insufficient long option contracts for asset {self.asset_internal_id}. "
                             f"Required to consume: {qty_to_consume}, "
                             f"Total available before this consumption: {available_before_this_op}, "
                             f"Remaining to consume: {quantity_remaining_to_consume}.")

        logger.debug(f"Successfully consumed {qty_to_consume - quantity_remaining_to_consume} long option contracts. Details: {consumed_lot_details}")
        return consumed_lot_details


    def consume_short_option_get_proceeds(self, quantity_contracts_to_consume: Decimal) -> List[ConsumedLotDetail]:
        if self.asset_category != AssetCategory.OPTION:
             raise TypeError(f"consume_short_option_get_proceeds called on non-option asset {self.asset_internal_id} (Category: {self.asset_category.name})")

        qty_to_consume = quantity_contracts_to_consume.quantize(global_config.PRECISION_QUANTITY, context=self.ctx)
        if qty_to_consume <= Decimal(0):
            logger.warning(f"Quantity to consume for short option proceeds must be positive. Got {qty_to_consume}. Asset ID: {self.asset_internal_id}. Returning empty list.")
            return []

        consumed_lot_details: List[ConsumedLotDetail] = []
        quantity_remaining_to_consume = qty_to_consume
        short_lots_to_remove_indices: List[int] = []

        logger.debug(f"Attempting to consume {qty_to_consume} short option contracts for asset {self.asset_internal_id}...")

        for i, current_short_lot in enumerate(self.short_lots):
            if quantity_remaining_to_consume <= Decimal(0): break
            qty_available_in_lot = current_short_lot.quantity_shorted

            qty_consumed_from_this_lot: Decimal
            if qty_available_in_lot <= quantity_remaining_to_consume:
                qty_consumed_from_this_lot = qty_available_in_lot
                short_lots_to_remove_indices.append(i)
                logger.debug(f"  Fully consuming short option lot (Src: {current_short_lot.source_transaction_id}, Open: {current_short_lot.opening_date}) Qty Contracts: {qty_consumed_from_this_lot}")
            else:
                qty_consumed_from_this_lot = quantity_remaining_to_consume
                current_short_lot.quantity_shorted = self.ctx.subtract(current_short_lot.quantity_shorted, qty_consumed_from_this_lot)
                current_short_lot.total_sale_proceeds_eur = self.ctx.multiply(current_short_lot.quantity_shorted, current_short_lot.unit_sale_proceeds_eur) # Renamed
                logger.debug(f"  Partially consuming short option lot (Src: {current_short_lot.source_transaction_id}, Open: {current_short_lot.opening_date}) Qty Contracts: {qty_consumed_from_this_lot}. Remaining Qty Contracts: {current_short_lot.quantity_shorted}")

            consumed_lot_details.append(ConsumedLotDetail(
                consumed_quantity=qty_consumed_from_this_lot,
                value_per_unit_eur=current_short_lot.unit_sale_proceeds_eur, # Renamed
                original_lot_date=current_short_lot.opening_date,
                original_lot_source_tx_id=current_short_lot.source_transaction_id
            ))
            quantity_remaining_to_consume = self.ctx.subtract(quantity_remaining_to_consume, qty_consumed_from_this_lot)

        for i in sorted(short_lots_to_remove_indices, reverse=True):
            logger.debug(f"  Removing fully consumed short option lot index {i} (Src: {self.short_lots[i].source_transaction_id})")
            del self.short_lots[i]

        small_tolerance_qty = Decimal('1e-10')
        if quantity_remaining_to_consume.copy_abs() > small_tolerance_qty:
            current_total_qty_in_lots = sum(sl.quantity_shorted for sl in self.short_lots)
            available_before_this_op = current_total_qty_in_lots + (qty_to_consume - quantity_remaining_to_consume)
            raise ValueError(f"Insufficient short option contracts for asset {self.asset_internal_id}. "
                             f"Required to consume: {qty_to_consume}, "
                             f"Total available before this consumption: {available_before_this_op}, "
                             f"Remaining to consume: {quantity_remaining_to_consume}.")

        logger.debug(f"Successfully consumed {qty_to_consume - quantity_remaining_to_consume} short option contracts. Details: {consumed_lot_details}")
        return consumed_lot_details


    def get_current_position_quantity(self) -> Decimal:
        current_long_qty = sum(lot.quantity for lot in self.lots) if self.lots else Decimal(0)
        current_short_qty_abs = sum(short_lot.quantity_shorted for short_lot in self.short_lots) if self.short_lots else Decimal(0)

        net_quantity = self.ctx.subtract(current_long_qty, current_short_qty_abs)
        return net_quantity.quantize(global_config.PRECISION_QUANTITY, context=self.ctx)

    def reduce_cost_basis_for_capital_repayment(self, repayment_amount_eur: Decimal) -> Decimal:
        """
        Reduces cost basis of FIFO lots for tax-free capital repayments.
        Returns excess amount that becomes taxable income.

        The repayment is paid PER SHARE, so the reduction is allocated
        PRO-RATA by lot quantity — a sequential oldest-first reduction
        misallocated basis between (potentially exempt) old lots and
        taxable new ones even though the totals matched. A lot's basis
        floors at zero; whatever cannot be absorbed by any lot's basis
        is returned as taxable excess.
        """
        if repayment_amount_eur <= Decimal('0') or not self.lots:
            return repayment_amount_eur

        total_qty = sum(lot.quantity for lot in self.lots)
        if total_qty <= Decimal('0'):
            return repayment_amount_eur

        per_unit = self.ctx.divide(repayment_amount_eur, total_qty)
        remaining_repayment = repayment_amount_eur

        def _reduce(lot, amount):
            nonlocal remaining_repayment
            reduction = min(amount, lot.total_cost_basis_eur)
            if reduction <= Decimal('0'):
                return
            lot.total_cost_basis_eur = self.ctx.subtract(lot.total_cost_basis_eur, reduction)
            lot.unit_cost_basis_eur = self.ctx.divide(lot.total_cost_basis_eur, lot.quantity) if lot.quantity > Decimal('0') else Decimal('0')
            if lot.cost_components is not None:
                # The repayment returns part of each payment in proportion.
                lot.cost_components = _scale_components(
                    lot.cost_components, lot.total_cost_basis_eur, self.ctx)
            # The reduction is an EUR amount. Subtracting it from a USD original
            # would need a rate this function has no business choosing, and
            # scaling by the EUR ratio would invent one — so the original is
            # dropped and the caller falls back to the EUR basis, which is the
            # only figure that reflects the repayment.
            lot.total_cost_original = None
            lot.cost_currency = None
            remaining_repayment = self.ctx.subtract(remaining_repayment, reduction)

        # Pass 1: pro-rata by quantity.
        for lot in self.lots:
            _reduce(lot, self.ctx.multiply(per_unit, lot.quantity))

        # Pass 2: remainder left by exhausted lots (or rounding) is absorbed
        # by whatever basis is still available before becoming excess.
        if remaining_repayment > Decimal('0'):
            for lot in self.lots:
                if remaining_repayment <= Decimal('0'):
                    break
                _reduce(lot, remaining_repayment)

        if remaining_repayment < Decimal('1e-9'):
            remaining_repayment = Decimal('0')
        return remaining_repayment  # Excess that becomes taxable income
