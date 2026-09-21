"""The HTTP application: the Orders and Inventory routes behind the committed OpenAPI contract."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI

from orders_stock import db, inventory, orders
from orders_stock.config import Settings
from orders_stock.problems import install_problem_handlers

APPLICATION_NAME = "orders-stock-api"
POOL_MAX_SIZE = 10
OPENAPI_PATH = Path(__file__).resolve().parents[2] / "specs" / "openapi.yaml"


@cache
def openapi_document() -> dict[str, Any]:
    """specs/openapi.yaml, the one contract; the framework's generated schema is not used."""
    with OPENAPI_PATH.open(encoding="utf-8") as file:
        document: dict[str, Any] = yaml.safe_load(file)
    return document


def create_app(settings: Settings) -> FastAPI:
    document = openapi_document()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # wait=False: the API starts, and answers 503, even while PostgreSQL is down.
        app.state.pool = db.create_pool(
            settings.database_url,
            APPLICATION_NAME,
            max_size=POOL_MAX_SIZE,
            timeout=settings.database_pool_timeout,
        )
        try:
            yield
        finally:
            app.state.pool.close()

    app = FastAPI(
        title=document["info"]["title"],
        version=document["info"]["version"],
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.openapi = lambda: document  # type: ignore[method-assign]
    install_problem_handlers(app)
    app.include_router(orders.router)
    app.include_router(inventory.router)
    return app
