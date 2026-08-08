# Reference

## Commands

```
fleetwright state             where this project is; run this first
fleetwright init              write a starter fleetwright.toml
fleetwright apply             register skills and define kinds from it
fleetwright start              begin a run; prints its id
fleetwright runs              every run, newest first
fleetwright skill NAME        say what a skill name means
fleetwright skills            registered skills and their use
fleetwright skill-check       re-hash sources, compare to what was registered
fleetwright define KIND       say what this kind of work IS
fleetwright add KIND NAME…    enqueue units; --from-file for a corpus
fleetwright claim [KIND]      take work; exits 1 when the queue is dry
fleetwright done UNIT_ID      mark finished
fleetwright fail UNIT_ID      report one that could not be done
fleetwright release UNIT_ID   hand back, no attempt burned
fleetwright status            what is left, who holds what
fleetwright prompt [KIND]     the spawn prompt, generated from the kind
fleetwright brief UNIT_ID     exactly what one unit was told
fleetwright lineage UNIT_ID   what caused it, and what it caused
fleetwright kinds [KIND]      every definition a kind has had
fleetwright results [KIND]    what the fleet handed back
fleetwright dashboard         a live view of the fleet, in a browser
fleetwright wait              block until done; exit 1 if anything failed
fleetwright retry [NAME...]   put failed units back, attempts reset
fleetwright cancel [NAME...]  stop work that has not started
fleetwright reclaim           return expired leases now
fleetwright serve             the MCP server, on stdio
fleetwright install-skill     teach Claude Code to run fleets
fleetwright demo              a fleet, a crash, a recovery
```

Every command except `demo` takes `--db` (default `work.db`).

### `state`

**The first thing to run in a session that has just arrived.** It finds the
database even if you do not know its name, and ends with the single next
command rather than leaving that to be inferred.

```
fleetwright 0.16.0 · work.db
  520 units: 412 done, 3 failed, 99 waiting, 6 in flight
  kinds: audit, extract
  skills: house-style, xrad-extraction

RUNS
 *20260805-150055-1ce3  extract tomus II       412/520   3 failed  14m
  20260804-165650-24a8  audit tomus I           90/90              9m

NEEDS ATTENTION
  3 unit(s) no worker could finish
    p0071: no text layer in the scan; p0114: OCR returned 0 characters
    -> fleetwright retry --all   # after fixing the cause

NEXT
  fleetwright wait --run 20260805-150055-1ce3   # 105 unit(s) left
```

A summary that reports three failures without saying `fleetwright retry` has
moved the work of knowing the tool onto whoever is reading it, which for a
fresh agent is the entire problem. Every line under NEEDS ATTENTION carries
what to do about it.

Over MCP the same thing is `project_state`, and its description tells an agent
to call it before anything else.

### `init` / `apply`

```bash
fleetwright init            # writes a commented fleetwright.toml
fleetwright apply --run "$RUN"
```

```toml
[skills.house-style]
source  = "docs/house-style.md"
version = "1.0"

[kinds.extract]
instructions = "Read $path and record every claim it makes, quoting verbatim."
done_when    = "every claim in the file is recorded, or you have established there are none"
returns      = '{"claims": <int>, "notes": "<string>"}'
skills       = ["house-style"]
units_glob   = "scans/*.png"
meta         = { path = "scans/$name" }
```

Setting a fleet up was five commands in the right order, living in whoever's
shell history ran them last. This is the same thing in a file you can review,
diff and commit.

**TOML rather than YAML** for one boring reason and one good one: `tomllib` is
in the standard library, so the zero-dependency rule holds; and TOML has no
significant whitespace, so a prompt pasted into it cannot change meaning
because of an indent.

**Applying twice is a no-op.** Both underlying calls replace rather than
append, so an edited file is an edit. A config you are afraid to re-apply is
one people stop applying, and then it stops describing what is running.

Kinds are durable and belong in the file. Units are per run and stay on the
command line, except for the convenience of `units_from` and `units_glob`.

### `start` / `runs`

