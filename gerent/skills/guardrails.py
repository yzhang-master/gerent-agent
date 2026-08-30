"""Guardrails: what replaces approval prompts.

The agent never asks permission, so safety cannot come from prompts. It comes from
making work undoable, bounded, and accounted for. See docs/autonomy.md and ADR 0002.

A denial is returned to the model as a normal tool result, never raised past the act
boundary - a denial the agent can read lets it route around the boundary; a denial that
kills the run produces no report at all.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from gerent.core.config import GuardrailConfig
from gerent.core.errors import BudgetExhausted, GuardrailDenied, KillSwitch
from gerent.core.types import Budget, Usage

log = structlog.get_logger(__name__)

# Deliberately short and NOT extensible. A long deny list becomes a policy engine, a
# policy engine grows exceptions, and exceptions need someone to approve them - which
# is the thing being removed. See ADR 0002.
_DENY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rm-root", re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rR][a-zA-Z]*f?[^|;&]*\s+/(\s|$)")),
    ("rm-root", re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*f[a-zA-Z]*[rR][^|;&]*\s+/(\s|$)")),
    (
        "force-push-default",
        re.compile(r"git\s+push\b[^|;&]*(--force|-f)\b[^|;&]*\b(main|master)\b"),
    ),
    ("privilege-escalation", re.compile(r"^\s*(sudo|doas|su)\b")),
    ("disk-write", re.compile(r"\b(mkfs|dd)\b[^|;&]*\bof=/dev/")),
    ("fork-bomb", re.compile(r":\(\)\s*\{.*\|.*&.*\}")),
    ("history-wipe", re.compile(r"\bgit\s+(reflog\s+expire|gc)\b[^|;&]*--prune=(now|all)")),
]

# Credential material. Reads are refused unless the exact path is allowlisted.
_CREDENTIAL_PARTS = (".ssh", ".aws", ".gnupg", ".kube", ".docker/config.json", ".netrc")
_CREDENTIAL_NAMES = (".env", "credentials", "id_rsa", "id_ed25519", ".npmrc", ".pypirc")


@dataclass
class RunBudget:
    """Tracks spend for one run.

    Exhaustion ends the run cleanly *with* a report - never mid-write. A budget that
    truncates without a report is worse than no budget, because nobody knows what state
    anything is in.
    """

    budget: Budget
    started_at: float = field(default_factory=time.monotonic)
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0

    def add(self, usage: Usage) -> None:
        self.usage = self.usage + usage

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def exceeded(self) -> str | None:
        # `is not None` rather than truthiness: a limit of 0 means "none allowed",
        # and treating it as unlimited would be the opposite of what was asked.
        b = self.budget
        if b.max_wall_clock_s is not None and self.elapsed_s > b.max_wall_clock_s:
            return f"wall clock {self.elapsed_s:.0f}s exceeded limit {b.max_wall_clock_s:.0f}s"
        total = self.usage.input_tokens + self.usage.output_tokens
        if b.max_tokens is not None and total > b.max_tokens:
            return f"token budget exhausted ({total} > {b.max_tokens})"
        if b.max_cost_usd is not None and self.usage.cost_usd > b.max_cost_usd:
            return f"cost budget exhausted (${self.usage.cost_usd:.2f} > ${b.max_cost_usd:.2f})"
        if b.max_tool_calls is not None and self.tool_calls >= b.max_tool_calls:
            return f"tool call budget exhausted ({self.tool_calls} >= {b.max_tool_calls})"
        return None

    def check(self) -> None:
        if reason := self.exceeded():
            raise BudgetExhausted(reason)


class Guardrails:
    def __init__(self, config: GuardrailConfig) -> None:
        self.config = config
        self._roots = [Path(r).resolve() for r in config.workspace_roots]
        self._allowed_credentials = {Path(p).resolve() for p in config.allow_credentials}
        self._killed = False
        self._install_signal_handler()

    # ── kill switch ──────────────────────────────────────────────────────────

    def _install_signal_handler(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            self._killed = True
            log.warning("guardrails.kill_switch", signal=signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not the main thread, or no signal support. The sentinel file still
                # works, so this is a degraded stop, not a broken one.
                pass

    def check_kill_switch(self) -> None:
        """Halt after the in-flight tool call, then report."""
        if self._killed:
            raise KillSwitch("stop requested by signal")
        if self.config.kill_switch_file.expanduser().exists():
            raise KillSwitch(f"stop requested via {self.config.kill_switch_file}")

    # ── paths ────────────────────────────────────────────────────────────────

    def resolve_path(self, raw: str | Path, *, for_write: bool = False) -> Path:
        """Resolve and verify a path against the sandbox roots.

        Symlinks are resolved BEFORE the containment check. Checking the literal path
        first would let a symlink inside the sandbox pointing outside it defeat the
        whole mechanism.
        """
        path = Path(raw).expanduser()
        # strict=False so a not-yet-created file still resolves its parent chain.
        resolved = path.resolve()

        if not any(_is_within(resolved, root) for root in self._roots):
            raise GuardrailDenied(
                f"path {resolved} is outside the workspace roots "
                f"({', '.join(str(r) for r in self._roots)})",
                rule="sandbox-root",
            )

        if not for_write and self._is_credential(resolved):
            raise GuardrailDenied(
                f"{resolved} looks like credential material and is not allowlisted",
                rule="credentials",
            )
        return resolved

    def _is_credential(self, path: Path) -> bool:
        if path in self._allowed_credentials:
            return False
        text = str(path)
        if any(f"/{part}" in text for part in _CREDENTIAL_PARTS):
            return True
        return path.name in _CREDENTIAL_NAMES

    # ── commands ─────────────────────────────────────────────────────────────

    def check_command(self, command: str) -> None:
        """Refuse the small non-negotiable set. Everything else runs."""
        for rule, pattern in _DENY_PATTERNS:
            if pattern.search(command):
                raise GuardrailDenied(
                    f"refused: command matches the {rule!r} hard-deny rule", rule=rule
                )
        try:
            tokens = shlex.split(command)
        except ValueError:
            return  # unparseable quoting is the shell's problem, not a guardrail's
        if tokens and tokens[0] in {"shutdown", "reboot", "halt", "poweroff"}:
            raise GuardrailDenied("refused: host power control", rule="power")

    def env_for_subprocess(self) -> dict[str, str]:
        """Environment for spawned processes, with provider keys stripped.

        A subprocess has no need for the agent's API keys, and a command that echoes
        its environment should not be able to exfiltrate them.
        """
        env = dict(os.environ)
        for key in list(env):
            if re.search(r"(API_KEY|_TOKEN|SECRET|PASSWORD)$", key):
                env.pop(key, None)
        return env


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
