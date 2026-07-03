# src/utils/decimal_context.py
"""
Global decimal context setup, shared by every process/thread entry point.

``decimal.getcontext()`` is THREAD-LOCAL in CPython: configuring it in the
main thread does not affect worker threads. Any code path that runs engine
calculations on a non-main thread (e.g. a web server's job executor) must
call :func:`setup_decimal_context` in that thread — typically via
``ThreadPoolExecutor(initializer=setup_decimal_context)``.
"""
import logging
from decimal import getcontext

import src.config as config

logger = logging.getLogger(__name__)

_VALID_ROUNDING_MODES = [
    "ROUND_CEILING", "ROUND_DOWN", "ROUND_FLOOR", "ROUND_HALF_DOWN",
    "ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_UP", "ROUND_05UP",
]


def setup_decimal_context():
    """Sets the decimal precision and rounding mode for the CURRENT thread."""
    getcontext().prec = config.INTERNAL_CALCULATION_PRECISION
    rounding_mode_to_set = config.DECIMAL_ROUNDING_MODE
    if rounding_mode_to_set not in _VALID_ROUNDING_MODES:
        logger.warning(f"Invalid DECIMAL_ROUNDING_MODE '{rounding_mode_to_set}' in config. Using ROUND_HALF_UP as fallback.")
        rounding_mode_to_set = "ROUND_HALF_UP"

    getcontext().rounding = rounding_mode_to_set
    logger.info(f"Global decimal precision set to {getcontext().prec}, rounding mode to {getcontext().rounding}.")
