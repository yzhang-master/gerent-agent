"""CLI port: TurnRequest in, rendered TurnEvent out, report at the end."""

from typer.testing import CliRunner

import gerent.reasoning.router as router_module
from gerent.ports.cli import app
from gerent.reasoning.providers.fake import FakeProvider

runner = CliRunner()


def write_config(tmp_path, workspace):
    path = tmp_path / "gerent.toml"
    path.write_text(
        f"""
[providers.fake]
enabled = true

[roles]
planner = ["fake:m"]
worker  = ["fake:m"]
cheap   = ["fake:m"]
coder   = ["fake:m"]

[guardrails]
workspace_roots = ["{workspace}"]
checkpoint_dir = "{workspace}/.cps"
"""
    )
    return path


def test_do_prints_a_report(tmp_path, monkeypatch, workspace):
    class Scripted(FakeProvider):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.call_tool("read_file", path="hello.txt").say("The file says 'original'.")

    monkeypatch.setitem(router_module.PROVIDER_CLASSES, "fake", Scripted)
    config = write_config(tmp_path, workspace)

    result = runner.invoke(app, ["do", "read hello.txt", "-c", str(config), "-w", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "read_file" in result.output
    # The report is the interface for an agent that never interrupts.
    assert "Assumptions I made" in result.output
    assert "Cost" in result.output


def test_doctor_diagnoses_a_broken_config(tmp_path):
    bad = tmp_path / "gerent.toml"
    bad.write_text('[roles]\nworker = ["nosuch:model"]\n')
    result = runner.invoke(app, ["doctor", "-c", str(bad)])
    assert result.exit_code == 0, "doctor must still run on a broken config"
    assert "configuration problem" in result.output
    assert "skills" in result.output
