"""Error taxonomy.

The distinction that matters most is retryable vs fatal at the provider boundary: the
router walks its failover chain on the former and stops on the latter. A 400 will be
equally malformed at the next provider, and retrying it just burns money twice.
"""

from __future__ import annotations


class GerentError(Exception):
    """Base for everything raised by this project."""


class ConfigError(GerentError):
    """Configuration is invalid. Raised at startup, never mid-run."""


class ProviderError(GerentError):
    def __init__(self, message: str, *, provider: str = "", status: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status = status


class RetryableProviderError(ProviderError):
    """Transient: 429, 5xx, timeout, connection reset. The router fails over."""


class FatalProviderError(ProviderError):
    """Permanent: 400, 401, 404. Failing over would repeat the same mistake."""


class ProviderUnavailable(ProviderError):
    """The adapter cannot run at all - SDK not installed, or no credentials."""


class RefusalError(ProviderError):
    """The model declined the request."""


class SkillError(GerentError):
    """A skill could not be invoked at all.

    Note this is *not* how a skill reports failure. A skill that runs and fails returns
    SkillResult(ok=False); raising kills the turn, and an autonomous agent needs to read
    the failure and route around it. See docs/skills.md.
    """


class GuardrailDenied(GerentError):
    """A guardrail refused an action.

    Caught at the act boundary and converted into a tool_result, never propagated - a
    denial the agent can read is useful, a denial that kills the run is not.
    """

    def __init__(self, message: str, *, rule: str = ""):
        super().__init__(message)
        self.rule = rule


class BudgetExhausted(GerentError):
    """A run ceiling was hit. Ends the run cleanly, with a report."""


class KillSwitch(GerentError):
    """SIGTERM or the sentinel file. Halt after the in-flight tool call, then report."""
