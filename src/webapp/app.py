# src/webapp/app.py
"""FastAPI application factory for the local web GUI."""
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.webapp.routes import router
from src.webapp.serializers import format_czk
from src.webapp.services import RunService

_HERE = Path(__file__).resolve().parent


def create_app(services: Optional[RunService] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.services.runner.shutdown(wait=False)

    app = FastAPI(title="IBKR Tax Engine", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.services = services or RunService()

    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.filters["czk"] = format_czk
    templates.env.globals["today_year"] = lambda: date.today().year
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    app.include_router(router)

    return app
