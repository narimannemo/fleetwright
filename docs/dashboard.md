# Watching a fleet

```bash
superagentic dashboard --db work.db                     # http://127.0.0.1:8787
superagentic dashboard --db a.db --project ./queues     # several projects
superagentic dashboard --db work.db --out fleet.html    # a static snapshot
```

The layout is a sidebar and a detail pane: **projects** at the top, **runs**
below them, and the session at the bottom. Selecting a run scopes every panel
in the detail pane to it, and the selection lives in the URL so it can be sent
to someone.

## Projects are databases

A project *is* a SQLite file — there is no project table and no registry.
Putting one inside a database would make that file the index for the others, so
moving or deleting it would break the rest; the filename is already the name
people use.

```bash
superagentic dashboard --db kircher.db --project plutarch.db
superagentic dashboard --db kircher.db --project ./queues   # a directory of them
```

## The login, and what it is not

```bash
export SUPERAGENTIC_TOKEN="$(openssl rand -hex 24)"
superagentic dashboard --db work.db --host 0.0.0.0
```

It is a **shared access token, not user accounts.** There is no user model in
this library and inventing one for a dashboard would be pretending to an
identity system that does not exist. The token is given at startup, never
stored, compared with `hmac.compare_digest` so a wrong one does not leak its
correct prefix through timing, and exchanged for a session cookie that is
`HttpOnly` and `SameSite=Strict`.

**There is no TLS.** On a network the token travels in clear text. So:

> The server **refuses to bind to anything but loopback unless a token is
> set**, and warns on every start when it binds off-loopback anyway.

```
$ superagentic dashboard --host 0.0.0.0
refusing to bind 0.0.0.0 without --token.
  This serves queue contents and machine names over plain HTTP.
  Either keep it on 127.0.0.1, or set a token:
      superagentic dashboard --host 0.0.0.0 --token "$(openssl rand -hex 24)"
```

That refusal is the entire security design. A login form whose real effect is
to make an unencrypted service *feel* safe is worse than no login form — it is
the reason someone passes `--host 0.0.0.0` once and forgets. Prefer the
environment variable over `--token`: a flag lands in shell history and in `ps`
output for anyone else on the box.

For anything beyond a trusted network, put it behind a reverse proxy that
terminates TLS. This does not try to be that proxy.

## What it is for

`superagentic status` gives you a number:

```
kind        open  leased    done  failed    left
extract        8       6     210       3      14
```

That number is **identical** whether five workers are moving through the queue
steadily or three have died and one is stuck on a page it will never finish.
Every panel below exists to separate those two situations, and nothing is on
the page that does not.

## The panels, and the question each answers

### Runs — *what has this fleet ever done?*

Every run, newest first: units, done, failed, left, workers, elapsed, and
**parallel** — worker-seconds divided by wall-clock. That last column is the
one worth learning to read. A four-worker run showing `0.8x` had three workers
idle most of the time; adding more would have changed nothing, and the fix is
more units or more evenly sized ones. A six-worker run showing `5.1x` was
genuinely saturated.

Click a run and every panel below scopes to it. The selection lives in the URL,
so a scoped view can be sent to someone.

### Jobs — *what happened to this one unit?*

Every other panel aggregates. This is the only one that answers a question
about a single job: its status, who holds or held it, how many attempts it has
taken, how long it ran, how much lease is left, and the note or result it
produced.

Filter by status, or search across name, worker, model **and note** — so a
failure is findable by what it said rather than by remembering which page it
was. Paginated at 100 a page, and the page number lives in the URL.

### The model column

A worker can say what it is when it claims:

```bash
superagentic claim extract --worker agent-1 --model claude-opus-5
```

or `model` on `claim_job` over MCP, or `SUPERAGENTIC_MODEL` in the environment.

It is **declared, never detected.** Nothing here can verify it, and pretending
otherwise would make it evidence when it is only a label. It earns a column
because it is the one thing you cannot reconstruct afterwards — which model did
these forty units, and were they faster or worse:

```
claude-opus-5      done 159  failed  0  mean 1.74s
claude-sonnet-5    done 141  failed  0  mean 0.68s
```

That is the same corpus, interleaved between two models, which is the only
comparison worth making — two models on two different sets of units are
measuring the units, not the models.

### Who held what, when

One lane per worker, one bar per unit, time across the page. This is the panel
that answers the question a fleet actually raises, which is not *what caused
what* but **was it saturated, who sat idle, and what held up the end**:

