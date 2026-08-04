"""Work leases in one SQLite file, so a fleet of workers divides a job list
instead of racing it.

Ten workers pointed at the same corpus all start on page one. `superagentic` is
the table they check first: one of them takes the page, the others are told to
take a different one, and if the one that took it dies, its work comes back.

    import superagentic as sa

    conn = sa.connect("work.db")
    sa.add(conn, "translate", [f"page-{i}" for i in range(1, 500)])

    while (units := sa.claim(conn, "translate", lease=1800)):
        for u in units:
            do_the_work(u.name)
            sa.finish(conn, u.unit_id)

A lease, not a lock: a lock held by a crashed worker is worse than no lock at
all, because nothing can tell a busy worker from a dead one. See
`superagentic.leases` for the reasoning, and `docs/concepts.md` for what this
deliberately does not give you — starting with exactly-once, which nothing can.
"""

from .leases import (
    DEFAULT_LEASE,
    DONE,
    FAILED,
    LEASED,
    MAX_ATTEMPTS,
    OPEN,
    Unit,
    add,
    claim,
    connect,
    define,
    fail,
    failures,
    finish,
    heartbeat,
    leased,
    progress,
    reclaim,
    register_skill,
    release,
    resolve_skills,
    results,
    run,
    runs,
    skills,
    spec,
    start_run,
    this_worker,
    unit_id,
    units,
    worker_prompt,
)

__version__ = "0.8.0"

__all__ = ["DEFAULT_LEASE", "DONE", "FAILED", "LEASED", "MAX_ATTEMPTS", "OPEN",
           "Unit", "add", "claim", "connect", "define", "fail", "failures",
           "finish", "heartbeat", "leased", "progress", "reclaim", "release",
           "register_skill", "resolve_skills", "results", "run", "runs",
           "skills", "spec", "start_run", "this_worker",
           "unit_id", "units", "worker_prompt"]
