"""Semantic memory: durable facts carried across sessions.

Retrieval is the ceiling on the whole system. pgvector is NOT assumed - where it is
unavailable this degrades to Postgres full-text search behind the same interface, which
has worse recall and is a perfectly adequate v1. See docs/memory.md.

Facts are superseded rather than deleted, so what the agent believed stays inspectable
when it behaves oddly.
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gerent.core.types import new_id

_WORD = re.compile(r"[a-z0-9]+")
_PREFIX_MIN = 4


def _matches(term: str, words: set[str]) -> bool:
    """Exact match, or a shared prefix of at least four characters.

    Deliberately not a stemmer: this agent is meant to work in any language, and an
    English stemmer would quietly make recall worse everywhere else. A shared prefix
    catches deploy/deploys and test/testing without assuming a language, and misses
    genuinely irregular forms - the documented failure mode in docs/memory.md.
    """
    if term in words:
        return True
    if len(term) < _PREFIX_MIN:
        return False
    return any(
        len(w) >= _PREFIX_MIN and (w.startswith(term[:_PREFIX_MIN]) and (
            w.startswith(term) or term.startswith(w)
        ))
        for w in words
    )


@dataclass
class Memory:
    content: str
    actor_id: uuid.UUID
    id: uuid.UUID = field(default_factory=new_id)
    superseded_by: uuid.UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryStore(ABC):
    @abstractmethod
    async def remember(self, content: str, actor_id: uuid.UUID) -> Memory: ...

    @abstractmethod
    async def recall(self, query: str, actor_id: uuid.UUID, limit: int = 8) -> list[Memory]: ...

    @abstractmethod
    async def supersede(self, memory_id: uuid.UUID, by: uuid.UUID | None = None) -> bool: ...


class InMemoryMemoryStore(MemoryStore):
    """Non-durable. Used before a database is configured, and by tests."""

    durable = False

    def __init__(self) -> None:
        self._items: list[Memory] = []

    async def remember(self, content: str, actor_id: uuid.UUID) -> Memory:
        memory = Memory(content=content, actor_id=actor_id)
        self._items.append(memory)
        return memory

    async def recall(self, query: str, actor_id: uuid.UUID, limit: int = 8) -> list[Memory]:
        terms = set(_WORD.findall(query.lower()))
        scored = []
        for memory in self._items:
            # actor scoping is applied on every retrieval from day one; a cross-tenant
            # leak is not a bug to discover after adding tenancy.
            if memory.actor_id != actor_id or memory.superseded_by is not None:
                continue
            words = set(_WORD.findall(memory.content.lower()))
            overlap = sum(1 for t in terms if _matches(t, words))
            if overlap:
                scored.append((overlap, memory))
        scored.sort(key=lambda t: (t[0], t[1].created_at), reverse=True)
        return [m for _, m in scored[:limit]]

    async def supersede(self, memory_id: uuid.UUID, by: uuid.UUID | None = None) -> bool:
        for memory in self._items:
            if memory.id == memory_id:
                memory.superseded_by = by or memory_id
                return True
        return False


class PostgresMemoryStore(MemoryStore):
    durable = True

    def __init__(self, db: Any, *, use_vectors: bool = False) -> None:
        self._db = db
        self._use_vectors = use_vectors

    async def remember(self, content: str, actor_id: uuid.UUID) -> Memory:
        memory = Memory(content=content, actor_id=actor_id)
        async with self._db.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO actors (id, name) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                actor_id, "actor",
            )
            await conn.execute(
                "INSERT INTO memories (id, actor_id, content, created_at) "
                "VALUES ($1, $2, $3, $4)",
                memory.id, actor_id, content, memory.created_at,
            )
        return memory

    async def recall(self, query: str, actor_id: uuid.UUID, limit: int = 8) -> list[Memory]:
        # Full-text path. The vector path plugs in here when pgvector is present and an
        # embedding provider is configured; the interface does not change.
        async with self._db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, actor_id, content, superseded_by, created_at
                FROM memories
                WHERE actor_id = $1
                  AND superseded_by IS NULL
                  AND tsv @@ plainto_tsquery('simple', $2)
                ORDER BY ts_rank(tsv, plainto_tsquery('simple', $2)) DESC, created_at DESC
                LIMIT $3
                """,
                actor_id, query, limit,
            )
        return [
            Memory(
                id=r["id"],
                actor_id=r["actor_id"],
                content=r["content"],
                superseded_by=r["superseded_by"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def supersede(self, memory_id: uuid.UUID, by: uuid.UUID | None = None) -> bool:
        async with self._db.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE memories SET superseded_by = $2 WHERE id = $1",
                memory_id, by or memory_id,
            )
        return result.endswith("1")
