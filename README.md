# superagentic

[![ci](https://github.com/narimannemo/superagentic/actions/workflows/ci.yml/badge.svg)](https://github.com/narimannemo/superagentic/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/superagentic)](https://pypi.org/project/superagentic/)
[![python](https://img.shields.io/pypi/pyversions/superagentic)](https://pypi.org/project/superagentic/)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)

**Ten agents pointed at the same corpus all start on page one.**

No prompt fixes that. An agent cannot work on "something the others aren't"
when it has no way to find out what the others are doing. It needs somewhere to
say *I've taken this one*, and somewhere to look before it starts.

That place is a table. This is that table and six verbs — a library, a CLI, and
an MCP server, in one SQLite file with **no dependencies at all**.

```bash
uv tool install superagentic     # the CLI, isolated, on your PATH
uv pip install superagentic      # or as a library, in your project
```

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

## From Python

```python
import superagentic as sa

conn = sa.connect("work.db")
sa.add(conn, "translate", [f"page-{i}" for i in range(1, 2364)])

while units := sa.claim(conn, "translate", lease=1800):
    for u in units:
        try:
            do_the_work(u.name)
            sa.finish(conn, u.unit_id)
        except Exception as e:
            sa.fail(conn, u.unit_id, note=str(e))
```

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

Six MCP tools: `claim_job`, `finish_job`, `release_job`, `fail_job`,
`heartbeat_job`, `job_status`. The descriptions tell the agent to claim before
starting and to **stop when the queue is empty rather than invent work** —
which is the failure mode worth designing against, since an agent with nothing
to do will reliably find something.

```json
{"mcpServers": {"work": {"command": "superagentic",
                         "args": ["serve", "--db", "work.db"]}}}
```

See [MCP](docs/mcp.md).

## Documentation

| | |
|---|---|
| [Concepts](docs/concepts.md) | leases, attempts, and what this deliberately is not |
| [MCP](docs/mcp.md) | wiring it to Claude Code, Cursor, or your own agent |
| [Reference](docs/reference.md) | every command and the Python API |

## What it is not

**Not a scheduler.** No dependencies between units, no backoff, no cron, one
integer of priority. If you need those, run a real queue and keep this for the
hand-out.

**Not a broker.** One SQLite file on one filesystem. Many processes, one box.
SQLite over NFS is not safe and this does not pretend otherwise.

**Not exactly-once.** See above. Nothing is.

**It does not know what your work is.** `kind` and `name` are strings you chose
and this only ever compares them for equality.

Apache-2.0. Contributions welcome — [CONTRIBUTING.md](CONTRIBUTING.md) says
what will and will not be accepted before you spend an evening.
