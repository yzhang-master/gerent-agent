"""Semantic memory: scoping, supersession, and automatic injection."""

import uuid

from gerent.core.kernel import Kernel
from gerent.core.types import Actor, Role, Source, TurnRequest
from gerent.memory.store import InMemoryMemoryStore
from gerent.reasoning.engine import Engine
from gerent.reasoning.providers.fake import FakeProvider
from gerent.reasoning.router import Router
from gerent.skills.registry import SkillRegistry


async def test_recall_is_scoped_to_the_actor():
    """Filtered from day one - a cross-tenant leak is not a bug to find after adding
    tenancy."""
    store = InMemoryMemoryStore()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    await store.remember("this repo uses pnpm, not npm", alice)
    await store.remember("this repo uses cargo", bob)

    found = await store.recall("which package manager does this repo use", alice)

    assert len(found) == 1 and "pnpm" in found[0].content


async def test_superseded_facts_stop_being_recalled_but_are_not_deleted():
    store = InMemoryMemoryStore()
    actor = uuid.uuid4()
    old = await store.remember("the project uses npm", actor)
    await store.remember("the project uses pnpm", actor)

    await store.supersede(old.id)

    found = await store.recall("project package manager", actor)
    assert [m.content for m in found] == ["the project uses pnpm"]
    assert old.superseded_by is not None, "superseded, not deleted - it stays inspectable"


async def test_memories_are_injected_automatically(config, workspace):
    """The `recall` skill's description promises this, so it has to be true."""
    store = InMemoryMemoryStore()
    actor = Actor(name="t")
    await store.remember("deploys always go to staging first", actor.id)

    provider = FakeProvider().say("understood")
    router = Router(config)
    router.register("fake", provider)
    kernel = Kernel(
        config,
        Engine(router),
        SkillRegistry(config.skills).discover(),
        workspace=workspace,
        memories=store,
    )
    request = TurnRequest(
        session_id=uuid.uuid4(), source=Source.CLI, text="deploy the app", actor=actor
    )

    async for _ in kernel.run(request):
        pass

    sent = provider.calls[0].messages
    system_msgs = [m for m in sent if m.role is Role.SYSTEM]
    assert system_msgs, "relevant memories must reach the model"
    assert "staging first" in system_msgs[0].text()
    # Volatile per-turn context must not be in the cached system prompt.
    assert sent[-1].role is Role.USER


async def test_a_broken_memory_store_does_not_fail_the_turn(config, workspace):
    class Broken(InMemoryMemoryStore):
        async def recall(self, *a, **kw):
            raise RuntimeError("database is down")

    router = Router(config)
    router.register("fake", FakeProvider().say("fine"))
    kernel = Kernel(
        config,
        Engine(router),
        SkillRegistry(config.skills).discover(),
        workspace=workspace,
        memories=Broken(),
    )
    request = TurnRequest(
        session_id=uuid.uuid4(), source=Source.CLI, text="hello", actor=Actor(name="t")
    )
    events = [e async for e in kernel.run(request)]
    assert events[-1].kind.value == "done", "memory is an enhancement, not a dependency"
