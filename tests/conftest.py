"""Fixtures for the acceptance suite: a real PostgreSQL, the app, a live server and subprocesses.

The conventions are those of specs/06-acceptance.md, "Test conventions".
"""

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orders_stock import db, inventory
from orders_stock.api import create_app
from orders_stock.config import Settings
from orders_stock.demo.seed import insert_demo_catalogue
from orders_stock.migrate import apply_migrations

from contract import ContractChecker

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://orders_stock:orders_stock@localhost:5432/orders_stock_test",
)
COMMAND = str(Path(sys.executable).parent / "orders-stock")
AC_API_01_PREFIX = "test_ac_api_01"

DESELECTED = pytest.StashKey[bool]()
CHECKER = ContractChecker()

O1: dict[str, Any] = {
    "order_ref": "web-100045",
    "customer_id": "cust-42",
    "items": [{"sku": "BAN-001", "qty": 2}, {"sku": "APL-003", "qty": 1}],
}


# Collection -------------------------------------------------------------------------------


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Run AC-API-01 last, after every test that produces responses."""
    items.sort(key=lambda item: item.name.startswith(AC_API_01_PREFIX))


def pytest_deselected(items: list[pytest.Item]) -> None:
    if items:
        items[0].config.stash[DESELECTED] = True


def selects_whole_suite(config: pytest.Config) -> bool:
    """True unless a path, -k, -m or any deselection narrowed the run."""
    if config.option.keyword or config.option.markexpr or config.stash.get(DESELECTED, False):
        return False
    if config.args_source != pytest.Config.ArgsSource.ARGS:
        return True
    tests_dir = Path(__file__).resolve().parent
    return all(Path(arg).resolve() in {tests_dir, tests_dir.parent} for arg in config.args)


# Database ---------------------------------------------------------------------------------


def connect(database_url: str = TEST_DATABASE_URL) -> psycopg.Connection:
    """A configured autocommit connection: each statement commits, `transaction()` blocks too."""
    conn = db.connect(database_url, "orders-stock-test")
    conn.autocommit = True
    return conn


def reset_schema(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")


def reset_data(conn: psycopg.Connection, *, seed: bool) -> None:
    conn.execute(
        "TRUNCATE order_events, order_items, orders, stock_levels, products RESTART IDENTITY"
    )
    conn.execute("UPDATE consumer_offsets SET last_event_id = 0 WHERE consumer = 'stock-applier'")
    if seed:
        insert_demo_catalogue(conn)


@pytest.fixture(scope="session", autouse=True)
def database() -> str:
    """Once per session: refuse a non-test or unreachable database, recreate the schema, migrate."""
    params = conninfo_to_dict(TEST_DATABASE_URL)
    name = params.get("dbname", "")
    if not str(name).endswith("_test"):
        pytest.exit(f"TEST_DATABASE_URL must name a database ending in _test, not {name!r}")
    try:
        conn = connect()
    except psycopg.OperationalError as error:
        target = " ".join(
            f"{key}={params[key]}" for key in ("user", "host", "port", "dbname") if key in params
        )
        cause = next(iter(str(error).splitlines()), type(error).__name__)
        pytest.exit(
            f"The test database {target!r} is not reachable ({cause}). "
            "Start PostgreSQL or set TEST_DATABASE_URL; see the PostgreSQL section of README.md."
        )
    with conn:
        reset_schema(conn)
    apply_migrations(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest, database: str) -> None:
    with connect() as conn:
        reset_data(conn, seed=request.node.get_closest_marker("no_seed") is None)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with connect() as connection:
        yield connection


def scalar(conn: psycopg.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def count(conn: psycopg.Connection, table: str) -> int:
    return int(scalar(conn, f"SELECT count(*) FROM {table}"))


def on_hand(conn: psycopg.Connection, sku: str) -> int:
    return int(scalar(conn, "SELECT on_hand FROM stock_levels WHERE sku = %s", (sku,)))


def order_status(conn: psycopg.Connection, order_ref: str) -> str:
    return str(scalar(conn, "SELECT status FROM orders WHERE order_ref = %s", (order_ref,)))


def offset(conn: psycopg.Connection) -> int:
    return int(
        scalar(conn, "SELECT last_event_id FROM consumer_offsets WHERE consumer = 'stock-applier'")
    )


def drain(conn: psycopg.Connection, batch_size: int = 100) -> list[inventory.AppliedBatch]:
    """Call the stock applier until it has nothing left to apply."""
    batches = []
    while (batch := inventory.apply_next_batch(conn, batch_size)) is not None:
        batches.append(batch)
    return batches


def wait_until(predicate: Callable[[], bool], timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {message}")
        time.sleep(0.05)


# The application --------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=TEST_DATABASE_URL, stock_worker_poll_interval=0.1)


@pytest.fixture(scope="session")
def contract() -> ContractChecker:
    return CHECKER


def check_response(response: httpx.Response) -> None:
    """httpx response hook: fail the current test on any contract violation."""
    response.read()
    problems = CHECKER.check(
        response.request.method,
        response.request.url.path,
        response.status_code,
        {name.lower(): value for name, value in response.headers.items()},
        response.content,
    )
    if problems:
        raise AssertionError("response violates specs/openapi.yaml:\n" + "\n".join(problems))


def contract_client(client: httpx.Client) -> httpx.Client:
    client.event_hooks = {"request": [], "response": [check_response]}
    return client


@pytest.fixture
def make_client() -> Iterator[Callable[[Settings], TestClient]]:
    """TestClients over create_app(settings), each checked against the contract."""
    stack: list[TestClient] = []

    def make(app_settings: Settings) -> TestClient:
        client = TestClient(create_app(app_settings), raise_server_exceptions=False)
        client.__enter__()
        stack.append(client)
        contract_client(client)
        return client

    yield make
    for client in reversed(stack):
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[[Settings], TestClient], settings: Settings) -> TestClient:
    return make_client(settings)


class ContractRecordingApp:
    """ASGI wrapper that checks every response the live server sends, whoever the client is."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.problems: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 0
        headers: dict[str, str] = {}
        body = bytearray()

        async def recording_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers.update(
                    (name.decode().lower(), value.decode()) for name, value in message["headers"]
                )
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    self.problems += CHECKER.check(
                        scope["method"], scope["path"], status, headers, bytes(body)
                    )
            await send(message)

        await self.app(scope, receive, recording_send)


