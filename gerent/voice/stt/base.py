"""Speech recognition interface.

Streaming on both sides: a batch interface would add a full utterance of latency at each
end, and the entire voice UX is one number. See docs/voice.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class Transcript:
    text: str
    is_final: bool = False
    confidence: float = 1.0
    # What makes the agent world-wide: it drives locale routing and per-turn TTS voice
    # selection, so a user can switch language mid-conversation and be answered in it.
    detected_lang: str | None = None


class STTProvider(ABC):
    name: str = "stt"

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, ""

    @abstractmethod
    def stream(
        self, pcm: AsyncIterator[bytes], locale: str | None = None
    ) -> AsyncIterator[Transcript]:
        """Consume 16 kHz mono PCM and yield partial then final transcripts.

        `locale=None` means detect it; passing one is faster and more accurate when the
        language is known.
        """
        raise NotImplementedError
