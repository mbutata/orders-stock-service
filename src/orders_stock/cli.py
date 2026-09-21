"""The `orders-stock` command line: one entry point for every process and one-shot command.

Diagnostics go to stderr and command results to stdout. Exit status is 0 on success, 1 on a
runtime failure and 2 on a usage error.
"""

import argparse
import copy
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn
import uvicorn.config

from orders_stock.api import create_app
from orders_stock.config import Settings
from orders_stock.demo.burst import run_burst
from orders_stock.demo.feed_consumer import run_feed_consumer
from orders_stock.demo.seed import run_seed
from orders_stock.inventory import run_stock_worker
from orders_stock.migrate import apply_migrations

logger = logging.getLogger("orders_stock")

# Libraries that log every request or migration step at INFO; their failures still show.
QUIET_LOGGERS = ("httpx", "httpcore", "yoyo")


def api_log_config() -> dict[str, Any]:
    """Uvicorn's default logging, with the access log moved from stdout to stderr."""
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["handlers"]["access"]["stream"] = "ext://sys.stderr"
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orders-stock", description="Order intake, stock and the order-accepted feed."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("migrate", help="apply pending database migrations")

    api = commands.add_parser("api", help="serve the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)

    commands.add_parser("stock-worker", help="run the stock applier until SIGINT or SIGTERM")
    commands.add_parser("seed", help="insert the demo catalogue and initial stock levels")

    burst = commands.add_parser("burst", help="submit a burst of orders with duplicates")
    burst.add_argument("--prefix", default="web", help="order_ref prefix (default: web)")

    feed = commands.add_parser("consume-feed", help="print order.accepted events as they arrive")
    feed.add_argument("--cursor-file", type=Path, default=Path(".feed-cursor"))
    feed.add_argument("--from-start", action="store_true", help="ignore the cursor file")
    feed.add_argument("--poll-interval", type=float, default=1.0)
    return parser


def stop_on_signals() -> threading.Event:
    """An event that SIGINT and SIGTERM set, for loops that stop between units of work."""
    stop = threading.Event()

    def handle(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    return stop


def run(args: argparse.Namespace, settings: Settings) -> int:
    match args.command:
        case "migrate":
            print(f"applied {apply_migrations(settings.database_url)} migration(s)")
        case "api":
            uvicorn.run(
                create_app(settings),
                host=args.host,
                port=args.port,
                workers=1,
                access_log=True,
                log_config=api_log_config(),
                log_level=settings.log_level.lower(),
            )
        case "stock-worker":
            run_stock_worker(settings, stop_on_signals())
        case "seed":
            run_seed(settings)
        case "burst":
            return run_burst(settings, args.prefix)
        case "consume-feed":
            return run_feed_consumer(
                settings,
                args.cursor_file,
                from_start=args.from_start,
                poll_interval=args.poll_interval,
                stop=stop_on_signals(),
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger().level))
    try:
        return run(args, settings)
    except Exception:
        logger.exception("orders-stock %s failed", args.command)
        return 1


if __name__ == "__main__":
    sys.exit(main())
