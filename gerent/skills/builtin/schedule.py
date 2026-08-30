"""Letting the agent manage its own time.

Self-scheduling is what makes the agent feel like a colleague rather than a command:
"retry the deploy check in 20 minutes" and "summarise the repo every weekday at 09:00"
both fall out of the existing loop. See docs/scheduling.md#self-scheduling.
"""

from __future__ import annotations

from gerent.skills.base import Risk, Skill, SkillContext, SkillResult


class ScheduleWork(Skill):
    name = "schedule"
    description = (
        "Schedule yourself to do something later: a cron expression for recurring work, "
        "an ISO-8601 timestamp for a one-off, or a duration like '20m' for an interval. "
        "Use it to retry something that failed for a transient reason, or to set up "
        "recurring work the user asked for. Times are interpreted in the given IANA "
        "timezone."
    )
    risk = Risk.WRITE
    defer_loading = False
    input_schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["cron", "once", "interval"]},
            "expr": {
                "type": "string",
                "description": "Cron expression, ISO-8601 timestamp, or duration (20m).",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "What to do when it fires. Self-contained - there will be no "
                    "conversation to refer back to."
                ),
            },
            "timezone": {"type": "string", "description": "IANA name, e.g. Europe/Warsaw."},
        },
        "required": ["kind", "expr", "prompt"],
        "additionalProperties": False,
    }

    async def run(
        self,
        ctx: SkillContext,
        kind: str = "cron",
        expr: str = "",
        prompt: str = "",
        timezone: str = "UTC",
    ) -> SkillResult:
        from gerent.scheduler.models import Schedule, ScheduleKind, next_fire

        scheduler = getattr(ctx, "scheduler", None)
        try:
            schedule = Schedule(
                prompt=prompt,
                kind=ScheduleKind(kind),
                expr=expr,
                timezone=timezone,
                actor_id=ctx.actor.id,
            )
            schedule.next_fire_at = next_fire(schedule)
        except Exception as exc:  # noqa: BLE001
            return SkillResult.failure(f"could not schedule that: {exc}")

        if schedule.next_fire_at is None:
            return SkillResult.failure(f"{expr!r} has no future firing - it is in the past")

        if scheduler is None:
            # The scheduler is not running in this process, so the schedule cannot be
            # persisted. Saying so is better than silently dropping it.
            return SkillResult.failure(
                "the scheduler is not running, so this cannot be saved. Start the agent "
                "with the scheduler enabled, or do the work now instead."
            )

        await scheduler.add(schedule)
        return SkillResult(
            ok=True,
            content=(
                f"scheduled ({kind} {expr!r} in {timezone}); "
                f"next firing {schedule.next_fire_at.isoformat()}"
            ),
        )
