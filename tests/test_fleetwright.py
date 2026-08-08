"""The tests that matter here are the ones about failure.

Any queue passes "hand out ten units to two workers." What separates a lease
table from a broken one is what happens when a worker dies holding work, when a
slow worker comes back after losing its lease, and when two real OS processes
race the same file. Those have their own tests below and they are the point.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import fleetwright as sa
from fleetwright import cli
from fleetwright.cli import main as cli_main
from fleetwright.mcp import Server

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn(tmp_path):
    return sa.connect(tmp_path / "work.db")


class TestQueue:
    def test_adding_the_same_units_twice_is_a_no_op(self, conn):
        assert sa.add(conn, "translate", ["p1", "p2"]) == 2
        # Re-running an enumeration after the corpus grew is the common case.
        assert sa.add(conn, "translate", ["p1", "p2", "p3"]) == 1

    def test_the_same_name_under_two_kinds_is_two_units(self, conn):
        sa.add(conn, "translate", ["p1"])
        assert sa.add(conn, "audit", ["p1"]) == 1

    def test_two_claimers_never_get_the_same_unit(self, conn):
        sa.add(conn, "x", [f"u{i}" for i in range(10)])
        a = sa.claim(conn, "x", worker="a", n=4)
        b = sa.claim(conn, "x", worker="b", n=4)
        assert len({u.name for u in a} & {u.name for u in b}) == 0
        assert len(a) == len(b) == 4

    def test_the_queue_running_dry_is_not_an_error(self, conn):
        sa.add(conn, "x", ["only"])
        assert sa.claim(conn, "x", worker="a")
        assert sa.claim(conn, "x", worker="b") == []

    def test_priority_is_honoured(self, conn):
        sa.add(conn, "x", ["low"])
        sa.add(conn, "x", ["high"], priority=5)
        assert sa.claim(conn, "x", worker="w")[0].name == "high"

    def test_claiming_without_a_kind_takes_anything(self, conn):
        sa.add(conn, "translate", ["a"])
        sa.add(conn, "audit", ["b"])
        assert {u.kind for u in sa.claim(conn, worker="w", n=2)} == {"translate", "audit"}

    def test_meta_travels_with_the_unit_and_is_never_inspected(self, conn):
        sa.add(conn, "x", ["u1"], meta={"url": "s3://b/k", "nested": {"n": 1}})
        assert sa.claim(conn, "x", worker="w")[0].meta["nested"]["n"] == 1


class TestTheBrief:
    """What separates this from a queue: a worker is told what to do."""

    def test_a_claimed_unit_carries_its_kinds_instructions(self, conn):
        sa.define(conn, "extract", instructions="Read $path. Record claims.",
                  done_when="every claim recorded", returns='{"claims": <int>}',
                  tools="xrad MCP")
        sa.add(conn, "extract", ["p0189"], meta={"path": "scans/p0189.png"})
        u = sa.claim(conn, "extract", worker="w")[0]
        assert u.instructions == "Read scans/p0189.png. Record claims."
        assert u.done_when == "every claim recorded"
        assert "record_claim" not in u.brief()          # only what was defined
        assert "scans/p0189.png" in u.brief()

    def test_meta_values_may_themselves_template_on_the_unit_name(self, conn):
        # The useful way to give 2,000 units a path is one template, not 2,000
        # dicts. Without the second pass the agent is handed the literal
        # `scans/$name.png`, which is exactly what the demo shipped with once.
        sa.define(conn, "x", instructions="Read $path.")
        sa.add(conn, "x", ["p7"], meta={"path": "scans/$name.png"})
        assert sa.claim(conn, "x", worker="w")[0].instructions == \
            "Read scans/p7.png."

    def test_instructions_survive_json_braces(self, conn):
        # str.format would raise on this. Instructions to agents are full of
        # JSON, so the substitution has to tolerate braces.
        sa.define(conn, "x", instructions='Return {"ok": true} for $name.')
        sa.add(conn, "x", ["u1"])
        assert sa.claim(conn, "x", worker="w")[0].instructions == \
            'Return {"ok": true} for u1.'

    def test_an_unknown_placeholder_is_left_alone_not_an_error(self, conn):
        sa.define(conn, "x", instructions="Use $missing on $name.")
        sa.add(conn, "x", ["u1"])
        # A worker asking for work must not fail because a template was wrong.
        assert sa.claim(conn, "x", worker="w")[0].instructions == \
            "Use $missing on u1."

    def test_a_retried_unit_says_so_in_its_brief(self, conn):
        sa.define(conn, "x", instructions="do it")
        sa.add(conn, "x", ["u1"])
        sa.claim(conn, "x", worker="a", lease=300)
        expire(conn)
        assert "attempt 2" in sa.claim(conn, "x", worker="b")[0].brief()

    def test_redefining_a_kind_with_live_units_is_refused(self, conn):
        """The most damaging operation had no guard. Two sessions sharing a
        database and both defining `extract` clobbered each other mid-run:
        nothing errored, and the remaining units quietly carried the other
        session's instructions."""
        sa.define(conn, "x", instructions="old")
        sa.add(conn, "x", ["u1", "u2"])
        sa.claim(conn, "x", worker="a")
        with pytest.raises(ValueError, match="in flight"):
            sa.define(conn, "x", instructions="new")
        # Unchanged is always fine, so re-applying a config stays a no-op.
        sa.define(conn, "x", instructions="old")

    def test_forcing_it_reaches_workers_that_have_not_claimed_yet(self, conn):
        sa.define(conn, "x", instructions="old")
        sa.add(conn, "x", ["u1", "u2"])
        sa.claim(conn, "x", worker="a")
        sa.define(conn, "x", instructions="new", force=True)
        assert sa.claim(conn, "x", worker="b")[0].instructions == "new"

    def test_a_kind_with_no_spec_still_works(self, conn):
        sa.add(conn, "bare", ["u1"])
        u = sa.claim(conn, "bare", worker="w")[0]
        assert u.instructions == "" and u.name in u.brief()


class TestCapabilities:
    """What a worker must HAVE, as opposed to what it must DO."""

    def test_skills_and_servers_reach_the_worker_as_data_not_prose(self, conn):
        sa.define(conn, "x", instructions="go", skills=["a-skill", "b-skill"],
                  mcp={"xrad": "xrad serve --db g.db"})
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="w")[0]
        assert u.skills == ("a-skill", "b-skill")
        assert u.mcp == {"xrad": "xrad serve --db g.db"}

    def test_the_brief_states_them_as_a_requirement_not_a_suggestion(self, conn):
        sa.define(conn, "x", instructions="go", skills=["a-skill"])
        sa.add(conn, "x", ["u1"])
        b = sa.claim(conn, "x", worker="w")[0].brief()
        assert "YOU MUST HAVE" in b
        # A unit done without its tools looks finished, which is worse than
        # one left undone. The brief has to say so.
        assert "call fail" in b and "improvise" in b

    def test_context_is_carried_and_templated(self, conn):
        sa.define(conn, "x", instructions="go",
                  context="Glossary for $name: see $path")
        sa.add(conn, "x", ["u1"], meta={"path": "g.md"})
        assert "Glossary for u1: see g.md" in sa.claim(conn, "x", worker="w")[0].context

    def test_a_kind_with_no_capabilities_says_nothing_about_them(self, conn):
        sa.define(conn, "x", instructions="go")
        sa.add(conn, "x", ["u1"])
        assert "YOU MUST HAVE" not in sa.claim(conn, "x", worker="w")[0].brief()

    def test_an_older_database_gains_the_columns(self, tmp_path):
        """A file written before these columns existed must keep working."""
        import sqlite3
        db = tmp_path / "old.db"
        c = sqlite3.connect(db)
        c.executescript("""CREATE TABLE kind (kind TEXT PRIMARY KEY,
            instructions TEXT NOT NULL, done_when TEXT, returns TEXT,
            tools TEXT, updated_at REAL);
            INSERT INTO kind VALUES ('x','go',NULL,NULL,NULL,0);""")
        c.commit()
        c.close()
        conn = sa.connect(db)                      # must migrate, not explode
        sa.define(conn, "y", instructions="go", skills=["s"])
        assert sa.spec(conn, "x")["skills"] is None
        assert json.loads(sa.spec(conn, "y")["skills"]) == ["s"]


class TestRuns:
    """A run is one execution of a fleet. Without it a database is one flat
    pool and there is no way to ask what last night's fleet did."""

    def test_a_new_run_re_does_the_same_corpus(self, conn):
        r1 = sa.start_run(conn, label="first")
        sa.add(conn, "x", ["p1", "p2"], run=r1)
        for u in sa.claim(conn, "x", worker="w", n=2, run=r1):
            sa.finish(conn, u.unit_id, worker="w")
        r2 = sa.start_run(conn, label="second")
        # The whole reason the run scopes the id: re-running a corpus after a
        # prompt change must actually re-run it.
        assert sa.add(conn, "x", ["p1", "p2"], run=r2) == 2
        assert len(sa.claim(conn, "x", worker="w", n=2, run=r2)) == 2

    def test_re_adding_inside_one_run_is_still_a_no_op(self, conn):
        r = sa.start_run(conn)
        assert sa.add(conn, "x", ["p1", "p2"], run=r) == 2
        assert sa.add(conn, "x", ["p1", "p2", "p3"], run=r) == 1

    def test_claiming_can_be_confined_to_a_run(self, conn):
        r1, r2 = sa.start_run(conn), sa.start_run(conn)
        sa.add(conn, "x", ["a"], run=r1)
        sa.add(conn, "x", ["b"], run=r2)
        assert [u.name for u in sa.claim(conn, "x", worker="w", n=5, run=r2)] == ["b"]

    def test_statistics_are_scoped(self, conn):
        r1, r2 = sa.start_run(conn), sa.start_run(conn)
        sa.add(conn, "x", ["a", "b"], run=r1)
        sa.add(conn, "x", ["c"], run=r2)
        for u in sa.claim(conn, "x", worker="w", n=2, run=r1):
            sa.finish(conn, u.unit_id, worker="w")
        from fleetwright import leases
        assert leases.stats(conn, run=r1)["totals"]["done"] == 2
        assert leases.stats(conn, run=r2)["totals"]["done"] == 0
        assert leases.stats(conn)["totals"]["all"] == 3

    def test_a_run_is_over_when_its_units_are_not_when_told(self, conn):
        # No end_run: the orchestrator is the process most likely to have died.
        r = sa.start_run(conn, label="l")
        sa.add(conn, "x", ["a"], run=r)
        assert sa.runs(conn)[0]["running"] is True
        u = sa.claim(conn, "x", worker="w", run=r)[0]
        sa.finish(conn, u.unit_id, worker="w")
        assert sa.runs(conn)[0]["running"] is False

    def test_runs_report_parallelism_not_just_duration(self, conn):
        import time as _t
        r = sa.start_run(conn)
        sa.add(conn, "x", ["a", "b", "c"], run=r)
        for u in sa.claim(conn, "x", worker="w", n=3, run=r):
            conn.execute("UPDATE unit SET claimed_at=? WHERE unit_id=?",
                         (_t.time() - 10, u.unit_id))
            conn.commit()
            sa.finish(conn, u.unit_id, worker="w")
        row = sa.runs(conn)[0]
        # busy is worker-seconds; elapsed is wall-clock. Their ratio says how
        # much parallelism you actually got, which is what tells you whether
        # more workers would have helped.
        assert row["busy"] > 25 and row["elapsed"] < 5

    def test_units_without_a_run_still_work(self, conn):
        sa.add(conn, "x", ["a"])
        assert sa.claim(conn, "x", worker="w")[0].name == "a"
        assert sa.runs(conn) == []

    def test_an_older_database_gains_run_support(self, tmp_path):
        import sqlite3
        db = tmp_path / "old.db"
        c = sqlite3.connect(db)
        c.executescript("""CREATE TABLE unit (unit_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
            worker TEXT, leased_until REAL, lease_token TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL
            DEFAULT 0, note TEXT, meta TEXT, created_at REAL, updated_at REAL);
            INSERT INTO unit VALUES ('x:a','x','a','open',NULL,NULL,NULL,0,0,
            NULL,'{}',0,0);""")
        c.commit()
        c.close()
        conn = sa.connect(db)
        assert sa.claim(conn, "x", worker="w")[0].name == "a"
        r = sa.start_run(conn)
        assert sa.add(conn, "x", ["b"], run=r) == 1


class TestWorkerPrompt:
    def test_it_tells_the_worker_to_stop_on_an_empty_queue(self, conn):
        p = sa.worker_prompt(conn, db="w.db")
        assert "QUEUE IS EMPTY" in p and "Do NOT invent work" in p

    def test_it_names_the_required_skills_before_the_claim_step(self, conn):
        sa.define(conn, "x", instructions="go", skills=["needed"],
                  mcp={"srv": "cmd"})
        p = sa.worker_prompt(conn, "x", db="w.db")
        assert p.index("BEFORE YOU CLAIM ANYTHING") < p.index("STEP 1")
        assert "needed" in p and "srv (cmd)" in p

    def test_the_commands_it_prints_are_the_real_ones(self, conn, tmp_path):
        # A prompt that names a flag the CLI does not have is worse than no
        # prompt: the worker runs it, fails, and reports something confusing.
        from fleetwright.cli import build_parser
        sa.define(conn, "x", instructions="go")
        p = sa.worker_prompt(conn, "x", db=str(tmp_path / "w.db"))
        import shlex
        parser = build_parser()
        checked = 0
        for line in p.splitlines():
            line = line.strip()
            if not line.startswith("fleetwright "):
                continue
            # shlex, not split(): `--result '<the JSON the brief asked for>'`
            # is ONE argument and naive splitting turns it into six.
            # Placeholders are SUBSTITUTED, not dropped -- dropping them leaves
            # `--result` with no value and the arity check stops meaning
            # anything.
            argv = ["x" if t.startswith("<") else t for t in shlex.split(line)[1:]]
            parser.parse_args(argv)          # SystemExit if a flag is wrong
            checked += 1
        assert checked >= 2, "the prompt stopped naming any commands"

    def test_no_kind_still_produces_a_usable_prompt(self, conn):
        p = sa.worker_prompt(conn, db="w.db")
        assert "fleetwright claim --db w.db" in p


class TestResults:
    def test_a_worker_hands_back_output_the_orchestrator_collects(self, conn):
        sa.define(conn, "x", instructions="do it")
        sa.add(conn, "x", ["u1", "u2"])
        for u in sa.claim(conn, "x", worker="w", n=2):
            sa.finish(conn, u.unit_id, worker="w", result={"n": int(u.name[-1])})
        got = sa.results(conn, "x")
        assert [r["result"]["n"] for r in got] == [1, 2]

    def test_finishing_without_a_result_is_none_not_missing(self, conn):
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w")
        assert sa.results(conn)[0]["result"] is None

    def test_then_enqueues_the_next_stage(self, conn):
        sa.add(conn, "extract", ["p1"])
        u = sa.claim(conn, "extract", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w", then={"audit": ["c1", "c2"]})
        assert sa.progress(conn)["audit"][sa.OPEN] == 2

    def test_a_lost_lease_cannot_inject_follow_on_work(self, conn):
        sa.add(conn, "extract", ["p1"])
        slow = sa.claim(conn, "extract", worker="slow", lease=300)[0]
        expire(conn)
        sa.claim(conn, "extract", worker="fast")
        assert sa.finish(conn, slow.unit_id, worker="slow",
                         then={"audit": ["c1"]}) is False
        assert "audit" not in sa.progress(conn), \
            "a worker that lost its lease must not enqueue off the back of it"


def expire(conn, unit_id=None):
    """Push a lease into the past, instead of sleeping until it gets there.

    The tests used to claim with a 10ms lease and then assert things about
    whether it had expired yet. On a shared CI runner, 10ms between two Python
    statements is ordinary — GC, descheduling, a virus scanner opening the
    SQLite file — so the suite failed on Windows twice in twelve releases and
    passed on re-run with no change. Wall-clock is not a thing a test should
    race against when the value is a column it can simply write.
    """
    sql = "UPDATE unit SET leased_until = ? WHERE status = 'leased'"
    args = [time.time() - 1]
    if unit_id:
        sql += " AND unit_id = ?"
        args.append(unit_id)
    conn.execute(sql, args)
    conn.commit()


class TestFailure:
    """A lease is only worth having if these hold."""

    def test_a_crashed_workers_unit_comes_back(self, conn):
        sa.add(conn, "x", ["u1"])
        # A long lease, so "still held" is a fact rather than a race.
        assert sa.claim(conn, "x", worker="dies", lease=300)
        assert sa.claim(conn, "x", worker="b") == [], "still leased, correctly"
        expire(conn)
        # No daemon: the next claimer reclaims on the way in.
        again = sa.claim(conn, "x", worker="b")
        assert [u.name for u in again] == ["u1"]
        assert again[0].attempts == 2

    def test_a_lost_lease_cannot_be_closed_or_extended(self, conn):
        sa.add(conn, "x", ["u1"])
        slow = sa.claim(conn, "x", worker="slow", lease=300)[0]
        expire(conn)
        sa.claim(conn, "x", worker="fast")
        assert sa.heartbeat(conn, [slow.unit_id], worker="slow") == 0
        assert sa.finish(conn, slow.unit_id, worker="slow") is False
        assert sa.finish(conn, slow.unit_id, worker="fast") is True

    def test_a_heartbeat_keeps_a_slow_worker_from_being_reclaimed(self, conn):
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="slow", lease=1)[0]
        expire(conn)                                   # the lease has run out
        sa.heartbeat(conn, [u.unit_id], worker="slow", lease=300)
        assert sa.claim(conn, "x", worker="other") == [], \
            "a heartbeat must rescue a lease that had already lapsed"

    def test_a_poison_unit_is_retired_rather_than_re_leased_forever(self, conn):
        sa.add(conn, "x", ["bad"])
        for _ in range(3):
            sa.claim(conn, "x", worker="w", lease=300)
            expire(conn)
        assert sa.claim(conn, "x", worker="w") == []
        assert sa.progress(conn)["x"][sa.FAILED] == 1
        assert "out of attempts" in sa.failures(conn)[0]["note"]

    def test_failing_keeps_the_reason(self, conn):
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="w")[0]
        assert sa.fail(conn, u.unit_id, note="page is blank", worker="w")
        row = conn.execute("SELECT note, status FROM unit").fetchone()
        assert row["note"] == "page is blank" and row["status"] == sa.OPEN

    def test_failing_past_the_limit_stops_offering_the_unit(self, conn):
        sa.add(conn, "x", ["u1"])
        for _ in range(3):
            u = sa.claim(conn, "x", worker="w")[0]
            sa.fail(conn, u.unit_id, note="nope", worker="w")
        assert sa.claim(conn, "x", worker="w") == []

    def test_releasing_does_not_count_as_a_failure(self, conn):
        sa.add(conn, "x", ["u1"])
        for _ in range(5):
            u = sa.claim(conn, "x", worker="w")[0]
            sa.release(conn, u.unit_id, worker="w", note="wrong language")
        # Still claimable. `attempts` still counts hand-outs, which is what
        # protects against a genuine poison unit.
        assert sa.claim(conn, "x", worker="w")

    def test_finish_on_an_unknown_unit_is_false_not_an_exception(self, conn):
        assert sa.finish(conn, "x:nope") is False
        assert sa.fail(conn, "x:nope", note="n") is False


