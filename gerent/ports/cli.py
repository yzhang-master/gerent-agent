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
from gerent.memory.store import InMemoryMemoryStore
from gerent.planning.executor import Executor
from gerent.planning.planner import Planner, needs_plan
from gerent.planning.store import InMemoryPlanStore, PostgresPlanStore
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
    # Without a DSN this is non-durable, which the `do` command states plainly rather
    # than letting the user assume otherwise.
    memories = InMemoryMemoryStore()
    return Kernel(config, Engine(router), registry, workspace=workspace, memories=memories)


_streaming = False


def _render_event(event, *, quiet: bool) -> None:
    """The TurnEvent rendering table for a terminal. This is the whole port contract."""
    global _streaming
    match event.kind:
        case EventKind.TEXT_DELTA:
            console.print(event.text, end="", markup=False, highlight=False)
            _streaming = True
        case EventKind.THINKING:
            if not quiet and event.text.strip():
                _newline()
                console.print(f"[dim]{event.text.strip()[:120]}[/dim]")
        case EventKind.ASSUMPTION:
            _newline()
            console.print(f"[yellow]assumed:[/yellow] {event.text}")
        case EventKind.TOOL_RESULT:
            _newline()
            mark = "[green]✓[/green]" if event.data.get("ok") else "[red]✗[/red]"
            detail = event.text.splitlines()[0][:100] if event.text else ""
            console.print(f"{mark} [cyan]{event.data.get('skill')}[/cyan] {detail}")
        case EventKind.DEGRADATION:
            _newline()
            console.print(f"[yellow]degraded:[/yellow] {event.text}")
        case EventKind.ERROR:
            _newline()
            console.print(f"[red]error:[/red] {event.text}")
        case EventKind.DONE:
            _newline()


def _newline() -> None:
    global _streaming
    if _streaming:
        console.print()
        _streaming = False


async def _render(kernel: Kernel, request: TurnRequest, journal: Journal, *, quiet: bool) -> None:
    async for event in kernel.run(request, journal=journal):
        _render_event(event, quiet=quiet)


async def _plan_store(config: Config):
    """The real store is Postgres; in-memory is a stated degradation, not a default."""
    if not config.db.dsn:
        return InMemoryPlanStore(), False
    from gerent.db.pool import Database

    db = Database(config.db.dsn, min_size=config.db.pool_min, max_size=config.db.pool_max)
    await db.connect()
    await db.migrate()
    return PostgresPlanStore(db), True


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

        if not needs_plan(text):
            # Most goals are one turn. Planning them doubles latency and produces
            # four-step plans for one-step jobs.
            await _render(kernel, request, journal, quiet=quiet)
            return

        store, durable = await _plan_store(config)
        if not durable:
            console.print(
                "[yellow]note:[/yellow] no [bold]db.dsn[/bold] configured - this plan is "
                "held in memory and will not survive a crash. See docs/data-model.md."
            )
        executor = Executor(kernel, Planner(kernel.engine), store, config)
        plan = await Planner(kernel.engine).plan(text, journal=journal)
        console.print(f"[dim]plan: {len(plan.steps)} step(s)[/dim]")
        for index, step in enumerate(plan.steps, 1):
            console.print(f"[dim]  {index}. {step.description}[/dim]")

        async for event in executor.run(plan, request, journal):
            _render_event(event, quiet=quiet)
        journal.finish()

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
