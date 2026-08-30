# Configuration

One file, `gerent.toml`, resolved in order: `./gerent.toml` →
`~/.config/gerent/gerent.toml` → built-in defaults. Environment variables override any
key as `GERENT__SECTION__KEY`.

**Secrets never live in this file.** API keys are read from the environment or from an
`ant auth login`-style credential store. A config file is something people paste into
issues.

## Full example

```toml
[agent]
name          = "gerent"
default_locale = "en-US"
timezone      = "Europe/Warsaw"        # IANA name, never an offset

# ── Providers ────────────────────────────────────────────────────────────────
[providers.anthropic]
enabled = true
# key from ANTHROPIC_API_KEY, or an `ant auth login` profile

[providers.openrouter]
enabled  = true
base_url = "https://openrouter.ai/api/v1"
# key from OPENROUTER_API_KEY

[providers.openai]
enabled = false

[providers.ollama]
enabled  = true
base_url = "http://localhost:11434"

# ── Roles ────────────────────────────────────────────────────────────────────
# Ordered failover chains. The router walks the chain on 429/5xx/timeout.
[roles]
planner = ["anthropic:claude-opus-5", "openrouter:openai/gpt-5"]
worker  = ["anthropic:claude-sonnet-5"]
cheap   = ["ollama:qwen3", "anthropic:claude-haiku-4-5"]
coder   = ["delegate:codex", "anthropic:claude-opus-5"]

[roles.effort]
planner = "xhigh"
worker  = "high"
cheap   = "low"
coder   = "xhigh"

# ── Guardrails ───────────────────────────────────────────────────────────────
[guardrails]
workspace_roots   = ["/home/pus/Documents/gerent-agent", "/home/pus/projects"]
allow_credentials = []                  # explicit allowlist; empty = deny all
checkpoint_dir    = "~/.gerent/checkpoints"
kill_switch_file  = "~/.gerent/STOP"

# ── Budgets ──────────────────────────────────────────────────────────────────
[budgets.interactive]
max_wall_clock = "30m"
max_tokens     = 2_000_000
max_cost_usd   = 5.00
max_tool_calls = 200

[budgets.scheduled]                     # tighter: nobody is watching
max_wall_clock = "10m"
max_tokens     = 500_000
max_cost_usd   = 1.00
max_tool_calls = 60

# ── Planning ─────────────────────────────────────────────────────────────────
[planning]
max_replans            = 3
max_step_attempts      = 2
max_parallel_steps     = 4              # filesystem steps serialize regardless

# ── Memory ───────────────────────────────────────────────────────────────────
[memory]
backend          = "postgres"
retrieval_top_k  = 8
auto_extract     = true                 # propose durable facts after a run

# ── Skills ───────────────────────────────────────────────────────────────────
[skills]
domains_dir     = "./domains"
disabled        = []
resident        = ["read_file", "write_file", "shell", "remember", "recall", "schedule"]
retrieval_top_k = 12                    # when deferred loading is unavailable

# ── Voice ────────────────────────────────────────────────────────────────────
[voice]
enabled = false
stt     = "deepgram"                    # deepgram | whisper_local
tts     = "cartesia"                    # cartesia  | piper_local
vad     = "silero"
barge_in = true
input_device  = "default"
output_device = "default"

# ── Scheduler ────────────────────────────────────────────────────────────────
[scheduler]
enabled          = true
poll_interval    = "30s"
report_sink      = "file:~/.gerent/reports/"   # file: | webhook: | email:
max_retry_depth  = 3                    # bounds agent self-rescheduling

# ── Database ─────────────────────────────────────────────────────────────────
[db]
dsn         = "postgresql://localhost/gerent"
pool_min    = 2
pool_max    = 10

# ── Logging ──────────────────────────────────────────────────────────────────
[logging]
level  = "info"
format = "console"                      # console | json
```

## Keys worth thinking about

**`roles`** — the main lever on cost and quality. Chains are failover, not
load-balancing; the router only advances on availability errors, never on a 400. See
[providers.md](providers.md#routing-and-failover).

**`guardrails.workspace_roots`** — everything the agent may touch. This is the primary
containment boundary, and it should be as narrow as the work allows.

**`budgets.scheduled`** — deliberately tighter than interactive. An unattended run that
spends five dollars at 03:00 is discovered at the end of the month.

**`scheduler.report_sink`** — where unattended reports go. Set it. A report that lands
in a logfile nobody opens is indistinguishable from a run that never happened.

**`voice.enabled = false`** by default — the audio stack pulls optional dependencies
and grabs a microphone; that should be opt-in.

## Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic; optional if an `ant auth login` profile exists |
| `OPENROUTER_API_KEY` | OpenRouter |
| `OPENAI_API_KEY` | OpenAI |
| `DEEPGRAM_API_KEY` | Cloud STT |
| `CARTESIA_API_KEY` | Cloud TTS |
| `GERENT_CONFIG` | Explicit config path |
| `GERENT__<SECTION>__<KEY>` | Override any config key |

## Validation

Config is parsed into a Pydantic model at startup and **fails loudly**:

- a role naming a disabled or unknown provider
- a `workspace_root` that does not exist
- a `timezone` that is not a valid IANA name
- an enabled provider whose SDK is not installed
- a `dsn` that cannot be reached

Every one of these is a misconfiguration that would otherwise surface hours later as
strange behaviour, usually in an unattended run.
