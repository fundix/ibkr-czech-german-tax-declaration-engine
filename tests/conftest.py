# tests/conftest.py
import pytest
import tempfile
import os
from decimal import Decimal, getcontext, ROUND_HALF_UP # Default rounding for tests if config fails

# Attempt to import app_config from the refactored structure
try:
    from src import config as app_config
except ImportError:
    # Fallback if src.config is not found (e.g., PYTHONPATH issues during test discovery)
    class MockAppConfig:
        INTERNAL_CALCULATION_PRECISION = 28 # Updated name
        DECIMAL_ROUNDING_MODE = "ROUND_HALF_UP"
        TRADES_FILE_PATH = "trades.csv"
        CASH_TRANSACTIONS_FILE_PATH = "cash.csv"
        POSITIONS_START_FILE_PATH = "pos_start.csv"
        POSITIONS_END_FILE_PATH = "pos_end.csv"
        CORPORATE_ACTIONS_FILE_PATH = "corp_actions.csv"
        CLASSIFICATION_CACHE_FILE_PATH = "user_classifications.json" # Updated name
        ECB_RATES_CACHE_FILE_PATH = "ecb_rates.json" # Updated name
        TAX_YEAR = 2023
        IS_INTERACTIVE_CLASSIFICATION = False # Updated name
        MAX_FALLBACK_DAYS_EXCHANGE_RATES = 7
        CURRENCY_CODE_MAPPING_ECB = {"CNH": "CNY"}
        OUTPUT_PRECISION_AMOUNTS = Decimal("0.01") # Added for test_result_defs.py
        APPLY_CONCEPTUAL_DERIVATIVE_LOSS_CAPPING = True

    app_config = MockAppConfig()
    print("Warning: Using MockAppConfig in tests/conftest.py. Ensure src is in PYTHONPATH.")


@pytest.fixture(scope="session", autouse=True)
def set_decimal_precision_session_wide():
    """
    Set global decimal precision and rounding for all tests in the session.
    This mirrors the setup in your main_application or setup_decimal_context.
    """
    prec = app_config.INTERNAL_CALCULATION_PRECISION # Updated name
    rounding_mode_str = app_config.DECIMAL_ROUNDING_MODE
    
    getcontext().prec = prec
    
    valid_rounding_modes = ["ROUND_CEILING", "ROUND_DOWN", "ROUND_FLOOR", "ROUND_HALF_DOWN",
                            "ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_UP", "ROUND_05UP"]
    if rounding_mode_str in valid_rounding_modes:
        getcontext().rounding = rounding_mode_str # type: ignore
    else:
        print(f"Warning: Invalid DECIMAL_ROUNDING_MODE '{rounding_mode_str}'. Using ROUND_HALF_UP for tests.")
        getcontext().rounding = ROUND_HALF_UP # type: ignore
    # print(f"\nDecimal context set for test session: Precision={getcontext().prec}, Rounding={getcontext().rounding}")


@pytest.fixture(autouse=True)
def no_real_cnb_cache(monkeypatch):
    """Keep every test off the developer's real ČNB rate cache.

    ``RunService`` builds its default CZK converter on
    ``cache/cnb_exchange_rates.json`` — real rates, owned by the web app. A
    test that left the default in place read that file, fetched from ČNB
    whatever it lacked, and (now that entries written by an older version
    are re-checked against ČNB on first use) could rewrite it. The default
    is replaced by the pinned golden rates; a test that injects its own
    ``converter_factory`` is unaffected.
    """
    from pathlib import Path

    from src.countries.cz.config import CzTaxConfig
    from src.utils.cnb_exchange_rate_provider import CNBExchangeRateProvider

    # A hard stop first, and whatever else can or cannot be imported: any
    # provider built on that file raises — an aggregation run without an
    # injected provider builds one there too. Loud, because a silent read is
    # how the real cache came to hold the synthetic dataset's dates.
    real_cache = Path(CzTaxConfig().cnb_cache_file_path).resolve()
    original_init = CNBExchangeRateProvider.__init__

    def guarded_init(self, *args, **kwargs):
        path = kwargs.get("cache_file_path",
                          args[0] if args else "cache/cnb_exchange_rates.json")
        if Path(path).resolve() == real_cache:
            raise AssertionError(
                "This test built a ČNB provider on the real rate cache "
                f"({real_cache}). Pass a provider (fx_provider= / "
                "cz_fx_provider=) or a converter_factory, or give the "
                "provider a cache_file_path under tmp_path."
            )
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(CNBExchangeRateProvider, "__init__", guarded_init)

    # The web service's default converter would trip that guard on every
    # run; it gets the pinned golden rates instead.
    try:
        from src.webapp.services import RunService
    except ImportError:
        return
    from tests.support.golden_fx import golden_cz_converter

    monkeypatch.setattr(RunService, "_cz_converter",
                        lambda self: golden_cz_converter())


