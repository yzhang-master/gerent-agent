import uuid
from pathlib import Path

import pytest

from gerent.core.config import Config
from gerent.core.kernel import Kernel
from gerent.core.types import Actor, Source, TurnRequest
from gerent.reasoning.engine import Engine
from gerent.reasoning.providers.fake import FakeProvider
from gerent.reasoning.router import Router
from gerent.skills.registry import SkillRegistry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "hello.txt").write_text("original content\n")
    return tmp_path


@pytest.fixture
def config(workspace: Path) -> Config:
    return Config.model_validate(
        {
            "providers": {"fake": {"enabled": True}},
            "roles": {"worker": ["fake:m"], "planner": ["fake:m"], "cheap": ["fake:m"]},
            "guardrails": {
                "workspace_roots": [str(workspace)],
                "checkpoint_dir": str(workspace / ".checkpoints"),
                "kill_switch_file": str(workspace / ".STOP"),
            },
        }
    )


@pytest.fixture
def make_kernel(config, workspace):
    def _make(provider: FakeProvider) -> Kernel:
        router = Router(config)
        router.register("fake", provider)
        registry = SkillRegistry(config.skills).discover()
        return Kernel(config, Engine(router), registry, workspace=workspace)

    return _make


@pytest.fixture
def turn():
    def _turn(text: str, source: Source = Source.CLI) -> TurnRequest:
        return TurnRequest(
            session_id=uuid.uuid4(), source=source, text=text, actor=Actor(name="test")
        )

    return _turn