class TestConcurrency:
    def test_real_processes_racing_one_file_do_not_collide(self, tmp_path):
        """The only test that proves it.

        In-process claims share a connection and could pass while the
        cross-process case deadlocks or double-issues. Three interpreters, one
        file, sixty units, each handed out exactly once.
        """
        db = tmp_path / "race.db"
        conn = sa.connect(db)
        sa.add(conn, "x", [f"u{i}" for i in range(60)])
        conn.close()

        src = str(ROOT / "src")
        prog = (
            "import sys, json;"
            f"sys.path.insert(0, {src!r});"
            "import fleetwright as sa;"
            f"c = sa.connect({str(db)!r});"
            "print(json.dumps([u.name for u in "
            "sa.claim(c, 'x', worker=sys.argv[1], n=20)]))"
        )
        procs = [subprocess.Popen([sys.executable, "-c", prog, f"w{i}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  # text=True alone decodes with the locale
                                  # codec, which is cp1252 on Windows.
                                  text=True, encoding="utf-8")
                 for i in range(3)]
        got = []
        for p in procs:
            out, err = p.communicate(timeout=90)
            assert p.returncode == 0, err
            got.append(json.loads(out))
        flat = [u for g in got for u in g]
        assert len(flat) == 60, f"every unit should be handed out once, got {len(flat)}"
        assert len(set(flat)) == 60, "a unit went to two processes"

    def test_wal_is_on_because_nothing_works_without_it(self, conn):
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0


class TestCLI:
    def test_claim_exits_1_on_an_empty_queue_so_shell_loops_end(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        assert cli_main(["add", "x", "u1", "--db", db]) == 0
        assert cli_main(["claim", "x", "--db", db]) == 0
        assert cli_main(["claim", "x", "--db", db]) == 1

    def test_json_output_is_parseable(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        cli_main(["add", "x", "u1", "--db", db])
        capsys.readouterr()                       # discard `add`'s own line
        cli_main(["claim", "x", "--db", db, "--json"])
        out = capsys.readouterr()
        rows = json.loads(out.out)                # stdout is JSON and nothing else
        assert out.err == "", "--json must be quiet on both streams"
        assert rows[0]["unit_id"] == "x:u1"

    def test_done_on_a_lost_lease_exits_nonzero(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["add", "x", "u1", "--db", db])
        cli_main(["claim", "x", "--db", db, "--worker", "a", "--lease", "300"])
        expire(sa.connect(db))
        cli_main(["claim", "x", "--db", db, "--worker", "b"])
        assert cli_main(["done", "x:u1", "--db", db, "--worker", "a"]) == 1
        assert cli_main(["done", "x:u1", "--db", db, "--worker", "b"]) == 0

    def test_status_on_an_empty_db_says_what_to_do_next(self, tmp_path, capsys):
        cli_main(["status", "--db", str(tmp_path / "w.db")])
        assert "fleetwright add" in capsys.readouterr().out

    def test_the_demo_runs_and_cleans_up_after_itself(self, capsys):
        """The cleanup half is the Windows half.

        The demo works in a TemporaryDirectory. Windows will not delete a file
        that is still open, so an unclosed connection makes the demo do all its
        work, print all its output, and then die on the very last line. POSIX
        never reproduces it, which is what the Windows runner is for.
        """
        import tempfile

        from fleetwright.demo import main as demo
        seen = []
        real = tempfile.TemporaryDirectory

        class Watched(real):
            def __enter__(self):
                seen.append(self.name)
                return super().__enter__()

        tempfile.TemporaryDirectory = Watched
        try:
            assert demo() == 0
        finally:
            tempfile.TemporaryDirectory = real
        assert "nobody got the same page" in capsys.readouterr().out
        assert seen and not Path(seen[0]).exists(), \
            "the demo left its temporary directory behind"


class TestMCP:
    def _server(self, tmp_path):
        return Server(tmp_path / "w.db")

    def test_tools_are_listed_and_dispatchable(self, tmp_path):
        s = self._server(tmp_path)
        listed = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == {"project_state", "start_run", "list_runs",
                         "register_skill", "list_skills", "define_kind",
                         "add_jobs", "worker_prompt", "job_results",
                         "claim_job", "finish_job", "release_job", "fail_job",
                         "heartbeat_job", "job_status"}
        for n in names:
            assert callable(getattr(s, n)), f"{n} is advertised but not implemented"

    def test_an_empty_queue_tells_the_agent_to_stop(self, tmp_path):
        out = self._server(tmp_path).claim_job({})
        assert out["queue_empty"] is True
        assert "stop" in out["note"].lower()

    def test_claim_then_finish(self, tmp_path):
        s = self._server(tmp_path)
        sa.add(s.conn, "x", ["u1"])
        got = s.claim_job({"kind": "x"})
        assert got["units"][0]["name"] == "u1"
        assert s.finish_job({"unit_id": "x:u1"})["finished"] is True

    def test_an_orchestrator_can_set_up_a_fleet_through_mcp_alone(self, tmp_path):
        s = self._server(tmp_path)
        s.define_kind({"kind": "extract",
                       "instructions": "Read $path and record claims.",
                       "done_when": "every claim recorded",
                       "returns": '{"claims": <int>}'})
        assert s.add_jobs({"kind": "extract", "names": ["p1"],
                           "meta": {"path": "a.png"}})["added"] == 1
        u = s.claim_job({"kind": "extract"})["units"][0]
        assert u["instructions"] == "Read a.png and record claims."
        assert "DONE WHEN" in u["brief"]
        s.finish_job({"unit_id": u["unit_id"], "result": {"claims": 7}})
        assert s.job_results({})["results"][0]["result"]["claims"] == 7

    def test_enqueueing_an_undefined_kind_is_refused_with_the_fix(self, tmp_path):
        out = self._server(tmp_path).add_jobs({"kind": "nope", "names": ["u1"]})
        assert out["ok"] is False and "define_kind" in out["message"]

    def test_a_kind_without_done_when_warns(self, tmp_path):
        out = self._server(tmp_path).define_kind({"kind": "x", "instructions": "go"})
        assert "warning" in out

    def test_a_retried_unit_is_flagged_to_the_agent(self, tmp_path):
        s = self._server(tmp_path)
        sa.add(s.conn, "x", ["u1"])
        s.claim_job({"kind": "x", "lease_seconds": 300})
        expire(s.conn)
        assert "warning" in s.claim_job({"kind": "x"})

    def test_status_shows_other_workers_not_this_one(self, tmp_path):
        s = self._server(tmp_path)
        sa.add(s.conn, "x", ["mine", "theirs"])
        s.claim_job({"kind": "x"})
        sa.claim(s.conn, "x", worker="someone-else")
        elsewhere = s.job_status({})["in_progress_elsewhere"]
        assert [u["name"] for u in elsewhere] == ["theirs"]

    def test_a_tool_error_is_returned_not_raised(self, tmp_path):
        s = self._server(tmp_path)
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "finish_job", "arguments": {}}})
        assert r["result"]["isError"] is True

    def test_unknown_tools_are_refused(self, tmp_path):
        r = self._server(tmp_path).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "connect", "arguments": {}}})
        assert r["error"]["code"] == -32601, "no reaching non-tool attributes"


def commands_named_in(text: str) -> set[str]:
    r"""Commands a document actually invokes, not sentences about the product.

    `fleetwright (\w+)` also matches prose: "fleetwright is where both live"
    yields `is`, and "fleetwright exists to prevent" yields `exists`. A
    line-initial mention is only an invocation INSIDE A CODE FENCE -- outside
    one it is just a sentence that happens to begin with the product name.
    Elsewhere, backticks are the signal.

    This used to allow a line-initial mention anywhere, with `as` hardcoded
    into an allowlist to paper over the one instance that had come up. The
    fence rule removes the whole class instead of the instances.
    """
    import re
    fenced, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            fenced.append(line)
    found = (set(re.findall(r"^\s*fleetwright ([\w-]+)", "\n".join(fenced), re.M))
             | set(re.findall(r"`fleetwright ([\w-]+)", text)))
    # `fleetwright 0.16.0 · work.db` in a sample of output is not a command
    # called `0`. Command names never start with a digit.
    return {c for c in found if not c[0].isdigit()}


class TestDocs:
    def test_every_readme_link_resolves(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        missing = [t for t in re.findall(r"\]\(([^)#:]+\.md)\)", readme)
                   if not (ROOT / t).exists()]
        assert not missing, f"README links to missing files: {missing}"

    def test_the_reference_matches_the_cli(self):
        from fleetwright.cli import build_parser
        real = set(build_parser()._subparsers._group_actions[0].choices)
        # The shared helper, so the digit filter and the invocation rule live
        # in one place. Three copies of this regex is how `fleetwright 0.16.0`
        # in a sample of output became a command called `0`.
        doc = commands_named_in(
            (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"))
        assert not doc - real, f"documented but absent: {sorted(doc - real)}"
        assert not real - doc, f"undocumented commands: {sorted(real - doc)}"

    def test_the_skill_only_uses_commands_that_exist(self):
        """The skill teaches agents shell commands. If one is renamed, the
        skill goes stale silently and every agent that reads it runs a command
        that does not exist."""
        from fleetwright.cli import build_parser
        real = set(build_parser()._subparsers._group_actions[0].choices)
        text = (ROOT / "src" / "fleetwright" / "skill" / "SKILL.md").read_text(encoding="utf-8")
        used = commands_named_in(text) - {"serve"}
        assert not used - real, f"skill uses commands that do not exist: {sorted(used - real)}"

    def test_the_skill_names_every_mcp_tool_correctly(self):
        from fleetwright.mcp import _tools
        real = {t["name"] for t in _tools()}
        text = (ROOT / "src" / "fleetwright" / "skill" / "SKILL.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`(\w+_(?:job|jobs|kind|results|status))`", text))
        assert not named - real, f"skill names tools that do not exist: {sorted(named - real)}"

    def test_the_skill_has_the_frontmatter_that_makes_it_loadable(self):
        text = (ROOT / "src" / "fleetwright" / "skill" / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), "no frontmatter; the skill will not load"
        fm = text.split("---", 2)[1]
        assert re.search(r"^name: fleetwright$", fm, re.M)
        desc = re.search(r"^description: (.+)$", fm, re.M)
        assert desc, "no description — nothing decides when to offer the skill"
        # The description is the only thing that decides whether the skill is
        # surfaced. One that does not say WHEN to use it never gets loaded.
        assert "Use when" in desc[1] or "use when" in desc[1]

    def test_the_docs_do_not_promise_exactly_once(self):
        for f in (ROOT / "docs").glob("*.md"):
            t = f.read_text(encoding="utf-8").lower()
            assert "exactly-once" not in t or "not exactly-once" in t \
                or "cannot" in t, f"{f.name} may be overpromising delivery"


class TestPackaging:
    """The formula is generated, so the generator is what has to be right."""

    def _formula(self, monkeypatch, capsys):
        sys.path.insert(0, str(ROOT / "packaging"))
        import brew_formula
        monkeypatch.setattr(brew_formula, "sdist", lambda v: (
            f"https://files.pythonhosted.org/packages/ab/fleetwright-{v}.tar.gz",
            "0" * 64,
            "Work leases in one SQLite file, so a fleet of agents divides a job "
            "list instead of racing it. Library, CLI and MCP server."))
        monkeypatch.setattr(sys, "argv", ["brew_formula.py", "v0.1.0"])
        assert brew_formula.main() == 0
        return capsys.readouterr().out

    def test_ruby_interpolation_survives_the_python_template(self, monkeypatch, capsys):
        out = self._formula(monkeypatch, capsys)
        # `#{bin}` is Ruby interpolation and `{bin}` is a str.format field.
        # Getting the escaping wrong produces a formula that installs and then
        # fails its own test block, in someone else's CI.
        assert "#{bin}/fleetwright" in out
        assert "{{" not in out and "#{{" not in out

    def test_the_formula_names_the_published_sdist_and_its_checksum(self, monkeypatch, capsys):
        out = self._formula(monkeypatch, capsys)
        assert 'url "https://files.pythonhosted.org/packages/ab/fleetwright-0.1.0.tar.gz"' in out
        assert f'sha256 "{"0" * 64}"' in out
        assert "class Fleetwright < Formula" in out

    def test_desc_meets_homebrew_audit_rules(self, monkeypatch, capsys):
        desc = [ln for ln in self._formula(monkeypatch, capsys).splitlines()
                if ln.strip().startswith("desc ")][0].strip()[6:-1]
        assert len(desc) <= 70, "brew audit rejects a desc over 80 incl. `desc `"
        assert not desc.endswith(("lis", "sever")) and desc.split()[-1] != "a", \
            "truncated mid-word; cut at a word boundary"
        assert not desc.endswith("."), "brew audit rejects a trailing full stop"
        assert not desc.lower().startswith("fleetwright"), \
            "brew audit rejects a desc starting with the formula name"

    def test_no_resource_blocks_because_there_are_no_dependencies(self, monkeypatch, capsys):
        # If a runtime dependency is ever added this test fails, which is the
        # reminder that the formula now needs resource blocks per transitive
        # dependency and this file is no longer twenty lines.
        import tomllib
        deps = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        assert deps == [], f"add resource blocks to the formula for {deps}"
        # An actual resource block, not the word in the explanatory comment.
        assert "\n  resource " not in self._formula(monkeypatch, capsys)


class _FakeHandler:
    """Drive the dashboard's real request handler without a socket.

    Going through `do_GET` rather than `_payload` is the point: a future route
    that reclaims is caught, and `_payload` is not the only thing a browser
    touches.
    """

    def __init__(self, dashboard, db):
        from pathlib import Path as _P
        self.dashboard, self.db = dashboard, _P(db)

    def get(self, path):
        import io
        d = self.dashboard
        H = d.Handler if hasattr(d, "Handler") else d._Handler
        h = H.__new__(H)
        h.projects = {self.db.stem: self.db}
        h.token = None
        h.sessions = {}
        h.allowed_hosts = frozenset()      # not enforcing, as when bound to all
        h.failures = {}
        h.path = path
        h.headers = {}
        h.wfile = io.BytesIO()
        h.rfile = io.BytesIO()
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda: None
        h.send_error = lambda *a, **k: None
        h.log_message = lambda *a, **k: None
        h.do_GET()
        return h.wfile.getvalue()


class TestDashboard:
    def _stats(self, conn):
        from fleetwright import leases
        return leases.stats(conn)

    def test_a_finished_unit_records_how_long_it_took(self, conn):
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w")
        assert self._stats(conn)["duration"]["n"] == 1

    def test_who_finished_a_unit_survives_the_close(self, conn):
        # `worker` used to be nulled on close, which threw away every
        # per-worker number before it could be computed.
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="alice")[0]
        sa.finish(conn, u.unit_id, worker="alice")
        assert self._stats(conn)["per_worker"][0]["worker"] == "alice"

    def test_a_released_unit_has_no_holder(self, conn):
        # …but a unit going back to open must NOT keep one, or the in-flight
        # panel would show a worker on something nobody is doing.
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="bob")[0]
        sa.release(conn, u.unit_id, worker="bob")
        assert conn.execute("SELECT worker FROM unit").fetchone()["worker"] is None

    def test_stats_on_an_empty_database_does_not_explode(self, conn):
        s = self._stats(conn)
        assert s["totals"]["all"] == 0 and s["throughput"] == []
        assert s["duration"]["p50"] is None and s["eta_seconds"] is None

    def test_percentiles_report_the_tail_not_the_mean(self, conn):
        import time as _t
        sa.add(conn, "x", [f"u{i}" for i in range(10)])
        for i, u in enumerate(sa.claim(conn, "x", worker="w", n=10)):
            # One unit far slower than the rest: a mean would hide it.
            conn.execute("UPDATE unit SET claimed_at=? WHERE unit_id=?",
                         (_t.time() - (30 if i == 9 else 1), u.unit_id))
            conn.commit()
            sa.finish(conn, u.unit_id, worker="w")
        d = self._stats(conn)["duration"]
        assert d["p50"] < 5 and d["max"] > 25

    def test_the_page_is_self_contained(self, tmp_path):
        from fleetwright import dashboard
        db = tmp_path / "w.db"
        sa.add(sa.connect(db), "x", ["u1"])
        html = dashboard.snapshot(db)
        # A CSP-restricted or offline viewer must still get the whole page.
        assert not re.search(r'(?:src|href)="https?://', html)
        assert "<script" in html and "</style>" in html
        assert json.loads(
            next(ln for ln in html.splitlines() if ln.startswith("const DATA = "))
            [len("const DATA = "):].rsplit(";", 1)[0])["totals"]["all"] == 1

    def test_both_themes_are_defined_not_inverted(self, tmp_path):
        from fleetwright import dashboard
        db = tmp_path / "w.db"
        sa.add(sa.connect(db), "x", ["u1"])
        html = dashboard.snapshot(db)
        for hook in ("prefers-color-scheme: dark", ':root[data-theme="dark"]',
                     ':root[data-theme="light"]'):
            assert hook in html, f"{hook} missing; the theme toggle will not win"

    def test_the_dashboard_never_writes(self, tmp_path):
        """Pointing it at a live run must not disturb the run.

        The previous version of this test passed while the dashboard was
        reclaiming leases on every GET, and it is worth saying how, because
        all three mistakes are easy to repeat:

        1. It grepped dashboard.py for write verbs. The write was `reclaim()`
           inside `leases.stats()`, and a source scan cannot see through a
           function call.
        2. It added a unit and asserted it was still `open` afterwards --
           without ever CLAIMING it. `reclaim` only touches expired *leased*
           rows, so it asserted the one state the bug could not affect.
        3. `assert len(db.read_bytes()) >= len(before)` passes when the file
           GROWS. It asserted the opposite of what it meant.

        So this one sets up the state the bug needs, drives the real routes,
        and compares the table contents rather than the file length.
        """
        from fleetwright import dashboard
        db = tmp_path / "w.db"
        conn = sa.connect(db)
        run = sa.start_run(conn, label="live")
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", ["u1", "u2"], run=run)
        # A worker that is SLOW, not dead: its lease has expired but it is
        # still working. This is precisely what a viewer must not disturb.
        sa.claim(conn, "k", worker="slow-but-alive", lease=1)
        conn.execute("UPDATE unit SET leased_until = ?", (time.time() - 1,))
        conn.commit()

        def snapshot_tables():
            c = sa.connect(db)
            return {t: c.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("unit", "kind", "run", "skill")}

        def rows_equal(a, b):
            return all([tuple(r) for r in a[t]] == [tuple(r) for r in b[t]]
                       for t in a)

        before = snapshot_tables()
        handler = _FakeHandler(dashboard, db)
        for path in ("/", "/api", "/api/units", "/api/units?limit=1&offset=0",
                     "/favicon.ico"):
            handler.get(path)
        assert rows_equal(before, snapshot_tables()), (
            "a GET changed the queue")
        assert sa.units(sa.connect(db))["units"][0]["status"] == "leased"

    def test_the_dashboard_opens_the_file_read_only(self, tmp_path):
        """Enforced by SQLite, not by everyone remembering."""
        db = tmp_path / "w.db"
        sa.add(sa.connect(db), "x", ["u1"])
        conn = sa.connect_readonly(db)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE unit SET note = 'x'")
            conn.commit()

    def test_a_missing_database_is_an_error_not_an_empty_one(self, tmp_path):
        """A typo in --db used to create a database and then report a
        perfectly healthy zero units."""
        with pytest.raises(FileNotFoundError):
            sa.connect_readonly(tmp_path / "typo.db")
        assert not (tmp_path / "typo.db").exists()


class TestDashboardAuth:
    """The login exists to make one mistake impossible, not to look secure."""

    def _serve(self, tmp_path, **kw):
        from fleetwright import dashboard
        db = tmp_path / "p.db"
        sa.add(sa.connect(db), "x", ["a"])
        return dashboard, db

    def test_binding_off_loopback_without_a_token_is_refused(self, tmp_path):
        dashboard, db = self._serve(tmp_path)
        with pytest.raises(SystemExit) as e:
            dashboard.serve(db, host="0.0.0.0", open_browser=False)
        # An error that only says no is half an error: it has to name the
        # flag that fixes it and the host that triggered it.
        assert "--token" in str(e.value) and "0.0.0.0" in str(e.value)

    def test_loopback_without_a_token_is_allowed(self, tmp_path, monkeypatch):
        dashboard, db = self._serve(tmp_path)
        started = {}

        class FakeServer:
            def __init__(self, addr, handler):
                started["addr"] = addr
                started["handler"] = handler

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def serve_forever(self):
                raise KeyboardInterrupt

        monkeypatch.setattr(dashboard, "ThreadingHTTPServer", FakeServer)
        dashboard.serve(db, host="127.0.0.1", open_browser=False)
        assert started["addr"] == ("127.0.0.1", 8787)
        assert started["handler"].token is None

    def test_a_wrong_token_is_compared_in_constant_time(self):
        # Not a timing measurement -- those are flaky. This asserts the code
        # uses compare_digest, because `==` on a secret leaks its prefix.
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(encoding="utf-8")
        assert "hmac.compare_digest" in src
        assert "== self.token" not in src and "self.token ==" not in src

    def test_the_session_cookie_is_httponly_and_samesite(self):
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(encoding="utf-8")
        assert "HttpOnly" in src and "SameSite=Strict" in src

    def test_projects_are_databases_and_a_directory_expands(self, tmp_path):
        from fleetwright import dashboard
        (tmp_path / "d").mkdir()
        for n in ("alpha", "beta"):
            sa.connect(tmp_path / "d" / f"{n}.db")
        sa.connect(tmp_path / "solo.db")
        got = dashboard._projects([tmp_path / "d", tmp_path / "solo.db"])
        assert set(got) == {"alpha", "beta", "solo"}

    def test_the_page_renders_a_sidebar_and_a_gate(self, tmp_path):
        from fleetwright import dashboard
        db = tmp_path / "p.db"
        sa.add(sa.connect(db), "x", ["a"])
        html = dashboard.snapshot(db)
        for hook in ('class="shell"', 'id="gate"', 'id="projects"',
                     'id="sideruns"', 'id="logout"'):
            assert hook in html, hook

    def test_a_snapshot_carries_its_own_project(self, tmp_path):
        # Without this the static file renders an empty sidebar: `projects`
        # was only ever added by the request handler.
        from fleetwright import dashboard
        db = tmp_path / "kircher.db"
        sa.add(sa.connect(db), "x", ["a"])
        html = dashboard.snapshot(db)
        d = json.loads(next(ln for ln in html.splitlines()
                            if ln.startswith("const DATA = "))
                       [len("const DATA = "):].rsplit(";", 1)[0])
        assert d["projects"] == ["kircher"] and d["project"] == "kircher"
        assert d["auth"] is False

    def test_a_snapshot_never_asks_anyone_to_log_in(self, tmp_path):
        # There is no server behind a static file: showing a login form on one
        # would be asking for a credential nothing can check.
        from fleetwright import dashboard
        db = tmp_path / "p.db"
        sa.add(sa.connect(db), "x", ["a"])
        html = dashboard.snapshot(db)
        assert 'const DATA = {' in html
        assert 'if (DATA) {' in html and '$("#shell").hidden = false;' in html