@dataclass
class LiveApi:
    url: str
    clients: list[httpx.Client] = field(default_factory=list)

    def client(self) -> httpx.Client:
        client = contract_client(httpx.Client(base_url=self.url, timeout=10))
        self.clients.append(client)
        return client


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def live_api(settings: Settings) -> Iterator[LiveApi]:
    """Uvicorn serving create_app() in a background thread on 127.0.0.1 and a free port."""
    app = ContractRecordingApp(create_app(settings))
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, name="live-api", daemon=True)
    thread.start()
    wait_until(lambda: server.started, 10, "the live API to start")
    live = LiveApi(url=f"http://127.0.0.1:{port}")
    yield live
    for http_client in live.clients:
        http_client.close()
    server.should_exit = True
    thread.join(10)
    assert not app.problems, "responses violate specs/openapi.yaml:\n" + "\n".join(app.problems)


# Subprocesses -----------------------------------------------------------------------------


class Command:
    """An `orders-stock` subprocess whose stdout and stderr lines are collected as they arrive."""

    def __init__(self, args: list[str], env: Mapping[str, str], cwd: Path | None = None) -> None:
        self.process = subprocess.Popen(
            [COMMAND, *args],
            env={**os.environ, **env},
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self._readers = [
            threading.Thread(target=self._read, args=(stream, lines), daemon=True)
            for stream, lines in (
                (self.process.stdout, self.stdout),
                (self.process.stderr, self.stderr),
            )
        ]
        for reader in self._readers:
            reader.start()

    def _read(self, stream: Any, lines: list[str]) -> None:
        with stream:  # closed at end of output, once the process has exited
            for line in stream:
                lines.append(line.rstrip("\n"))

    def wait_for_stdout_lines(self, count: int, timeout: float = 15) -> None:
        wait_until(
            lambda: len(self.stdout) >= count or self.process.poll() is not None,
            timeout,
            f"{count} stdout lines (have {self.stdout}, stderr {self.stderr})",
        )
        assert len(self.stdout) >= count, f"exited early: {self.stderr}"

    def send_signal(self, signum: int) -> None:
        self.process.send_signal(signum)

    def wait(self, timeout: float = 15) -> int:
        code = self.process.wait(timeout)
        for reader in self._readers:
            reader.join(5)
        return code

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.wait()


@pytest.fixture
def command_env(settings: Settings) -> dict[str, str]:
    return {
        "DATABASE_URL": TEST_DATABASE_URL,
        "STOCK_WORKER_POLL_INTERVAL": str(settings.stock_worker_poll_interval),
        "LOG_LEVEL": "INFO",
    }


@pytest.fixture
def run_command(command_env: dict[str, str]) -> Iterator[Callable[..., Command]]:
    """Start `orders-stock` subprocesses; any still running at teardown are killed."""
    started: list[Command] = []

    def start(*args: str, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Command:
        command = Command(list(args), {**command_env, **(env or {})}, cwd)
        started.append(command)
        return command

    yield start
    for command in started:
        command.kill()


def run_to_completion(
    start: Callable[..., Command], *args: str, env: Mapping[str, str] | None = None
) -> tuple[int, list[str], list[str]]:
    command = start(*args, env=env)
    code = command.wait(60)
    return code, command.stdout, command.stderr
