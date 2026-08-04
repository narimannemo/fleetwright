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
from string import Template
from typing import Any

#: Long enough that a heartbeat is optional for short units, short enough that
#: a crashed worker's unit is not stranded for an afternoon.
DEFAULT_LEASE = 900.0

#: After this many hand-outs without a `finish`, a unit stops being offered.
#: It is almost always a bad unit rather than bad luck, and re-leasing it
#: forever costs a worker every time.
MAX_ATTEMPTS = 3

OPEN, LEASED, DONE, FAILED = "open", "leased", "done", "failed"

SCHEMA = """
-- What a kind of work IS, as opposed to which units of it are outstanding.
--
-- This is the table that makes the difference between a queue and something an
-- agent can use. A freshly spawned agent has no context: it did not read your
-- orchestration code and it will not remember the last session. Handing it
-- `page-0189` tells it nothing. Handing it `page-0189` together with what to
-- do, what finished looks like, and what to hand back is the whole job.
--
-- Keeping the instructions HERE rather than in the spawn prompt is the point.
-- A prompt is written once by whoever launched the fleet and is invisible to
-- the ninth agent that starts an hour later; a kind is read at claim time by
-- every worker that ever takes one of these units, including the one that
-- picks up a unit a dead agent abandoned.
CREATE TABLE IF NOT EXISTS kind (
    kind TEXT PRIMARY KEY,
    instructions TEXT NOT NULL,
    done_when TEXT,
    returns TEXT,
    tools TEXT,
    updated_at REAL
);

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
    -- When the current holder took it. Without this there is no way to know
    -- how long a unit took, which is the number anyone watching a fleet
    -- actually wants: it gives duration, percentiles, and an ETA.
    claimed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    -- What the worker handed back. The orchestrator that spawned the fleet has
    -- to collect the output from somewhere, and a queue that takes work but
    -- returns nothing makes every caller invent a side channel.
    result TEXT,
    -- Whatever the worker needs in order to do the work: a path, a URL, a page
    -- range. Opaque JSON, never inspected — but its keys are substituted into
    -- the kind's instructions, so `$path` in the instructions becomes this
    -- unit's path.
    meta TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS unit_pick ON unit(kind, status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS unit_lease ON unit(status, leased_until);
"""


@dataclass(frozen=True)
class Unit:
    """One piece of work, leased to you until `leased_until`.

    `instructions`, `done_when` and `returns` come from the unit's kind with
    this unit's `meta` substituted in, so a worker holding a Unit has
    everything it needs and does not have to be told anything else.
    """

    unit_id: str
    kind: str
    name: str
    attempts: int
    leased_until: float
    meta: dict
    instructions: str = ""
    done_when: str = ""
    returns: str = ""
    tools: str = ""

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.leased_until - time.time())

    def brief(self) -> str:
        """The whole assignment as text, for pasting into an agent's prompt."""
        parts = [f"UNIT: {self.name}   (kind: {self.kind}, id: {self.unit_id})"]
        if self.attempts > 1:
            parts.append(f"NOTE: attempt {self.attempts}. A previous worker took "
                         "this and never finished it.")
        if self.instructions:
            parts.append(f"\nWHAT TO DO\n{self.instructions}")
        if self.tools:
            parts.append(f"\nUSE\n{self.tools}")
        if self.done_when:
            parts.append(f"\nDONE WHEN\n{self.done_when}")
        if self.returns:
            parts.append(f"\nHAND BACK\n{self.returns}")
        parts.append(f"\nCall finish (unit_id={self.unit_id}) when done, or fail "
                     "with a reason. Do not start any other unit.")
        return "\n".join(parts)


