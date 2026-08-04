"""`superagentic` — the command line.

argparse rather than a CLI framework, because this package has no runtime
dependencies and that is worth more than a prettier `--help`. It gets installed
next to whatever the workers actually run, and the least it can do is not have
opinions about their Click version.

Exit codes are the interface here, not the text. `claim` exits 1 with no output
when the queue is dry, so a shell loop terminates on its own:

    while unit=$(superagentic claim extract --json); do … done
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, leases


def _conn(a: argparse.Namespace):
    return leases.connect(Path(a.db))


def _cmd_define(a: argparse.Namespace) -> int:
    instructions = a.instructions
    if a.instructions_file:
        instructions = (sys.stdin.read() if a.instructions_file == "-"
                        else Path(a.instructions_file).read_text(encoding="utf-8"))
    if not instructions:
        print("no instructions — pass --instructions or --instructions-file",
              file=sys.stderr)
        return 2
    leases.define(_conn(a), a.kind, instructions, done_when=a.done_when,
                  returns=a.returns, tools=a.tools)
    print(f"defined {a.kind}")
    if not a.done_when:
        print("  warning: no --done-when. Every worker will decide for itself "
              "what finished means, and they will not agree.", file=sys.stderr)
    return 0


def _cmd_results(a: argparse.Namespace) -> int:
    rows = leases.results(_conn(a), a.kind)
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for r in rows:
        print(f"{r['name']}\t{json.dumps(r['result'], ensure_ascii=False)}")
    print(f"{len(rows)} finished", file=sys.stderr)
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
                       meta=json.loads(a.meta) if a.meta else None)
    print(f"{added:,} new · {len(names) - added:,} already queued")
    if leases.spec(conn, a.kind) is None:
        # Not an error — a bare queue is a legitimate use. But it is almost
        # always a forgotten `define`, and the worker finds out much later.
        print(f"  warning: {a.kind!r} has no instructions. Workers claiming "
              f"these get a bare name. `superagentic define {a.kind} …`",
              file=sys.stderr)
    return 0


def _cmd_claim(a: argparse.Namespace) -> int:
    worker = a.worker or leases.this_worker()
    got = leases.claim(_conn(a), a.kind, worker=worker, lease=a.lease, n=a.n)
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
        # For piping straight into an agent: `superagentic claim x --brief |
        # claude -p -`. The whole assignment, nothing else on stdout.
        print("\n\n".join(u.brief() for u in got))
        return 0
    for u in got:
        again = f"  (attempt {u.attempts})" if u.attempts > 1 else ""
        print(f"{u.name}\t{u.unit_id}\tlease {u.seconds_left:.0f}s{again}")
    print(f"held by {worker}", file=sys.stderr)
    return 0


def _cmd_done(a: argparse.Namespace) -> int:
    if leases.finish(_conn(a), a.unit_id, worker=a.worker, note=a.note,
                     result=json.loads(a.result) if a.result else None):
        print(f"done {a.unit_id}")
        return 0
    print(f"not yours — {a.unit_id}'s lease expired and another worker holds it",
          file=sys.stderr)
    return 1


def _cmd_fail(a: argparse.Namespace) -> int:
    if leases.fail(_conn(a), a.unit_id, note=a.note, worker=a.worker):
        print(f"failed {a.unit_id} — {a.note}")
        return 0
    print(f"not yours — {a.unit_id}", file=sys.stderr)
    return 1


def _cmd_release(a: argparse.Namespace) -> int:
    if leases.release(_conn(a), a.unit_id, worker=a.worker, note=a.note):
        print(f"released {a.unit_id}")
        return 0
    print(f"not yours — {a.unit_id}", file=sys.stderr)
    return 1


def _cmd_status(a: argparse.Namespace) -> int:
    conn = _conn(a)
    prog = leases.progress(conn, a.kind)
    if not prog:
        print("no units queued — `superagentic add <kind> --from-file …`")
        return 0
    if a.json:
        print(json.dumps(prog, indent=2, sort_keys=True))
        return 0
    w = max(len(k) for k in prog) + 2
    print(f"{'kind':<{w}}{'open':>8}{'leased':>8}{'done':>8}{'failed':>8}{'left':>8}")
    for kind, s in sorted(prog.items()):
        left = s[leases.OPEN] + s[leases.LEASED]
        print(f"{kind:<{w}}{s[leases.OPEN]:>8,}{s[leases.LEASED]:>8,}"
              f"{s[leases.DONE]:>8,}{s[leases.FAILED]:>8,}{left:>8,}")
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


def _cmd_reclaim(a: argparse.Namespace) -> int:
    n = leases.reclaim(_conn(a))
    print(f"{n} expired lease(s) returned to the pool")
    return 0


def _cmd_serve(a: argparse.Namespace) -> int:
    from .mcp import Server
    Server(Path(a.db)).serve()
    return 0


def _cmd_demo(_a: argparse.Namespace) -> int:
    from .demo import main as demo_main
    return demo_main()


def _cmd_dashboard(a: argparse.Namespace) -> int:
    from . import dashboard
    db = Path(a.db)
    if a.out:
        Path(a.out).write_text(dashboard.snapshot(db), encoding="utf-8")
        print(f"wrote {a.out}")
        return 0
    dashboard.serve(db, host=a.host, port=a.port, open_browser=not a.no_open)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="superagentic",
        description="Work leases in one SQLite file, so a fleet of workers "
                    "divides a corpus instead of racing it.")
    p.add_argument("--version", action="version", version=f"superagentic {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--db", default="work.db")
        return sp

    s = common(sub.add_parser("define", help="say what a kind of work IS"))
    s.add_argument("kind")
    s.add_argument("--instructions", help="what to do; written for an agent "
                                          "with no other context")
    s.add_argument("--instructions-file", help="read them from a file; - for stdin")
    s.add_argument("--done-when", help="what finished looks like")
    s.add_argument("--returns", help="the shape a worker should hand back")
    s.add_argument("--tools", help="which tools or MCP servers to use")
    s.set_defaults(fn=_cmd_define)

    s = common(sub.add_parser("results", help="what the fleet produced"))
    s.add_argument("kind", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_results)

    s = common(sub.add_parser("add", help="enqueue units of work"))
    s.add_argument("kind")
    s.add_argument("name", nargs="*")
    s.add_argument("--from-file", help="one unit per line; - for stdin")
    s.add_argument("--priority", type=int, default=0)
    s.add_argument("--meta", help="JSON carried with each unit; its keys are "
                                  "substituted into the instructions")
    s.set_defaults(fn=_cmd_add)

    s = common(sub.add_parser("claim", help="take work nobody else holds"))
    s.add_argument("kind", nargs="?", help="omit to take any kind")
    s.add_argument("--worker", help="defaults to hostname:pid")
    s.add_argument("--lease", type=float, default=leases.DEFAULT_LEASE,
                   help="seconds; several times your slowest unit")
    s.add_argument("-n", type=int, default=1, help="take a batch")
    s.add_argument("--json", action="store_true")
    s.add_argument("--brief", action="store_true",
                   help="print the full assignment, for piping into an agent")
    s.set_defaults(fn=_cmd_claim)

    s = common(sub.add_parser("done", help="mark a unit finished"))
    s.add_argument("unit_id")
    s.add_argument("--result", help="JSON the worker produced")
    s.add_argument("--note")
    s.add_argument("--worker")
    s.set_defaults(fn=_cmd_done)

    s = common(sub.add_parser("fail", help="report a unit that could not be done"))
    s.add_argument("unit_id")
    s.add_argument("--note", required=True, help="why; it is kept")
    s.add_argument("--worker")
    s.set_defaults(fn=_cmd_fail)

    s = common(sub.add_parser("release", help="hand a unit back, no attempt burned"))
    s.add_argument("unit_id")
    s.add_argument("--note")
    s.add_argument("--worker")
    s.set_defaults(fn=_cmd_release)

    s = common(sub.add_parser("status", help="what is left, who holds what"))
    s.add_argument("kind", nargs="?")
    s.add_argument("--who", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_status)

    s = common(sub.add_parser("reclaim", help="return expired leases now"))
    s.set_defaults(fn=_cmd_reclaim)

    s = common(sub.add_parser("serve", help="run the MCP server on stdio"))
    s.set_defaults(fn=_cmd_serve)

    s = common(sub.add_parser("dashboard", help="a live view of the fleet"))
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--host", default="127.0.0.1",
                   help="loopback by default; this has no authentication")
    s.add_argument("--out", help="write a static snapshot instead of serving")
    s.add_argument("--no-open", action="store_true",
                   help="do not open a browser")
    s.set_defaults(fn=_cmd_dashboard)

    s = sub.add_parser("demo", help="a four-worker fleet, in sixty seconds")
    s.set_defaults(fn=_cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