```bash
RUN=$(fleetwright start --label "Tomus II extraction" --db work.db)
fleetwright add extract --from-file pages.txt --run "$RUN" --db work.db
fleetwright runs --db work.db
```

A **run** is one execution of a fleet. It groups the units so you can ask what
*that* fleet did rather than what the database contains, and it **scopes unit
ids** — without which a second run over the same corpus would find everything
already done and do nothing, while re-running an enumeration *inside* a run
must still add nothing new. Both are wanted; they are only compatible if the
run is part of the key.

There is no `end`. A run is over when its units are, which has to be derivable
because the orchestrator is the process most likely to have died.

`--run` also filters `claim`, `status`, `results` and `dashboard`.

### `skill-check`

```bash
fleetwright skill-check              # every registered skill with a digest
fleetwright skill-check xrad-extraction
```

```
  xrad-extraction          OK      99cdba1cede56a04
  latin-palaeography       CHANGED registered 4465e472f5ecbf95, now 8b21c0e4d9a1f772
```

Re-hashes each source and compares it to what was registered. Exits 1 if
anything changed, because units claimed before and after used different text.
The brief prints a digest, and without this nothing could confirm the file a
worker just read hashes to it: a fingerprint you cannot check at the moment it
matters is decoration.

### `skill` / `skills`

```bash
fleetwright skill xrad-extraction --source skills/xrad/SKILL.md --version 1.2
fleetwright skill latin-palaeography --source https://example.org/pal --version 0.4
fleetwright skills
```

```
skill                version   digest              units  source
 xrad-extraction     1.2       27efcd11f4a60074      412  skills/xrad/SKILL.md
 latin-palaeography  0.4       -                     412  https://example.org/pal
?never-registered    -         -                      18
```

Without this a kind carries the bare string `xrad-extraction` and nothing
anywhere knows what that is, where a worker gets it, or which version it was.
Three kinds needing the same skill repeat the string, and renaming means
editing all three.

**A readable file is hashed.** That is what makes *"which version did these 400
units use"* answerable after someone edits a skill halfway through a run —
`--version` alone relies on the author remembering to bump it.

Skills used by units but never registered are listed with a `?`. That means a
kind names something nothing records where to get.

**Nothing is fetched or installed.** Distribution belongs to whatever runs your
agents — see [Concepts](concepts.md).

### `define`

```bash
fleetwright define extract \
  --instructions 'Read $path. Record every claim it makes, quoting verbatim.' \
  --done-when    'every claim on the page is recorded, or you have established
                  there are none' \
  --returns      '{"claims": <int>, "notes": "<string>"}' \
  --tools        'the xrad MCP server: record_claim, check_quote'

fleetwright define extract --instructions-file prompts/extract.md
```

Say what the work **is**, once. Every worker that claims a unit of this kind is
handed these instructions — including a worker spawned an hour later, and the
one that inherits a unit a crashed worker dropped. That is the reason they live
here and not in the prompt you spawn workers with.

`$name` is the unit; `$key` is any string or number in that unit's `meta`.
Substitution is `string.Template`, so **JSON in your instructions is safe** and
an unknown `$placeholder` is left alone rather than failing at the moment a
worker asks for work.

Re-defining replaces. Instructions are read at claim time, so a correction
reaches every worker that has not yet claimed, without restarting anything.

### `add`

```bash
fleetwright add translate p1 p2 p3
fleetwright add translate --from-file pages.txt --priority 5
fleetwright add extract --from-file pages.txt --meta '{"path": "scans/$name.png"}'
ls corpus/ | fleetwright add extract --from-file -
```

Keyed on `kind:name`, so re-running an enumeration after the corpus grows adds
only what is new. The same name under two kinds is two units.

### `claim`

```bash
fleetwright claim translate --lease 1800 -n 5
fleetwright claim --json                      # the unit and its full brief
fleetwright claim extract --brief             # just the assignment, as text
```

`--brief` prints the whole assignment and nothing else, for handing straight to
an agent:

```bash
fleetwright claim extract --brief | claude -p -
```

Exits **1** with no stdout when there is nothing to take, so:

```bash
while unit=$(fleetwright claim extract --json); do … done
```

