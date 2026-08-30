"""Scriptable TTS for tests - records what it was asked to say."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from gerent.voice.tts.base import TTSProvider


class FakeTTS(TTSProvider):
    name = "fake-tts"

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.said: list[str] = []
        self._delay = delay_s

    async def synthesize(
        self, text: AsyncIterator[str], voice: str = "default", locale: str = "en-US"
    ) -> AsyncIterator[bytes]:
        async for chunk in text:
            self.said.append(chunk)
            if self._delay:
                await asyncio.sleep(self._delay)
            yield b"\x00\x00" * 240
