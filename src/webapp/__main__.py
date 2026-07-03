# src/webapp/__main__.py
"""Entry point: ``uv run --extra web python -m src.webapp``"""
import argparse
import logging
import threading
import webbrowser

import uvicorn

from src.utils.decimal_context import setup_decimal_context
from src.webapp import settings
from src.webapp.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lokální web GUI daňového enginu")
    parser.add_argument("--host", default=settings.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=settings.DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Neotevírat prohlížeč po startu.")
    args = parser.parse_args()

    setup_decimal_context()  # main thread; the job worker sets its own
    app = create_app()

    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(f"http://{args.host}:{args.port}/",)).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
