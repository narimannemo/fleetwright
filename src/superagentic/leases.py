"""Work leases in one SQLite file. No broker, no daemon, no scheduler.

Ten workers pointed at the same corpus all start on page one. There is no
clever prompt and no amount of instruction that fixes this — a worker cannot
choose "something the others are not doing" when it has no way to learn what
the others are doing. It needs somewhere to say *I have taken this one*, and
somewhere to look before it starts.

That place is a table. The whole library is that table and six verbs.

**A lease, not a lock.** This is the only genuinely hard part, and every other
decision follows from it. A lock held by a crashed worker is worse than no lock
at all: the unit is neither being worked nor available, and nothing in the
system can distinguish a busy worker from a dead one. A lease makes that
distinction the passage of time — renew it and you keep the unit, stop renewing
and it returns to the pool. `reclaim()` runs at the top of every `claim()`, so
the next worker asking for work does the cleanup on its way in and there is
nothing to run on a timer.

**At-least-once, and it cannot be otherwise.** A worker that is *slow* rather
than *dead* will have its lease expire, a second worker will take the unit, and
both will finish it. No timeout distinguishes those cases; distributed systems
do not get to have one. Two defences, and you want both: heartbeat while you
work, and make the write at the end idempotent. Anything that claims to give
you exactly-once over an unreliable worker is lying or is a transaction
manager.

**Attempts are counted on the way in.** A unit that segfaults its worker never
gets to report anything, so counting failures when they are reported would
re-lease such a unit forever. Counting at hand-out retires it after
`MAX_ATTEMPTS` and puts it in front of a person, which is where a poison unit
belongs.

**`kind` and `unit` are opaque.** They are strings you chose, compared only for
equality. `("translate", "page-0189")`, `("audit", "claim-42")`,
`("thumbnail", "IMG_2231.CR2")` are all fine and all mean nothing here. This is
a coordination primitive and not a scheduler: no dependencies between units, no
backoff, no priorities beyond one integer, no cron. If you need those, run a
real queue.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

#: Long enough that a heartbeat is optional for short units, short enough that
#: a crashed worker's unit is not stranded for an afternoon.
DEFAULT_LEASE = 900.0

#: After this many hand-outs without a `finish`, a unit stops being offered.
#: It is almost always a bad unit rather than bad luck, and re-leasing it
#: forever costs a worker every time.
MAX_ATTEMPTS = 3

OPEN, LEASED, DONE, FAILED = "open", "leased", "done", "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS unit (
    -- kind:name, so enqueueing the same work twice is a no-op rather than a
    -- second copy. Callers re-run their enumeration all the time.
    unit_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    worker TEXT,
    -- Unix seconds. Past this the lease is void and anyone may take it.
    leased_until REAL,
    -- Stamped by the claimer and matched on read, so two workers racing the
    -- same UPDATE cannot read each other's rows back.
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    -- Whatever the caller needs in order to do the work. Opaque JSON; nothing
    -- here ever reads inside it.
    meta TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS unit_pick ON unit(kind, status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS unit_lease ON unit(status, leased_until);
"""


@dataclass(frozen=True)
class Unit:
    """One piece of work, leased to you until `leased_until`."""

    unit_id: str
    kind: str
    name: str
    attempts: int
    leased_until: float
    meta: dict

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.leased_until - time.time())


def this_worker() -> str:
    """A default identity that still means something in a log six hours later."""
    return f"{socket.gethostname()}:{os.getpid()}"


