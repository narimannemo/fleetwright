# superagentic

[![ci](https://github.com/narimannemo/superagentic/actions/workflows/ci.yml/badge.svg)](https://github.com/narimannemo/superagentic/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/superagentic)](https://pypi.org/project/superagentic/)
[![python](https://img.shields.io/pypi/pyversions/superagentic)](https://pypi.org/project/superagentic/)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)

**Spawn ten agents on one job and they all start on page one — then each
invents its own idea of what "done" means.**

Two problems, and they have the same cause: a freshly spawned agent has no
context. It did not read your orchestration code, it cannot see the other nine,
and it will not remember any of this next session. So it needs two things it
can only get by asking:

- **which unit is mine** — nobody else is on it, and if I die it comes back;
- **what am I supposed to do with it** — the task, what finished looks like,
  what to hand back.

`superagentic` is where both live. The orchestrator *defines the work* and
*enqueues the units*; every worker *claims one and is handed the assignment
with it*. A library, a CLI and an MCP server, in one SQLite file, with **no
dependencies at all**.

```bash
uv tool install superagentic                      # the CLI, on your PATH
uvx superagentic demo                             # or run it without installing
uv pip install superagentic                       # or as a library
brew install narimannemo/tap/superagentic         # or via Homebrew
```

## The shape of it

```python
import superagentic as sa
conn = sa.connect("work.db")

# The orchestrator, once. This is the part a prompt cannot do:
# the ninth worker, spawned an hour from now, reads the same thing.
sa.define(conn, "extract",
    instructions="Read $path. Record every claim it makes, quoting verbatim.",
    done_when="every claim on the page is recorded, or you have established "
              "there are none",
    returns='{"claims": <int>, "notes": "<string>"}',
    tools="the `xrad` MCP server: record_claim, check_quote")

sa.add(conn, "extract", pages, meta={"path": "scans/$name.png"})
```

Then spawn ten agents with one instruction — *claim work and do it* — and each
of them is handed this:

```
UNIT: p0189   (kind: extract, id: extract:p0189)

WHAT TO DO
Read scans/p0189.png. Record every claim it makes, quoting verbatim.

USE
the `xrad` MCP server: record_claim, check_quote

DONE WHEN
every claim on the page is recorded, or you have established there are none

HAND BACK
{"claims": <int>, "notes": "<string>"}

Call finish (unit_id=extract:p0189) when done, or fail with a reason.
Do not start any other unit.
```

When they are finished, `sa.results(conn, "extract")` is what they produced.

## In sixty seconds

```bash
uvx superagentic demo
```

```
-- 2. three workers claim, and never collide ---------------
   worker-a: page-1, page-2
   worker-b: page-3, page-4
   worker-c: page-5, page-6
   6 units handed out, 6 distinct -- nobody got the same page

-- 3. two finish. the third crashes, holding its work ------
   worker-c: [process dies without reporting anything]

-- 4. its lease expires, and the work comes back -----------
   another worker asks immediately:
     nothing -- still leased
   ...one second later, after the lease expired:
     worker-d picked up page-5  (attempt 2)
   No daemon ran. reclaim() happens on the way into claim().

-- 5. and the dead worker cannot close what it lost --------
   worker-c calls finish on page-5: False
   worker-d calls finish on page-5: True
```

## A lease, not a lock

This is the only hard part of the problem, and every other decision follows
from it.

**A lock held by a crashed worker is worse than no lock at all.** The unit is
neither being worked nor available, and nothing in the system can tell a busy
worker from a dead one. A lease makes that distinction the passage of time:
renew it and you keep the unit, stop renewing and it returns to the pool.

There is no daemon and no cron. `reclaim()` runs at the top of every `claim()`,
so the next worker asking for work does the cleanup on its way in.

## At-least-once, and nothing can do better

Said here rather than in a footnote, because the alternative is you finding out
in production:

> A worker that is **slow** rather than **dead** will have its lease expire,
> another worker will take the unit, and both will finish it.

No timeout distinguishes those two cases. Two defences, and you want both:
**heartbeat** while you work, so only genuinely stalled units are reclaimed;
and make the write at the end **idempotent**, so a unit done twice converges.

When a lease is lost, `finish` returns `False` rather than raising. Handle it —
this worker no longer owns the unit and should claim a different one.

## The worker loop

```python
while units := sa.claim(conn, "translate", lease=1800):
    for u in units:
        try:
            out = do_the_work(u.name, u.instructions)
            sa.finish(conn, u.unit_id, result=out)
        except Exception as e:
            sa.fail(conn, u.unit_id, note=str(e))
```

Stages compose without this becoming a scheduler — a finishing worker hands the
next stage its units:

```python
sa.finish(conn, u.unit_id, result={"claims": 12},
          then={"audit": [f"claim-{i}" for i in ids]})
```

Nothing is enqueued if the close failed, so a worker that lost its lease cannot
inject work off the back of a unit it no longer owns.

## From the shell

`claim` exits 1 with no output when the queue is dry, so a loop ends by itself.
Eight workers, no coordinator:

```bash
superagentic add extract --from-file pages.txt

for i in $(seq 1 8); do
  ( while unit=$(superagentic claim extract --json --lease 1800); do
      id=$(echo "$unit" | jq -r '.[0].unit_id')
      name=$(echo "$unit" | jq -r '.[0].name')
      if my-extractor "$name"; then
        superagentic done "$id"
      else
        superagentic fail "$id" --note "extractor exited $?"
      fi
    done ) &
done
wait
superagentic status --who
```

## From an agent

Nine MCP tools, split by who uses them.

**The orchestrator** — the agent that spawns the fleet — uses `define_kind`,
`add_jobs` and `job_results`. It can set up an entire fleet without touching a
shell.

**Each worker** uses `claim_job`, `finish_job`, `release_job`, `fail_job`,
`heartbeat_job` and `job_status`.

The tool descriptions carry the protocol, because that is all a worker reads:
claim before starting, **do what the unit's `brief` says rather than what you
assume the task is**, and **stop when the queue is empty rather than invent
work** — which is the failure mode worth designing against, since an agent with
nothing to do will reliably find something, and what it finds is usually a unit
somebody else has.

```json
{"mcpServers": {"work": {"command": "superagentic",
                         "args": ["serve", "--db", "work.db"]}}}
```

See [MCP](docs/mcp.md).

## Watching it run

```bash
superagentic dashboard --db work.db          # http://127.0.0.1:8787
superagentic dashboard --out fleet.html      # a static snapshot
```

`14 left` is the same number whether four workers are moving through it steadily
or three have died and one is stuck on a poison unit. The dashboard is the
difference: throughput over time, **what every worker is holding right now and
for how long**, duration p50 against p95, and a stripe on any unit held past
three times the p95 — because "is anyone stuck?" is the question, and a raw
duration column does not answer it.

Served from `http.server`, CSS and JS inline, SVG drawn by hand. No framework,
no build step, nothing fetched. Read-only, so pointing it at a live run cannot
disturb the run, and bound to loopback because it has no authentication.

## Documentation

| | |
|---|---|
| [Concepts](docs/concepts.md) | leases, attempts, and what this deliberately is not |
| [MCP](docs/mcp.md) | wiring it to Claude Code, Cursor, or your own agent |
| [Dashboard](docs/dashboard.md) | what each panel answers, and why percentiles not averages |
| [Reference](docs/reference.md) | every command and the Python API |
| [Packaging](packaging/README.md) | uv, Homebrew, pip — and which to use |
| [Skill](skills/README.md) | drop-in Claude Code skill, so an agent knows how to run a fleet |

## What it is not

**Not a scheduler.** No dependencies between units, no backoff, no cron, one
integer of priority. If you need those, run a real queue and keep this for the
hand-out.

**Not a broker.** One SQLite file on one filesystem. Many processes, one box.
SQLite over NFS is not safe and this does not pretend otherwise.

**Not exactly-once.** See above. Nothing is.

**It does not do your work or check it.** It hands out units and carries your
instructions verbatim. Whether the agent followed them is between you and the
agent.

Apache-2.0. Contributions welcome — [CONTRIBUTING.md](CONTRIBUTING.md) says
what will and will not be accepted before you spend an evening.
