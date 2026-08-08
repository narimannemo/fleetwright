"""`fleetwright` — the command line.

argparse rather than a CLI framework, because this package has no runtime
dependencies and that is worth more than a prettier `--help`. It gets installed
next to whatever the workers actually run, and the least it can do is not have
opinions about their Click version.

Exit codes are the interface here, not the text. `claim` exits 1 with no output
when the queue is dry, so a shell loop terminates on its own:

    while unit=$(fleetwright claim extract --json); do … done
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, config, leases, shape

#: Set this and every command in the shell, and every subagent that inherits
#: the environment, talks to the same file regardless of where it runs.
DB_ENV = "FLEETWRIGHT_DB"


def _looks_like_ours(path: Path) -> bool:
    """Is this a fleetwright database, as opposed to somebody else's SQLite?"""
    import sqlite3 as _s
    from contextlib import closing
    try:
        with closing(_s.connect(f"file:{path}?mode=ro", uri=True)) as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        return {"unit", "kind"} <= names
    except Exception:  # noqa: BLE001 - unreadable means not ours
        return False


def _neighbours(where: Path) -> list[Path]:
    """Other fleetwright databases sitting next to the one that is missing."""
    return [c for c in sorted(where.glob("*.db")) if _looks_like_ours(c)]


def resolve_db(a: argparse.Namespace) -> Path:
    """Which file this command means, and a loud refusal when that is unclear.

    Every "the database reset itself" story is this function's fault, because
    `connect()` creates whatever path it is handed. Two ways to lose a queue,
    both silent, both reported as a perfectly healthy zero units:

        cd sub && fleetwright status      # a SECOND work.db, in sub/
        fleetwright status --db worrk.db  # a THIRD, from one typo

    So: an explicit `--db` is honoured literally, but if it does not exist and
    a real fleetwright database is sitting beside it, that is a typo far more
    often than a new project, and it now says so instead of creating a file.
    The default `work.db` is searched for UP the tree, the way git finds a
    repository, so a subdirectory joins the project instead of starting a
    rival one. `FLEETWRIGHT_DB` overrides both and is what a fleet should use,
    since a subagent inherits the environment.
    """
    explicit = a.db != "work.db"
    if not explicit and os.environ.get(DB_ENV):
        return Path(os.environ[DB_ENV]).expanduser()

    p = Path(a.db).expanduser()
    if p.exists():
        return p

    if explicit:
        # Only when the name is CLOSE to one that exists. "Is there any other
        # database here" was the first rule and it was too blunt: a second
        # queue called audit.db beside work.db is an ordinary thing to want,
        # and refusing it would be a worse bug than the one being fixed.
        # `worrk.db` against `work.db` is a typo; `audit.db` is not.
        near = _neighbours(p.parent if str(p.parent) != "" else Path())
        close = difflib.get_close_matches(
            p.name, [c.name for c in near], n=3, cutoff=0.8)
        if close and not getattr(a, "create", False):
            print(f"no database at {p}", file=sys.stderr)
            print(f"  did you mean {', '.join(close)}?", file=sys.stderr)
            print("  a name this close to an existing database is a typo more "
                  "often than a new project, and creating one silently is how "
                  "a queue appears to have emptied itself.", file=sys.stderr)
            print(f"  pass --create if you do mean a new one, or set {DB_ENV}.",
                  file=sys.stderr)
            raise SystemExit(2)
        return p

    # The default, and nothing here. Walk up, like git looking for .git.
    for d in [Path.cwd(), *Path.cwd().parents]:
        cand = d / "work.db"
        if cand.is_file() and _looks_like_ours(cand):
            return cand
    return p


def _conn(a: argparse.Namespace):
    db = resolve_db(a)
    fresh = not db.exists()
    conn = leases.connect(db)
    if fresh:
        # Say it once, out loud. A new file that appears in silence is
        # indistinguishable from the old one having been emptied.
        print(f"created a new database at {db.resolve()}", file=sys.stderr)
    return conn


def _cmd_skill(a: argparse.Namespace) -> int:
    r = leases.register_skill(_conn(a), a.name, source=a.source,
                              version=a.version, note=a.note)
    print(f"registered {r['name']}"
          + (f" v{r['version']}" if r["version"] else "")
          + (f" [{r['digest']}]" if r["digest"] else ""), flush=True)
    if a.source and not r["digest"]:
        # Not an error: a URL or a sentence is a legitimate source. But then
        # nothing can tell one version of it from another later.
        print("  note: source is not a readable file, so nothing was hashed — "
              "`--version` is all that will distinguish revisions later",
              file=sys.stderr)
    return 0


def _cmd_skill_check(a: argparse.Namespace) -> int:
    """Re-hash a skill's source and compare it to what was registered.

    The brief prints a digest, and until now nothing could confirm the file a
    worker just read hashes to it. A fingerprint you cannot check at the moment
    it matters is decoration.
    """
    import hashlib
    rows = {r["name"]: r for r in leases.skills(_conn(a))}
    names = [a.name] if a.name else [n for n, r in rows.items() if r.get("digest")]
    if a.name and a.name not in rows:
        print(f"{a.name!r} is not registered", file=sys.stderr)
        return 2
    bad = 0
    for n in names:
        r = rows[n]
        if not r.get("source"):
            print(f"  {n:24s} no source recorded")
            continue
        f = Path(r["source"])
        if not f.is_file():
            print(f"  {n:24s} source is not a readable file: {r['source']}")
            continue
        now = hashlib.sha256(f.read_text(encoding="utf-8").encode()).hexdigest()[:16]
        if not r.get("digest"):
            print(f"  {n:24s} registered without a digest; it now hashes to {now}")
        elif now == r["digest"]:
            print(f"  {n:24s} OK      {now}")
        else:
            bad += 1
            print(f"  {n:24s} CHANGED registered {r['digest']}, now {now}")
    if bad:
        print(f"\n{bad} skill(s) changed since registration. Units claimed "
              "before and after used different text; re-register to record the "
              "new version.", file=sys.stderr)
    return 1 if bad else 0


