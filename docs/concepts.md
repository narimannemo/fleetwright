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

**Heartbeats land between tool calls, not during one.** This is a real limit
and it is structural. An agent inside a single long generation is blocked: it
cannot call `heartbeat_job` in the middle of thinking, so the lease has to
cover the worst-case latency of one model call, not the average. That is why
the example uses `--lease 1800`. The cost is the other side of the same coin:
a worker that dies holding that unit leaves it idle for thirty minutes, which
is most of what a lease was for, in exactly the case that needed it. Nothing
here fixes that. Size the lease to your slowest single generation and know what
you are buying.

**Unit-scoped writes.** Make the thing you do at the end keyed on the unit, so
a duplicate overwrites rather than appends. If your write appends, a duplicated
unit duplicates data and no lease scheme will save you.

Note what that does *not* promise. "Idempotent, so a unit done twice
converges" is true of deterministic work and false of generative work: two
model runs over *record every claim it makes* produce two different,
both-plausible extractions. Keying on the unit means you keep one of them
rather than both. It does not mean you keep the same one. If which one matters,
key on `(unit_id, attempt)` and choose deliberately.

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
being treated as a failure, and **gives the attempt back** — for a while it did
not, so six honest hand-backs left a unit at the limit and the next worker
merely to crash sent it straight to `failed`.

**How many attempts is a property of the WORK, not of a claim.** It is declared
on the kind:

```bash
fleetwright define extract --instructions '...' --max-attempts 5
```

`reclaim()` is global — it sweeps every expired unit in the file — so it reads
each unit's own kind. It used to be handed whatever `--max-attempts` the
calling worker passed to `claim`, which meant `claim b --max-attempts 1`
retired three units of an unrelated kind in an unrelated run. `--max-attempts`
on `claim` now bounds only what that call hands out.

## Clocks

Every timestamp here is `time.time()`, and that is a deliberate choice rather
than an oversight. A lease is an interval compared **across processes**, so
`monotonic()` is not a drop-in: two processes have no shared monotonic origin.

The consequences are worth knowing:

- An NTP step **forward** expires every live lease at once, and the next
  `claim` reclaims work that is still being done.
- An NTP step **backward** freezes reclamation until wall-clock catches up.
- Two machines whose clocks disagree write timestamps that disagree. A unit can
  finish "before" the run that contains it started, which is why elapsed time
  is computed as a span over every timestamp on record rather than as
  `last - started`.

None of this is hypothetical on a fleet spread over more than one host. Keep
the machines on NTP, and size leases so a second or two of drift is noise.

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

## Skills are registered, pinned, and never fetched

A kind requires skills by name. The registry says what a name **means**:

```bash
fleetwright skill xrad-extraction --source skills/xrad/SKILL.md --version 1.2
```

Three things follow, and each is a decision:

**One definition, many kinds.** Without a registry every kind carries the bare
string, three kinds needing the same skill repeat it, and renaming means
editing all three.

**A readable source is hashed, and the hash is pinned onto the unit at claim
time.** This is the part that earns its keep. Edit a skill halfway through a
run and half the units were produced under one version and half under another
— pinning at claim time is the only thing that can tell them apart afterwards.
Looking the skill up later reports whatever it is *now*, which is precisely the
wrong answer:

```
p1: v1.2 [27efcd11f4a60074]
p2: v1.3 [783ae7d0b1ef6eb2]      # same run, after someone edited the file
```

`--version` alone relies on whoever edited it remembering to bump it. The
digest does not.

**Nothing is fetched or installed, ever.** The moment this downloads a skill it
has to know about Claude Code's `.claude/skills`, Cursor's rules, and every
runtime after them — and it stops working for the shell fleet that has none of
those. It records *where* a skill is and *what it hashed to*; putting it in
place belongs to whatever runs your agents, and the brief tells the worker to
fail rather than proceed without it. A test asserts the module never touches
the network.

**What it cannot do: verify.** Nothing here can check that a worker really
loaded a skill, any more than it can check the model it claims to be. The brief
states the requirement and says to fail rather than substitute; whether the
agent complied is between you and the agent.

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

## One database is one trust boundary, and the only one

There is no permission model. Everything that shares a file shares everything:
two orchestrators, twenty workers, all of it. That is worth knowing before you
point a second session at the same `work.db`.

- **A worker with no `--run` claims from every run**, not just its own. Often
  what you want, since it makes one dedup pool. Never what you expect.
- **Either session can cancel, retry or redefine the other's work.** Nothing
  checks.
- **Kinds and skills are global to the file**, not scoped to a run.

Only one thing is actually protected: a worker cannot `finish` or `heartbeat` a
unit it does not hold, because those match on the worker.

So pick deliberately:

| Two sessions | Do this |
|---|---|
| unrelated work | separate database files; there was nothing to share |
| same work, different inputs | one file, one kind, two runs, and **always pass `--run`** |
| different work, one file | namespace the kind names (`extract-t1`, `extract-t2`) |

And because a shared kind name is the sharp edge, redefining a kind with live
units is refused unless forced, and every unit pins the definition it was
claimed under so `fleetwright brief` can always say what it was told.

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
- **It does not spawn anything.** `fleetwright prompt` generates the prompt to
  spawn workers with; running it is your runtime's job. Making this package
  spawn agents would mean it needed an agent runtime, credentials, and an
  opinion about which one — and it would stop working for the shell fleet that
  needs none of those.
