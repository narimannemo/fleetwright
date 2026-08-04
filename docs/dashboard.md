# Watching a fleet

```bash
superagentic dashboard --db work.db            # http://127.0.0.1:8787
superagentic dashboard --db work.db --out fleet.html
```

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
