"""Role-based routing with failover.

Callers ask for a role - planner, worker, cheap, coder - never a model. The router
resolves the role to an ordered chain and walks it on availability errors.

See docs/providers.md#routing-and-failover.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import structlog

from gerent.core.config import Config
from gerent.core.errors import (
    ConfigError,
    FatalProviderError,
    ProviderError,
    ProviderUnavailable,
    RetryableProviderError,
)
from gerent.reasoning.providers.anthropic import AnthropicProvider
from gerent.reasoning.providers.base import CompletionRequest, ModelProvider, ProviderEvent
from gerent.reasoning.providers.openai_compat import (
    OllamaProvider,
    OpenAIProvider,
    OpenRouterProvider,
)

log = structlog.get_logger(__name__)

PROVIDER_CLASSES: dict[str, type[ModelProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
}


class Route:
    def __init__(
        self, key: str, provider: ModelProvider, model: str, effort: str | None
    ) -> None:
        # `key` is the name the operator configured, which is what logs and reports
        # must quote - the provider instance's own class name may differ.
        self.key = key
        self.provider = provider
        self.model = model
        self.effort = effort

    @property
    def label(self) -> str:
        return f"{self.key}:{self.model}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"Route({self.label})"


class Router:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._instances: dict[str, ModelProvider] = {}

    def register(self, name: str, provider: ModelProvider) -> None:
        """Inject a provider instance directly - used by tests and by FakeProvider."""
        self._instances[name] = provider

    def _instance(self, name: str) -> ModelProvider:
        if name in self._instances:
            return self._instances[name]
        cls = PROVIDER_CLASSES.get(name)
        if cls is None:
            raise ConfigError(f"unknown provider {name!r}")
        pc = self._config.providers.get(name)
        api_key = os.environ.get(pc.api_key_env) if pc and pc.api_key_env else None
        provider = cls(api_key=api_key, base_url=pc.base_url if pc else None)
        self._instances[name] = provider
        return provider

    def routes(self, role: str) -> list[Route]:
        """Resolve a role to its chain. Entries that cannot run are skipped, not fatal."""
        effort = self._config.roles.effort.get(role)
        out: list[Route] = []
        for entry in self._config.roles.chain(role):
            provider_name, _, model = entry.partition(":")
            if provider_name == "delegate":
                # Agent CLIs are a skill, not a provider. See ADR 0003.
                continue
            if not model:
                raise ConfigError(f"role {role!r}: entry {entry!r} must be 'provider:model'")
            try:
                out.append(Route(provider_name, self._instance(provider_name), model, effort))
            except (ProviderUnavailable, ConfigError) as exc:
                log.warning("route.unavailable", role=role, entry=entry, reason=str(exc))
        if not out:
            raise ConfigError(
                f"no usable provider for role {role!r}; "
                f"chain was {self._config.roles.chain(role)}"
            )
        return out

    async def stream(self, role: str, req: CompletionRequest) -> AsyncIterator[ProviderEvent]:
        """Stream a completion for `role`, failing over on availability errors.

        Failover happens only *before the first event is emitted*. Once text has been
        streamed to a port it cannot be un-said, so a mid-stream failure is re-raised
        rather than silently restarted on another provider - which would otherwise
        show the user two different half-answers spliced together.
        """
        routes = self.routes(role)
        last: Exception | None = None

        for index, route in enumerate(routes):
            req.model = route.model
            req.effort = route.effort
            emitted = False
            try:
                async for event in route.provider.stream(req):
                    emitted = True
                    yield event
                return
            except RetryableProviderError as exc:
                last = exc
                if emitted:
                    log.error("route.failed_mid_stream", route=route.label, error=str(exc))
                    raise
                log.warning(
                    "route.failover",
                    role=role,
                    failed=route.label,
                    remaining=len(routes) - index - 1,
                    error=str(exc),
                )
                # Note it so the report can explain why this turn cost more than usual:
                # caches are provider-scoped, so a failover turn loses cache warmth.
                req.degradations.append(f"failed over from {route.label} ({exc})")
                continue
            except FatalProviderError:
                # A malformed request is equally malformed at the next provider.
                raise

        raise last or ProviderError(f"no route succeeded for role {role!r}")
