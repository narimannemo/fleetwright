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

import hashlib
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
#: Terminal, like done and failed, and deliberately NOT a deletion. A queue
#: that forgets what you cancelled cannot tell you later why a run is short.
CANCELLED = "cancelled"

#: Every status, in the order a person reads them. Consumers that enumerate
#: statuses must use this rather than a literal tuple, which is how adding one
#: silently breaks a total somewhere.
STATUSES = (OPEN, LEASED, DONE, FAILED, CANCELLED)
#: The ones that mean "no more work will happen here".
TERMINAL = (DONE, FAILED, CANCELLED)

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
-- What a skill name MEANS. Without this a kind carries the string
-- "xrad-extraction" and nothing anywhere knows what that is, where a worker
-- gets it, or which version it was — so three kinds needing the same skill
-- repeat the string, and renaming means editing all three.
--
-- This registry deliberately does NOT fetch or install anything. Distribution
-- belongs to whatever runs your agents: the moment this downloads a skill it
-- needs to know about Claude Code's .claude/skills, Cursor's rules, and every
-- runtime after them. It records where a skill is and what it hashed to; the
-- runtime puts it in place.
CREATE TABLE IF NOT EXISTS skill (
    name TEXT PRIMARY KEY,
    source TEXT,
    version TEXT,
    -- sha256 of the content, when content was given. This is what makes "which
    -- version did these 400 units use" answerable after someone edits a skill
    -- halfway through a run.
    digest TEXT,
    note TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS kind (
    kind TEXT PRIMARY KEY,
    instructions TEXT NOT NULL,
    done_when TEXT,
    returns TEXT,
    tools TEXT,
    -- What a worker must HAVE to do this work, as opposed to what it must do.
    -- Both are JSON, and both are opaque here: a skill name means nothing to
    -- this library, which is the point -- it can be a Claude Code skill, a
    -- Cursor rule, or a string your own runtime understands. Structured rather
    -- than prose so the spawner can act on it, and so a worker can REFUSE a
    -- unit whose requirements it cannot meet instead of improvising.
    skills TEXT,
    mcp TEXT,
    -- Read-only material every worker of this kind gets: a glossary, coding
    -- conventions, a schema. Deliberately set by whoever defines the kind and
    -- never written by a worker -- see `context` in docs/concepts.md for why
    -- worker-to-worker state is refused.
    context TEXT,
    updated_at REAL
);

-- One execution of a fleet: the orchestrator started it, enqueued units into
-- it, spawned workers, and eventually it is over. Without this a database is
-- one flat pool and there is no way to ask what LAST NIGHT'S run did, only
-- what the queue contains right now.
--
-- A run also SCOPES unit ids, which is the part that matters. Units are keyed
-- on kind:name and enqueueing is idempotent, so without a scope a second run
-- over the same corpus would find every unit already done and do nothing. With
-- one, re-running your enumeration inside a run still adds nothing new, and a
-- new run genuinely re-does the work. Those are both what you want and they
-- are only compatible if the run is part of the key.
--
-- There is no `finished_at`: it is max(updated_at) over the run's units, which
-- is correct even when the orchestrator died without recording anything.
CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    label TEXT,
    started_by TEXT,
    note TEXT,
    started_at REAL
);

-- Every definition a kind has ever had, keyed by the hash of its content.
--
-- Storing the rendered brief on each unit would be the obvious way to answer
-- "what was this unit actually told", and it is O(units): a 400,000 unit
-- corpus would carry most of a gigabyte of near-identical text. Content
-- addressing makes it one row per DISTINCT definition instead, and the brief
-- for any unit is re-derivable from its pinned definition plus its own meta.
CREATE TABLE IF NOT EXISTS kind_version (
    digest TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    instructions TEXT NOT NULL,
    done_when TEXT, returns TEXT, tools TEXT,
    skills TEXT, mcp TEXT, context TEXT,
    first_seen REAL
);

