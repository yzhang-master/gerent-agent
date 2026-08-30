"""Configuration: one TOML file, environment overrides, loud validation at startup.

Secrets are never read from the config file - a config file is something people paste
into issues. Keys come from the environment.

See docs/configuration.md.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from gerent.core.errors import ConfigError

CONFIG_NAME = "gerent.toml"


class AgentConfig(BaseModel):
    name: str = "gerent"
    default_locale: str = "en-US"
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{v!r} is not a valid IANA timezone name") from exc
        return v


class ProviderConfig(BaseModel):
    enabled: bool = False
    base_url: str | None = None
    api_key_env: str | None = None


class RolesConfig(BaseModel):
    """Ordered failover chains, keyed by role. Callers ask for roles, never models."""

    planner: list[str] = ["anthropic:claude-opus-5"]
    worker: list[str] = ["anthropic:claude-opus-5"]
    cheap: list[str] = ["anthropic:claude-haiku-4-5"]
    coder: list[str] = ["anthropic:claude-opus-5"]
    effort: dict[str, str] = Field(
        default_factory=lambda: {
            "planner": "xhigh",
            "worker": "high",
            "cheap": "low",
            "coder": "xhigh",
        }
    )

    def chain(self, role: str) -> list[str]:
        chain = getattr(self, role, None)
        if not chain:
            raise ConfigError(f"no provider chain configured for role {role!r}")
        return chain


class GuardrailConfig(BaseModel):
    workspace_roots: list[Path] = Field(default_factory=lambda: [Path.cwd()])
    allow_credentials: list[Path] = Field(default_factory=list)
    checkpoint_dir: Path = Path("~/.gerent/checkpoints")
    kill_switch_file: Path = Path("~/.gerent/STOP")
    shell_timeout_s: float = 120.0
    max_output_bytes: int = 100_000

    @field_validator("workspace_roots", "allow_credentials", mode="after")
    @classmethod
    def _expand(cls, v: list[Path]) -> list[Path]:
        return [p.expanduser().resolve() for p in v]

    @field_validator("checkpoint_dir", "kill_switch_file", mode="after")
    @classmethod
    def _expand_one(cls, v: Path) -> Path:
        return v.expanduser()


class BudgetConfig(BaseModel):
    max_wall_clock_s: float | None = 1800
    max_tokens: int | None = 2_000_000
    max_cost_usd: float | None = 5.0
    max_tool_calls: int | None = 200


class BudgetsConfig(BaseModel):
    interactive: BudgetConfig = BudgetConfig()
    # Tighter by default: an unattended run that spends $5 at 03:00 is discovered at
    # the end of the month.
    scheduled: BudgetConfig = BudgetConfig(
        max_wall_clock_s=600, max_tokens=500_000, max_cost_usd=1.0, max_tool_calls=60
    )


class PlanningConfig(BaseModel):
    max_replans: int = 3
    max_step_attempts: int = 2
    max_parallel_steps: int = 4


class MemoryConfig(BaseModel):
    retrieval_top_k: int = 8
    auto_extract: bool = True


class SkillsConfig(BaseModel):
    domains_dir: Path = Path("./domains")
    disabled: list[str] = Field(default_factory=list)
    resident: list[str] = Field(
        default_factory=lambda: [
            "read_file",
            "write_file",
            "edit_file",
            "shell",
            "remember",
            "recall",
        ]
    )
    retrieval_top_k: int = 12


class SchedulerConfig(BaseModel):
    enabled: bool = False
    poll_interval_s: float = 30.0
    report_sink: str = "file:~/.gerent/reports/"
    max_retry_depth: int = 3


class DbConfig(BaseModel):
    dsn: str | None = None
    pool_min: int = 2
    pool_max: int = 10


class LoggingConfig(BaseModel):
    level: str = "info"
    format: str = "console"


class Config(BaseModel):
    agent: AgentConfig = AgentConfig()
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    roles: RolesConfig = RolesConfig()
    guardrails: GuardrailConfig = GuardrailConfig()
    budgets: BudgetsConfig = BudgetsConfig()
    planning: PlanningConfig = PlanningConfig()
    memory: MemoryConfig = MemoryConfig()
    skills: SkillsConfig = SkillsConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    db: DbConfig = DbConfig()
    logging: LoggingConfig = LoggingConfig()

    def enabled_providers(self) -> list[str]:
        return [n for n, p in self.providers.items() if p.enabled]

    def validate_roles(self) -> None:
        """Fail loudly on a role pointing at a provider that is off or unknown.

        This is the misconfiguration that otherwise surfaces hours later as strange
        behaviour, usually in an unattended run.
        """
        enabled = set(self.enabled_providers())
        for role in ("planner", "worker", "cheap", "coder"):
            for entry in self.roles.chain(role):
                provider = entry.split(":", 1)[0]
                if provider == "delegate":
                    continue
                if provider not in self.providers:
                    raise ConfigError(
                        f"role {role!r} references unknown provider {provider!r}; "
                        f"known: {sorted(self.providers) or '(none configured)'}"
                    )
                if provider not in enabled:
                    raise ConfigError(
                        f"role {role!r} references provider {provider!r}, which is disabled"
                    )


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict[str, Any]:
    """GERENT__SECTION__KEY=value overrides any config key."""
    out: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith("GERENT__"):
            continue
        parts = [p.lower() for p in key.removeprefix("GERENT__").split("__")]
        if not parts:
            continue
        try:
            value: Any = tomllib.loads(f"v = {raw}")["v"]
        except tomllib.TOMLDecodeError:
            value = raw
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return out


def find_config_file(explicit: Path | None = None) -> Path | None:
    if explicit:
        if not explicit.exists():
            raise ConfigError(f"config file not found: {explicit}")
        return explicit
    if env := os.environ.get("GERENT_CONFIG"):
        return Path(env)
    for candidate in (Path.cwd() / CONFIG_NAME, Path.home() / ".config/gerent" / CONFIG_NAME):
        if candidate.exists():
            return candidate
    return None


def load_config(path: Path | None = None) -> Config:
    """Resolve ./gerent.toml -> ~/.config/gerent/gerent.toml -> defaults, then env."""
    raw: dict[str, Any] = {}
    if found := find_config_file(path):
        try:
            raw = tomllib.loads(found.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{found}: {exc}") from exc

    merged = _deep_merge(raw, _env_overrides())
    try:
        config = Config.model_validate(merged)
    except Exception as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc

    for root in config.guardrails.workspace_roots:
        if not root.exists():
            raise ConfigError(f"guardrails.workspace_roots: {root} does not exist")
    return config