def this_worker() -> str:
    """A default identity that still means something in a log six hours later."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to a file written by an older version.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
    so a new column is invisible to every database created before it. The
    failure is an OperationalError deep inside a query, on someone else's
    machine, with a file they cannot easily recreate.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(unit)")}
    for col, decl in (("claimed_at", "REAL"), ("result", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE unit ADD COLUMN {col} {decl}")
    conn.commit()


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
    _migrate(conn)
    return conn


def _render(text: str | None, unit_name: str, meta: dict) -> str:
    """Substitute this unit's details into a kind's instructions.

    `string.Template`, not `str.format`, and the choice matters: instructions
    to an agent very often contain JSON, and `{"a": 1}` makes `format` raise.
    `$path` is rare in prose and `safe_substitute` leaves anything it does not
    recognise alone rather than failing at the moment a worker asks for work.
    """
    if not text:
        return ""
    # Two passes, because the useful way to give two thousand units a path is
    # one template — `meta={"path": "scans/$name.png"}` — not two thousand
    # dicts. So `$name` is expanded inside the meta VALUES first, and the
    # result is what the instructions see.
    scalars = {k: v for k, v in meta.items() if isinstance(v, (str, int, float))}
    resolved = {k: (Template(v).safe_substitute(name=unit_name)
                    if isinstance(v, str) else v)
                for k, v in scalars.items()}
    return Template(text).safe_substitute(
        {"name": unit_name, "unit": unit_name, **resolved})


def define(conn: sqlite3.Connection, kind: str, instructions: str, *,
           done_when: str | None = None, returns: str | None = None,
           tools: str | None = None) -> None:
    """Say what this kind of work IS, once, so every worker is told the same thing.

    The alternative is putting the instructions in the prompt that spawns the
    fleet — where the ninth agent, started an hour later by a `claim` loop,
    never sees them, and where the agent that inherits a dead worker's unit
    certainly does not.

    Use `$name` for the unit, and `$key` for any string or number in that
    unit's `meta`:

        define(conn, "extract",
               instructions="Read $path. Record every claim it makes.",
               done_when="every claim on the page is recorded, or you have "
                         "established there are none",
               returns='{"claims": <int>, "notes": "<string>"}',
               tools="the `xrad` MCP server: record_claim, check_quote")

    Re-defining a kind replaces it. Instructions are read at claim time, so a
    correction reaches every worker that has not yet claimed, without
    restarting anything.
    """
    conn.execute(
        "INSERT OR REPLACE INTO kind (kind, instructions, done_when, returns, "
        "tools, updated_at) VALUES (?,?,?,?,?,?)",
        (kind, instructions, done_when, returns, tools, time.time()))
    conn.commit()


def spec(conn: sqlite3.Connection, kind: str) -> dict | None:
    """What a kind of work is, unrendered. `None` if it was never defined."""
    r = conn.execute("SELECT * FROM kind WHERE kind = ?", (kind,)).fetchone()
    return dict(r) if r else None


def _to_unit(r: sqlite3.Row, spec_row: sqlite3.Row | None = None) -> Unit:
    meta = json.loads(r["meta"] or "{}")
    sp = spec_row or {}
    return Unit(r["unit_id"], r["kind"], r["name"], r["attempts"],
                r["leased_until"] or 0.0, meta,
                _render(sp["instructions"] if sp else "", r["name"], meta),
                _render(sp["done_when"] if sp else "", r["name"], meta),
                _render(sp["returns"] if sp else "", r["name"], meta),
                _render(sp["tools"] if sp else "", r["name"], meta))


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
                   claimed_at = ?, attempts = attempts + 1, updated_at = ?
             WHERE unit_id IN (
                   SELECT unit_id FROM unit
                    WHERE status = ? {kind_clause}
                    ORDER BY priority DESC, created_at
                    LIMIT ?)""",
        [LEASED, worker, now + lease, token, now, now, OPEN, *params, n])
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM unit WHERE lease_token = ? ORDER BY priority DESC, created_at",
        (token,)).fetchall()
    # One lookup per distinct kind, not per unit: a batch of 20 is almost
    # always 20 units of the same kind.
    specs = {k: conn.execute("SELECT * FROM kind WHERE kind = ?", (k,)).fetchone()
             for k in {r["kind"] for r in rows}}
    return [_to_unit(r, specs.get(r["kind"])) for r in rows]


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
           worker: str | None, note: str | None, result: Any = None) -> bool:
    now = time.time()
    # `worker` is KEPT on a terminal status and cleared only when the unit goes
    # back to open. Who finished what is the basis of every per-worker number,
    # and nulling it on close threw that away for no benefit -- claimability is
    # decided by `status`, never by `worker`.
    keep = status in (DONE, FAILED)
    sql = (f"UPDATE unit SET status=?, worker={'worker' if keep else 'NULL'}, "
           "leased_until=NULL, lease_token=NULL, note=?, result=?, updated_at=? "
           "WHERE unit_id=? AND status=?")
    args: list = [status, note,
                  None if result is None else json.dumps(result, ensure_ascii=False),
                  now, unit_id, LEASED]
    if worker is not None:
        sql += " AND worker=?"
        args.append(worker)
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.rowcount > 0


def finish(conn: sqlite3.Connection, unit_id: str, *, worker: str | None = None,
           note: str | None = None, result: Any = None,
           then: dict[str, list[str]] | None = None) -> bool:
    """Mark a unit done, hand back a result, and optionally enqueue what follows.

    `False` means the lease was not yours to close — this worker was slow, it
    expired, and someone else owns the unit now. Worth handling rather than
    asserting on: the work may not be wasted, but this worker should stop
    heartbeating and go ask for something else.

    `result` is whatever the worker produced, JSON-serialisable. The
    orchestrator that spawned the fleet has to collect output from somewhere,
    and a queue that takes work but returns nothing makes every caller invent a
    side channel.

    `then` enqueues follow-on work, and is how a pipeline is built without this
    becoming a scheduler:

        finish(conn, u.unit_id, result={"claims": 12},
               then={"audit": [f"claim-{i}" for i in ids]})

    Nothing is enqueued if the close failed, so a worker that lost its lease
    cannot inject work off the back of a unit it no longer owns.
    """
    ok = _close(conn, unit_id, DONE, worker=worker, note=note, result=result)
    if ok and then:
        for kind, names in then.items():
            if names:
                add(conn, kind, list(names))
    return ok


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


