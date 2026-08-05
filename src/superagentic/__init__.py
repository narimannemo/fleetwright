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

from . import config, shape
from .leases import (
    CANCELLED,
    DEFAULT_LEASE,
    DONE,
    FAILED,
    LEASED,
    MAX_ATTEMPTS,
    OPEN,
    STATUSES,
    TERMINAL,
    Unit,
    add,
    brief_for,
    cancel,
    claim,
    connect,
    define,
    fail,
    failures,
    finish,
    heartbeat,
    iter_results,
    kind_digest,
    kind_versions,
    leased,
    outstanding,
    progress,
    reclaim,
    register_skill,
    release,
    resolve_skills,
    results,
    retry,
    run,
    runs,
    skills,
    spec,
    start_run,
    state,
    this_worker,
    unit_id,
    units,
    worker_prompt,
)


def skill_text() -> str:
    """The Claude Code skill, as text. One copy, shipped inside the package.

    `superagentic install-skill` writes this into a project. Keeping it here
    rather than beside the source means it travels in the wheel and cannot
    drift from the CLI it documents.
    """
    from pathlib import Path
    return (Path(__file__).parent / "skill" / "SKILL.md").read_text(encoding="utf-8")


__version__ = "0.17.0"

__all__ = ["CANCELLED", "DEFAULT_LEASE", "DONE", "FAILED", "LEASED",
           "MAX_ATTEMPTS", "OPEN", "STATUSES", "TERMINAL",
           "Unit", "add", "brief_for", "cancel", "claim", "connect", "define", "fail", "failures",
           "finish", "heartbeat", "iter_results", "kind_digest",
           "kind_versions", "leased", "progress", "reclaim", "release",
           "outstanding", "register_skill", "resolve_skills", "results", "retry",
           "run", "runs", "state",
           "skills", "spec", "start_run", "this_worker",
           "config", "shape", "skill_text", "unit_id", "units", "worker_prompt"]
