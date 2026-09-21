"""SQL for products. The Catalogue is the only component that queries `products`."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int


def get_prices(conn: psycopg.Connection, skus: Collection[str]) -> dict[str, int]:
    """Current `price_cents` for each known SKU; unknown SKUs are absent from the result."""
    rows = conn.execute(
        "SELECT sku, price_cents FROM products WHERE sku = ANY(%s)", (list(skus),)
    ).fetchall()
    return dict(rows)


def insert_missing_products(conn: psycopg.Connection, products: Iterable[Product]) -> int:
    """Insert the products whose SKU is not present yet; return how many were inserted."""
    products = list(products)
    cursor = conn.execute(
        """
        INSERT INTO products (sku, name, price_cents)
        SELECT * FROM unnest(%s::text[], %s::text[], %s::bigint[])
        ON CONFLICT (sku) DO NOTHING
        """,
        (
            [product.sku for product in products],
            [product.name for product in products],
            [product.price_cents for product in products],
        ),
    )
    return cursor.rowcount
