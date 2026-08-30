"""Delegating to an agent CLI.

Codex and Claude Code own their own loop, tools, and working directory, so they plug in
here as subcontractors rather than as ModelProviders - wiring them in as providers would
nest two agent loops with no shared budget or guardrails. See ADR 0003.

Guardrails apply unchanged: the sub-agent runs inside a sandbox root and the kernel
checkpoints before it starts, so a subcontractor's mistake is exactly as reversible as
the agent's own.
"""

from __future__ import annotations

import asyncio
import shutil

from gerent.skills.base import Artifact, Cost, Risk, Skill, SkillContext, SkillResult

# argv templates. The prompt is passed as a single argument, never interpolated into a
# shell string.
AGENTS: dict[str, list[str]] = {
    "codex": ["codex", "exec", "--full-auto"],
    "claude": ["claude", "-p", "--permission-mode", "acceptEdits"],
}


class Delegate(Skill):
    name = "delegate"
    description = (
        "Hand a whole, self-contained task to a specialist coding agent (codex or "
        "claude) running in the workspace, and get back a summary and the diff it "
        "produced. Use it for substantial implementation work you would otherwise do "
        "over many edits. Give it one coherent task with enough context to finish "
        "alone - it cannot see this conversation, and it will not ask you anything."
    )
    domain = "core"
    risk = Risk.DESTRUCTIVE
    reversible = False
    cost_hint = Cost.HIGH
    input_schema = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": ["codex", "claude"]},
            "task": {
                "type": "string",
                "description": "The complete instruction, self-contained.",
            },
            "timeout_s": {"type": "number"},
        },
        "required": ["agent", "task"],
        "additionalProperties": False,
    }

    async def run(
        self,
        ctx: SkillContext,
        agent: str = "codex",
        task: str = "",
        timeout_s: float = 900.0,
    ) -> SkillResult:
        argv = AGENTS.get(agent)
        if argv is None:
            return SkillResult.failure(f"unknown agent {agent!r}; known: {sorted(AGENTS)}")
        if shutil.which(argv[0]) is None:
            return SkillResult.failure(
                f"{argv[0]} is not installed or not on PATH - do the work yourself instead"
            )

        await ctx.progress(f"delegating to {agent}: {task[:80]}")
        before = await _git_head(ctx)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            task,
            cwd=str(ctx.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=ctx.guardrails.env_for_subprocess() if ctx.guardrails else None,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SkillResult.failure(
                f"{agent} exceeded {timeout_s:.0f}s and was killed. The workspace may be "
                "partially modified; the checkpoint taken before this call can undo it."
            )

        output = out.decode("utf-8", "replace")
        tail = output[-4000:]
        diff = await _git_diff(ctx, before)

        artifacts = [Artifact(kind="diff", path=str(ctx.workspace), summary="delegate diff")]
        content = f"{agent} exited with code {proc.returncode}.\n\n{tail}"
        if diff:
            content += f"\n\nChanges made:\n{diff[:4000]}"

        # The sub-agent's own token spend is invisible here; it does not appear in the
        # run budget. Stated in ADR 0003 rather than papered over.
        return SkillResult(ok=proc.returncode == 0, content=content, artifacts=artifacts)


async def _git_head(ctx: SkillContext) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=str(ctx.workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def _git_diff(ctx: SkillContext, _before: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", "--stat", "HEAD",
        cwd=str(ctx.workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace").strip()
