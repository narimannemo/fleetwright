"""`fleetwright demo` — a fleet, a crash, and a recovery, in sixty seconds.

Runs against a throwaway database so it can be run anywhere, including from a
`uvx` with nothing installed. ASCII only: box-drawing characters raise
UnicodeEncodeError on a Windows console under the default code page, which is
a silly way for a demo to fail on the platform it most needs to reassure.
"""

from __future__ import annotations

import tempfile
import time
from contextlib import closing
from pathlib import Path

from . import leases


def _rule(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(0, 58 - len(title)))


def main() -> int:
    # `closing`, and it is not cosmetic. Windows refuses to delete a file that
    # is still open, so leaving the connection to be garbage-collected makes
    # TemporaryDirectory's cleanup raise PermissionError -- the demo does all
    # its work, prints all its output, and then dies on the last line. POSIX
    # never shows this; the Windows CI runner is the only reason it was found.
    with tempfile.TemporaryDirectory() as d, \
            closing(leases.connect(Path(d) / "demo.db")) as conn:

        _rule("1. the orchestrator says what the work IS, once")
        leases.define(
            conn, "translate",
            instructions="Translate $path into English. Keep the line breaks.",
            done_when="the whole page is translated, or you have established "
                      "it has no text",
            returns='{"words": <int>}',
            tools="your own translator; do not guess at proper nouns")
        print("   defined 'translate' -- every worker that ever claims one of")
        print("   these is handed it, including ones spawned an hour from now")

        n = leases.add(conn, "translate", [f"page-{i}" for i in range(1, 7)],
                       meta={"path": "scans/$name.tif"})
        print(f"   {n} units queued")
        print(f"   re-running the same enumeration adds "
              f"{leases.add(conn, 'translate', [f'page-{i}' for i in range(1, 7)])}"
              f" -- it is keyed on kind:name")

        _rule("2. a worker claims, and is told what to do")
        first = leases.claim(conn, "translate", worker="worker-0", lease=1.0)[0]
        for line in first.brief().splitlines():
            print(f"   | {line}")
        leases.release(conn, first.unit_id, worker="worker-0")

        _rule("3. three workers claim, and never collide")
        taken = {}
        for w in ("worker-a", "worker-b", "worker-c"):
            got = leases.claim(conn, "translate", worker=w, n=2, lease=1.0)
            taken[w] = [u.name for u in got]
            print(f"   {w}: {', '.join(taken[w])}")
        everything = [n for names in taken.values() for n in names]
        print(f"   {len(everything)} units handed out, "
              f"{len(set(everything))} distinct -- nobody got the same page")

        _rule("4. two finish, handing back what they produced")
        for w in ("worker-a", "worker-b"):
            for i, name in enumerate(taken[w]):
                leases.finish(conn, f"translate:{name}", worker=w,
                              result={"words": 800 + i})
            print(f"   {w}: done")
        print("   worker-c: [process dies without reporting anything]")

        _rule("5. its lease expires, and the work comes back")
        print("   another worker asks immediately:")
        print(f"     {leases.claim(conn, 'translate', worker='worker-d') or 'nothing -- still leased'}")
        time.sleep(1.1)
        back = leases.claim(conn, "translate", worker="worker-d", n=2)
        print("   ...one second later, after the lease expired:")
        for u in back:
            print(f"     worker-d picked up {u.name}  (attempt {u.attempts})")
        print("   No daemon ran. reclaim() happens on the way into claim().")

        _rule("6. and the dead worker cannot close what it lost")
        u = back[0]
        print(f"   worker-c calls finish on {u.name}: "
              f"{leases.finish(conn, u.unit_id, worker='worker-c')}")
        print(f"   worker-d calls finish on {u.name}: "
              f"{leases.finish(conn, u.unit_id, worker='worker-d')}")

        _rule("7. the orchestrator collects what they produced")
        # back[0] was closed in step 6; finish the other one WITH a result, so
        # this shows real output rather than a second close that does nothing.
        leases.finish(conn, back[1].unit_id, worker="worker-d",
                      result={"words": 812})
        for r in leases.results(conn, "translate")[:3]:
            print(f"   {r['name']:<10} {r['result']}")
        print(f"   ...{len(leases.results(conn, 'translate'))} finished in total")

        _rule("what this is not")
        print("   At-least-once, not exactly-once. A worker that is SLOW and one")
        print("   that is DEAD are indistinguishable -- no timeout separates")
        print("   them -- so a unit can be done twice. Heartbeat while you work,")
        print("   and make the write at the end idempotent.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
