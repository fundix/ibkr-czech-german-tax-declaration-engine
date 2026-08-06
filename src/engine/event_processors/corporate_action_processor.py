# src/engine/event_processors/corporate_action_processor.py
import logging
from decimal import Decimal
from typing import List, Dict, Any 

from src.domain.events import (
    CorpActionSplitForward, CorpActionMergerCash, CorpActionStockDividend, CorpActionMergerStock,
    CorporateActionEvent, FinancialEvent, CorpActionExpireDividendRights
)
from src.domain.results import RealizedGainLoss
from src.engine.fifo_manager import FifoLedger
from .base_processor import EventProcessor
from src.domain.enums import FinancialEventType # Added for checking event type
from src.engine.merger_policy import MergerMechanics, merger_event_key
from src.utils.type_utils import parse_ibkr_date

logger = logging.getLogger(__name__)

def _format_asset_info(asset_obj) -> str:
    """Helper to format asset information for logging."""
    if not asset_obj:
        return "Unknown Asset"
    desc = asset_obj.description or asset_obj.get_classification_key()
    symbol = asset_obj.ibkr_symbol or "N/A"
    return f"'{desc}' (Symbol: {symbol})"

class SplitProcessor(EventProcessor):
    def process(self, event: FinancialEvent, ledger: FifoLedger, context: Dict[str, Any]) -> List[RealizedGainLoss]:
        if not ledger:
            logger.error(f"SplitProcessor received event {event.event_id} but no ledger provided. Cannot process.")
            return []
        if not isinstance(event, CorpActionSplitForward):
            logger.error(f"SplitProcessor received incorrect event type: {type(event).__name__} (ID: {event.event_id}).")
            return []
        # Check using renamed FinancialEventType from enums.py
        if event.event_type != FinancialEventType.CORP_SPLIT_FORWARD:
            logger.error(f"SplitProcessor received event with type {event.event_type} but expected CORP_SPLIT_FORWARD. ID: {event.event_id}")
            return []
        try:
            logger.info(f"Processing {event.event_type.name} for asset {ledger.asset_internal_id} on {event.event_date} (ID: {event.event_id}). Ratio: {event.new_shares_per_old_share}")
            ledger.adjust_lots_for_split(event)
        except Exception as e:
            logger.error(f"Error processing Split event {event.event_id} in ledger for asset {ledger.asset_internal_id}: {e}", exc_info=True)
        return []

class MergerCashProcessor(EventProcessor):
    def process(self, event: FinancialEvent, ledger: FifoLedger, context: Dict[str, Any]) -> List[RealizedGainLoss]:
        if not ledger:
            logger.error(f"MergerCashProcessor received event {event.event_id} but no ledger provided. Cannot process.")
            return []
        if not isinstance(event, CorpActionMergerCash):
            logger.error(f"MergerCashProcessor received incorrect event type: {type(event).__name__} (ID: {event.event_id}).")
            return []
        if event.event_type != FinancialEventType.CORP_MERGER_CASH:
            logger.error(f"MergerCashProcessor received event with type {event.event_type} but expected CORP_MERGER_CASH. ID: {event.event_id}")
            return []
        try:
            logger.info(f"Processing {event.event_type.name} for asset {ledger.asset_internal_id} on {event.event_date} (ID: {event.event_id}). Cash/Share: {event.cash_per_share_eur} EUR")
            if event.cash_per_share_eur is None:
                 logger.error(f"Cash Merger event {event.event_id} is missing cash_per_share_eur. Cannot process.")
                 return []
            realized_gains_losses = ledger.consume_all_lots_for_cash_merger(event)
            logger.info(f"Cash Merger generated {len(realized_gains_losses)} RealizedGainLoss records.")
            return realized_gains_losses
        except ValueError as e:
             logger.critical(f"Critical error processing Cash Merger {event.event_id} in ledger for asset {ledger.asset_internal_id}: {e}", exc_info=True)
             raise e
        except Exception as e:
            logger.error(f"Unexpected error processing Cash Merger event {event.event_id} for asset {ledger.asset_internal_id}: {e}", exc_info=True)
            return []