def connect(path: str | Path = "work.db") -> sqlite3.Connection:
    """Open (and create) the lease file.

    WAL because a fleet is several processes on one file: under the default
    rollback journal a reader blocks a writer and the second worker gets
    `database is locked` immediately. The busy timeout covers the case WAL does
    not — two writers — by waiting rather than raising, which for a lease table
    is the whole difference between working and not.
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


def _to_unit(r: sqlite3.Row) -> Unit:
    return Unit(r["unit_id"], r["kind"], r["name"], r["attempts"],
                r["leased_until"] or 0.0, json.loads(r["meta"] or "{}"))


def add(conn: sqlite3.Connection, kind: str, names: list[str], *,
        priority: int = 0, meta: dict | None = None) -> int:
    """Enqueue units of one kind. Returns how many were new.

    Idempotent on `kind:name`, so re-running your enumeration after the corpus
    grew adds the new units and leaves the finished ones finished. That is the
    common case and it should not need a flag.
    """
    now = time.time()
    payload = json.dumps(meta or {})
    before = conn.execute("SELECT count(*) FROM unit").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO unit "
        "(unit_id, kind, name, status, priority, meta, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(f"{kind}:{n}", kind, n, OPEN, priority, payload, now, now) for n in names])
    conn.commit()
    return conn.execute("SELECT count(*) FROM unit").fetchone()[0] - before


def claim(conn: sqlite3.Connection, kind: str | None = None, *,
          worker: str | None = None, lease: float = DEFAULT_LEASE,
          n: int = 1, max_attempts: int = MAX_ATTEMPTS) -> list[Unit]:
    """Take up to `n` units, or get `[]` when there is nothing to do.

    The atomicity is one `UPDATE`. SQLite runs it inside an implicit
    transaction holding a write lock, so `status = 'open'` is evaluated under
    that lock and two workers cannot both match the same row. Each call stamps
    a fresh `lease_token` and reads back by it — without that, a concurrent
    claimer's rows would be indistinguishable from ours on the follow-up
    SELECT.

    Deliberately not `RETURNING`, which needs SQLite 3.35 and would quietly
    drop the older system libraries some 3.11 installs still ship with.
    """
    worker = worker or this_worker()
    reclaim(conn, max_attempts=max_attempts)
    token = uuid.uuid4().hex
    now = time.time()
    kind_clause, params = ("AND kind = ?", [kind]) if kind else ("", [])
    conn.execute(
        f"""UPDATE unit
               SET status = ?, worker = ?, leased_until = ?, lease_token = ?,
                   attempts = attempts + 1, updated_at = ?
             WHERE unit_id IN (
                   SELECT unit_id FROM unit
                    WHERE status = ? {kind_clause}
                    ORDER BY priority DESC, created_at
                    LIMIT ?)""",
        [LEASED, worker, now + lease, token, now, OPEN, *params, n])
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM unit WHERE lease_token = ? ORDER BY priority DESC, created_at",
        (token,)).fetchall()
    return [_to_unit(r) for r in rows]


def heartbeat(conn: sqlite3.Connection, unit_ids: list[str], *, worker: str,
              lease: float = DEFAULT_LEASE) -> int:
    """Push the expiry out on work still in progress.

    Matching on `worker` on purpose: a worker whose lease already expired and
    was taken by someone else must not be able to extend it back. It has lost
    the unit, and the honest signal is that this returns 0.
    """
    if not unit_ids:
        return 0
    now = time.time()
    cur = conn.execute(
        f"UPDATE unit SET leased_until = ?, updated_at = ? "
        f"WHERE status = ? AND worker = ? AND unit_id IN "
        f"({','.join('?' * len(unit_ids))})",
        [now + lease, now, LEASED, worker, *unit_ids])
    conn.commit()
    return cur.rowcount


def _close(conn: sqlite3.Connection, unit_id: str, status: str, *,
           worker: str | None, note: str | None) -> bool:
    now = time.time()
    sql = ("UPDATE unit SET status=?, worker=NULL, leased_until=NULL, "
           "lease_token=NULL, note=?, updated_at=? WHERE unit_id=? AND status=?")
    args: list = [status, note, now, unit_id, LEASED]
    if worker is not None:
        sql += " AND worker=?"
        args.append(worker)
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.rowcount > 0


def finish(conn: sqlite3.Connection, unit_id: str, *, worker: str | None = None,
           note: str | None = None) -> bool:
    """Mark a unit done. `False` means the lease was not yours to close.

    Worth handling rather than asserting on: `False` means this worker was
    slow, the lease expired, and someone else owns the unit now. The work is
    not necessarily wasted — with an idempotent write it converged — but this
    worker should stop heartbeating and go ask for something else.
    """
    return _close(conn, unit_id, DONE, worker=worker, note=note)


def release(conn: sqlite3.Connection, unit_id: str, *, worker: str | None = None,
            note: str | None = None) -> bool:
    """Give a unit back unfinished, without calling it a failure.

    For the worker that looks at a unit and decides it is not the right one to
    do — wrong language, empty page, out of budget. Returning it immediately is
    much better than holding the lease until it expires.
    """
    return _close(conn, unit_id, OPEN, worker=worker, note=note)


def fail(conn: sqlite3.Connection, unit_id: str, *, note: str,
         worker: str | None = None, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """Report a unit that could not be done, with the reason.

    Back to `open` while attempts remain, `failed` once they do not. The note
    survives either way — a queue that forgets why something failed makes the
    next person re-run it to find out.
    """
    row = conn.execute("SELECT attempts FROM unit WHERE unit_id=?",
                       (unit_id,)).fetchone()
    if row is None:
        return False
    status = FAILED if row["attempts"] >= max_attempts else OPEN
    return _close(conn, unit_id, status, worker=worker, note=note)


def reclaim(conn: sqlite3.Connection, *, max_attempts: int = MAX_ATTEMPTS,
            now: float | None = None) -> int:
    """Return expired leases to the pool. How a fleet survives a crash.

    Runs at the top of every `claim`, so no daemon and no cron: the next worker
    to ask for work does the cleanup. A unit that has burned through
    `max_attempts` is retired to `failed` rather than reopened — three workers
    have now died on it and the fourth will too.
    """
    t = time.time() if now is None else now
    conn.execute(
        "UPDATE unit SET status=?, worker=NULL, leased_until=NULL, "
        "lease_token=NULL, note=?, updated_at=? "
        "WHERE status=? AND leased_until < ? AND attempts >= ?",
        (FAILED, "lease expired and out of attempts", t, LEASED, t, max_attempts))
    cur = conn.execute(
        "UPDATE unit SET status=?, worker=NULL, leased_until=NULL, "
        "lease_token=NULL, updated_at=? WHERE status=? AND leased_until < ?",
        (OPEN, t, LEASED, t))
    conn.commit()
    return cur.rowcount


def progress(conn: sqlite3.Connection, kind: str | None = None) -> dict:
    """Counts by kind and status. For a status line, and for deciding to stop."""
    q = ("SELECT kind, status, count(*) AS n FROM unit "
         + ("WHERE kind = ? " if kind else "") + "GROUP BY kind, status")
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(q, (kind,) if kind else ()):
        out.setdefault(r["kind"], {OPEN: 0, LEASED: 0, DONE: 0, FAILED: 0})
        out[r["kind"]][r["status"]] = r["n"]
    return out


def leased(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Who is holding what, and for how much longer. The fleet, visible."""
    reclaim(conn)
    return conn.execute(
        "SELECT * FROM unit WHERE status = ? ORDER BY leased_until", (LEASED,)
    ).fetchall()


def failures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Units no worker could finish, with the note saying why."""
    return conn.execute(
        "SELECT * FROM unit WHERE status = ? ORDER BY updated_at", (FAILED,)
    ).fetchall()
