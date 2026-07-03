# src/main.py
import logging
import sys
from decimal import getcontext
import os # For path operations if needed for PDF output

# Configuration and CLI
import src.config as config
from src.cli import parse_arguments

# Core pipeline runner
from src.pipeline_runner import run_core_processing_pipeline, ProcessingOutput

# Loss Offsetting Engine
from src.engine.loss_offsetting import LossOffsettingEngine

# Reporting
from src.reporting.console_reporter import generate_console_tax_report, generate_stock_trade_report_for_symbol
from src.reporting.diagnostic_reports import (
    print_grouped_event_details,
    print_asset_positions_diagnostic,
    print_assets_by_category_diagnostic,
    print_object_counts_diagnostic,
    print_realized_gains_losses_diagnostic,
    print_vorabpauschale_diagnostic,
    print_asset_pl_summary_debug
)
from src.reporting.pdf_generator import PdfReportGenerator # Added PDF Generator

# Configure logging (can be moved to a dedicated setup function if complex)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def setup_decimal_context():
    """Sets the global decimal precision and rounding mode."""
    getcontext().prec = config.INTERNAL_CALCULATION_PRECISION # Renamed from INTERNAL_WORKING_PRECISION
    valid_rounding_modes = ["ROUND_CEILING", "ROUND_DOWN", "ROUND_FLOOR", "ROUND_HALF_DOWN",
                            "ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_UP", "ROUND_05UP"]
    rounding_mode_to_set = config.DECIMAL_ROUNDING_MODE
    if rounding_mode_to_set not in valid_rounding_modes:
        logger.warning(f"Invalid DECIMAL_ROUNDING_MODE '{rounding_mode_to_set}' in config. Using ROUND_HALF_UP as fallback.")
        rounding_mode_to_set = "ROUND_HALF_UP"

    getcontext().rounding = rounding_mode_to_set
    logger.info(f"Global decimal precision set to {getcontext().prec}, rounding mode to {getcontext().rounding}.")