class StockDividendProcessor(EventProcessor):
    def process(self, event: FinancialEvent, ledger: FifoLedger, context: Dict[str, Any]) -> List[RealizedGainLoss]:
        if not ledger:
            logger.error(f"StockDividendProcessor received event {event.event_id} but no ledger provided. Cannot process.")
            return []
        if not isinstance(event, CorpActionStockDividend):
             logger.error(f"StockDividendProcessor received incorrect event type: {type(event).__name__} (ID: {event.event_id}).")
             return []
        if event.event_type != FinancialEventType.CORP_STOCK_DIVIDEND:
            logger.error(f"StockDividendProcessor received event with type {event.event_type} but expected CORP_STOCK_DIVIDEND. ID: {event.event_id}")
            return []
        try:
             logger.info(f"Processing {event.event_type.name} for asset {ledger.asset_internal_id} on {event.event_date} (ID: {event.event_id}). New Shares: {event.quantity_new_shares_received} (German tax: zero cost basis)")
             # FMV no longer required - German tax treatment uses zero cost basis
             ledger.add_lot_for_stock_dividend(event)
        except ValueError as e:
             logger.critical(f"Critical error processing Stock Dividend {event.event_id} in ledger for asset {ledger.asset_internal_id}: {e}", exc_info=True)
             raise e
        except Exception as e:
            logger.error(f"Error processing Stock Dividend event {event.event_id} in ledger for asset {ledger.asset_internal_id}: {e}", exc_info=True)
        return []

