"""Request models, domain dataclasses and domain errors of the Orders component."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

ORDER_ACCEPTED = "order.accepted"
ORDER_ACCEPTED_VERSION = 1

Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=IDENTIFIER_PATTERN)]


def format_timestamp(moment: datetime) -> str:
    """RFC 3339 in UTC with a `Z` suffix and microsecond precision."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Request models ---------------------------------------------------------------------------
# Strict: unknown members are rejected and nothing is coerced, so "2" is not a quantity.


class OrderItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sku: Identifier
    qty: Annotated[int, Field(ge=1, le=10000)]


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    order_ref: Identifier
    customer_id: Identifier
    items: Annotated[list[OrderItemCreate], Field(min_length=1, max_length=100)]

    def repeated_sku_indexes(self) -> list[int]:
        """Indexes of items whose SKU already appeared earlier in the order."""
        seen: set[str] = set()
        repeated = []
        for index, item in enumerate(self.items):
            if item.sku in seen:
                repeated.append(index)
            seen.add(item.sku)
        return repeated

    def to_new_order(self) -> NewOrder:
        return NewOrder(
            order_ref=self.order_ref,
            customer_id=self.customer_id,
            items=tuple(NewOrderItem(sku=item.sku, qty=item.qty) for item in self.items),
        )


# Domain -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class NewOrderItem:
    sku: str
    qty: int


@dataclass(frozen=True)
class NewOrder:
    """A validated create request: at least one item and at most one item per SKU."""

    order_ref: str
    customer_id: str
    items: tuple[NewOrderItem, ...]

    def content(self) -> tuple[str, frozenset[tuple[str, int]]]:
        """What makes two submissions the same order: the customer and the set of (sku, qty)."""
        return self.customer_id, frozenset((item.sku, item.qty) for item in self.items)


@dataclass(frozen=True)
class OrderItem:
    sku: str
    qty: int
    unit_price_cents: int
    line_total_cents: int

    def to_json(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "qty": self.qty,
            "unit_price_cents": self.unit_price_cents,
            "line_total_cents": self.line_total_cents,
        }


OrderStatus = Literal["accepted", "stock_committed"]


@dataclass(frozen=True)
class Order:
    order_ref: str
    customer_id: str
    status: OrderStatus
    items: tuple[OrderItem, ...]  # ascending SKU order
    total_cents: int
    accepted_at: datetime
    stock_committed_at: datetime | None

    def content(self) -> tuple[str, frozenset[tuple[str, int]]]:
        return self.customer_id, frozenset((item.sku, item.qty) for item in self.items)

    def snapshot(self) -> dict[str, Any]:
        """The order at acceptance: the `order.accepted` payload (`OrderAcceptedData`)."""
        return {
            "order_ref": self.order_ref,
            "customer_id": self.customer_id,
            "items": [item.to_json() for item in self.items],
            "total_cents": self.total_cents,
            "accepted_at": format_timestamp(self.accepted_at),
        }

    def to_json(self) -> dict[str, Any]:
        """The `Order` representation of the HTTP contract."""
        return {
            "order_ref": self.order_ref,
            "customer_id": self.customer_id,
            "status": self.status,
            "items": [item.to_json() for item in self.items],
            "total_cents": self.total_cents,
            "accepted_at": format_timestamp(self.accepted_at),
            "stock_committed_at": (
                format_timestamp(self.stock_committed_at) if self.stock_committed_at else None
            ),
        }


@dataclass(frozen=True)
class OrderEvent:
    event_id: int
    event_type: str
    event_version: int
    order_ref: str
    occurred_at: datetime
    payload: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        """The `OrderEvent` envelope of the feed."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_version": self.event_version,
            "occurred_at": format_timestamp(self.occurred_at),
            "data": self.payload,
        }


@dataclass(frozen=True)
class AcceptResult:
    outcome: Literal["created", "duplicate"]
    order: Order


class OrderRefConflict(Exception):
    """The `order_ref` belongs to an order with a different customer or different items."""

    def __init__(self, order_ref: str) -> None:
        super().__init__(f"order_ref {order_ref!r} already used with different content")
        self.order_ref = order_ref


class UnknownSkus(Exception):
    """A new order references SKUs absent from the catalogue."""

    def __init__(self, unknown: list[tuple[int, str]]) -> None:
        super().__init__(f"unknown SKUs: {', '.join(sku for _, sku in unknown)}")
        self.unknown = unknown  # (item index, sku)
