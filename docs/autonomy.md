# Autonomy and guardrails

Gerent does not ask permission. It receives a goal, works it end to end, resolves its
own ambiguities, and reports when finished — the way you would brief a competent
colleague rather than supervise an intern.

That is a requirement, not a default. This document is about what makes it defensible.

## What is given up

State it plainly: **with approval gates removed, mistakes execute.** A wrong `rm`, a
bad refactor, a misread instruction — there is no dialog in front of any of them.

Approval prompts were doing real work. Something has to replace them, or this is just
an unsafe agent with a nicer story.

## What replaces it

The substitution is **from "ask first" to "always undoable, always bounded, always
accounted for."** A prompt stops a mistake before it happens. Reversibility fixes it
after. For work an agent does unattended — at 03:00, or while you are in a meeting —
only the second one is available, because there is nobody there to answer the prompt.

Four mechanisms, in `skills/guardrails.py` and `skills/checkpoint.py`.

### 1. Sandboxed roots

Filesystem, shell, and delegate skills operate only inside configured workspace roots.

Path validation resolves symlinks **before** checking containment — the check must be
on the real path, or a symlink inside the sandbox pointing out of it defeats the whole
mechanism. An escape attempt is a hard error and a journal entry, never a prompt.

```toml
[guardrails]
workspace_roots = ["/home/pus/Documents/gerent-agent", "/home/pus/projects"]
```

### 2. Checkpoint before irreversible

Any skill with `reversible=False` or `risk=DESTRUCTIVE` triggers a checkpoint first:

- **Inside a git repo** — commit the working tree to a scratch ref
  (`refs/gerent/<run-id>/<seq>`). Cheap, and it never touches the user's branches,
  index, or stash.
- **Outside one** — content-addressed snapshot of the files in scope, under
  `~/.gerent/checkpoints/<run-id>/`.

Every checkpoint records the exact command to restore it, and those commands appear in
the run report. **This is what replaces the confirmation dialog:** the agent acts, and
you can always put it back.

Honest limits — checkpoints cover the filesystem and nothing else. A dropped database
table, a sent email, a posted webhook, a deleted cloud resource: not covered. Skills
that reach outside the workspace must either be idempotent, run against a dry-run mode
first, or be listed as hard denies.

### 3. Hard denies

A short, non-negotiable list that refuses outright — no prompt, no override:

- writes resolving outside `workspace_roots`
- reads of credential material (`~/.ssh`, `~/.aws`, `.env`, keychains) unless the path
  is explicitly allowlisted
- `rm -rf /` and recognized equivalents
- `git push --force` to a default branch
- `sudo`, package-manager installs outside the sandbox, and privilege escalation

A denial returns a normal `tool_result` saying what was refused and why — **never** an
exception. The agent should read it, understand the boundary, and route around it. An
exception would kill the run and produce no report, which is the worst of both worlds.

The list is short on purpose. A long deny list becomes a policy engine, a policy engine
grows exceptions, and exceptions need approval — which is the thing being removed.

### 4. Budgets and the kill switch

```toml
[budgets]
max_wall_clock = "30m"
max_tokens     = 2_000_000
max_cost_usd   = 5.00
max_tool_calls = 200
```

Exhaustion ends the run **cleanly**: finish the in-flight tool call, then report what
was done and what remains. Never mid-write. A budget that truncates the run without a
report is worse than no budget, because the human has no idea what state anything is
in.

Where the provider supports a native task budget, it is passed through so the model
paces itself and lands gracefully instead of being cut off. Where it does not, Gerent
accounts for tokens client-side.

**Kill switch** — `SIGTERM` or a sentinel file halts after the current tool call, then
reports. `SIGKILL` is not graceful by definition, which is why plan state lives in
Postgres: an unclean death loses the report, not the work
([planning.md](planning.md#resumption)).

## Deciding instead of asking

The other half of autonomy: the agent must not stall on ambiguity.

A gated agent blocks and asks. A human worker decides, notes the decision, and keeps
going. Gerent does the latter — there is deliberately no `blocked` state in the plan
state machine.

When a step is underdetermined, the agent picks the most reasonable option and records:

```python
@dataclass
class Assumption:
    question: str      # "Which test framework?"
    chosen: str        # "pytest"
    rationale: str     # "pyproject.toml already lists pytest as a dev dependency"
    confidence: float
    step_id: UUID
```

Assumptions stream as `TurnEvent.assumption` and get their own section in the final
report. Low-confidence assumptions sort to the top, because those are the ones a human
would want to check first.

The failure mode this creates is real: a confident wrong assumption early in a plan can
waste the whole run. Mitigations are ordinary engineering — prefer evidence from the
workspace over guessing, record confidence honestly, and surface the assumption
prominently rather than burying it.

## Unattended runs

Scheduler-fired runs get the **same** autonomy — a worker that needs supervision at
03:00 is not a worker that runs at 03:00. What differs is calibration:

| | Interactive | Scheduled |
|---|---|---|
| Budgets | Config default | Tighter by default |
| Report | Printed to the terminal | Delivered to the configured sink |
| Failure | Human sees it immediately | Must be *pushed*, not left in a log |

That last row is the one people get wrong. An unattended agent that fails silently into
a logfile nobody reads is indistinguishable from one that was never scheduled.

## Audit

Every skill invocation appends to the journal: skill, arguments digest, guardrail
decision, checkpoint ref, provider and model used, duration, outcome. Arguments are
digested rather than stored raw where they may contain secrets.

The journal is the substrate the report is rendered from, and the record you read when
something went wrong. See [reporting.md](reporting.md).

## Review checklist

For anything that touches this area:

- Does every new `reversible=False` skill actually checkpoint? (test, not code review)
- Does path validation resolve symlinks before the containment check?
- Does a denial return a `tool_result` rather than raising?
- Does budget exhaustion still produce a report?
- Is there any code path where the agent waits for human input? There must not be.
