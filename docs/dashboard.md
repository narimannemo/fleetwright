# Watching a fleet

```bash
fleetwright dashboard --db work.db                     # http://127.0.0.1:8787
fleetwright dashboard --db a.db --project ./queues     # several projects
fleetwright dashboard --db work.db --out fleet.html    # a static snapshot
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
fleetwright dashboard --db kircher.db --project plutarch.db
fleetwright dashboard --db kircher.db --project ./queues   # a directory of them
```

## The login, and what it is not

```bash
export FLEETWRIGHT_TOKEN="$(openssl rand -hex 24)"
fleetwright dashboard --db work.db --host 0.0.0.0
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
$ fleetwright dashboard --host 0.0.0.0
refusing to bind 0.0.0.0 without --token.
  This serves queue contents and machine names over plain HTTP.
  Either keep it on 127.0.0.1, or set a token:
      fleetwright dashboard --host 0.0.0.0 --token "$(openssl rand -hex 24)"
```

That refusal is the entire security design. A login form whose real effect is
to make an unencrypted service *feel* safe is worse than no login form — it is
the reason someone passes `--host 0.0.0.0` once and forgets. Prefer the
environment variable over `--token`: a flag lands in shell history and in `ps`
output for anyone else on the box.

For anything beyond a trusted network, put it behind a reverse proxy that
terminates TLS. This does not try to be that proxy.

## What it is for

`fleetwright status` gives you a number:

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
fleetwright claim extract --worker agent-1 --model claude-opus-5
```

or `model` on `claim_job` over MCP, or `FLEETWRIGHT_MODEL` in the environment.

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

The pipeline as nodes and edges, shown only when something actually chains.
Node height is unit count, edge width is how many units one kind caused in
another, and the strip along the bottom of each node is its status mix, so the
shape carries the numbers rather than decorating them:

```
                 +---------+
            40   | extract |   40
          +----->|  40u    |----+
+------+  |      +---------+    |    +-------+  80   +---------+
| scan |--+                     +--->| audit |------>| publish |
|  40u |  |      +---------+    |    |  80u  |       |   80u   |
+------+  +----->|  gloss  |----+    +-------+       +---------+
            40   |   40u   |   40
                 +---------+
```

**Laid out in columns, not force-directed.** A pipeline has a direction, and a
force layout throws it away: the same data lands somewhere different on every
load, and "which way does the work flow" stops being answerable at a glance.
A node's column is the *longest* path to it, so a stage sits to the right of
everything that can feed it however many hops away that is. Cycles are
tolerated rather than assumed away, since nothing stops an `audit` kind
enqueueing back into `extract`.

**One node per kind, not per unit.** 255 unit nodes is a hairball and 400,000
is a dead browser tab. For a single unit, `fleetwright lineage <unit_id>`
walks its own chain in both directions.

The exact counts stay in a list underneath, which is also the reading that does
not depend on colour.

For a single unit, `fleetwright lineage <unit_id>` walks the chain in both
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
fleetwright dashboard --db work.db --out fleet.html
```

One self-contained file with the data baked in — nothing fetched, no server, no
refresh. For attaching to a run report, sending to someone, or looking at a
fleet that finished hours ago.

## From Python

