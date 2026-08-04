# Changelog

Kept by hand, in [Keep a Changelog](https://keepachangelog.com) shape. The
release workflow reads the section matching the tag and fails if there isn't
one — release notes generated from commit subjects tell a reader what changed
and never why.

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