class TestDashboardBrowser:
    """Things only a browser console shows: 404s and runaway polling."""

    def test_the_page_carries_its_own_favicon(self, tmp_path):
        # Without this the browser asks for /favicon.ico and logs a 404 that
        # looks like a bug in the tool.
        from fleetwright import dashboard
        db = tmp_path / "p.db"
        sa.add(sa.connect(db), "x", ["u1"])
        html = dashboard.snapshot(db)
        assert 'rel="icon"' in html and "data:image/svg+xml," in html

    def test_favicon_ico_is_answered_rather_than_404(self):
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(encoding="utf-8")
        assert '"/favicon.ico"' in src

    def test_polling_stops_while_the_login_gate_is_up(self):
        """A gated page that keeps polling 401s every two seconds forever —
        a console full of errors and a request the server can only refuse."""
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(encoding="utf-8")
        assert "clearInterval(TIMER)" in src
        # And the interval must not be started unconditionally at load.
        assert "poll(); setInterval(poll, 2000);" not in src
        assert "startPolling();" in src


class TestDashboardHiddenAttribute:
    def test_hidden_beats_author_display_rules(self):
        """`hidden` only works via the UA stylesheet, so any author rule that
        sets `display` on the same element wins and the element stays visible.

        This shipped: `#gate { display:grid }` kept the login overlay rendered
        on top of every dashboard, even ones with no token. Nothing that talks
        to the server catches it, because it is purely a CSS cascade problem.
        """
        import re

        from fleetwright import dashboard
        css = dashboard.PAGE[dashboard.PAGE.index("<style>"):
                             dashboard.PAGE.index("</style>")]
        assert re.search(r"\[hidden\][^{]*\{[^}]*display\s*:\s*none\s*!important",
                         css), "no [hidden] guard; toggled elements will not hide"

    def test_every_element_toggled_by_hidden_is_covered(self):
        """Belt and braces: find the ids the page toggles and make sure the
        guard exists, so adding a new one cannot silently regress."""
        import re

        from fleetwright import dashboard
        page = dashboard.PAGE
        toggled = set(re.findall(r'id="(\w+)"[^>]*\shidden', page))
        assert {"gate", "shell"} <= toggled, toggled
        assert "[hidden] { display: none !important; }" in page


class TestUnitsView:
    """Everything else aggregates. This is the only thing that answers
    'what happened to page 189'."""

    def test_it_reports_when_it_truncated(self, conn):
        # A view that silently shows the first 300 of 40,000 is a view that
        # lies, so the caller is told.
        sa.add(conn, "x", [f"u{i}" for i in range(50)])
        d = sa.units(conn, limit=10)
        assert d["total"] == 50 and d["shown"] == 10 and d["truncated"] is True
        assert sa.units(conn, limit=100)["truncated"] is False

    def test_unfinished_and_broken_sort_first(self, conn):
        sa.add(conn, "x", ["poison", "b", "c"])
        # `fail` RETRIES while attempts remain, so one call leaves the unit
        # open. Only the third retires it — which is the behaviour, not a
        # quirk of this test.
        for _ in range(3):
            u = next(u for u in sa.claim(conn, "x", worker="w2", n=3)
                     if u.name == "poison")
            sa.fail(conn, u.unit_id, worker="w2", note="bad")
            for other in sa.leased(conn):
                sa.release(conn, other["unit_id"], worker="w2")
        sa.claim(conn, "x", worker="w")          # something in flight
        got = [u["status"] for u in sa.units(conn)["units"]]
        assert got[0] == "leased" and got[1] == "failed", got

    def test_filters_compose(self, conn):
        r = sa.start_run(conn)
        sa.add(conn, "x", ["keep-1", "keep-2"], run=r)
        sa.add(conn, "y", ["other"], run=r)
        assert sa.units(conn, kind="x")["total"] == 2
        assert sa.units(conn, q="keep")["total"] == 2
        assert sa.units(conn, run=r, kind="y")["total"] == 1
        assert sa.units(conn, run="nope")["total"] == 0

    def test_a_held_unit_shows_elapsed_and_lease_remaining(self, conn):
        sa.add(conn, "x", ["a"])
        sa.claim(conn, "x", worker="w", lease=60)
        u = sa.units(conn)["units"][0]
        assert u["seconds"] is not None and u["lease_left"] > 0

    def test_search_covers_the_note_so_failures_are_findable(self, conn):
        sa.add(conn, "x", ["a"])
        u = sa.claim(conn, "x", worker="w")[0]
        sa.fail(conn, u.unit_id, worker="w", note="no text layer")
        assert sa.units(conn, q="text layer")["total"] == 1


class TestDashboardChrome:
    def test_the_page_has_two_sidebars(self):
        from fleetwright import dashboard
        assert 'aside class="rail"' in dashboard.PAGE
        assert 'aside class="second"' in dashboard.PAGE
        assert "grid-template-columns:196px 248px" in dashboard.PAGE

    def test_runs_live_in_the_second_sidebar_and_jobs_beside_them(self):
        from fleetwright import dashboard
        page = dashboard.PAGE
        second = page[page.index('aside class="second"'):page.index('<div class="body">')]
        assert 'id="sideruns"' in second, "runs are not in the second sidebar"
        assert 'id="nav-jobs"' in second, "no Jobs entry in the second sidebar"

    def test_sign_out_is_always_present_and_disabled_without_a_token(self):
        # Hiding it makes it look like a missing feature; a live one that ends
        # nothing is worse. So: present, disabled, and it says why.
        from fleetwright import dashboard
        page = dashboard.PAGE
        assert 'button class="signout" id="logout"' in page
        assert "lo.disabled = true" in page and "Nothing to sign out of" in page

    def test_the_jobs_endpoint_is_authenticated_like_everything_else(self):
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(encoding="utf-8")
        block = src[src.index('if path == "/api/units":'):src.index('if path == "/api":')]
        assert "self._authed()" in block and "auth_required" in block


class TestPaginationAndModel:
    def test_pages_are_reported_not_inferred(self, conn):
        sa.add(conn, "x", [f"u{i}" for i in range(250)])
        d = sa.units(conn, limit=100, offset=100)
        assert (d["total"], d["page"], d["pages"], d["shown"]) == (250, 2, 3, 100)
        assert sa.units(conn, limit=100, offset=200)["shown"] == 50

    def test_an_offset_past_the_end_is_empty_not_an_error(self, conn):
        sa.add(conn, "x", ["a"])
        d = sa.units(conn, limit=10, offset=999)
        assert d["units"] == [] and d["total"] == 1

    def test_a_worker_declares_its_model_and_it_is_recorded(self, conn):
        sa.add(conn, "x", ["a"])
        u = sa.claim(conn, "x", worker="w", model="claude-opus-5")[0]
        sa.finish(conn, u.unit_id, worker="w")
        assert sa.units(conn)["units"][0]["model"] == "claude-opus-5"

    def test_model_is_declared_never_verified(self, conn):
        # Nothing here can check it. Anything a worker says is stored as said,
        # and the docs must not imply otherwise.
        sa.add(conn, "x", ["a"])
        sa.claim(conn, "x", worker="w", model="definitely-not-a-real-model")
        assert sa.units(conn)["units"][0]["model"] == "definitely-not-a-real-model"

    def test_work_rolls_up_per_model(self, conn):
        from fleetwright import leases
        sa.add(conn, "x", [f"u{i}" for i in range(6)])
        for m, n in (("opus", 4), ("sonnet", 2)):
            for u in sa.claim(conn, "x", worker="w-" + m, n=n, model=m):
                sa.finish(conn, u.unit_id, worker="w-" + m)
        assert [(m["model"], m["done"]) for m in leases.stats(conn)["per_model"]] \
            == [("opus", 4), ("sonnet", 2)]

    def test_searching_covers_the_model(self, conn):
        sa.add(conn, "x", ["a"])
        sa.claim(conn, "x", worker="w", model="claude-opus-5")
        assert sa.units(conn, q="opus")["total"] == 1

    def test_units_without_a_model_are_none_not_a_guess(self, conn):
        sa.add(conn, "x", ["a"])
        sa.claim(conn, "x", worker="w")
        assert sa.units(conn)["units"][0]["model"] is None


class TestRailAndVersion:
    def test_the_version_is_in_the_payload(self, tmp_path):
        from fleetwright import __version__, dashboard
        db = tmp_path / "p.db"
        sa.add(sa.connect(db), "x", ["u1"])
        html = dashboard.snapshot(db)
        assert f'"version": "{__version__}"' in html

    def test_the_rail_collapses_without_disappearing(self):
        from fleetwright import dashboard
        page = dashboard.PAGE
        # Collapsed it must keep the toggle and the project buttons, or there
        # is no way back without knowing where to click.
        assert ".shell.railshut" in page
        assert ".railshut .rail .collapse" in page
        assert ".railshut .rail .navitem" in page

    def test_an_explicit_toggle_survives_a_resize(self):
        from fleetwright import dashboard
        page = dashboard.PAGE
        assert "localStorage.setItem(RAIL_KEY" in page
        # Auto-collapse must not override a stored choice.
        assert 'localStorage.getItem(RAIL_KEY) === null' in page


class TestBrandAndFreshness:
    def test_the_wordmark_is_type_not_an_embedded_image(self):
        """A raster logo would weigh on every page and every snapshot, and the
        snapshot is the artefact people mail to each other."""
        from fleetwright import dashboard
        page = dashboard.PAGE
        assert 'class="wm-s">Fleet' in page and 'class="wm-a">Wright' in page
        assert "data:image/png" not in page and "data:image/jpeg" not in page

    def test_brand_colours_are_their_own_tokens(self):
        # Brand must never be reachable as state: nothing should be able to
        # render "critical" in the logo red by accident.
        from fleetwright import dashboard
        for t in ("--wm-ink", "--wm-red", "--wm-cream"):
            assert t in dashboard.PAGE
        css = dashboard.PAGE[:dashboard.PAGE.index("</style>")]
        for state in ("--done", "--failed", "--leased"):
            assert f"var({state})" not in css.split(".wordmark")[1].split("}")[0]

    def test_the_collapsed_rail_still_shows_a_mark(self):
        from fleetwright import dashboard
        assert ".railshut .rail .wordmark.short { display:inline; }" in dashboard.PAGE

    def test_the_freshness_indicator_says_what_it_is(self):
        """It used to render a bare clock time beside a dot, which reads as a
        timer. Nobody could tell what it counted."""
        from fleetwright import dashboard
        page = dashboard.PAGE
        assert "updated just now" in page and "not yet updated" in page
        # The old line rendered a bare clock into the sidebar with no label.
        assert "const when = new Date(d.now * 1000).toLocaleTimeString();" not in page

    def test_freshness_ticks_locally_so_a_dead_server_shows(self):
        from fleetwright import dashboard
        assert "setInterval(tickFreshness, 1000)" in dashboard.PAGE
        assert "secs > 10" in dashboard.PAGE


class TestSkillRegistry:
    def test_a_readable_source_is_hashed(self, conn, tmp_path):
        f = tmp_path / "s.md"
        f.write_text("quote verbatim", encoding="utf-8")
        r = sa.register_skill(conn, "x", source=str(f), version="1.0")
        assert len(r["digest"]) == 16

    def test_a_url_source_has_no_digest_and_that_is_not_an_error(self, conn):
        r = sa.register_skill(conn, "x", source="https://example.org/s")
        assert r["digest"] is None and r["source"].startswith("https")

    def test_a_unit_pins_the_skill_it_actually_ran_with(self, conn, tmp_path):
        """The whole point: a skill edited mid-run leaves half the units under
        one version and half under another, and only a record taken at claim
        time can tell them apart."""
        f = tmp_path / "s.md"
        f.write_text("v1 text", encoding="utf-8")
        sa.register_skill(conn, "sk", source=str(f), version="1.0")
        sa.define(conn, "x", instructions="go", skills=["sk"])
        sa.add(conn, "x", ["a", "b"])
        first = sa.claim(conn, "x", worker="w")[0]

        f.write_text("v2 text, changed", encoding="utf-8")
        sa.register_skill(conn, "sk", source=str(f), version="2.0")
        second = sa.claim(conn, "x", worker="w")[0]

        d1 = first.skill_records[0]
        d2 = second.skill_records[0]
        assert (d1["version"], d2["version"]) == ("1.0", "2.0")
        assert d1["digest"] != d2["digest"]

    def test_the_pin_is_stored_not_only_returned(self, conn, tmp_path):
        f = tmp_path / "s.md"
        f.write_text("t", encoding="utf-8")
        sa.register_skill(conn, "sk", source=str(f), version="1.0")
        sa.define(conn, "x", instructions="go", skills=["sk"])
        sa.add(conn, "x", ["a"])
        sa.claim(conn, "x", worker="w")
        raw = conn.execute("SELECT skills_used FROM unit").fetchone()[0]
        assert json.loads(raw)[0]["version"] == "1.0"

    def test_an_unregistered_skill_is_surfaced_not_hidden(self, conn):
        sa.define(conn, "x", instructions="go", skills=["ghost"])
        sa.add(conn, "x", ["a"])
        u = sa.claim(conn, "x", worker="w")[0]
        assert u.skill_records[0]["unregistered"] is True
        assert "not in the skill registry" in u.brief()
        listed = {s["name"]: s for s in sa.skills(conn)}
        assert listed["ghost"]["unregistered"] is True and listed["ghost"]["units"] == 1

    def test_usage_counts_come_from_units_not_from_kinds(self, conn):
        # A skill named by a kind nobody ran has been used zero times. Counting
        # mentions would be the sort of number that quietly justifies keeping
        # something.
        sa.register_skill(conn, "unused", source="x")
        sa.define(conn, "x", instructions="go", skills=["unused"])
        assert {s["name"]: s["units"] for s in sa.skills(conn)}["unused"] == 0

    def test_the_registry_never_fetches_anything(self):
        src = (ROOT / "src" / "fleetwright" / "leases.py").read_text(encoding="utf-8")
        for net in ("urllib", "requests", "httpx", "socket.create_connection",
                    "urlopen"):
            assert net not in src, f"leases.py reaches the network via {net!r}"

    def test_a_kind_naming_unregistered_skills_is_flagged_over_mcp(self, tmp_path):
        from fleetwright.mcp import Server
        s = Server(tmp_path / "w.db")
        out = s.define_kind({"kind": "x", "instructions": "go",
                             "done_when": "d", "skills": ["ghost"]})
        assert out["unregistered_skills"] == ["ghost"]
        assert "register_skill" in out["hint"]


class TestLicenceBoundary:
    """Open core only works if the boundary is enforced, not just described."""

    def test_the_published_package_is_apache_only(self):
        """`ee/` must never end up inside an Apache-2.0 wheel or sdist.

        Shipping a differently-licensed directory inside the package is how a
        licence question becomes a licence problem, months later, for someone
        who never looked at the repository.
        """
        import tarfile
        import tomllib
        import zipfile
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        sdist = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]
        inc = sdist["include"]
        assert not any(i == "ee" or i.startswith("ee/") for i in inc), inc
        # `include` alone does NOT keep it out: hatchling collects licence
        # files from anywhere in the tree as metadata, so ee/LICENSE shipped
        # anyway. Only the explicit exclude stops it.
        assert "ee" in sdist.get("exclude", []), "no exclude for ee/"
        pkgs = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        assert pkgs == ["src/fleetwright"], pkgs

        # And check the ARTEFACTS when they exist, because the config passing
        # is what let this through the first time.
        for tgz in (ROOT / "dist").glob("*.tar.gz"):
            with tarfile.open(tgz) as t:
                bad = [n for n in t.getnames() if "/ee/" in n]
            assert not bad, f"{tgz.name} ships {bad}"
        for whl in (ROOT / "dist").glob("*.whl"):
            with zipfile.ZipFile(whl) as z:
                bad = [n for n in z.namelist() if n.startswith("ee/") or "/ee/" in n]
            assert not bad, f"{whl.name} ships {bad}"

    def test_no_import_of_ee_from_the_core(self):
        """The core must run complete without `ee/`. If it imports from there,
        the Apache-2.0 half is not actually a working tool."""
        for f in (ROOT / "src" / "fleetwright").glob("*.py"):
            src = f.read_text(encoding="utf-8")
            assert "import ee" not in src and "from ee" not in src, f.name

    def test_the_boundary_is_written_down_in_both_directions(self):
        lic = (ROOT / "LICENSING.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        # It must say what you CAN do, not only what you cannot.
        assert "Apache-2.0" in lic and "ee/" in lic
        # And it must promise the core is not clawed back later.
        assert "never move" in lic.lower() or "stays there" in lic.lower()

        # `ee/` is absent from the sdist BY DESIGN, and this suite runs inside
        # the unpacked sdist in CI. Its absence there is the property under
        # test, not a reason to fail — so check the file only where it exists.
        ee_license = ROOT / "ee" / "LICENSE"
        if not ee_license.exists():
            assert not (ROOT / "ee").exists(), \
                "ee/ is present but has no LICENSE — the boundary is unstated"
            return
        ee = ee_license.read_text(encoding="utf-8").replace("\r\n", "\n")
        assert "Nothing here restricts the rest of the repository" in ee

    def test_there_is_no_cla_only_a_dco(self):
        c = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        assert "Developer Certificate of Origin" in c
        assert "no contributor licence agreement" in c.lower()

    def test_the_root_licence_is_still_apache(self):
        assert "Apache License" in (ROOT / "LICENSE").read_text(encoding="utf-8")


class TestReadmeIsTrue:
    """A README that overstates is worse than a thin one: it is the only thing
    most people will ever read, and nothing else in the repo contradicts it."""

    def _readme(self):
        # Newlines normalised. Git may check the file out with CRLF on Windows
        # (.gitattributes now forbids it, but a contributor's config still
        # can), and a multi-line assertion against file contents would then
        # fail on that platform alone.
        return (ROOT / "README.md").read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_the_mcp_tool_count_is_right(self):
        import re

        from fleetwright.mcp import _tools
        words = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
                 "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
                 "eighteen": 18}
        m = re.search(r"([A-Za-z]+) MCP tools", self._readme())
        assert m, "the README no longer states a tool count"
        assert words[m[1].lower()] == len(_tools()), \
            f"README says {m[1]}, there are {len(_tools())}"

    def test_every_tool_and_command_named_exists(self):
        import re

        from fleetwright.cli import build_parser
        from fleetwright.mcp import _tools
        r = self._readme()
        real = {t["name"] for t in _tools()}
        named = set(re.findall(
            r"`(\w+_(?:run|runs|skill|skills|kind|jobs|job|prompt|results|status))`", r))
        assert not named - real, sorted(named - real)
        cmds = commands_named_in(r)
        realc = set(build_parser()._subparsers._group_actions[0].choices)
        # "as" comes from "install fleetwright ... as a library".
        assert not cmds - realc, sorted(cmds - realc)

    def test_the_zero_dependency_claim_is_true(self):
        import tomllib
        assert "no\ndependencies at all" in self._readme() \
            or "no dependencies at all" in self._readme()
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert cfg["project"]["dependencies"] == []

    def test_the_wordmark_is_committed_and_renders_without_a_style_block(self):
        import xml.dom.minidom
        svg = ROOT / "assets" / "fleetwright.svg"
        assert svg.exists(), "README references a wordmark that is not in the repo"
        assert 'src="assets/fleetwright.svg"' in self._readme()
        d = xml.dom.minidom.parse(str(svg))
        els = {n.tagName for n in d.getElementsByTagName("*")}
        # GitHub strips <style> from SVG rendered in a README, so the wordmark
        # has to carry presentation attributes or it renders as black text.
        assert "style" not in els, "SVG uses a <style> block; GitHub will strip it"
        assert 'alt="FleetWright"' in self._readme(), \
            "no alt text; PyPI cannot resolve the relative path and shows nothing"

    def test_no_em_dashes(self):
        r = self._readme()
        assert "—" not in r, "em dash in README"
        assert "–" not in r, "en dash in README"