CREATE TABLE IF NOT EXISTS unit (
    -- kind:name, so enqueueing the same work twice is a no-op rather than a
    -- second copy. Callers re-run their enumeration all the time.
    unit_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    -- NULL for units enqueued without a run. They still work; they simply
    -- appear as "ungrouped" rather than under a run.
    run_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    worker TEXT,
    -- The skill records as they stood WHEN THIS UNIT WAS CLAIMED. Pinned, not
    -- looked up later: a skill edited mid-run leaves half the units produced
    -- under one version and half under another, and only a record taken at
    -- claim time can tell them apart.
    skills_used TEXT,
    -- Which definition of its kind this unit was claimed under. Without it, a
    -- kind redefined mid-run leaves no record of what any unit was told, and
    -- two sessions sharing a database can clobber each other invisibly.
    kind_digest TEXT,
    -- What the unit cost, as the worker reports it. DECLARED, NOT MEASURED:
    -- nothing here can observe a model's token usage, and pretending
    -- otherwise would make these numbers evidence when they are testimony.
    -- They are stored because they are the only thing you cannot reconstruct
    -- afterwards, and because "did the cheaper model do these worse" is the
    -- question a fleet operator actually has.
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost REAL,
    -- What the worker says it is: "claude-opus-5", "gpt-5.4", "a bash script".
    -- Declared, never detected -- nothing here can verify it, and pretending
    -- otherwise would make it evidence when it is only a label. It earns its
    -- column because it is the one thing you cannot reconstruct afterwards:
    -- which model did these forty units, and were they faster or worse.
    model TEXT,
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
    skills: tuple[str, ...] = ()
    skill_records: tuple[dict, ...] = ()
    mcp: dict[str, str] | None = None
    context: str = ""

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
        req = []
        if self.skills:
            # Name, version and where to get it. A bare name tells a worker
            # what to blame, not what to load.
            for rec in (self.skill_records or
                        tuple({"name": n} for n in self.skills)):
                bits = [rec["name"]]
                if rec.get("version"):
                    bits.append(f"v{rec['version']}")
                if rec.get("digest"):
                    bits.append(f"[{rec['digest']}]")
                line = "  - " + " ".join(bits)
                if rec.get("source"):
                    line += f"\n      from: {rec['source']}"
                if rec.get("unregistered"):
                    line += "\n      (not in the skill registry — nothing "
                    line += "records where to get this)"
                req.append(line)
            req.insert(0, "skills:")
        if self.mcp:
            req.append("MCP servers: " + ", ".join(
                f"{k} ({v})" for k, v in self.mcp.items()))
        if req:
            # Stated as a requirement, not a suggestion. A worker that cannot
            # load these should fail the unit with that reason -- a unit done
            # without its tools is worse than one left undone, because it looks
            # finished.
            parts.append("\nYOU MUST HAVE\n" + "\n".join(req)
                         + "\nIf any of these is unavailable, call fail with "
                           "that as the reason. Do not improvise a substitute.")
        if self.tools:
            parts.append(f"\nUSE\n{self.tools}")
        if self.context:
            parts.append(f"\nCONTEXT\n{self.context}")
        if self.meta:
            # Substituted into the instructions but never shown, so a caller
            # who puts a count or a size in meta had no way to surface it. The
            # first real fleet had units of 2 to 24 claims and nothing said so.
            shown = {k: v for k, v in self.meta.items()
                     if isinstance(v, (str, int, float, bool))}
            if shown:
                parts.append("\nABOUT THIS UNIT\n" + "\n".join(
                    f"  {k}: {v}" for k, v in shown.items()))
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


#: Created AFTER the migration, never inside SCHEMA. An index names a column,
#: and on a database written by an older version that column does not exist
#: yet -- so `CREATE INDEX ... ON unit(run_id)` fails before the ALTER that
#: would have added it. Splitting them makes the order explicit instead of
#: depending on where in one script a statement happens to sit.
INDEXES = """
CREATE INDEX IF NOT EXISTS unit_pick ON unit(kind, status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS unit_lease ON unit(status, leased_until);
CREATE INDEX IF NOT EXISTS unit_run ON unit(run_id, status);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to a file written by an older version.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists,
    so a new column is invisible to every database created before it. The
    failure is an OperationalError deep inside a query, on someone else's
    machine, with a file they cannot easily recreate.
    """
    for table, cols in (("unit", (("claimed_at", "REAL"), ("result", "TEXT"),
                                  ("run_id", "TEXT"), ("model", "TEXT"),
                                  ("skills_used", "TEXT"), ("tokens_in", "INTEGER"),
                                  ("tokens_out", "INTEGER"), ("cost", "REAL"),
                                  ("kind_digest", "TEXT"))),
                        ("kind", (("skills", "TEXT"), ("mcp", "TEXT"),
                                  ("context", "TEXT")))):
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
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
    conn.executescript(INDEXES)
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
           tools: str | None = None, skills: list[str] | None = None,
           mcp: dict[str, str] | None = None, context: str | None = None,
           force: bool = False) -> str:
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
               skills=["xrad-extraction"],
               mcp={"xrad": "xrad serve --db graph.db"},
               context=Path("ontology/glossary.md").read_text())

    `skills` and `mcp` say what a worker must HAVE; `instructions` says what it
    must DO. They are opaque strings -- a skill name means nothing here, which
    is what keeps this library agnostic about which agent runtime you use --
    but they arrive as structured fields so a spawner can act on them and a
    worker can refuse a unit it is not equipped for.

    `context` is read-only material every worker of this kind receives. It is
    set here and never written by a worker: see docs/concepts.md for why
    worker-to-worker state is refused.

    Re-defining a kind replaces it. Instructions are read at claim time, so a
    correction reaches every worker that has not yet claimed, without
    restarting anything.
    """
    sk = json.dumps(list(skills)) if skills else None
    mc = json.dumps(mcp) if mcp else None
    digest = kind_digest(kind, instructions, done_when, returns, tools, sk, mc,
                         context)

    prev = conn.execute("SELECT * FROM kind WHERE kind = ?", (kind,)).fetchone()
    if prev is not None and not force:
        was = kind_digest(kind, prev["instructions"], prev["done_when"],
                          prev["returns"], prev["tools"], prev["skills"],
                          prev["mcp"], prev["context"])
        if was != digest:
            open_, leased = outstanding(conn, kind=kind)
            if open_ or leased:
                # The most damaging operation had no guard on it. Two sessions
                # sharing a database and both defining `extract` clobbered each
                # other mid-run: nothing errored, and the remaining units
                # quietly carried the other session's instructions.
                raise ValueError(
                    f"kind {kind!r} has {open_} waiting and {leased} in flight, "
                    f"and this changes its definition.\n"
                    f"  Units already claimed keep the text they were given; "
                    f"units claimed after this would get the new text.\n"
                    f"  Either finish or cancel them first, or pass force=True "
                    f"if that is what you mean.")

    conn.execute(
        "INSERT OR IGNORE INTO kind_version (digest, kind, instructions, "
        "done_when, returns, tools, skills, mcp, context, first_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (digest, kind, instructions, done_when, returns, tools, sk, mc, context,
         time.time()))
    conn.execute(
        "INSERT OR REPLACE INTO kind (kind, instructions, done_when, returns, "
        "tools, skills, mcp, context, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (kind, instructions, done_when, returns, tools, sk, mc, context,
         time.time()))
    conn.commit()
    return digest


def spec(conn: sqlite3.Connection, kind: str) -> dict | None:
    """What a kind of work is, unrendered. `None` if it was never defined."""
    r = conn.execute("SELECT * FROM kind WHERE kind = ?", (kind,)).fetchone()
    return dict(r) if r else None


def _to_unit(r: sqlite3.Row, spec_row: sqlite3.Row | None = None,
             pinned=()) -> Unit:
    meta = json.loads(r["meta"] or "{}")
    sp = spec_row or {}
    return Unit(r["unit_id"], r["kind"], r["name"], r["attempts"],
                r["leased_until"] or 0.0, meta,
                _render(sp["instructions"] if sp else "", r["name"], meta),
                _render(sp["done_when"] if sp else "", r["name"], meta),
                _render(sp["returns"] if sp else "", r["name"], meta),
                _render(sp["tools"] if sp else "", r["name"], meta),
                tuple(json.loads(sp["skills"])) if sp and sp["skills"] else (),
                tuple(pinned) if pinned else (
                    tuple(json.loads(r["skills_used"])) if r["skills_used"] else ()),
                json.loads(sp["mcp"]) if sp and sp["mcp"] else None,
                _render(sp["context"] if sp else "", r["name"], meta))


def unit_id(kind: str, name: str, run: str | None = None) -> str:
    """The key a unit is stored under.

    Scoped by run when there is one. Without the scope, a second run over the
    same corpus would find every unit already done and do nothing — while
    re-running your enumeration *within* a run must still add nothing new.
    Both are wanted, and they are only compatible if the run is part of the key.
    """
    return f"{run}/{kind}:{name}" if run else f"{kind}:{name}"


def add(conn: sqlite3.Connection, kind: str, names: list[str], *,
        priority: int = 0, meta: dict | None = None,
        run: str | None = None) -> int:
    """Enqueue units of one kind. Returns how many were new.

    Idempotent on `(run, kind, name)`, so re-running your enumeration after the
    corpus grew adds the new units and leaves the finished ones finished. That
    is the common case and it should not need a flag.
    """
    now = time.time()
    payload = json.dumps(meta or {})
    before = conn.execute("SELECT count(*) FROM unit").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO unit "
        "(unit_id, kind, name, run_id, status, priority, meta, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(unit_id(kind, n, run), kind, n, run, OPEN, priority, payload, now, now)
         for n in names])
    conn.commit()
    return conn.execute("SELECT count(*) FROM unit").fetchone()[0] - before


def claim(conn: sqlite3.Connection, kind: str | None = None, *,
          worker: str | None = None, lease: float = DEFAULT_LEASE,
          n: int = 1, max_attempts: int = MAX_ATTEMPTS,
          run: str | None = None, model: str | None = None) -> list[Unit]:
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
    where, params = "", []
    if kind:
        where, params = where + " AND kind = ?", [*params, kind]
    if run:
        where, params = where + " AND run_id = ?", [*params, run]
    conn.execute(
        f"""UPDATE unit
               SET status = ?, worker = ?, model = ?, leased_until = ?,
                   lease_token = ?, claimed_at = ?, attempts = attempts + 1,
                   updated_at = ?
             WHERE unit_id IN (
                   SELECT unit_id FROM unit
                    WHERE status = ? {where}
                    ORDER BY priority DESC, created_at
                    LIMIT ?)""",
        [LEASED, worker, model, now + lease, token, now, now, OPEN, *params, n])
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM unit WHERE lease_token = ? ORDER BY priority DESC, created_at",
        (token,)).fetchall()
    # One lookup per distinct kind, not per unit: a batch of 20 is almost
    # always 20 units of the same kind.
    specs = {k: conn.execute("SELECT * FROM kind WHERE kind = ?", (k,)).fetchone()
             for k in {r["kind"] for r in rows}}
    # Pin what the skills were AT CLAIM TIME, and hand the same records to the
    # caller. Reading them back out of the row would return the value the row
    # had before this update, and looking them up later would report whatever
    # they are now — which is the question this exists to answer when someone
    # edits a skill halfway through a run.
    pinned: dict[str, list[dict]] = {}
    digests: dict[str, str | None] = {}
    for k, sp in specs.items():
        names = json.loads(sp["skills"]) if sp and sp["skills"] else []
        pinned[k] = resolve_skills(conn, names) if names else []
        # Pin the DEFINITION too, not only the skills. A kind redefined
        # mid-run otherwise leaves no record of what any unit was told, which
        # is exactly the question you have when one unit's output looks wrong.
        digests[k] = kind_digest(
            k, sp["instructions"], sp["done_when"], sp["returns"], sp["tools"],
            sp["skills"], sp["mcp"], sp["context"]) if sp else None
    for r in rows:
        conn.execute(
            "UPDATE unit SET skills_used = ?, kind_digest = ? WHERE unit_id = ?",
            (json.dumps(pinned[r["kind"]]) if pinned.get(r["kind"]) else None,
             digests.get(r["kind"]), r["unit_id"]))
    conn.commit()
    return [_to_unit(r, specs.get(r["kind"]), pinned.get(r["kind"]) or ())
            for r in rows]


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
           then: dict[str, list[str]] | None = None,
           tokens_in: int | None = None, tokens_out: int | None = None,
           cost: float | None = None) -> bool:
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
    if ok and any(v is not None for v in (tokens_in, tokens_out, cost)):
        conn.execute(
            "UPDATE unit SET tokens_in = ?, tokens_out = ?, cost = ? "
            "WHERE unit_id = ?", (tokens_in, tokens_out, cost, unit_id))
        conn.commit()
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


def progress(conn: sqlite3.Connection, kind: str | None = None, *,
             run: str | None = None) -> dict:
    """Counts by kind and status. For a status line, and for deciding to stop."""
    clauses, args = [], []
    if kind:
        clauses.append("kind = ?")
        args.append(kind)
    if run:
        clauses.append("run_id = ?")
        args.append(run)
    q = ("SELECT kind, status, count(*) AS n FROM unit "
         + ("WHERE " + " AND ".join(clauses) + " " if clauses else "")
         + "GROUP BY kind, status")
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute(q, args):
        out.setdefault(r["kind"], dict.fromkeys(STATUSES, 0))
        out[r["kind"]][r["status"]] = r["n"]
    return out


def leased(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Who is holding what, and for how much longer. The fleet, visible."""
    reclaim(conn)
    return conn.execute(
        "SELECT * FROM unit WHERE status = ? ORDER BY leased_until", (LEASED,)
    ).fetchall()


