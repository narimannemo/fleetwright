# Changelog

Kept by hand, in [Keep a Changelog](https://keepachangelog.com) shape. The
release workflow reads the section matching the tag and fails if there isn't
one — release notes generated from commit subjects tell a reader what changed
and never why.

## [0.21.0] — 2026-08-07

**Renamed from `superagentic` to `fleetwright`.** Same project, same author,
new name on PyPI. Entries below 0.21.0 keep the old name because that is what
those releases actually shipped as; rewriting them would make this file lie
about history.

```bash
pip install fleetwright        # was: pip install superagentic
fleetwright state              # was: superagentic state
```

`SUPERAGENTIC_TOKEN` and friends are now `FLEETWRIGHT_*`.

### Fixed — an external audit, every finding reproduced first

- **`finish`, `fail` and `release` did not check ownership by default.** The
  predicate was only added when a worker name was passed, and all three
  defaulted it to `None`, so "a worker cannot finish a unit it does not hold"
  was opt-in — while `claim` had always defaulted its worker. A stale worker's
  result overwrote the live holder's, the live holder was refused, and the row
  was credited to the wrong worker. `worker=ANY` (`--any-worker`) keeps the
  deliberate unowned close.
- **The dashboard reclaimed leases on every GET**, because `stats()` begins
  with `reclaim()`. A tab left open on a live run stole leases from workers
  that were merely slow, at the polling cadence: the panel built to show what
  is stuck was what made it stuck. It now passes `reclaim_first=False` and
  opens the file `mode=ro`, so SQLite refuses a write rather than the code
  promising not to attempt one.
- **`echo null | fleetwright serve` killed the server**, stranding every lease
  the process held. `handle()` called `.get` on whatever JSON arrived, and a
  JSON-RPC batch is a list the spec requires a server to accept. Batches now
  work, notifications get no reply (the spec says MUST NOT, and real clients
  send `notifications/cancelled` routinely), parse errors are answered rather
  than silently dropped, and `initialize` echoes a protocol version we speak.
- **Stored XSS in `dashboard --out`.** `json.dumps` does not escape `</`, so a
  unit named `</script><img src=x onerror=…>` closed the tag and the rest of
  the snapshot was attacker-controlled. No database access needed: a worker
  enqueues that name over MCP and the orchestrator mails the file to someone.
- **`reclaim()` overwrote the real failure note** with "lease expired and out
  of attempts" — the tautology replacing the only record of why, in the column
  `failures()` and the "Could not finish" panel exist to show.
- **`release()` burned an attempt** while being documented as "without calling
  it a failure", so six honest hand-backs left a unit at the limit and the next
  worker merely to crash sent it straight to `failed`.
- **`claim --max-attempts` retired unrelated work.** It was passed into the
  global `reclaim()`, which applies its limit to every expired unit in the
  file: `claim b --max-attempts 1` retired three units of kind `a` in another
  run. Retirement is now a per-kind policy (`define --max-attempts`) read per
  unit, and the flag on `claim` bounds only what that call hands out.
- **`finish(then=…)` committed the close and then raised.** Nothing validated
  `then`, so a bad element raised inside `add()` after the unit was already
  `done`: the caller saw a failure on a unit that had finished, lost the whole
  follow-on stage, and could not retry because the unit was closed. A bare
  string also enqueued one unit per character. Validated before the close now.
- **A claimed unit could have no `kind_digest`.** The pin ran after the claim
  committed, so a crash in between left a leased row unpinned and `brief_for`
  fell back to the *current* definition — the exact answer pinning exists to
  prevent. The pin now happens inside the claiming `UPDATE`.
- **`state()` raised forever on an unreadable skill source** — the function
  whose description says CALL THIS FIRST. A skill that became binary or lost
  its read permission is now reported in `attention` rather than fatal.
- **`?limit=abc` dropped the connection** instead of returning 400, `_find_db`
  leaked a connection per candidate file, `stats(buckets=0)` raised, and
  `reclaim()` reported only reopened units and not retirements.

### Added

- **The lease token as a credential.** Ownership rested on a worker *name*, and
  two processes sharing one are indistinguishable — which the generated prompt
  guaranteed, since it printed `--worker agent-1` for every worker it made.
  `claim` returns a per-claim token, every brief carries it, and `finish`,
  `fail`, `release` and `heartbeat` accept `token=` / `--token`. Optional: a
  shell fleet should not have to plumb it through `jq` on day one. `prompt` no
  longer emits a shared name at all.
- **`define --max-attempts` / `max_attempts` in `fleetwright.toml`.**
- **`bench/contention.py`**, and its numbers in the README. 64 processes over
  5,000 units: all finish, 0 duplicates, 0 `SQLITE_BUSY`, 1,419 units/s, p99
  claim 667 ms — the tail is published too, because it grows with worker count
  and would matter for millisecond-long units.
- **Content-Security-Policy and X-Frame-Options** on the dashboard, and
  sessions that expire server-side to match the cookie.
- A dashboard screenshot in the README.

### Notes

- **A test now drives every dashboard route against a live run and compares the
  tables.** The old one passed throughout the bug: it grepped `dashboard.py`
  for write verbs while the write was inside `leases.stats()`; it asserted a
  unit was still `open` without ever claiming it, so it checked the one state
  `reclaim` cannot touch; and its file-size assertion passed when the file grew.
- Every new test here was checked by reverting its fix and confirming it fails.
  That found two of mine asserting nothing.
- The README no longer presents leases as a discovery. Gray and Cheriton 1989,
  SQS 2006, Beanstalkd TTR 2007, and litequeue already does expiring claims on
  SQLite.
- "Idempotent, so a unit done twice converges" is gone. It is true of
  deterministic work and false of the generative example on the same page.

## [0.20.0] — 2026-08-06

### Added

- **The pipeline drawn as nodes and edges**, in the "what caused what" panel,
  and `superagentic.graph()` behind it. Node height is unit count, edge width
  is how many units one kind caused in another, and the strip along each node's
  bottom is its status mix, so the shape carries the numbers rather than
  decorating them.

  **Laid out in columns, not force-directed.** A pipeline has a direction and a
  force layout throws it away: the same data lands somewhere different on every
  load. A node's column is the longest path to it, so a merge sits past the
  longest branch feeding it rather than beside it. Cycles terminate rather than
  spin, because nothing stops an `audit` kind enqueueing back into `extract`.

  **One node per kind, not per unit**, for the same reason the flow list was:
  255 unit nodes is a hairball and 400,000 is a dead tab. The exact counts stay
  in the list underneath, which is also the reading that does not depend on
  colour.

### Notes

- **A test now fails if any CSS declaration names a token that does not
  exist.** The first version of the diagram used `var(--card)` where the token
  is called `--raise`. A declaration naming an undefined custom property is
  dropped, and an SVG rect or text with no fill is **black**, so the whole
  diagram rendered as black boxes with invisible labels on a black ground while
  the server returned 200, the console stayed clean and every test passed.

## [0.19.1] — 2026-08-06

Both found by opening the dashboard and looking at it.

### Fixed

- **A run's elapsed time could be negative**, printing `-755.0s` with a blank
  parallelism, which reads as a broken product rather than what it is.
  Workers on a second machine write their own clock into the shared database
  and two hosts a minute apart is ordinary, so a unit that finished "before"
  its run began is a thing that happens. Elapsed now spans every timestamp on
  record rather than trusting the run's own clock. Clamping to zero would have
  been worse: it would say a run that plainly did work took no time.
- **`python -m superagentic.cli` died with `NameError: name '_cmd_init' is not
  defined`.** The `if __name__ == "__main__"` guard sat two thirds of the way
  down the file, so running the module as a script called `main()` before the
  handlers below it existed. The console script was fine, because importing a
  module runs all of it before anything calls `main()`, which is exactly why no
  test caught it: an in-process test finishes importing first. Now tested by
  subprocess, which is the only way to see it.

### Added

- **`python -m superagentic`**, for a wheel installed with `pip install
  --target`, a zipapp, or a virtualenv whose `bin` is not on PATH: cases where
  the package imports and the command does not exist.

## [0.19.0] — 2026-08-05

### Added

- **`superagentic finish --then '{"audit": ["p1-c0"]}'`.** Staged work has been
  in the library since the beginning and in the skill's advice since it was
  written, and there was no flag for it, so no shell worker could ever do what
  the skill told it to do. Refused with the unit still yours if the JSON is
  malformed or names a kind nothing defined, because a worker handed a bare
  name with no instructions does the wrong work confidently.
- **`--max-attempts` on `claim` and `fail`**, which the library has always
  taken and the CLI hardcoded to 3.

### Notes

- **A test now fails if any library keyword has no way to be set from a
  command.** Three separate releases shipped a feature that existed in the
  library and not on the CLI: `add --run` parsed and never read, `spawned_by`
  stored and drawn with no flag to set it, and `--then` documented in the skill
  and absent from the parser. All three passed every test, because the tests
  call the library and the workers use the shell. Exceptions are listed by name
  with the reason each is fine.

## [0.18.1] — 2026-08-05

Both of these were found by installing 0.18.0 from PyPI and using it, not by
running the tests, which passed on all six platforms.

### Fixed

- **`superagentic add extract --db work.db p1 p2` reported `unrecognized
  arguments: p1 p2`.** argparse fills a trailing `nargs="*"` from the FIRST run
  of positionals, which is empty when the names come after the flags, so only
  `add extract p1 p2 --db work.db` worked. Both are things people write and the
  error named the units rather than the ordering, so it read as "those units
  are bad". Affected `add`, `retry` and `cancel`. Which flags take a value is
  now read off the parser itself, so it cannot drift from the flags it has to
  know about, and a test fails if a fourth variadic subcommand is added without
  the fix.
- **`spawned_by` was recorded by the library and drawn by the dashboard, but no
  CLI could set it.** 0.18.0 added the column and the panel and left the flag
  out, so the edge it exists to record was always null for anyone using the
  shell. `superagentic claim --spawned-by WHO`, and `SUPERAGENTIC_SPAWNED_BY`
  in the environment, which is the useful one: a subagent inherits its parent's
  environment, so an orchestrator exports it once and every worker it spawns is
  labelled without editing a single worker prompt.

## [0.18.0] — 2026-08-05

### Fixed

- **`then=` dropped the run and recorded no parent.** A second stage fell out
  of its run entirely, so `wait --run` returned as soon as stage one finished
  and `runs` under-reported the work. Follow-on units now inherit the parent's
  run and record `parent_unit_id`.

### Added

- **`superagentic lineage <unit_id>`**, walking the chain in both directions.
  `then=` is the only place a unit causes another unit to exist, and it is the
  only relationship in this schema that is not a hierarchy: run, kind and
  worker all fan out from one parent, but lineage is a chain across stages and
  it branches.
- **`spawned_by` on a claim**, so the orchestrator-to-worker edge exists at
  all. Nothing here can see that one Claude session spawned ten subagents;
  without being told, there is no such edge to draw. Declared, like model.
- **A "who held what, when" panel**: one lane per worker, one bar per unit,
  time across the page, with the percentage of wall-clock each lane was busy.
  This is the picture worth having. Two sessions of five workers each did
  roughly equal unit counts and ran at 69–92% and 24–41% busy respectively, and
  no table shows that.
- **A "what caused what" panel**, aggregated to kinds and hidden entirely when
  nothing chains. A forest of individual lineages is not a picture;
  `extract -> audit -> gloss` with counts on the edges is.

### Notes

- Everything else in this schema is a tree: `run -> unit`, `kind -> unit`,
  `worker -> unit`. Drawing that as a node-link graph would be a worse version
  of the tables that already exist, so it is deliberately not drawn.

## [0.17.0] — 2026-08-05

### Added

- **`superagentic state`, and `project_state` over MCP.** The first thing a
  session that has just arrived should run. It **finds the database even if you
  do not know its name** (recognised by its tables, so it cannot pick up
  someone else's SQLite file), and reports which runs exist, which are still
  going, how much is done, and what needs a human.

  Every line under NEEDS ATTENTION carries **what to do about it**. A summary
  that reports three failures without saying `superagentic retry` has moved the
  work of knowing the tool onto whoever is reading it, which for a fresh agent
  is the entire problem. It detects failures nobody retried, units held far
  longer than usual, skills whose source changed since registration, and skills
  a kind requires that nothing registers.

  It ends with the single next command rather than leaving that to be inferred.
  In an empty directory it says how to start instead.

- The skill now tells a new session to run it before deciding anything, and to
  **join a run that is still going rather than starting a second one over the
  same work**. The MCP tool description says CALL THIS FIRST.

### Fixed

- Three copies of the same "which commands does this document name" regex had
  drifted apart, and one of them read `superagentic 0.16.0` in a sample of
  output as a command called `0`. There is one helper now.

## [0.16.0] — 2026-08-05

Both of these come from asking what two Claude sessions sharing one database
can do to each other. The answer was: everything, silently.

### Added

- **Every unit pins the definition it was claimed under.** `superagentic brief
  <unit_id>` answers what that unit was actually told, however the kind has
  changed since. `spec()` reports what a kind says *now*, which is precisely
  the wrong answer when you are asking why one unit's output looks different.
  **Content-addressed, not copied.** Storing the rendered brief on each unit is
  O(units), and a 400,000 unit corpus would carry most of a gigabyte of
  near-identical text. One row per distinct definition, and the brief is
  re-derived from it plus that unit's own `meta`.
- **`superagentic kinds`** shows every definition a kind has had and how many
  units ran under each, with the current one marked.

### Fixed

- **Redefining a kind with units waiting or in flight is refused.** Two
  sessions sharing a database and both defining `extract` clobbered each other
  mid-run: nothing errored, and the remaining units quietly carried the other
  session's instructions. `--force` (or `force=True`, or `force` over MCP) is
  how you mean it, and the message says what will happen either way.
  Applying an **unchanged** definition is always allowed, so re-applying a
  config stays a no-op.

### Notes

- **One database is one trust boundary, and the only one.** There is no
  permission model: a worker with no `--run` claims from every run, either
  session can cancel or retry the other's work, and kinds and skills are global
  to the file. The only thing actually protected is that a worker cannot
  `finish` or `heartbeat` a unit it does not hold. `docs/concepts.md` now says
  so, with a table of which arrangement to pick.

## [0.15.0] — 2026-08-05

### Added

- **`results --jsonl` streams**, one object per line. A finished corpus can be
  hundreds of thousands of units, and building a list in memory to print it is
  the difference between a command that works and one that gets killed. It also
  lets a consumer start before the fleet has finished.
- **`results --flat`** lifts the result's own keys to the top level, so
  `jq .claims` works instead of `jq .result.claims`. On a collision the
  **envelope wins**, because a row whose `name` silently became something the
  worker returned is a row you cannot join on; the shadowed value is kept as
  `result_<key>` rather than dropped, and a non-object result stays under
  `result`.
- **Rows now carry what you cannot reconstruct later**: model, worker, cost,
  tokens, duration, attempts, run and status, not just the payload.
- **`results --status`**, repeatable, so failures and their notes can be read
  alongside the successes instead of through a separate command.
- `leases.iter_results()` as the streaming API; `results()` stays as the list.

### Fixed

- `results --json` no longer materialises the whole corpus to print it. It is
  assembled a row at a time, and there are tests for the empty case and for the
  document still parsing, because hand-assembled JSON is exactly the sort of
  thing that ships a trailing comma.

## [0.14.0] — 2026-08-05

### Added

- **Cost and tokens per unit.** `finish --cost 0.031 --tokens-in 3100
  --tokens-out 900`, and the same on `finish_job` over MCP. Rolled up per
  model, per kind and per run, with a Cost tile and a per-model table in the
  dashboard.
  **Declared, never measured**: nothing here can observe a model's usage, and
  treating these as evidence rather than testimony would be a lie. They earn
  their place because they are the only thing you cannot reconstruct
  afterwards, and because *"did the cheaper model do these worse"* is the
  question a fleet operator actually has. Same corpus, interleaved:

  ```
  claude-opus-5      127 done   $4.11   $0.0324/unit   524k tokens
  claude-sonnet-5    113 done   $0.73   $0.0065/unit   461k tokens
  ```

  Averages are over the units that **reported**, and the totals say how many
  did, so a figure computed from 3 of 400 units is not quoted as the run's
  cost.
- **`superagentic init` and `apply`, with a `superagentic.toml`.** Setting a
  fleet up was five commands in the right order, living in whoever's shell
  history ran them last. Now it is a file you can review, diff and commit.
  TOML rather than YAML for one boring reason and one good one: `tomllib` is in
  the standard library so the zero-dependency rule holds, and TOML has no
  significant whitespace, so a prompt pasted into it cannot change meaning
  because of an indent.
  **Applying twice is a no-op**, because a config you are afraid to re-apply is
  one people stop applying, and then it stops describing what is running.
  Kinds are durable and belong in the file; units are per run and stay on the
  command line, with `units_from` and `units_glob` as the one convenience.

## [0.13.0] — 2026-08-05

### Added

- **`superagentic install-skill`.** The whole on-ramp is now two commands:

  ```bash
  uv tool install superagentic
  superagentic install-skill
  ```

  Then you ask Claude in English, and it defines the work, enqueues the units,
  spawns the workers in one message, waits, and checks the database. Before
  this, using superagentic meant reading the docs and running five commands in
  the right order, which is a first-five-minutes problem and first five minutes
  decide adoption.

### Changed

- **The README leads with what actually happens to people**: you ask Claude to
  process 400 pages, it spawns eight subagents, and all eight start on page
  one. The old opening described the mechanism before the reader had a reason
  to care about it.
- **The skill is rewritten around spawning subagents.** It now says explicitly
  to spawn them in ONE message, since spawning in several makes the fleet a
  fleet of one, and that is the easiest mistake an orchestrator can make.
- **One copy of the skill, inside the package** at
  `superagentic/skill/SKILL.md`, shipped in the wheel so `install-skill` works
  for anyone who installed from PyPI. The duplicate under `skills/` is gone: a
  second copy is a copy that drifts from the CLI it documents, and this project
  has been bitten by duplication more than once. A test asserts there is
  exactly one.

## [0.12.0] — 2026-08-05

The three that turn a fleet from hand-driven into scriptable. All three were
obviously missing the moment superagentic was used on real work.

### Added

- **`wait`** blocks until nothing is open and nothing is in flight, and **the
  exit code is the interface**: `0` clean, `1` something failed, `2` timed out.
  Without it every script wraps a polling loop around `status` and parses text.
  Progress goes to stderr and only when it changes.
- **`retry`** puts failed units back with **attempts reset to zero**, not up by
  one. The unit failed under the old code; carrying its history forward would
  retire it again after a single try, which is exactly wrong when the thing
  that changed is the fix. The note is kept. Refuses to run without a scope,
  because bare `retry` would reopen every failed unit in the file and that
  cannot be undone.
- **`cancel`** stops work that has not started and leaves in-flight work alone,
  because half-finished work is still work. `--now` also takes it back, and
  those workers find out the way they find out about any lost lease.
  **`cancelled` is a status, not a deletion**: a queue that forgets what you
  cancelled cannot say why a run came up short three weeks later.
- `leases.STATUSES` and `leases.TERMINAL`, so nothing enumerates statuses by
  hand again.

### Fixed

- **`cancelled` shipped invisible in `status`.** The table had a hand-written
  list of columns, so three cancelled units vanished and the row stopped adding
  up. It is driven off `STATUSES` now, with a test asserting every status
  appears and the row totals.

## [0.11.0] — 2026-08-05

### Added

- **`returns` is now checked.** It was prose, so a worker could hand back a
  bare string against a declared object and be told nothing. The same text you
  already write is now the check:

  ```
  {"claims": <int>, "notes": "<string>", "tags?": ["<string>"]}
  ```

  Not JSON Schema, deliberately: the audience is an agent reading a brief, and
  the brief being the documentation *and* the contract is the point. Extra keys
  are allowed; missing keys and wrong types are refused, **the unit stays
  leased**, and the worker can fix the shape and finish again. `--no-check`
  overrides. A `returns` written as a sentence disables checking, because a
  sentence is a legitimate thing to write.

### Fixed

- **The Windows flake, diagnosed.** `test_a_crashed_workers_unit_comes_back`
  claimed with a **10 millisecond** lease and then asserted the unit was still
  held. On a shared runner, 10ms between two Python statements is ordinary, and
  when it elapsed the lease expired, `reclaim()` returned the unit, and the
  assertion failed. That is why it failed twice in twelve releases and passed
  on re-run with no change.
  Every timing test now writes `leased_until` into the past instead of sleeping
  toward it. No test races the wall clock, and the suite is 20% faster.

## [0.10.0] — 2026-08-05

Everything here came from using superagentic on real work for the first time:
three agents auditing unjudged claims from a 1652 folio. Six units, 56
verdicts. All three workers independently reported the same bug.

### Fixed

- **`add --run` was accepted and never read.** Every unit landed with
  `run_id = NULL`, so **runs never worked from the CLI at all**, across four
  releases. Every run test called `leases.add()` directly, so the library was
  covered and the surface people actually use was not. There are CLI-level
  tests now, and a sweep asserting every flag of every subcommand is read by
  its handler.
- **The brief told workers to call a command that does not exist.** It ends
  "Call finish", the library function is `finish()`, the MCP tool is
  `finish_job`, and the CLI verb was `done`. A shell worker following its own
  brief ran nothing. `finish` is now the command at every layer, with `done`
  kept as an alias because it is in shipped prompts. A test asserts every verb
  the brief names is a real command.
- **A malformed `--result` printed a Python traceback and left the unit
  leased**, so it was silently redone when the lease expired while the worker
  reported success. It is now a clean error, exit 2, and the unit stays yours.
- **A large result had no route on Linux.** Linux caps a single argument at
  128 KB (`MAX_ARG_STRLEN`) whatever `ARG_MAX` says, so a 455 KB result passes
  on macOS and fails with `E2BIG` on Linux, in the main data path, where CI
  cannot see it. `--result-file` fixes it and the generated prompt says when to
  use it.

### Added

- **`skill-check`** re-hashes each registered skill's source and compares it to
  what was recorded, exiting 1 if anything changed. The brief prints a digest
  and nothing could check it: a fingerprint you cannot verify at the moment it
  matters is decoration.
- **The brief shows `meta`.** It was substituted into instructions but never
  displayed, so a caller could not tell a worker how big its unit was. The
  first real fleet had units of 2 to 24 claims and nothing said so.
- A warning when a result is not the shape the kind declared in `returns`. Not
  validation, since `returns` is prose, but a bare string against `{...}` is
  worth a line.

### Changed

- The generated worker prompt no longer claims an empty queue "prints nothing".
  It prints nothing **on stdout** and a note on stderr. Two of three workers
  flagged the contradiction.

## [0.9.3] — 2026-08-04

### Fixed

- **The `python` badge said 3.11 while CI tests 3.11, 3.12 and 3.13.** The
  classifiers listed one version, the badge faithfully reported it, and anyone
  on 3.13 saw a package that looked like it would not run. Both later versions
  are now declared, with a test asserting the classifiers and the CI matrix
  always agree, in both directions: listing fewer understates support, listing
  more claims something untested.

## [0.9.2] — 2026-08-04

### Fixed

- **The Windows job failed on a line ending.** `.gitattributes` was empty, so
  Git's default on Windows rewrites LF to CRLF in the working tree, and a new
  README test asserted a string spanning two lines against the file's contents.
  It passed on Linux and macOS and could only ever fail on Windows.
  `* text=auto eol=lf` now pins the checkout everywhere, every doc read in the
  suite normalises newlines regardless, and a test reproduces the whole thing
  without needing Windows by handing those assertions a CRLF file.
- **The sdist job broke on the same shape of bug**, twice in one release: a
  test read a repo file the tarball does not ship. `ee/LICENSE` first, then
  `.gitattributes`. Both are now guarded by an existence check, because the
  suite runs inside the unpacked sdist in CI and their absence there is
  correct rather than a failure.
- `subprocess.Popen(..., text=True)` in the cross-process test now passes
  `encoding="utf-8"`. Without it the pipes decode with the locale codec, which
  is cp1252 on Windows.

### Changed

- **README rewritten around what the package is for**, leading with the two
  questions a freshly spawned agent cannot answer for itself: which unit is
  mine, and what am I supposed to do with it. Everything else follows from
  those. The SuperAgentic wordmark is now at the top, committed as an SVG that
  carries presentation attributes rather than a `<style>` block, since GitHub
  strips embedded stylesheets from SVG in a README.
- Five tests keep the README honest: the stated MCP tool count must equal the
  real one, every tool and command it names must exist, the zero-dependency
  claim is checked against `pyproject.toml`, the wordmark must be committed and
  free of a `<style>` block, and there are no em dashes.

## [0.9.1] — 2026-08-04

### Fixed

- **0.9.0 never published.** The change that keeps `ee/` out of the sdist broke
  the test that verifies `ee/` is kept out — it read `ee/LICENSE`, which is now
  correctly absent from the tarball, and the suite runs inside the unpacked
  sdist in CI. The test now treats that absence as the property under test
  rather than a failure, and still checks the file wherever it does exist.
- **0.6.0 never published either** — Windows `pytest` failed on that tag. The
  content is superseded by 0.7.0, which passed Windows, so PyPI simply skips
  0.6.0 rather than being re-cut for it.

Both were tagged and reported as shipped without the release run being
checked. The releases now on PyPI are 0.1.0–0.5.0, 0.7.0, 0.8.0 and 0.9.1.

## [0.9.0] — 2026-08-04

### Changed

- **Open core.** Everything in this repository is Apache-2.0 **except `ee/`**,
  which is currently empty. [LICENSING.md](LICENSING.md) draws the line and
  argues for it. This is the shape Langfuse, GitLab and Grafana use.
  Apache-2.0 explicitly keeps the right to run it, modify it, and **sell a
  service built on it** — that is not being taken away, and the versions
  already published could not be taken back in any case.
- **The boundary is a rule, not a mood.** `ee/` is for what an organisation
  needs and one person never misses — SSO, audit log, RBAC, retention,
  alerting. The core must be a complete tool on its own: a fleet coordinator
  that cannot coordinate a fleet without a licence is a demo with a paywall.
  **Nothing already released under Apache-2.0 will move into `ee/`.**
- **DCO, and deliberately no CLA.** `ee/` accepts no outside contributions,
  which removes the reason a CLA would exist. Nobody signs away rights so that
  one directory can be commercial.

### Fixed

- **`ee/LICENSE` was shipping inside the Apache-2.0 sdist.** Hatchling collects
  licence files from anywhere in the tree as metadata, so listing `ee` nowhere
  in `include` was not enough — an explicit `exclude` is what keeps it out.
  Found by inspecting the built tarball; the test that was supposed to prevent
  it had only read the config and passed. It now opens the artefacts, and CI
  greps them too.

## [0.8.0] — 2026-08-04

### Added

- **A skill registry.** `superagentic skill NAME --source FILE --version V`,
  `superagentic skills`, plus `register_skill` / `list_skills` over MCP.
  Previously a kind carried the bare string `xrad-extraction` and nothing
  anywhere knew what that was, where a worker got it, or which version.
- **Skills are pinned onto the unit at claim time**, with a content digest when
  the source is a readable file. This is the part that earns its keep: edit a
  skill halfway through a run and half the units were produced under one
  version and half under another — a record taken at claim time is the only
  thing that can tell them apart. Looking it up later reports whatever it is
  now, which is the wrong answer.
- **Skills used but never registered are surfaced**, in the brief, in
  `superagentic skills`, and in `define_kind`'s reply — a kind naming something
  nothing records where to get is worth saying out loud.
- A **Skills** panel in the dashboard.

### Notes

- **Nothing is fetched or installed.** The moment this downloads a skill it has
  to know about Claude Code's `.claude/skills`, Cursor's rules, and every
  runtime after them — and it stops working for the shell fleet that has none
  of those. A test asserts the module never touches the network.
- Usage counts come from what units pinned, not from which kinds mention a
  skill. A skill named by a kind nobody ran has been used zero times.

### Fixed

- `superagentic skill` and `skills` flush stdout before writing to stderr. A
  warning about one invocation was appearing above another's success line,
  because stderr is unbuffered and stdout is not when piped.

## [0.7.0] — 2026-08-04

### Added

- **The SuperAgentic wordmark**, set as type rather than embedded as an image.
  A raster logo would weigh on every page and on every static snapshot, and the
  snapshot is the artefact people actually mail to each other. Two-tone italic
  with the cream keyline from the artwork, done with `paint-order: stroke fill`
  so the outline sits behind the letterforms instead of eating into them.
  Brand lives in its own three tokens — `--wm-ink`, `--wm-red`, `--wm-cream` —
  so nothing can render "critical" in the logo red by accident. Collapsed, the
  rail shows `SA` in the same two colours. Favicon matches.

### Fixed

- **The sidebar clock was unlabelled and read as a timer.** It rendered a bare
  `17:35:02` beside a coloured dot; it was the time of the last poll, and
  nothing on screen said so. It now reads `updated just now` / `updated 12s
  ago`, ticks locally once a second between polls, and turns amber past ten
  seconds — the poll is every two, so anything older means the server has
  stopped answering. Frozen at a plausible clock time, a dead server looked
  alive.

## [0.6.0] — 2026-08-04

### Added

- **`model` on a claim.** `--model`, `SUPERAGENTIC_MODEL`, or `model` on
  `claim_job`. Stored per unit, shown in Jobs, searchable, and rolled up in
  `stats()["per_model"]` with counts and mean duration.
  **Declared, never detected** — nothing here can verify it, and pretending
  otherwise would make it evidence when it is only a label. It earns a column
  because it is the one thing you cannot reconstruct afterwards: which model
  did these forty units, and were they faster or worse.
- **Pagination in Jobs**, 100 a page, page number in the URL. Changing a filter
  returns to page 1, and a page past the end lands on the last real one rather
  than an empty table.
- **The rail collapses**, automatically below 1180px and manually with a
  toggle. An explicit choice is remembered and wins at every width — auto
  behaviour that overrides what someone just clicked is worse than none.
  Collapsed it keeps the toggle and the project buttons, so there is always a
  way back.
- **The version in the sidebar**, from `__version__` via the payload.

## [0.5.0] — 2026-08-04

### Added

- **A Jobs view, and `units()` behind it.** Everything until now aggregated —
  totals, percentiles, per-worker rollups — and none of it answered "what
  happened to page 189". Jobs lists individual units with status, worker,
  attempts, elapsed, lease remaining and the note or result, filters by status,
  and searches name, worker **and note**, so a failure is findable by what it
  said. The list is bounded and reports when it truncated: a view that silently
  shows the first 300 of 40,000 is a view that lies.
- **Two sidebars.** A narrow rail for projects and the session, a second for
  Views and Runs. Runs moved out of the main pane, where the table was pushing
  everything else below the fold.
- **The session row is always present.** `Sign out` is shown whether or not a
  token is configured — disabled when there is nothing to sign out of, with a
  tooltip saying so. Hiding it made it look like a missing feature; showing a
  live button that ends nothing would be worse.
- `GET /api/units`, authenticated like everything else.

## [0.4.2] — 2026-08-04

### Fixed

- **The login overlay covered every dashboard, including ones with no token.**
  `hidden` works only through the UA stylesheet's `[hidden] { display: none }`,
  so `#gate { display:grid }` — an author rule on the same element — silently
  beat it. The gate is `position:fixed; inset:0; z-index:20`, so it rendered on
  top of everything and the page appeared to demand a token that was never
  configured. `.shell` had the same rule and the same problem.
  Fixed with a global `[hidden] { display: none !important; }` rather than by
  remembering the rule at each call site.
  Nothing that talks to the server could catch this: `/api` returned 200 and
  `auth: false` throughout. It is purely a CSS cascade bug, and it needed a
  browser to see.

## [0.4.1] — 2026-08-04

### Fixed

- **The login gate polled forever.** With the gate up the page kept calling
  `/api` every two seconds, so the browser console filled with 401s and the
  server got a request it could only ever refuse. The interval now stops when
  the gate appears and starts again after signing in — and it is no longer
  started unconditionally at load, only once the first poll has succeeded.
- **No favicon, so every browser logged a 404** for `/favicon.ico` that looked
  like a fault in the tool. The page now carries an inline SVG icon — a
  draining queue, three bars each shorter than the last — and the server
  answers `/favicon.ico` with 204 for anything that asks regardless.

## [0.4.0] — 2026-08-04

### Added

- **A sidebar.** Projects at the top, runs below, session at the bottom;
  detail on the right. Selecting a run scopes every panel and the selection is
  in the URL, so a scoped view can be sent to someone.
- **Projects.** A project *is* a database — `--project PATH` (repeatable, and a
  directory expands to the `*.db` in it). No project table: putting one inside
  a database would make that file the registry for the others, so moving it
  would break the rest.
- **An access token, with sign in and sign out.** `--token`, or better
  `SUPERAGENTIC_TOKEN`, since a flag lands in shell history and in `ps`.
  Compared with `hmac.compare_digest`; session cookie is `HttpOnly` and
  `SameSite=Strict`; nothing is stored on disk.
- **The server refuses to bind off-loopback without a token**, and warns every
  time it binds off-loopback anyway. That refusal is the security design: there
  is no TLS here, and a login form whose real effect is to make an unencrypted
  service feel safe is worse than none. It is a shared token, not user
  accounts — there is no user model in this library and inventing one for a
  dashboard would be pretending to an identity system that does not exist.

### Fixed

- **A static snapshot rendered an empty sidebar.** `projects`, `project` and
  `auth` were added by the request handler and so were missing from the file
  `--out` writes. Moved into the shared payload, with a test.

## [0.3.0] — 2026-08-04

### Added

- **Runs.** `start_run()` / `runs()` and `superagentic start` / `runs`, plus
  `start_run` and `list_runs` over MCP. A run is one execution of a fleet:
  without it a database is one flat pool and there is no way to ask what last
  night's fleet did, only what the queue holds right now.
- **A run scopes unit ids**, and that is the part that matters. Enqueueing is
  idempotent on `kind:name`, so without a scope a second run over the same
  corpus would find everything already done and do nothing — while re-running
  an enumeration *inside* a run must still add nothing new. Both are wanted and
  they are only compatible if the run is part of the key.
- **`--run` filters** `claim`, `status`, `results`, `dashboard`, and
  `stats()` / `progress()` / `results()` in the library.
- **A runs panel in the dashboard**, newest first, with a **parallel** column —
  worker-seconds over wall-clock, i.e. how much concurrency you actually got. A
  four-worker run showing `0.8x` had three workers idle; more would not have
  helped. Click a run and every panel scopes to it; the selection is in the URL,
  so a scoped view can be sent to someone.
- **No `end_run`.** A run is over when its units are, which has to be derivable
  because the orchestrator is the process most likely to have died.

### Fixed

- **`CREATE INDEX` ran before the migration that adds the column it names**, so
  opening a database written by an older version raised
  `no such column: run_id`. Indexes are now a separate script applied after the
  ALTERs. Found by a test that builds an old schema by hand — this class of bug
  is invisible to every test that starts from a fresh file.

## [0.2.0] — 2026-08-04

### Added

- **A kind can declare what a worker must HAVE**, not only what it must do:
  `skills=[...]`, `mcp={name: command}`, `context=...` — and `--skill`,
  `--mcp name=cmd`, `--context FILE` on the CLI. They arrive as structured
  fields rather than the free-text `tools` hint, so a spawner can act on them
  and the brief tells the worker to **fail a unit it is not equipped for
  instead of improvising**. A unit done without its tools looks finished, which
  is worse than one left undone.
  The strings stay opaque — a skill name means nothing here, which is what
  keeps this agnostic about which agent runtime you use.
- **`superagentic prompt <kind>`** and the `worker_prompt` MCP tool: the spawn
  prompt, generated from the kind. Generic about the task, because that comes
  from the queue at claim time; specific about the capabilities, because a
  skill a worker never loaded is not something it can discover halfway through.
  A test parses every command the prompt prints against the real CLI — a prompt
  naming a flag that does not exist is worse than no prompt.
- **`context`**: read-only material every worker of a kind receives.

### Notes

- **Worker-to-worker mutable state is refused, and it is a correctness
  argument.** Units must be independent; leases are at-least-once, so any unit
  may run twice. If A writes context B reads, re-running A silently changes B's
  input and nothing in the results would show it. Stages hand things forward
  explicitly with `result=` and `then=`. Written up in docs/concepts.md.
- **No supervisor agent, deliberately.** Spawning would mean this package
  needed an agent runtime, credentials, and an opinion about which one — and it
  would stop working for the shell fleet that needs none of those. It generates
  the prompt; running it is your runtime's job.
- `connect()` migrates the three new `kind` columns, so a 0.1.0 database keeps
  working. Tested against a hand-built old schema.

## [0.1.0] — 2026-08-04

First release.

### Added

- **Kinds: what the work IS, not just which units are outstanding.**
  `define(kind, instructions, done_when=, returns=, tools=)`, read at claim
  time and handed over with every unit. A freshly spawned agent has no context;
  putting the task in the prompt that spawned the fleet leaves it invisible to
  the worker started an hour later and to the one that inherits a crashed
  worker's unit. `Unit.brief()` is the whole assignment as one block of text,
  because an agent given four fields will read one of them.
- **Results.** `finish(..., result=...)` and `results(conn, kind)`, so the
  orchestrator that spawned the fleet can collect what it produced instead of
  inventing a side channel.
- **`then=` on finish**, enqueueing the next stage from the worker that just
  finished — a pipeline without this becoming a scheduler. Nothing is enqueued
  if the close failed, so a worker that lost its lease cannot inject work off
  the back of a unit it no longer owns.
- **Nine MCP tools, split by role.** `define_kind` / `add_jobs` /
  `job_results` for the orchestrator; `claim_job` / `finish_job` /
  `release_job` / `fail_job` / `heartbeat_job` / `job_status` for each worker.
  An agent can set up an entire fleet without touching a shell, and `add_jobs`
  refuses a kind nobody defined rather than letting a worker discover it.
- **`superagentic dashboard` — a live view of the fleet**, served from
  `http.server` with its CSS and JS inline and its SVG drawn by hand. No
  framework, no build step, nothing fetched from a CDN. `--out` writes a
  self-contained snapshot instead. Read-only and bound to loopback.
  `14 left` is the same number whether the fleet is healthy or three workers
  have died, so the panels are the ones that distinguish those: throughput over
  time, what each worker holds and for how long, p50 against p95, and a
  severity stripe on any unit held past 3x the p95.
- **`claimed_at`, and `worker` kept on a terminal status.** Without the first
  there is no duration, so no percentiles and no ETA; without the second there
  is no per-worker anything, because closing a unit used to erase who did it.
  `connect()` migrates an older file rather than failing deep inside a query.
- **`stats()`** computes all of it in one pass. A dashboard polls, and twelve
  round trips against a file another process is writing to eventually reads a
  torn picture where the totals do not add up.
- **Work leases in one SQLite file.** `add` / `claim` / `finish` / `fail` /
  `release` / `heartbeat`, plus `progress`, `leased` and `failures` for looking
  at a running fleet. `kind` and `name` are opaque strings.
- **A CLI** where the exit codes are the interface: `claim` exits 1 with no
  output on an empty queue, so a shell loop ends by itself.
- **An MCP server**, so an agent claims work rather than being told what to work
  on. The tool descriptions carry the protocol — claim before starting, stop
  when the queue is empty rather than inventing work.
- **No runtime dependencies.** argparse for the CLI, json over stdio for MCP.
  This gets installed next to whatever the workers run and should have no
  opinion about their dependency versions.

### Notes

- **At-least-once, and it says so.** A slow worker and a dead worker are
  indistinguishable; no timeout separates them. Heartbeat while you work and
  make the final write idempotent.
- Extracted from a claim-store project where a fleet of extraction agents kept
  starting on the same page. It imported nothing from that project, which is
  why it is here instead.
