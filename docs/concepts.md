# Concepts

Five ideas, each of which is a decision someone will want to change. The reason
is here so the argument can be had properly.

## A kind is what the work IS; a unit is one piece of it

The queue half of this is unremarkable. The half that matters for a fleet of
agents is that **a claimed unit arrives with its own instructions**.

A freshly spawned agent has no context. It did not read your orchestration
code, it cannot see its nine siblings, and it will not remember any of it next
session. If `claim` hands it `page-0189` and nothing else, the task has to come
from the prompt that spawned it — and that prompt is invisible to:

- the worker spawned an hour later by a loop that outlived the launch;
- the worker that inherits a unit a crashed worker dropped;
- you, next week, trying to work out what these agents were told.

So the instructions live on the **kind**, are read at **claim** time, and are
handed over with every unit:

```python
sa.define(conn, "extract",
    instructions="Read $path. Record every claim it makes, quoting verbatim.",
    done_when="every claim on the page is recorded, or you have established "
              "there are none",
    returns='{"claims": <int>}',
    tools="the `xrad` MCP server: record_claim")
```

`done_when` is the one people skip and the one that costs most. Without it each
worker decides for itself what finished means, and ten agents will produce ten
standards on the same corpus. Both the CLI and the MCP tool warn when it is
missing.

Substitution is `string.Template`, not `str.format`, for a specific reason:
instructions to an agent are full of JSON, and `{"ok": true}` makes `format`
raise. `$name` is the unit; `$key` is anything in its `meta`; an unrecognised
`$placeholder` is left alone rather than failing at the moment a worker asks
for work. Meta values are themselves templated on `$name` first, so
`meta={"path": "scans/$name.png"}` gives two thousand units their own path
without building two thousand dicts.

## A lease, not a lock

A worker takes a unit and gets it **until a moment in time**, not until it says
otherwise.

The alternative — a lock held until released — fails the case this exists for.
A worker crashes. The lock is still held. The unit is now neither being worked
nor available, and nothing in the system can distinguish that from a worker
that is simply taking a while. The queue drains to a set of units nobody will
ever finish and nobody can take.

Time makes the distinction. `reclaim()` returns expired leases to the pool, and
runs at the top of every `claim()`, so there is nothing to schedule: the next
worker asking for work does the cleanup on its way in.

Pick `lease` as several times your **slowest** unit, not your average. A lease
that is too long costs you a delayed retry once. A lease that is too short
costs you duplicated work every single time.

## At-least-once

A slow worker and a dead worker are indistinguishable. No timeout separates
them, and no library gets to have one. So:

> A unit can be handed to a second worker while the first is still working on
> it, and both can finish it.

Two defences:

**Heartbeat.** Extend the lease while you work, so only genuinely stalled units
are reclaimed.

```python
u = sa.claim(conn, "translate")[0]
for chunk in slow_work(u.name):
    sa.heartbeat(conn, [u.unit_id], worker=me)
```

**Idempotent writes.** Make the thing you do at the end converge when repeated —
an upsert keyed on content, a write to a path derived from the unit name. If
your write appends, a duplicated unit duplicates data and no lease scheme will
save you.

And handle the `False`: `finish` returns it when the lease was lost. The worker
should stop and claim something else rather than carry on.

## Attempts count on the way in

`attempts` increments when a unit is **handed out**, not when a failure is
reported.

A unit that segfaults its worker, OOMs it, or hangs it never reports anything.
If failures were only counted when reported, that unit would be re-leased
forever, costing a worker every time round. Counting at hand-out means three
tries and then `failed` — in front of a person, which is where a poison unit
belongs.

`release()` exists for the other case: a worker that looks at a unit and
decides it is not the right one to do. It hands the unit straight back without
being treated as a failure.

## Capabilities: what a worker must HAVE

`instructions` says what to do. `skills` and `mcp` say what a worker needs
before it can do anything at all, and they are separate for a reason: a skill a
worker never loaded is not something it can discover halfway through a unit.

```python
sa.define(conn, "extract",
    instructions="Read $path. Record every claim it makes.",
    skills=["xrad-extraction", "latin-palaeography"],
    mcp={"xrad": "xrad serve --db graph.db"})
```

They are opaque strings. A skill name means nothing to this library — it can be
a Claude Code skill, a Cursor rule, or a token your own runtime understands —
which is exactly what keeps this agnostic about how you run agents. What
changes is that they arrive as **structured fields rather than prose**, so a
spawner can act on them, and the brief tells the worker to **fail a unit it is
not equipped for rather than improvise a substitute**.

That last part is the point. A unit done without its tools *looks finished*,
and a queue full of units that look finished is worse than one with obvious
gaps.

## Context is read-only, and worker-to-worker state is refused

`context` is material every worker of a kind receives: a glossary, conventions,
a schema. It is set when the kind is defined and no worker can write it.

That asymmetry is deliberate, and it is a correctness argument rather than a
preference:

> Units must be independent. Leases are **at-least-once**, so any unit may be
> run twice. If worker A writes context that worker B reads, re-running A
> silently changes B's input — and you would never find that from the results.

It also converts an unordered queue into an ordered one without saying so.
Every guarantee here rests on units not depending on each other; shared mutable
state removes that quietly.

When a stage genuinely must hand something forward, do it explicitly:

```python
sa.finish(conn, u.unit_id, result={"claims": ids},
          then={"audit": [f"claim-{i}" for i in ids]})
```

Ordered, visible in the queue, idempotent, and refused outright if the
finishing worker had already lost its lease.

## `kind` and `name` are opaque

They are strings you chose. Nothing here parses them, and a unit is keyed on
`kind:name`, which is why enqueueing the same enumeration twice is a no-op.

That is deliberate. The moment this library knows what a page is, it has
opinions about your pipeline, and the next person has to work around them.

## What this is not

- **Not a scheduler.** No dependencies between units, no retry backoff, no
  cron, one integer of priority. Those are real needs and a real queue serves
  them better.
- **Not a broker.** One SQLite file, one filesystem, many processes. SQLite
  over NFS is not safe.
- **Not exactly-once.** Nothing is.
- **No progress inside a unit.** A unit is atomic here. Make them small enough
  that losing one costs little.
- **No shared mutable state between workers.** See above — it is refused, not
  merely absent.
- **It does not spawn anything.** `superagentic prompt` generates the prompt to
  spawn workers with; running it is your runtime's job. Making this package
  spawn agents would mean it needed an agent runtime, credentials, and an
  opinion about which one — and it would stop working for the shell fleet that
  needs none of those.
