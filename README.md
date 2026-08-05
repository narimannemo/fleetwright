<img src="assets/superagentic.svg" alt="SuperAgentic" width="420">

[![ci](https://github.com/narimannemo/superagentic/actions/workflows/ci.yml/badge.svg)](https://github.com/narimannemo/superagentic/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/superagentic)](https://pypi.org/project/superagentic/)
[![python](https://img.shields.io/pypi/pyversions/superagentic)](https://pypi.org/project/superagentic/)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](pyproject.toml)

**Spawn ten agents on one job and they all start on page one. Then each one
invents its own idea of what "done" means.**

Both failures have the same cause. A freshly spawned agent has no context: it
did not read your orchestration code, it cannot see the other nine, and it will
not remember any of this next session. So it needs two things it can only get
by asking.

1. **Which unit is mine?** Nobody else is on it, and if I die it comes back.
2. **What am I supposed to do with it?** The task, what finished looks like,
   what to hand back, and which skills I need before I start.

SuperAgentic is where both of those live. The orchestrator defines the work and
enqueues the units. Every worker claims one and is handed the assignment with
it. Nothing collides, nothing guesses, and afterwards you can see what actually
happened.

A library, a CLI, an MCP server and a dashboard, in one SQLite file, with **no
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

# One execution of a fleet. Everything below belongs to it, so afterwards you
# can ask what THIS run did rather than what the database happens to contain.
run = sa.start_run(conn, label="Tomus II extraction")

# What a skill name means, once. A readable file is hashed, so units claimed
# before and after an edit stay tellable apart.
sa.register_skill(conn, "xrad-extraction",
                  source="skills/xrad/SKILL.md", version="1.2")

# What the work IS. This is the part a spawn prompt cannot do: the ninth
# worker, started an hour from now, reads exactly the same thing.
sa.define(conn, "extract",
    instructions="Read $path. Record every claim it makes, quoting verbatim.",
    done_when="every claim on the page is recorded, or you have established "
              "there are none",
    returns='{"claims": <int>, "notes": "<string>"}',
    skills=["xrad-extraction"],
    mcp={"xrad": "xrad serve --db graph.db"})

sa.add(conn, "extract", pages, run=run, meta={"path": "scans/$name.png"})
```

Now spawn ten agents whose entire prompt is *claim work and do what it says*.
Each one is handed this:

```
UNIT: p0189   (kind: extract, id: extract:p0189)

WHAT TO DO
Read scans/p0189.png. Record every claim it makes, quoting verbatim.

YOU MUST HAVE
skills:
  - xrad-extraction v1.2 [99cdba1cede56a04]
      from: skills/xrad/SKILL.md
MCP servers: xrad (xrad serve --db graph.db)
If any of these is unavailable, call fail with that as the reason.
Do not improvise a substitute.

DONE WHEN
every claim on the page is recorded, or you have established there are none

HAND BACK
{"claims": <int>, "notes": "<string>"}

Call finish (unit_id=extract:p0189) when done, or fail with a reason.
Do not start any other unit.
```

You do not write that prompt. `superagentic prompt extract -n 10` generates it
from the kind, so the template cannot drift from the work it describes.

## In sixty seconds

```bash
uvx superagentic demo
```

```
-- 3. three workers claim, and never collide -----------------
   worker-a: page-1, page-2
   worker-b: page-3, page-4
   worker-c: page-5, page-6
   6 units handed out, 6 distinct -- nobody got the same page

-- 4. two finish. the third crashes, holding its work --------
   worker-c: [process dies without reporting anything]

-- 5. its lease expires, and the work comes back -------------
   ...one second later, after the lease expired:
     worker-d picked up page-5  (attempt 2)
   No daemon ran. reclaim() happens on the way into claim().

-- 6. and the dead worker cannot close what it lost ----------
   worker-c calls finish on page-5: False
   worker-d calls finish on page-5: True
```

## A lease, not a lock

This is the only genuinely hard part of the problem, and every other decision
follows from it.

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
**heartbeat** while you work, so only genuinely stalled units are reclaimed,
and make the write at the end **idempotent**, so a unit done twice converges.

When a lease is lost, `finish` returns `False` rather than raising. Handle it.
That worker no longer owns the unit and should claim a different one.

## Watching it run

```bash
superagentic dashboard --db work.db          # http://127.0.0.1:8787
superagentic dashboard --out fleet.html      # a static snapshot
```

`14 left` is the same number whether five workers are moving through the queue
steadily or three have died and one is stuck on a page it will never finish.
Every panel exists to separate those two situations:

| Panel | The question it answers |
|---|---|
| Runs | what has this fleet ever done, and how much parallelism did it really get |
| Throughput | is it still moving |
| In flight | **is anyone stuck**, since anything held past 3x the p95 is marked |
| Jobs | what happened to this one unit |
| Workers and models | did one model do these faster, or worse |
| Skills in use | which version of which skill, and what nobody registered |
| Could not finish | what needs a human |

Served from `http.server` with the CSS and JS inline and the SVG drawn by hand.
No framework, no build step, nothing fetched. It is read only, so pointing it
at a live run cannot disturb the run, and a test enforces that. There is an
optional access token, and because there is no TLS the server **refuses to bind
off loopback unless one is set**.

## From the shell

`claim` exits 1 with no output when the queue is dry, so a loop ends by itself.
Eight workers, no coordinator:

```bash
superagentic add extract --from-file pages.txt --run "$RUN"

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

A script driving a fleet does not need to parse any of that. `wait` blocks
until the work is over and the **exit code is the interface**: `0` finished
cleanly, `1` something failed, `2` timed out.

```bash
RUN=$(superagentic start --label "nightly extraction")
superagentic add extract --from-file pages.txt --run "$RUN"
./spawn-my-workers.sh &

if superagentic wait --run "$RUN" --timeout 7200; then
  ./collect-results.sh
else
  superagentic status --run "$RUN"        # something failed, or it stalled
  superagentic retry --run "$RUN"         # fix the bug, re-run only what broke
fi
```

## From an agent

Fourteen MCP tools, split by who uses them.

**The orchestrator** uses `start_run`, `register_skill`, `define_kind`,
`add_jobs`, `worker_prompt`, `job_results`, `list_runs` and `list_skills`. It
can stand up an entire fleet without touching a shell.

**Each worker** uses `claim_job`, `finish_job`, `release_job`, `fail_job`,
`heartbeat_job` and `job_status`.

```json
{"mcpServers": {"work": {"command": "superagentic",
                         "args": ["serve", "--db", "work.db"]}}}
```

The tool descriptions carry the protocol, because that is all a worker reads:
claim before starting, do what the brief says rather than what you assume, and
**stop when the queue is empty rather than invent work**. That last one is the
failure mode worth designing against, since an agent with nothing to do will
reliably find something, and what it finds is usually a unit somebody else has.

## Documentation

| | |
|---|---|
| [Concepts](docs/concepts.md) | leases, capabilities, skills, and what is deliberately refused |
| [MCP](docs/mcp.md) | wiring it to Claude Code, Cursor, or your own agent |
| [Dashboard](docs/dashboard.md) | what each panel answers, and why percentiles not averages |
| [Reference](docs/reference.md) | every command and the Python API |
| [Skill](skills/README.md) | a drop in Claude Code skill, so an agent knows how to run a fleet |
| [Packaging](packaging/README.md) | uv, Homebrew, pip, and which to use |
| [Licensing](LICENSING.md) | Apache-2.0 everywhere except `ee/`, and where the line is |

## What it is not

**Not a scheduler.** No dependencies between units, no backoff, no cron, one
integer of priority. If you need those, run a real queue and keep this for the
hand out.

**Not a broker.** One SQLite file on one filesystem. Many processes, one box.
SQLite over NFS is not safe and this does not pretend otherwise.

**Not exactly once.** See above. Nothing is.

**It does not spawn agents.** `superagentic prompt` generates the prompt to
spawn them with. Running it belongs to your runtime, because the moment this
package spawns anything it needs credentials and an opinion about which agent
framework you use.

**It does not fetch or install skills**, and it cannot verify that a worker
loaded one, any more than it can verify the model a worker says it is. Both are
declared. The brief states the requirement and says to fail rather than
substitute.

## Licence

**Apache-2.0**, including the right to run it, modify it, and sell a service
built on it. The `ee/` directory is the one exception and is currently empty.
[LICENSING.md](LICENSING.md) draws the line and argues for it.

Contributions welcome under the DCO, with no CLA.
[CONTRIBUTING.md](CONTRIBUTING.md) says what will and will not be accepted
before you spend an evening.
