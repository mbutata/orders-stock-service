"""AC-OPS-*: migrations, seeding and the burst command."""

import re
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import (
    O1,
    Command,
    LiveApi,
    count,
    drain,
    offset,
    on_hand,
    reset_schema,
    run_to_completion,
)

SPECS = Path(__file__).resolve().parents[1] / "specs"
TABLES = {"products", "orders", "order_items", "order_events", "stock_levels", "consumer_offsets"}

SEED_TABLE = [
    "sku      name              price_cents  on_hand",
    "APL-003  Apples 1kg                349       40",
    "BAN-001  Bananas 1kg               199       50",
    "BRD-004  Sourdough 800g            425       20",
    "MLK-002  Whole milk 1L             115       30",
]

BURST_TABLE = [
    "order_ref   submitted  201  200  409  total_cents",
    "web-100045          3    1    2    0          747",
    "web-100046          1    1    0    0          460",
    "web-100047          3    1    1    1          624",
    "web-100048          1    1    0    0          813",
    "web-100049          2    1    1    0          597",
    "web-100050          1    1    0    0          850",
]


def ddl_constraint_names() -> set[str]:
    """Every named constraint in the DDL of specs/02-domain-model.md."""
    model = (SPECS / "02-domain-model.md").read_text(encoding="utf-8")
    ddl = model.split("### DDL", 1)[1].split("```sql", 1)[1].split("```", 1)[0]
    return set(re.findall(r"CONSTRAINT (\w+)", ddl))


@pytest.mark.no_seed
def test_ac_ops_01_migrations(
    conn: psycopg.Connection, run_command: Callable[..., Command]
) -> None:
    reset_schema(conn)

    code, stdout, stderr = run_to_completion(run_command, "migrate")
    assert code == 0, stderr
    assert stdout == ["applied 1 migration(s)"]

    tables = {
        name
        for (name,) in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    assert tables >= TABLES
    constraints = {name for (name,) in conn.execute("SELECT conname FROM pg_constraint").fetchall()}
    expected = ddl_constraint_names()
    assert len(expected) == 14
    assert expected <= constraints
    triggers = {name for (name,) in conn.execute("SELECT tgname FROM pg_trigger").fetchall()}
    assert "order_events_append_only" in triggers

    code, stdout, stderr = run_to_completion(run_command, "migrate")
    assert code == 0, stderr
    assert stdout == ["applied 0 migration(s)"]


@pytest.mark.no_seed
def test_ac_ops_02_seeding(
    client: TestClient, conn: psycopg.Connection, run_command: Callable[..., Command]
) -> None:
    code, stdout, stderr = run_to_completion(run_command, "seed")
    assert code == 0, stderr
    assert stdout == [*SEED_TABLE, "seed: inserted 4 product(s)"]

    assert client.post("/orders", json=O1).status_code == 201
    drain(conn)

    code, stdout, stderr = run_to_completion(run_command, "seed")
    assert code == 0, stderr
    assert stdout[-1] == "seed: inserted 0 product(s)"
    assert on_hand(conn, "BAN-001") == 48
    assert count(conn, "orders") == 1
    assert count(conn, "order_events") == 1
    assert offset(conn) == 1


def test_ac_ops_03_the_burst_command(
    live_api: LiveApi, conn: psycopg.Connection, run_command: Callable[..., Command]
) -> None:
    env = {"API_URL": live_api.url}

    code, stdout, stderr = run_to_completion(run_command, "burst", env=env)
    assert code == 0, stderr
    assert stdout[:-1] == BURST_TABLE
    assert stdout[-1] == "summary: submitted=11 created=6 duplicate=4 conflict=1 failed=0"
    assert count(conn, "orders") == 6
    assert count(conn, "order_events") == 6

    code, stdout, stderr = run_to_completion(run_command, "burst", env=env)
    assert code == 0, stderr
    assert stdout[-1] == "summary: submitted=11 created=0 duplicate=10 conflict=1 failed=0"
    assert count(conn, "orders") == 6
    assert count(conn, "order_events") == 6
