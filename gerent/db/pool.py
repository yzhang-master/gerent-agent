"""Postgres connection pool and forward-only migrations.

Optional at runtime: without a DSN the agent runs with in-memory state, which is fine
for a single interactive turn and explicitly not fine for durable plans - the caller is
warned rather than silently downgraded. See docs/data-model.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from gerent.core.errors import ConfigError

log = structlog.get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 10) -> None:
        self.dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: Any = None

    async def connect(self) -> Database:
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover
            raise ConfigError("asyncpg not installed - pip install 'gerent[db]'") from exc
        self._pool = await asyncpg.create_pool(
            self.dsn, min_size=self._min, max_size=self._max
        )
        return self

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> Any:
        if self._pool is None:
            raise ConfigError("database not connected")
        return self._pool

    async def migrate(self) -> list[str]:
        """Apply pending migrations in filename order.

        0002 (pgvector) is allowed to fail: the extension may not be installed, and
        retrieval degrades to full-text search behind the same interface rather than
        the agent refusing to start.
        """
        applied: list[str] = []
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                try:
                    async with conn.transaction():
                        await conn.execute(path.read_text())
                        await conn.execute(
                            "INSERT INTO schema_migrations (name) VALUES ($1)", path.name
                        )
                    applied.append(path.name)
                except Exception as exc:
                    if "pgvector" in path.name or "vector" in str(exc).lower():
                        log.warning(
                            "db.optional_migration_skipped", migration=path.name, error=str(exc)
                        )
                        continue
                    raise
        return applied

    async def has_pgvector(self) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'memories' AND column_name = 'embedding'"
            )
        return row is not None