def _cmd_skills(a: argparse.Namespace) -> int:
    rows = leases.skills(_conn(a))
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no skills registered — `fleetwright skill <name> --source FILE`")
        return 0
    w = max(len(r["name"]) for r in rows) + 2
    print(f"{'skill':<{w}}{'version':<10}{'digest':<18}{'units':>7}  source",
          flush=True)
    for r in rows:
        mark = "?" if r.get("unregistered") else " "
        print(f"{mark}{r['name']:<{w - 1}}{(r['version'] or '-'):<10}"
              f"{(r['digest'] or '-'):<18}{r['units']:>7}  {r['source'] or ''}")
    sys.stdout.flush()
    if any(r.get("unregistered") for r in rows):
        print("\n? used by units but never registered — nothing records where "
              "to get it", file=sys.stderr)
    return 0


def _cmd_define(a: argparse.Namespace) -> int:
    instructions = a.instructions
    if a.instructions_file:
        instructions = (sys.stdin.read() if a.instructions_file == "-"
                        else Path(a.instructions_file).read_text(encoding="utf-8"))
    if not instructions:
        print("no instructions — pass --instructions or --instructions-file",
              file=sys.stderr)
        return 2
    mcp = {}
    for spec in (a.mcp or []):
        if "=" not in spec:
            print(f"--mcp must be name=command, got {spec!r}", file=sys.stderr)
            return 2
        name, cmd = spec.split("=", 1)
        mcp[name] = cmd
    context = (Path(a.context).read_text(encoding="utf-8") if a.context else None)
    try:
        digest = leases.define(
            _conn(a), a.kind, instructions, done_when=a.done_when,
            returns=a.returns, tools=a.tools, skills=a.skill or None,
            mcp=mcp or None, context=context,
            max_attempts=a.max_attempts, force=a.force)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"defined {a.kind} [{digest}]")
    if not a.done_when:
        print("  warning: no --done-when. Every worker will decide for itself "
              "what finished means, and they will not agree.", file=sys.stderr)
    return 0


def _cmd_start(a: argparse.Namespace) -> int:
    rid = leases.start_run(_conn(a), label=a.label, started_by=a.by,
                           note=a.note, run_id=a.id)
    # The id alone on stdout, so `RUN=$(fleetwright start ...)` works.
    print(rid)
    print(f"run started: {a.label or rid}", file=sys.stderr)
    return 0


def _cmd_runs(a: argparse.Namespace) -> int:
    rows = leases.runs(_conn(a), limit=a.limit)
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no runs — `fleetwright start --label ...` before enqueueing")
        return 0
    print(f"{'run':22}{'label':26}{'units':>7}{'done':>7}{'failed':>8}"
          f"{'left':>6}{'workers':>8}{'elapsed':>9}")
    for r in rows:
        mark = "*" if r["running"] else " "
        el = r["elapsed"]
        el_s = "—" if el is None else (f"{el:.0f}s" if el < 90 else f"{el/60:.1f}m")
        print(f"{mark}{r['run_id']:21}{(r['label'] or '')[:24]:26}"
              f"{r['units']:>7,}{r['done']:>7,}{r['failed']:>8,}"
              f"{r['left']:>6,}{r['workers']:>8}{el_s:>9}")
    if any(r["running"] for r in rows):
        print("\n* still running", file=sys.stderr)
    return 0


def _cmd_prompt(a: argparse.Namespace) -> int:
    conn = _conn(a)
    if a.kind and leases.spec(conn, a.kind) is None:
        print(f"{a.kind!r} is not defined — `fleetwright define {a.kind} …` first",
              file=sys.stderr)
        return 2
    for i in range(1, a.n + 1):
        if a.n > 1:
            print(f"{'=' * 30} worker {i} of {a.n} {'=' * 30}")
        print(leases.worker_prompt(
            # The RESOLVED path, not the string that was typed. This prompt is
            # pasted into agents that may run from anywhere, and `--db work.db`
            # from the wrong directory is how a worker ends up creating its own
            # empty queue and reporting nothing to do.
            conn, a.kind, db=str(resolve_db(a).resolve()), lease=a.lease,
            # Only suffix a name the caller actually chose. Defaulting to
            # "agent" and printing `--worker agent-1` for a single worker is
            # how eight spawned workers all ended up called agent-1.
            worker=(f"{a.worker}-{i}" if a.n > 1 else a.worker)
            if a.worker else None))
        if a.n > 1:
            print()
    return 0


def _cmd_brief(a: argparse.Namespace) -> int:
    """Exactly what one unit was told, however the kind has changed since."""
    text = leases.brief_for(_conn(a), a.unit_id)
    if text is None:
        print(f"no such unit: {a.unit_id}", file=sys.stderr)
        return 1
    print(text)
    return 0


