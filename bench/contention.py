#!/usr/bin/env python3
"""How many real processes can share one lease file before it falls over.

Processes, not threads. Threads in one interpreter share a GIL and a connection
pool and would measure almost nothing: the whole question is what SQLite does
when N *operating system* processes contend for one write lock on one file.

What it asserts, and why each one matters:

- **every unit finishes** -- a queue that loses work under load is not a queue
- **no unit is claimed twice** -- the single atomic UPDATE is the entire safety
  argument, so this is the number that would falsify it
- **no SQLITE_BUSY reaches the caller** -- `busy_timeout` is supposed to turn
  contention into waiting rather than into an exception a worker cannot handle

Latency is reported at p50/p95/p99 rather than as a mean. Under lock contention
the distribution is long-tailed by construction, and a mean hides exactly the
part a worker feels.

    python bench/contention.py                 # 32 workers, 2000 units
    python bench/contention.py 64 5000
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fleetwright as sa  # noqa: E402


def worker(db: str, out: mp.Queue) -> None:
    """Claim and finish until the queue is dry, recording what happened."""
    conn = sa.connect(db)
    me = f"w{os.getpid()}"
    claimed, latencies, busy = [], [], 0
    while True:
        t0 = time.perf_counter()
        try:
            got = sa.claim(conn, "unit", worker=me, lease=120)
        except sqlite3.OperationalError as e:
            # The thing that must not happen. Recorded rather than retried,
            # because retrying here would hide it.
            busy += 1
            if "locked" in str(e) or "busy" in str(e):
                continue
            raise
        latencies.append(time.perf_counter() - t0)
        if not got:
            break
        u = got[0]
        claimed.append(u.unit_id)
        sa.finish(conn, u.unit_id, worker=me, token=u.token,
                  result={"n": len(claimed)})
    conn.close()
    out.put({"worker": me, "claimed": claimed, "latencies": latencies,
             "busy": busy})


def main(n_workers: int = 32, n_units: int = 2000) -> int:
    tmp = Path(tempfile.mkdtemp()) / "bench.db"
    conn = sa.connect(tmp)
    sa.define(conn, "unit", "do $name", done_when="d")
    run = sa.start_run(conn, label=f"contention {n_workers}x{n_units}")
    sa.add(conn, "unit", [f"u{i:06d}" for i in range(n_units)], run=run)
    conn.close()

    print(f"{n_workers} processes, {n_units} units, SQLite "
          f"{sqlite3.sqlite_version}, {os.cpu_count()} cores")

    out: mp.Queue = mp.Queue()
    procs = [mp.Process(target=worker, args=(str(tmp), out))
             for _ in range(n_workers)]
    t0 = time.perf_counter()
    for p in procs:
        p.start()
    results = [out.get() for _ in procs]
    for p in procs:
        p.join()
    wall = time.perf_counter() - t0

    all_claims = [c for r in results for c in r["claimed"]]
    lat = sorted(x for r in results for x in r["latencies"])
    busy = sum(r["busy"] for r in results)
    duplicates = len(all_claims) - len(set(all_claims))

    conn = sa.connect(tmp)
    done = conn.execute("SELECT count(*) FROM unit WHERE status='done'"
                        ).fetchone()[0]
    other = conn.execute("SELECT count(*) FROM unit WHERE status!='done'"
                         ).fetchone()[0]

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))] * 1000

    print(f"  finished        {done}/{n_units}"
          + (f"   ({other} NOT done)" if other else ""))
    print(f"  duplicates      {duplicates}")
    print(f"  SQLITE_BUSY     {busy}")
    print(f"  wall            {wall:.2f}s   ({done / wall:.0f} units/s)")
    print(f"  claim latency   p50 {pct(0.50):.1f} ms   "
          f"p95 {pct(0.95):.1f} ms   p99 {pct(0.99):.1f} ms")

    ok = done == n_units and duplicates == 0 and busy == 0 and other == 0
    print("  RESULT          " + ("ok" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    args = [int(x) for x in sys.argv[1:3]]
    raise SystemExit(main(*args))