`--worker` defaults to `hostname:pid`. `--model` records what the worker says
it is — declared, never verified — so `stats()` can compare one model's work
against another's. `FLEETWRIGHT_MODEL` works too.

### `finish` / `fail` / `release`

```bash
fleetwright finish  extract:p0189 --worker w3      # `done` is an alias
fleetwright finish  extract:p0189 --token "$TOKEN" # from the brief
fleetwright finish  extract:p0189 --worker w3 --cost 0.031 --tokens-in 3100
fleetwright finish  extract:p0189 --worker w3 --result-file out.json
fleetwright fail    extract:p0189 --worker w3 --note "no text layer"
fleetwright release extract:p0189 --worker w3 --note "wrong language"
```

**A close has to say who it is**, with `--worker`, `--token`, or an explicit
`--any-worker`. There is no default, and that is deliberate: the library
defaults an omitted worker to `hostname:pid`, which is correct for a library
(one process claims and finishes) and wrong here, because a shell worker claims
in one command and finishes in another. Inheriting that default refused every
close in the documented shell pattern, silently, while reporting "another
worker holds it".

`--token` is the stronger one. A worker *name* can be shared by two processes
told to call themselves the same thing; the token is unique per claim.

`--cost`, `--tokens-in` and `--tokens-out` are **declared, never measured**:
nothing here can observe a model's usage. They are stored because they are the
only thing you cannot reconstruct afterwards, and because *"did the cheaper
model do these worse"* is the question a fleet operator actually has. `stats()`
rolls them up per model, per kind and per run, and reports how many units
reported at all, so a total over 3 of 400 is not quoted as the run's cost.

Use `--result-file` for anything large: Linux caps a single shell argument at 128 KB whatever `ARG_MAX` says.

`finish` exits 1 if the lease had already expired and another worker owns the
unit. `fail` retries until attempts run out, then sets the unit aside. `release`
does not count against the limit.

### `status`

```bash
fleetwright status --who
fleetwright status extract --json
```

### `prompt`

```bash
fleetwright prompt extract --db work.db -n 4
```

The prompt to spawn workers with, **generated from the kind** rather than
copied out of documentation. It is generic about the task — that comes from the
queue at claim time — and specific about what the worker must have, because a
skill it never loaded is not something it can discover halfway through.

A prompt pasted out of a README drifts from the kind it was written for, and
nothing tells you when it has. A test asserts every command the prompt prints
actually parses against the real CLI.

### `finish --then`

```bash
fleetwright finish "$UNIT" --then '{"audit": ["p001-c0", "p001-c1"]}'
```

The only way one unit causes another to exist. The new units inherit the
finishing unit's run and record it as their parent, so `wait --run` covers the
pipeline instead of returning when stage one drains.

Refused, with the unit still yours, if the JSON is malformed or names a kind
nothing has defined. Enqueueing into an undefined kind hands its worker a bare
name, and a worker with no instructions does the wrong work confidently.

### `claim --spawned-by`

```bash
export FLEETWRIGHT_SPAWNED_BY="session-a"     # once, before spawning
```

Who spawned this worker. Declared, never measured: a subagent cannot observe
that a session spawned it, so it has to be told. The environment variable is
the useful form, because a subagent inherits its parent's environment and one
export labels the whole fleet without editing any worker prompt.

### `backup`

```bash
fleetwright backup work.db.2026-08-08
```

**`cp work.db elsewhere/` is the obvious thing to do and it is wrong.** In WAL
mode the most recent commits live in `work.db-wal`, so copying the one file
gets a database missing whatever finished last, and it fails silently, because
what you copied is a perfectly valid database of an earlier moment. That is the
shape of every "my data went backwards" report.

`backup` runs `VACUUM INTO`, which reads a live database without taking a lock
off the writers, folds the WAL in, and writes one file with nothing beside it.
It refuses to overwrite: a backup command that can destroy the previous backup
is not a backup command.

### Which database am I talking to?

Three ways to end up with an empty queue that reports itself as perfectly
healthy, and what each now does:

