# Reference

## Commands

```
superagentic define KIND       say what this kind of work IS
superagentic add KIND NAME…    enqueue units; --from-file for a corpus
superagentic claim [KIND]      take work; exits 1 when the queue is dry
superagentic done UNIT_ID      mark finished
superagentic fail UNIT_ID      report one that could not be done
superagentic release UNIT_ID   hand back, no attempt burned
superagentic status            what is left, who holds what
superagentic results [KIND]    what the fleet handed back
superagentic dashboard         a live view of the fleet, in a browser
superagentic reclaim           return expired leases now
superagentic serve             the MCP server, on stdio
superagentic demo              a fleet, a crash, a recovery
```

Every command except `demo` takes `--db` (default `work.db`).

### `define`

```bash
superagentic define extract \
  --instructions 'Read $path. Record every claim it makes, quoting verbatim.' \
  --done-when    'every claim on the page is recorded, or you have established
                  there are none' \
  --returns      '{"claims": <int>, "notes": "<string>"}' \
  --tools        'the xrad MCP server: record_claim, check_quote'

superagentic define extract --instructions-file prompts/extract.md
```

Say what the work **is**, once. Every worker that claims a unit of this kind is
handed these instructions — including a worker spawned an hour later, and the
one that inherits a unit a crashed worker dropped. That is the reason they live
here and not in the prompt you spawn workers with.

`$name` is the unit; `$key` is any string or number in that unit's `meta`.
Substitution is `string.Template`, so **JSON in your instructions is safe** and
an unknown `$placeholder` is left alone rather than failing at the moment a
worker asks for work.

Re-defining replaces. Instructions are read at claim time, so a correction
reaches every worker that has not yet claimed, without restarting anything.

### `add`

```bash
superagentic add translate p1 p2 p3
superagentic add translate --from-file pages.txt --priority 5
superagentic add extract --from-file pages.txt --meta '{"path": "scans/$name.png"}'
ls corpus/ | superagentic add extract --from-file -
```

Keyed on `kind:name`, so re-running an enumeration after the corpus grows adds
only what is new. The same name under two kinds is two units.

### `claim`

```bash
superagentic claim translate --lease 1800 -n 5
superagentic claim --json                      # the unit and its full brief
superagentic claim extract --brief             # just the assignment, as text
```

`--brief` prints the whole assignment and nothing else, for handing straight to
an agent:

```bash
superagentic claim extract --brief | claude -p -
```

Exits **1** with no stdout when there is nothing to take, so:

```bash
while unit=$(superagentic claim extract --json); do … done
```

`--worker` defaults to `hostname:pid`.

### `done` / `fail` / `release`

```bash
superagentic done    extract:p0189
superagentic fail    extract:p0189 --note "no text layer"
superagentic release extract:p0189 --note "wrong language"
```

`done` exits 1 if the lease had already expired and another worker owns the
unit. `fail` retries until attempts run out, then sets the unit aside. `release`
does not count against the limit.

### `status`

```bash
superagentic status --who
superagentic status extract --json
```

### `results`

```bash
superagentic results extract --json
```

What finished units handed back, in the order they finished. For the process
that spawned the fleet and now has to assemble the output.

### `dashboard`

```bash
superagentic dashboard --db work.db              # http://127.0.0.1:8787
superagentic dashboard --db work.db --out fleet.html   # static snapshot
```

Six tiles (left, done, in flight, failed, throughput, ETA), units finished over
time, progress per kind, **what every worker is holding right now and for how
long**, a per-worker table, and everything nobody could finish with the reason.

Served from `http.server` with the page's CSS and JS inline — no framework, no
build step, nothing fetched from a CDN. Read-only: it opens the database,
reads, and serves, so pointing it at a live run cannot disturb the run.

Binds to **loopback only**. It exposes queue contents and machine names and has
no authentication; `--host` will override that, deliberately explicitly.

### `reclaim`

Returns expired leases immediately. Rarely needed — `claim` does it — but
useful when you want to see the state before any worker asks.

### `serve`

```bash
superagentic serve --db work.db
```

See [MCP](mcp.md).

### `demo`

```bash
uvx superagentic demo
```

Runs against a temporary database, so it is safe anywhere.

## Python API

```python
import superagentic as sa

conn = sa.connect("work.db")

sa.define(conn, kind, instructions, *, done_when=None, returns=None, tools=None)
sa.spec(conn, kind)                                   -> dict | None
sa.add(conn, kind, names, priority=0, meta=None)      -> int (how many were new)
sa.claim(conn, kind=None, *, worker=None, lease=900, n=1)  -> list[Unit]
sa.heartbeat(conn, unit_ids, *, worker, lease=900)    -> int (rows extended)
sa.finish(conn, unit_id, *, worker=None, note=None, result=None, then=None) -> bool
sa.fail(conn, unit_id, *, note, worker=None)          -> bool
sa.release(conn, unit_id, *, worker=None, note=None)  -> bool
sa.reclaim(conn)                                      -> int
sa.progress(conn, kind=None)                          -> {kind: {status: n}}
sa.leased(conn)                                       -> rows, who holds what
sa.failures(conn)                                     -> rows, with the note
sa.results(conn, kind=None)                           -> what workers handed back
```

`Unit` carries `unit_id`, `kind`, `name`, `attempts`, `leased_until`, `meta`,
the rendered `instructions` / `done_when` / `returns` / `tools`, a
`seconds_left` property, and `brief()` — the whole assignment as one block of
text, because an agent handed four fields will read one of them.

`then` on `finish` enqueues the next stage, and is how a pipeline is built
without this becoming a scheduler:

```python
sa.finish(conn, u.unit_id, result={"claims": 12},
          then={"audit": [f"claim-{i}" for i in ids]})
```

Nothing is enqueued if the close failed, so a worker that lost its lease cannot
inject work off the back of a unit it no longer owns.

The booleans are load-bearing: `False` from `finish` means the lease expired
and another worker owns the unit. Do not assert on it — handle it.

## Versioning

Semantic, with one project-specific reading: **a lease becoming weaker is a
breaking change.** If `claim` starts handing out units it used to withhold, code
relying on that exclusivity is silently doing work twice.
