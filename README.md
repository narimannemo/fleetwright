<img src="assets/fleetwright.svg" alt="fleetwright" width="420">

[![ci](https://github.com/narimannemo/fleetwright/actions/workflows/ci.yml/badge.svg)](https://github.com/narimannemo/fleetwright/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/fleetwright)](https://pypi.org/project/fleetwright/)
[![python](https://img.shields.io/pypi/pyversions/fleetwright)](https://pypi.org/project/fleetwright/)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](pyproject.toml)

**You ask Claude to extract every claim from 400 scanned pages. It spawns
eight subagents. All eight start on page one.**

And each one decides for itself what "done" means, so you get eight standards
on one corpus.

Both failures have the same cause. A subagent has no context: it cannot see the
other seven, and it did not read the reasoning you did before spawning it. So
it needs two things it can only get by asking.

1. **Which page is mine?** Nobody else is on it, and if I die it comes back.
2. **What am I supposed to do with it?** The task, what finished looks like,
   what to hand back, and which skills I need first.

fleetwright is where both live. You define the work once and enqueue the
units; every worker claims one and is handed the assignment with it. Nothing
collides, nothing guesses, and afterwards you can see what actually happened.

A library, a CLI, an MCP server and a dashboard, in one SQLite file, with **no
dependencies at all**.

## Start here

```bash
uv tool install fleetwright
fleetwright install-skill
```

That writes a skill into `.claude/skills/`. Now ask Claude in English:

> extract every claim from the 400 files in `scans/`, using 8 agents

Claude reads the skill and does the rest: defines the work, enqueues the units,
spawns the workers **in one message so they run at once**, waits for them, and
collects the results. You watch it with:

```bash
fleetwright status --who      # who is holding what, right now
fleetwright dashboard         # or the whole picture in a browser
```

If you would rather drive it yourself, everything below is what the skill is
doing on your behalf. And if you run the same work often, put it in a file
instead of a shell history:

```bash
fleetwright init      # writes a commented fleetwright.toml
fleetwright apply     # registers the skills, defines the kinds, enqueues
```

## The shape of it

```python
import fleetwright as sa
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

You do not write that prompt. `fleetwright prompt extract -n 10` generates it
from the kind, so the template cannot drift from the work it describes.

## In sixty seconds

```bash
uvx fleetwright demo
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
follows from it. **It is also not new.** Leases are Gray and Cheriton, 1989;
SQS shipped visibility timeouts in 2006 and Beanstalkd its TTR in 2007; and
[litequeue](https://github.com/litements/litequeue) already does expiring
claims on SQLite, with `retry_expired()` and a claim id that makes `done()`
return false for a stale holder. If you want a small SQLite queue, use it.

What is here and not there is everything above the queue: a **kind** that says
what the work is so the ninth worker gets the same brief as the first, skills
and definitions **hashed and pinned per unit** so you can tell what any unit
was actually told, **runs** to scope a corpus, and a dashboard. The queue is
the boring part, and it should be.

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
and make the write at the end **unit-scoped**, keyed on the unit name, so a
duplicate overwrites rather than appends.

That is deliberately weaker than "idempotent, so a unit done twice converges",
which is what this used to say and is not true of the example on this page. Two
model runs over *record every claim it makes, quoting verbatim* produce two
different, both-plausible extractions. Overwriting keyed on the unit means you
get one of them rather than both concatenated; it does not mean you get the
same one. For deterministic work convergence is real. For generative work,
plan on the two outputs differing, and if which one you keep matters, key on
`(unit_id, attempt)` and choose.

When a lease is lost, `finish` returns `False` rather than raising. Handle it.
That worker no longer owns the unit and should claim a different one.

## Does it hold up under contention

```bash
python bench/contention.py 64 5000
```

Real processes, not threads: the whole question is what SQLite does when N
operating-system processes contend for one write lock on one file, and threads
in one interpreter would measure almost nothing.

On an 8-core M-series Mac, SQLite 3.50.4:

| Workers | Units | Finished | Duplicates | `SQLITE_BUSY` | Throughput | p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| 32 | 2,000 | 2,000 | **0** | **0** | 1,513/s | 0.3 ms | 11 ms | 165 ms |
| 64 | 5,000 | 5,000 | **0** | **0** | 1,419/s | 0.7 ms | 48 ms | 667 ms |

The two zeroes are the point. Duplicates would falsify the single atomic
`UPDATE` that the whole safety argument rests on, and a `SQLITE_BUSY` reaching
a caller would mean `busy_timeout` had failed to turn contention into waiting.

**Read the tail, not the median.** At 64 workers the p99 claim takes two thirds
of a second, and it gets worse the more workers you add, because they are
queueing for one write lock. It does not matter here: a unit is an agent doing
work for tens of seconds, so even 667 ms is under 3% of it. It would matter a
great deal if your units were milliseconds long, and if they are, this is the
wrong tool.

## Watching it run

```bash
fleetwright dashboard --db work.db          # http://127.0.0.1:8787
fleetwright dashboard --out fleet.html      # a static snapshot
```

![The dashboard: a five-stage run, five workers, and the pipeline drawn as
nodes and edges](docs/img/dashboard.png)

`14 left` is the same number whether five workers are moving through the queue
steadily or three have died and one is stuck on a page it will never finish.
Every panel exists to separate those two situations:

| Panel | The question it answers |
|---|---|
| Runs | what has this fleet ever done, and how much parallelism did it really get |
| Throughput | is it still moving |
| In flight | **is anyone stuck**, since anything held past 3x the p95 is marked |
| Jobs | what happened to this one unit |
| Workers and models | did one model do these faster, cheaper, or worse |
| Skills in use | which version of which skill, and what nobody registered |
| Could not finish | what needs a human |

Served from `http.server` with the CSS and JS inline and the SVG drawn by hand.
No framework, no build step, nothing fetched. It opens the database read only
(`mode=ro`, so SQLite refuses a write rather than the code promising not to
make one) and a test drives every route against a live run and compares the
tables before and after. There is an
optional access token, and because there is no TLS the server **refuses to bind
off loopback unless one is set**.

## From the shell

`claim` exits 1 with no output when the queue is dry, so a loop ends by itself.
Eight workers, no coordinator:

```bash
fleetwright add extract --from-file pages.txt --run "$RUN"

for i in $(seq 1 8); do
  ( me="worker-$i"
    while unit=$(fleetwright claim extract --json --lease 1800 --worker "$me"); do
      id=$(echo "$unit" | jq -r '.[0].unit_id')
      name=$(echo "$unit" | jq -r '.[0].name')
      if my-extractor "$name"; then
        fleetwright done "$id" --worker "$me"
      else
        fleetwright fail "$id" --note "extractor exited $?" --worker "$me"
      fi
    done ) &
done
wait
fleetwright status --who
```

`--worker "$me"` on the claim **and** on the close, with the same name. Each of
those is a separate process, so there is no identity that carries from one
command to the next, and a close that cannot show whose unit it is is refused
rather than guessed at. Pass `--token` from the brief instead if you prefer, or
`--any-worker` when you are cleaning up after a fleet that is gone.

A script driving a fleet does not need to parse any of that. `wait` blocks
until the work is over and the **exit code is the interface**: `0` finished
cleanly, `1` something failed, `2` timed out.

```bash
RUN=$(fleetwright start --label "nightly extraction")
fleetwright add extract --from-file pages.txt --run "$RUN"
./spawn-my-workers.sh &

if fleetwright wait --run "$RUN" --timeout 7200; then
  ./collect-results.sh
else
  fleetwright status --run "$RUN"        # something failed, or it stalled
  fleetwright retry --run "$RUN"         # fix the bug, re-run only what broke
fi
```

## From an agent

Fifteen MCP tools, split by who uses them.

**A session that has just arrived** calls `project_state` first: which runs
exist, which are still going, what failed, and the single next command. You
have no memory of the last session and that is how you get it.

**The orchestrator** then uses `start_run`, `register_skill`, `define_kind`,
`add_jobs`, `worker_prompt`, `job_results`, `list_runs` and `list_skills`. It
can stand up an entire fleet without touching a shell.

**Each worker** uses `claim_job`, `finish_job`, `release_job`, `fail_job`,
`heartbeat_job` and `job_status`.

```json
{"mcpServers": {"work": {"command": "fleetwright",
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
| [Skill](skills/README.md) | the Claude Code skill, and how `install-skill` works |
| [Packaging](packaging/README.md) | uv, Homebrew, pip, and which to use |
| [Licensing](LICENSING.md) | Apache-2.0 everywhere except `ee/`, and where the line is |

## What it is not

**Not a scheduler.** No dependencies between units, no backoff, no cron, one
integer of priority. If you need those, run a real queue and keep this for the
hand out.

**Not a broker.** One SQLite file on one filesystem. Many processes, one box.
SQLite over NFS is not safe and this does not pretend otherwise.

**Not exactly once.** See above. Nothing is.

**It does not spawn agents.** `fleetwright prompt` generates the prompt to
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
