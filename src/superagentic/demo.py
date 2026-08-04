"""`superagentic demo` — a fleet, a crash, and a recovery, in sixty seconds.

Runs against a throwaway database so it can be run anywhere, including from a
`uvx` with nothing installed. ASCII only: box-drawing characters raise
UnicodeEncodeError on a Windows console under the default code page, which is
a silly way for a demo to fail on the platform it most needs to reassure.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from . import leases


def _rule(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(0, 58 - len(title)))


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        conn = leases.connect(Path(d) / "demo.db")

        _rule("1. a corpus of 6 pages is enqueued")
        n = leases.add(conn, "translate", [f"page-{i}" for i in range(1, 7)])
        print(f"   {n} units queued")
        print(f"   re-running the same enumeration adds "
              f"{leases.add(conn, 'translate', [f'page-{i}' for i in range(1, 7)])}"
              f" -- it is keyed on kind:name")

        _rule("2. three workers claim, and never collide")
        taken = {}
        for w in ("worker-a", "worker-b", "worker-c"):
            got = leases.claim(conn, "translate", worker=w, n=2, lease=1.0)
            taken[w] = [u.name for u in got]
            print(f"   {w}: {', '.join(taken[w])}")
        everything = [n for names in taken.values() for n in names]
        print(f"   {len(everything)} units handed out, "
              f"{len(set(everything))} distinct -- nobody got the same page")

        _rule("3. two finish. the third crashes, holding its work")
        for w in ("worker-a", "worker-b"):
            for name in taken[w]:
                leases.finish(conn, f"translate:{name}", worker=w)
            print(f"   {w}: done")
        print("   worker-c: [process dies without reporting anything]")

        _rule("4. its lease expires, and the work comes back")
        print("   another worker asks immediately:")
        print(f"     {leases.claim(conn, 'translate', worker='worker-d') or 'nothing -- still leased'}")
        time.sleep(1.1)
        back = leases.claim(conn, "translate", worker="worker-d", n=2)
        print("   ...one second later, after the lease expired:")
        for u in back:
            print(f"     worker-d picked up {u.name}  (attempt {u.attempts})")
        print("   No daemon ran. reclaim() happens on the way into claim().")

        _rule("5. and the dead worker cannot close what it lost")
        u = back[0]
        print(f"   worker-c calls finish on {u.name}: "
              f"{leases.finish(conn, u.unit_id, worker='worker-c')}")
        print(f"   worker-d calls finish on {u.name}: "
              f"{leases.finish(conn, u.unit_id, worker='worker-d')}")

        _rule("what this is not")
        print("   At-least-once, not exactly-once. A worker that is SLOW and one")
        print("   that is DEAD are indistinguishable -- no timeout separates")
        print("   them -- so a unit can be done twice. Heartbeat while you work,")
        print("   and make the write at the end idempotent.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
