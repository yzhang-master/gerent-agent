"""Router behaviour: role resolution and the failover boundary."""

import pytest

from gerent.core.config import Config
from gerent.core.errors import ConfigError, RetryableProviderError
from gerent.reasoning.providers.base import CompletionRequest, TextDelta
from gerent.reasoning.providers.fake import FakeProvider
from gerent.reasoning.router import Router


def make_config(**roles):
    return Config.model_validate(
        {
            "providers": {"a": {"enabled": True}, "b": {"enabled": True}},
            "roles": roles or {"worker": ["a:m1", "b:m2"]},
        }
    )


def req():
    return CompletionRequest(model="", messages=[])


async def drain(router, role, request):
    return [e async for e in router.stream(role, request)]


async def test_fails_over_before_first_event():
    router = Router(make_config())
    router.register("a", FakeProvider(fail_times=1))
    router.register("b", FakeProvider().say("from b"))

    r = req()
    events = await drain(router, "worker", r)

    assert any(isinstance(e, TextDelta) and e.text == "from b" for e in events)
    assert any("failed over from a:m1" in d for d in r.degradations), (
        "a failover must be recorded so the report can explain the lost cache warmth"
    )


async def test_does_not_fail_over_mid_stream():
    """Once text has reached a port it cannot be un-said, so a mid-stream failure
    must surface rather than splice a second provider's answer onto the first."""

    class HalfBroken(FakeProvider):
        async def stream(self, request):
            yield TextDelta("partial answer")
            raise RetryableProviderError("died mid-stream", provider="a", status=503)

    router = Router(make_config())
    router.register("a", HalfBroken())
    router.register("b", FakeProvider().say("from b"))

    with pytest.raises(RetryableProviderError):
        await drain(router, "worker", req())


async def test_unknown_role_is_a_config_error():
    router = Router(make_config())
    with pytest.raises(ConfigError):
        router.routes("nonexistent")


async def test_delegate_entries_are_skipped():
    """Agent CLIs are a skill, not a provider - ADR 0003."""
    router = Router(make_config(coder=["delegate:codex", "a:m1"]))
    router.register("a", FakeProvider())
    routes = router.routes("coder")
    assert len(routes) == 1 and routes[0].model == "m1"


async def test_effort_is_applied_from_role_config():
    router = Router(make_config(cheap=["a:m1"]))
    fake = FakeProvider().say("hi")
    router.register("a", fake)
    await drain(router, "cheap", req())
    assert fake.calls[0].effort == "low"
