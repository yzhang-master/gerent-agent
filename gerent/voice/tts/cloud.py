"""Cloud TTS backends.

NOT YET IMPLEMENTED - see the note in gerent/voice/stt/cloud.py. Integration point:

  Cartesia  wss://api.cartesia.ai/tts/websocket
            auth:   X-API-Key: $CARTESIA_API_KEY, Cartesia-Version header
            send:   JSON per utterance - model_id, transcript, voice, language,
                    output_format {container: raw, encoding: pcm_s16le, sample_rate}
            recv:   base64 audio chunks until a done message
            -> decode and yield each chunk as PCM bytes

The pipeline already hands this one sentence at a time, so nothing here needs to
buffer or re-chunk.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from gerent.core.errors import ProviderUnavailable
from gerent.voice.tts.base import TTSProvider


class CartesiaTTS(TTSProvider):
    name = "cartesia"

    @classmethod
    def available(cls) -> tuple[bool, str]:
        if not os.environ.get("CARTESIA_API_KEY"):
            return False, "CARTESIA_API_KEY is not set"
        return False, "not implemented yet - see gerent/voice/tts/cloud.py"

    def synthesize(
        self, text: AsyncIterator[str], voice: str = "default", locale: str = "en-US"
    ) -> AsyncIterator[bytes]:
        raise ProviderUnavailable(
            "CartesiaTTS is not implemented yet; use a local backend",
            provider=self.name,
        )
