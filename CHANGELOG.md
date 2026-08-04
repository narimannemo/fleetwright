# Changelog

Kept by hand, in [Keep a Changelog](https://keepachangelog.com) shape. The
release workflow reads the section matching the tag and fails if there isn't
one — release notes generated from commit subjects tell a reader what changed
and never why.

## [0.1.0] — 2026-08-04

First release.

### Added

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
