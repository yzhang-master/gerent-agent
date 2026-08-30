"""Renders the journal into what a human reads.

For an agent that never interrupts, the report is the interface. Fixed structure, and
three rules the renderer enforces: failures are as prominent as successes, assumptions
always appear, and the undo section is mandatory whenever a checkpoint was taken -
reversibility the human cannot find is not reversibility.

See docs/reporting.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gerent.reporting.journal import Journal, JournalKind


@dataclass
class Report:
    goal: str
    outcome: str
    did: list[str] = field(default_factory=list)
    changed: list[tuple[str, str]] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    restore_commands: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0

    def to_markdown(self) -> str:
        lines = [f"# {self.goal or 'Run'}", "", self.outcome, ""]

        if self.did:
            lines += ["## What I did", ""]
            lines += [f"- {item}" for item in self.did]
            lines.append("")

        if self.changed:
            lines += ["## What changed", "", "| Path | Change |", "|------|--------|"]
            lines += [f"| `{path}` | {change} |" for path, change in self.changed]
            lines.append("")

        # Always rendered, even when empty: these are the decisions a human would
        # otherwise have been asked to make, and suppressing the "obvious" ones is how
        # a wrong one slips through.
        lines += ["## Assumptions I made", ""]
        if self.assumptions:
            for a in sorted(self.assumptions, key=lambda x: x.get("confidence", 0.5)):
                conf = a.get("confidence", 0.5)
                lines.append(
                    f"- **{a.get('question', '?')}** → {a.get('chosen', '?')} "
                    f"_(confidence {conf:.0%}; {a.get('rationale', 'no rationale recorded')})_"
                )
        else:
            lines.append("None - nothing was underdetermined.")
        lines.append("")

        # Never a footnote. A report that lists only wins is broken.
        if self.failures or self.degradations:
            lines += ["## What didn't work", ""]
            lines += [f"- {f}" for f in self.failures]
            lines += [f"- _degraded_: {d}" for d in self.degradations]
            lines.append("")

        lines += [
            "## Cost",
            "",
            f"{self.duration_s:.1f}s · {self.tokens:,} tokens · ${self.cost_usd:.4f}",
            "",
        ]

        if self.restore_commands:
            lines += ["## How to undo this", "", "```bash"]
            lines += self.restore_commands
            lines += ["```", ""]

        return "\n".join(lines)


def build_report(journal: Journal) -> Report:
    did: list[str] = []
    changed: list[tuple[str, str]] = []
    failures: list[str] = []
    assumptions: list[dict] = []
    degradations: list[str] = []
    restores: list[str] = []
    tool_calls = 0

    for entry in journal.entries:
        p = entry.payload
        match entry.kind:
            case JournalKind.TOOL_RESULT:
                tool_calls += 1
                skill = p.get("skill", "?")
                if p.get("ok"):
                    did.append(f"`{skill}` — {_first_line(p.get('content', ''))}")
                else:
                    failures.append(f"`{skill}` failed — {_first_line(p.get('content', ''))}")
            case JournalKind.DENIAL:
                failures.append(
                    f"`{p.get('skill', '?')}` refused by the {p.get('rule', '?')} rule"
                )
            case JournalKind.ASSUMPTION:
                assumptions.append(p)
            case JournalKind.DEGRADATION:
                detail = p.get("detail", "")
                if detail not in degradations:
                    degradations.append(detail)
            case JournalKind.CHECKPOINT:
                if cmd := p.get("restore_command"):
                    if cmd not in restores:
                        restores.append(cmd)
            case JournalKind.ERROR:
                failures.append(f"{p.get('kind', 'error')}: {p.get('detail', '')}")
            case _:
                pass

    for entry in journal.of_kind(JournalKind.TOOL_RESULT):
        skill = entry.payload.get("skill")
        if skill in ("write_file", "edit_file") and entry.payload.get("ok"):
            changed.append((_path_of(entry.payload.get("content", "")), skill))

    outcome = _outcome(journal, tool_calls, failures)
    return Report(
        goal=journal.goal,
        outcome=outcome,
        did=did,
        changed=changed,
        assumptions=assumptions,
        failures=failures,
        degradations=degradations,
        restore_commands=restores,
        duration_s=journal.duration_s,
        tokens=journal.usage.input_tokens + journal.usage.output_tokens,
        cost_usd=journal.usage.cost_usd,
    )


def _outcome(journal: Journal, tool_calls: int, failures: list[str]) -> str:
    if failures and tool_calls == 0:
        return f"**Did not complete.** {failures[0]}"
    if failures:
        return (
            f"**Completed with {len(failures)} failure(s)** across {tool_calls} "
            "tool call(s). See *What didn't work* below."
        )
    if tool_calls:
        return f"Completed. {tool_calls} tool call(s), no failures."
    return "Completed without needing any tools."


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "(no output)"
    return line[:160]


def _path_of(content: str) -> str:
    parts = content.split()
    return parts[1] if len(parts) > 1 else content[:60]
