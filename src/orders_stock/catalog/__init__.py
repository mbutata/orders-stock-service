"""Catalogue: products and their current prices, the reference data that prices orders."""

from orders_stock.catalog.repository import Product, get_prices, insert_missing_products

__all__ = ["Product", "get_prices", "insert_missing_products"]
