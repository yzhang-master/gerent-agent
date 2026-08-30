# Skills

A skill is a capability the agent can invoke. Skills are how Gerent is *domain-wide*:
the core knows nothing about legal research or home automation, and gains both by
having their packs dropped into `domains/`.

## The contract

```python
class Skill(ABC):
    name: str                    # unique, snake_case
    description: str             # the model reads this — it is a prompt, not a docstring
    domain: str                  # "core", "legal", "home", ...
    input_schema: dict           # JSON Schema, additionalProperties: False
    risk: Risk                   # READ | WRITE | DESTRUCTIVE | EXTERNAL
    reversible: bool = True      # False ⇒ a checkpoint is mandatory before running
    defer_loading: bool = True   # keeps the tool surface bounded
    cost_hint: Cost = Cost.LOW   # rough expense, for budget accounting

    @abstractmethod
    async def run(self, ctx: SkillContext, **kwargs) -> SkillResult: ...
```

```python
@dataclass
class SkillResult:
    ok: bool
    content: str                 # what the model sees
    artifacts: list[Artifact]    # files, diffs, audio — referenced, not inlined
    error: str | None = None
```

**`description` is a prompt.** It is the only thing the model uses to decide whether
this skill is the right one. "Runs a shell command" is worse than "Runs a shell command
in the workspace and returns stdout, stderr, and exit code. Use for builds, tests, and
git. Not for editing files — use `write_file`." Write it for a competent colleague who
has never seen the codebase.

**Failure is a result, not an exception.** A skill that cannot do its job returns
`ok=False` with a useful `error`. Raising kills the turn; returning lets the agent read
what went wrong and try something else — which is the entire point of an autonomous
worker.

## `SkillContext`

What a skill is given. Note what is absent: the kernel.

```python
@dataclass
class SkillContext:
    session_id: UUID
    actor: Actor
    workspace: Path              # already validated against sandbox roots
    journal: Journal             # for recording what happened
    budget: BudgetView           # read-only remaining spend
    locale: str
    emit: Callable[[TurnEvent], Awaitable[None]]   # progress events
```

Skills must not import `gerent.core.kernel`. A skill that reaches back into the loop
that called it cannot be tested, reused, or run in a subprocess later.

## Registry and discovery

`skills/registry.py` discovers skills from three sources at startup:

1. `gerent/skills/builtin/` — always present
2. `domains/*/` — local drop-in packs
3. installed packages exposing a `gerent.skills` entry point

A pack is a directory with a manifest and a module exporting `Skill` subclasses:

```
domains/legal/
  gerent_pack.toml     # name, version, requires, enabled
  skills.py
  README.md
```

Discovery is strict about a few things, because a broken pack must not take the agent
down: duplicate `name` is a startup error; a pack that fails to import is logged,
skipped, and reported as unavailable; `input_schema` is validated at registration, not
at first call.

**Adding a domain requires no core change.** That is the property being protected here,
and it is worth failing a code review over.

## Tool rendering

Tool JSON is *generated* from the class and rendered per provider. Authored once,
never maintained in two places:

```
Skill  ──►  anthropic: {name, description, input_schema}
       └─►  openai:    {type: "function", function: {name, description, parameters}}
```

Keep schemas inside the JSON Schema subset every provider accepts: objects with typed
properties, `required`, `enum`, `additionalProperties: false`, and shallow nesting.
Avoid `oneOf`/`anyOf`/`allOf`, `$ref`, and deep nesting. Exotic schema constructs are
the single most common cause of cross-provider tool-calling failures, and they fail by
producing subtly wrong arguments rather than by erroring.

Set `additionalProperties: false` and mark every non-optional field `required`. Where a
provider supports strict validation, enable it — it turns a class of silent
wrong-argument bugs into loud ones.

## Keeping the tool surface bounded

This is the scaling problem that "domain-wide" creates. Twenty domains at fifteen
skills each is 300 tool schemas; sending them on every request would cost more tokens
than the conversation and measurably degrade selection quality.

**Resident set.** A small always-loaded core: `read_file`, `write_file`, `shell`,
`remember`, `recall`, `schedule`. These are needed on nearly every turn.

**Everything else is deferred**, and reached one of two ways depending on the provider:

- *Native* (`capabilities.deferred_tools`): mark packs `defer_loading` and let the
  provider's tool-search surface them on demand. One constraint — the search tool
  itself must never be deferred, and at least one tool must remain resident, or the
  request is rejected.
- *Fallback* (`skills/retrieval.py`): embed skill names and descriptions, retrieve
  top-K against the turn's intent, inject only those. Where pgvector is unavailable
  this degrades again to Postgres full-text search, which is adequate — skill
  descriptions are short and keyword-dense.

Either way the per-request surface stays roughly constant as installed domains grow.
Retrieval has a real failure mode worth naming: a skill whose description does not
match how the user phrases the task is invisible, and looks to the user like the agent
lacking a capability it actually has. Descriptions are the fix.

## Built-in skills

| Skill | Risk | Reversible | Notes |
|---|---|---|---|
| `read_file` | READ | — | Sandbox-checked after symlink resolution |
| `write_file` | WRITE | yes | Checkpointed; diff captured for the report |
| `edit_file` | WRITE | yes | Exact-match replacement; fails loudly on ambiguity |
| `shell` | DESTRUCTIVE | no | Sandboxed cwd, timeout, output cap, hard-deny list |
| `web_search` / `web_fetch` | EXTERNAL | — | Provider-native where available |
| `remember` / `recall` | WRITE / READ | yes | See [memory.md](memory.md) |
| `schedule` | WRITE | yes | The agent schedules its own follow-ups |
| `delegate` | DESTRUCTIVE | no | Hands a task to Codex / Claude Code — see [providers.md](providers.md#delegation-agent-clis-as-a-skill) |

`shell` is marked `DESTRUCTIVE`/`reversible=False` because its effects cannot be
inferred from its arguments. A checkpoint is therefore taken before every invocation.
That is deliberately conservative, and it is what makes an unattended agent with shell
access defensible — see [autonomy.md](autonomy.md).

## Writing a pack

```python
class SearchCaseLaw(Skill):
    name = "search_case_law"
    domain = "legal"
    description = (
        "Search case law by query, jurisdiction, and date range. Returns citations "
        "with summaries. Use for finding precedent; use `fetch_case` for full text."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "jurisdiction": {"type": "string", "enum": ["us-federal", "us-state", "uk"]},
            "since": {"type": "string", "description": "ISO date"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    risk = Risk.EXTERNAL

    async def run(self, ctx, query, jurisdiction=None, since=None) -> SkillResult:
        ...
```

A checklist for review:

- Does `description` say when *not* to use it, and which skill to use instead?
- Does the schema stay in the portable subset, with `additionalProperties: false`?
- Does it return `ok=False` on failure rather than raising?
- Is `risk` honest? `reversible=False` for anything whose effect cannot be read off its
  arguments.
- Does it emit progress events for anything slower than a second or two?