```bash
cd sub && fleetwright status     # finds the project's work.db up the tree
fleetwright status --db worrk.db # "did you mean work.db?", exit 2
export FLEETWRIGHT_DB=/abs/work.db   # pins one file for the whole session
```

`work.db` is a **relative** default, so before this a subdirectory got its own
brand-new database rather than the project's. It is now searched for up the
tree, the way git finds a repository. An explicit `--db` is always honoured
literally, but a name close to an existing database is a typo far more often
than a new project, so that is refused rather than created; pass `--create` if
you mean it. And any command that does create a file now says so on stderr,
because a new database appearing in silence is indistinguishable from the old
one having been emptied.

`FLEETWRIGHT_DB` is the one to use for a fleet: a subagent inherits its
parent's environment, so exporting it once points every worker at the same
file no matter what directory each of them runs in.

### `lineage`

```bash
fleetwright lineage 'run/gloss:p001-c0-g'
```

```
extract:p001  [done]
  audit:p001-c0  [done]
    gloss:p001-c0-g  [done]  <- this one
```

`then={"audit": [...]}` is the only place a unit causes another unit to exist,
and it is the only relationship here that is not a hierarchy: run, kind and
worker all fan out from one parent, but lineage is a chain across stages and it
branches.

**It also fixes a bug.** Follow-on units used to inherit neither the parent's
run nor a link to it, so a second stage fell out of the run entirely: `wait
--run` returned as soon as stage one finished, and `runs` under-reported the
work.

### `brief` / `kinds`

```bash
fleetwright brief 'run/extract:p0189'   # what THAT unit was told
fleetwright kinds extract               # every definition it has had
```

```
kind              digest             units  first line of instructions
 extract          8e21c0e4d9a1f772      140  Read $path. Record every claim...
*extract          4465e472f5ecbf95      100  Read $path. Record every claim, and
```

A kind's definition is **pinned onto each unit when it is claimed**, keyed by
the hash of its content, so `brief` answers what a unit was actually told
however the kind has changed since. `spec()` reports what a kind says *now*,
which is precisely the wrong answer when you are asking why one unit's output
looks different.

Content-addressed rather than copied: storing the rendered brief on every unit
is O(units), and a 400,000 unit corpus would carry most of a gigabyte of
near-identical text. One row per distinct definition instead, and the brief is
re-derived from it plus that unit's own `meta`.

### `results`

```bash
fleetwright results extract --json
```

What finished units handed back, in the order they finished. For the process
that spawned the fleet and now has to assemble the output.

### `dashboard`

```bash
fleetwright dashboard --db work.db              # http://127.0.0.1:8787
fleetwright dashboard --db work.db --out fleet.html   # static snapshot
```

Six tiles (left, done, in flight, failed, throughput, ETA), units finished over
time, progress per kind, **what every worker is holding right now and for how
long**, a per-worker table, and everything nobody could finish with the reason.

Served from `http.server` with the page's CSS and JS inline — no framework, no
build step, nothing fetched from a CDN. Read-only: it opens the database,
reads, and serves, so pointing it at a live run cannot disturb the run.

Binds to **loopback only**. It exposes queue contents and machine names and has
no authentication; `--host` will override that, deliberately explicitly.

### `dashboard` layout

Two sidebars and a detail pane.

| | |
|---|---|
| **Rail** (left) | brand, **Projects**, and the session — signed-in state and **Sign out** |
| **Second sidebar** | **Views** (Overview / Jobs) and the **Runs** list |
| **Detail** | the selected view, scoped to the selected run |

**Jobs** is the only view that shows individual units rather than counts:
status, worker, attempts, elapsed, lease remaining, and the note or result. It
filters by status and searches name, worker and note — so a failure is findable
by what it said. The list is bounded and **says when it truncated**, because a
view that silently shows the first 300 of 40,000 is a view that lies.

### `wait`

```bash
fleetwright wait --run "$RUN" --timeout 3600
```

Blocks until nothing is open and nothing is in flight. **The exit code is the
interface**: `0` finished cleanly, `1` something failed, `2` timed out. Without
it every script driving a fleet wraps a polling loop around `status` and parses
text out of it.