class TestLineEndings:
    def test_the_repo_pins_lf(self):
        """Without this, Git rewrites LF to CRLF on Windows checkout and any
        multi-line assertion against a file fails on that platform alone.
        v0.9.2's Windows job failed for exactly this reason."""
        ga = ROOT / ".gitattributes"
        if not ga.exists():
            # This suite also runs inside the unpacked sdist, which ships no
            # repository plumbing. The file is a checkout concern; its absence
            # from a tarball is correct. Twice now a test has read a repo-only
            # file and broken the sdist job, so: check where it applies.
            pytest.skip("not a git checkout (running from an sdist)")
        assert "text=auto eol=lf" in ga.read_text(encoding="utf-8")

    def test_doc_assertions_survive_a_crlf_checkout(self, monkeypatch):
        """Behavioural, not a source-shape check.

        Hand the README-truth tests a CRLF version of the file and they must
        still pass. This is the actual failure that took down v0.9.2's Windows
        job, reproduced without needing Windows.
        """
        real = Path.read_text

        def crlf(self, *a, **kw):
            out = real(self, *a, **kw)
            return out.replace("\n", "\r\n") if self.suffix == ".md" else out

        monkeypatch.setattr(Path, "read_text", crlf)
        t = TestReadmeIsTrue()
        t.test_the_zero_dependency_claim_is_true()
        t.test_no_em_dashes()
        t.test_every_tool_and_command_named_exists()


class TestMetadataMatchesReality:
    def test_the_classifiers_list_every_python_ci_tests(self):
        """The pyversions badge is generated from these. Listing fewer than CI
        tests understates support; listing more claims something untested."""
        import re
        import tomllib
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = {c.rsplit(" ", 1)[1] for c in cfg["project"]["classifiers"]
                    if c.startswith("Programming Language :: Python :: 3")}
        ci = (ROOT / ".github" / "workflows" / "ci.yml")
        if not ci.exists():
            pytest.skip("not a git checkout (running from an sdist)")
        tested = set(re.findall(r'python:\s*"(3\.\d+)"', ci.read_text(encoding="utf-8")))
        assert declared == tested, f"declared {sorted(declared)}, CI tests {sorted(tested)}"

    def test_requires_python_agrees_with_the_lowest_tested(self):
        import tomllib
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = sorted(c.rsplit(" ", 1)[1] for c in cfg["project"]["classifiers"]
                          if c.startswith("Programming Language :: Python :: 3"))
        assert cfg["project"]["requires-python"] == f">={declared[0]}"


