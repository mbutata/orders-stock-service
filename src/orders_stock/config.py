"""Runtime configuration, read once from the environment into a frozen dataclass."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "postgresql://orders_stock:orders_stock@localhost:5432/orders_stock"
DEFAULT_API_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    database_pool_timeout: float = 5.0
    api_url: str = DEFAULT_API_URL
    stock_worker_batch_size: int = 100
    stock_worker_poll_interval: float = 0.5
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        """Build settings from environment variables, falling back to the documented defaults."""
        defaults = cls()
        return cls(
            database_url=environ.get("ORDERS_STOCK_DATABASE_URL", defaults.database_url),
            database_pool_timeout=float(
                environ.get("ORDERS_STOCK_DATABASE_POOL_TIMEOUT", defaults.database_pool_timeout)
            ),
            api_url=environ.get("ORDERS_STOCK_API_URL", defaults.api_url),
            stock_worker_batch_size=int(
                environ.get("ORDERS_STOCK_WORKER_BATCH_SIZE", defaults.stock_worker_batch_size)
            ),
            stock_worker_poll_interval=float(
                environ.get(
                    "ORDERS_STOCK_WORKER_POLL_INTERVAL", defaults.stock_worker_poll_interval
                )
            ),
            log_level=environ.get("ORDERS_STOCK_LOG_LEVEL", defaults.log_level).upper(),
        )
