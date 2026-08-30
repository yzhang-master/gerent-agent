"""End-to-end turns through the kernel, driven by a scripted provider - no API spend."""

from pathlib import Path

from gerent.core.types import EventKind
from gerent.reasoning.providers.fake import FakeProvider
from gerent.reporting.journal import Journal, JournalKind
from gerent.reporting.reporter import build_report


async def drain(kernel, request, journal=None):
    return [e async for e in kernel.run(request, journal=journal)]


async def test_plain_reply(make_kernel, turn):
    kernel = make_kernel(FakeProvider().say("Warsaw is the capital of Poland."))
    events = await drain(kernel, turn("what is the capital of Poland?"))
    text = "".join(e.text for e in events if e.kind is EventKind.TEXT_DELTA)
    assert "Warsaw" in text
    assert events[-1].kind is EventKind.DONE


async def test_tool_call_then_answer(make_kernel, turn, workspace: Path):
    provider = FakeProvider().call_tool("read_file", path="hello.txt").say("It says 'original'.")
    kernel = make_kernel(provider)
    journal = Journal(goal="read the file")

    events = await drain(kernel, turn("read hello.txt"), journal)

    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert results and results[0].data == {"skill": "read_file", "ok": True}
    # The second model call must have seen the tool result.
    assert len(provider.calls) == 2
    assert any(m.role == "tool" for m in provider.calls[1].messages)


async def test_write_is_checkpointed_and_undoable(make_kernel, turn, workspace: Path):
    """A destructive skill must checkpoint first, and the report must say how to undo."""
    provider = FakeProvider().call_tool("shell", command="echo hi > new.txt").say("done")
    kernel = make_kernel(provider)
    journal = Journal(goal="make a file")

    await drain(kernel, turn("create new.txt"), journal)

    checkpoints = journal.of_kind(JournalKind.CHECKPOINT)
    assert checkpoints, "shell is reversible=False and must be checkpointed before running"
    report = build_report(journal)
    assert report.restore_commands, "a checkpoint without a restore command is not reversible"
    assert "## How to undo this" in report.to_markdown()


async def test_guardrail_denial_is_a_result_not_a_crash(make_kernel, turn):
    """A denial the agent can read lets it route around the boundary; one that raises
    kills the run and produces no report."""
    provider = FakeProvider().call_tool("shell", command="sudo apt install x").say("I cannot.")
    kernel = make_kernel(provider)
    journal = Journal(goal="install something")

    events = await drain(kernel, turn("install a package"), journal)

    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert results and results[0].data["ok"] is False
    assert events[-1].kind is EventKind.DONE, "the run must still finish and report"
    assert any("privilege-escalation" in f for f in build_report(journal).failures)


async def test_hard_denies_do_not_block_legitimate_commands(make_kernel, turn):
    """The deny list is short on purpose: `rm -rf ./build` is ordinary work."""
    provider = FakeProvider().call_tool("shell", command="rm -rf ./build").say("cleaned")
    kernel = make_kernel(provider)
    events = await drain(kernel, turn("clean the build dir"))
    result = next(e for e in events if e.kind is EventKind.TOOL_RESULT)
    assert result.data["ok"] is True, "a normal rm inside the workspace must not be refused"


async def test_sandbox_escape_is_refused(make_kernel, turn):
    provider = FakeProvider().call_tool("read_file", path="../../../../etc/passwd").say("nope")
    kernel = make_kernel(provider)
    events = await drain(kernel, turn("read /etc/passwd"))
    result = next(e for e in events if e.kind is EventKind.TOOL_RESULT)
    assert result.data["ok"] is False
    assert "outside the workspace roots" in result.text


async def test_budget_exhaustion_still_reports(make_kernel, turn, config):
    config.budgets.interactive.max_tool_calls = 0
    provider = FakeProvider().call_tool("read_file", path="hello.txt").say("done")
    kernel = make_kernel(provider)
    journal = Journal(goal="anything")

    events = await drain(kernel, turn("read it"), journal)

    assert events[-1].kind is EventKind.DONE
    report = build_report(journal)
    assert any("budget" in f for f in report.failures)
    assert "What didn't work" in report.to_markdown()


async def test_unknown_skill_is_reported_not_raised(make_kernel, turn):
    provider = FakeProvider().call_tool("nonexistent_skill", x=1).say("ok")
    kernel = make_kernel(provider)
    events = await drain(kernel, turn("do a thing"))
    result = next(e for e in events if e.kind is EventKind.TOOL_RESULT)
    assert result.data["ok"] is False and "no such skill" in result.text
