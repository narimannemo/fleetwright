"""A file that says what the work is, so it lives in git rather than history.

Setting up a fleet took five commands in the right order, and they lived in
whoever's shell history ran them last. Nothing was reviewable, nothing was
diffable, and the only record of how a corpus was extracted was the extraction
itself.

    fleetwright apply fleetwright.toml

TOML, not YAML, for one boring reason and one good one. The boring one is that
`tomllib` is in the standard library since 3.11 and this package has no
dependencies. The good one is that TOML has no significant whitespace, so a
prompt pasted into it cannot change meaning because of an indent.

**What belongs in the file and what does not.** A kind is durable: it is what
this work IS, it changes rarely, and it should be reviewed when it changes.
Units are per run and usually come from a glob or a listing, so they stay on
the command line. The file may name a source for them as a convenience, and
that is the only place the two mix.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import leases

EXAMPLE = '''# fleetwright.toml
#
#   fleetwright apply          # register the skills, define the kinds
#
# Kinds are durable: this is what the work IS. Units are per run and stay on
# the command line, because they usually come from a listing.

# [skills.<name>] says what a skill name means. A readable source is hashed,
# so units claimed before and after an edit stay tellable apart.
[skills.house-style]
source  = "docs/house-style.md"
version = "1.0"

[kinds.extract]
# Written for an agent with NO other context. $name is the unit; $key is any
# value in that unit's meta.
instructions = """
Read $path and record every claim it makes, quoting verbatim.
Do not paraphrase, and do not record a claim you cannot quote.
"""

# The field people skip and the one that costs most. Without it every worker
# decides for itself when to stop, and they disagree.
done_when = "every claim in the file is recorded, or you have established there are none"

# Checked. A worker handing back the wrong shape is refused and keeps its
# lease, so it can fix the shape and finish again.
returns = '{"claims": <int>, "notes": "<string>"}'

# How many hand-outs before a unit of this kind stays failed instead of going
# back in the queue. Belongs to the WORK: an expensive kind may deserve one
# attempt and a flaky one five. Omit for the default of 3.
# max_attempts = 3

# What a worker must HAVE. It is told to fail rather than improvise without it.
skills = ["house-style"]
mcp    = { xrad = "xrad serve --db graph.db" }

# Optional: where this kind's units come from, for the common case where the
# list is a file or a glob.
# units_from = "units.txt"
# units_glob = "scans/*.png"
# meta       = { path = "scans/$name" }
'''


def load(path: str | Path) -> dict:
    """Parse the file, or say precisely what is wrong with it."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no config at {p}")
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{p}: {e}") from None


def apply(conn, cfg: dict, *, root: Path | None = None,
          force: bool = False) -> dict:
    """Register the skills and define the kinds. Idempotent.

    Both underlying calls replace rather than append, so applying the same file
    twice is a no-op and applying an edited one is an edit. That matters more
    than it sounds: a config you are afraid to re-apply is a config people stop
    applying, and then it stops describing what is actually running.
    """
    root = Path(root or ".")
    out: dict = {"skills": [], "kinds": [], "units": {}, "warnings": []}

    for name, spec in (cfg.get("skills") or {}).items():
        src = spec.get("source")
        if src and not Path(src).is_absolute():
            # Relative to the config, not to wherever you happened to run it.
            src = str((root / src).resolve())
        leases.register_skill(conn, name, source=src,
                              version=spec.get("version"),
                              note=spec.get("note"))
        out["skills"].append(name)

    for name, spec in (cfg.get("kinds") or {}).items():
        if "instructions" not in spec:
            raise ValueError(f"kind {name!r} has no instructions")
        if not spec.get("done_when"):
            out["warnings"].append(
                f"kind {name!r} has no done_when: every worker will decide for "
                "itself when to stop, and they will disagree")
        for s in spec.get("skills") or []:
            if s not in (cfg.get("skills") or {}) and \
                    not leases.spec(conn, s):
                out["warnings"].append(
                    f"kind {name!r} requires skill {s!r}, which nothing "
                    "registers: workers are told to load it but not where "
                    "to get it")
        ctx = spec.get("context")
        if ctx and (root / ctx).is_file():
            ctx = (root / ctx).read_text(encoding="utf-8")
        leases.define(conn, name, spec["instructions"],
                      done_when=spec.get("done_when"),
                      returns=spec.get("returns"),
                      tools=spec.get("tools"),
                      skills=spec.get("skills"),
                      mcp=spec.get("mcp"),
                      context=ctx, max_attempts=spec.get("max_attempts"),
                      force=force)
        out["kinds"].append(name)
    return out


def units_for(cfg: dict, kind: str, *, root: Path | None = None) -> tuple[list[str], dict]:
    """The unit names a kind declares a source for, and its meta. May be empty.

    Only `units_from` and `units_glob` are honoured, and both are conveniences.
    Anything more expressive belongs in the shell that already knows how to
    list your work.
    """
    root = Path(root or ".")
    spec = (cfg.get("kinds") or {}).get(kind) or {}
    names: list[str] = []
    if spec.get("units_from"):
        f = root / spec["units_from"]
        names += [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
                  if ln.strip()]
    if spec.get("units_glob"):
        names += [p.name for p in sorted(root.glob(spec["units_glob"]))]
    # Order-preserving dedup: a file and a glob that overlap should not enqueue
    # the same unit twice, and `add` would silently ignore the second anyway.
    seen, unique = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique, spec.get("meta") or {}