Progress goes to stderr and only when it changes, so stdout stays clean and an
hour-long run does not print eighteen hundred identical lines.

### `retry`

```bash
fleetwright retry --run "$RUN"          # every failed unit in that run
fleetwright retry p0189 p0233           # just these
fleetwright retry --all --include-cancelled
```

**Attempts go back to zero**, not up by one. The unit failed under the old
code; carrying its history forward would retire it again after a single try,
which is exactly wrong when the thing that changed is the fix. The note is
kept, because why it failed last time is still worth reading.

Refuses to run without a scope. Bare `retry` would reopen every failed unit in
the file across every run, which is never what anyone means and cannot be
undone.

### `cancel`

```bash
fleetwright cancel --run "$RUN"         # stop what has not started
fleetwright cancel --run "$RUN" --now   # and take back what is in flight
```

By default it cancels `open` units only and lets in-flight work finish, because
half-finished work is still work. `--now` also takes back leased units, and the
workers holding them find out the way they find out about any lost lease:
`finish` returns false.

**Cancelled is a status, not a deletion.** A queue that forgets what you
cancelled cannot answer why a run came up short three weeks later.

### `reclaim`

Returns expired leases immediately. Rarely needed — `claim` does it — but
useful when you want to see the state before any worker asks.

### `serve`

```bash
fleetwright serve --db work.db
```

See [MCP](mcp.md).

### `install-skill`

```bash
fleetwright install-skill          # this project: .claude/skills/fleetwright/
fleetwright install-skill --user   # every project on this machine
```

Writes the bundled skill where Claude Code will find it. After that you ask in
English, and Claude defines the work, enqueues it, spawns the workers **in one
message so they run at once**, waits, and checks the database rather than the
agents' own reports.

The skill ships inside the wheel at `fleetwright/skill/SKILL.md`, so there is
exactly one copy and it cannot drift from the CLI it documents.

### `demo`

```bash
uvx fleetwright demo
```

Runs against a temporary database, so it is safe anywhere.

## Python API

```python
import fleetwright as sa

conn = sa.connect("work.db")

sa.define(conn, kind, instructions, *, done_when=None, returns=None, tools=None)
sa.spec(conn, kind)                                   -> dict | None
sa.add(conn, kind, names, priority=0, meta=None)      -> int (how many were new)
sa.claim(conn, kind=None, *, worker=None, lease=900, n=1)  -> list[Unit]
sa.heartbeat(conn, unit_ids, *, worker, lease=900)    -> int (rows extended)
sa.finish(conn, unit_id, *, worker=None, note=None, result=None, then=None) -> bool
sa.fail(conn, unit_id, *, note, worker=None)          -> bool
sa.release(conn, unit_id, *, worker=None, note=None)  -> bool
sa.reclaim(conn)                                      -> int
sa.progress(conn, kind=None)                          -> {kind: {status: n}}
sa.leased(conn)                                       -> rows, who holds what
sa.failures(conn)                                     -> rows, with the note
sa.results(conn, kind=None)                           -> what workers handed back
```

`Unit` carries `unit_id`, `kind`, `name`, `attempts`, `leased_until`, `meta`,
the rendered `instructions` / `done_when` / `returns` / `tools`, a
`seconds_left` property, and `brief()` — the whole assignment as one block of
text, because an agent handed four fields will read one of them.

`then` on `finish` enqueues the next stage, and is how a pipeline is built
without this becoming a scheduler:

```python
sa.finish(conn, u.unit_id, result={"claims": 12},
          then={"audit": [f"claim-{i}" for i in ids]})
```

Nothing is enqueued if the close failed, so a worker that lost its lease cannot
inject work off the back of a unit it no longer owns.

The booleans are load-bearing: `False` from `finish` means the lease expired
and another worker owns the unit. Do not assert on it — handle it.

## Versioning

Semantic, with one project-specific reading: **a lease becoming weaker is a
breaking change.** If `claim` starts handing out units it used to withhold, code
relying on that exclusivity is silently doing work twice.