def _cmd_lineage(a: argparse.Namespace) -> int:
    lin = leases.lineage(_conn(a), a.unit_id)
    if not lin:
        print(f"no such unit: {a.unit_id}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(lin, indent=2, ensure_ascii=False))
        return 0
    for i, anc in enumerate(lin["ancestors"]):
        print(f"{'  ' * i}{anc['kind']}:{anc['name']}  [{anc['status']}]")
    depth = len(lin["ancestors"])
    u = lin["unit"]
    print(f"{'  ' * depth}{u['kind']}:{u['name']}  [{u['status']}]  <- this one")

    def show(nodes, d):
        for n in nodes:
            note = f"  {n['note']}" if n["status"] == leases.FAILED and n["note"] else ""
            print(f"{'  ' * d}{n['kind']}:{n['name']}  [{n['status']}]{note}")
            show(n["children"], d + 1)
    show(lin["descendants"], depth + 1)
    return 0


def _cmd_kinds(a: argparse.Namespace) -> int:
    rows = leases.kind_versions(_conn(a), a.kind)
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no kinds defined")
        return 0
    current = {r["kind"]: leases.kind_digest(
        r["kind"], r["instructions"], r["done_when"], r["returns"], r["tools"],
        r["skills"], r["mcp"], r["context"])
        for r in [dict(x) for x in _conn(a).execute("SELECT * FROM kind")]}
    print(f"{'kind':<18}{'digest':<18}{'units':>7}  first line of instructions")
    for r in rows:
        mark = "*" if current.get(r["kind"]) == r["digest"] else " "
        first = (r["instructions"] or "").strip().splitlines()[:1]
        print(f"{mark}{r['kind']:<17}{r['digest']:<18}{r['units']:>7}  "
              f"{(first[0] if first else '')[:44]}")
    print("\n* the definition in force now", file=sys.stderr)
    return 0


def _cmd_results(a: argparse.Namespace) -> int:
    status = tuple(a.status) if a.status else (leases.DONE,)
    rows = leases.iter_results(_conn(a), a.kind, run=a.run, status=status,
                               flat=a.flat)
    n = 0
    if a.jsonl:
        # Streamed and flushed per line, so this can be piped into something
        # that starts working before the fleet has finished.
        for r in rows:
            print(json.dumps(r, ensure_ascii=False), flush=True)
            n += 1
    elif a.json:
        # Assembled by hand rather than json.dumps(list(...)) so the whole
        # corpus never has to be in memory at once.
        print("[")
        for r in rows:
            print(("  " if n == 0 else ",\n  ")
                  + json.dumps(r, ensure_ascii=False), end="")
            n += 1
        print("\n]" if n else "]")
    else:
        for r in rows:
            payload = r if a.flat else r.get("result")
            print(f"{r['name']}\t{json.dumps(payload, ensure_ascii=False)}")
            n += 1
    print(f"{n} row(s)", file=sys.stderr)
    return 0


def _cmd_add(a: argparse.Namespace) -> int:
    names = list(a.name or [])
    if a.from_file == "-":
        names += [ln.strip() for ln in sys.stdin if ln.strip()]
    elif a.from_file:
        # ALWAYS explicit. read/open without an encoding uses the locale
        # codec, which is cp1252 on Windows, and a UTF-8 unit list read that
        # way yields different names -- so `add` and `claim` disagree about
        # what the unit is called and nothing matches.
        with open(a.from_file, encoding="utf-8") as fh:
            names += [ln.strip() for ln in fh if ln.strip()]
    if not names:
        print("no units — pass them as arguments, or --from-file (- for stdin)",
              file=sys.stderr)
        return 2
    conn = _conn(a)
    added = leases.add(conn, a.kind, names, priority=a.priority,
                       meta=json.loads(a.meta) if a.meta else None,
                       # This was accepted and never read for four releases.
                       # Runs worked from the library and never from the CLI,
                       # because every run test called leases.add directly.
                       run=a.run)
    print(f"{added:,} new · {len(names) - added:,} already queued")
    if leases.spec(conn, a.kind) is None:
        # Not an error — a bare queue is a legitimate use. But it is almost
        # always a forgotten `define`, and the worker finds out much later.
        print(f"  warning: {a.kind!r} has no instructions. Workers claiming "
              f"these get a bare name. `fleetwright define {a.kind} …`",
              file=sys.stderr)
    return 0


def _cmd_claim(a: argparse.Namespace) -> int:
    worker = a.worker or leases.this_worker()
    got = leases.claim(_conn(a), a.kind, worker=worker, lease=a.lease, n=a.n,
                       run=a.run,
                       max_attempts=a.max_attempts,
                       model=a.model or os.environ.get("FLEETWRIGHT_MODEL"),
                       spawned_by=a.spawned_by
                       or os.environ.get("FLEETWRIGHT_SPAWNED_BY"))
    if not got:
        if not a.json:
            print("nothing to claim", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps([{"unit_id": u.unit_id, "kind": u.kind, "name": u.name,
                           "attempts": u.attempts, "meta": u.meta,
                           "instructions": u.instructions,
                           "done_when": u.done_when, "returns": u.returns,
                           "tools": u.tools, "brief": u.brief()} for u in got]))
        return 0
    if a.brief:
        # For piping straight into an agent: `fleetwright claim x --brief |
        # claude -p -`. The whole assignment, nothing else on stdout.
        print("\n\n".join(u.brief() for u in got))
        return 0
    for u in got:
        again = f"  (attempt {u.attempts})" if u.attempts > 1 else ""
        print(f"{u.name}\t{u.unit_id}\tlease {u.seconds_left:.0f}s{again}")
    print(f"held by {worker}", file=sys.stderr)
    return 0


def _read_result(a: argparse.Namespace):
    """The result payload, from a flag or a file, with real errors.

    `--result-file` exists because Linux caps a SINGLE argument at 128 KB
    (MAX_ARG_STRLEN) whatever ARG_MAX says. A 455 KB result passes on macOS and
    fails with E2BIG on Linux, in the main data path, and CI cannot catch it
    because CI never produces a large result.
    """
    raw = a.result
    if getattr(a, "result_file", None):
        if raw:
            print("pass --result or --result-file, not both", file=sys.stderr)
            raise SystemExit(2)
        raw = Path(a.result_file).read_text(encoding="utf-8")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # A traceback here is bad twice over: it is unreadable, and the unit
        # stays leased because `done` never ran, so it is silently redone when
        # the lease expires and the worker reports success either way.
        print(f"--result is not valid JSON: {e}", file=sys.stderr)
        print("  the unit is still yours; fix the JSON and call finish again,",
              file=sys.stderr)
        print("  or use --result-file to avoid shell quoting entirely.",
              file=sys.stderr)
        raise SystemExit(2) from None


def _who(a: argparse.Namespace) -> str | None:
    """The worker identity a closing command should use, or exit explaining.

    The library defaults an omitted worker to `this_worker()`, which is right
    for a library: one process claims and finishes, so the identity matches.
    It is WRONG here. `this_worker()` is `hostname:pid`, and a shell fleet
    claims in one process and finishes in another --

        u=$(fleetwright claim work --json)      # pid 4021
        fleetwright finish "$id"                # pid 4022, a different name

    -- so inheriting the library default silently refused every close in the
    documented shell pattern. Forty units claimed, forty "not yours", nothing
    finished. Found by running the CI fleet, not by any test.

    So a close from the command line needs evidence: a name, the lease token,
    or an explicit "I know it is not mine". Refusing beats guessing, because
    the two ways of guessing are "close somebody else's unit" and "refuse
    everything", and this way the error says which flag to add.
    """
    if getattr(a, "any_worker", False):
        return leases.ANY
    if a.worker:
        return a.worker
    if getattr(a, "token", None):
        # The token is per-claim and unique, so it proves ownership on its own
        # and the name adds nothing.
        return leases.ANY
    print("who are you? this command cannot tell, and closing a unit needs to "
          "know.", file=sys.stderr)
    print("  --worker NAME   the name you claimed with (a shell worker must "
          "pass the same one to claim and to finish)", file=sys.stderr)
    print("  --token T       the token from your brief, which is unique to "
          "your claim", file=sys.stderr)
    print("  --any-worker    close it whoever holds it", file=sys.stderr)
    raise SystemExit(2)


def _cmd_done(a: argparse.Namespace) -> int:
    conn = _conn(a)
    result = _read_result(a)
    if not a.no_check:
        row = conn.execute("SELECT kind FROM unit WHERE unit_id = ?",
                           (a.unit_id,)).fetchone()
        spec = leases.spec(conn, row["kind"]) if row else None
        declared = (spec or {}).get("returns")
        if result is None and shape.parse(declared) is not None:
            # A missing result is a shape violation, not an exemption. The
            # check used to be skipped entirely when there was no result, so a
            # kind declaring {"claims": <int>} accepted a finish with nothing
            # at all -- and an agent that trips the gate once learns to drop
            # --result rather than to fix the shape.
            print(f"{row['kind']!r} declares a result and none was given:",
                  file=sys.stderr)
            print(f"  returns: {declared}", file=sys.stderr)
            print("  the unit is still yours. Finish again with --result, "
                  "or pass --no-check.", file=sys.stderr)
            raise SystemExit(2)
        problems = shape.describe(declared, result) if result is not None else []
        if problems:
            # Refused, not warned. The unit stays leased, so the worker can fix
            # the shape and finish again without losing the work — the same
            # contract as malformed JSON. A warning would be ignored by exactly
            # the callers this exists for.
            print(f"the result does not match what {row['kind']!r} declares it "
                  f"returns:", file=sys.stderr)
            print(f"  returns: {spec['returns']}", file=sys.stderr)
            for pr in problems[:10]:
                print(f"  {pr}", file=sys.stderr)
            print("  the unit is still yours. Fix the shape and finish again, "
                  "or pass --no-check.", file=sys.stderr)
            raise SystemExit(2)
    then = None
    if a.then:
        # Same contract as a malformed --result: refuse, keep the lease, say
        # what is wrong. Enqueueing nothing while reporting success would lose
        # a whole stage silently.
        try:
            then = json.loads(a.then)
        except json.JSONDecodeError as e:
            print(f"--then is not valid JSON: {e}", file=sys.stderr)
            print('  expected {"kind": ["name", ...]}; the unit is still yours.',
                  file=sys.stderr)
            raise SystemExit(2) from None
        if not isinstance(then, dict) or not all(
                isinstance(v, list) for v in then.values()):
            print('--then must be {"kind": ["name", ...]}', file=sys.stderr)
            print("  the unit is still yours.", file=sys.stderr)
            raise SystemExit(2)
        unknown = [k for k in then if not leases.spec(conn, k)]
        if unknown:
            print(f"--then names undefined kind(s): {', '.join(unknown)}",
                  file=sys.stderr)
            print("  `fleetwright define` them first; a unit with no "
                  "instructions gives its worker a bare name.", file=sys.stderr)
            raise SystemExit(2)
    if leases.finish(conn, a.unit_id, worker=_who(a), token=a.token, note=a.note,
                     result=result, tokens_in=a.tokens_in,
                     tokens_out=a.tokens_out, cost=a.cost, then=then):
        print(f"done {a.unit_id}")
        for kind, names in (then or {}).items():
            print(f"  queued {len(names)} {kind}")
        return 0
    print(f"not yours — {a.unit_id}'s lease expired and another worker holds it",
          file=sys.stderr)
    return 1


def _cmd_fail(a: argparse.Namespace) -> int:
    if leases.fail(_conn(a), a.unit_id, note=a.note, worker=_who(a),
                   token=a.token,
                   max_attempts=a.max_attempts):
        print(f"failed {a.unit_id} — {a.note}")
        return 0
    print(f"not yours — {a.unit_id}", file=sys.stderr)
    return 1


def _cmd_release(a: argparse.Namespace) -> int:
    if leases.release(_conn(a), a.unit_id, worker=_who(a), token=a.token,
                      note=a.note):
        print(f"released {a.unit_id}")
        return 0
    print(f"not yours — {a.unit_id}", file=sys.stderr)
    return 1


def _find_db(explicit: str) -> Path | None:
    """The database a fresh session should look at.

    A new arrival does not know a work.db exists, let alone what it is called.
    If the default is not there, look for one: a fleetwright database is
    recognisable by its tables, so this cannot pick up someone else's SQLite
    file by accident.
    """
    p = Path(explicit)
    if p.exists():
        return p
    if explicit != "work.db":
        return None
    import sqlite3 as _s
    from contextlib import closing
    for cand in sorted(Path().glob("*.db")):
        try:
            # `with connect(...)` commits or rolls back; it does NOT close. In
            # a directory of a hundred .db files that leaked ninety-nine file
            # handles before finding the right one.
            with closing(_s.connect(f"file:{cand}?mode=ro", uri=True)) as c:
                names = {r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            if {"unit", "kind"} <= names:
                return cand
        except Exception:  # noqa: BLE001 - an unreadable file is simply not it
            continue
    return None


def _cmd_state(a: argparse.Namespace) -> int:
    db = _find_db(str(resolve_db(a)))
    if db is None:
        print(f"no fleetwright database here (looked for {a.db} and *.db)")
        print()
        print("If this project has never used it:")
        print("  fleetwright install-skill   # then just ask Claude in English")
        print("  fleetwright init            # or set the work up yourself")
        return 0
    conn = leases.connect(db)
    st = leases.state(conn)
    if a.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 0

    t = st["totals"]
    print(f"fleetwright {__version__} · {db}")
    print(f"  {t['all']:,} units: {t[leases.DONE]:,} done, {t[leases.FAILED]:,} "
          f"failed, {t[leases.OPEN]:,} waiting, {t[leases.LEASED]:,} in flight"
          + (f", {t[leases.CANCELLED]:,} cancelled" if t[leases.CANCELLED] else ""))
    if st["kinds"]:
        print(f"  kinds: {', '.join(st['kinds'])}")
    if st["skills"]:
        print(f"  skills: {', '.join(st['skills'])}")

    if st["runs"]:
        print("\nRUNS")
        for r in st["runs"][:6]:
            mark = "*" if r["running"] else " "
            el = r["elapsed"]
            el_s = "" if el is None else (f"{el:.0f}s" if el < 90 else f"{el/60:.0f}m")
            print(f" {mark}{r['run_id']:<22}{(r['label'] or '')[:28]:<30}"
                  f"{r['done']:>5}/{r['units']:<6}"
                  + (f"{r['failed']} failed  " if r["failed"] else "          ")
                  + el_s)
        if st["live"]:
            print("  * still running", file=sys.stderr)
    if st["totals"]["ungrouped"]:
        print(f"  {st['totals']['ungrouped']:,} unit(s) belong to no run "
              "(enqueued without --run)")

    if st["attention"]:
        print("\nNEEDS ATTENTION")
        for item in st["attention"]:
            print(f"  {item['what']}")
            if item.get("detail"):
                print(f"    {item['detail'][:90]}")
            print(f"    -> {item['do']}")

    print(f"\nNEXT\n  {st['next']}")
    return 0


def _cmd_status(a: argparse.Namespace) -> int:
    conn = _conn(a)
    prog = leases.progress(conn, a.kind, run=a.run)
    if not prog:
        print("no units queued — `fleetwright add <kind> --from-file …`")
        return 0
    if a.json:
        print(json.dumps(prog, indent=2, sort_keys=True))
        return 0
    w = max(len(k) for k in prog) + 2
    # Driven off leases.STATUSES rather than a hand-written list of columns.
    # A status that exists but is not printed makes the row stop adding up, and
    # `cancelled` did exactly that the first time it shipped.
    head = "".join(f"{st:>10}" for st in leases.STATUSES)
    print(f"{'kind':<{w}}{head}{'left':>8}")
    for kind, counts in sorted(prog.items()):
        left = counts[leases.OPEN] + counts[leases.LEASED]
        row = "".join(f"{counts[st]:>10,}" for st in leases.STATUSES)
        print(f"{kind:<{w}}{row}{left:>8,}")
    if a.who:
        held = leases.leased(conn)
        if held:
            print()
            for r in held:
                print(f"  {r['worker']:<28}{r['name']:<24}"
                      f"{max(0.0, r['leased_until'] - time.time()):.0f}s left")
    bad = leases.failures(conn)
    if bad:
        print(f"\n{len(bad)} units no worker could finish:")
        for r in bad[:10]:
            print(f"  {r['name']:<24}{r['note'] or ''}")
    return 0


def _cmd_wait(a: argparse.Namespace) -> int:
    """Block until the work is over, and say what happened in the exit code.

    Without this every script that drives a fleet wraps a polling loop around
    `status` and parses text out of it. The exit code is the interface: 0 for
    everything finished cleanly, 1 if anything failed, 2 on timeout. That is
    what makes a fleet usable from a Makefile or from CI.
    """
    conn = _conn(a)
    deadline = time.time() + a.timeout if a.timeout else None
    last = None
    while True:
        leases.reclaim(conn)
        open_, leased = leases.outstanding(conn, run=a.run, kind=a.kind)
        prog = leases.progress(conn, a.kind, run=a.run)
        done = sum(p[leases.DONE] for p in prog.values())
        failed = sum(p[leases.FAILED] for p in prog.values())
        line = f"{done} done, {failed} failed, {open_} waiting, {leased} in flight"
        if not a.quiet and line != last:
            # Only on change: a line every two seconds for an hour is not
            # progress reporting, it is noise that hides the one line that
            # mattered.
            print(line, file=sys.stderr, flush=True)
            last = line
        if open_ == 0 and leased == 0:
            if not prog:
                print("nothing queued", file=sys.stderr)
                return 0
            if not a.quiet:
                print("finished", file=sys.stderr)
            return 1 if failed else 0
        if deadline and time.time() >= deadline:
            print(f"timed out after {a.timeout:g}s with {open_ + leased} "
                  f"unit(s) outstanding", file=sys.stderr)
            return 2
        time.sleep(a.interval)


def _cmd_retry(a: argparse.Namespace) -> int:
    if not (a.run or a.kind or a.name or a.all):
        # Bare `retry` would reopen every failed unit in the file, across every
        # run. That is never what someone means and it is not undoable.
        print("refusing to retry everything: pass --run, --kind, a name, "
              "or --all", file=sys.stderr)
        return 2
    out = leases.retry(_conn(a), run=a.run, kind=a.kind, names=a.name or None,
                       include_cancelled=a.include_cancelled)
    print(f"{out['retrying']} unit(s) back in the queue, attempts reset")
    return 0


def _cmd_cancel(a: argparse.Namespace) -> int:
    if not (a.run or a.kind or a.name or a.all):
        print("refusing to cancel everything: pass --run, --kind, a name, "
              "or --all", file=sys.stderr)
        return 2
    out = leases.cancel(_conn(a), run=a.run, kind=a.kind, names=a.name or None,
                        now=a.now)
    print(f"{out['cancelled']} unit(s) cancelled"
          + ("" if a.now else "; anything in flight was left to finish"))
    return 0


def _cmd_reclaim(a: argparse.Namespace) -> int:
    n = leases.reclaim(_conn(a))
    print(f"{n} expired lease(s) returned to the pool")
    return 0


def _cmd_backup(a: argparse.Namespace) -> int:
    src = resolve_db(a)
    if not src.exists():
        print(f"no database at {src}", file=sys.stderr)
        return 2
    conn = leases.connect_readonly(src)
    try:
        out = leases.backup(conn, a.to)
    except FileExistsError as e:
        print(f"{e} already exists; a backup that overwrites the last one is "
              f"not a backup", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"{src} -> {out}  ({out.stat().st_size:,} bytes)")
    print("  one file, with the WAL folded in. `cp` would have missed "
          "whatever finished most recently.", file=sys.stderr)
    return 0


def _cmd_serve(a: argparse.Namespace) -> int:
    from .mcp import Server
    # Through the same resolution as every other command. An MCP server on a
    # different file from the workers is a fleet that silently does nothing.
    Server(resolve_db(a)).serve()
    return 0


def _cmd_demo(_a: argparse.Namespace) -> int:
    from .demo import main as demo_main
    return demo_main()


def _cmd_dashboard(a: argparse.Namespace) -> int:
    from . import dashboard
    db = resolve_db(a)
    if a.out:
        Path(a.out).write_text(dashboard.snapshot(db, run=a.run), encoding="utf-8")
        print(f"wrote {a.out}")
        return 0
    # Env var as well as a flag: a token on the command line lands in shell
    # history and in `ps` output for anyone on the box.
    tf = a.token_file or os.environ.get("FLEETWRIGHT_TOKEN_FILE")
    token = a.token or os.environ.get("FLEETWRIGHT_TOKEN")
    if tf:
        token = Path(tf).expanduser().read_text(encoding="utf-8").strip()
    if token == "auto":
        token = dashboard.new_token()
        # Printed once, to stderr, so a pipe to a log file does not silently
        # swallow the only copy.
        print(f"  access token: {token}", file=sys.stderr)
    # A colon-separated list, like PATH, because that is the shape people
    # already have a mental model for and it needs no file anywhere. One
    # export in a profile and `fleetwright dashboard` shows every repository
    # you work on, each labelled by its directory.
    from_env = [x for x in os.environ.get("FLEETWRIGHT_PROJECTS", "").split(os.pathsep) if x]
    dashboard.serve([db, *[Path(x) for x in (a.project or [])],
                     *[Path(x).expanduser() for x in from_env]],
                    host=a.host, port=a.port, open_browser=not a.no_open,
                    token=token, allow_host=a.allow_host)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fleetwright",
        description="Work leases in one SQLite file, so a fleet of workers "
                    "divides a corpus instead of racing it.")
    p.add_argument("--version", action="version", version=f"fleetwright {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, creates: bool = True):
        sp.add_argument("--db", default="work.db",
                        help=f"defaults to work.db, searched for up the tree. "
                             f"{DB_ENV} pins one for the whole session.")
        if creates:
            # Only on commands that can actually make one. `state` reports and
            # `dashboard` reads; offering them a flag that does nothing is how
            # a CLI teaches people that flags are decorative.
            sp.add_argument("--create", action="store_true",
                            help="make a new database even though a name this "
                                 "close to it already exists")
        return sp

    s = common(sub.add_parser(
        "start", help="begin a run, and print its id"))
    s.add_argument("--label", help="what this run is, in a few words")
    s.add_argument("--by", help="who started it; defaults to hostname:pid")
    s.add_argument("--note")
    s.add_argument("--id", help="use this id instead of a generated one")
    s.set_defaults(fn=_cmd_start)

    s = common(sub.add_parser("runs", help="every run, newest first"))
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_runs)

    s = common(sub.add_parser("skill", help="say what a skill name means"))
    s.add_argument("name")
    s.add_argument("--source", help="a path, a URL, or a sentence. A readable "
                                    "file is hashed so revisions are tellable "
                                    "apart later.")
    s.add_argument("--version")
    s.add_argument("--note")
    s.set_defaults(fn=_cmd_skill)

    s = common(sub.add_parser(
        "skill-check", help="re-hash skill sources and compare to what was registered"))
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=_cmd_skill_check)

    s = common(sub.add_parser("skills", help="registered skills and their use"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_skills)

    s = common(sub.add_parser("define", help="say what a kind of work IS"))
    s.add_argument("kind")
    s.add_argument("--instructions", help="what to do; written for an agent "
                                          "with no other context")
    s.add_argument("--instructions-file", help="read them from a file; - for stdin")
    s.add_argument("--done-when", help="what finished looks like")
    s.add_argument("--returns", help="the shape a worker should hand back")
    s.add_argument("--tools", help="free-text hint; prefer --skill and --mcp")
    s.add_argument("--skill", action="append",
                   help="a skill a worker MUST load; repeatable")
    s.add_argument("--mcp", action="append", metavar="NAME=COMMAND",
                   help="an MCP server a worker MUST have; repeatable")
    s.add_argument("--context", metavar="FILE",
                   help="read-only material every worker of this kind receives")
    s.add_argument("--max-attempts", type=int, metavar="N",
                   help="how many hand-outs before a unit of this kind stays "
                        "failed instead of returning to the queue. Belongs to "
                        "the WORK, not to a claim: an expensive kind may "
                        "deserve one attempt and a flaky one five.")
    s.add_argument("--force", action="store_true",
                   help="redefine even with units waiting or in flight")
    s.set_defaults(fn=_cmd_define)

    s = common(sub.add_parser(
        "prompt", help="the spawn prompt for a worker, generated from the kind"))
    s.add_argument("kind", nargs="?")
    s.add_argument("-n", type=int, default=1, help="print one per worker")
    s.add_argument("--worker", default=None,
                   help="name the workers yourself. Omit and each process "
                        "picks its own, which is what you want: a shared name "
                        "makes two processes indistinguishable to every "
                        "ownership check.")
    s.add_argument("--lease", type=float, default=leases.DEFAULT_LEASE)
    s.set_defaults(fn=_cmd_prompt)

    s = common(sub.add_parser(
        "brief", help="exactly what one unit was told, however the kind changed since"))
    s.add_argument("unit_id")
    s.set_defaults(fn=_cmd_brief)

    s = common(sub.add_parser(
        "lineage", help="what caused this unit, and what it caused"))
    s.add_argument("unit_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_lineage)

    s = common(sub.add_parser(
        "kinds", help="every definition a kind has had, and what ran under each"))
    s.add_argument("kind", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_kinds)

    s = common(sub.add_parser("results", help="what the fleet produced"))
    s.add_argument("kind", nargs="?")
    s.add_argument("--run", help="only this run")
    s.add_argument("--json", action="store_true")
    s.add_argument("--jsonl", action="store_true",
                   help="one object per line, streamed; for jq and for pipes")
    s.add_argument("--flat", action="store_true",
                   help="lift the result's keys to the top level, so `jq .claims` "
                        "works instead of `jq .result.claims`")
    s.add_argument("--status", action="append", choices=list(leases.STATUSES),
                   help="default done; repeat to include failed too")
    s.set_defaults(fn=_cmd_results)

    s = common(sub.add_parser("add", help="enqueue units of work"))
    s.add_argument("kind")
    s.add_argument("name", nargs="*")
    s.add_argument("--from-file", help="one unit per line; - for stdin")
    s.add_argument("--priority", type=int, default=0)
    s.add_argument("--meta", help="JSON carried with each unit; its keys are "
                                  "substituted into the instructions")
    s.add_argument("--run", help="the run these units belong to")
    s.set_defaults(fn=_cmd_add)

    s = common(sub.add_parser("claim", help="take work nobody else holds"))
    s.add_argument("kind", nargs="?", help="omit to take any kind")
    s.add_argument("--worker", help="defaults to hostname:pid")
    s.add_argument("--lease", type=float, default=leases.DEFAULT_LEASE,
                   help="seconds; several times your slowest unit")
    s.add_argument("-n", type=int, default=1, help="take a batch")
    s.add_argument("--json", action="store_true")
    s.add_argument("--run", help="take work only from this run")
    s.add_argument("--max-attempts", type=int, default=leases.MAX_ATTEMPTS,
                   help="a unit handed out this many times is left failed "
                        "rather than claimed again")
    s.add_argument("--model", help="what you are, e.g. claude-opus-5. Recorded "
                                   "as declared -- nothing verifies it. "
                                   "FLEETWRIGHT_MODEL also works.")
    s.add_argument("--brief", action="store_true",
                   help="print the full assignment, for piping into an agent")
    # The one edge nothing here can observe. A subagent cannot see that a
    # session spawned it, so it has to be told, and env is the right channel:
    # a subagent inherits its parent's environment, so an orchestrator exports
    # this once and every worker it spawns is labelled without touching a
    # single worker prompt.
    s.add_argument("--spawned-by", metavar="WHO",
                   help="who spawned this worker, e.g. the orchestrating "
                        "session. Declared, never measured. "
                        "FLEETWRIGHT_SPAWNED_BY also works.")
    s.set_defaults(fn=_cmd_claim)

    # `finish` at every layer. The library has always been finish(), the MCP
    # tool finish_job, and the brief says "call finish" — only the CLI said
    # `done`, so every shell worker following its own brief ran a command that
    # does not exist. All three workers in the first real fleet hit it.
    # `done` stays as an alias: it is in shipped prompts and shell scripts.
    for verb, helptext in (("finish", "mark a unit finished"),
                           ("done", "alias for finish")):
        s = common(sub.add_parser(verb, help=helptext))
        s.add_argument("unit_id")
        s.add_argument("--result", help="JSON the worker produced")
        s.add_argument("--result-file", metavar="FILE",
                       help="read the result from a file; use this for anything "
                            "large, since Linux caps one argument at 128 KB")
        s.add_argument("--note")
        s.add_argument("--worker",
                       help="omit and this process's own identity is used, "
                            "the same one `claim` records")
        s.add_argument("--any-worker", action="store_true",
                       help="close it whoever holds it. An operator cleaning "
                            "up after a fleet that is gone, not a worker.")
        s.add_argument("--token", metavar="T",
                       help="the token from your brief. A worker NAME can be "
                            "shared by two processes; this cannot.")
        s.add_argument("--no-check", action="store_true",
                       help="skip the check against the kind's declared returns")
        # The only way one unit causes another to exist, and until now it was
        # reachable from the library alone -- while the skill told shell
        # workers to use it.
        s.add_argument("--then", metavar="JSON",
                       help='enqueue the next stage as this one finishes, e.g. '
                            '\'{"audit": ["p1-c0", "p1-c1"]}\'. They inherit '
                            "this unit's run and record it as their parent, so "
                            "`wait --run` covers them and `lineage` finds them.")
        # Declared, not measured. Nothing here can observe a model's usage.
        s.add_argument("--tokens-in", type=int, metavar="N")
        s.add_argument("--tokens-out", type=int, metavar="N")
        s.add_argument("--cost", type=float, metavar="X",
                       help="what this unit cost you, in whatever currency you "
                            "are counting; recorded as reported")
        s.set_defaults(fn=_cmd_done)

    s = common(sub.add_parser("fail", help="report a unit that could not be done"))
    s.add_argument("unit_id")
    s.add_argument("--note", required=True, help="why; it is kept")
    s.add_argument("--worker")
    s.add_argument("--any-worker", action="store_true",
                   help="fail it whoever holds it")
    s.add_argument("--token", metavar="T", help="the token from your brief")
    s.add_argument("--max-attempts", type=int, default=leases.MAX_ATTEMPTS,
                   help="how many hand-outs before this stays failed rather "
                        "than returning to the queue")
    s.set_defaults(fn=_cmd_fail)

    s = common(sub.add_parser("release", help="hand a unit back, no attempt burned"))
    s.add_argument("unit_id")
    s.add_argument("--any-worker", action="store_true",
                   help="release it whoever holds it")
    s.add_argument("--token", metavar="T", help="the token from your brief")
    s.add_argument("--note")
    s.add_argument("--worker")
    s.set_defaults(fn=_cmd_release)

    s = common(sub.add_parser(
        "state", help="where this project is, for a session that just arrived"),
        creates=False)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_state)

    s = common(sub.add_parser("status", help="what is left, who holds what"))
    s.add_argument("kind", nargs="?")
    s.add_argument("--run", help="only this run")
    s.add_argument("--who", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_status)

    s = common(sub.add_parser(
        "wait", help="block until the work is done; exit 1 if anything failed"))
    s.add_argument("--run")
    s.add_argument("--kind")
    s.add_argument("--timeout", type=float, help="seconds; exit 2 if exceeded")
    s.add_argument("--interval", type=float, default=2.0)
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=_cmd_wait)

    s = common(sub.add_parser(
        "retry", help="put failed units back in the queue, attempts reset"))
    s.add_argument("name", nargs="*")
    s.add_argument("--run")
    s.add_argument("--kind")
    s.add_argument("--all", action="store_true", help="every failed unit")
    s.add_argument("--include-cancelled", action="store_true")
    s.set_defaults(fn=_cmd_retry)

    s = common(sub.add_parser(
        "cancel", help="stop work that has not started"))
    s.add_argument("name", nargs="*")
    s.add_argument("--run")
    s.add_argument("--kind")
    s.add_argument("--all", action="store_true")
    s.add_argument("--now", action="store_true",
                   help="also take back units already in flight")
    s.set_defaults(fn=_cmd_cancel)

    s = common(sub.add_parser(
        "backup", help="a consistent copy, safe to take while a fleet runs"),
        creates=False)
    s.add_argument("to", help="the file to write; it must not exist")
    s.set_defaults(fn=_cmd_backup)

    s = common(sub.add_parser("reclaim", help="return expired leases now"))
    s.set_defaults(fn=_cmd_reclaim)

    s = common(sub.add_parser("serve", help="run the MCP server on stdio"))
    s.set_defaults(fn=_cmd_serve)

    s = common(sub.add_parser("dashboard", help="a live view of the fleet"),
               creates=False)
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--host", default="127.0.0.1",
                   help="loopback by default; off-loopback requires --token")
    s.add_argument("--project", action="append", metavar="PATH",
                   help="another database, a repository holding one, or a "
                        "directory of them; repeatable. FLEETWRIGHT_PROJECTS "
                        "is the same thing as a PATH-style list, so one "
                        "export shows every repository you work on.")
    s.add_argument("--token", metavar="T",
                   help="access token, or `auto` to generate one and print it. "
                        "Prefer --token-file or FLEETWRIGHT_TOKEN: a flag "
                        "lands in shell history and in `ps` for every user "
                        "on the machine.")
    s.add_argument("--token-file", metavar="FILE",
                   help="read the token from a file, so it is never an "
                        "argument. FLEETWRIGHT_TOKEN_FILE also works.")
    s.add_argument("--allow-host", action="append", metavar="NAME",
                   help="a host name this dashboard may be reached by, for a "
                        "reverse proxy. Repeatable. Loopback is always "
                        "allowed; anything else is refused, which is what "
                        "stops a web page rebinding its DNS to 127.0.0.1 and "
                        "reading your fleet.")
    s.add_argument("--run", help="open on this run rather than everything")
    s.add_argument("--out", help="write a static snapshot instead of serving")
    s.add_argument("--no-open", action="store_true",
                   help="do not open a browser")
    s.set_defaults(fn=_cmd_dashboard)

    s = sub.add_parser("init", help="write a starter fleetwright.toml")
    s.add_argument("--file", default="fleetwright.toml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=_cmd_init)

    s = common(sub.add_parser(
        "apply", help="register skills and define kinds from a config file"))
    s.add_argument("--file", default="fleetwright.toml")
    s.add_argument("--run", help="enqueue declared units into this run")
    s.add_argument("--no-units", action="store_true",
                   help="apply skills and kinds only")
    s.add_argument("--force", action="store_true",
                   help="redefine kinds even with units waiting or in flight")
    s.set_defaults(fn=_cmd_apply)

    s = sub.add_parser(
        "install-skill",
        help="teach Claude Code to run fleets: writes .claude/skills/fleetwright")
    s.add_argument("--user", action="store_true",
                   help="install for every project on this machine")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=_cmd_install_skill)

    s = sub.add_parser("demo", help="a four-worker fleet, in sixty seconds")
    s.set_defaults(fn=_cmd_demo)
    return p


