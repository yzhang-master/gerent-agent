"""Scriptable STT for tests - no microphone, no network."""

from __future__ import annotations

from collections.abc import AsyncIterator

from gerent.voice.stt.base import STTProvider, Transcript


class FakeSTT(STTProvider):
    name = "fake-stt"

    def __init__(self, *transcripts: Transcript) -> None:
        self._transcripts = list(transcripts)

    async def stream(
        self, pcm: AsyncIterator[bytes], locale: str | None = None
    ) -> AsyncIterator[Transcript]:
        async for _ in pcm:
            break
        for transcript in self._transcripts:
            yield transcript
