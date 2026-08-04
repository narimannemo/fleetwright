# Contributing

## What will be accepted

- **Bugs in the lease logic.** Especially anything where a unit is handed to
  two workers, or is lost forever. A failing test is the whole bug report.
- **Portability fixes.** Older SQLite, Windows path and encoding behaviour, a
  filesystem where the locking assumptions break.
- **Documentation that removes a surprise.** If something took you an hour to
  work out, that hour is the contribution.

## What will not

- **A second backend.** Postgres, Redis, S3. The entire proposition is one file
  and no broker; a backend abstraction costs that and buys a use case already
  served by Celery, RQ, or a real queue.
- **A scheduler.** Dependencies between units, retry backoff, cron, priority
  classes. Every one of these is reasonable and none of them belong here.
- **Async.** The operations are single SQLite statements measured in
  microseconds. An async surface would be a second API for no gain.
- **Anything claiming exactly-once.** It is not achievable over an unreliable
  worker and a promise of it in the docs is worse than the current honesty.
- **Runtime dependencies.** There are none. That is a feature and it is not
  negotiable for a small convenience.
- **Anything into `ee/`.** It takes no outside contributions, so that the rest
  of the project needs no CLA. If you want to build one of the things listed in
  [ee/README.md](ee/README.md), say so first — the right answer is often that
  it belongs in the Apache core instead.

## Licensing and the DCO

Contributions go to the **Apache-2.0 core** — everything outside `ee/`. See
[LICENSING.md](LICENSING.md) for why that directory exists and where the line
is.

Sign off your commits:

```bash
git commit -s -m "..."
```

That adds a `Signed-off-by:` line, which is the
[Developer Certificate of Origin](https://developercertificate.org/): you are
saying you wrote it, or have the right to submit it. **It is not a copyright
assignment.** You keep your copyright and it stays Apache-2.0.

There is deliberately **no contributor licence agreement**, and there will not
be one. A CLA exists so a maintainer can relicense your work later; `ee/`
accepts no outside contributions, which removes the need entirely. Nobody signs
away rights here so that one directory can be commercial.

### Developer Certificate of Origin

By signing off you certify the [DCO 1.1](https://developercertificate.org/) —
in short, that the contribution is yours to give.

## Working on it

```bash
uv venv && uv pip install -e ".[dev]"
pytest -q
ruff check .
```

The tests that matter are in `TestFailure` and `TestConcurrency` — a crashed
worker's unit coming back, a lost lease that cannot be closed, and three real
OS processes racing one file. Any change to `claim` or `reclaim` must keep the
cross-process test green; in-process tests share a connection and will pass
while the real case double-issues.

Every test is named after the behaviour it protects, not the function it calls.
Please keep that up — `test_a_crashed_workers_unit_comes_back` says what breaks
if it fails, and `test_reclaim_2` does not.