class MergerStockProcessor(EventProcessor):
    def process(self, event: FinancialEvent, ledger: FifoLedger, context: Dict[str, Any]) -> List[RealizedGainLoss]:
        if not ledger:
             logger.error(f"MergerStockProcessor received event {event.event_id} but no source ledger provided. Cannot process.")
             return []
        if not isinstance(event, CorpActionMergerStock):
             logger.error(f"MergerStockProcessor received incorrect event type: {type(event).__name__} (ID: {event.event_id}).")
             return []
        if event.event_type != FinancialEventType.CORP_MERGER_STOCK:
            logger.error(f"MergerStockProcessor received event with type {event.event_type} but expected CORP_MERGER_STOCK. ID: {event.event_id}")
            return []

        # A stock-for-stock merger is either a deferral (§23b/§23c: cost and
        # holding period carry over) or a taxable disposal at the fair value of
        # the consideration. Which one cannot be read off the statement — it
        # follows from the tax residence and legal form of the companies — so
        # the regime is recorded per event and looked up here.
        #
        # Returning [] as this used to would leave the old asset's lots in place
        # and the new asset with none: wrong positions, and a realised gain or a
        # taxed deferral either silently missing or silently invented. Refusing
        # is the only safe answer while the mechanics are unimplemented.
        asset_resolver = context.get('asset_resolver')
        old_asset = asset_resolver.get_asset_by_id(event.asset_internal_id) if asset_resolver else None
        new_asset = asset_resolver.get_asset_by_id(event.new_asset_internal_id) if asset_resolver else None

        key = merger_event_key(
            action_id=getattr(event, 'ca_action_id_ibkr', None) or event.ibkr_transaction_id,
            event_date=event.event_date,
            old_symbol=getattr(old_asset, 'ibkr_symbol', None),
            new_symbol=getattr(new_asset, 'ibkr_symbol', None),
        )
        policy = context.get('merger_policy')
        if policy is None:
            raise ValueError(
                f"Stock-for-stock merger '{key}' cannot be processed: no merger "
                f"policy is configured for this run, so there is nothing to say "
                f"whether the lots carry over or are realised."
            )

        decision = policy.decide(key)
        if not decision.is_decided:
            # The policy writes this text — it is the one that knows the law.
            raise ValueError(decision.reason)

        if decision.mechanics is MergerMechanics.CARRY_OVER:
            return self._carry_over(event, ledger, context, key, decision,
                                    old_asset, new_asset)

        if decision.mechanics is MergerMechanics.TAXABLE_DISPOSAL:
            return self._taxable_disposal(event, ledger, context, key, decision,
                                          old_asset, new_asset)

        raise ValueError(
            f"Stock-for-stock merger '{key}' resolves to "
            f"{decision.mechanics.name}"
            f"{f' [{decision.label}]' if decision.label else ''}, but applying "
            f"it is not implemented yet — refusing rather than reporting "
            f"figures that ignore the merger."
        )

    def _taxable_disposal(self, event, ledger, context, key, decision,
                          old_asset, new_asset) -> List[RealizedGainLoss]:
        """Realise the old shares at the consideration's fair value.

        The path an ordinary US stock-for-stock merger takes: outside
        §23b/§23c the exchange is a disposal of the old shares measured at the
        fair value of what was received, and the new shares start a fresh cost
        basis and a fresh holding period.

        Fair value is the new share's closing price on the merger date, taken
        through the injected price provider (§19 zákona o oceňování majetku,
        including its 30-day look-back) and converted to the engine's base
        currency at that same date. Both are refused rather than guessed when
        unavailable: a merger valued at the wrong price is a wrong §10 gain.
        """
        self._guard_common(event, context, key, old_asset)

        price_provider = context.get('security_price_provider')
        if price_provider is None:
            raise ValueError(
                f"Stock-for-stock merger '{key}' is a taxable disposal, which "
                "has to be valued at the consideration's fair value, but the run "
                "provided no security price provider."
            )
        converter = context.get('currency_converter')
        if converter is None:
            raise ValueError(
                f"Stock-for-stock merger '{key}': cannot convert the "
                "consideration's fair value without a currency converter."
            )

        merger_date = parse_ibkr_date(event.event_date)
        new_symbol = getattr(new_asset, 'ibkr_symbol', None)
        new_currency = getattr(new_asset, 'currency', None)
        if not new_symbol:
            raise ValueError(
                f"Stock-for-stock merger '{key}': the asset received has no "
                "symbol, so its market price cannot be looked up."
            )

        quote = price_provider.get_close(new_symbol, new_currency or "USD", merger_date)
        if quote is None:
            raise ValueError(
                f"Stock-for-stock merger '{key}' is a taxable disposal and needs "
                f"the closing price of {new_symbol} on {event.event_date}, but no "
                "price is available for that date nor in the 30 days before it. "
                "Supply it manually or add a symbol override in "
                "data/webapp/symbol_map.json."
            )

        unit_value_eur = converter.convert_to_eur(
            original_amount=quote.price,
            original_currency=quote.currency,
            date_of_conversion=quote.price_date,
        )
        if unit_value_eur is None:
            raise ValueError(
                f"Stock-for-stock merger '{key}': cannot convert "
                f"{quote.price} {quote.currency} to EUR for {quote.price_date}."
            )
        unit_value_eur = Decimal(str(unit_value_eur))

        if not ledger.lots:
            raise ValueError(
                f"Stock-for-stock merger '{key}' has no long lots to realise: the "
                f"source ledger for {getattr(old_asset, 'ibkr_symbol', '?')} is "
                "empty. Either the corporate-action row is duplicated, the "
                "position is missing from the start-of-year file, or its purchases "
                "were dropped for a failed FX conversion."
            )
        if ledger.short_lots:
            raise ValueError(
                f"Stock-for-stock merger '{key}' holds short lots on the disposing "
                "side. Realising a short position against the acquirer's share "
                "price is not the same transaction as closing it, and the correct "
                "treatment is undecided — refusing rather than inventing one."
            )

        realized, credited = ledger.realize_all_lots_for_merger(
            unit_value_eur=unit_value_eur,
            ratio=event.new_shares_received_per_old,
            merger_date=event.event_date,
        )

        # The new shares start clean: their cost is what they were just valued
        # at, and their holding period starts now — no carry-over applies.
        target_ledger = context['ledger_for'](event.new_asset_internal_id)
        source_id = (
            getattr(event, 'ca_action_id_ibkr', None)
            or event.ibkr_transaction_id
            or f"MERGER_{event.event_date}"
        )
        credited_total = target_ledger.open_lots_at_merger_value(
            quantities=credited,
            unit_value_eur=unit_value_eur,
            merger_date=event.event_date,
            source_transaction_id=str(source_id),
            carried_from_prefix=getattr(old_asset, 'ibkr_symbol', None) or "?",
        )

        applied = context.get('applied_merger_keys')
        if applied is not None:
            applied.add(key)

        logger.info(
            f"Merger '{key}' [{decision.label}]: realised {len(realized)} lot(s) "
            f"of {getattr(old_asset, 'ibkr_symbol', '?')} at "
            f"{unit_value_eur} EUR per {new_symbol} share (close of "
            f"{quote.price_date}{' — look-back applied' if quote.used_lookback else ''}), "
            f"and opened {credited_total} share(s) at that value. Not a deferral: the "
            "holding period restarts."
        )
        return realized

    def _guard_common(self, event, context, key, old_asset) -> None:
        """Refusals that apply to either mechanics, all before any mutation."""
        applied = context.get('applied_merger_keys')
        if applied is not None and key in applied:
            raise ValueError(
                f"Stock-for-stock merger '{key}' was already applied in this "
                "run — the corporate-action row appears to be duplicated."
            )
        if event.asset_internal_id == event.new_asset_internal_id:
            raise ValueError(
                f"Stock-for-stock merger '{key}' names the same asset as both "
                f"source and target ({getattr(old_asset, 'ibkr_symbol', '?')}) — "
                "this is the receiving leg and carries no lots."
            )
        qty_exchanged = getattr(event, 'quantity_exchanged', None)
        if qty_exchanged is not None and qty_exchanged > Decimal(0):
            raise ValueError(
                f"Stock-for-stock merger '{key}' reports a positive quantity "
                f"({qty_exchanged}): this is the receiving leg of action "
                f"{getattr(event, 'ca_action_id_ibkr', None)}."
            )
        ratio = event.new_shares_received_per_old
        if (ratio is None or not isinstance(ratio, Decimal)
                or not ratio.is_finite() or ratio <= Decimal(0)):
            raise ValueError(
                f"Stock-for-stock merger '{key}' has an unusable exchange ratio "
                f"({ratio!r}), scraped from: "
                f"{getattr(event, 'ibkr_activity_description', None)!r}."
            )
        if parse_ibkr_date(event.event_date) is None:
            raise ValueError(
                f"Stock-for-stock merger '{key}' has an unparseable date "
                f"'{event.event_date}'."
            )
        if context.get('ledger_for') is None:
            raise ValueError(
                f"Stock-for-stock merger '{key}' cannot be applied: the run did "
                "not provide a ledger accessor, so the target asset's lots are "
                "unreachable. This is an engine wiring error, not a data problem."
            )

    def _carry_over(self, event, ledger, context, key, decision,
                    old_asset, new_asset) -> List[RealizedGainLoss]:
        """Move the lots to the new asset, keeping cost and the running holding.

        Every refusal happens BEFORE either ledger is touched, so a rejected
        merger leaves no partial state. Only the two rescale failures can fire
        after the drain, and those roll the source ledger back.
        """
        self._guard_common(event, context, key, old_asset)

        ratio = event.new_shares_received_per_old
        ledger_for = context['ledger_for']
        target_ledger = ledger_for(event.new_asset_internal_id)
        applied = context.get('applied_merger_keys')

        # R7 — an empty source is indistinguishable between a duplicated row, a
        # position absent from the SOY file, and buys dropped for failed FX. All
        # three mean the carried basis and holding start would be lost silently:
        # the EOY check compares quantities and cannot see a missing cost basis.
        if not ledger.lots and not ledger.short_lots:
            raise ValueError(
                f"Stock-for-stock merger '{key}' has no lots to carry: the source "
                f"ledger for {getattr(old_asset, 'ibkr_symbol', '?')} is empty. "
                "Either the corporate-action row is duplicated, the position is "
                "missing from the start-of-year file, or its purchases were dropped "
                "for a failed FX conversion. Transferring nothing would silently "
                "lose the carried acquisition cost and holding period."
            )

        source_id = (
            getattr(event, 'ca_action_id_ibkr', None)
            or event.ibkr_transaction_id
            or f"MERGER_{event.event_date}"
        )
        old_symbol = getattr(old_asset, 'ibkr_symbol', None) or "?"

        long_lots = ledger.drain_all_long_lots()
        short_lots = ledger.drain_all_short_lots()
        try:
            transferred = target_ledger.receive_carried_lots_from_merger(
                long_lots=long_lots,
                short_lots=short_lots,
                ratio=ratio,
                merger_date=event.event_date,
                source_transaction_id=str(source_id),
                carried_from_prefix=old_symbol,
            )
        except Exception:
            # R10/R11 and any lot-validation failure: put the source back exactly
            # as it was. The target was only ever extended on success.
            ledger.restore_drained_lots(long_lots, short_lots)
            raise

        if applied is not None:
            applied.add(key)

        earliest = min(
            (lot.holding_period_start for lot in long_lots if lot.holding_period_start),
            default=None,
        )
        logger.info(
            f"Merger '{key}' [{decision.label}]: carried "
            f"{len(long_lots)} long + {len(short_lots)} short lot(s) from "
            f"{old_symbol} to {getattr(new_asset, 'ibkr_symbol', '?')} at ratio "
            f"{ratio} — {transferred} share(s) credited, holding period running "
            f"from {earliest}. No realised gain: this is a deferral."
        )
        return []

