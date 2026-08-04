# Changelog

Kept by hand, in [Keep a Changelog](https://keepachangelog.com) shape. The
release workflow reads the section matching the tag and fails if there isn't
one — release notes generated from commit subjects tell a reader what changed
and never why.

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
