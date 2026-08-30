# 0005 — Own tool loop, not a vendor tool runner

**Status:** Accepted

## Context

Vendor SDKs ship helpers that drive the tool-call loop: you supply tool functions and
the helper handles the request → execute → resend cycle. They remove real boilerplate,
and some offer per-turn hooks sufficient for interception and retries.

## Decision

Implement the loop directly.

## Consequences

**Gained:** conversation history lives in Postgres, which is what makes a run
resumable after a crash, auditable afterward, reportable to a human, and
re-renderable for cross-provider failover. Vendor-specific mid-turn conditions
(`pause_turn`, refusal stop reasons) are handled explicitly rather than by whatever the
helper decides. One loop shape across every provider, including those whose SDK ships
no helper at all.

**Paid:** roughly a hundred lines of loop we maintain, and the obligation to get the
edge cases right ourselves — parallel tool calls returned in a single message,
accumulating streamed tool-call fragments, error results that must come back as
`tool_result` rather than exceptions.

## Rejected

*Vendor tool runners.* They keep history in private memory. Every property listed above
depends on owning that history — and a helper that hides it cannot provide any of them.
Notably, at least one such runner exits silently on a paused turn, which unattended is
indistinguishable from successful completion.
