"""Voice pipeline: sentence chunking, barge-in truncation, and spoken summaries."""

import asyncio

import pytest

from gerent.core.kernel import Kernel
from gerent.reasoning.engine import Engine
from gerent.reasoning.providers.fake import FakeProvider
from gerent.reasoning.router import Router
from gerent.skills.registry import SkillRegistry
from gerent.voice.pipeline import SentenceChunker, VoicePipeline
from gerent.voice.stt.fake import FakeSTT
from gerent.voice.tts.fake import FakeTTS


def test_chunker_releases_whole_sentences():
    chunker = SentenceChunker()
    got = []
    for token in ["The build ", "failed", ". I fixed ", "the import", "."]:
        got += chunker.feed(token)
    assert got == ["The build failed.", "I fixed the import."]


def test_abbreviations_do_not_freeze_the_chunker():
    """A short boundary must be skipped, not treated as a stopping point - otherwise
    the whole reply is held back and played as one lump after a long silence."""
    chunker = SentenceChunker()
    released = chunker.feed("Dr. Smith arrived at the office. Then he left.")
    # "Dr." is skipped as too short, but both real sentences still come out - the
    # abbreviation must not stop everything after it.
    assert released == ["Dr. Smith arrived at the office.", "Then he left."]
    assert chunker.flush() == ""


def make_pipeline(config, workspace, provider, *, tts=None):
    router = Router(config)
    router.register("fake", provider)
    kernel = Kernel(
        config, Engine(router), SkillRegistry(config.skills).discover(), workspace=workspace
    )
    return VoicePipeline(kernel, FakeSTT(), tts or FakeTTS())


async def test_reply_is_spoken_sentence_by_sentence(config, workspace):
    tts = FakeTTS()
    pipeline = make_pipeline(
        config, workspace, FakeProvider().say("The build failed. I fixed the import."), tts=tts
    )
    audio = []
    await pipeline.handle_utterance("what happened", locale="en-US", sink=audio.append)

    assert tts.said == ["The build failed.", "I fixed the import."]
    assert audio, "PCM must reach the sink"


async def test_barge_in_truncates_history_to_what_was_heard(config, workspace):
    """If history records the full intended reply while the user heard eight words,
    every later turn reasons from a conversation that did not happen."""
    tts = FakeTTS(delay_s=0.05)
    pipeline = make_pipeline(
        config,
        workspace,
        FakeProvider().say("First sentence here. Second sentence here. Third sentence here."),
        tts=tts,
    )

    task = asyncio.create_task(
        pipeline.handle_utterance("go", locale="en-US", sink=lambda _: None)
    )
    await asyncio.sleep(0.06)  # partway through speaking
    spoken = pipeline.interrupt()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert spoken, "something was said before the interruption"
    assert len(spoken) < len("First sentence here. Second sentence here. Third sentence here.")


async def test_locale_flows_through_to_synthesis(config, workspace):
    tts = FakeTTS()
    pipeline = make_pipeline(config, workspace, FakeProvider().say("Dzień dobry."), tts=tts)
    captured = {}

    original = tts.synthesize

    def spy(text, voice="default", locale="en-US"):
        captured["locale"] = locale
        return original(text, voice, locale)

    tts.synthesize = spy
    await pipeline.handle_utterance("cześć", locale="pl-PL", sink=lambda _: None)
    assert captured["locale"] == "pl-PL", "detected language must drive the voice"