```python
from fleetwright import leases
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

## Several projects at once

A project **is** a database. There is no project table and no registry file,
because a registry makes one file the index for the others, and then moving or
deleting that file breaks the rest.

```bash
export FLEETWRIGHT_PROJECTS="$HOME/code/apply-intelligence:$HOME/code/project-kzd"
fleetwright dashboard
```

Colon-separated, like `PATH`, so it needs no new file and no new syntax to
learn. `--project` takes the same values and is repeatable. Either accepts a
database, a repository holding one, or a directory of them.

**Each project is named after what you call it.** Every repository holds the
default `work.db`, so naming by filename produced one project called `work` and
gave every other one its absolute path. The label is now the shortest thing
that is both informative and unique: the directory for a default filename, the
filename when it was chosen deliberately, and more of the path only when two
would otherwise collide.

| On disk | In the sidebar |
|---|---|
| `~/code/myth-analysis/work.db` | `myth-analysis` |
| `~/code/myth-analysis/audit.db` | `audit` |
| `~/old/myth-analysis/work.db` and `~/new/myth-analysis/work.db` | `old/myth-analysis`, `new/myth-analysis` |

## Access, and what it is actually protecting against

There are no user accounts. There is one shared token, because this is a
console you point at your own work, and inventing accounts would be pretending
to an identity system that does not exist. What follows is what that does and
does not buy you.

### Running it on your own machine

The default binds `127.0.0.1` with no token, and that is the right default. Two
things make it safe enough, and one thing does not.

**The `Host` header is checked.** This is the part people leave out, and it is
the one that matters. A page you visit at `evil.com` cannot normally read
`http://127.0.0.1:8787` because the browser blocks the cross-origin read. But
`evil.com` can publish a one-second DNS TTL, serve you a page, and re-resolve
its own name to `127.0.0.1`. The browser then believes the origin *is*
`evil.com`, same-origin is satisfied, and the page reads your fleet: unit
names, notes, results, worker names, and whatever paths you put in `meta`. The
browser cannot detect this. The server can, because the `Host` header says
`evil.com` and this server knows it is not called that. So it answers `421`.

**Loopback is not a user boundary.** On a machine with other people or other
accounts on it, anyone who can open a socket can read the dashboard. If that
describes your machine, set a token:

```bash
export FLEETWRIGHT_TOKEN="$(openssl rand -hex 24)"
```

### Running it on a server

**Do not put it on a network in plain HTTP.** It refuses `--host 0.0.0.0`
without a token, but a token over plain HTTP is a token typed into a form and
sent in the clear, which is worse than no token because it feels like
protection. The two right answers, in order:

**An SSH tunnel, which needs no configuration and no certificate:**

```bash
# on the server: leave it where it is
fleetwright dashboard --db work.db

# on your laptop
ssh -N -L 8787:127.0.0.1:8787 you@that-machine
# then open http://127.0.0.1:8787
```

The dashboard never leaves loopback, the traffic is encrypted by ssh, and
authentication is the ssh key you already have. This is the recommendation.

**A reverse proxy terminating TLS**, if several people need it:

```bash
fleetwright dashboard --token-file /etc/fleetwright/token \
  --allow-host fleet.example.com
```

`--allow-host` is required here: behind a proxy the legitimate `Host` is
whatever the proxy passes, so without being told, the server cannot tell that
name from an attacker's. Have the proxy set `X-Forwarded-Proto: https`, and the
session cookie is issued with `Secure`.

### The token

- **16 characters minimum, refused below that.** `--token abc` used to be
  accepted, and the only thing between it and a dictionary was a half-second
  sleep on a wrong guess. That sleep did nothing: the server is threaded, so it
  delayed one connection while sixty others ran beside it. Measured, 200 wrong
  guesses took 2.1 seconds.
- **Ten wrong guesses from one address locks it out for a minute.** That is the
  real limit, and it is what makes guessing pointless rather than merely slow.
- **`--token auto`** generates one and prints it, so nobody has to invent one.
- **Prefer `--token-file` or `FLEETWRIGHT_TOKEN`** over `--token`. A flag lands
  in your shell history and in `ps` output for every other user on the machine.
- Compared with `hmac.compare_digest`, so a wrong token takes the same time to
  reject whichever character is wrong.

### What is deliberately not here

No TLS, no accounts, no roles, no audit log. A tool that ships its own
half-implemented crypto is worse than one that says plainly: put it behind ssh
or a proxy that does this properly. The snapshot from `--out` has no access
control at all, by design, because it is a file: whoever can read the file can
read the fleet.