class ExpireDividendRightsProcessor(EventProcessor):
    def process(self, event: FinancialEvent, ledger: FifoLedger, context: Dict[str, Any]) -> List[RealizedGainLoss]:
        if not isinstance(event, CorpActionExpireDividendRights):
            logger.error(f"ExpireDividendRightsProcessor received incorrect event type: {type(event).__name__} (ID: {event.event_id}).")
            return []
        
        # These events are used only for post-processing DI/ED consolidation, no FIFO ledger processing needed
        return []

class GenericCorporateActionProcessor(EventProcessor):
     def process(self, event: FinancialEvent, ledger: FifoLedger, context: Dict[str, Any]) -> List[RealizedGainLoss]:
        if not isinstance(event, CorporateActionEvent):
            logger.error(f"GenericCorporateActionProcessor received non-CorporateActionEvent type: {type(event).__name__} (ID: {event.event_id}).")
            return []
        
        # Get asset information for better logging
        asset_resolver = context.get('asset_resolver')
        asset_obj = asset_resolver.get_asset_by_id(event.asset_internal_id) if asset_resolver else None
        
        ledger_id_str = f"ledger for asset {ledger.asset_internal_id}" if ledger else "no ledger provided"
        logger.warning(f"No specific processor found for Corporate Action type {event.event_type.name} for asset {_format_asset_info(asset_obj)} (IBKR Action ID: {getattr(event, 'ca_action_id_ibkr', 'N/A')}, Event ID: {event.event_id}) with {ledger_id_str}. No ledger modifications performed.")
        return []