def iter_results(conn: sqlite3.Connection, kind: str | None = None, *,
                 run: str | None = None, status: str | tuple[str, ...] = DONE,
                 flat: bool = False):
    """Stream what the fleet produced, one row at a time.

    A generator rather than a list because a finished corpus can be hundreds of
    thousands of units, and materialising all of them to print them is the
    difference between a command that works and one that gets killed.

    Each row carries what you would want when analysing the output and cannot
    reconstruct later: which model produced it, which worker, what it cost, how
    long it took, and how many attempts it needed.
    """
    want = (status,) if isinstance(status, str) else tuple(status)
    clauses = [f"status IN ({','.join('?' * len(want))})"]
    args: list = list(want)
    for col, val in (("kind", kind), ("run_id", run)):
        if val:
            clauses.append(f"{col} = ?")
            args.append(val)
    q = ("SELECT * FROM unit WHERE " + " AND ".join(clauses)
         + " ORDER BY updated_at")
    for r in conn.execute(q, args):
        result = json.loads(r["result"]) if r["result"] else None
        row = {
            "kind": r["kind"], "name": r["name"], "unit_id": r["unit_id"],
            "run": r["run_id"], "status": r["status"],
            "worker": r["worker"], "model": r["model"],
            "attempts": r["attempts"], "note": r["note"],
            "cost": r["cost"], "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "seconds": ((r["updated_at"] - r["claimed_at"])
                        if r["claimed_at"] and r["updated_at"] else None),
            "finished_at": r["updated_at"],
        }
        if not flat:
            yield {**row, "result": result}
            continue
        # Flat: the result's own keys come up to the top level, so `jq .claims`
        # works instead of `jq .result.claims`. The envelope always wins a
        # collision, because a row whose `name` silently became something the
        # worker returned is a row you cannot join on. The shadowed value is
        # kept rather than dropped.
        merged = dict(result) if isinstance(result, dict) else {}
        shadowed = {f"result_{k}": merged[k] for k in merged if k in row}
        merged.update(row)
        merged.update(shadowed)
        if result is not None and not isinstance(result, dict):
            merged["result"] = result
        yield merged