@pytest.fixture
def temp_data_dir():
    """
    Creates a temporary directory for test input/output files.
    Yields the path to this directory.
    Cleans up the directory after the test.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        cache_subdir = os.path.join(tmpdir, "cache")
        os.makedirs(cache_subdir, exist_ok=True)
        # data_subdir = os.path.join(tmpdir, "data") # Not strictly needed if files go to tmpdir root
        # os.makedirs(data_subdir, exist_ok=True)
        yield tmpdir

@pytest.fixture
def mock_config_paths(temp_data_dir, monkeypatch):
    """
    Mocks file paths in app_config to use the temp_data_dir.
    This is useful if the application directly uses config.TRADES_FILE_PATH etc.
    Returns a dictionary of these temporary paths for explicit use in tests.
    """
    data_path = lambda filename: os.path.join(temp_data_dir, filename)
    cache_path = lambda filename: os.path.join(temp_data_dir, "cache", filename)

    # Define paths for explicit use first
    paths_dict = {
        "trades": data_path("trades.csv"),
        "cash": data_path("cash_transactions.csv"),
        "pos_start": data_path("positions_start_of_year.csv"),
        "pos_end": data_path("positions_end_of_year.csv"),
        "corp_actions": data_path("corporate_actions.csv"),
        "classification_cache": cache_path("user_classifications.json"),
        "ecb_cache": cache_path("ecb_exchange_rates.json"),
        "temp_dir_root": temp_data_dir
    }

    # Attempt to monkeypatch the actual src.config module if it's loaded
    try:
        # This assumes 'src.config' is the canonical path to the config module
        # as it would be imported by other application modules.
        target_config_module = "src.config" 
        
        # Check if module is loaded and patchable, common for when tests import app code that imports config
        import sys
        if target_config_module in sys.modules:
            config_module_obj = sys.modules[target_config_module]
            monkeypatch.setattr(config_module_obj, "TRADES_FILE_PATH", paths_dict["trades"])
            monkeypatch.setattr(config_module_obj, "CASH_TRANSACTIONS_FILE_PATH", paths_dict["cash"])
            monkeypatch.setattr(config_module_obj, "POSITIONS_START_FILE_PATH", paths_dict["pos_start"])
            monkeypatch.setattr(config_module_obj, "POSITIONS_END_FILE_PATH", paths_dict["pos_end"])
            monkeypatch.setattr(config_module_obj, "CORPORATE_ACTIONS_FILE_PATH", paths_dict["corp_actions"])
            monkeypatch.setattr(config_module_obj, "CLASSIFICATION_CACHE_FILE_PATH", paths_dict["classification_cache"]) # Updated name
            monkeypatch.setattr(config_module_obj, "ECB_RATES_CACHE_FILE_PATH", paths_dict["ecb_cache"]) # Updated name
            monkeypatch.setattr(config_module_obj, "IS_INTERACTIVE_CLASSIFICATION", False) # Updated name, ensure non-interactive
        else:
            # This might occur if tests are structured such that src.config isn't loaded when conftest runs,
            # or if the way config is imported varies. Passing paths explicitly to pipeline_runner is robust.
            print(f"Warning: {target_config_module} not in sys.modules during conftest. Direct config patching might be incomplete.")
            print("Tests should rely on explicit file paths passed to the processing pipeline.")

    except Exception as e: # Catch broad exceptions during patching
        print(f"Notice: Skipping monkeypatch of config paths due to an issue: {e}. Ensure config is structured as expected or pass paths explicitly.")

    return paths_dict


@pytest.fixture
def default_tax_year():
    """Returns the default tax year for tests."""
    return 2023
