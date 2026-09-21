"""The `seed` command: insert the demo catalogue and initial stock levels, never update them."""

import psycopg

from orders_stock import catalog, db, inventory
from orders_stock.config import Settings
from orders_stock.demo.seed_data import DEMO_PRODUCTS, INITIAL_ON_HAND

APPLICATION_NAME = "orders-stock-seed"
NAME_WIDTH = 16


def insert_demo_catalogue(conn: psycopg.Connection) -> int:
    """In one transaction, insert every missing demo product and its stock level."""
    with conn.transaction():
        inserted = catalog.insert_missing_products(conn, DEMO_PRODUCTS)
        inventory.insert_missing_stock_levels(conn, INITIAL_ON_HAND)
    return inserted


def catalogue_table(conn: psycopg.Connection) -> list[str]:
    """The demo catalogue with its current prices and stock, one line per product."""
    prices = catalog.get_prices(conn, [product.sku for product in DEMO_PRODUCTS])
    sku_width = max(len("sku"), *(len(product.sku) for product in DEMO_PRODUCTS))
    name_width = max(NAME_WIDTH, *(len(product.name) for product in DEMO_PRODUCTS))
    lines = [f"{'sku':<{sku_width}}  {'name':<{name_width}}  price_cents  on_hand"]
    for product in DEMO_PRODUCTS:
        level = inventory.get_stock(conn, product.sku)
        on_hand = "" if level is None else str(level.on_hand)
        lines.append(
            f"{product.sku:<{sku_width}}  {product.name:<{name_width}}  "
            f"{prices.get(product.sku, ''):>11}  {on_hand:>7}"
        )
    return lines


def run_seed(settings: Settings) -> None:
    with db.connect(settings.database_url, APPLICATION_NAME) as conn:
        inserted = insert_demo_catalogue(conn)
        lines = catalogue_table(conn)
    for line in lines:
        print(line)
    print(f"seed: inserted {inserted} product(s)")