def results(conn: sqlite3.Connection, kind: str | None = None, *,
            run: str | None = None) -> list[dict]:
    """Every finished unit's output, as a list. See `iter_results` to stream."""
    return list(iter_results(conn, kind, run=run))


def failures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Units no worker could finish, with the note saying why."""
    return conn.execute(
        "SELECT * FROM unit WHERE status = ? ORDER BY updated_at", (FAILED,)
    ).fetchall()


def stats(conn: sqlite3.Connection, *, buckets: int = 40,
          run: str | None = None) -> dict:
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
    rows = conn.execute(
        "SELECT * FROM unit" + (" WHERE run_id = ?" if run else ""),
        (run,) if run else ()).fetchall()

    by_kind: dict[str, dict[str, int]] = {}
    done_times: list[float] = []
    durations: list[float] = []
    per_worker: dict[str, dict] = {}
    per_model: dict[str, dict] = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], dict.fromkeys(STATUSES, 0))
        k[r["status"]] += 1
        if r["status"] == DONE:
            done_times.append(r["updated_at"] or now)
            if r["claimed_at"]:
                durations.append(max(0.0, (r["updated_at"] or now) - r["claimed_at"]))
        if r["model"] and r["status"] in (DONE, FAILED):
            m = per_model.setdefault(r["model"], {
                "model": r["model"], "done": 0, "failed": 0, "seconds": 0.0,
                "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "priced": 0})
            m["done" if r["status"] == DONE else "failed"] += 1
            if r["claimed_at"] and r["status"] == DONE:
                m["seconds"] += max(0.0, (r["updated_at"] or now) - r["claimed_at"])
            m["tokens_in"] += r["tokens_in"] or 0
            m["tokens_out"] += r["tokens_out"] or 0
            if r["cost"] is not None:
                m["cost"] += r["cost"]
                # Counted separately so a per-unit average is over the units
                # that actually reported, not over everything.
                m["priced"] += 1
        if r["worker"] and r["status"] in (DONE, FAILED):
            w = per_worker.setdefault(r["worker"], {"worker": r["worker"],
                                                    "done": 0, "failed": 0,
                                                    "seconds": 0.0})
            w["done" if r["status"] == DONE else "failed"] += 1
            if r["claimed_at"] and r["status"] == DONE:
                w["seconds"] += max(0.0, (r["updated_at"] or now) - r["claimed_at"])

    totals = {s: sum(k[s] for k in by_kind.values()) for s in STATUSES}
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
        "run": run,
        "by_kind": by_kind,
        "totals": totals | {"all": len(rows), "left": left},
        "workers": [
            {"worker": r["worker"], "model": r["model"], "name": r["name"],
             "kind": r["kind"],
             "seconds_left": round(max(0.0, (r["leased_until"] or 0) - now)),
             "seconds_held": round(now - r["claimed_at"]) if r["claimed_at"] else None,
             "attempts": r["attempts"]}
            for r in rows if r["status"] == LEASED],
        "throughput": series,
        "duration": {"n": len(ds), "p50": pct(0.5), "p95": pct(0.95),
                     "max": ds[-1] if ds else None},
        "per_worker": sorted(per_worker.values(), key=lambda w: -w["done"]),
        "per_model": sorted(per_model.values(), key=lambda m: -m["done"]),
        "failures": [{"name": r["name"], "kind": r["kind"], "note": r["note"],
                      "attempts": r["attempts"]}
                     for r in rows if r["status"] == FAILED],
        "retried": sum(1 for r in rows if r["attempts"] > 1),
        "cost": {
            "total": sum(r["cost"] or 0.0 for r in rows),
            "tokens_in": sum(r["tokens_in"] or 0 for r in rows),
            "tokens_out": sum(r["tokens_out"] or 0 for r in rows),
            # How much of the run reported at all. A total over 3 of 400 units
            # is a number that will be quoted as if it were the run's cost.
            "priced": sum(1 for r in rows if r["cost"] is not None),
            "units": len(rows),
        },
        "units_per_min": round(per_sec * 60, 1),
        "eta_seconds": round(left / per_sec) if per_sec and left else None,
    }


#: The worker loop, as a prompt. Kept here rather than in the docs because a
#: template that lives in prose gets copied, edited, and drifts — and the copy
#: that drifts is the one running your fleet at 3am.
WORKER_PROMPT = """You are ONE WORKER IN A FLEET. Other agents are working this \
same queue right now. Your worker name is `$worker`.

