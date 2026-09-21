"""HTTP route of the Inventory component: the stock read."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from orders_stock.db import request_pool
from orders_stock.inventory import repository
from orders_stock.problems import SKU_NOT_FOUND, ProblemError

router = APIRouter()


@router.get("/stock/{sku}")
def get_stock_level(
    sku: str, pool: Annotated[ConnectionPool, Depends(request_pool)]
) -> JSONResponse:
    with pool.connection() as conn:
        level = repository.get_stock(conn, sku)
    if level is None:
        raise ProblemError(SKU_NOT_FOUND, f"No stock level for SKU {sku!r}.")
    return JSONResponse(level.to_json())
