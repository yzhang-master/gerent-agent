# Architecture decision records

Short records of the choices that were genuinely contested. Each states what was
decided, what it costs, and what was rejected.

Read these when a design in `docs/` looks arbitrary. It usually isn't, and the ADR says
what the alternative was.

| # | Decision | Status |
|---|---|---|
| [0001](0001-normalized-message-format.md) | Normalized message format, not vendor-native | Accepted |
| [0002](0002-no-human-approval.md) | No human approval; reversibility instead | Accepted |
| [0003](0003-agent-clis-as-skills.md) | Agent CLIs plug in as skills, not providers | Accepted |
| [0004](0004-durable-plans.md) | Plans are durable DAGs in Postgres | Accepted |
| [0005](0005-own-tool-loop.md) | Own tool loop, not a vendor tool runner | Accepted |
| [0006](0006-cloud-default-voice.md) | Cloud speech by default, local as fallback | Accepted |
| [0007](0007-custom-scheduler.md) | Custom scheduler over a scheduling library | Accepted |
