"""The fixed demo catalogue and its initial stock levels."""

from orders_stock.catalog import Product

DEMO_PRODUCTS: tuple[Product, ...] = (
    Product(sku="APL-003", name="Apples 1kg", price_cents=349),
    Product(sku="BAN-001", name="Bananas 1kg", price_cents=199),
    Product(sku="BRD-004", name="Sourdough 800g", price_cents=425),
    Product(sku="MLK-002", name="Whole milk 1L", price_cents=115),
)

INITIAL_ON_HAND: dict[str, int] = {
    "APL-003": 40,
    "BAN-001": 50,
    "BRD-004": 20,
    "MLK-002": 30,
}
