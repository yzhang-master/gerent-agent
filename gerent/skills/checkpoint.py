"""Checkpoints: the mechanism that replaces the confirmation dialog.

Any skill whose effect cannot be inferred from its arguments is checkpointed before it
runs, and the exact restore command appears in the run report. The agent acts, and you
can always put it back. See docs/autonomy.md and ADR 0002.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass
class CheckpointRef:
    kind: str  # "git" | "snapshot" | "none"
    ref: str
    restore_command: str
    created_at: datetime
    note: str = ""


async def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


class Checkpointer:
    def __init__(self, run_id: uuid.UUID, snapshot_dir: Path) -> None:
        self.run_id = run_id
        self.snapshot_dir = Path(snapshot_dir).expanduser()
        self._seq = 0
        self.taken: list[CheckpointRef] = []

    async def checkpoint(self, workspace: Path, label: str = "") -> CheckpointRef:
        self._seq += 1
        repo = await self._git_root(workspace)
        ref = (
            await self._git_checkpoint(repo, label)
            if repo
            else await self._snapshot(workspace, label)
        )
        self.taken.append(ref)
        log.info("checkpoint.taken", kind=ref.kind, ref=ref.ref, label=label)
        return ref

    async def _git_root(self, workspace: Path) -> Path | None:
        code, out = await _git(workspace, "rev-parse", "--show-toplevel")
        return Path(out) if code == 0 and out else None

    async def _git_checkpoint(self, repo: Path, label: str) -> CheckpointRef:
        """Commit the working tree to a scratch ref.

        Uses a throwaway index file, so the user's index, branches, stash and HEAD are
        never touched - a checkpoint must not disturb work in progress.
        """
        ref_name = f"refs/gerent/{self.run_id}/{self._seq}"
        tmp_index = self.snapshot_dir / f"index-{self.run_id}-{self._seq}"
        tmp_index.parent.mkdir(parents=True, exist_ok=True)
        env = {"GIT_INDEX_FILE": str(tmp_index)}
        try:
            code, out = await _git(repo, "add", "-A", env=env)
            if code != 0:
                return await self._snapshot(repo, label, note=f"git add failed: {out}")
            code, tree = await _git(repo, "write-tree", env=env)
            if code != 0:
                return await self._snapshot(repo, label, note=f"write-tree failed: {tree}")

            message = f"gerent checkpoint {self._seq}: {label or 'pre-action'}"
            parent_args: list[str] = []
            code, head = await _git(repo, "rev-parse", "HEAD")
            if code == 0 and head:
                parent_args = ["-p", head]
            code, commit = await _git(
                repo,
                "commit-tree",
                tree,
                *parent_args,
                "-m",
                message,
                env={
                    **env,
                    "GIT_AUTHOR_NAME": "gerent",
                    "GIT_AUTHOR_EMAIL": "gerent@localhost",
                    "GIT_COMMITTER_NAME": "gerent",
                    "GIT_COMMITTER_EMAIL": "gerent@localhost",
                },
            )
            if code != 0:
                return await self._snapshot(repo, label, note=f"commit-tree failed: {commit}")
            await _git(repo, "update-ref", ref_name, commit)
        finally:
            tmp_index.unlink(missing_ok=True)

        return CheckpointRef(
            kind="git",
            ref=commit,
            # Restores tracked content from the checkpoint tree. Files created after
            # the checkpoint are left in place - stated plainly so the report does not
            # promise more than this undoes.
            restore_command=f"git restore --source={commit} --worktree -- .",
            created_at=datetime.now(UTC),
            note=f"scratch ref {ref_name}",
        )

    async def _snapshot(self, workspace: Path, label: str, note: str = "") -> CheckpointRef:
        """Content-addressed copy, for workspaces that are not git repositories."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        digest = hashlib.sha256(f"{workspace}{self._seq}{stamp}".encode()).hexdigest()[:12]
        dest = self.snapshot_dir / str(self.run_id) / f"{self._seq:03d}-{digest}"
        dest.parent.mkdir(parents=True, exist_ok=True)

        # The snapshot directory may live inside the workspace being snapshotted;
        # copying it into itself recurses until the filesystem complains.
        snapshot_root = self.snapshot_dir.resolve()
        patterns = shutil.ignore_patterns(
            ".git", "node_modules", "__pycache__", ".venv", "*.pyc"
        )

        def ignore(directory: str, names: list[str]) -> set[str]:
            skipped = set(patterns(directory, names))
            for name in names:
                candidate = (Path(directory) / name).resolve()
                if candidate == snapshot_root or snapshot_root.is_relative_to(candidate):
                    skipped.add(name)
            return skipped

        def copy() -> None:
            shutil.copytree(workspace, dest, dirs_exist_ok=True, ignore=ignore, symlinks=True)

        await asyncio.to_thread(copy)
        return CheckpointRef(
            kind="snapshot",
            ref=str(dest),
            restore_command=f"cp -a {dest}/. {workspace}/",
            created_at=datetime.now(UTC),
            note=note or f"snapshot of {workspace}",
        )
