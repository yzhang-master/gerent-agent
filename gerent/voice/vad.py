"""Voice activity detection - where an utterance starts and stops.

Endpointing quality is what people experience as "it interrupted me" or "it didn't
notice I finished". In a noisy room it degrades before recognition accuracy does, and
the symptom is premature cut-off rather than garbled text.
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass

FRAME_MS = 30
SAMPLE_RATE = 16_000
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000  # 16-bit mono


@dataclass
class VadConfig:
    # Hangover: speech must be absent for this long before the utterance is considered
    # over. Too short and normal pauses split a sentence in two.
    silence_ms: int = 600
    min_speech_ms: int = 200
    energy_threshold: float = 0.015


class EnergyVad:
    """RMS-energy endpointing.

    A deliberate fallback rather than the intended default: silero-VAD (ONNX, CPU-cheap)
    is far better in a room with background noise. This exists so the pipeline runs, and
    is tested, without onnxruntime installed.
    """

    name = "energy"

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self._speech_ms = 0
        self._silence_ms = 0
        self.in_speech = False

    @staticmethod
    def rms(frame: bytes) -> float:
        if not frame:
            return 0.0
        samples = array.array("h")
        samples.frombytes(frame[: len(frame) // 2 * 2])
        if not samples:
            return 0.0
        total = sum(float(s) * float(s) for s in samples)
        return math.sqrt(total / len(samples)) / 32768.0

    def accept(self, frame: bytes) -> str:
        """Feed one frame. Returns 'start', 'end', or '' for no transition."""
        loud = self.rms(frame) >= self.config.energy_threshold
        if loud:
            self._speech_ms += FRAME_MS
            self._silence_ms = 0
            if not self.in_speech and self._speech_ms >= self.config.min_speech_ms:
                self.in_speech = True
                return "start"
        else:
            self._silence_ms += FRAME_MS
            if self.in_speech and self._silence_ms >= self.config.silence_ms:
                self.in_speech = False
                self._speech_ms = 0
                return "end"
            if not self.in_speech:
                self._speech_ms = 0
        return ""

    def reset(self) -> None:
        self._speech_ms = self._silence_ms = 0
        self.in_speech = False
