"""Connection factories and the per-connection configuration every process uses.

Every connection has autocommit off, READ COMMITTED set explicitly, TimeZone=UTC and a
5-second statement timeout, so a pathological lock wait surfaces as an error instead of a hang
(specs/05-reliability.md, "Transaction model").
"""

from collections.abc import Iterator
from typing import Any

import psycopg
from fastapi import Request
from psycopg import IsolationLevel
from psycopg_pool import ConnectionPool

STATEMENT_TIMEOUT = "5s"


def connection_kwargs(application_name: str) -> dict[str, Any]:
    """libpq parameters applied on top of the connection URI."""
    return {
        "autocommit": False,
        "application_name": application_name,
        "options": f"-c TimeZone=UTC -c statement_timeout={STATEMENT_TIMEOUT}",
    }


def configure_connection(conn: psycopg.Connection) -> None:
    """Set the isolation level explicitly rather than inheriting the server default."""
    conn.isolation_level = IsolationLevel.READ_COMMITTED


def connect(database_url: str, application_name: str) -> psycopg.Connection:
    """Open one configured connection, for one-shot commands and tests."""
    conn = psycopg.connect(database_url, **connection_kwargs(application_name))
    configure_connection(conn)
    return conn


def create_pool(
    database_url: str, application_name: str, *, max_size: int, timeout: float
) -> ConnectionPool:
    """Open a pool without waiting for PostgreSQL, so the process starts even if it is down."""
    pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=max_size,
        timeout=timeout,
        kwargs=connection_kwargs(application_name),
        configure=configure_connection,
        check=ConnectionPool.check_connection,
        name=application_name,
        open=False,
    )
    pool.open(wait=False)
    return pool


def request_connection(request: Request) -> Iterator[psycopg.Connection]:
    """FastAPI dependency: a pooled connection for the duration of one request."""
    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        yield conn