You have not been told what the work is. That is deliberate — the queue will
tell you.$requires

STEP 1 — claim a unit:
    $claim_cmd
  If that exits non-zero, THE QUEUE IS EMPTY. It prints nothing on stdout and
  a short note on stderr. Stop immediately and report. Do NOT invent work. Do
  NOT go looking for things to process on your own.

STEP 2 — the output IS your assignment. It says what to do, what counts as
  done, and the exact shape to hand back. Do exactly that and nothing more.

STEP 3 — report the result:
    $done_cmd
  If the result is large, write it to a file and use --result-file instead:
  a single shell argument is capped at 128 KB on Linux.
  If that says the lease expired, do not argue — claim a different unit.

STEP 4 — go back to STEP 1.

RULES
- Never work on a unit you did not claim.
- Never claim a second unit before finishing the first.
- Stop the moment the queue is empty.

Your final message: how many units you completed, and their names."""


def worker_prompt(conn: sqlite3.Connection, kind: str | None = None, *,
                  db: str = "work.db", worker: str = "agent-1",
                  lease: float = DEFAULT_LEASE) -> str:
    """The prompt to spawn a worker with, generated from the kind.

    The template is deliberately generic about the *task* — that comes from the
    queue at claim time — but specific about what the worker must HAVE, because
    a skill it never loaded is not something it can discover halfway through.

    Generated rather than copied. A prompt pasted out of documentation drifts
    from the kind it was written for, and nothing tells you when it has.
    """
    sp = spec(conn, kind) if kind else None
    req = ""
    if sp:
        bits = []
        if sp.get("skills"):
            bits.append("- load these skills first: "
                        + ", ".join(json.loads(sp["skills"])))
        if sp.get("mcp"):
            bits.append("- these MCP servers must be configured: "
                        + ", ".join(f"{k} ({v})"
                                    for k, v in json.loads(sp["mcp"]).items()))
        if bits:
            req = ("\n\nBEFORE YOU CLAIM ANYTHING\n" + "\n".join(bits)
                   + "\nIf you cannot, say so and stop. Do not start work you "
                     "are not equipped for.")
    k = f" {kind}" if kind else ""
    return Template(WORKER_PROMPT).safe_substitute(
        worker=worker, requires=req,
        claim_cmd=f"superagentic claim{k} --db {db} --brief "
                  f"--worker {worker} --model '<which model you are>' "
                  f"--lease {lease:g}",
        done_cmd=f"superagentic finish <unit_id> --db {db} --worker {worker} "
                 f"--result '<the JSON the brief asked for>'")


def start_run(conn: sqlite3.Connection, *, label: str | None = None,
              started_by: str | None = None, note: str | None = None,
              run_id: str | None = None) -> str:
    """Begin a run, and get the id to enqueue into.

    An orchestrator calls this once before spawning anything. Everything it
    enqueues with that id belongs to the run, and every statistic can then be
    asked of that run rather than of the whole file.

    The default id is a timestamp so runs sort chronologically by id alone,
    which is what you want when reading a list of them. Pass `run_id` when you
    have a better handle -- a CI build number, a ticket.

    There is deliberately no `end_run`. A run is over when its units are, and
    that has to be derivable, because the orchestrator is exactly the process
    most likely to have died.
    """
    now = time.time()
    if run_id is None:
        # Deterministic and sortable. Seconds are not enough: two runs started
        # in the same second by a script would collide and silently merge.
        run_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + \
            f"-{uuid.uuid4().hex[:4]}"
    conn.execute(
        "INSERT OR REPLACE INTO run (run_id, label, started_by, note, started_at) "
        "VALUES (?,?,?,?,?)",
        (run_id, label, started_by or this_worker(), note, now))
    conn.commit()
    return run_id


def runs(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    """Every run, newest first, with what it did.

    One query with a join rather than a `stats()` call per run: a list of forty
    runs would otherwise be forty passes over the unit table, and this is the
    view a dashboard polls.
    """
    now = time.time()
    rows = conn.execute("""
        SELECT r.run_id, r.label, r.started_by, r.note, r.started_at,
               count(u.unit_id) AS units,
               sum(u.status = 'done')   AS done,
               sum(u.status = 'failed') AS failed,
               sum(u.status = 'open')   AS open,
               sum(u.status = 'leased') AS leased,
               sum(u.attempts > 1)      AS retried,
               max(u.updated_at)        AS last,
               count(DISTINCT u.kind)   AS kinds,
               count(DISTINCT CASE WHEN u.status IN ('done','failed')
                                   THEN u.worker END) AS workers,
               sum(CASE WHEN u.status = 'done' AND u.claimed_at IS NOT NULL
                        THEN u.updated_at - u.claimed_at END) AS busy,
               sum(u.cost) AS cost, sum(u.tokens_in) AS tin,
               sum(u.tokens_out) AS tout,
               sum(u.cost IS NOT NULL) AS priced
          FROM run r LEFT JOIN unit u ON u.run_id = r.run_id
         GROUP BY r.run_id ORDER BY r.started_at DESC LIMIT ?""", (limit,))
    out = []
    for r in rows:
        left = (r["open"] or 0) + (r["leased"] or 0)
        out.append({
            "run_id": r["run_id"], "label": r["label"],
            "started_by": r["started_by"], "note": r["note"],
            "started_at": r["started_at"],
            "units": r["units"] or 0, "done": r["done"] or 0,
            "failed": r["failed"] or 0, "left": left,
            "retried": r["retried"] or 0, "kinds": r["kinds"] or 0,
            "workers": r["workers"] or 0,
            "running": left > 0,
            # Wall-clock from the start to the last thing that moved. While a
            # run is live that is "so far"; once it is over it is the duration.
            "elapsed": (r["last"] or now) - r["started_at"] if r["started_at"] else None,
            # Worker-seconds actually spent. Against elapsed it says how much
            # parallelism you really got, which is the number that tells you
            # whether more workers would have helped.
            "busy": r["busy"] or 0.0,
            "cost": r["cost"] or 0.0,
            "tokens_in": r["tin"] or 0,
            "tokens_out": r["tout"] or 0,
            "priced": r["priced"] or 0,
        })
    return out


def run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """One run's record, without its statistics. `None` if there is no such run."""
    r = conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def units(conn: sqlite3.Connection, *, run: str | None = None,
          kind: str | None = None, status: str | None = None,
          q: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    """Individual units, for looking at rather than counting.

    Everything else here aggregates: totals, percentiles, per-worker rollups.
    None of it answers "what happened to page 189", which is the question
    someone actually has when a result looks wrong.

    Bounded by `limit` and it says so in the return, because a queue can hold
    a hundred thousand units and a view that silently shows the first three
    hundred of them is a view that lies.
    """
    where, args = [], []
    for col, val in (("run_id", run), ("kind", kind), ("status", status)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if q:
        where.append("(name LIKE ? OR worker LIKE ? OR note LIKE ? "
                     "OR model LIKE ?)")
        args += [f"%{q}%"] * 4
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT count(*) FROM unit{clause}", args).fetchone()[0]
    rows = conn.execute(
        # Unfinished first, then most recently touched: the rows worth looking
        # at are the ones still moving and the ones that just broke.
        f"SELECT * FROM unit{clause} ORDER BY "
        "CASE status WHEN 'leased' THEN 0 WHEN 'failed' THEN 1 "
        "WHEN 'open' THEN 2 ELSE 3 END, updated_at DESC LIMIT ? OFFSET ?",
        [*args, limit, offset]).fetchall()
    now = time.time()
    return {
        "total": total,
        "shown": len(rows),
        "offset": offset,
        "limit": limit,
        "pages": max(1, -(-total // limit)) if limit else 1,
        "page": offset // limit + 1 if limit else 1,
        "truncated": total > len(rows),
        "units": [{
            "unit_id": r["unit_id"], "kind": r["kind"], "name": r["name"],
            "run_id": r["run_id"], "status": r["status"], "worker": r["worker"],
            "model": r["model"], "cost": r["cost"],
            "kind_digest": r["kind_digest"],
            "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
            "attempts": r["attempts"], "note": r["note"],
            "result": json.loads(r["result"]) if r["result"] else None,
            "updated_at": r["updated_at"],
            "seconds": (
                (r["updated_at"] - r["claimed_at"])
                if r["claimed_at"] and r["status"] in (DONE, FAILED)
                else (now - r["claimed_at"]) if r["claimed_at"]
                and r["status"] == LEASED else None),
            "lease_left": (max(0.0, (r["leased_until"] or 0) - now)
                           if r["status"] == LEASED else None),
        } for r in rows],
    }


def register_skill(conn: sqlite3.Connection, name: str, *,
                   source: str | None = None, version: str | None = None,
                   note: str | None = None, content: str | None = None) -> dict:
    """Say what a skill name means, once.

    `source` is where a worker gets it — a path, a URL, or a sentence. If it is
    a readable file and no `content` was passed, the file is read and hashed,
    so `version` stops being the only thing standing between you and "which of
    these did the run actually use".

    **Nothing is fetched or installed.** Distribution belongs to whatever runs
    your agents; the moment this downloads a skill it has to know about Claude
    Code's `.claude/skills`, Cursor's rules, and every runtime after them. It
    records where a skill is and what it hashed to. Putting it in place is the
    runtime's job, and the brief tells the worker to fail rather than proceed
    without it.
    """
    if content is None and source:
        f = Path(source)
        try:
            if f.is_file():
                content = f.read_text(encoding="utf-8")
        except OSError:
            content = None
    digest = (hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
              if content else None)
    conn.execute(
        "INSERT OR REPLACE INTO skill (name, source, version, digest, note, "
        "updated_at) VALUES (?,?,?,?,?,?)",
        (name, source, version, digest, note, time.time()))
    conn.commit()
    return {"name": name, "source": source, "version": version, "digest": digest}


def skills(conn: sqlite3.Connection) -> list[dict]:
    """Every registered skill, with how much work has actually used it.

    The usage count comes from what units pinned at claim time, not from which
    kinds mention it — a skill named by a kind nobody ever ran has been used
    zero times, and saying otherwise would be the sort of number that quietly
    justifies keeping something.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM skill ORDER BY name")]
    used: dict[str, int] = {}
    for (raw,) in conn.execute(
            "SELECT skills_used FROM unit WHERE skills_used IS NOT NULL"):
        for rec in json.loads(raw):
            used[rec.get("name")] = used.get(rec.get("name"), 0) + 1
    for r in rows:
        r["units"] = used.get(r["name"], 0)
    # Skills that units pinned but nobody registered: worth surfacing, not
    # hiding, because it means a kind names something undefined.
    for name, n in sorted(used.items()):
        if not any(r["name"] == name for r in rows):
            rows.append({"name": name, "source": None, "version": None,
                         "digest": None, "note": None, "units": n,
                         "unregistered": True})
    return rows


def resolve_skills(conn: sqlite3.Connection, names) -> list[dict]:
    """Skill names to their records, keeping the order the kind declared."""
    out = []
    for n in names or ():
        r = conn.execute("SELECT * FROM skill WHERE name = ?", (n,)).fetchone()
        out.append(dict(r) if r else {"name": n, "source": None,
                                      "version": None, "digest": None,
                                      "unregistered": True})
    return out


def cancel(conn: sqlite3.Connection, *, run: str | None = None,
           kind: str | None = None, names: list[str] | None = None,
           now: bool = False) -> dict:
    """Stop work that has not started, and optionally take back what has.

    By default this cancels only `open` units and leaves whatever is in flight
    to finish. That is almost always what someone wants: the fleet is running
    the wrong thing, and half-finished work is still work. `now=True` also
    takes back leased units, and the workers holding them discover it the way
    they discover any lost lease, because `finish` returns False.

    Cancelled is a status, not a deletion. A queue that forgets what you
    cancelled cannot answer why a run came up short three weeks later.
    """
    where, args = ["status IN (?, ?)" if now else "status = ?"], []
    args += [OPEN, LEASED] if now else [OPEN]
    for col, val in (("run_id", run), ("kind", kind)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if names:
        where.append(f"name IN ({','.join('?' * len(names))})")
        args += list(names)
    cur = conn.execute(
        f"UPDATE unit SET status=?, worker=NULL, leased_until=NULL, "
        f"lease_token=NULL, note=COALESCE(note, 'cancelled'), updated_at=? "
        f"WHERE {' AND '.join(where)}",
        [CANCELLED, time.time(), *args])
    conn.commit()
    return {"cancelled": cur.rowcount}


def retry(conn: sqlite3.Connection, *, run: str | None = None,
          kind: str | None = None, names: list[str] | None = None,
          include_cancelled: bool = False) -> dict:
    """Put failed units back in the queue, with their attempts reset.

    The normal shape of a day: a run finishes, three units failed, you find the
    bug, and you want to re-run those three and nothing else. Without this the
    only route is editing SQLite by hand, because `attempts` is already at the
    limit and `claim` will never offer them again.

    Attempts go back to zero rather than up by one. The unit failed under the
    old code; carrying its history forward would retire it again after one
    more try, which is precisely wrong when the thing that changed is the fix.
    The note is kept, because why it failed last time is still worth reading.
    """
    statuses = [FAILED, CANCELLED] if include_cancelled else [FAILED]
    where = [f"status IN ({','.join('?' * len(statuses))})"]
    args: list = list(statuses)
    for col, val in (("run_id", run), ("kind", kind)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    if names:
        where.append(f"name IN ({','.join('?' * len(names))})")
        args += list(names)
    cur = conn.execute(
        f"UPDATE unit SET status=?, attempts=0, worker=NULL, leased_until=NULL, "
        f"lease_token=NULL, updated_at=? WHERE {' AND '.join(where)}",
        [OPEN, time.time(), *args])
    conn.commit()
    return {"retrying": cur.rowcount}


def outstanding(conn: sqlite3.Connection, *, run: str | None = None,
                kind: str | None = None) -> tuple[int, int]:
    """(not started, in flight). A run is over when both are zero."""
    where, args = [], []
    for col, val in (("run_id", run), ("kind", kind)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    clause = (" AND " + " AND ".join(where)) if where else ""
    row = conn.execute(
        f"SELECT sum(status = ?) AS o, sum(status = ?) AS l FROM unit "
        f"WHERE 1=1{clause}", [OPEN, LEASED, *args]).fetchone()
    return (row["o"] or 0), (row["l"] or 0)


def kind_digest(kind: str, instructions: str, done_when, returns, tools,
                skills, mcp, context) -> str:
    """A stable hash of everything a worker is told, so a change is detectable.

    Every field that reaches the brief goes in. A definition that differs only
    in a field nobody reads would otherwise look like a change, and one that
    differs in a field workers DO read must never look unchanged.
    """
    blob = "\x00".join(str(x) for x in
                       (kind, instructions, done_when, returns, tools, skills,
                        mcp, context))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def kind_versions(conn: sqlite3.Connection, kind: str | None = None) -> list[dict]:
    """Every definition a kind has had, with how many units ran under each."""
    q = "SELECT * FROM kind_version" + (" WHERE kind = ?" if kind else "") \
        + " ORDER BY kind, first_seen"
    rows = [dict(r) for r in conn.execute(q, (kind,) if kind else ())]
    used: dict[str, int] = {}
    for (d,) in conn.execute(
            "SELECT kind_digest FROM unit WHERE kind_digest IS NOT NULL"):
        used[d] = used.get(d, 0) + 1
    for r in rows:
        r["units"] = used.get(r["digest"], 0)
    return rows


def brief_for(conn: sqlite3.Connection, unit_id: str) -> str | None:
    """Exactly what one unit was told, re-derived from what it was pinned to.

    This is the whole point of the pin. After a kind is redefined, `spec()`
    reports what the kind says NOW, which is precisely the wrong answer when
    you are asking why a particular unit produced what it did.
    """
    r = conn.execute("SELECT * FROM unit WHERE unit_id = ?", (unit_id,)).fetchone()
    if r is None:
        return None
    sp = None
    if r["kind_digest"]:
        sp = conn.execute("SELECT * FROM kind_version WHERE digest = ?",
                          (r["kind_digest"],)).fetchone()
    if sp is None:                       # never claimed, or claimed before pins
        sp = conn.execute("SELECT * FROM kind WHERE kind = ?",
                          (r["kind"],)).fetchone()
    pinned = json.loads(r["skills_used"]) if r["skills_used"] else ()
    return _to_unit(r, sp, pinned).brief()


def state(conn: sqlite3.Connection, *, stale_after: float = 0.0) -> dict:
    """Where this project is, for something that has just arrived.

    A new session knows nothing: not which runs exist, not which are still
    going, not what broke. `status` answers "how many units of each kind",
    which is the second question. This is the first one, and it ends with the
    literal next command rather than leaving that to be inferred.

    Everything here is a fact plus what to do about it. A summary that reports
    three failures without saying `superagentic retry` has moved the work of
    knowing the tool onto whoever is reading, which for a fresh agent is the
    whole problem.
    """
    now = time.time()
    reclaim(conn)
    rs = runs(conn, limit=10)
    live = [r for r in rs if r["running"]]
    prog = progress(conn)
    totals = {s: sum(p[s] for p in prog.values()) for s in STATUSES}
    ungrouped = conn.execute(
        "SELECT count(*) FROM unit WHERE run_id IS NULL").fetchone()[0]

    attention: list[dict] = []
    if totals[FAILED]:
        bad = failures(conn)[:3]
        attention.append({
            "what": f"{totals[FAILED]} unit(s) no worker could finish",
            "detail": "; ".join(f"{b['name']}: {b['note'] or '?'}" for b in bad),
            "do": "superagentic retry --all   # after fixing the cause",
        })
    held = leased(conn)
    stuck = [h for h in held
             if h["claimed_at"] and now - h["claimed_at"] > (stale_after or 900)]
    if stuck:
        attention.append({
            "what": f"{len(stuck)} unit(s) held for a long time",
            "detail": ", ".join(f"{h['name']} by {h['worker']}" for h in stuck[:3]),
            "do": "superagentic status --who   # the workers may be gone",
        })
    changed = []
    for sk in skills(conn):
        src = sk.get("source")
        if not (src and sk.get("digest")):
            continue
        f = Path(src)
        if f.is_file():
            now_digest = hashlib.sha256(
                f.read_text(encoding="utf-8").encode()).hexdigest()[:16]
            if now_digest != sk["digest"]:
                changed.append(sk["name"])
    if changed:
        attention.append({
            "what": f"{len(changed)} skill(s) changed since registration",
            "detail": ", ".join(changed),
            "do": "superagentic skill-check",
        })
    unregistered = [s["name"] for s in skills(conn) if s.get("unregistered")]
    if unregistered:
        attention.append({
            "what": f"{len(unregistered)} skill(s) required but never registered",
            "detail": ", ".join(unregistered),
            "do": "superagentic skill <name> --source FILE",
        })

    # The single next thing. Ordered by what actually blocks progress.
    if not prog:
        nxt = ("superagentic init && superagentic apply"
               if not conn.execute("SELECT count(*) FROM kind").fetchone()[0]
               else "superagentic add <kind> --from-file units.txt --run <run>")
    elif live:
        r = live[0]
        nxt = (f"superagentic wait --run {r['run_id']}"
               f"   # {r['left']:,} unit(s) left")
    elif totals[FAILED]:
        nxt = "superagentic retry --all   # after fixing the cause"
    else:
        nxt = "superagentic results <kind> --jsonl --flat   # everything is done"

    return {
        "now": now,
        "runs": rs,
        "live": [r["run_id"] for r in live],
        "totals": totals | {"all": sum(totals.values()), "ungrouped": ungrouped},
        "kinds": sorted(prog),
        "skills": [s["name"] for s in skills(conn)],
        "attention": attention,
        "next": nxt,
    }
