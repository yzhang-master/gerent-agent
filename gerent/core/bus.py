"""In-process pub/sub for TurnEvent.

An interface rather than direct calls so it can become Redis/NATS when the agent
becomes a service. v1 is a single process, so this is deliberately small.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from gerent.core.types import TurnEvent


class EventBus:
    def __init__(self, maxsize: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[TurnEvent | None]] = set()
        self._maxsize = maxsize

    async def publish(self, event: TurnEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A slow consumer must not stall the kernel. Dropping a display event
                # is survivable; blocking the agent on a stuck renderer is not. The
                # durable record is the journal, not this bus.
                pass

    async def close(self) -> None:
        for q in list(self._subscribers):
            q.put_nowait(None)

    def subscribe(self) -> _Subscription:
        q: asyncio.Queue[TurnEvent | None] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return _Subscription(self, q)


class _Subscription:
    def __init__(self, bus: EventBus, queue: asyncio.Queue[TurnEvent | None]) -> None:
        self._bus = bus
        self._queue = queue

    async def __aenter__(self) -> AsyncIterator[TurnEvent]:
        return self._iter()

    async def __aexit__(self, *exc: object) -> None:
        self._bus._subscribers.discard(self._queue)

    async def _iter(self) -> AsyncIterator[TurnEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event
