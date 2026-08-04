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
    added = leases.add(_conn(a), a.kind, names, priority=a.priority)
    print(f"{added:,} new · {len(names) - added:,} already queued")
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
                           "attempts": u.attempts, "meta": u.meta} for u in got]))
        return 0
    for u in got:
        again = f"  (attempt {u.attempts})" if u.attempts > 1 else ""
        print(f"{u.name}\t{u.unit_id}\tlease {u.seconds_left:.0f}s{again}")
    print(f"held by {worker}", file=sys.stderr)
    return 0


def _cmd_done(a: argparse.Namespace) -> int:
    if leases.finish(_conn(a), a.unit_id, worker=a.worker, note=a.note):
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

    s = common(sub.add_parser("add", help="enqueue units of work"))
    s.add_argument("kind")
    s.add_argument("name", nargs="*")
    s.add_argument("--from-file", help="one unit per line; - for stdin")
    s.add_argument("--priority", type=int, default=0)
    s.set_defaults(fn=_cmd_add)

    s = common(sub.add_parser("claim", help="take work nobody else holds"))
    s.add_argument("kind", nargs="?", help="omit to take any kind")
    s.add_argument("--worker", help="defaults to hostname:pid")
    s.add_argument("--lease", type=float, default=leases.DEFAULT_LEASE,
                   help="seconds; several times your slowest unit")
    s.add_argument("-n", type=int, default=1, help="take a batch")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_cmd_claim)

    s = common(sub.add_parser("done", help="mark a unit finished"))
    s.add_argument("unit_id")
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

    s = sub.add_parser("demo", help="a four-worker fleet, in sixty seconds")
    s.set_defaults(fn=_cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
