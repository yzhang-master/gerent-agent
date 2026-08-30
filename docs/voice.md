# Voice

Speech in and speech out, streaming both directions, in any of ~90 languages. Voice is
not a mode the agent switches into — it is a port that produces `TurnRequest` and
renders `TurnEvent`, exactly like the CLI.

## Pipeline

```
mic ──► PCM ──► VAD ──► utterance ──► STT ──► TurnRequest
                                                   │
                                                 kernel
                                                   │
speaker ◄── audio ◄── TTS ◄── sentence chunks ◄── text_delta
```

`voice/pipeline.py` owns the duplex loop. `sounddevice` for I/O, **silero-VAD** (ONNX)
for endpointing — small enough to be free on CPU, and far better than energy
thresholding in a room with background noise.

## Provider interfaces

Both sides stream. Batch interfaces would add a full utterance of latency at each end.

```python
class STTProvider(ABC):
    async def stream(
        self, pcm: AsyncIterator[bytes], locale: str | None
    ) -> AsyncIterator[Transcript]: ...

class TTSProvider(ABC):
    async def synthesize(
        self, text: AsyncIterator[str], voice: str, locale: str
    ) -> AsyncIterator[bytes]: ...
```

```python
@dataclass
class Transcript:
    text: str
    is_final: bool
    confidence: float
    detected_lang: str | None    # ← this is what makes it world-wide
```

`detected_lang` feeds locale routing and per-turn TTS voice selection, so a user can
switch languages mid-conversation and the reply comes back in the language they just
spoke. Passing `locale=None` means "detect it"; passing a locale is faster and more
accurate when the language is known.

## Backends

| | Default (cloud) | Fallback (local) |
|---|---|---|
| STT | Deepgram — streaming, multilingual | `faster-whisper` `base`/`small`, int8 |
| TTS | Cartesia — streaming, low latency | Piper |

Cloud is the default because **this machine has no GPU**. `faster-whisper base` is
roughly realtime on 8 CPU cores; `small` is slower than realtime and unusable
conversationally; anything larger is out of the question. Piper is intelligible and
noticeably robotic.

The local path exists for offline and privacy-sensitive use, and it is honestly worse.
It is a fallback, not a peer.

## Latency budget

The whole user experience is one number: time from the user stopping speaking to the
first audio out.

| Stage | Target |
|---|---|
| VAD endpoint detection | 100 ms |
| STT final transcript | 300 ms |
| First model token | 600 ms |
| TTS first audio chunk | 300 ms |
| **Total** | **~1.3 s** |

Above roughly 2 seconds, people start talking over the agent because they assume it did
not hear them.

Every leg is instrumented from day one, because when this regresses it is never obvious
which stage did it.

**Routing is part of the budget.** A `planner`-class model on a conversational turn
blows it outright, which is why voice acknowledgements route to the `cheap` role and
only substantive turns escalate. See [providers.md](providers.md#routing-and-failover).

## Barge-in

If the user starts speaking while the agent is talking, the agent stops.

```
VAD fires during playback
  → cancel the TTS task
  → stop playback immediately
  → truncate the assistant message to what was actually spoken
  → treat the new speech as the next turn
```

The truncation step is the one that gets missed. If the conversation history records
the full intended reply while the user only heard the first eight words, every
subsequent turn is reasoning from a transcript that does not match reality — and the
divergence compounds silently.

Without barge-in the whole thing feels like a phone menu. It is not a refinement.

## Rendering `TurnEvent`

| Event | Voice rendering |
|---|---|
| `thinking` | Optional filler ("let me check that…") on long pauses — this is why the Anthropic adapter requests summarized thinking rather than the omitted default |
| `text_delta` | Buffered to sentence boundaries, then synthesized |
| `tool_call` / `tool_result` | Suppressed; optionally a soft earcon for long operations |
| `assumption` | Spoken briefly — "I'm assuming pytest" |
| `report` | Spoken **summary**: outcome, count of changes, low-confidence assumptions. Never reads diffs aloud; the full report goes to the terminal or the sink |
| `done` | Pipeline re-arms for the next utterance |

Sentence-boundary chunking is what makes TTS sound like speech rather than a stutter.
Chunking on token boundaries is audibly wrong.

## Known limits

- **No speaker diarization** in v1 — one speaker assumed. Multi-party audio degrades
  badly.
- **Noisy environments** hurt VAD endpointing more than they hurt STT accuracy; the
  symptom is premature cut-off, not garbled text.
- **Code and file paths are miserable to speak.** Spoken output summarizes; anything
  precise goes to the terminal.
- **Cloud STT/TTS means audio leaves the machine.** The local backends exist for when
  that is unacceptable, at the quality cost stated above.
