"""Skill discovery and the bounded tool surface.

Adding a domain requires no core change. That is the property being protected here, and
it is worth failing a code review over. See docs/skills.md.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
from pathlib import Path

import structlog

from gerent.core.config import SkillsConfig
from gerent.reasoning.providers.base import Capabilities, ToolSpec
from gerent.skills import builtin
from gerent.skills.base import Skill
from gerent.skills.retrieval import rank

log = structlog.get_logger(__name__)


class SkillRegistry:
    def __init__(self, config: SkillsConfig | None = None) -> None:
        self.config = config or SkillsConfig()
        self._skills: dict[str, Skill] = {}
        self.unavailable: dict[str, str] = {}

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, skill: Skill) -> None:
        skill.validate()  # at registration, not at first call
        if skill.name in self._skills:
            raise ValueError(
                f"duplicate skill name {skill.name!r} "
                f"(already provided by domain {self._skills[skill.name].domain!r})"
            )
        if skill.name in self.config.disabled:
            return
        self._skills[skill.name] = skill

    def discover(self) -> SkillRegistry:
        self._discover_builtin()
        self._discover_domains(self.config.domains_dir)
        return self

    def _discover_builtin(self) -> None:
        for info in pkgutil.iter_modules(builtin.__path__):
            self._load_module(f"{builtin.__name__}.{info.name}", label=f"builtin/{info.name}")

    def _discover_domains(self, domains_dir: Path) -> None:
        root = Path(domains_dir).expanduser()
        if not root.is_dir():
            return
        for pack in sorted(p for p in root.iterdir() if p.is_dir()):
            entry = pack / "skills.py"
            if not entry.exists():
                continue
            spec = importlib.util.spec_from_file_location(f"gerent_domain_{pack.name}", entry)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                self._register_from(module)
            except Exception as exc:
                # A broken pack must not take the agent down. It is skipped, logged,
                # and reported as unavailable.
                self.unavailable[pack.name] = str(exc)
                log.warning("skills.pack_failed", pack=pack.name, error=str(exc))

    def _load_module(self, dotted: str, label: str) -> None:
        try:
            module = importlib.import_module(dotted)
            self._register_from(module)
        except Exception as exc:
            self.unavailable[label] = str(exc)
            log.warning("skills.module_failed", module=dotted, error=str(exc))

    def _register_from(self, module: object) -> None:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Skill) and obj is not Skill and not inspect.isabstract(obj):
                if obj.__module__ != getattr(module, "__name__", None):
                    continue  # imported, not defined here
                self.register(obj())

    # ── access ───────────────────────────────────────────────────────────────

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def resident(self) -> list[Skill]:
        return [s for n in self.config.resident if (s := self._skills.get(n))]

    def specs(
        self, capabilities: Capabilities, query: str = ""
    ) -> tuple[list[ToolSpec], str | None]:
        """The tool surface for one turn, bounded regardless of installed domains.

        Returns (specs, degradation). Ordering is deterministic - an unstable tool list
        invalidates the cached prefix on every request.
        """
        skills = self.all()
        if not skills:
            return [], None

        if capabilities.deferred_tools:
            # Native: everything ships, non-resident entries marked deferred. The
            # search tool itself is never deferred and at least one tool stays
            # resident, or the request is rejected.
            resident = {s.name for s in self.resident()} or {skills[0].name}
            specs = []
            for skill in sorted(skills, key=lambda s: s.name):
                spec = skill.to_spec()
                spec.defer_loading = skill.name not in resident
                specs.append(spec)
            return specs, None

        keep = {s.name for s in self.resident()}
        pool = [s for s in skills if s.name not in keep]
        chosen = keep | {s.name for s in rank(query, pool, self.config.retrieval_top_k)}
        specs = [s.to_spec() for s in sorted(skills, key=lambda s: s.name) if s.name in chosen]
        for spec in specs:
            spec.defer_loading = False

        degradation = None
        if len(specs) < len(skills):
            degradation = (
                f"deferred tool loading unavailable; retrieved {len(specs)} of "
                f"{len(skills)} skills by relevance"
            )
        return specs, degradation
