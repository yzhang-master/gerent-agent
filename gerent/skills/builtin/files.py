"""Filesystem skills. Every path goes through the guardrails' symlink-safe resolver."""

from __future__ import annotations

from gerent.core.errors import GuardrailDenied
from gerent.skills.base import Artifact, Risk, Skill, SkillContext, SkillResult

_MAX_READ_BYTES = 200_000


class ReadFile(Skill):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace and return its contents with line "
        "numbers. Use before editing so you match existing text exactly. For listing a "
        "directory or searching many files, use `shell` instead."
    )
    risk = Risk.READ
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace."},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    async def run(self, ctx: SkillContext, path: str = "") -> SkillResult:
        try:
            resolved = ctx.guardrails.resolve_path(ctx.workspace / path)
        except GuardrailDenied as exc:
            return SkillResult.failure(str(exc))
        if not resolved.is_file():
            return SkillResult.failure(f"{path}: not a file")
        raw = resolved.read_bytes()[:_MAX_READ_BYTES]
        text = raw.decode("utf-8", "replace")
        numbered = "\n".join(f"{i:5d}\t{line}" for i, line in enumerate(text.splitlines(), 1))
        return SkillResult(ok=True, content=numbered or "(empty file)")


class WriteFile(Skill):
    name = "write_file"
    description = (
        "Create a file or overwrite it entirely with new content. Creates parent "
        "directories as needed. To change part of an existing file, prefer `edit_file` - "
        "overwriting a file you have not read loses content."
    )
    risk = Risk.WRITE
    reversible = True
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    async def run(self, ctx: SkillContext, path: str = "", content: str = "") -> SkillResult:
        try:
            resolved = ctx.guardrails.resolve_path(ctx.workspace / path, for_write=True)
        except GuardrailDenied as exc:
            return SkillResult.failure(str(exc))
        existed = resolved.exists()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        verb = "overwrote" if existed else "created"
        return SkillResult(
            ok=True,
            content=f"{verb} {path} ({len(content)} bytes)",
            artifacts=[Artifact(kind="file", path=str(resolved), summary=verb)],
        )


class EditFile(Skill):
    name = "edit_file"
    description = (
        "Replace an exact string in a file with new text. The old string must appear "
        "exactly once; if it appears zero times or more than once the edit is refused "
        "rather than guessed at. Read the file first to match its text precisely."
    )
    risk = Risk.WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string", "description": "Exact text to replace."},
            "new": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    }

    async def run(
        self, ctx: SkillContext, path: str = "", old: str = "", new: str = ""
    ) -> SkillResult:
        try:
            resolved = ctx.guardrails.resolve_path(ctx.workspace / path, for_write=True)
        except GuardrailDenied as exc:
            return SkillResult.failure(str(exc))
        if not resolved.is_file():
            return SkillResult.failure(f"{path}: not a file")
        text = resolved.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return SkillResult.failure(
                f"{path}: the old string was not found. Read the file and match its "
                "exact text, including indentation."
            )
        if count > 1:
            return SkillResult.failure(
                f"{path}: the old string appears {count} times; include more "
                "surrounding context so it identifies exactly one location."
            )
        resolved.write_text(text.replace(old, new, 1), encoding="utf-8")
        return SkillResult(
            ok=True,
            content=f"edited {path}",
            artifacts=[Artifact(kind="file", path=str(resolved), summary="edited")],
        )