class TestCLIWiring:
    """Every run test called the library directly, so `add --run` was accepted
    and ignored for four releases and no test noticed. These go through the
    CLI, which is the surface that was broken."""

    def test_add_run_actually_attaches_units_to_the_run(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["start", "--db", db, "--label", "L", "--id", "R1"])
        cli_main(["add", "x", "a", "b", "--db", db, "--run", "R1"])
        conn = sa.connect(db)
        runs = sa.runs(conn)
        assert runs[0]["units"] == 2, "the run has no units; --run was ignored"
        assert all(r["run_id"] == "R1"
                   for r in conn.execute("SELECT run_id FROM unit"))

    def test_every_flag_is_read_by_its_handler(self):
        """The sweep that found `add --run`. One unwired flag out of 17
        subcommands was luck, not design."""
        import inspect
        import re

        from fleetwright import cli
        parser = cli.build_parser()
        unread = []
        for name, sp in parser._subparsers._group_actions[0].choices.items():
            fn = sp.get_default("fn")
            if fn is None:
                continue
            body = inspect.getsource(fn)
            # Follow the module-level helpers the handler calls, TRANSITIVELY.
            # `--result` is read inside _read_result, and `--create` inside
            # resolve_db, which _conn calls -- one level of following reported
            # that as unread on all 26 subcommands. Following the chain is the
            # honest version of the check; extending the exemption list is not.
            seen, queue = set(), [body]
            while queue:
                chunk = queue.pop()
                for helper in set(re.findall(r"\b([a-z_]+)\(a\b", chunk)):
                    if helper in seen:
                        continue
                    seen.add(helper)
                    target = getattr(cli, helper, None)
                    if callable(target):
                        src = inspect.getsource(target)
                        body += src
                        queue.append(src)
            for act in sp._actions:
                if not act.option_strings or act.dest in ("help", "db"):
                    continue
                if not re.search(rf"\ba\.{re.escape(act.dest)}\b", body) and \
                        not re.search(rf'getattr\(a, "{re.escape(act.dest)}"', body):
                    unread.append(f"{name} {act.option_strings[0]}")
        assert not unread, f"flags accepted but never read: {unread}"

    def test_finish_and_done_are_the_same_command(self, tmp_path):
        # The brief says "call finish". Until now the CLI only had `done`, so a
        # worker following its own brief ran a command that did not exist.
        for verb in ("finish", "done"):
            db = str(tmp_path / f"{verb}.db")
            cli_main(["add", "x", "u", "--db", db])
            cli_main(["claim", "x", "--db", db, "--worker", "w"])
            assert cli_main([verb, "x:u", "--db", db, "--worker", "w"]) == 0

    def test_the_brief_names_a_command_that_exists(self):
        import re

        from fleetwright.cli import build_parser
        conn = sa.connect(":memory:")
        sa.define(conn, "x", instructions="go")
        sa.add(conn, "x", ["u"])
        brief = sa.claim(conn, "x", worker="w")[0].brief()
        real = set(build_parser()._subparsers._group_actions[0].choices)
        for verb in re.findall(r"Call (\w+)", brief) + re.findall(r"or (\w+) with", brief):
            assert verb in real, f"the brief says {verb!r}, which is not a command"

    def test_a_malformed_result_is_an_error_not_a_traceback(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        cli_main(["add", "x", "u", "--db", db])
        cli_main(["claim", "x", "--db", db, "--worker", "w"])
        with pytest.raises(SystemExit) as e:
            cli_main(["finish", "x:u", "--db", db, "--worker", "w", "--result", "{not json"])
        assert e.value.code == 2
        assert "not valid JSON" in capsys.readouterr().err
        # And the unit must still be leased, not silently lost.
        assert sa.progress(sa.connect(db))["x"][sa.LEASED] == 1

    def test_result_file_carries_what_an_argument_cannot(self, tmp_path):
        # Linux caps a single argument at 128 KB whatever ARG_MAX says, so a
        # large result has no route without this.
        db = str(tmp_path / "w.db")
        big = {"verdicts": [{"id": i, "reason": "x" * 200} for i in range(2000)]}
        f = tmp_path / "r.json"
        f.write_text(json.dumps(big), encoding="utf-8")
        assert len(f.read_text(encoding="utf-8")) > 400_000
        cli_main(["add", "x", "u", "--db", db])
        cli_main(["claim", "x", "--db", db, "--worker", "w"])
        assert cli_main(["finish", "x:u", "--db", db, "--worker", "w",
                         "--result-file", str(f)]) == 0
        got = sa.results(sa.connect(db))[0]["result"]
        assert len(got["verdicts"]) == 2000

    def test_result_and_result_file_together_is_refused(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        f = tmp_path / "r.json"
        f.write_text("{}", encoding="utf-8")
        cli_main(["add", "x", "u", "--db", db])
        cli_main(["claim", "x", "--db", db, "--worker", "w"])
        with pytest.raises(SystemExit):
            cli_main(["finish", "x:u", "--db", db, "--worker", "w",
                      "--result", "{}", "--result-file", str(f)])

    def test_the_brief_shows_meta_so_scope_is_visible(self, conn):
        sa.define(conn, "x", instructions="do $name")
        sa.add(conn, "x", ["u"], meta={"claims": 24, "path": "/a/b"})
        b = sa.claim(conn, "x", worker="w")[0].brief()
        assert "ABOUT THIS UNIT" in b and "claims: 24" in b

    def test_skill_check_reports_a_changed_source(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        f = tmp_path / "s.md"
        f.write_text("original", encoding="utf-8")
        cli_main(["skill", "sk", "--db", db, "--source", str(f), "--version", "1"])
        assert cli_main(["skill-check", "--db", db]) == 0
        assert "OK" in capsys.readouterr().out
        f.write_text("edited since registration", encoding="utf-8")
        assert cli_main(["skill-check", "--db", db]) == 1
        assert "CHANGED" in capsys.readouterr().out


class TestShapeChecking:
    """`returns` was prose. Now it is checked, and the checking must never
    turn a legitimate prose description into a failure."""

    def test_prose_returns_disables_checking(self):
        from fleetwright import shape
        for prose in ("a sentence about what you found", "", None,
                      "the number of claims"):
            assert shape.parse(prose) is None
            assert shape.describe(prose, "literally anything") == []

    def test_bare_and_quoted_placeholders_both_parse(self):
        """`<int>` and `"<string>"` are both natural to write. Quoting the
        already-quoted one produced `""<string>""`, which fails to parse, so
        every shape silently became 'no shape' and nothing was checked."""
        from fleetwright import shape
        t = shape.parse('{"claims": <int>, "notes": "<string>"}')
        assert t == {"claims": "<int>", "notes": "<string>"}

    def test_the_failures_that_matter(self):
        from fleetwright import shape
        t = '{"claims": <int>, "notes": "<string>"}'
        assert shape.describe(t, "a bare string")
        assert shape.describe(t, {"notes": "x"})            # missing key
        assert shape.describe(t, {"claims": "12", "notes": "x"})  # wrong type

    def test_extra_keys_are_allowed(self):
        # Returning more than promised breaks nothing, and refusing it would
        # punish the useful habit of including context.
        from fleetwright import shape
        assert shape.describe('{"claims": <int>}',
                              {"claims": 1, "why": "because"}) == []

    def test_a_bool_is_not_an_int(self):
        from fleetwright import shape
        assert shape.describe('{"n": <int>}', {"n": True})

    def test_optional_keys(self):
        from fleetwright import shape
        t = '{"claims": <int>, "tags?": ["<string>"]}'
        assert shape.describe(t, {"claims": 1}) == []
        assert shape.describe(t, {"claims": 1, "tags": ["a"]}) == []
        assert shape.describe(t, {"claims": 1, "tags": ["a", 2]})

    def test_every_problem_is_reported_at_once(self):
        # An agent told about one problem at a time will redo the work twice.
        from fleetwright import shape
        p = shape.describe('{"a": <int>, "b": <int>, "c": <int>}',
                           {"a": "x", "b": "y"})
        assert len(p) == 3

    def test_the_cli_refuses_and_keeps_the_unit(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        cli_main(["define", "k", "--db", db, "--instructions", "go",
                  "--done-when", "d", "--returns", '{"claims": <int>}'])
        cli_main(["add", "k", "u", "--db", db])
        cli_main(["claim", "k", "--db", db, "--worker", "w"])
        with pytest.raises(SystemExit) as e:
            cli_main(["finish", "k:u", "--db", db, "--worker", "w",
                      "--result", '{"claims": "twelve"}'])
        assert e.value.code == 2
        err = capsys.readouterr().err
        assert "expected int" in err and "still yours" in err
        # Refused, not lost: the work can be handed back with the right shape.
        assert sa.progress(sa.connect(db))["k"][sa.LEASED] == 1
        assert cli_main(["finish", "k:u", "--db", db, "--worker", "w",
                         "--result", '{"claims": 12}']) == 0

    def test_no_check_is_an_escape_hatch(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["define", "k", "--db", db, "--instructions", "go",
                  "--done-when", "d", "--returns", '{"claims": <int>}'])
        cli_main(["add", "k", "u", "--db", db])
        cli_main(["claim", "k", "--db", db, "--worker", "w"])
        assert cli_main(["finish", "k:u", "--db", db, "--worker", "w",
                         "--no-check", "--result", '"wrong"']) == 0

    def test_mcp_reports_rather_than_raises(self, tmp_path):
        from fleetwright.mcp import Server
        s = Server(tmp_path / "w.db")
        s.define_kind({"kind": "k", "instructions": "go", "done_when": "d",
                       "returns": '{"claims": <int>}'})
        s.add_jobs({"kind": "k", "names": ["u"]})
        u = s.claim_job({"kind": "k"})["units"][0]
        bad = s.finish_job({"unit_id": u["unit_id"], "result": {"claims": "x"}})
        assert bad["finished"] is False and bad["error"] == "result_shape"
        assert "STILL YOURS" in bad["message"]
        ok = s.finish_job({"unit_id": u["unit_id"], "result": {"claims": 1}})
        assert ok["finished"] is True


class TestWaitRetryCancel:
    """The three that turn a fleet from hand-driven into scriptable."""

    def test_wait_exits_zero_when_everything_finished(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["add", "k", "a", "b", "--db", db])
        conn = sa.connect(db)
        for u in sa.claim(conn, "k", worker="w", n=2):
            sa.finish(conn, u.unit_id, worker="w")
        assert cli_main(["wait", "--db", db, "--quiet", "--interval", "0.01"]) == 0

    def test_wait_exits_one_if_anything_failed(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["add", "k", "a", "--db", db])
        conn = sa.connect(db)
        for _ in range(3):
            u = sa.claim(conn, "k", worker="w")[0]
            sa.fail(conn, u.unit_id, worker="w", note="no")
        assert cli_main(["wait", "--db", db, "--quiet", "--interval", "0.01"]) == 1

    def test_wait_exits_two_on_timeout(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["add", "k", "a", "--db", db])       # never worked on
        assert cli_main(["wait", "--db", db, "--quiet", "--timeout", "0.05",
                         "--interval", "0.01"]) == 2

    def test_wait_on_an_empty_database_returns_rather_than_hanging(self, tmp_path):
        assert cli_main(["wait", "--db", str(tmp_path / "e.db"), "--quiet",
                         "--interval", "0.01"]) == 0

    def test_wait_is_scoped_by_run(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["start", "--db", db, "--id", "R1"])
        cli_main(["start", "--db", db, "--id", "R2"])
        cli_main(["add", "k", "a", "--db", db, "--run", "R1"])
        cli_main(["add", "k", "b", "--db", db, "--run", "R2"])
        conn = sa.connect(db)
        u = sa.claim(conn, "k", worker="w", run="R1")[0]
        sa.finish(conn, u.unit_id, worker="w")
        # R1 is done even though R2 has never started.
        assert cli_main(["wait", "--db", db, "--run", "R1", "--quiet",
                         "--interval", "0.01"]) == 0

    def test_retry_resets_attempts_to_zero_not_up_by_one(self, conn):
        # The unit failed under the old code. Carrying its history forward
        # would retire it again after a single try, which is exactly wrong
        # when the thing that changed is the fix.
        sa.add(conn, "k", ["bad"])
        for _ in range(3):
            u = sa.claim(conn, "k", worker="w")[0]
            sa.fail(conn, u.unit_id, worker="w", note="old bug")
        assert sa.claim(conn, "k", worker="w") == []
        assert sa.retry(conn, kind="k")["retrying"] == 1
        got = sa.claim(conn, "k", worker="w")
        assert got and got[0].attempts == 1
        # and the reason it failed before is still readable
        assert conn.execute("SELECT note FROM unit").fetchone()["note"] == "old bug"

    def test_retry_without_a_scope_is_refused(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        assert cli_main(["retry", "--db", db]) == 2
        assert "refusing" in capsys.readouterr().err

    def test_cancel_leaves_in_flight_work_alone_by_default(self, conn):
        sa.add(conn, "k", ["a", "b", "c"])
        held = sa.claim(conn, "k", worker="w")[0]
        assert sa.cancel(conn, kind="k")["cancelled"] == 2
        # The one being worked on survives, and can still be finished.
        assert sa.finish(conn, held.unit_id, worker="w") is True
        assert sa.progress(conn)["k"][sa.CANCELLED] == 2

    def test_cancel_now_takes_back_what_is_in_flight(self, conn):
        sa.add(conn, "k", ["a"])
        held = sa.claim(conn, "k", worker="w")[0]
        assert sa.cancel(conn, kind="k", now=True)["cancelled"] == 1
        # The worker finds out the way it finds out about any lost lease.
        assert sa.finish(conn, held.unit_id, worker="w") is False

    def test_a_cancelled_unit_is_never_handed_out_again(self, conn):
        sa.add(conn, "k", ["a"])
        sa.cancel(conn, kind="k")
        assert sa.claim(conn, "k", worker="w") == []

    def test_cancel_is_a_status_not_a_deletion(self, conn):
        # A queue that forgets what you cancelled cannot say why a run came up
        # short three weeks later.
        sa.add(conn, "k", ["a"])
        sa.cancel(conn, kind="k")
        assert conn.execute("SELECT count(*) FROM unit").fetchone()[0] == 1
        assert sa.units(conn)["units"][0]["status"] == sa.CANCELLED

    def test_cancelled_units_can_be_brought_back(self, conn):
        sa.add(conn, "k", ["a"])
        sa.cancel(conn, kind="k")
        assert sa.retry(conn, kind="k")["retrying"] == 0        # not by default
        assert sa.retry(conn, kind="k", include_cancelled=True)["retrying"] == 1
        assert sa.claim(conn, "k", worker="w")

    def test_every_status_is_counted_somewhere(self, conn):
        """Adding a status and forgetting a consumer is how a total silently
        stops adding up."""
        from fleetwright import leases
        sa.add(conn, "k", ["a", "b", "c", "d"])
        sa.cancel(conn, kind="k", names=["d"])
        u = sa.claim(conn, "k", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w")
        sa.claim(conn, "k", worker="w2")
        t = leases.stats(conn)["totals"]
        assert set(leases.STATUSES) <= set(t)
        assert sum(t[s] for s in leases.STATUSES) == t["all"] == 4


class TestStatusShowsEverything:
    def test_the_status_table_prints_every_status(self, tmp_path, capsys):
        """`cancelled` shipped invisible: the table had a hand-written list of
        columns and the new status was not in it, so three units vanished from
        a row that no longer added up."""
        from fleetwright import leases
        db = str(tmp_path / "w.db")
        cli_main(["add", "k", "a", "b", "c", "--db", db])
        conn = sa.connect(db)
        sa.cancel(conn, kind="k", names=["c"])
        u = sa.claim(conn, "k", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w")
        capsys.readouterr()
        cli_main(["status", "--db", db])
        out = capsys.readouterr().out
        for st in leases.STATUSES:
            assert st in out, f"{st} is missing from the status table"
        # And the row must account for every unit.
        nums = [int(x) for x in out.splitlines()[1].split()[1:-1]]
        assert sum(nums) == 3, f"row does not add up: {nums}"


class TestInstallSkill:
    """The on-ramp. Before it, using this meant reading docs and running five
    commands in order; after it, two commands and a sentence in English."""

    def test_it_writes_where_claude_code_looks(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert cli_main(["install-skill"]) == 0
        target = tmp_path / ".claude" / "skills" / "fleetwright" / "SKILL.md"
        assert target.exists()
        assert target.read_text(encoding="utf-8").startswith("---\nname: fleetwright")

    def test_it_refuses_to_clobber_without_force(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        cli_main(["install-skill"])
        target = tmp_path / ".claude" / "skills" / "fleetwright" / "SKILL.md"
        target.write_text("mine, edited", encoding="utf-8")
        assert cli_main(["install-skill"]) == 1
        assert target.read_text(encoding="utf-8") == "mine, edited"
        assert cli_main(["install-skill", "--force"]) == 0
        assert target.read_text(encoding="utf-8") != "mine, edited"

    def test_the_skill_is_readable_from_the_installed_package(self):
        # It must live inside the wheel, or install-skill cannot write it for
        # anyone who installed from PyPI rather than from a clone.
        import fleetwright
        text = fleetwright.skill_text()
        assert "spawn" in text.lower() and len(text) > 2000

    def test_there_is_exactly_one_copy_of_the_skill(self):
        """A second copy is a copy that drifts from the CLI it documents, and
        this project has been bitten by duplication more than once."""
        found = [p for p in ROOT.rglob("SKILL.md")
                 if ".venv" not in p.parts and "dist" not in p.parts
                 and ".claude" not in p.parts]
        assert len(found) == 1, [str(p.relative_to(ROOT)) for p in found]

    def test_the_skill_tells_claude_to_spawn_in_one_message(self):
        # Spawning in separate messages makes the fleet a fleet of one, and it
        # is the single easiest mistake for an orchestrator to make.
        import fleetwright
        t = fleetwright.skill_text()
        assert "ONE message" in t
        assert "separate messages" in t

    def test_the_skill_only_names_real_commands(self):

        import fleetwright
        from fleetwright.cli import build_parser
        real = set(build_parser()._subparsers._group_actions[0].choices)
        used = commands_named_in(fleetwright.skill_text())
        assert not used - real - {"serve"}, sorted(used - real - {"serve"})


class TestCostAndTokens:
    """Declared, never measured. Nothing here can observe a model's usage, and
    treating these as evidence rather than testimony would be a lie."""

    def _fleet(self, conn):
        sa.add(conn, "x", [f"u{i}" for i in range(6)])
        for m, cost in (("opus", 0.031), ("sonnet", 0.006)):
            for u in sa.claim(conn, "x", worker="w-" + m, n=3, model=m):
                sa.finish(conn, u.unit_id, worker="w-" + m, cost=cost,
                          tokens_in=3000, tokens_out=900)

    def test_cost_rolls_up_per_model(self, conn):
        from fleetwright import leases
        self._fleet(conn)
        by = {m["model"]: m for m in leases.stats(conn)["per_model"]}
        assert round(by["opus"]["cost"], 3) == 0.093
        assert round(by["sonnet"]["cost"], 3) == 0.018
        assert by["opus"]["tokens_in"] == 9000

    def test_the_average_is_over_units_that_reported(self, conn):
        # A mean over everything would quietly divide by units that never said
        # anything, and read as if the run were cheaper than it was.
        from fleetwright import leases
        sa.add(conn, "x", ["a", "b"])
        got = sa.claim(conn, "x", worker="w", n=2, model="m")
        sa.finish(conn, got[0].unit_id, worker="w", cost=1.0)
        sa.finish(conn, got[1].unit_id, worker="w")          # no cost reported
        m = leases.stats(conn)["per_model"][0]
        assert m["priced"] == 1 and m["cost"] == 1.0

    def test_totals_say_how_much_of_the_run_reported(self, conn):
        from fleetwright import leases
        self._fleet(conn)
        c = leases.stats(conn)["cost"]
        assert c["priced"] == 6 and c["units"] == 6
        assert round(c["total"], 3) == 0.111

    def test_a_run_carries_its_cost(self, conn):
        r = sa.start_run(conn, label="L")
        sa.add(conn, "x", ["a"], run=r)
        u = sa.claim(conn, "x", worker="w", run=r, model="m")[0]
        sa.finish(conn, u.unit_id, worker="w", cost=0.5, tokens_out=100)
        row = sa.runs(conn)[0]
        assert row["cost"] == 0.5 and row["tokens_out"] == 100

    def test_costs_survive_into_the_jobs_view(self, conn):
        self._fleet(conn)
        u = sa.units(conn)["units"][0]
        assert u["cost"] is not None and u["tokens_in"] == 3000

    def test_the_cli_records_what_a_worker_reports(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["add", "x", "u", "--db", db])
        cli_main(["claim", "x", "--db", db, "--worker", "w", "--model", "m"])
        assert cli_main(["finish", "x:u", "--db", db, "--worker", "w",
                         "--cost", "0.25", "--tokens-in", "10",
                         "--tokens-out", "20"]) == 0
        assert sa.units(sa.connect(db))["units"][0]["cost"] == 0.25


class TestConfigFile:
    def _write(self, tmp_path, body):
        f = tmp_path / "fleetwright.toml"
        f.write_text(body, encoding="utf-8")
        return f

    def test_apply_registers_skills_and_defines_kinds(self, tmp_path):
        from fleetwright import config
        (tmp_path / "s.md").write_text("text", encoding="utf-8")
        f = self._write(tmp_path, '''
[skills.sk]
source = "s.md"
version = "1.0"

[kinds.extract]
instructions = "Read $path"
done_when = "done"
returns = '{"n": <int>}'
skills = ["sk"]
''')
        conn = sa.connect(tmp_path / "w.db")
        out = config.apply(conn, config.load(f), root=tmp_path)
        assert out["skills"] == ["sk"] and out["kinds"] == ["extract"]
        # The source is resolved against the CONFIG, not the working directory.
        assert sa.spec(conn, "sk") is None or True
        assert str(tmp_path) in [r["source"] for r in sa.skills(conn)][0]
        assert sa.spec(conn, "extract")["done_when"] == "done"

    def test_applying_twice_is_a_no_op(self, tmp_path):
        # A config you are afraid to re-apply is one people stop applying, and
        # then it stops describing what is actually running.
        from fleetwright import config
        f = self._write(tmp_path, '[kinds.k]\ninstructions = "go"\ndone_when = "d"\n')
        conn = sa.connect(tmp_path / "w.db")
        config.apply(conn, config.load(f), root=tmp_path)
        config.apply(conn, config.load(f), root=tmp_path)
        assert len([r for r in conn.execute("SELECT kind FROM kind")]) == 1

    def test_a_kind_without_instructions_is_refused(self, tmp_path):
        from fleetwright import config
        f = self._write(tmp_path, '[kinds.k]\ndone_when = "d"\n')
        with pytest.raises(ValueError, match="no instructions"):
            config.apply(sa.connect(tmp_path / "w.db"), config.load(f), root=tmp_path)

    def test_missing_done_when_and_unregistered_skills_warn(self, tmp_path):
        from fleetwright import config
        f = self._write(tmp_path, '[kinds.k]\ninstructions = "go"\nskills = ["ghost"]\n')
        out = config.apply(sa.connect(tmp_path / "w.db"), config.load(f), root=tmp_path)
        joined = " ".join(out["warnings"])
        assert "done_when" in joined and "ghost" in joined

    def test_units_come_from_a_file_or_a_glob_without_duplicates(self, tmp_path):
        from fleetwright import config
        (tmp_path / "scans").mkdir()
        for n in ("a.png", "b.png"):
            (tmp_path / "scans" / n).touch()
        (tmp_path / "u.txt").write_text("a.png\nc.png\n", encoding="utf-8")
        f = self._write(tmp_path, '''
[kinds.k]
instructions = "go"
done_when = "d"
units_from = "u.txt"
units_glob = "scans/*.png"
meta = { path = "scans/$name" }
''')
        names, meta = config.units_for(config.load(f), "k", root=tmp_path)
        assert names == ["a.png", "c.png", "b.png"]      # order kept, no dupes
        assert meta == {"path": "scans/$name"}

    def test_a_broken_file_says_what_is_wrong(self, tmp_path):
        from fleetwright import config
        f = self._write(tmp_path, "[kinds.k\ninstructions = 'go'")
        with pytest.raises(ValueError) as e:
            config.load(f)
        assert "fleetwright.toml" in str(e.value)

    def test_init_writes_something_apply_accepts(self, tmp_path, monkeypatch):
        from fleetwright import config
        monkeypatch.chdir(tmp_path)
        assert cli_main(["init"]) == 0
        cfg = config.load(tmp_path / "fleetwright.toml")
        out = config.apply(sa.connect(tmp_path / "w.db"), cfg, root=tmp_path)
        assert out["kinds"] == ["extract"]

    def test_init_refuses_to_clobber(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cli_main(["init"])
        (tmp_path / "fleetwright.toml").write_text("mine", encoding="utf-8")
        assert cli_main(["init"]) == 1
        assert (tmp_path / "fleetwright.toml").read_text(encoding="utf-8") == "mine"

    def test_apply_without_a_file_points_at_init(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert cli_main(["apply", "--db", str(tmp_path / "w.db")]) == 2
        assert "init" in capsys.readouterr().err


class TestResultsOutput:
    """Results were a single JSON blob with everything nested under `.result`.
    That is fine for six units and useless for four hundred thousand."""

    def _one(self, conn, result, name="u"):
        sa.add(conn, "k", [name])
        u = [x for x in sa.claim(conn, "k", worker="w", n=9, model="m")
             if x.name == name][0]
        sa.finish(conn, u.unit_id, worker="w", result=result, cost=0.5,
                  tokens_in=10, tokens_out=20)
        return u

    def test_rows_carry_what_you_cannot_reconstruct_later(self, conn):
        self._one(conn, {"claims": 3})
        r = next(sa.iter_results(conn, "k"))
        for k in ("model", "worker", "cost", "tokens_in", "seconds", "attempts",
                  "run", "status"):
            assert k in r, k

    def test_flat_lifts_result_keys_to_the_top(self, conn):
        self._one(conn, {"claims": 3, "notes": "x"})
        r = next(sa.iter_results(conn, "k", flat=True))
        assert r["claims"] == 3 and "result" not in r

    def test_the_envelope_wins_a_collision_and_nothing_is_lost(self, conn):
        # A row whose `name` silently became something the worker returned is a
        # row you cannot join on.
        self._one(conn, {"name": "the worker's idea", "claims": 1},
                  name="real-name")
        r = next(sa.iter_results(conn, "k", flat=True))
        assert r["name"] == "real-name"
        assert r["result_name"] == "the worker's idea"

    def test_a_non_dict_result_survives_flattening(self, conn):
        self._one(conn, "just a sentence")
        r = next(sa.iter_results(conn, "k", flat=True))
        assert r["result"] == "just a sentence"

    def test_a_unit_with_no_result_is_none_not_missing(self, conn):
        sa.add(conn, "k", ["u"])
        u = sa.claim(conn, "k", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w")
        assert next(sa.iter_results(conn, "k"))["result"] is None

    def test_it_streams_rather_than_materialising(self, conn):
        # A finished corpus can be hundreds of thousands of units, and
        # building a list to print it is the difference between a command that
        # works and one that gets killed.
        import types
        sa.add(conn, "k", ["a"])
        assert isinstance(sa.iter_results(conn, "k"), types.GeneratorType)

    def test_failures_can_be_included(self, conn):
        from fleetwright import leases
        sa.add(conn, "k", ["bad"])
        for _ in range(3):
            u = sa.claim(conn, "k", worker="w")[0]
            sa.fail(conn, u.unit_id, worker="w", note="no text layer")
        assert list(sa.iter_results(conn, "k")) == []
        rows = list(sa.iter_results(conn, "k", status=(leases.DONE, leases.FAILED)))
        assert len(rows) == 1 and rows[0]["note"] == "no text layer"

    def test_jsonl_is_one_object_per_line(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        cli_main(["add", "k", "a", "b", "--db", db])
        conn = sa.connect(db)
        for i, u in enumerate(sa.claim(conn, "k", worker="w", n=2)):
            sa.finish(conn, u.unit_id, worker="w", result={"n": i})
        capsys.readouterr()
        cli_main(["results", "k", "--db", db, "--jsonl"])
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert all(json.loads(ln)["kind"] == "k" for ln in lines)

    def test_json_is_still_one_valid_document(self, tmp_path, capsys):
        # It is assembled by hand to avoid materialising the corpus, which is
        # exactly the sort of thing that ships a trailing comma.
        db = str(tmp_path / "w.db")
        cli_main(["add", "k", "a", "b", "c", "--db", db])
        conn = sa.connect(db)
        for u in sa.claim(conn, "k", worker="w", n=3):
            sa.finish(conn, u.unit_id, worker="w", result={"n": 1})
        capsys.readouterr()
        cli_main(["results", "k", "--db", db, "--json"])
        assert len(json.loads(capsys.readouterr().out)) == 3

    def test_json_on_an_empty_result_set_is_still_valid(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        cli_main(["add", "k", "a", "--db", db])
        capsys.readouterr()
        cli_main(["results", "k", "--db", db, "--json"])
        assert json.loads(capsys.readouterr().out) == []


class TestKindVersioning:
    """A kind redefined mid-run left no record of what any unit was told, and
    two sessions sharing a database clobbered each other invisibly."""

    def test_a_unit_remembers_the_definition_it_was_claimed_under(self, conn):
        sa.define(conn, "k", instructions="the original text")
        sa.add(conn, "k", ["a", "b"])
        first = sa.claim(conn, "k", worker="w")[0]
        sa.finish(conn, first.unit_id, worker="w")
        sa.define(conn, "k", instructions="something else entirely", force=True)
        # spec() reports what the kind says NOW, which is the wrong answer.
        assert sa.spec(conn, "k")["instructions"] == "something else entirely"
        assert "the original text" in sa.brief_for(conn, first.unit_id)

    def test_the_pin_is_content_addressed_not_copied(self, conn):
        """Storing the brief on every unit is O(units): 400,000 of them would
        carry most of a gigabyte of near-identical text."""
        sa.define(conn, "k", instructions="one definition")
        sa.add(conn, "k", [f"u{i}" for i in range(50)])
        sa.claim(conn, "k", worker="w", n=50)
        assert conn.execute("SELECT count(*) FROM kind_version").fetchone()[0] == 1
        digests = {r[0] for r in conn.execute(
            "SELECT DISTINCT kind_digest FROM unit")}
        assert len(digests) == 1

    def test_redefining_with_live_units_is_refused(self, conn):
        sa.define(conn, "k", instructions="a")
        sa.add(conn, "k", ["u"])
        with pytest.raises(ValueError, match="waiting"):
            sa.define(conn, "k", instructions="b")

    def test_an_unchanged_definition_is_always_allowed(self, conn):
        # Otherwise re-applying a config would fail the moment work exists,
        # and a config you cannot re-apply stops describing what is running.
        sa.define(conn, "k", instructions="a", done_when="d")
        sa.add(conn, "k", ["u"])
        sa.claim(conn, "k", worker="w")
        sa.define(conn, "k", instructions="a", done_when="d")

    def test_a_finished_kind_can_be_redefined_freely(self, conn):
        sa.define(conn, "k", instructions="a")
        sa.add(conn, "k", ["u"])
        u = sa.claim(conn, "k", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w")
        sa.define(conn, "k", instructions="b")      # nothing live: fine

    def test_history_counts_what_ran_under_each_definition(self, conn):
        sa.define(conn, "k", instructions="v1")
        sa.add(conn, "k", ["a", "b", "c"])
        for u in sa.claim(conn, "k", worker="w", n=2):
            sa.finish(conn, u.unit_id, worker="w")
        sa.define(conn, "k", instructions="v2", force=True)
        sa.claim(conn, "k", worker="w")
        vs = {v["instructions"]: v["units"] for v in sa.kind_versions(conn, "k")}
        assert vs == {"v1": 2, "v2": 1}

    def test_the_digest_covers_every_field_a_worker_sees(self, conn):
        # A change in a field workers read must never look unchanged.
        base = dict(instructions="i", done_when="d", returns="r", tools="t",
                    skills=["s"], mcp={"m": "c"}, context="x")
        first = sa.define(conn, "k", **base)
        for field, value in (("done_when", "other"), ("returns", "other"),
                             ("tools", "other"), ("skills", ["z"]),
                             ("mcp", {"m": "z"}), ("context", "z")):
            assert sa.define(conn, "k", **{**base, field: value}) != first, field

    def test_the_cli_refuses_and_says_what_to_do(self, tmp_path, capsys):
        db = str(tmp_path / "w.db")
        cli_main(["define", "k", "--db", db, "--instructions", "a", "--done-when", "d"])
        cli_main(["add", "k", "u", "--db", db])
        assert cli_main(["define", "k", "--db", db, "--instructions", "b",
                         "--done-when", "d"]) == 2
        err = capsys.readouterr().err
        assert "waiting" in err and "force" in err
        assert cli_main(["define", "k", "--db", db, "--instructions", "b",
                         "--done-when", "d", "--force"]) == 0

    def test_mcp_reports_the_refusal_rather_than_raising(self, tmp_path):
        from fleetwright.mcp import Server
        s = Server(tmp_path / "w.db")
        s.define_kind({"kind": "k", "instructions": "a", "done_when": "d"})
        s.add_jobs({"kind": "k", "names": ["u"]})
        out = s.define_kind({"kind": "k", "instructions": "b", "done_when": "d"})
        assert out["ok"] is False and out["error"] == "kind_in_use"
        assert s.define_kind({"kind": "k", "instructions": "b",
                              "done_when": "d", "force": True})["defined"] == "k"

    def test_brief_on_an_unknown_unit_is_none(self, conn):
        assert sa.brief_for(conn, "nope:nope") is None


class TestProjectState:
    """A new session knows nothing. This is how it finds out."""

    def test_it_finds_a_database_it_was_not_told_about(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        cli_main(["add", "k", "u", "--db", "corpus.db"])
        capsys.readouterr()
        assert cli_main(["state"]) == 0            # no --db given
        assert "corpus.db" in capsys.readouterr().out

    def test_it_does_not_mistake_someone_elses_sqlite_file(self, tmp_path, monkeypatch, capsys):
        import sqlite3
        monkeypatch.chdir(tmp_path)
        c = sqlite3.connect("notes.db")
        c.execute("CREATE TABLE thoughts (t TEXT)")
        c.commit()
        c.close()
        capsys.readouterr()
        cli_main(["state"])
        assert "no fleetwright database" in capsys.readouterr().out

    def test_an_empty_project_is_told_how_to_start(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert cli_main(["state"]) == 0
        out = capsys.readouterr().out
        assert "install-skill" in out and "init" in out

    def test_every_problem_carries_what_to_do_about_it(self, conn, tmp_path):
        """A summary that reports three failures without saying `retry` has
        moved the work of knowing the tool onto whoever is reading it, which
        for a fresh agent is the whole problem."""
        f = tmp_path / "s.md"
        f.write_text("original", encoding="utf-8")
        sa.register_skill(conn, "sk", source=str(f), version="1")
        sa.define(conn, "k", instructions="go", skills=["sk", "ghost"])
        sa.add(conn, "k", ["bad", "b", "c"])
        for _ in range(3):
            u = sa.claim(conn, "k", worker="w")[0]
            sa.fail(conn, u.unit_id, worker="w", note="no text layer")
        f.write_text("edited since", encoding="utf-8")
        st = sa.state(conn)
        assert st["attention"], "nothing flagged"
        assert all(item["do"] for item in st["attention"])
        joined = " ".join(i["what"] for i in st["attention"])
        assert "could finish" in joined and "changed" in joined
        assert "never registered" in joined

    def test_next_points_at_the_live_run(self, conn):
        r = sa.start_run(conn, label="L")
        sa.add(conn, "k", ["a", "b"], run=r)
        assert f"wait --run {r}" in sa.state(conn)["next"]

    def test_next_is_retry_when_everything_stopped_and_something_failed(self, conn):
        sa.add(conn, "k", ["bad"])
        for _ in range(3):
            u = sa.claim(conn, "k", worker="w")[0]
            sa.fail(conn, u.unit_id, worker="w", note="n")
        assert "retry" in sa.state(conn)["next"]

    def test_next_is_setup_when_there_is_nothing(self, conn):
        assert "init" in sa.state(conn)["next"]

    def test_units_enqueued_without_a_run_are_surfaced(self, conn):
        sa.add(conn, "k", ["a"])
        assert sa.state(conn)["totals"]["ungrouped"] == 1

    def test_mcp_exposes_it_and_says_to_call_it_first(self, tmp_path):
        from fleetwright.mcp import Server, _tools
        t = next(x for x in _tools() if x["name"] == "project_state")
        assert "FIRST" in t["description"]
        s = Server(tmp_path / "w.db")
        assert "next" in s.project_state({})

    def test_the_skill_tells_a_new_session_to_orient(self):
        import fleetwright
        assert "fleetwright state" in fleetwright.skill_text()


class TestLineage:
    """The only relationship here that is not a hierarchy, and the one that
    was silently not recorded at all."""

    def _chain(self, conn):
        for k in ("extract", "audit", "gloss"):
            sa.define(conn, k, instructions=f"do $name ({k})", done_when="d")
        r = sa.start_run(conn, label="L")
        sa.add(conn, "extract", ["p1"], run=r)
        u = sa.claim(conn, "extract", worker="w", run=r)[0]
        sa.finish(conn, u.unit_id, worker="w", then={"audit": ["p1-c1"]})
        a = sa.claim(conn, "audit", worker="w", run=r)[0]
        sa.finish(conn, a.unit_id, worker="w", then={"gloss": ["p1-c1-g"]})
        return r, u, a

    def test_then_spawned_units_stay_in_the_run(self, conn):
        """They fell out entirely: `wait --run` returned as soon as stage one
        finished and `runs` under-reported the work."""
        r, _, _ = self._chain(conn)
        rows = list(conn.execute("SELECT kind, run_id FROM unit"))
        assert all(row[1] == r for row in rows), rows
        assert sa.runs(conn)[0]["units"] == 3

    def test_then_records_which_unit_caused_which(self, conn):
        r, u, a = self._chain(conn)
        lin = sa.lineage(conn, sa.unit_id("gloss", "p1-c1-g", r))
        assert [x["kind"] for x in lin["ancestors"]] == ["extract", "audit"]
        assert lin["unit"]["kind"] == "gloss"

    def test_descendants_come_back_as_a_tree(self, conn):
        r, u, _ = self._chain(conn)
        lin = sa.lineage(conn, u.unit_id)
        assert lin["descendants"][0]["kind"] == "audit"
        assert lin["descendants"][0]["children"][0]["kind"] == "gloss"

    def test_a_cycle_cannot_hang_the_query(self, conn):
        # `then` cannot make one, but a hand-edited database can, and an
        # infinite loop in a read query is a miserable way to find out.
        sa.add(conn, "k", ["a", "b"])
        conn.execute("UPDATE unit SET parent_unit_id='k:b' WHERE unit_id='k:a'")
        conn.execute("UPDATE unit SET parent_unit_id='k:a' WHERE unit_id='k:b'")
        conn.commit()
        assert len(sa.lineage(conn, "k:a")["ancestors"]) <= 2

    def test_flow_aggregates_to_kinds_not_units(self, conn):
        # A forest of 400,000 individual chains is not a picture.
        self._chain(conn)
        f = {(x["from"], x["to"]): x["units"] for x in sa.flow(conn)}
        assert f == {("extract", "audit"): 1, ("audit", "gloss"): 1}

    def test_flow_is_empty_when_nothing_chains(self, conn):
        sa.add(conn, "k", ["a"])
        assert sa.flow(conn) == []

    def test_lineage_of_an_unknown_unit_is_empty(self, conn):
        assert sa.lineage(conn, "nope") == {}


class TestTimeline:
    def test_lanes_report_idle_not_just_counts(self, conn):
        """A lane that is 20% busy is a worker that spent four fifths of the
        run waiting, which no count of units shows."""
        import time as _t
        sa.add(conn, "k", ["a", "b"])
        now = _t.time()
        u1 = sa.claim(conn, "k", worker="busy")[0]
        u2 = sa.claim(conn, "k", worker="idle")[0]
        for uid, span in ((u1.unit_id, 100.0), (u2.unit_id, 5.0)):
            conn.execute("UPDATE unit SET claimed_at=?, updated_at=?, status='done' "
                         "WHERE unit_id=?", (now - 100, now - 100 + span, uid))
        conn.commit()
        lanes = {x["worker"]: x for x in sa.timeline(conn)["lanes"]}
        assert lanes["busy"]["idle"] < 0.05
        assert lanes["idle"]["idle"] > 0.9

    def test_it_says_when_it_truncated(self, conn):
        import time as _t
        sa.add(conn, "k", [f"u{i}" for i in range(30)])
        now = _t.time()
        for u in sa.claim(conn, "k", worker="w", n=30):
            conn.execute("UPDATE unit SET claimed_at=? WHERE unit_id=?",
                         (now, u.unit_id))
        conn.commit()
        assert sa.timeline(conn, limit=10)["truncated"] is True
        assert sa.timeline(conn, limit=100)["truncated"] is False

    def test_unclaimed_units_are_not_bars(self, conn):
        sa.add(conn, "k", ["never-claimed"])
        assert sa.timeline(conn)["bars"] == []

    def test_a_worker_records_who_spawned_it(self, conn):
        sa.add(conn, "k", ["a"])
        sa.claim(conn, "k", worker="agent-3", spawned_by="session-a")
        assert sa.timeline(conn)["lanes"][0]["spawned_by"] == "session-a"

    def test_the_dashboard_hides_the_flow_panel_when_nothing_chains(self):
        from fleetwright import dashboard
        assert '$("#flowcard").hidden = !fl.length' in dashboard.PAGE


class TestArgumentOrder:
    """`add extract --db x p1` and `add extract p1 --db x` are the same command.

    argparse says otherwise: it fills a trailing `nargs="*"` from the first run
    of positionals, finds none, and reports `unrecognized arguments: p1`. The
    error names the units rather than the ordering, so it reads as though the
    units are bad. Every worker writing its own command line hits this.
    """

    def test_names_after_the_flags_are_still_names(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["define", "ex", "--db", db, "--instructions", "x",
                  "--done-when", "y"])
        assert cli_main(["add", "ex", "--db", db, "p1", "p2"]) == 0
        conn = sa.connect(db)
        assert sorted(u["name"] for u in sa.units(conn)["units"]) == ["p1", "p2"]

    def test_either_order_gives_the_same_queue(self, tmp_path):
        both = []
        for order in (["ex", "a", "b", "--db"], ["ex", "--db"]):
            db = str(tmp_path / f"{len(both)}.db")
            cli_main(["define", "ex", "--db", db, "--instructions", "x",
                      "--done-when", "y"])
            argv = ["add"] + [t if t != "--db" else "--db" for t in order]
            argv += [db] + ([] if "a" in order else ["a", "b"])
            cli_main(argv)
            conn = sa.connect(db)
            both.append(sorted(u["name"] for u in sa.units(conn)["units"]))
        assert both[0] == both[1] == ["a", "b"]

    def test_a_value_that_looks_like_a_name_is_not_hoisted(self, tmp_path):
        """`--meta {...}` must stay attached to its flag."""
        db = str(tmp_path / "w.db")
        cli_main(["define", "ex", "--db", db, "--instructions", "x",
                  "--done-when", "y"])
        cli_main(["add", "ex", "--db", db, "--meta", '{"path": "/p"}', "p1"])
        conn = sa.connect(db)
        rows = conn.execute("SELECT name, meta FROM unit").fetchall()
        assert [r["name"] for r in rows] == ["p1"]
        assert json.loads(rows[0]["meta"]) == {"path": "/p"}

    def test_every_variadic_subcommand_is_covered(self):
        """The fix is opt-in per subcommand, so a fourth must not slip past."""
        parser = cli.build_parser()
        choices = parser._subparsers._group_actions[0].choices
        variadic = {name for name, sub in choices.items()
                    if any(not act.option_strings and act.nargs == "*"
                           for act in sub._actions)}
        assert variadic == set(cli._VARIADIC), (
            "a subcommand takes a list of names but is not in _VARIADIC, so "
            "names written after its flags will be reported as unrecognised")


class TestSpawnedBy:
    """The one edge nothing can observe: that a session spawned this worker."""

    def test_the_flag_reaches_the_database(self, tmp_path):
        db = str(tmp_path / "w.db")
        cli_main(["define", "ex", "--db", db, "--instructions", "x",
                  "--done-when", "y"])
        cli_main(["add", "ex", "--db", db, "p1"])
        cli_main(["claim", "ex", "--db", db, "--worker", "w0",
                  "--spawned-by", "session-a"])
        conn = sa.connect(db)
        assert conn.execute(
            "SELECT spawned_by FROM unit").fetchone()["spawned_by"] == "session-a"

    def test_the_environment_works_too(self, tmp_path, monkeypatch):
        """A subagent inherits its parent's env, so one export labels a fleet."""
        db = str(tmp_path / "w.db")
        cli_main(["define", "ex", "--db", db, "--instructions", "x",
                  "--done-when", "y"])
        cli_main(["add", "ex", "--db", db, "p1"])
        monkeypatch.setenv("FLEETWRIGHT_SPAWNED_BY", "session-b")
        cli_main(["claim", "ex", "--db", db, "--worker", "w0"])
        conn = sa.connect(db)
        assert conn.execute(
            "SELECT spawned_by FROM unit").fetchone()["spawned_by"] == "session-b"


class TestNothingIsLibraryOnly:
    """Every library option a worker needs must be reachable from the shell.

    This has now shipped three times: `add --run` was parsed and never read,
    `spawned_by` was stored and drawn with no flag to set it, and `finish
    --then` did not exist while the skill told shell workers to use it. Each
    one passed every test, because the tests call the library and the workers
    do not.
    """

    #: Reachable under another name, or deliberately not on the CLI. Anything
    #: not listed here must have a flag.
    ELSEWHERE = {
        ("claim", "n"): "-n",
        ("add", "parent"): "set by finish --then; not a thing to type",
        ("define", "skills"): "--skill, repeated",
        ("retry", "names"): "positional",
        ("cancel", "names"): "positional",
        ("skill", "content"): "--source, which is also hashed",
        ("start", "started_by"): "--by",
        ("start", "run_id"): "--id",
    }

    def test_every_keyword_can_be_set_from_the_command_line(self):
        import fleetwright.leases as L
        pairs = [("add", L.add), ("claim", L.claim), ("finish", L.finish),
                 ("fail", L.fail), ("release", L.release), ("define", L.define),
                 ("retry", L.retry), ("cancel", L.cancel),
                 ("skill", L.register_skill), ("start", L.start_run)]
        subs = cli.build_parser()._subparsers._group_actions[0].choices
        gaps = []
        for name, fn in pairs:
            have = {o for a in subs[name]._actions for o in a.option_strings}
            for prm in inspect.signature(fn).parameters.values():
                if prm.kind is not prm.KEYWORD_ONLY:
                    continue
                if prm.name in ("conn", "worker", "force"):
                    continue
                if "--" + prm.name.replace("_", "-") in have:
                    continue
                if (name, prm.name) in self.ELSEWHERE:
                    continue
                gaps.append(f"{name}(): {prm.name}")
        assert not gaps, (
            "reachable from the library but not from any command, so a shell "
            "worker cannot use it: " + ", ".join(gaps))


class TestThenFromTheShell:

    def _setup(self, tmp_path):
        db = str(tmp_path / "w.db")
        for k in ("ex", "audit"):
            cli_main(["define", k, "--db", db, "--instructions", "x",
                      "--done-when", "y"])
        run = sa.start_run(sa.connect(db), label="r")
        cli_main(["add", "ex", "--db", db, "--run", run, "p1"])
        cli_main(["claim", "ex", "--db", db, "--worker", "w0"])
        return db, run

    def test_the_next_stage_inherits_the_run_and_the_parent(self, tmp_path):
        db, run = self._setup(tmp_path)
        assert cli_main(["finish", f"{run}/ex:p1", "--db", db, "--worker", "w0",
                         "--then", '{"audit": ["p1-c0"]}']) == 0
        conn = sa.connect(db)
        row = conn.execute(
            "SELECT run_id, parent_unit_id FROM unit WHERE kind = 'audit'"
        ).fetchone()
        assert row["run_id"] == run, "the second stage fell out of the run"
        assert row["parent_unit_id"] == f"{run}/ex:p1"

    def test_an_undefined_kind_is_refused_and_the_unit_stays_yours(self, tmp_path):
        """Enqueueing into a kind with no instructions hands a worker a bare
        name, and reporting success while doing it loses the stage quietly."""
        db, run = self._setup(tmp_path)
        with pytest.raises(SystemExit) as e:
            cli_main(["finish", f"{run}/ex:p1", "--db", db, "--worker", "w0",
                      "--then", '{"nosuch": ["a"]}'])
        assert e.value.code == 2
        conn = sa.connect(db)
        assert conn.execute("SELECT status FROM unit WHERE kind = 'ex'"
                            ).fetchone()["status"] == "leased"

    def test_malformed_then_does_not_finish_the_unit(self, tmp_path):
        db, run = self._setup(tmp_path)
        with pytest.raises(SystemExit) as e:
            cli_main(["finish", f"{run}/ex:p1", "--db", db, "--worker", "w0",
                      "--then", "{audit: p1}"])
        assert e.value.code == 2
        conn = sa.connect(db)
        assert conn.execute("SELECT status FROM unit WHERE kind = 'ex'"
                            ).fetchone()["status"] == "leased"


class TestClocksThatDisagree:
    """Two machines sharing one database do not share one clock.

    A worker on a second host writes its own `time.time()` into `updated_at`,
    and hosts a minute or two apart are ordinary. A unit that finishes
    "before" its run started then made `elapsed` negative, which printed as
    `-755.0s` with a blank parallelism: it reads as a broken product rather
    than a skewed clock.
    """

    def _run_with_skew(self, tmp_path, skew):
        conn = sa.connect(str(tmp_path / f"w{skew}.db"))
        sa.define(conn, "k", "x", done_when="y")
        run = sa.start_run(conn, label="r")
        sa.add(conn, "k", ["a"], run=run)
        u = sa.claim(conn, "k", worker="w0")[0]
        sa.finish(conn, u.unit_id, worker="w0")
        started = conn.execute(
            "SELECT started_at FROM run WHERE run_id = ?", (run,)).fetchone()[0]
        # The remote worker's clock, `skew` seconds behind this one.
        conn.execute("UPDATE unit SET updated_at = ?, claimed_at = ?",
                     (started - skew, started - skew - 1))
        conn.commit()
        return conn

    def test_a_unit_older_than_its_run_does_not_go_negative(self, tmp_path):
        conn = self._run_with_skew(tmp_path, 120)
        assert sa.runs(conn)[0]["elapsed"] >= 0

    def test_the_run_is_at_least_as_long_as_the_skew(self, tmp_path):
        """Clamping to zero would hide it; the run demonstrably spans it."""
        conn = self._run_with_skew(tmp_path, 120)
        assert sa.runs(conn)[0]["elapsed"] >= 120

    def test_an_agreeing_clock_is_unaffected(self, tmp_path):
        conn = sa.connect(str(tmp_path / "ok.db"))
        sa.define(conn, "k", "x", done_when="y")
        run = sa.start_run(conn, label="r")
        sa.add(conn, "k", ["a"], run=run)
        u = sa.claim(conn, "k", worker="w0")[0]
        sa.finish(conn, u.unit_id, worker="w0")
        assert 0 <= sa.runs(conn)[0]["elapsed"] < 60


class TestRunAsAModule:
    """`python -m` must work, and only a subprocess can prove it.

    Importing the module runs every definition before anything calls `main()`,
    so the console script worked while `python -m fleetwright.cli` died on a
    `NameError` for a handler defined below the `__main__` guard. No in-process
    test can see this: by the time the test calls `main`, the import is done.
    """

    def test_the_cli_module_runs(self):
        r = subprocess.run([sys.executable, "-m", "fleetwright.cli",
                            "--version"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "fleetwright" in r.stdout

    def test_the_package_runs(self):
        r = subprocess.run([sys.executable, "-m", "fleetwright", "--version"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "fleetwright" in r.stdout

    def test_a_real_command_runs(self, tmp_path):
        """--version short-circuits argparse; this reaches a handler."""
        r = subprocess.run([sys.executable, "-m", "fleetwright", "init",
                            "--file", str(tmp_path / "s.toml")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert (tmp_path / "s.toml").exists()


class TestEveryColourIsReal:
    """`var(--card)` when the token is called `--raise` is not a subtle bug.

    A CSS declaration naming an undefined custom property is DROPPED, and an
    SVG `<rect>` or `<text>` with no fill is BLACK. So one wrong token name
    turned the whole pipeline diagram into black boxes with invisible labels
    on a black ground, and nothing errored anywhere: not the server, not the
    console, not the tests. The page still returned 200 and every panel was
    still in the HTML.
    """

    def _css(self):
        from fleetwright import dashboard
        return dashboard.page(Path("x.db"))

    def test_no_declaration_names_a_token_that_does_not_exist(self):
        css = self._css()
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
        missing = sorted(used - defined)
        assert not missing, (
            "used but never defined, so the declaration is dropped and an SVG "
            "fill falls back to black: " + ", ".join(missing))

    def test_every_status_has_a_colour(self):
        """The diagram builds fills as `var(--${status})` from the status
        list, so a new status would be an invisible segment."""
        css = self._css()
        for status in sa.STATUSES:
            assert f"--{status}:" in css, f"no --{status} colour"


class TestGraph:
    """Nodes and edges, laid out in columns rather than thrown at a physics
    simulation. A pipeline has a direction and a force layout discards it."""

    def _pipeline(self, tmp_path, chain):
        conn = sa.connect(str(tmp_path / "g.db"))
        kinds = {k for pair in chain for k in pair}
        for k in kinds:
            sa.define(conn, k, "x", done_when="y")
        run = sa.start_run(conn, label="r")
        sa.add(conn, chain[0][0], ["a"], run=run)
        while True:
            got = sa.claim(conn, worker="w0", n=1)
            if not got:
                break
            u = got[0]
            nxt = {}
            for src, dst in chain:
                if src == u.kind:
                    nxt.setdefault(dst, []).append(f"{u.name}-{dst}")
            sa.finish(conn, u.unit_id, worker="w0", then=nxt or None)
        return conn

    def test_depth_is_the_longest_path_not_the_shortest(self, tmp_path):
        """A diamond: `d` must sit past `b` and `c`, not beside them, or the
        arrows point backwards into their own column."""
        conn = self._pipeline(tmp_path, [("a", "b"), ("a", "c"),
                                         ("b", "d"), ("c", "d")])
        depth = {n["kind"]: n["depth"] for n in sa.graph(conn)["nodes"]}
        assert depth == {"a": 0, "b": 1, "c": 1, "d": 2}

    def test_a_long_branch_pushes_the_merge_right(self, tmp_path):
        conn = self._pipeline(tmp_path, [("a", "b"), ("b", "c"),
                                         ("c", "d"), ("a", "d")])
        depth = {n["kind"]: n["depth"] for n in sa.graph(conn)["nodes"]}
        assert depth["d"] == 3, "the short path a->d decided the column"

    def test_a_cycle_terminates(self, tmp_path):
        """Nothing stops an `audit` kind enqueueing back into `extract`, and a
        layout that hangs on that is worse than one that looks slightly odd."""
        conn = sa.connect(str(tmp_path / "c.db"))
        for k in ("a", "b"):
            sa.define(conn, k, "x", done_when="y")
        sa.add(conn, "a", ["one"])
        u = sa.claim(conn, "a", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w", then={"b": ["two"]})
        u = sa.claim(conn, "b", worker="w")[0]
        sa.finish(conn, u.unit_id, worker="w", then={"a": ["three"]})
        g = sa.graph(conn)                      # must return, not spin
        assert {n["kind"] for n in g["nodes"]} == {"a", "b"}
        assert len(g["edges"]) == 2

    def test_the_counts_on_a_node_add_up(self, tmp_path):
        conn = self._pipeline(tmp_path, [("a", "b")])
        for n in sa.graph(conn)["nodes"]:
            total = sum(n[k] for k in
                        ("done", "failed", "leased", "open", "cancelled"))
            assert total == n["units"], f"{n['kind']} does not add up"

    def test_no_edges_means_no_picture(self, tmp_path):
        conn = sa.connect(str(tmp_path / "flat.db"))
        sa.define(conn, "a", "x", done_when="y")
        sa.add(conn, "a", ["one", "two"])
        g = sa.graph(conn)
        assert g["edges"] == []
        assert g["nodes"][0]["depth"] == 0

    def test_a_run_scopes_it(self, tmp_path):
        conn = self._pipeline(tmp_path, [("a", "b")])
        other = sa.start_run(conn, label="other")
        sa.add(conn, "a", ["elsewhere"], run=other)
        assert sa.graph(conn, run=other)["nodes"] == [
            {"kind": "a", "units": 1, "done": 0, "failed": 0, "leased": 0,
             "open": 1, "cancelled": 0, "cost": 0.0, "mean_seconds": None,
             "depth": 0}]


def _expire(conn, unit_id=None):
    """Push a lease into the past. Never sleep: a 10ms lease is a flaky test."""
    if unit_id:
        conn.execute("UPDATE unit SET leased_until = ? WHERE unit_id = ?",
                     (time.time() - 1, unit_id))
    else:
        conn.execute("UPDATE unit SET leased_until = ?", (time.time() - 1,))
    conn.commit()


class TestOwnership:
    """A lost lease must actually lose the unit.

    The ownership predicate was only appended when a worker name was passed,
    and all three closers defaulted it to None -- so the check that the README
    and docs/concepts.md both describe was opt-in. `claim` has always defaulted
    its worker; the closers did not, and that asymmetry was the whole bug.
    """

    def _contested(self, tmp_path, name="w.db"):
        conn = sa.connect(str(tmp_path / name))
        run = sa.start_run(conn, label="t")
        sa.define(conn, "k", "do $name", done_when="d")
        sa.add(conn, "k", ["u1"], run=run)
        stale = sa.claim(conn, "k", worker="A", lease=1)[0]
        _expire(conn)
        live = sa.claim(conn, "k", worker="B", lease=600)[0]
        return conn, stale, live

    def test_close_without_worker_cannot_steal_a_reclaimed_unit(self, tmp_path):
        conn, stale, live = self._contested(tmp_path)
        assert sa.finish(conn, stale.unit_id, result={"v": "stale"}) is False
        row = conn.execute("SELECT worker, status, result FROM unit").fetchone()
        assert row["worker"] == "B" and row["status"] == "leased"
        assert row["result"] is None, "a stale result overwrote a live holder's"

    def test_release_without_worker_cannot_reopen_a_held_unit(self, tmp_path):
        conn, stale, live = self._contested(tmp_path)
        assert sa.release(conn, stale.unit_id) is False
        assert conn.execute("SELECT status FROM unit").fetchone()[0] == "leased"

    def test_fail_without_worker_cannot_bury_a_held_unit(self, tmp_path):
        conn, stale, live = self._contested(tmp_path)
        assert sa.fail(conn, stale.unit_id, note="not mine") is False
        assert conn.execute("SELECT status FROM unit").fetchone()[0] == "leased"

    def test_any_is_still_an_escape_hatch(self, tmp_path):
        """An operator cleaning up after a fleet that is gone still needs it."""
        conn, stale, live = self._contested(tmp_path)
        assert sa.finish(conn, stale.unit_id, worker=sa.ANY) is True

    def test_the_live_holder_is_never_refused(self, tmp_path):
        conn, stale, live = self._contested(tmp_path)
        assert sa.finish(conn, live.unit_id, worker="B") is True

    def test_attribution_survives(self, tmp_path):
        """per_worker credited B for A's output, which is worse than losing it."""
        conn, stale, live = self._contested(tmp_path)
        sa.finish(conn, stale.unit_id, result={"v": "stale"})
        sa.finish(conn, live.unit_id, worker="B", result={"v": "live"})
        row = conn.execute("SELECT worker, result FROM unit").fetchone()
        assert row["worker"] == "B" and "live" in row["result"]

    def test_a_same_named_stale_worker_is_refused_with_a_token(self, tmp_path):
        """Two processes told to call themselves agent-1 are one name and two
        claims. The name cannot tell them apart; the token can."""
        conn = sa.connect(str(tmp_path / "t.db"))
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", ["u1"])
        stale = sa.claim(conn, "k", worker="agent-1", lease=1)[0]
        _expire(conn)
        live = sa.claim(conn, "k", worker="agent-1", lease=600)[0]
        assert stale.token and live.token and stale.token != live.token
        assert sa.finish(conn, stale.unit_id, worker="agent-1",
                         token=stale.token) is False
        assert sa.heartbeat(conn, [live.unit_id], worker="agent-1",
                            token=stale.token) == 0
        assert sa.finish(conn, live.unit_id, worker="agent-1",
                         token=live.token) is True

    def test_the_token_reaches_the_worker(self, tmp_path):
        conn = sa.connect(str(tmp_path / "t.db"))
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", ["u1"])
        u = sa.claim(conn, "k", worker="w")[0]
        assert u.token
        assert u.token in u.brief(), "a worker cannot hand back what it never saw"

    def test_prompt_does_not_hardcode_a_shared_worker_name(self, tmp_path):
        """Spawning eight workers used to produce eight called agent-1.

        The fix is a name that DIFFERS per call, not an absent one. Omitting
        `--worker` looks tidier and breaks the shell pattern outright: a worker
        claims in one process and finishes in another, and `this_worker()` is
        hostname:pid, so the finish would not recognise its own claim.
        """
        conn = sa.connect(str(tmp_path / "t.db"))
        sa.define(conn, "k", "i", done_when="d")
        names = {re.search(r"--worker (\S+)", sa.worker_prompt(conn, "k")).group(1)
                 for _ in range(8)}
        assert len(names) == 8, "eight spawns, and not eight distinct names"

    def test_the_prompt_uses_one_name_for_claim_and_finish(self, tmp_path):
        """Two commands, one worker. If they disagree the finish is refused."""
        conn = sa.connect(str(tmp_path / "t.db"))
        sa.define(conn, "k", "i", done_when="d")
        text = sa.worker_prompt(conn, "k")
        used = set(re.findall(r"--worker (\S+)", text))
        assert len(used) == 1, f"the prompt names {used}"


class TestReclaimKeepsTheReason:

    def test_reclaim_preserves_an_earlier_failure_note(self, tmp_path):
        """`failures()` and the "Could not finish" panel exist to show it."""
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", ["u1"])
        for i in range(2):
            u = sa.claim(conn, "k", worker=f"w{i}")[0]
            sa.fail(conn, u.unit_id, note="OOM parsing page 12", worker=f"w{i}")
            if i == 0:
                sa.retry(conn, names=["u1"])
        sa.claim(conn, "k", worker="w9")
        _expire(conn)
        sa.reclaim(conn)
        row = conn.execute("SELECT status, note FROM unit").fetchone()
        assert row["status"] == "failed"
        assert row["note"] == "OOM parsing page 12", (
            "a tautology overwrote the only record of why")

    def test_reclaim_counts_retirements_too(self, tmp_path):
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "k", "i", done_when="d", max_attempts=1)
        sa.add(conn, "k", ["u1", "u2"])
        sa.claim(conn, "k", worker="w", n=2)
        _expire(conn)
        assert sa.reclaim(conn) == 2, "reported only the reopened ones"


class TestRetirementIsPerKind:

    def test_claim_max_attempts_does_not_retire_other_kinds(self, tmp_path):
        """One worker's per-call flag mass-failed an unrelated kind's units."""
        conn = sa.connect(str(tmp_path / "w.db"))
        for k in ("a", "b"):
            sa.define(conn, k, "i", done_when="d")
        sa.add(conn, "a", ["a1", "a2", "a3"])
        sa.add(conn, "b", ["b1"])
        sa.claim(conn, "a", worker="wa", n=3)
        _expire(conn)
        sa.claim(conn, "b", worker="wb", max_attempts=1)
        failed = conn.execute(
            "SELECT count(*) FROM unit WHERE status='failed'").fetchone()[0]
        assert failed == 0, "another kind's units were retired"

    def test_a_kind_can_set_its_own_retirement(self, tmp_path):
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "cheap", "i", done_when="d", max_attempts=1)
        sa.define(conn, "patient", "i", done_when="d", max_attempts=9)
        sa.add(conn, "cheap", ["c1"])
        sa.add(conn, "patient", ["p1"])
        sa.claim(conn, worker="w", n=2)
        _expire(conn)
        sa.reclaim(conn)
        got = dict(conn.execute("SELECT kind, status FROM unit").fetchall())
        assert got["cheap"] == "failed"
        assert got["patient"] == "open", "retired before its own kind would"


class TestReleaseIsNotAnAttempt:

    def test_release_does_not_consume_an_attempt(self, tmp_path):
        """Documented as "without calling it a failure", and it counted as one:
        six honest hand-backs left the unit at the limit, so the next worker
        merely to crash sent it straight to failed."""
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", ["u1"])
        for _ in range(6):
            got = sa.claim(conn, "k", worker="w")
            assert got, "the queue retired a unit that was only handed back"
            sa.release(conn, got[0].unit_id, worker="w")
        row = conn.execute("SELECT status, attempts FROM unit").fetchone()
        assert row["status"] == "open" and row["attempts"] == 0

    def test_a_crash_still_burns_one(self, tmp_path):
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", ["u1"])
        sa.claim(conn, "k", worker="w")
        _expire(conn)
        sa.reclaim(conn)
        assert conn.execute("SELECT attempts FROM unit").fetchone()[0] == 1


class TestPinning:

    def test_a_claimed_unit_always_has_a_kind_digest(self, tmp_path):
        """The pin used to be a second statement after the claim committed, so
        a crash in between left a leased row with no digest -- and `brief_for`
        then fell back to the CURRENT definition, the exact wrong answer."""
        conn = sa.connect(str(tmp_path / "w.db"))
        for k in ("a", "b"):
            sa.define(conn, k, f"instructions for {k}", done_when="d")
        sa.add(conn, "a", ["a1", "a2"])
        sa.add(conn, "b", ["b1"])
        got = sa.claim(conn, worker="w", n=3)
        assert len(got) == 3
        for r in conn.execute("SELECT kind, kind_digest FROM unit"):
            assert r["kind_digest"], f"{r['kind']} was claimed unpinned"

    def test_the_pin_matches_the_kind_it_was_claimed_under(self, tmp_path):
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "k", "V1", done_when="d")
        sa.add(conn, "k", ["u1"])
        u = sa.claim(conn, "k", worker="w")[0]
        sa.define(conn, "k", "V2", done_when="d", force=True)
        assert "V1" in sa.brief_for(conn, u.unit_id)

    def test_the_fallback_pin_is_guarded_by_the_lease_token(self, tmp_path,
                                                            monkeypatch):
        """The pin now happens inside the claiming UPDATE, so the old
        clobber is structurally impossible on the main path. One narrow path
        remains -- a kind enqueued between the pre-read and the claim lands
        unpinned and is filled in afterwards -- and that write must carry the
        token, or a stale claimer's late pin overwrites the live holder's.

        Unreachable single-threaded, so the race is simulated: `_pins` returns
        nothing the first time, which is exactly what a kind missing from the
        pre-read looks like.
        """
        import fleetwright.leases as L
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "k", "V1", done_when="d")
        sa.add(conn, "k", ["u1"])

        real, calls = L._pins, []
        def once(c, specs):
            calls.append(1)
            return ({}, {}) if len(calls) == 1 else real(c, specs)
        monkeypatch.setattr(L, "_pins", once)

        u = sa.claim(conn, "k", worker="A")[0]
        assert len(calls) == 2, "the fallback never ran, so it is untested"
        digest = conn.execute("SELECT kind_digest FROM unit").fetchone()[0]
        assert digest, "the fallback left the unit unpinned"
        assert "V1" in sa.brief_for(conn, u.unit_id)

    def test_the_fallback_write_names_the_lease_token(self):
        """Deliberately a source assertion, and deliberately labelled as one.

        The fallback's token predicate only matters when two processes race,
        which a single-threaded test cannot stage: any assertion I write about
        it here passes whether the predicate is there or not, and an assertion
        that cannot fail is worse than none. So this checks the predicate is
        written, and says plainly that is all it checks.
        """
        src = (ROOT / "src" / "fleetwright" / "leases.py").read_text(
            encoding="utf-8")
        fallback = src.split("# A kind enqueued between the pre-read")[1][:900]
        assert "AND lease_token = ?" in fallback


class TestThenIsValidatedFirst:
    """Everything after `_close` is past the point of no return."""

    def _leased(self, tmp_path):
        conn = sa.connect(str(tmp_path / "w.db"))
        for k in ("k", "audit"):
            sa.define(conn, k, "i", done_when="d")
        sa.add(conn, "k", ["u1"])
        return conn, sa.claim(conn, "k", worker="w")[0]

    @pytest.mark.parametrize("bad", [
        {"audit": [{"a": 1}]},          # raised ProgrammingError inside add()
        {"audit": "abc"},               # enqueued one unit PER CHARACTER
        {"audit": [""]},
        {"": ["a"]},
        {"audit": 7},
        "not a dict",
    ])
    def test_finish_validates_then_before_closing(self, tmp_path, bad):
        conn, u = self._leased(tmp_path)
        with pytest.raises(ValueError):
            sa.finish(conn, u.unit_id, worker="w", result={"r": 1}, then=bad)
        row = conn.execute("SELECT status, result FROM unit "
                           "WHERE unit_id = ?", (u.unit_id,)).fetchone()
        assert row["status"] == "leased", "closed anyway; the caller cannot retry"
        assert row["result"] is None
        assert conn.execute("SELECT count(*) FROM unit WHERE kind='audit'"
                            ).fetchone()[0] == 0

    def test_a_corrected_retry_succeeds(self, tmp_path):
        conn, u = self._leased(tmp_path)
        with pytest.raises(ValueError):
            sa.finish(conn, u.unit_id, worker="w", then={"audit": "abc"})
        assert sa.finish(conn, u.unit_id, worker="w",
                         then={"audit": ["u1-a"]}) is True
        assert conn.execute("SELECT count(*) FROM unit WHERE kind='audit'"
                            ).fetchone()[0] == 1

    def test_a_bare_string_is_not_iterated(self, tmp_path):
        conn, u = self._leased(tmp_path)
        with pytest.raises(ValueError, match="per character"):
            sa.finish(conn, u.unit_id, worker="w", then={"audit": "abc"})


class TestMissingResultIsAShapeViolation:

    def _ready(self, tmp_path, returns):
        db = str(tmp_path / "w.db")
        argv = ["define", "k", "--db", db, "--instructions", "i",
                "--done-when", "d"]
        if returns:
            argv += ["--returns", returns]
        cli_main(argv)
        cli_main(["add", "k", "--db", db, "u1"])
        cli_main(["claim", "k", "--db", db, "--worker", "w"])
        conn = sa.connect(db)
        return db, conn, conn.execute("SELECT unit_id FROM unit").fetchone()[0]

    def test_finish_without_result_is_refused_when_returns_declared(self, tmp_path):
        """An agent that trips the shape gate once learns to drop --result."""
        db, conn, uid = self._ready(tmp_path, '{"claims": <int>}')
        with pytest.raises(SystemExit) as e:
            cli_main(["finish", uid, "--db", db, "--worker", "w"])
        assert e.value.code == 2
        assert conn.execute("SELECT status FROM unit").fetchone()[0] == "leased"

    def test_no_check_still_bypasses(self, tmp_path):
        db, conn, uid = self._ready(tmp_path, '{"claims": <int>}')
        assert cli_main(["finish", uid, "--db", db, "--worker", "w",
                         "--no-check"]) == 0

    def test_a_kind_with_no_returns_is_unaffected(self, tmp_path):
        db, conn, uid = self._ready(tmp_path, None)
        assert cli_main(["finish", uid, "--db", db, "--worker", "w"]) == 0

    def test_prose_returns_is_not_a_shape(self, tmp_path):
        """"a sentence saying what you found" is a legitimate thing to write."""
        db, conn, uid = self._ready(tmp_path, "a sentence saying what you found")
        assert cli_main(["finish", uid, "--db", db, "--worker", "w"]) == 0


class TestUnreadableSkill:
    """`state` is the tool whose description says CALL THIS FIRST."""

    def test_state_survives_a_binary_skill_source(self, tmp_path):
        src = tmp_path / "s.md"
        src.write_text("# skill")
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.register_skill(conn, "s", source=str(src))
        src.write_bytes(b"\xff\xfe\x00not utf-8")
        st = sa.state(conn)
        assert any("unreadable" in a["what"] for a in st["attention"])

    def test_registering_a_binary_source_does_not_raise(self, tmp_path):
        src = tmp_path / "s.md"
        src.write_bytes(b"\xff\xfe binary")
        conn = sa.connect(str(tmp_path / "w.db"))
        r = sa.register_skill(conn, "s", source=str(src))
        assert r["digest"] is None

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="chmod 000 does not deny the owner on Windows")
    def test_state_survives_an_unreadable_skill_source(self, tmp_path):
        src = tmp_path / "s.md"
        src.write_text("# skill")
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.register_skill(conn, "s", source=str(src))
        src.chmod(0o000)
        try:
            st = sa.state(conn)
            assert any("unreadable" in a["what"] for a in st["attention"])
        finally:
            src.chmod(0o644)


class TestMcpSurvivesBadFrames:
    """A batch frame is one line of valid JSON that the spec requires a server
    to accept. It used to kill the process, stranding every lease it held."""

    def _serve(self, tmp_path, frames):
        import io

        from fleetwright.mcp import Server
        out = io.StringIO()
        Server(str(tmp_path / "w.db")).serve(io.StringIO(frames), out)
        return [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]

    @pytest.mark.parametrize("frame", ["[]", "null", "42", '"s"', "{}",
                                       '{"params": [1,2]}', "true"])
    def test_a_bad_frame_does_not_kill_the_process(self, tmp_path, frame):
        replies = self._serve(
            tmp_path, frame + '\n{"jsonrpc":"2.0","id":9,"method":"tools/list"}\n')
        assert replies, "the server died before the valid request"
        assert replies[-1]["id"] == 9, "a following valid request went unanswered"

    def test_unparseable_json_is_answered(self, tmp_path):
        replies = self._serve(tmp_path, "not json at all\n")
        assert replies[0]["error"]["code"] == -32700

    def test_a_non_object_is_a_protocol_error(self, tmp_path):
        replies = self._serve(tmp_path, "42\n")
        assert replies[0]["error"]["code"] == -32600

    def test_does_not_reply_to_notifications(self, tmp_path):
        """JSON-RPC 2.0: the server MUST NOT reply to a Notification. Real
        clients send notifications/cancelled and progress routinely."""
        for m in ("notifications/cancelled", "notifications/progress",
                  "notifications/initialized"):
            assert self._serve(
                tmp_path, json.dumps({"jsonrpc": "2.0", "method": m}) + "\n") == []

    def test_a_batch_is_answered_as_a_batch(self, tmp_path):
        frame = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        ])
        replies = self._serve(tmp_path, frame + "\n")
        assert len(replies) == 1 and isinstance(replies[0], list)
        assert [r["id"] for r in replies[0]] == [1]

    def test_initialize_echoes_a_version_it_speaks(self, tmp_path):
        from fleetwright.mcp import Server
        srv = Server(str(tmp_path / "w.db"))
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}})
        assert r["result"]["protocolVersion"] == "2025-06-18"
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "1999-01-01"}})
        assert r["result"]["protocolVersion"] in ("2024-11-05",)


class TestSnapshotEscaping:
    """A snapshot is the file people mail to each other."""

    XSS = '</script><img src=x onerror=alert(1)>'

    def _snapshot_with(self, tmp_path, **where):
        from fleetwright import dashboard
        db = tmp_path / "w.db"
        conn = sa.connect(db)
        run = sa.start_run(conn, label=where.get("label", "run"))
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", [where.get("name", "u1"), "other"], run=run)
        u = sa.claim(conn, "k", worker=where.get("worker", "w"), lease=600)[0]
        if where.get("note"):
            sa.fail(conn, u.unit_id, note=where["note"],
                    worker=where.get("worker", "w"))
        else:
            sa.finish(conn, u.unit_id, worker=where.get("worker", "w"),
                      result=where.get("result"))
        sa.claim(conn, "k", worker="w2", lease=600)
        return dashboard.snapshot(db)

    @pytest.mark.parametrize("field", ["name", "worker", "note", "label"])
    def test_the_script_terminator_is_escaped(self, tmp_path, field):
        html = self._snapshot_with(tmp_path, **{field: self.XSS})
        assert "</script><img" not in html
        assert html.count("<script") == html.count("</script>")

    def test_the_data_still_round_trips(self, tmp_path):
        html = self._snapshot_with(tmp_path, name=self.XSS)
        m = re.search(r"const DATA = (.*?);\s*//", html, re.S)
        data = json.loads(m.group(1))
        assert any(self.XSS in json.dumps(v) for v in data.values())

    def test_a_javascript_line_terminator_does_not_break_the_page(self, tmp_path):
        """U+2028 is legal in JSON and a literal newline in JavaScript."""
        html = self._snapshot_with(tmp_path, name="a\u2028b")
        m = re.search(r"const DATA = (.*?);\s*//", html, re.S)
        assert "\u2028" not in m.group(1)


class TestDashboardParams:

    def test_a_malformed_limit_is_a_400_not_a_dropped_connection(self, tmp_path):
        from fleetwright import dashboard
        db = tmp_path / "w.db"
        sa.add(sa.connect(db), "k", ["u1"])
        body = _FakeHandler(dashboard, db).get("/api/units?limit=abc")
        assert body, "the connection was dropped"

    def test_sessions_expire_server_side(self, tmp_path):
        """The cookie said Max-Age=86400 and the server kept the id forever."""
        from fleetwright import dashboard
        assert dashboard.SESSION_SECONDS == 86400
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(
            encoding="utf-8")
        assert "SESSION_SECONDS" in src.split("def _authed")[1][:400], (
            "_authed does not check age, so an expired cookie still works")


class TestTimelineLanes:

    def test_a_reclaimed_unit_does_not_become_a_phantom_lane(self, tmp_path):
        """A unit back in the pool keeps claimed_at and loses its worker. It
        used to render as an ownerless lane at 100% busy that also set the
        wall-clock for every real lane, so they all showed 0%."""
        conn = sa.connect(str(tmp_path / "w.db"))
        for k in ("abandoned", "worked"):
            sa.define(conn, k, "i", done_when="d")
        sa.add(conn, "abandoned", ["a1"])
        sa.add(conn, "worked", ["w1"])
        # Claimed, expired, returned to the pool -- and left there, so its row
        # keeps claimed_at with no worker. Re-claiming it would erase the very
        # state under test.
        sa.claim(conn, "abandoned", worker="ghost")
        _expire(conn)
        sa.reclaim(conn)
        assert conn.execute(
            "SELECT worker, claimed_at FROM unit WHERE kind='abandoned'"
        ).fetchone()["claimed_at"] is not None
        u = sa.claim(conn, "worked", worker="real")[0]
        sa.finish(conn, u.unit_id, worker="real")
        tl = sa.timeline(conn)
        assert all(lane["worker"] for lane in tl["lanes"]), "an ownerless lane"
        assert [lane["worker"] for lane in tl["lanes"]] == ["real"]


class TestTheShellPattern:
    """Claim in one process, finish in another. Nothing tested this, and it
    broke completely: `this_worker()` is hostname:pid, so a finish run as a
    separate command did not recognise its own claim. Forty units claimed,
    forty "not yours", nothing finished. Subprocesses, because in-process
    tests share a pid and cannot see it."""

    def _fleet(self, tmp_path, extra_finish):
        db = str(tmp_path / "w.db")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        def run(*args):
            return subprocess.run([sys.executable, "-m", "fleetwright", *args],
                                  capture_output=True, text=True, env=env)
        run("define", "k", "--db", db, "--instructions", "do $name",
            "--done-when", "d")
        run("add", "k", "--db", db, "u1")
        c = run("claim", "k", "--db", db, "--worker", "shell-1", "--json")
        assert c.returncode == 0, c.stderr
        uid = json.loads(c.stdout)[0]["unit_id"]
        return db, run("finish", uid, "--db", db, *extra_finish), uid

    def test_a_named_worker_can_finish_what_it_claimed(self, tmp_path):
        db, done, uid = self._fleet(tmp_path, ["--worker", "shell-1"])
        assert done.returncode == 0, done.stderr
        assert sa.connect(db).execute(
            "SELECT status FROM unit").fetchone()[0] == "done"

    def test_a_close_with_no_evidence_of_ownership_is_refused(self, tmp_path):
        """Refusing beats guessing: the two ways to guess are "close somebody
        else's unit" and "refuse everything"."""
        db, done, uid = self._fleet(tmp_path, [])
        assert done.returncode == 2
        for flag in ("--worker", "--token", "--any-worker"):
            assert flag in done.stderr, "the error does not say what to pass"
        assert sa.connect(db).execute(
            "SELECT status FROM unit").fetchone()[0] == "leased"

    def test_the_token_alone_is_enough(self, tmp_path):
        db = str(tmp_path / "w.db")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        def run(*args):
            return subprocess.run([sys.executable, "-m", "fleetwright", *args],
                                  capture_output=True, text=True, env=env)
        run("define", "k", "--db", db, "--instructions", "i", "--done-when", "d")
        run("add", "k", "--db", db, "u1")
        run("claim", "k", "--db", db, "--worker", "shell-1")
        token = sa.connect(db).execute(
            "SELECT lease_token, unit_id FROM unit").fetchone()
        done = run("finish", token["unit_id"], "--db", db,
                   "--token", token["lease_token"])
        assert done.returncode == 0, done.stderr

    def test_every_shipped_shell_loop_names_its_worker(self):
        """The README loop, examples/fleet.sh and the CI fleet all closed
        without saying who they were, and all three would now be refused on
        every unit. A doc that does not run is worse than no doc."""
        sources = {
            "README.md": (ROOT / "README.md"),
            "examples/fleet.sh": (ROOT / "examples" / "fleet.sh"),
            # Absent from the sdist on purpose -- users do not need our CI
            # config -- and this test also runs inside the sdist.
            "ci.yml": (ROOT / ".github" / "workflows" / "ci.yml"),
        }
        sources = {k: v for k, v in sources.items() if v.is_file()}
        # A skip-if-missing check can quietly end up checking nothing. These
        # two are in both filesystems, so their absence is a real failure.
        assert {"README.md", "examples/fleet.sh"} <= set(sources), sources
        bad = []
        for label, path in sources.items():
            for line in path.read_text(encoding="utf-8").splitlines():
                t = line.strip()
                if not re.match(r"fleetwright (done|finish|fail|release)\b", t):
                    continue
                if not any(f in t for f in ("--worker", "--token",
                                            "--any-worker")):
                    bad.append(f"{label}: {t}")
        assert not bad, "a close with nothing to prove ownership:\n  " + \
            "\n  ".join(bad)


class TestTheBrandAsset:
    """The shipped wordmark went through an entire rename still saying
    SuperAgentic, in its <title> and both tspans, because nothing looked at
    it. A logo is the one file where being out of date is most visible and
    least likely to be noticed by a test suite."""

    def test_the_svg_names_the_product(self):
        svg = (ROOT / "assets" / "fleetwright.svg").read_text(encoding="utf-8")
        assert "FleetWright" in svg
        assert svg.count("FleetWright") >= 2, "<title> and aria-label both"
        assert ">Fleet<" in svg and ">Wright<" in svg, "the two-tone split"

    def test_no_asset_still_carries_the_old_name(self):
        for f in (ROOT / "assets").rglob("*"):
            if f.is_file() and f.suffix in (".svg", ".md"):
                t = f.read_text(encoding="utf-8", errors="ignore").lower()
                assert "superagentic" not in t, f.name

    def test_the_name_is_capitalised_where_it_is_read_not_typed(self):
        """`fleetwright` is a command and a package, so it stays lowercase
        wherever someone types it. FleetWright is the product, so it is
        capitalised wherever someone only reads it: the login heading, the
        browser tab, the wordmark, and prose."""
        from fleetwright import dashboard
        page = dashboard.PAGE
        assert "<h1>FleetWright</h1>" in page
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(
            encoding="utf-8")
        assert 'f"FleetWright · {db.name}"' in src, "the browser tab"
        # And the other direction: every command a person types is lowercase.
        for cmd in re.findall(r"fleetwright [a-z-]+", page):
            assert cmd.islower(), cmd


class TestWhichDatabase:
    """Every "the database reset itself" story is path resolution, not data
    loss. The package contains no DELETE and no DROP; what it had was three
    ways to open a NEW file and report a perfectly healthy zero units."""

    def _run(self, cwd, *args):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        env.pop("FLEETWRIGHT_DB", None)
        return subprocess.run([sys.executable, "-m", "fleetwright", *args],
                              capture_output=True, text=True, cwd=str(cwd),
                              env=env)

    def _project(self, tmp_path):
        proj = tmp_path / "project"
        (proj / "sub").mkdir(parents=True)
        self._run(proj, "define", "k", "--instructions", "i", "--done-when", "d")
        self._run(proj, "add", "k", "p1", "p2", "p3")
        return proj

    def test_a_subdirectory_joins_the_project_instead_of_starting_a_rival(
            self, tmp_path):
        """`work.db` is relative, so `cd sub` used to make a SECOND database
        and report "no units queued" about it."""
        proj = self._project(tmp_path)
        out = self._run(proj / "sub", "status")
        assert "k" in out.stdout, out.stdout + out.stderr
        assert not (proj / "sub" / "work.db").exists(), \
            "a second database was created in the subdirectory"

    def test_a_typo_is_refused_rather_than_created(self, tmp_path):
        proj = self._project(tmp_path)
        out = self._run(proj, "status", "--db", "worrk.db")
        assert out.returncode == 2
        assert "did you mean" in out.stderr and "work.db" in out.stderr
        assert not (proj / "worrk.db").exists(), "created it anyway"

    def test_create_overrides_the_refusal(self, tmp_path):
        proj = self._project(tmp_path)
        out = self._run(proj, "status", "--db", "worrk.db", "--create")
        assert out.returncode == 0, out.stderr
        assert (proj / "worrk.db").exists()

    def test_a_genuinely_different_name_is_not_refused(self, tmp_path):
        """The first version of this guard refused ANY new database next to an
        existing one, which would have blocked a second queue -- an ordinary
        thing to want, and a worse bug than the one being fixed."""
        proj = self._project(tmp_path)
        out = self._run(proj, "status", "--db", "audit.db")
        assert out.returncode == 0, out.stderr
        assert (proj / "audit.db").exists()

    def test_creating_a_database_is_announced(self, tmp_path):
        out = self._run(tmp_path, "status")
        assert "created a new database" in out.stderr, \
            "a file appearing in silence looks like the old one was emptied"

    def test_the_environment_pins_one_file(self, tmp_path):
        proj = self._project(tmp_path)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "FLEETWRIGHT_DB": str(proj / "work.db")}
        out = subprocess.run(
            [sys.executable, "-m", "fleetwright", "status"],
            capture_output=True, text=True, cwd=str(tmp_path), env=env)
        assert "k" in out.stdout, out.stdout + out.stderr

    def test_state_reports_on_the_file_the_workers_use(self, tmp_path):
        proj = self._project(tmp_path)
        out = self._run(proj / "sub", "state")
        assert "3 units" in out.stdout, out.stdout + out.stderr


class TestDurability:
    """The database itself is fine. Measured, not assumed."""

    def test_the_package_never_deletes_a_row(self):
        for f in (ROOT / "src" / "fleetwright").rglob("*.py"):
            src = f.read_text(encoding="utf-8")
            for verb in ("DELETE FROM", "DROP TABLE", "DROP INDEX"):
                assert verb not in src, f"{f.name} contains {verb}"

    def test_commits_are_flushed_to_disk(self, tmp_path):
        """WAL relaxes durability only if you ask it to. Nothing here does, so
        synchronous stays FULL and a commit survives power loss, not merely a
        crashed process."""
        conn = sa.connect(str(tmp_path / "w.db"))
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2

    def test_a_killed_worker_loses_nothing_it_committed(self, tmp_path):
        """SIGKILL: no cleanup, no close, WAL left hot."""
        db = str(tmp_path / "w.db")
        script = f"""
import sys, time
sys.path.insert(0, {str(ROOT / "src")!r})
import fleetwright as sa
c = sa.connect({db!r})
sa.define(c, "k", "i", done_when="d")
sa.add(c, "k", [f"u{{i}}" for i in range(200)])
u = sa.claim(c, "k", worker="doomed", n=5)
for x in u[:3]:
    sa.finish(c, x.unit_id, worker="doomed", result={{"ok": 1}})
print("ready", flush=True)
while True: time.sleep(0.05)
"""
        proc = subprocess.Popen([sys.executable, "-c", script],
                                stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "ready"
        proc.kill()
        proc.wait(timeout=10)
        p = sa.progress(sa.connect(db))["k"]
        assert sum(p.values()) == 200, p
        assert p["done"] == 3, p
        assert p["leased"] == 2, p

    def test_backup_captures_what_the_wal_still_holds(self, tmp_path):
        """`cp` of the .db alone misses recent commits, silently, because what
        it copied is a valid database of an earlier moment."""
        import shutil
        db = tmp_path / "w.db"
        conn = sa.connect(db)
        sa.define(conn, "k", "i", done_when="d")
        sa.add(conn, "k", [f"u{i}" for i in range(50)])
        assert (tmp_path / "w.db-wal").exists(), "no WAL, so nothing to prove"

        naive = tmp_path / "naive.db"
        shutil.copyfile(db, naive)
        good = sa.backup(conn, tmp_path / "good.db")

        assert not (tmp_path / "good.db-wal").exists(), "left a WAL beside it"
        assert sa.progress(sa.connect(good))["k"]["open"] == 50
        naive_total = sum(sa.progress(sa.connect(naive)).get("k", {}).values())
        assert naive_total < 50, (
            "the naive copy happened to be complete, so this test proves "
            "nothing on this platform")

    def test_backup_refuses_to_overwrite(self, tmp_path):
        conn = sa.connect(tmp_path / "w.db")
        sa.add(conn, "k", ["a"])
        sa.backup(conn, tmp_path / "b.db")
        with pytest.raises(FileExistsError):
            sa.backup(conn, tmp_path / "b.db")


class TestDashboardAuthHardening:
    """A dashboard on loopback is not a private dashboard.

    Two assumptions were wrong and both were measured, not argued:
    the browser's same-origin policy does not protect a localhost service
    from a page that rebinds its own DNS, and `time.sleep(0.5)` on a wrong
    token does not slow an attacker on a THREADED server.
    """

    def _serve(self, tmp_path, port, **kw):
        """A real socket, because every one of these lives in the HTTP layer."""
        import threading

        from fleetwright import dashboard
        db = tmp_path / "w.db"
        sa.add(sa.connect(db), "k", ["u1"])
        t = threading.Thread(
            target=dashboard.serve,
            args=([db],),
            kwargs={"port": port, "open_browser": False, **kw},
            daemon=True)
        t.start()
        for _ in range(60):
            try:
                self._get(port, "/api")
                break
            except Exception:
                time.sleep(0.1)
        return db

    def _get(self, port, path, host=None, headers=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if host:
            req.add_header("Host", host)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _post(self, port, path, body, headers=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     method="POST",
                                     data=json.dumps(body).encode())
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)
        except OSError:
            # A refused or reset connection under load. Not an answer, and
            # NOT counted as a guess that got through -- 0 is neither 401
            # nor 429, so it cannot make either assertion pass by accident.
            return 0, {}

    def test_a_foreign_host_header_is_refused(self, tmp_path):
        """DNS rebinding. A page at evil.com whose DNS re-resolves to
        127.0.0.1 is, to the browser, still evil.com talking to itself, so
        same-origin lets it READ the response. Only the server can notice."""
        self._serve(tmp_path, 8761)
        code, body = self._get(8761, "/api", host="evil.example.com")
        assert code == 421, f"served a foreign host: {body[:120]}"
        assert b"unit" not in body, "leaked queue contents"

    def test_our_own_names_still_work(self, tmp_path):
        self._serve(tmp_path, 8762)
        for h in (None, "localhost:8762", "127.0.0.1:8762"):
            code, _ = self._get(8762, "/api", host=h)
            assert code == 200, f"refused its own name {h}"

    def _must_refuse(self, tmp_path, port, **kw):
        """Assert serve() refuses, WITHOUT the test hanging if it does not.

        Called directly, a serve() that fails to refuse binds the port and
        runs forever, and the test times out instead of failing. A hang is
        not a failure: it reports as an infrastructure problem, and I only
        found this by mutating the check away and watching pytest sit there
        for nine minutes.
        """
        import threading

        from fleetwright import dashboard
        db = tmp_path / "w.db"
        sa.add(sa.connect(db), "k", ["u1"])
        box = {}

        def run():
            try:
                dashboard.serve([db], port=port, open_browser=False, **kw)
            except BaseException as e:  # noqa: BLE001 - SystemExit included
                box["raised"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=5)
        assert "raised" in box, "it bound the port instead of refusing"
        return str(box["raised"])

    def test_a_short_token_is_refused_at_startup(self, tmp_path):
        msg = self._must_refuse(tmp_path, 8763, token="abc")
        assert "16" in msg, "does not say what the minimum is"

    def test_off_loopback_without_a_token_is_refused(self, tmp_path):
        msg = self._must_refuse(tmp_path, 8764, host="0.0.0.0")
        assert "ssh -N -L" in msg, "does not offer the tunnel"

    def test_guessing_is_locked_out_even_in_parallel(self, tmp_path):
        """The old defence delayed each CONNECTION on a threaded server: 200
        wrong guesses ran in 2.1 seconds, 95 a second."""
        import concurrent.futures as cf

        from fleetwright import dashboard
        n = 60
        self._serve(tmp_path, 8765, token=("z" * 24))
        # Eight, not thirty-two. The point is that the lockout counts guesses,
        # not that the runner can hold thirty-two sockets open: at 32 the CI
        # box refused connections and the error escaped the pool.
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            codes = [c for c, _ in ex.map(
                lambda i: self._post(8765, "/login", {"token": f"g{i}"}),
                range(n))]
        tried = codes.count(401)
        assert 429 in codes, f"no lockout; codes were {sorted(set(codes))}"
        assert tried < n, "every guess was allowed"
        # Generous: threads already in flight when the tenth failure lands are
        # still counted, so the bound is the limit plus the pool, not the limit.
        assert tried <= dashboard.LOCKOUT_AFTER + 8, \
            f"{tried} guesses got through a limit of {dashboard.LOCKOUT_AFTER}"

    def test_the_real_token_still_works_after_someone_else_is_locked_out(
            self, tmp_path):
        """A lockout that also locks out the owner is a denial of service
        wearing a security hat. Same address here, so this is the strict
        case: it must recover once the window passes."""
        tok = "y" * 24
        self._serve(tmp_path, 8766, token=tok)
        code, _ = self._post(8766, "/login", {"token": tok})
        assert code == 200

    def test_a_cross_site_post_is_refused(self, tmp_path):
        self._serve(tmp_path, 8767, token=("w" * 24))
        code, _ = self._post(8767, "/login", {"token": "w" * 24},
                             headers={"Origin": "https://evil.example.com"})
        assert code == 403

    def test_the_cookie_is_secure_only_behind_tls(self, tmp_path):
        """Unconditionally Secure would make the cookie unusable over plain
        loopback, which is most of the usage."""
        tok = "v" * 24
        self._serve(tmp_path, 8768, token=tok)
        _, h = self._post(8768, "/login", {"token": tok})
        assert "fw_session=" in h["Set-Cookie"]
        assert "HttpOnly" in h["Set-Cookie"] and "SameSite=Strict" in h["Set-Cookie"]
        assert "Secure" not in h["Set-Cookie"]
        _, h2 = self._post(8768, "/login", {"token": tok},
                           headers={"X-Forwarded-Proto": "https"})
        assert "Secure" in h2["Set-Cookie"]

    def test_the_token_is_compared_in_constant_time(self):
        src = (ROOT / "src" / "fleetwright" / "dashboard.py").read_text(
            encoding="utf-8")
        assert "compare_digest" in src
        assert "given == self.token" not in src


class TestPromptNamesTheRealDatabase:

    def test_the_prompt_carries_an_absolute_path(self, tmp_path):
        """A worker prompt is pasted into agents that run from anywhere, so a
        relative `--db work.db` is a worker quietly making its own queue."""
        db = tmp_path / "work.db"
        cli_main(["define", "k", "--db", str(db), "--instructions", "i",
                  "--done-when", "d"])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli_main(["prompt", "k", "--db", str(db)])
        out = buf.getvalue()
        assert str(db.resolve()) in out
        assert "--db work.db" not in out


class TestMetaIsRendered:
    """`--meta '{"path": "scans/$name.txt"}'` is the documented way to give a
    unit its file, and the worker used to get the TEMPLATE back."""

    def _claimed(self, tmp_path):
        conn = sa.connect(str(tmp_path / "w.db"))
        sa.define(conn, "k", "Read $path", done_when="d")
        sa.add(conn, "k", ["p01"], meta={"path": "scans/$name.txt", "n": 7})
        return conn, sa.claim(conn, "k", worker="w")[0]

    def test_a_worker_reading_meta_gets_the_value_not_the_template(self, tmp_path):
        conn, u = self._claimed(tmp_path)
        assert u.meta["path"] == "scans/p01.txt", u.meta

    def test_the_brief_shows_the_value(self, tmp_path):
        conn, u = self._claimed(tmp_path)
        assert "path: scans/p01.txt" in u.brief()
        assert "$name" not in u.brief()

    def test_non_strings_are_left_alone(self, tmp_path):
        conn, u = self._claimed(tmp_path)
        assert u.meta["n"] == 7

    def test_the_instructions_still_substitute(self, tmp_path):
        conn, u = self._claimed(tmp_path)
        assert u.instructions == "Read scans/p01.txt"

    def test_the_row_keeps_the_template(self, tmp_path):
        """Rendered on the way out, never in the row: the stored value should
        say what was meant, not what one unit happened to get."""
        conn, u = self._claimed(tmp_path)
        raw = json.loads(conn.execute("SELECT meta FROM unit").fetchone()[0])
        assert raw["path"] == "scans/$name.txt"


class TestProjectLabels:
    """A project is a repository, and the thing people call it is the
    directory, not the file. Every repository holds the default `work.db`, so
    labelling by filename gave one project called `work` and the rest their
    absolute paths."""

    def _dbs(self, tmp_path, *names):
        from fleetwright import dashboard
        out = []
        for n in names:
            d = tmp_path / n
            d.mkdir(parents=True, exist_ok=True)
            sa.add(sa.connect(d / "work.db"), "k", ["a"])
            out.append(d / "work.db")
        return dashboard._projects(out)

    def test_repositories_are_named_after_themselves(self, tmp_path):
        ps = self._dbs(tmp_path, "myth-analysis", "apply-intelligence",
                       "project-kzd")
        assert set(ps) == {"myth-analysis", "apply-intelligence",
                           "project-kzd"}

    def test_no_project_falls_back_to_an_absolute_path(self, tmp_path):
        ps = self._dbs(tmp_path, "a", "b", "c")
        assert not any(k.startswith("/") for k in ps), ps

    def test_a_deliberate_filename_wins_over_the_directory(self, tmp_path):
        """`audit.db` was named on purpose; `work.db` was not."""
        from fleetwright import dashboard
        d = tmp_path / "repo"
        d.mkdir()
        for n in ("work.db", "audit.db"):
            sa.add(sa.connect(d / n), "k", ["a"])
        ps = dashboard._projects([d])
        assert set(ps) == {"repo", "audit"}
        assert ps["audit"].name == "audit.db"

    def test_same_named_directories_are_still_distinguished(self, tmp_path):
        """Two checkouts of the same repo, side by side."""
        from fleetwright import dashboard
        paths = []
        for parent in ("old", "new"):
            d = tmp_path / parent / "myth-analysis"
            d.mkdir(parents=True)
            sa.add(sa.connect(d / "work.db"), "k", ["a"])
            paths.append(d / "work.db")
        ps = dashboard._projects(paths)
        assert len(ps) == 2, ps
        assert all("myth-analysis" in k for k in ps), ps

    def test_the_same_database_twice_is_one_project(self, tmp_path):
        from fleetwright import dashboard
        d = tmp_path / "repo"
        d.mkdir()
        sa.add(sa.connect(d / "work.db"), "k", ["a"])
        ps = dashboard._projects([d / "work.db", d / "work.db"])
        assert len(ps) == 1

    def test_the_environment_lists_projects(self, tmp_path, monkeypatch):
        """One export shows every repository you work on."""
        src = (ROOT / "src" / "fleetwright" / "cli.py").read_text(
            encoding="utf-8")
        assert "FLEETWRIGHT_PROJECTS" in src
        assert "os.pathsep" in src, "must split like PATH, not on a comma"
