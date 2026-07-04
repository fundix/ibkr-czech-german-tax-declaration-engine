# src/main.py
import logging
import sys
import os # For path operations if needed for PDF output

# Configuration and CLI
import src.config as config
from src.cli import parse_arguments
from src.utils.decimal_context import setup_decimal_context

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


def main_application():
    """
    Main application entry point.
    Parses arguments, runs processing, and generates reports.
    """
    args = parse_arguments()
    setup_decimal_context()

    logger.info(f"Starting IBKR Tax Declaration Engine (country={args.country})...")

    from src.engine.pairing import PairingMethod
    cz_pairing_method = getattr(args, "cz_pairing_method", "fifo")
    # For a single chosen method, run the core with it up front; 'compare'
    # re-runs the core per method later via the matrix driver, so start on FIFO.
    _initial_pairing = (
        PairingMethod.FIFO if cz_pairing_method == "compare"
        else PairingMethod(cz_pairing_method)
    )

    def _run_pipeline_for(method: PairingMethod) -> ProcessingOutput:
        return run_core_processing_pipeline(
            trades_file_path=args.trades,
            cash_transactions_file_path=args.cash,
            positions_start_file_path=args.pos_start,
            positions_end_file_path=args.pos_end,
            corporate_actions_file_path=args.corp_actions,
            interactive_classification_mode=args.interactive,
            tax_year_to_process=args.tax_year,
            country_code=args.country,
            pairing_method=method,
        )

    try:
        processing_results: ProcessingOutput = _run_pipeline_for(_initial_pairing)
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
                tax_year=args.tax_year,
                apply_conceptual_derivative_loss_capping=config.APPLY_CONCEPTUAL_DERIVATIVE_LOSS_CAPPING
            )
            loss_offsetting_summary = loss_engine.calculate_reporting_figures()
            logger.info("Loss offsetting calculation completed.")
        except Exception as e:
            logger.error(f"Loss offsetting calculation failed: {e}. Tax reports might be incomplete or inaccurate.", exc_info=True)

    asset_resolver = processing_results.asset_resolver
    tax_year = args.tax_year

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

    # --- CZ aggregation: JSON/XLSX exports and/or FX/pairing comparison ---
    cz_fx_mode = getattr(args, "cz_fx_mode", "daily")
    wants_export = bool(args.output_json or args.output_xlsx or args.output_pdf)
    if wants_export or cz_fx_mode == "compare" or cz_pairing_method == "compare":
        if args.country == "cz":
            from src.countries.cz.aggregation_service import (
                run_cz_aggregation,
                run_cz_compare,
                run_cz_pairing_matrix,
            )
            from src.engine.pairing import ALL_METHODS

            if cz_pairing_method == "compare":
                # Full FX-mode × pairing-method matrix; export the cheapest cell.
                fx_modes = ["daily", "uniform"] if cz_fx_mode == "compare" else [cz_fx_mode]
                matrix = run_cz_pairing_matrix(
                    _run_pipeline_for, args.tax_year, fx_modes, ALL_METHODS
                )
                for line in matrix.render_lines():
                    print(line)
                cz_exports = []
                if matrix.best_cell is not None and matrix.best_result is not None:
                    fx, method_value = matrix.best_cell
                    cz_exports = [(f"{fx}.{method_value}", matrix.best_result)]
                force_suffix = True
            elif cz_fx_mode == "compare":
                comparison = run_cz_compare(processing_results, args.tax_year)
                for line in comparison.render_lines():
                    print(line)
                cz_exports = [("daily", comparison.daily), ("uniform", comparison.uniform)]
                force_suffix = True
            else:
                result = run_cz_aggregation(processing_results, args.tax_year, cz_fx_mode)
                if cz_pairing_method != "fifo":
                    cz_exports = [(f"{cz_fx_mode}.{cz_pairing_method}", result)]
                    force_suffix = True
                else:
                    cz_exports = [(cz_fx_mode, result)]
                    force_suffix = False

            def _mode_suffixed(path: str, mode: str) -> str:
                # In compare modes each result is exported side by side:
                # out.json -> out.daily.json / out.daily.optimal.json
                if not force_suffix:
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
                if args.output_pdf:
                    from src.countries.cz.exporters.pdf_exporter import export_cz_to_pdf
                    out_path = _mode_suffixed(args.output_pdf, mode)
                    export_cz_to_pdf(
                        cz_result,
                        output=out_path,
                        taxpayer_name=getattr(config, "TAXPAYER_NAME", None),
                        account_id=getattr(config, "ACCOUNT_ID", None),
                    )
                    logger.info(f"CZ PDF export ({mode} FX mode) written to {out_path}")
        else:
            logger.warning(f"JSON/XLSX export and --cz-fx-mode are currently only supported for --country cz, not '{args.country}'.")

    logger.info("Processing finished.")
    if processing_results.eoy_mismatch_error_count > 0:
        logger.warning(f"There were {processing_results.eoy_mismatch_error_count} EOY quantity mismatch errors. Review logs and output carefully.")

if __name__ == "__main__":
    main_application()
