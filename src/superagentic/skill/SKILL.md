---
name: superagentic
description: Run many subagents over one list of work without them colliding or each inventing its own idea of the task. Use whenever the user asks to process, extract, audit, translate, review, or convert a set of files, pages, records, tickets or rows and it would go faster with several agents at once. Also use when they say "in parallel", "with N agents", "spawn workers", or "fan out".
---

# Running a fleet of subagents

When you spawn ten subagents on one job, two things go wrong and they have the
same cause. **A subagent you spawn has no context.** It cannot see the other
nine, and it did not read the reasoning you did before spawning it.

So they all start on the first item. And each one decides for itself what
"done" means, so you get ten standards on one corpus.

Putting the task in the spawn prompt does not fix it. That prompt is invisible
to the worker you spawn an hour later, and to the one that picks up work a
crashed worker dropped.

**superagentic is the shared list they claim from, and the brief travels with
each unit.** You define the work once; every worker gets it at claim time.

## When to use this

**Use it** when the user wants 3 or more subagents over a list of independent
units.

**Do not use it** for one agent, for two units, or when each step depends on
the last. A plain loop is clearer and the setup is not free.

## First, in any session: find out where things are

You have no memory of previous sessions. Before deciding anything, run:

```bash
superagentic state
```

It finds the database even if you do not know its name, and tells you which
runs exist, which are still going, what failed, and the single next command.
If it says there is no database here, this project has not used superagentic
and you are starting fresh.

**If a run is still going, do not start a second one over the same work.** Join
it: spawn workers against the same database and they will claim what is left.

## The whole flow

### 1. Set up the queue (you, once)

```bash
DB=$PWD/work.db

# What this run is. Everything below belongs to it.
RUN=$(superagentic start --db "$DB" --label "extract claims from tomus II")

# What the work IS. The ninth worker, spawned an hour from now, reads this.
superagentic define extract --db "$DB" \
  --instructions 'Read $path. Record every claim it makes, quoting verbatim.' \
  --done-when    'every claim in the file is recorded, or you have established there are none' \
  --returns      '{"claims": <int>, "notes": "<string>"}'

# The units. Use ABSOLUTE paths: workers may not share your directory.
ls scans/*.png | xargs -n1 basename > units.txt
superagentic add extract --db "$DB" --from-file units.txt --run "$RUN" \
  --meta "{\"path\": \"$PWD/scans/\$name\"}"
```

Notes that matter:

- **`--done-when` is the field people skip and the one that costs most.**
  Without it every worker decides for itself when to stop and they disagree.
- **`--returns` is checked.** Write it as `{"claims": <int>}` and a worker
  handing back the wrong shape is refused, keeps its lease, and can fix it.
- `$name` is the unit; `$key` is any value in that unit's `meta`.

### 2. Get the worker prompt (do not write it yourself)

```bash
superagentic prompt extract --db "$DB"
```

It is generated from the kind, so it already names the skills the work
requires and already tells the worker to stop when the queue is empty.

### 3. Spawn the workers

**Spawn them all in ONE message with multiple tool calls**, or they run one
after another and you have gained nothing.

Give each one the prompt from step 2, changing only the worker name. Do not
add the task to the prompt: that is the entire point, and a worker told the
task twice will follow the wrong copy.

Pick the worker count from how long a unit takes, not from how many units
there are. More workers than units just means idle agents. Four to eight is
usually right.

### 4. Wait, and check

```bash
superagentic wait --db "$DB" --run "$RUN" --timeout 3600
```

Exit code is the answer: `0` finished cleanly, `1` something failed, `2` timed
out. While it runs, `superagentic status --db "$DB" --who` shows who holds
what.

### 5. Collect, and verify against the database

```bash
superagentic results extract --db "$DB" --run "$RUN" --json
```

**Check the database, not the agents' final messages.** A self-report is not
evidence:

```bash
superagentic status --db "$DB" --run "$RUN"
```

### 6. If anything failed

```bash
superagentic status --db "$DB" --run "$RUN"    # the notes say why
superagentic retry  --db "$DB" --run "$RUN"    # after fixing the cause
```

`retry` resets attempts, so a unit that failed three times under the old code
gets a full three tries under the new one.

## The rules that actually bite

**Set `--lease` to several times your slowest unit**, not the average. Too long
costs one delayed retry. Too short costs duplicated work every time.

**This is at-least-once.** A slow worker and a dead worker are
indistinguishable, so a unit can be done twice. If workers write somewhere,
make the write idempotent, keyed on the unit name rather than appended.

**A worker that loses its lease is told so.** `finish` reports failure rather
than raising. It should claim a different unit, not argue.

## Failure modes and what each means

| What you see | What it means | Do |
|---|---|---|
| `claim` exits 1 | queue is empty | stop; this is normal termination |
| `not yours`, lease expired | that worker was too slow | claim another; raise `--lease` next run |
| units stuck in `leased` | workers died holding them | they return at expiry; `reclaim` forces it |
| units in `failed` | three workers gave up | read the note; usually a bad unit, not bad luck |
| result does not match `returns` | wrong shape | the unit is still yours; fix and finish again |
| `attempts > 1` on a finished unit | handed out twice | fine after a crash; a problem if widespread |

## Requiring skills and servers

If the work needs a skill or an MCP server, say so on the kind and every worker
is told before it starts:

```bash
superagentic skill xrad-extraction --db "$DB" \
  --source skills/xrad/SKILL.md --version 1.2
superagentic define extract --db "$DB" --instructions '...' --done-when '...' \
  --skill xrad-extraction --mcp 'xrad=xrad serve --db graph.db'
```

The brief then tells the worker to **fail rather than improvise** if it cannot
load one. A unit done without its tools looks finished, which is worse than one
left undone.

## Over MCP instead of the shell

If `superagentic serve` is wired into the MCP config, the same flow is tools
rather than commands. `start_run`, `register_skill`, `define_kind`, `add_jobs`,
`worker_prompt`, `job_results` for you; `claim_job`, `finish_job`,
`release_job`, `fail_job`, `heartbeat_job`, `job_status` for each worker.

```json
{"mcpServers": {"work": {"command": "superagentic",
                         "args": ["serve", "--db", "work.db"]}}}
```

## Mistakes to avoid

- **Putting the task in the spawn prompt.** Invisible to any worker spawned
  later and to the one that inherits a crashed worker's unit. This is the
  entire failure superagentic exists to prevent.
- **Spawning workers in separate messages.** They run in sequence and the
  fleet is a fleet of one.
- **Spawning before enqueueing.** They all find an empty queue, stop correctly,
  and do nothing.
- **Omitting `--done-when`.** Ten agents, ten standards.
- **Relative paths in `--meta`.** Workers may run elsewhere.
- **Trusting the agents' final reports.** Check the database.
- **Using it for sequential work.** Units must be independent. For stages, a
  finishing worker enqueues the next with `then={"audit": [...]}`.