def _hoist_positionals(parser: argparse.ArgumentParser,
                       argv: list[str]) -> list[str]:
    """Move bare words in front of the flags, for `nargs="*"` subcommands.

    argparse cannot match `add extract --db x p1 p2`. It fills a trailing
    `nargs="*"` from the FIRST run of positionals, which is empty here, and
    then calls `p1 p2` unrecognised. `add extract p1 p2 --db x` works. Both
    are things people write, one of them dies, and the error names the units
    rather than the ordering, so it reads as "those units are bad".

    Which flags take a value is read off the parser, so this cannot drift
    away from the flags it has to know about.
    """
    takes_value = {o for act in parser._actions for o in act.option_strings
                   if act.nargs != 0}
    words, flags, i = [], [], 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":                      # everything after is literal
            words += argv[i + 1:]
            break
        if tok.startswith("-") and tok != "-":
            flags.append(tok)
            if "=" not in tok and tok in takes_value and i + 1 < len(argv):
                flags.append(argv[i + 1])
                i += 1
        else:
            words.append(tok)
        i += 1
    return words + flags


#: The subcommands whose last positional is `nargs="*"`. Asserted in the tests
#: against the built parser, so adding a fourth cannot quietly skip the fix.
_VARIADIC = ("add", "retry", "cancel")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # Only for the subcommands that actually take a list of names.
    for i, tok in enumerate(argv):
        if tok in _VARIADIC:
            sub = parser._subparsers._group_actions[0].choices[tok]
            argv = argv[:i + 1] + _hoist_positionals(sub, argv[i + 1:])
            break
        if not tok.startswith("-"):
            break                            # some other subcommand
    a = parser.parse_args(argv)
    return a.fn(a)




