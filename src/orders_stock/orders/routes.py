"""HTTP routes of the Orders component: order intake, order reads and the event feed."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

from orders_stock.db import request_pool
from orders_stock.orders import events, service
from orders_stock.orders.models import OrderCreate, OrderRefConflict, UnknownSkus
from orders_stock.problems import (
    ORDER_NOT_FOUND,
    ORDER_REF_CONFLICT,
    SCHEMA_MISMATCH_DETAIL,
    UNKNOWN_SKU,
    VALIDATION_ERROR,
    ErrorLocation,
    ProblemError,
    json_pointer,
)

router = APIRouter()

Pool = Annotated[ConnectionPool, Depends(request_pool)]

MAX_EVENT_ID = 2**63 - 1


@router.post("/orders")
def create_order(body: OrderCreate, pool: Pool) -> JSONResponse:
    repeated = body.repeated_sku_indexes()
    if repeated:
        raise ProblemError(
            VALIDATION_ERROR,
            SCHEMA_MISMATCH_DETAIL,
            errors=[
                ErrorLocation(
                    f"SKU {body.items[index].sku!r} appears more than once in the order.",
                    pointer=json_pointer(["items", index, "sku"]),
                )
                for index in repeated
            ],
        )
    try:
        with pool.connection() as conn:
            result = service.accept_order(conn, body.to_new_order())
    except OrderRefConflict as conflict:
        raise ProblemError(
            ORDER_REF_CONFLICT,
            f"Order {conflict.order_ref!r} already exists with different items or customer_id.",
        ) from conflict
    except UnknownSkus as unknown:
        count = len(unknown.unknown)
        raise ProblemError(
            UNKNOWN_SKU,
            f"{count} item{' references' if count == 1 else 's reference'} "
            "a SKU that is not in the catalogue.",
            errors=[
                ErrorLocation(
                    f"Unknown SKU {sku!r}.", pointer=json_pointer(["items", index, "sku"])
                )
                for index, sku in unknown.unknown
            ],
        ) from unknown

    order = result.order
    if result.outcome == "created":
        return JSONResponse(
            order.to_json(), status_code=201, headers={"Location": f"/orders/{order.order_ref}"}
        )
    return JSONResponse(order.to_json(), status_code=200)


@router.get("/orders/{order_ref}")
def get_order(order_ref: str, pool: Pool) -> JSONResponse:
    with pool.connection() as conn:
        order = service.get_order(conn, order_ref)
    if order is None:
        raise ProblemError(ORDER_NOT_FOUND, f"No order with order_ref {order_ref!r}.")
    return JSONResponse(order.to_json())


@router.get("/order-events")
def list_order_events(
    pool: Pool,
    after: Annotated[int, Query(ge=0, le=MAX_EVENT_ID)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JSONResponse:
    # One statement reads limit + 1 rows: the extra row only decides has_more.
    with pool.connection() as conn:
        page = events.read_events(conn, after, limit + 1)
    has_more = len(page) > limit
    page = page[:limit]
    return JSONResponse(
        {
            "events": [event.to_json() for event in page],
            "next_after": page[-1].event_id if page else after,
            "has_more": has_more,
        }
    )
