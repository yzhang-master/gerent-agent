"""The CLI port.

A port produces TurnRequest and renders TurnEvent. That is the whole contract - adding
the voice or WebSocket port means implementing this table again, and nothing else.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import structlog
import typer
from rich.console import Console
from rich.markdown import Markdown

from gerent.core.config import Config, load_config
from gerent.core.errors import GerentError
from gerent.core.kernel import Kernel
from gerent.core.types import Actor, EventKind, Source, TurnRequest
from gerent.reasoning.engine import Engine
from gerent.reasoning.router import PROVIDER_CLASSES, Router
from gerent.reporting.journal import Journal
from gerent.reporting.reporter import build_report
from gerent.skills.registry import SkillRegistry

app = typer.Typer(add_completion=False, help="Gerent - an autonomous agent.")
console = Console()


def _setup_logging(config: Config) -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(config.logging.level, 20)
        ),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
    )


def _build(config: Config, workspace: Path | None) -> Kernel:
    router = Router(config)
    registry = SkillRegistry(config.skills).discover()
    return Kernel(config, Engine(router), registry, workspace=workspace)


async def _render(kernel: Kernel, request: TurnRequest, journal: Journal, *, quiet: bool) -> None:
    """The TurnEvent rendering table for a terminal."""
    streaming = False
    async for event in kernel.run(request, journal=journal):
        match event.kind:
            case EventKind.TEXT_DELTA:
                console.print(event.text, end="", markup=False, highlight=False)
                streaming = True
            case EventKind.THINKING:
                if not quiet and event.text.strip():
                    console.print(f"[dim]{event.text.strip()[:120]}[/dim]")
            case EventKind.TOOL_RESULT:
                if streaming:
                    console.print()
                    streaming = False
                mark = "[green]✓[/green]" if event.data.get("ok") else "[red]✗[/red]"
                detail = event.text.splitlines()[0][:100] if event.text else ""
                console.print(f"{mark} [cyan]{event.data.get('skill')}[/cyan] {detail}")
            case EventKind.DEGRADATION:
                console.print(f"[yellow]degraded:[/yellow] {event.text}")
            case EventKind.ERROR:
                if streaming:
                    console.print()
                    streaming = False
                console.print(f"[red]error:[/red] {event.text}")
            case EventKind.DONE:
                if streaming:
                    console.print()


@app.command()
def chat(
    message: list[str] = typer.Argument(None, help="Message. Omitted, starts a REPL."),
    config_path: Path = typer.Option(None, "--config", "-c"),
    provider: str = typer.Option(None, "--provider", "-p", help="Override the provider chain."),
    model: str = typer.Option(None, "--model", "-m"),
    workspace: Path = typer.Option(None, "--workspace", "-w"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide reasoning."),
) -> None:
    """Talk to the agent."""
    config = _load(config_path, provider, model)
    _setup_logging(config)
    kernel = _build(config, workspace)
    session = uuid.uuid4()

    async def once(text: str) -> None:
        journal = Journal(goal=text)
        request = TurnRequest(
            session_id=session, source=Source.CLI, text=text, actor=Actor(name="cli")
        )
        await _render(kernel, request, journal, quiet=quiet)

    if message:
        asyncio.run(once(" ".join(message)))
        return

    console.print("[dim]gerent - ctrl-d to exit[/dim]")
    while True:
        try:
            text = console.input("[bold cyan]›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if text:
            asyncio.run(once(text))


@app.command()
def do(
    goal: list[str] = typer.Argument(..., help="What to accomplish."),
    config_path: Path = typer.Option(None, "--config", "-c"),
    provider: str = typer.Option(None, "--provider", "-p"),
    model: str = typer.Option(None, "--model", "-m"),
    workspace: Path = typer.Option(None, "--workspace", "-w"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Give the agent a goal, walk away, read the report.

    Runs to completion with no prompts and prints a result report.
    """
    text = " ".join(goal)
    config = _load(config_path, provider, model)
    _setup_logging(config)
    kernel = _build(config, workspace)
    journal = Journal(goal=text)

    async def run() -> None:
        request = TurnRequest(
            session_id=uuid.uuid4(),
            source=Source.CLI,
            text=text,
            actor=Actor(name="cli"),
        )
        await _render(kernel, request, journal, quiet=quiet)

    asyncio.run(run())
    console.rule("[bold]report[/bold]")
    console.print(Markdown(build_report(journal).to_markdown()))


@app.command()
def doctor(config_path: Path = typer.Option(None, "--config", "-c")) -> None:
    """Show what is configured, what is reachable, and what is missing."""
    config = _load(config_path, None, None, validate=False)
    try:
        config.validate_roles()
        console.print("[green]configuration valid[/green]")
    except GerentError as exc:
        console.print(f"[yellow]configuration problem:[/yellow] {exc}\n")
    roots = [str(r) for r in config.guardrails.workspace_roots]
    console.print(f"[bold]workspace roots[/bold]: {roots}")
    console.print(f"[bold]timezone[/bold]: {config.agent.timezone}")

    console.print("\n[bold]providers[/bold]")
    for name, cls in PROVIDER_CLASSES.items():
        ok, reason = cls.available()
        enabled = config.providers.get(name)
        state = "enabled" if enabled and enabled.enabled else "disabled"
        mark = "[green]ready[/green]" if ok else f"[yellow]unavailable[/yellow] ({reason})"
        console.print(f"  {name:12} {state:9} {mark}")

    console.print("\n[bold]roles[/bold]")
    for role in ("planner", "worker", "cheap", "coder"):
        console.print(f"  {role:9} {' → '.join(config.roles.chain(role))}")

    registry = SkillRegistry(config.skills).discover()
    console.print(f"\n[bold]skills[/bold] ({len(registry.all())})")
    for skill in sorted(registry.all(), key=lambda s: (s.domain, s.name)):
        flag = "" if skill.reversible else " [red](checkpointed)[/red]"
        console.print(f"  {skill.name:14} {skill.domain:8} {skill.risk}{flag}")
    if registry.unavailable:
        console.print("\n[yellow]unavailable packs[/yellow]")
        for pack, reason in registry.unavailable.items():
            console.print(f"  {pack}: {reason}")

    console.print("\n[bold]database[/bold]")
    console.print(f"  {config.db.dsn or '[dim]not configured - state is in-memory only[/dim]'}")


def _load(
    path: Path | None, provider: str | None, model: str | None, *, validate: bool = True
) -> Config:
    try:
        config = load_config(path)
    except GerentError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(1) from exc

    if provider:
        # An explicit --provider replaces every chain, which is how the same goal gets
        # run twice and the reports compared.
        config.providers.setdefault(provider, type(config.providers.get(provider) or _pc())())
        config.providers[provider].enabled = True
        for role in ("planner", "worker", "cheap", "coder"):
            chosen = model or _default_model(provider)
            setattr(config.roles, role, [f"{provider}:{chosen}"])
    if not validate:
        # `doctor` diagnoses a broken configuration; refusing to run on one would
        # withhold the diagnosis exactly when it is needed.
        return config
    try:
        config.validate_roles()
    except GerentError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(1) from exc
    return config


def _pc():
    from gerent.core.config import ProviderConfig

    return ProviderConfig


def _default_model(provider: str) -> str:
    return {
        "anthropic": "claude-opus-5",
        "openai": "gpt-5",
        "openrouter": "anthropic/claude-opus-5",
        "ollama": "qwen3",
    }.get(provider, "default")


def main() -> None:
    try:
        app()
    except GerentError as exc:
        console.print(f"[red]{type(exc).__name__}:[/red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
