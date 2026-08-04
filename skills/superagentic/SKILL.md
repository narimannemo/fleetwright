---
name: superagentic
description: Run many agents over one list of work without them colliding or inventing their own idea of the task. Use when about to spawn several agents to process files, pages, records, tickets, or any list of units — define the work once, enqueue the units, spawn workers that claim rather than guess, and collect what they produced.
---

# Coordinating a fleet of agents

Spawning ten agents on one job fails twice. They all start on the first item,
and each invents its own idea of what "done" means. Both failures have the same
cause: **a freshly spawned agent has no context.** It did not read your
orchestration, it cannot see its siblings, and it will not remember any of this
next session.

So do not put the task in the prompt you spawn them with. Put it in the queue,
where every worker reads it at claim time — including the one spawned an hour
later, and the one that inherits a unit a crashed worker dropped.

## When to use this

**Use it** when you are about to spawn 3+ agents over a list of units and each
unit is independent.

**Do not use it** for one agent, for two units, or when the steps are sequential
and each depends on the last. The overhead is not worth it and a plain loop is
clearer.

## Setup, once

```bash
uv tool install superagentic     # zero dependencies, ~1s
```

## The three steps

### 1. Define what the work IS

This is the step people skip, and skipping it is what produces ten agents with
ten standards.

```bash
superagentic define extract --db work.db \
  --instructions 'Read the file at $path. Record every claim it makes, quoting verbatim.' \
  --done-when    'every claim in the file is recorded, or you have established there are none' \
  --returns      '{"claims": <int>, "notes": "<string>"}' \
  --tools        'the Read tool; the xrad MCP server for record_claim'
```

- **`--instructions`** — write for an agent with NO other context. Assume it
  has read nothing else.
- **`--done-when`** — the field that matters most and is easiest to omit.
  Without it each worker decides for itself when to stop, and they disagree.
  Both the CLI and the MCP tool warn if you leave it out.
- **`--returns`** — the shape to hand back, so results are comparable.
- `$name` is the unit; `$key` is any value in that unit's `meta`. Substitution
  tolerates JSON braces and leaves unknown `$placeholders` alone.

### 2. Enqueue the units

```bash
printf '%s\n' file-a.py file-b.py file-c.py > units.txt
superagentic add extract --from-file units.txt --db work.db \
  --meta '{"path": "/abs/path/to/$name"}'
```

Idempotent on `kind:name` — re-running after the list grows adds only what is
new. `--meta` values are themselves templated on `$name`, so one template gives
two thousand units their own path.

Use **absolute paths**. Workers may not share your working directory.

### 3. Spawn workers with a generic prompt

The prompt says nothing about the task. That is the point — copy this verbatim
and change only the worker name, the kind, and the db path:

> You are ONE WORKER IN A FLEET. Other agents are working the same queue right
> now. Your worker name is `agent-N`.
>
> You have not been told what the work is. That is deliberate — the queue will
> tell you.
>
> **STEP 1 — claim a unit:**
> `superagentic claim <KIND> --db <ABS PATH> --brief --worker agent-N --lease 600`
> If it exits non-zero and prints nothing, THE QUEUE IS EMPTY. Stop immediately
> and report. Do NOT invent work. Do NOT go looking for things to process on
> your own.
>
> **STEP 2** — the output of that command IS your assignment. It says what to
> do, what counts as done, and the exact shape to hand back. Do exactly that and
> nothing more.
>
> **STEP 3 — report the result:**
> `superagentic done <unit_id> --db <ABS PATH> --worker agent-N --result '<the JSON the brief asked for>'`
> If `done` says the lease expired, do not argue — claim a different unit.
>
> **STEP 4** — go back to STEP 1.
>
> Rules: never work on a unit you did not claim; never claim a second before
> finishing the first; stop the moment the queue is empty.
>
> Final message: how many units you completed and their names.

Launch them **in a single message with multiple tool calls** so they run
concurrently.

Pick the worker count from how long a unit takes, not from how many units there
are. More workers than units just means idle agents.

### 4. Collect what they produced

```bash
superagentic results extract --db work.db --json
superagentic status --db work.db --who
```

**Verify against the database, not against what the agents said they did.** A
self-report is not evidence:

```bash
sqlite3 work.db "SELECT count(*), sum(status='done'), sum(attempts>1) FROM unit"
```

### 5. Watch it, if the run is long

```bash
superagentic dashboard --db work.db --no-open    # then open the printed URL
```

Throughput, who is holding what, p50 vs p95, and a marked stripe on anything
held past 3x the p95. Use it to answer *is anyone stuck* — which `status`
cannot tell you, because a healthy fleet and a stalled one show the same
`leased` count.

## Rules that matter

**Set `--lease` to several times your slowest unit**, not the average. Too long
costs one delayed retry; too short costs duplicated work every time.

**This is at-least-once.** A slow worker and a dead worker are
indistinguishable — no timeout separates them — so a unit can be done twice.
If workers write somewhere, make the write idempotent (keyed on the unit name,
not appended).

**Long units must heartbeat**, or another worker will start the same one:
`superagentic done` is not the only call — use `heartbeat_job` over MCP, or
re-claim with a longer lease.

## Failure modes, and what each means

| What you see | What it means | Do |
|---|---|---|
| `claim` exits 1, no output | queue is empty | stop; this is normal termination |
| `not yours — <id>'s lease expired` | this worker was too slow | claim a different unit; raise `--lease` next run |
| units stuck in `leased` | workers died holding them | they return automatically at expiry; `superagentic reclaim` forces it |
| units in `failed` | 3 workers could not finish it | read the note; it is usually a bad unit, not bad luck |
| `attempts > 1` on a finished unit | it was handed out twice | expected after a crash or a `release`; only a problem if widespread |

## From an agent, over MCP

If `superagentic serve` is wired into the MCP config, the same flow is nine
tools instead of shell commands:

```json
{"mcpServers": {"work": {"command": "superagentic",
                         "args": ["serve", "--db", "work.db"]}}}
```

**Orchestrator:** `define_kind`, `add_jobs`, `job_results`.
**Worker:** `claim_job`, `finish_job`, `release_job`, `fail_job`,
`heartbeat_job`, `job_status`.

`add_jobs` refuses a kind nobody defined, and says which call to make.

## Mistakes to avoid

- **Putting the task in the spawn prompt.** It is invisible to any worker
  spawned later and to the one that inherits a crashed worker's unit. That is
  the entire failure this exists to prevent.
- **Omitting `--done-when`.** Ten agents, ten standards.
- **Relative paths in `--meta`.** Workers may run elsewhere.
- **Trusting the agents' final reports.** Check the database.
- **Spawning workers before enqueueing.** They will all find an empty queue and
  stop, correctly, having done nothing.
- **Using it for sequential work.** Units must be independent. For stages, let
  a finishing worker enqueue the next one with `then={"audit": [...]}`.