def results(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    """What the fleet produced, for the orchestrator that spawned it.

    Only finished units, in the order they finished. `result` is decoded; a
    worker that finished without handing anything back has `None`, which is
    different from having handed back nothing.
    """
    q = ("SELECT kind, name, unit_id, result, note, updated_at FROM unit "
         "WHERE status = ?" + (" AND kind = ?" if kind else "") +
         " ORDER BY updated_at")
    args = (DONE, kind) if kind else (DONE,)
    return [{"kind": r["kind"], "name": r["name"], "unit_id": r["unit_id"],
             "result": json.loads(r["result"]) if r["result"] else None,
             "note": r["note"], "finished_at": r["updated_at"]}
            for r in conn.execute(q, args)]


def failures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Units no worker could finish, with the note saying why."""
    return conn.execute(
        "SELECT * FROM unit WHERE status = ? ORDER BY updated_at", (FAILED,)
    ).fetchall()


def stats(conn: sqlite3.Connection, *, buckets: int = 40) -> dict:
    """Everything a dashboard needs, in one pass over the table.

    One function rather than a dozen queries because a dashboard polls, and a
    poll that takes twelve round trips against a file another process is
    writing to will eventually read a torn picture: units counted as `leased`
    in one query and `done` in the next, so the totals do not add up and the
    number on screen flickers. This reads the rows once.

    `throughput` is bucketed over the window the run actually spans, not over
    a fixed hour, because a fleet that finished in ninety seconds and one that
    ran overnight both have to be legible.
    """
    now = time.time()
    reclaim(conn)
    rows = conn.execute("SELECT * FROM unit").fetchall()

    by_kind: dict[str, dict[str, int]] = {}
    done_times: list[float] = []
    durations: list[float] = []
    per_worker: dict[str, dict] = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {OPEN: 0, LEASED: 0, DONE: 0, FAILED: 0})
        k[r["status"]] += 1
        if r["status"] == DONE:
            done_times.append(r["updated_at"] or now)
            if r["claimed_at"]:
                durations.append(max(0.0, (r["updated_at"] or now) - r["claimed_at"]))
        if r["worker"] and r["status"] in (DONE, FAILED):
            w = per_worker.setdefault(r["worker"], {"worker": r["worker"],
                                                    "done": 0, "failed": 0,
                                                    "seconds": 0.0})
            w["done" if r["status"] == DONE else "failed"] += 1
            if r["claimed_at"] and r["status"] == DONE:
                w["seconds"] += max(0.0, (r["updated_at"] or now) - r["claimed_at"])

    totals = {s: sum(k[s] for k in by_kind.values())
              for s in (OPEN, LEASED, DONE, FAILED)}
    left = totals[OPEN] + totals[LEASED]

    # Percentiles, not a mean. One unit that hung for an hour drags a mean
    # somewhere no unit actually was; p50 and p95 say what to expect and what
    # the tail costs.
    ds = sorted(durations)
    def pct(p: float) -> float | None:
        return ds[min(len(ds) - 1, int(p * len(ds)))] if ds else None

    series: list[dict] = []
    span = 0.0
    if done_times:
        lo, hi = min(done_times), max(max(done_times), now)
        span = max(hi - lo, 1.0)
        width = span / buckets
        counts = [0] * buckets
        for t in done_times:
            counts[min(buckets - 1, int((t - lo) / width))] += 1
        series = [{"t": lo + i * width, "n": c} for i, c in enumerate(counts)]

    # Rate over the last quarter of the window, so an ETA reflects how the
    # fleet is running now rather than including the ramp-up.
    recent = [t for t in done_times if t >= now - max(span / 4, 30.0)]
    per_sec = len(recent) / max(span / 4, 30.0) if recent else 0.0

    return {
        "now": now,
        "by_kind": by_kind,
        "totals": totals | {"all": len(rows), "left": left},
        "workers": [
            {"worker": r["worker"], "name": r["name"], "kind": r["kind"],
             "seconds_left": round(max(0.0, (r["leased_until"] or 0) - now)),
             "seconds_held": round(now - r["claimed_at"]) if r["claimed_at"] else None,
             "attempts": r["attempts"]}
            for r in rows if r["status"] == LEASED],
        "throughput": series,
        "duration": {"n": len(ds), "p50": pct(0.5), "p95": pct(0.95),
                     "max": ds[-1] if ds else None},
        "per_worker": sorted(per_worker.values(), key=lambda w: -w["done"]),
        "failures": [{"name": r["name"], "kind": r["kind"], "note": r["note"],
                      "attempts": r["attempts"]}
                     for r in rows if r["status"] == FAILED],
        "retried": sum(1 for r in rows if r["attempts"] > 1),
        "units_per_min": round(per_sec * 60, 1),
        "eta_seconds": round(left / per_sec) if per_sec and left else None,
    }