def _cmd_init(a: argparse.Namespace) -> int:
    f = Path(a.file)
    if f.exists() and not a.force:
        print(f"{f} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    f.write_text(config.EXAMPLE, encoding="utf-8")
    print(f"wrote {f}")
    print("  edit it, then: fleetwright apply")
    return 0


def _cmd_apply(a: argparse.Namespace) -> int:
    f = Path(a.file)
    try:
        cfg = config.load(f)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        if isinstance(e, FileNotFoundError):
            print("  `fleetwright init` writes a starter one.", file=sys.stderr)
        return 2
    conn = _conn(a)
    try:
        out = config.apply(conn, cfg, root=f.parent, force=a.force)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{len(out['skills'])} skill(s), {len(out['kinds'])} kind(s) applied")
    for k in out["kinds"]:
        names, meta = config.units_for(cfg, k, root=f.parent)
        if not names:
            continue
        if a.no_units:
            print(f"  {k}: {len(names)} unit(s) declared, not enqueued (--no-units)")
            continue
        n = leases.add(conn, k, names, meta=meta or None, run=a.run)
        print(f"  {k}: {n:,} new unit(s) of {len(names):,} declared")
    for w in out["warnings"]:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


def _cmd_install_skill(a: argparse.Namespace) -> int:
    """Write the skill where Claude Code will find it.

    This is the whole on-ramp. Before it, using fleetwright meant reading the
    docs and running five commands in the right order. After it, you install
    the tool, run this once, and then ask Claude in English to process a list
    of things with eight agents: the skill tells it how, and it does the rest.
    """
    from . import skill_text
    dest = (Path.home() if a.user else Path.cwd()) / ".claude" / "skills" / "fleetwright"
    target = dest / "SKILL.md"
    if target.exists() and not a.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    target.write_text(skill_text(), encoding="utf-8")
    where = "for every project on this machine" if a.user else "for this project"
    print(f"installed {where}: {target}")
    print()
    print("Now ask Claude something like:")
    print('  "extract every claim from the 400 files in scans/, using 8 agents"')
    print("It will define the work, enqueue it, spawn the workers, and collect")
    print("the results. `fleetwright status --who` shows the fleet while it runs.")
    return 0


# Last line in the file, deliberately. It used to sit two thirds of the way
# down, so `python -m fleetwright.cli` ran main() before the handlers below
# it were defined and died with `NameError: name '_cmd_init' is not defined`.
# The console script was fine, because importing the module runs all of it
# before anything calls main(), which is why nothing noticed.
if __name__ == "__main__":
    raise SystemExit(main())
