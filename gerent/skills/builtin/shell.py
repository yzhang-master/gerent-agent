"""Shell execution.

Marked DESTRUCTIVE and reversible=False because its effects cannot be inferred from
its arguments, so a checkpoint is taken before every invocation. That is deliberately
conservative, and it is what makes an unattended agent with shell access defensible.
"""

from __future__ import annotations

import asyncio

from gerent.core.errors import GuardrailDenied
from gerent.skills.base import Cost, Risk, Skill, SkillContext, SkillResult


class Shell(Skill):
    name = "shell"
    description = (
        "Run a shell command in the workspace and return stdout, stderr and the exit "
        "code. Use for builds, tests, git, and inspecting the tree. Do not use it to "
        "edit files - use `write_file` or `edit_file`, which are checkpointed and "
        "produce reviewable diffs."
    )
    domain = "core"
    risk = Risk.DESTRUCTIVE
    reversible = False
    cost_hint = Cost.MEDIUM
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to run."},
            "timeout_s": {"type": "number", "description": "Seconds before it is killed."},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    async def run(
        self, ctx: SkillContext, command: str = "", timeout_s: float | None = None
    ) -> SkillResult:
        guardrails = ctx.guardrails
        try:
            guardrails.check_command(command)
        except GuardrailDenied as exc:
            # A denial the model can read lets it route around the boundary.
            return SkillResult.failure(str(exc))

        limit = timeout_s or guardrails.config.shell_timeout_s
        await ctx.progress(f"$ {command}")

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(ctx.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=guardrails.env_for_subprocess(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return SkillResult.failure(f"command timed out after {limit:.0f}s: {command}")

        cap = guardrails.config.max_output_bytes
        stdout = out[:cap].decode("utf-8", "replace")
        stderr = err[:cap].decode("utf-8", "replace")
        truncated = len(out) > cap or len(err) > cap

        parts = [f"exit code: {proc.returncode}"]
        if stdout.strip():
            parts.append(f"stdout:\n{stdout}")
        if stderr.strip():
            parts.append(f"stderr:\n{stderr}")
        if truncated:
            parts.append(f"[output truncated at {cap} bytes]")

        return SkillResult(ok=proc.returncode == 0, content="\n".join(parts))
