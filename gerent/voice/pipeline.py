"""The duplex voice loop.

Voice is a port, not a mode: it produces TurnRequest and renders TurnEvent, exactly like
the CLI. See docs/voice.md.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from collections.abc import AsyncIterator

import structlog

from gerent.core.kernel import Kernel
from gerent.core.types import Actor, EventKind, Source, TurnRequest
from gerent.reporting.journal import Journal
from gerent.reporting.reporter import build_report
from gerent.voice.stt.base import STTProvider
from gerent.voice.tts.base import TTSProvider
from gerent.voice.vad import EnergyVad

log = structlog.get_logger(__name__)

# Chunk on sentence boundaries. Synthesising token by token is audibly wrong - it
# stutters, and prosody has nothing to work with.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…])$|\n{2,}")
_MIN_CHUNK_CHARS = 12


class SentenceChunker:
    """Accumulates streamed text and releases it a sentence at a time."""

    def __init__(self, min_chars: int = _MIN_CHUNK_CHARS) -> None:
        self._buffer = ""
        self._min_chars = min_chars

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        out: list[str] = []
        while True:
            release_at = 0
            # Scan past boundaries that are too short to be a real sentence, rather
            # than stopping at the first one. "Dr." and "e.g." must not each become an
            # utterance - but they also must not freeze the chunker, which would hold
            # the entire reply back until flush and play it as one lump after a silence.
            for match in _SENTENCE_END.finditer(self._buffer):
                if len(self._buffer[: match.end()].strip()) >= self._min_chars:
                    release_at = match.end()
                    break
            if not release_at:
                break
            out.append(self._buffer[:release_at].strip())
            self._buffer = self._buffer[release_at:]
        return out

    def flush(self) -> str:
        remainder, self._buffer = self._buffer.strip(), ""
        return remainder


class VoicePipeline:
    def __init__(
        self,
        kernel: Kernel,
        stt: STTProvider,
        tts: TTSProvider,
        *,
        vad: EnergyVad | None = None,
        barge_in: bool = True,
        voice: str = "default",
    ) -> None:
        self.kernel = kernel
        self.stt = stt
        self.tts = tts
        self.vad = vad or EnergyVad()
        self.barge_in = barge_in
        self.voice = voice
        self.session_id = uuid.uuid4()
        self.spoken: list[str] = []
        self._playback: asyncio.Task | None = None

    async def handle_utterance(
        self, transcript_text: str, *, locale: str | None, sink
    ) -> str:
        """Run one spoken turn, speaking the reply as it streams.

        Returns what was actually spoken, which is not always what was generated -
        see `interrupt`.
        """
        journal = Journal(goal=transcript_text)
        request = TurnRequest(
            session_id=self.session_id,
            source=Source.VOICE,
            text=transcript_text,
            locale=locale,
            actor=Actor(name="voice"),
        )
        chunker = SentenceChunker()
        self.spoken = []

        async for event in self.kernel.run(request, journal=journal):
            match event.kind:
                case EventKind.TEXT_DELTA:
                    for sentence in chunker.feed(event.text):
                        await self._speak(sentence, locale, sink)
                case EventKind.ASSUMPTION:
                    await self._speak(f"I'm assuming {event.text}.", locale, sink)
                case EventKind.REPORT:
                    # Spoken summaries only. Reading a diff aloud is useless; the full
                    # report goes to the terminal or the configured sink.
                    report = build_report(journal)
                    await self._speak(_spoken_summary(report), locale, sink)
                case EventKind.ERROR:
                    await self._speak(f"Something went wrong: {event.text}", locale, sink)
                case _:
                    pass  # tool traffic is not narrated

        if tail := chunker.flush():
            await self._speak(tail, locale, sink)
        return " ".join(self.spoken)

    async def _speak(self, sentence: str, locale: str | None, sink) -> None:
        if not sentence.strip():
            return

        async def one() -> AsyncIterator[str]:
            yield sentence

        self._playback = asyncio.current_task()
        try:
            async for pcm in self.tts.synthesize(one(), self.voice, locale or "en-US"):
                # Sinks may be sync (a sounddevice buffer write) or async (a socket).
                result = sink(pcm)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            # Interrupted mid-sentence: what was said up to here still counts as said.
            self.spoken.append(sentence)
            raise
        self.spoken.append(sentence)

    def interrupt(self) -> str:
        """Stop talking, and report what was actually spoken.

        Truncating history to what the user heard is the step that gets missed. If the
        transcript records the full intended reply while only eight words were audible,
        every later turn reasons from a conversation that did not happen, and the
        divergence compounds silently.
        """
        if self._playback and not self._playback.done():
            self._playback.cancel()
        return " ".join(self.spoken)


def _spoken_summary(report) -> str:
    parts = [report.outcome.replace("**", "")]
    if report.changed:
        parts.append(f"{len(report.changed)} file(s) changed.")
    if report.failures:
        parts.append(f"{len(report.failures)} thing(s) failed.")
    low = [a for a in report.assumptions if a.get("confidence", 1.0) < 0.6]
    if low:
        parts.append(f"I made {len(low)} assumption(s) worth checking.")
    parts.append("The full report is in the terminal.")
    return " ".join(parts)
