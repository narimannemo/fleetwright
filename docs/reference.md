# Reference

## Commands

```
superagentic add KIND NAME…    enqueue units; --from-file for a corpus
superagentic claim [KIND]      take work; exits 1 when the queue is dry
superagentic done UNIT_ID      mark finished
superagentic fail UNIT_ID      report one that could not be done
superagentic release UNIT_ID   hand back, no attempt burned
superagentic status            what is left, who holds what
superagentic reclaim           return expired leases now
superagentic serve             the MCP server, on stdio
superagentic demo              a fleet, a crash, a recovery
```

Every command except `demo` takes `--db` (default `work.db`).

### `add`

```bash
superagentic add translate p1 p2 p3
superagentic add translate --from-file pages.txt --priority 5
ls corpus/ | superagentic add extract --from-file -
```

Keyed on `kind:name`, so re-running an enumeration after the corpus grows adds
only what is new. The same name under two kinds is two units.

### `claim`

```bash
superagentic claim translate --lease 1800 -n 5
superagentic claim --json                      # any kind, machine-readable
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

sa.add(conn, kind, names, priority=0, meta=None)      -> int (how many were new)
sa.claim(conn, kind=None, *, worker=None, lease=900, n=1)  -> list[Unit]
sa.heartbeat(conn, unit_ids, *, worker, lease=900)    -> int (rows extended)
sa.finish(conn, unit_id, *, worker=None, note=None)   -> bool
sa.fail(conn, unit_id, *, note, worker=None)          -> bool
sa.release(conn, unit_id, *, worker=None, note=None)  -> bool
sa.reclaim(conn)                                      -> int
sa.progress(conn, kind=None)                          -> {kind: {status: n}}
sa.leased(conn)                                       -> rows, who holds what
sa.failures(conn)                                     -> rows, with the note
```

`Unit` carries `unit_id`, `kind`, `name`, `attempts`, `leased_until`, `meta`,
and a `seconds_left` property.

The booleans are load-bearing: `False` from `finish` means the lease expired
and another worker owns the unit. Do not assert on it — handle it.

## Versioning

Semantic, with one project-specific reading: **a lease becoming weaker is a
breaking change.** If `claim` starts handing out units it used to withhold, code
relying on that exclusivity is silently doing work twice.
