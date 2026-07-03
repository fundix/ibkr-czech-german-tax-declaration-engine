# src/mcp_server/__main__.py
"""Entry point: ``uv run --extra mcp python -m src.mcp_server`` (stdio)."""
import logging
import sys

from src.utils.decimal_context import setup_decimal_context
from src.mcp_server.server import create_server

# stdio transport uses stdout for the protocol — log to stderr only.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def main() -> None:
    setup_decimal_context()  # main thread; the job worker sets its own
    create_server().run()


if __name__ == "__main__":
    main()
