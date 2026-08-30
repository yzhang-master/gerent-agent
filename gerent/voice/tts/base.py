"""Speech synthesis interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class TTSProvider(ABC):
    name: str = "tts"
    sample_rate: int = 24_000

    @classmethod
    def available(cls) -> tuple[bool, str]:
        return True, ""

    @abstractmethod
    def synthesize(
        self, text: AsyncIterator[str], voice: str = "default", locale: str = "en-US"
    ) -> AsyncIterator[bytes]:
        """Consume text chunks and yield PCM as it is produced.

        Text arrives already split on sentence boundaries by the pipeline; chunking on
        token boundaries is audibly wrong.
        """
        raise NotImplementedError
