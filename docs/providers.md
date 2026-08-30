# Providers

The LLM is a replaceable part. Claude, OpenAI, OpenRouter, and local models are
interchangeable behind one interface, and the agent can fail over between them
mid-conversation.

This is the most consequential document in the set, because the normalized message
format described here gets written to the database, and changing it later means
migrating every stored conversation.

## Two kinds of backend

Naming "Codex, Claude, OpenRouter" in one breath hides a real distinction, and
collapsing it is the main design trap here.

| | **Model API** | **Agent CLI** |
|---|---|---|
| Examples | Claude, GPT, OpenRouter, Ollama | Codex CLI, Claude Code |
| What it is | A completion endpoint | A complete agent with its own loop, tools, and working directory |
| Owns the loop? | No — Gerent does | **Yes — it does** |
| Plugs in as | `ModelProvider` | a **skill** (`delegate`) |

Wiring an agent CLI in as a `ModelProvider` would nest two agent loops inside each
other: Gerent deciding which tool to call, and Codex independently deciding which file
to edit, with neither aware of the other's plan or budget. Instead, agent CLIs are
subcontractors — see [Delegation](#delegation-agent-clis-as-a-skill) below.

## The `ModelProvider` interface

```python
class ModelProvider(ABC):
    name: str
    capabilities: Capabilities

    async def stream(self, req: CompletionRequest) -> AsyncIterator[ProviderEvent]: ...
    async def count_tokens(self, req: CompletionRequest) -> int: ...
```

Adapters live in `reasoning/providers/`: `anthropic.py`, `openai.py`, `openrouter.py`
(OpenAI-compatible wire format, different auth headers and model-id namespace),
`ollama.py`, and optionally `gemini.py`.

Every provider SDK is an **optional extra**. Installing Gerent must not drag in every
vendor's package; an adapter whose SDK is absent reports itself unavailable at startup
rather than crashing on import.

## Normalized types

Everything crossing the provider boundary is Gerent's own type. Vendor types stop at
the adapter.

```python
Block = TextBlock | ThinkingBlock | ToolCallBlock | ToolResultBlock | ImageBlock

@dataclass
class Msg:
    role: Role                  # SYSTEM | USER | ASSISTANT | TOOL
    blocks: list[Block]
    provider_meta: dict         # opaque; for round-tripping vendor extras (see below)

ProviderEvent = TextDelta | ThinkingDelta | ToolCallDelta | Usage | Stop
```

**Why this matters.** Conversation history is persisted in `messages` and replayed on
every turn. If those rows hold `anthropic.types.ContentBlock`, then the database only
speaks Anthropic, and failing over to another provider mid-conversation is impossible
because the history cannot be re-rendered. Normalize on the way in, re-render per
provider on the way out.

**`provider_meta`** is the escape hatch for vendor-specific data that must survive a
round trip to the *same* provider — Anthropic thinking-block signatures being the
motivating case. It is keyed by provider name and **dropped when re-rendering to a
different provider**, which is correct: another vendor cannot validate another vendor's
signatures.

### Normalization is lossy, and that is the trade

Going through a common format costs real fidelity:

- Thinking/reasoning blocks do not survive a provider switch.
- Provider-specific stop reasons collapse into a small shared enum.
- Cache breakpoints are re-derived per provider rather than carried across.

The alternative — storing native formats — buys that fidelity and forfeits portability,
failover, and a stable schema. For an agent whose entire premise is that the vendor is
swappable, portability wins. It is still a trade, not a free lunch.

## Capability negotiation

Providers are not feature-equivalent. Pretending otherwise is how multi-provider layers
rot: the abstraction quietly becomes "whatever the weakest backend supports", and the
strongest model gets used at a fraction of its ability.

Each adapter publishes a descriptor:

```python
@dataclass(frozen=True)
class Capabilities:
    tools: bool
    parallel_tool_calls: bool
    streaming: bool
    thinking: bool
    effort_levels: tuple[str, ...]      # () if unsupported
    prompt_caching: Literal["none", "automatic", "explicit"]
    deferred_tools: bool
    server_compaction: bool
    task_budget: bool
    vision: bool
    json_schema_output: bool
    max_context: int
```

The kernel reads it and **degrades explicitly**:

| Feature | Native support | Fallback when absent |
|---|---|---|
| Deferred tool loading | Anthropic (`defer_loading` + tool search) | Client-side tool retrieval — embed skill descriptions, inject top-K per turn ([skills.md](skills.md#keeping-the-tool-surface-bounded)) |
| Reasoning effort | Anthropic `output_config.effort`; OpenAI `reasoning.effort` | Ignored; the router picks a stronger model for demanding roles instead |
| Extended thinking | Anthropic, OpenAI reasoning models | No `thinking` events; the journal records final output only |
| Server-side compaction | Anthropic | Gerent's own summarizer over `messages` + journal |
| Task budget | Anthropic | Client-side token accounting; stop cleanly and report |
| Prompt caching | Anthropic (explicit breakpoints), OpenAI (automatic) | Render a stable prefix regardless — it helps everywhere and costs nothing |
| Parallel tool calls | Most | Serialize |

Two rules, both enforced by tests:

1. **A missing capability degrades; it never crashes.**
2. **Every degradation taken is recorded in the run report.** A cheap run that silently
   lost tool search produces mysteriously worse results; a cheap run that says "tool
   search unavailable on ollama:qwen3, used retrieval over 12 skills" is debuggable.

## Routing and failover

Roles, not hardcoded models. Callers ask for a *role*; the router resolves it.

```toml
[roles]
planner = ["anthropic:claude-opus-5", "openrouter:openai/gpt-5"]
worker  = ["anthropic:claude-sonnet-5", "openrouter:anthropic/claude-sonnet-5"]
cheap   = ["ollama:qwen3", "anthropic:claude-haiku-4-5"]
coder   = ["delegate:codex", "anthropic:claude-opus-5"]
```

| Role | Used for | Effort |
|---|---|---|
| `planner` | Decomposing goals, replanning after failure | highest available |
| `worker` | Ordinary tool-using turns | high |
| `cheap` | Classification, routing, voice acknowledgements | low |
| `coder` | Writing and editing code | highest available |

The router walks the chain on 429, 5xx, timeout, and connection errors. It does **not**
fail over on a 400 — a malformed request will be equally malformed at the next
provider, and retrying it just burns money in two places.

**Mid-conversation failover works only because history is normalized.** The next
provider re-renders the same `Msg` list. Two costs to design around:

- **Caches are provider-scoped.** Failing over forfeits cache warmth, so the fallback
  turn is more expensive than the turn that failed. Failover is for availability, not
  cost optimization.
- **Thinking blocks are dropped** in the re-render, so the fallback provider starts the
  turn without the reasoning that preceded it.

Voice has a routing consequence worth stating: a `planner`-class model on a
conversational turn blows the latency budget outright, which is why voice
acknowledgements route to `cheap`. See [voice.md](voice.md#latency-budget).

## Adapter notes

Details that are easy to get wrong and expensive to discover in production.

### Anthropic

- Model IDs are complete as written (`claude-opus-5`) — never append a date suffix.
- `thinking={"type": "adaptive", "display": "summarized"}`. The default display is
  `omitted`, which leaves thinking text empty — that reads as a long silent pause on
  the voice port and an empty reasoning trace in the journal.
- **Never send** `budget_tokens`, `temperature`, or `top_p` — all rejected with 400 on
  current models. Depth is controlled by `output_config.effort`.
- **Check `stop_reason` before reading content.** A refusal returns HTTP 200 with
  `stop_reason: "refusal"`; naive `content[0].text` access raises on it. Enable
  server-side fallback so refusals route automatically.
- Handle `pause_turn` explicitly by re-sending. Unattended, an unhandled `pause_turn`
  looks exactly like a completed turn — a truncated answer reported as success.
- Prompt caching is prefix-based: `tools` → `system` → `messages`. Keep the frozen
  system prompt and a **deterministically sorted** tool list first, breakpoint after
  them, everything volatile after. The classic silent invalidator is stamping the
  current time into the system prompt — and this agent genuinely needs current time and
  timezone, so inject them as a mid-conversation system message instead.

### OpenAI / OpenRouter

- One adapter's wire format, two configurations: OpenRouter is OpenAI-compatible with a
  different `base_url`, auth header, and a `vendor/model` id namespace.
- Tool arguments arrive as a **JSON string** and must be parsed with `json.loads` —
  never string-matched. Escaping differs between vendors and between models on the same
  vendor.
- Tool-call deltas stream in fragments and must be accumulated by index before parsing.
- OpenRouter's model roster changes without notice; a model id that worked last month
  may 404. Treat "model not found" as a config error with a clear message, not a crash.

### Ollama (local)

- No API key, no cost, full privacy — and on a CPU-only box, slow enough that it is
  realistic for `cheap` roles and little else.
- Tool-calling support varies sharply by model and is the most common failure. The
  conformance suite is what tells you whether a given local model is actually usable.

## Delegation: agent CLIs as a skill

`skills/builtin/delegate.py` exposes Codex CLI and Claude Code as subcontractors.

```
delegate(agent="codex", task="<scoped instruction>", workspace="<sandbox path>")
  → checkpoint the workspace
  → spawn the CLI, stream stdout/stderr into the journal
  → on exit, capture the diff
  → return SkillResult{ok, content=summary, artifacts=[diff]}
```

This mirrors how the agent is meant to behave generally: a competent worker hands a
specialist a scoped task and stays accountable for the result. Guardrails apply
unchanged — the delegate runs inside a sandbox root and is checkpointed first, so a
subcontractor's mistake is exactly as reversible as the agent's own.

Constraints worth knowing before relying on it: the sub-agent has its own context,
model, and cost that Gerent cannot see into; its token spend is invisible to the run
budget; and it may take a long time with little feedback. Delegate whole coherent
tasks, not fine-grained steps.

## The loop

Gerent implements its **own tool loop** rather than using any vendor's tool-runner
helper. Vendor runners keep conversation history in private memory, and Gerent's
history must live in Postgres to be resumable after a crash, auditable after the fact,
reportable to a human, and re-renderable for failover. A helper that hides the history
cannot provide any of those.

## Conformance

`tests/conformance/` is one parametrized suite that **every** adapter must pass:

- text streaming; usage reporting; error taxonomy mapping
- a single tool call; parallel tool calls; a multi-turn tool loop
- tool arguments parsed via `json.loads`, never string matching
- graceful degradation of every unsupported capability
- normalized round-trip: `Msg` → provider format → `Msg` preserves semantics

It runs against recorded cassettes in CI and against live providers on demand.

**Two providers ship in the first milestone deliberately.** An abstraction validated
against a single backend is not an abstraction — it is that backend's API with extra
indirection, and the second provider is where you find out.
