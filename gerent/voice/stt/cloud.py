"""Cloud STT backends.

NOT YET IMPLEMENTED. The interface, the pipeline, the VAD and the fakes are complete and
tested; these two adapters are the remaining work for M5, and each is a vendor-specific
streaming websocket protocol that cannot be written blind or verified without a key.

Integration points, so this is a small job rather than a research one:

  Deepgram  wss://api.deepgram.com/v1/listen
            params: model, language (omit to auto-detect), encoding=linear16,
                    sample_rate=16000, interim_results=true, endpointing=false
            auth:   Authorization: Token $DEEPGRAM_API_KEY
            send:   raw PCM frames as binary messages
            recv:   JSON with channel.alternatives[0].transcript, is_final,
                    confidence, and detected language when auto-detecting
            -> map each message to Transcript(text, is_final, confidence, detected_lang)

  Endpointing stays with our own VAD rather than the vendor's, so utterance boundaries
  behave identically across backends including the local one.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from gerent.core.errors import ProviderUnavailable
from gerent.voice.stt.base import STTProvider, Transcript


class DeepgramSTT(STTProvider):
    name = "deepgram"

    @classmethod
    def available(cls) -> tuple[bool, str]:
        if not os.environ.get("DEEPGRAM_API_KEY"):
            return False, "DEEPGRAM_API_KEY is not set"
        return False, "not implemented yet - see gerent/voice/stt/cloud.py"

    def stream(
        self, pcm: AsyncIterator[bytes], locale: str | None = None
    ) -> AsyncIterator[Transcript]:
        raise ProviderUnavailable(
            "DeepgramSTT is not implemented yet; use a local backend or the CLI port",
            provider=self.name,
        )
