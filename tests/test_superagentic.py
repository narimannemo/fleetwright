"""The tests that matter here are the ones about failure.

Any queue passes "hand out ten units to two workers." What separates a lease
table from a broken one is what happens when a worker dies holding work, when a
slow worker comes back after losing its lease, and when two real OS processes
race the same file. Those have their own tests below and they are the point.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import superagentic as sa
from superagentic.cli import main as cli_main
from superagentic.mcp import Server

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
        sa.claim(conn, "x", worker="a", lease=0.01)
        time.sleep(0.05)
        assert "attempt 2" in sa.claim(conn, "x", worker="b")[0].brief()

    def test_redefining_reaches_workers_that_have_not_claimed_yet(self, conn):
        sa.define(conn, "x", instructions="old")
        sa.add(conn, "x", ["u1", "u2"])
        sa.claim(conn, "x", worker="a")
        sa.define(conn, "x", instructions="new")
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
        from superagentic import leases
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
        from superagentic.cli import build_parser
        sa.define(conn, "x", instructions="go")
        p = sa.worker_prompt(conn, "x", db=str(tmp_path / "w.db"))
        import shlex
        parser = build_parser()
        checked = 0
        for line in p.splitlines():
            line = line.strip()
            if not line.startswith("superagentic "):
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
        assert "superagentic claim --db w.db" in p


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
        slow = sa.claim(conn, "extract", worker="slow", lease=0.01)[0]
        time.sleep(0.05)
        sa.claim(conn, "extract", worker="fast")
        assert sa.finish(conn, slow.unit_id, worker="slow",
                         then={"audit": ["c1"]}) is False
        assert "audit" not in sa.progress(conn), \
            "a worker that lost its lease must not enqueue off the back of it"


class TestFailure:
    """A lease is only worth having if these hold."""

    def test_a_crashed_workers_unit_comes_back(self, conn):
        sa.add(conn, "x", ["u1"])
        assert sa.claim(conn, "x", worker="dies", lease=0.01)
        assert sa.claim(conn, "x", worker="b") == [], "still leased, correctly"
        time.sleep(0.05)
        # No daemon: the next claimer reclaims on the way in.
        again = sa.claim(conn, "x", worker="b")
        assert [u.name for u in again] == ["u1"]
        assert again[0].attempts == 2

    def test_a_lost_lease_cannot_be_closed_or_extended(self, conn):
        sa.add(conn, "x", ["u1"])
        slow = sa.claim(conn, "x", worker="slow", lease=0.01)[0]
        time.sleep(0.05)
        sa.claim(conn, "x", worker="fast")
        assert sa.heartbeat(conn, [slow.unit_id], worker="slow") == 0
        assert sa.finish(conn, slow.unit_id, worker="slow") is False
        assert sa.finish(conn, slow.unit_id, worker="fast") is True

    def test_a_heartbeat_keeps_a_slow_worker_from_being_reclaimed(self, conn):
        sa.add(conn, "x", ["u1"])
        u = sa.claim(conn, "x", worker="slow", lease=0.05)[0]
        sa.heartbeat(conn, [u.unit_id], worker="slow", lease=30)
        time.sleep(0.08)
        assert sa.claim(conn, "x", worker="other") == []

    def test_a_poison_unit_is_retired_rather_than_re_leased_forever(self, conn):
        sa.add(conn, "x", ["bad"])
        for _ in range(3):
            sa.claim(conn, "x", worker="w", lease=0.01)
            time.sleep(0.02)
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
            "import superagentic as sa;"
            f"c = sa.connect({str(db)!r});"
            "print(json.dumps([u.name for u in "
            "sa.claim(c, 'x', worker=sys.argv[1], n=20)]))"
        )
        procs = [subprocess.Popen([sys.executable, "-c", prog, f"w{i}"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for i in range(3)]
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
        cli_main(["claim", "x", "--db", db, "--worker", "a", "--lease", "0.01"])
        time.sleep(0.05)
        cli_main(["claim", "x", "--db", db, "--worker", "b"])
        assert cli_main(["done", "x:u1", "--db", db, "--worker", "a"]) == 1
        assert cli_main(["done", "x:u1", "--db", db, "--worker", "b"]) == 0

    def test_status_on_an_empty_db_says_what_to_do_next(self, tmp_path, capsys):
        cli_main(["status", "--db", str(tmp_path / "w.db")])
        assert "superagentic add" in capsys.readouterr().out

    def test_the_demo_runs_and_cleans_up_after_itself(self, capsys):
        """The cleanup half is the Windows half.

        The demo works in a TemporaryDirectory. Windows will not delete a file
        that is still open, so an unclosed connection makes the demo do all its
        work, print all its output, and then die on the very last line. POSIX
        never reproduces it, which is what the Windows runner is for.
        """
        import tempfile

        from superagentic.demo import main as demo
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
        assert names == {"start_run", "list_runs", "define_kind", "add_jobs",
                         "worker_prompt", "job_results", "claim_job",
                         "finish_job", "release_job", "fail_job",
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
        s.claim_job({"kind": "x", "lease_seconds": 0.01})
        time.sleep(0.05)
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


class TestDocs:
    def test_every_readme_link_resolves(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        missing = [t for t in re.findall(r"\]\(([^)#:]+\.md)\)", readme)
                   if not (ROOT / t).exists()]
        assert not missing, f"README links to missing files: {missing}"

    def test_the_reference_matches_the_cli(self):
        from superagentic.cli import build_parser
        real = set(build_parser()._subparsers._group_actions[0].choices)
        doc = set(re.findall(r"^superagentic (\w+)",
                             (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
                             re.M))
        assert not doc - real, f"documented but absent: {sorted(doc - real)}"
        assert not real - doc, f"undocumented commands: {sorted(real - doc)}"

    def test_the_skill_only_uses_commands_that_exist(self):
        """The skill teaches agents shell commands. If one is renamed, the
        skill goes stale silently and every agent that reads it runs a command
        that does not exist."""
        from superagentic.cli import build_parser
        real = set(build_parser()._subparsers._group_actions[0].choices)
        text = (ROOT / "skills" / "superagentic" / "SKILL.md").read_text(encoding="utf-8")
        used = set(re.findall(r"superagentic (\w+)", text)) - {"serve"}
        assert not used - real, f"skill uses commands that do not exist: {sorted(used - real)}"

    def test_the_skill_names_every_mcp_tool_correctly(self):
        from superagentic.mcp import _tools
        real = {t["name"] for t in _tools()}
        text = (ROOT / "skills" / "superagentic" / "SKILL.md").read_text(encoding="utf-8")
        named = set(re.findall(r"`(\w+_(?:job|jobs|kind|results|status))`", text))
        assert not named - real, f"skill names tools that do not exist: {sorted(named - real)}"

    def test_the_skill_has_the_frontmatter_that_makes_it_loadable(self):
        text = (ROOT / "skills" / "superagentic" / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), "no frontmatter; the skill will not load"
        fm = text.split("---", 2)[1]
        assert re.search(r"^name: superagentic$", fm, re.M)
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
            f"https://files.pythonhosted.org/packages/ab/superagentic-{v}.tar.gz",
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
        assert "#{bin}/superagentic" in out
        assert "{{" not in out and "#{{" not in out

    def test_the_formula_names_the_published_sdist_and_its_checksum(self, monkeypatch, capsys):
        out = self._formula(monkeypatch, capsys)
        assert 'url "https://files.pythonhosted.org/packages/ab/superagentic-0.1.0.tar.gz"' in out
        assert f'sha256 "{"0" * 64}"' in out
        assert "class Superagentic < Formula" in out

    def test_desc_meets_homebrew_audit_rules(self, monkeypatch, capsys):
        desc = [ln for ln in self._formula(monkeypatch, capsys).splitlines()
                if ln.strip().startswith("desc ")][0].strip()[6:-1]
        assert len(desc) <= 70, "brew audit rejects a desc over 80 incl. `desc `"
        assert not desc.endswith(("lis", "sever")) and desc.split()[-1] != "a", \
            "truncated mid-word; cut at a word boundary"
        assert not desc.endswith("."), "brew audit rejects a trailing full stop"
        assert not desc.lower().startswith("superagentic"), \
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


class TestDashboard:
    def _stats(self, conn):
        from superagentic import leases
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
        from superagentic import dashboard
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
        from superagentic import dashboard
        html = dashboard.snapshot(tmp_path / "w.db")
        for hook in ("prefers-color-scheme: dark", ':root[data-theme="dark"]',
                     ':root[data-theme="light"]'):
            assert hook in html, f"{hook} missing; the theme toggle will not win"

    def test_the_dashboard_never_writes(self, tmp_path):
        """Pointing it at a live run must not disturb the run."""
        from superagentic import dashboard
        src = (ROOT / "src" / "superagentic" / "dashboard.py").read_text(encoding="utf-8")
        for verb in ("sa.claim(", "leases.claim(", "leases.finish(", "leases.add(",
                     "DELETE", "INSERT", "UPDATE"):
            assert verb not in src, f"dashboard.py contains {verb!r}"
        db = tmp_path / "w.db"
        sa.add(sa.connect(db), "x", ["u1"])
        before = db.read_bytes()
        dashboard.snapshot(db)
        assert sa.progress(sa.connect(db))["x"][sa.OPEN] == 1
        assert len(db.read_bytes()) >= len(before)


class TestDashboardAuth:
    """The login exists to make one mistake impossible, not to look secure."""

    def _serve(self, tmp_path, **kw):
        from superagentic import dashboard
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
        src = (ROOT / "src" / "superagentic" / "dashboard.py").read_text(encoding="utf-8")
        assert "hmac.compare_digest" in src
        assert "== self.token" not in src and "self.token ==" not in src

    def test_the_session_cookie_is_httponly_and_samesite(self):
        src = (ROOT / "src" / "superagentic" / "dashboard.py").read_text(encoding="utf-8")
        assert "HttpOnly" in src and "SameSite=Strict" in src

    def test_projects_are_databases_and_a_directory_expands(self, tmp_path):
        from superagentic import dashboard
        (tmp_path / "d").mkdir()
        for n in ("alpha", "beta"):
            sa.connect(tmp_path / "d" / f"{n}.db")
        sa.connect(tmp_path / "solo.db")
        got = dashboard._projects([tmp_path / "d", tmp_path / "solo.db"])
        assert set(got) == {"alpha", "beta", "solo"}

    def test_the_page_renders_a_sidebar_and_a_gate(self, tmp_path):
        from superagentic import dashboard
        db = tmp_path / "p.db"
        sa.add(sa.connect(db), "x", ["a"])
        html = dashboard.snapshot(db)
        for hook in ('class="shell"', 'id="gate"', 'id="projects"',
                     'id="sideruns"', 'id="logout"'):
            assert hook in html, hook

    def test_a_snapshot_carries_its_own_project(self, tmp_path):
        # Without this the static file renders an empty sidebar: `projects`
        # was only ever added by the request handler.
        from superagentic import dashboard
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
        from superagentic import dashboard
        db = tmp_path / "p.db"
        sa.add(sa.connect(db), "x", ["a"])
        html = dashboard.snapshot(db)
        assert 'const DATA = {' in html
        assert 'if (DATA) {' in html and '$("#shell").hidden = false;' in html