def main_application():
    """
    Main application entry point.
    Parses arguments, runs processing, and generates reports.
    """
    args = parse_arguments()
    setup_decimal_context()

    logger.info(f"Starting IBKR Tax Declaration Engine (country={args.country})...")

    try:
        processing_results: ProcessingOutput = run_core_processing_pipeline(
            trades_file_path=args.trades,
            cash_transactions_file_path=args.cash,
            positions_start_file_path=args.pos_start,
            positions_end_file_path=args.pos_end,
            corporate_actions_file_path=args.corp_actions,
            interactive_classification_mode=args.interactive,
            tax_year_to_process=config.TAX_YEAR,
            country_code=args.country,
        )
    except Exception as e:
        logger.critical(f"Core processing pipeline failed: {e}. Exiting.", exc_info=True)
        sys.exit(1)

    loss_offsetting_summary = None
    if args.report_tax_declaration or args.pdf_output_file: # Calculate if any report needing it is active
        logger.info("Calculating final tax figures with loss offsetting...")
        try:
            loss_engine = LossOffsettingEngine(
                realized_gains_losses=processing_results.realized_gains_losses,
                vorabpauschale_items=processing_results.vorabpauschale_items,
                # THE FIX IS HERE: Use processed_income_events instead of all_financial_events_enriched
                current_year_financial_events=processing_results.processed_income_events,
                asset_resolver=processing_results.asset_resolver,
                tax_year=config.TAX_YEAR,
                apply_conceptual_derivative_loss_capping=config.APPLY_CONCEPTUAL_DERIVATIVE_LOSS_CAPPING
            )
            loss_offsetting_summary = loss_engine.calculate_reporting_figures()
            logger.info("Loss offsetting calculation completed.")
        except Exception as e:
            logger.error(f"Loss offsetting calculation failed: {e}. Tax reports might be incomplete or inaccurate.", exc_info=True)

    asset_resolver = processing_results.asset_resolver
    tax_year = config.TAX_YEAR

    if args.group_by_type:
        print_assets_by_category_diagnostic(asset_resolver)
        print_asset_positions_diagnostic(asset_resolver)
        # For diagnostic output, it might still be useful to see all events
        print_grouped_event_details(processing_results.all_financial_events_enriched, asset_resolver)
        print_realized_gains_losses_diagnostic(processing_results.realized_gains_losses, asset_resolver)
        print_vorabpauschale_diagnostic(processing_results.vorabpauschale_items)

    if args.count_objects:
        print_object_counts_diagnostic(
            asset_resolver=asset_resolver,
            all_events=processing_results.all_financial_events_enriched, # Display count of all
            rgl_items=processing_results.realized_gains_losses,
            vp_items=processing_results.vorabpauschale_items
        )

    if args.debug_asset_summary:
        print_asset_pl_summary_debug(
            asset_resolver=asset_resolver,
            realized_gains_losses=processing_results.realized_gains_losses
        )

    if args.report_tax_declaration:
        if loss_offsetting_summary:
            generate_console_tax_report(
                realized_gains_losses=processing_results.realized_gains_losses,
                vorabpauschale_items=processing_results.vorabpauschale_items,
                # The console reporter uses this list and filters it itself for its detailed views
                all_financial_events=processing_results.all_financial_events_enriched,
                asset_resolver=asset_resolver,
                tax_year=tax_year,
                eoy_mismatch_count=processing_results.eoy_mismatch_error_count,
                loss_offsetting_summary=loss_offsetting_summary
            )
        else:
            logger.error("Console tax declaration report cannot be generated because loss offsetting calculation failed or was skipped.")

    if args.pdf_output_file:
        if loss_offsetting_summary:
            logger.info(f"Generating PDF report to {args.pdf_output_file}...")
            eoy_mismatch_details_for_pdf = []
            if processing_results.eoy_mismatch_error_count > 0 and not eoy_mismatch_details_for_pdf:
                 logger.warning(f"EOY mismatch count is {processing_results.eoy_mismatch_error_count}, but detailed mismatch data is not available for the PDF report. The PDF section will be limited.")

            pdf_generator = PdfReportGenerator(
                loss_offsetting_result=loss_offsetting_summary,
                # The PDF report should also use correctly filtered events for income sections
                all_financial_events=processing_results.processed_income_events,
                realized_gains_losses=processing_results.realized_gains_losses,
                vorabpauschale_items=processing_results.vorabpauschale_items,
                assets_by_id=asset_resolver.assets_by_internal_id,
                tax_year=tax_year,
                eoy_mismatch_details=eoy_mismatch_details_for_pdf,
                report_version="v3.2.3" # Updated to match PRD version reflecting this fix
            )
            pdf_generator.generate_report(args.pdf_output_file)
        else:
            logger.error(f"PDF report '{args.pdf_output_file}' cannot be generated because loss offsetting calculation failed or was skipped.")


    if args.report_stock_trades_details:
        generate_stock_trade_report_for_symbol(
            stock_symbol_arg=args.report_stock_trades_details,
            # This report filters events itself, so passing all enriched is fine
            all_financial_events=processing_results.all_financial_events_enriched,
            rgl_items=processing_results.realized_gains_losses,
            asset_resolver=asset_resolver,
            tax_year=tax_year
        )

    # --- CZ aggregation: JSON/XLSX exports and/or FX-mode comparison ---
    cz_fx_mode = getattr(args, "cz_fx_mode", "daily")
    if args.output_json or args.output_xlsx or cz_fx_mode == "compare":
        if args.country == "cz":
            from src.countries.registry import get_tax_plugin
            from src.countries.cz.config import CzTaxConfig
            from src.countries.cz.fx_policy import uniform_fx_policy
            from src.countries.cz.uniform_rates import CzUniformRateProvider
            from src.utils.fx_provider_factory import create_fx_provider

            # Without an FX provider the plugin degrades to EUR-only output
            # (no CZK figures, annual limit and rate threshold inactive), so
            # a real CZ run always wires a provider for the chosen mode.
            def _cz_aggregate(mode: str):
                if mode == "uniform":
                    cfg = CzTaxConfig(fx_policy=uniform_fx_policy())
                    provider = CzUniformRateProvider()
                else:
                    cfg = CzTaxConfig()
                    provider = create_fx_provider(
                        cfg.fx_policy.source,
                        cache_file_path=cfg.cnb_cache_file_path,
                    )
                plugin = get_tax_plugin("cz", config=cfg, fx_provider=provider)
                return plugin.get_tax_aggregator().aggregate(
                    realized_gains_losses=processing_results.realized_gains_losses,
                    financial_events=processing_results.processed_income_events,
                    asset_resolver=processing_results.asset_resolver,
                    tax_year=config.TAX_YEAR,
                )

            if cz_fx_mode == "compare":
                from src.countries.cz.fx_mode_compare import CzFxModeComparison
                comparison = CzFxModeComparison(
                    daily=_cz_aggregate("daily"),
                    uniform=_cz_aggregate("uniform"),
                )
                for line in comparison.render_lines():
                    print(line)
                cz_exports = [("daily", comparison.daily), ("uniform", comparison.uniform)]
            else:
                cz_exports = [(cz_fx_mode, _cz_aggregate(cz_fx_mode))]

            def _mode_suffixed(path: str, mode: str) -> str:
                # In compare mode both results are exported side by side:
                # out.json -> out.daily.json / out.uniform.json
                if cz_fx_mode != "compare":
                    return path
                root, dot, ext = path.rpartition(".")
                return f"{root}.{mode}.{ext}" if dot else f"{path}.{mode}"

            for mode, cz_result in cz_exports:
                if args.output_json:
                    from src.countries.cz.exporters.json_exporter import export_cz_to_json
                    out_path = _mode_suffixed(args.output_json, mode)
                    export_cz_to_json(cz_result, output=out_path)
                    logger.info(f"CZ JSON export ({mode} FX mode) written to {out_path}")
                if args.output_xlsx:
                    from src.countries.cz.exporters.xlsx_exporter import export_cz_to_xlsx
                    out_path = _mode_suffixed(args.output_xlsx, mode)
                    export_cz_to_xlsx(cz_result, output=out_path)
                    logger.info(f"CZ XLSX export ({mode} FX mode) written to {out_path}")
        else:
            logger.warning(f"JSON/XLSX export and --cz-fx-mode are currently only supported for --country cz, not '{args.country}'.")

    logger.info("Processing finished.")
    if processing_results.eoy_mismatch_error_count > 0:
        logger.warning(f"There were {processing_results.eoy_mismatch_error_count} EOY quantity mismatch errors. Review logs and output carefully.")

if __name__ == "__main__":
    main_application()