```
session-a/agent-0   ████ ██████ ███ █████████ ████        92% busy
session-b/agent-1   █    ██        █      ██             24% busy
```

Both workers did about the same number of units. One of them spent three
quarters of the run waiting. No count of units shows that, and it is the number
that decides whether more workers would have helped.

Each lane shows the percentage of wall-clock it was busy, hover gives the unit
and its duration, and colour is the unit's status.

### What caused what

Only shown when something actually chains, and **aggregated to kinds**. A
forest of four hundred thousand individual lineages is not a picture;
`extract -> audit -> gloss` with counts on the edges is.

```
extract -> audit    126 unit(s)
audit   -> gloss     69 unit(s)
```

For a single unit, `superagentic lineage <unit_id>` walks the chain in both
directions.

### Six tiles — *where does this stand?*

Left, done, in flight, failed, throughput, ETA. The ETA is computed from the
rate over the **last quarter of the run**, not the whole of it, so it reflects
how the fleet is going now rather than including the ramp-up while workers were
starting.

### Units finished over time — *is it still moving?*

A flat tail with units still open means the workers are gone, and no other
panel says that as quickly. The most recent bucket is drawn in the in-flight
colour so "now" is locatable without reading the axis.

### Progress by kind — *which stage is the bottleneck?*

One stacked bar per kind, with a 2px gap between segments so adjacent states
never fuse into a single shape. Every colour appears beside a written label in
the legend, so the meaning never rests on hue.

### In flight right now — *is anyone stuck?*

The panel with no equivalent in a request-tracing tool, because a fleet has
work that is **neither finished nor waiting**.

Each row is a worker, the unit it holds, how long it has held it, and how much
lease is left. A unit held longer than **three times the p95** gets a severity
stripe and a `slow` pill — the question is "is anyone stuck", and a raw
duration column does not answer it. The stripe is a shape, not just a colour,
so it survives greyscale and colour-blindness.

A unit on its second or later attempt is marked `retry`. One retry after a
crash is ordinary; several at once usually means the lease is too short.

### Workers — *is the work spread evenly?*

Units done, failed, and mean duration per worker. Wildly uneven counts mean
some workers died early; a worker with a much higher mean is usually on a
different kind of unit rather than being slow.

### Could not finish — *what needs a human?*

Units that three workers gave up on, with the note each recorded. This is the
list to read before re-running anything: it is almost always a property of
those units, not bad luck.

## Percentiles, not averages

`p50` and `p95` rather than a mean, because one unit that hung for an hour
drags a mean somewhere no unit actually was. p50 says what to expect; the gap
to p95 says what the tail costs. If they are close, the fleet is uniform and a
short lease is safe. If p95 is ten times p50, `--lease` must be sized for the
tail or slow units will be reclaimed while they are still being worked.

## How it is built, and the constraints that follow

**No dependencies.** `http.server` from the standard library, CSS and JS inline
in one file, SVG drawn by hand. A dashboard that needs a web framework is one
that does not get installed on the box where the fleet is actually running.

**Read-only.** It opens the database, reads, and serves. A test asserts the
module contains no `INSERT`, `UPDATE`, `DELETE` or any mutating call, so
pointing it at a live run cannot disturb the run.

**One pass per poll.** All of it comes from a single `stats()` read. A
dashboard polls, and a dozen round trips against a file another process is
writing to will eventually read a torn picture — units counted `leased` in one
query and `done` in the next, so the totals do not add up and the screen
flickers.

**Loopback only.** It exposes queue contents, unit names and machine names, and
has no authentication. `--host` overrides that, deliberately explicitly.

**Both themes.** Light and dark are separately chosen rather than one inverted,
and the viewer's own toggle wins over the OS preference.

## The snapshot

```bash
superagentic dashboard --db work.db --out fleet.html
```

One self-contained file with the data baked in — nothing fetched, no server, no
refresh. For attaching to a run report, sending to someone, or looking at a
fleet that finished hours ago.

## From Python

```python
from superagentic import leases
s = leases.stats(conn)

s["totals"]          # {open, leased, done, failed, all, left}
s["workers"]         # who holds what, with seconds_held and seconds_left
s["duration"]        # {n, p50, p95, max}
s["throughput"]      # [{t, n}] bucketed over the window the run spans
s["per_worker"]      # [{worker, done, failed, seconds}]
s["failures"]        # [{name, kind, note, attempts}]
s["eta_seconds"]     # None when nothing is left or nothing has finished
```

Use it to fail a build, page someone, or print a line at the end of a run —
the dashboard is one consumer of this, not the only one.
