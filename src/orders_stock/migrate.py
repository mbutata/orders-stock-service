"""Versioned schema migrations: the plain SQL files in migrations/, applied with yoyo-migrations."""

from pathlib import Path

from yoyo import get_backend, read_migrations

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def yoyo_url(database_url: str) -> str:
    """yoyo selects its database driver from the URI scheme; use psycopg 3."""
    for scheme in ("postgresql://", "postgres://"):
        if database_url.startswith(scheme):
            return "postgresql+psycopg://" + database_url.removeprefix(scheme)
    return database_url


def apply_migrations(database_url: str) -> int:
    """Apply pending migrations under yoyo's lock and return how many were applied."""
    backend = get_backend(yoyo_url(database_url))
    try:
        migrations = read_migrations(str(MIGRATIONS_DIR))
        with backend.lock():
            pending = backend.to_apply(migrations)
            backend.apply_migrations(pending)
        return len(pending)
    finally:
        backend.connection.close()
