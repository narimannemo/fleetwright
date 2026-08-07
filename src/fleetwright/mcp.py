"""An MCP server, so an agent claims work instead of guessing at it.

The alternative is telling an agent in its prompt which page to work on, which
means something outside the fleet has to decide the split in advance and be
right about how fast each worker turns out to be. It never is.

Here the agent asks. `claim_job` hands it a unit nobody else holds, and — the
part that matters — tells it plainly to **stop when the queue is empty rather
than invent work**. An agent with nothing to do will find something, and what
it finds is usually a unit another agent is already doing.

Deliberately no dependencies. MCP is JSON-RPC 2.0 over stdio with a small
method set; implementing it directly is a hundred lines and means this package
installs with nothing at all. A server that needs a framework to be reachable
is a server people do not reach.

    $ fleetwright serve --db work.db
    # or in an MCP client config:
    #   "work": {"command": "fleetwright", "args": ["serve", "--db", "work.db"]}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import leases, shape

PROTOCOL = "2024-11-05"

#: Versions this server will answer in. Echoing back a version we do not speak
#: would be a lie; answering only in ours ignores a client that asked politely.
SPOKEN = ("2024-11-05", "2025-03-26", "2025-06-18")


def _tools() -> list[dict]:
    # Written for an agent reading tool descriptions rather than documentation,
    # because nothing else will tell it the protocol. Every description says
    # what to do next and what the failure means.
    return [
        # -- the orchestrator's half. An agent that spawns a fleet defines the
        # work here first, so the workers it spawns need no prompt beyond
        # "claim work and do it".
        {
            "name": "project_state",
            "description": (
                "CALL THIS FIRST in a new session, before anything else. Where "
                "this project is: which runs exist and which are still going, "
                "how much is done, what failed, what needs a human, and the "
                "single next command. You have no memory of previous sessions "
                "and this is how you get it."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "start_run",
            "description": (
                "BEGIN A RUN BEFORE ENQUEUEING ANYTHING. A run is one execution "
                "of a fleet: it groups the units you are about to create, so "
                "afterwards you can ask what THIS fleet did rather than what "
                "the whole database contains. It also scopes unit ids, so "
                "running the same corpus again actually re-does the work "
                "instead of finding it already done. Returns a run_id to pass "
                "to add_jobs."),
            "inputSchema": {"type": "object", "properties": {
                "label": {"type": "string",
                          "description": "What this run is, in a few words."},
                "note": {"type": "string"},
                "started_by": {"type": "string",
                               "description": "Who you are. Kept for the record."}}},
        },
        {
            "name": "list_runs",
            "description": (
                "Every run, newest first, with what it did: units, done, "
                "failed, workers, elapsed. Call it to find a previous run's "
                "id, or to see whether one is still going."),
            "inputSchema": {"type": "object", "properties": {
                "limit": {"type": "integer", "default": 25}}},
        },
        {
            "name": "register_skill",
            "description": (
                "Say what a skill name means before a kind requires it: where "
                "a worker gets it and which version. A readable file is hashed, "
                "so units claimed before and after an edit are tellable apart "
                "afterwards. This does NOT fetch or install anything — putting "
                "the skill in place is your runtime's job."),
            "inputSchema": {"type": "object", "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "source": {"type": "string", "description":
                                           "a path, a URL, or a sentence"},
                                "version": {"type": "string"},
                                "note": {"type": "string"}}},
        },
        {
            "name": "list_skills",
            "description": (
                "Registered skills, their versions, and how many units have "
                "actually run with each. Skills used but never registered are "
                "listed too, flagged — that means a kind names something "
                "nothing records where to get."),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "define_kind",
            "description": (
                "Say what a kind of work IS, once, before enqueueing any of "
                "it. Every worker that claims a unit of this kind is handed "
                "these instructions — including workers spawned an hour later "
                "and the one that inherits a unit a crashed worker dropped. "
                "Put the task here, not in the prompt you spawn workers with. "
                "Use $name for the unit and $key for any value in its meta."),
            "inputSchema": {
                "type": "object", "required": ["kind", "instructions"],
                "properties": {
                    "kind": {"type": "string"},
                    "instructions": {"type": "string", "description":
                                     "What to do. Written for an agent with NO "
                                     "other context."},
                    "done_when": {"type": "string", "description":
                                  "What finished looks like. Without this a "
                                  "worker decides for itself and they disagree."},
                    "returns": {"type": "string", "description":
                                "The shape to hand back to finish_job."},
                    "tools": {"type": "string", "description":
                              "Free-text hint. Prefer `skills` and `mcp`."},
                    "skills": {"type": "array", "items": {"type": "string"},
                               "description":
                               "Skills a worker MUST load before starting. A "
                               "worker that cannot load one should FAIL the "
                               "unit rather than improvise."},
                    "mcp": {"type": "object", "description":
                            "MCP servers a worker must have, as "
                            "{name: command}."},
                    "context": {"type": "string", "description":
                                "Read-only material every worker of this kind "
                                "receives: a glossary, conventions, a schema. "
                                "Never written by a worker."},
                    "force": {"type": "boolean", "description":
                              "Redefine even when units are waiting or in "
                              "flight. Refused without this, because changing "
                              "a kind mid-run silently gives the remaining "
                              "units different instructions."}}},
        },
        {
            "name": "add_jobs",
            "description": (
                "Enqueue units of a defined kind. Idempotent on kind+name, so "
                "re-running an enumeration after the corpus grew adds only "
                "what is new. `meta` travels with each unit and its keys are "
                "substituted into the instructions."),
            "inputSchema": {
                "type": "object", "required": ["kind", "names"],
                "properties": {
                    "kind": {"type": "string"},
                    "names": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "integer", "default": 0},
                    "meta": {"type": "object"},
                    "run": {"type": "string", "description":
                            "The run_id from start_run. Omit only if you "
                            "genuinely do not want these units grouped."}}},
        },
        {
            "name": "worker_prompt",
            "description": (
                "The prompt to spawn workers with, generated from the kind. "
                "Use this rather than writing one: it already tells the worker "
                "to claim before starting, to load the skills the kind "
                "requires, and to STOP when the queue is empty. Spawn every "
                "worker in one message so they run concurrently."),
            "inputSchema": {"type": "object", "properties": {
                "kind": {"type": "string"},
                "n": {"type": "integer", "default": 1},
                "db": {"type": "string", "default": "work.db"}}},
        },
        {
            "name": "job_results",
            "description": (
                "Collect what the fleet produced. For the agent that spawned "
                "the workers and now has to assemble their output."),
            "inputSchema": {"type": "object", "properties": {
                "kind": {"type": "string"}, "run": {"type": "string"}}},
        },
        # -- the worker's half.
        {
            "name": "claim_job",
            "description": (
                "TAKE A UNIT OF WORK BEFORE STARTING ANY. Other workers share "
                "this queue, and a unit you did not claim is one two of you "
                "will do. Returns the unit and a unit_id, or reports the queue "
                "is empty — in which case STOP. Do not invent work. THE UNIT "
                "COMES WITH ITS OWN INSTRUCTIONS in `brief`: do what they say, "
                "not what you assume the task is. Call finish_job as soon as "
                "you are done, and heartbeat_job if the unit will take longer "
                "than the lease."),
            "inputSchema": {"type": "object", "properties": {
                "kind": {"type": "string",
                         "description": "translate, extract, audit… omit for any"},
                "n": {"type": "integer", "default": 1},
                "run": {"type": "string", "description": "take only this run's work"},
                "model": {"type": "string", "description":
                          "Which model you are, e.g. claude-opus-5. Say it "
                          "plainly; it is recorded as you declare it and is "
                          "how anyone later compares one model's work against "
                          "another's."},
                "lease_seconds": {"type": "number", "default": 900}}},
        },
        {
            "name": "finish_job",
            "description": (
                "Mark a claimed unit done. If this reports the lease expired, "
                "you took too long, another worker owns the unit now, and you "
                "should claim a different one rather than carry on."),
            "inputSchema": {"type": "object", "required": ["unit_id"],
                            "properties": {
                                "unit_id": {"type": "string"},
                                "result": {"description":
                                           "What you produced, in the shape the "
                                           "unit's instructions asked for."},
                                "then": {"type": "object", "description":
                                         "Follow-on work: {kind: [names]}. Use "
                                         "it to hand the next stage its units."},
                                "tokens_in": {"type": "integer", "description":
                                              "input tokens you used, if you know"},
                                "tokens_out": {"type": "integer"},
                                "cost": {"type": "number", "description":
                                         "what this unit cost. Reported, never "
                                         "checked, and the only way anyone can "
                                         "later compare one model against another."},
                                "note": {"type": "string"}}},
        },
        {
            "name": "release_job",
            "description": (
                "Give a unit back unfinished, without it counting as a "
                "failure — wrong language, blank input, out of budget. Much "
                "better than holding the lease until it expires. Use fail_job "
                "instead when the unit itself is the problem."),
            "inputSchema": {"type": "object", "required": ["unit_id"],
                            "properties": {"unit_id": {"type": "string"},
                                           "note": {"type": "string"}}},
        },
        {
            "name": "fail_job",
            "description": (
                "Report a unit you could not do, with the reason. It is "
                "retried a few times and then set aside for a human. The note "
                "is kept — say what actually went wrong, not that it failed."),
            "inputSchema": {"type": "object", "required": ["unit_id", "note"],
                            "properties": {"unit_id": {"type": "string"},
                                           "note": {"type": "string"}}},
        },
        {
            "name": "heartbeat_job",
            "description": (
                "Extend the lease on work still in progress. Call it for any "
                "unit taking longer than its lease, or another worker will "
                "start the same unit while you are still on it."),
            "inputSchema": {"type": "object", "required": ["unit_id"],
                            "properties": {"unit_id": {"type": "string"},
                                           "lease_seconds": {"type": "number",
                                                             "default": 900}}},
        },
        {
            "name": "job_status",
            "description": (
                "What is left, what other workers are running right now, and "
                "what nobody could finish. Call this first in a new session to "
                "find out where the work stands before claiming anything."),
            "inputSchema": {"type": "object", "properties": {
                "kind": {"type": "string"}, "run": {"type": "string"}}},
        },
    ]


class Server:
    def __init__(self, db: Path):
        self.conn = leases.connect(db)
        # Stable for the life of the process. An agent asked to supply its own
        # id would supply a different one each session, and the lease it took
        # at 10:00 could not be renewed or closed at 10:20. One server process
        # is one worker for as long as it lives.
        self.worker = leases.this_worker()

    # -- tools -------------------------------------------------------------

    def project_state(self, _a: dict) -> dict:
        return leases.state(self.conn)

    def start_run(self, a: dict) -> dict:
        rid = leases.start_run(self.conn, label=a.get("label"),
                               started_by=a.get("started_by") or self.worker,
                               note=a.get("note"))
        return {"run_id": rid,
                "next": "Pass this run_id to add_jobs. Every statistic can "
                        "then be asked of this run alone."}

    def list_runs(self, a: dict) -> dict:
        return {"runs": leases.runs(self.conn, limit=int(a.get("limit", 25)))}

    def register_skill(self, a: dict) -> dict:
        return leases.register_skill(self.conn, a["name"], source=a.get("source"),
                                     version=a.get("version"), note=a.get("note"))

    def list_skills(self, _a: dict) -> dict:
        return {"skills": leases.skills(self.conn)}

    def define_kind(self, a: dict) -> dict:
        try:
            digest = leases.define(
                self.conn, a["kind"], a["instructions"],
                done_when=a.get("done_when"), returns=a.get("returns"),
                tools=a.get("tools"), skills=a.get("skills"),
                mcp=a.get("mcp"), context=a.get("context"),
                force=bool(a.get("force")))
        except ValueError as e:
            return {"ok": False, "error": "kind_in_use", "message": str(e)}
        out = {"defined": a["kind"], "digest": digest}
        unknown = [r["name"] for r in leases.resolve_skills(self.conn, a.get("skills"))
                   if r.get("unregistered")]
        if unknown:
            out["unregistered_skills"] = unknown
            out["hint"] = ("These skills are not registered, so a worker is "
                           "told to load them but not where to get them. "
                           "Call register_skill for each.")
        if not a.get("done_when"):
            out["warning"] = ("No done_when. Workers will each decide for "
                              "themselves what finished means, and they will "
                              "not agree.")
        return out

    def add_jobs(self, a: dict) -> dict:
        if leases.spec(self.conn, a["kind"]) is None:
            return {"ok": False,
                    "error": "undefined_kind",
                    "message": f"define_kind({a['kind']!r}) first — otherwise "
                               "every worker that claims one of these is handed "
                               "a bare name and no instructions."}
        added = leases.add(self.conn, a["kind"], list(a["names"]),
                           priority=int(a.get("priority", 0)),
                           meta=a.get("meta"), run=a.get("run"))
        out = {"added": added, "already_queued": len(a["names"]) - added}
        if not a.get("run"):
            out["warning"] = ("No run. These units are ungrouped, so nothing "
                              "will be able to report on this fleet as a unit "
                              "of work. Call start_run first.")
        return out

    def worker_prompt(self, a: dict) -> dict:
        kind = a.get("kind")
        if kind and leases.spec(self.conn, kind) is None:
            return {"ok": False, "error": "undefined_kind",
                    "message": f"define_kind({kind!r}) first"}
        n = int(a.get("n", 1))
        db = a.get("db", "work.db")
        return {"prompts": [
            leases.worker_prompt(self.conn, kind, db=db,
                                 worker=f"agent-{i}" if n > 1 else None)
            for i in range(1, n + 1)],
            "note": "Spawn these in ONE message so the workers run at the "
                    "same time. Each is already told to stop when the queue "
                    "is empty."}

    def job_results(self, a: dict) -> dict:
        rows = leases.results(self.conn, a.get("kind"), run=a.get("run"))
        return {"count": len(rows), "results": rows}

    def claim_job(self, a: dict) -> dict:
        got = leases.claim(self.conn, a.get("kind"), worker=self.worker,
                           lease=a.get("lease_seconds", leases.DEFAULT_LEASE),
                           n=int(a.get("n", 1)), run=a.get("run"),
                           model=a.get("model"))
        if not got:
            return {"units": [], "queue_empty": True,
                    "note": "Nothing left to claim. Stop rather than inventing "
                            "work — another worker is probably on what is left."}
        out = {"units": [{"unit_id": u.unit_id, "kind": u.kind, "name": u.name,
                          "attempts": u.attempts, "meta": u.meta,
                          "lease_seconds_left": round(u.seconds_left),
                          "instructions": u.instructions,
                          "done_when": u.done_when,
                          "returns": u.returns,
                          "tools": u.tools,
                          "skills": list(u.skills),
                          "skill_records": list(u.skill_records),
                          "mcp": u.mcp or {},
                          "context": u.context,
                          # The same thing as one block of text, because an
                          # agent handed four fields will read one of them.
                          "brief": u.brief()}
                         for u in got],
               "worker": self.worker}
        if any(not u.instructions for u in got):
            out["warning"] = ("This kind has no instructions — nobody called "
                              "define_kind. You are being handed a bare name.")
        if any(u.attempts > 1 for u in got):
            out["warning"] = ("This unit has been handed out before and never "
                              "finished. It may be the reason.")
        return out

    def finish_job(self, a: dict) -> dict:
        # Validate the follow-on stage BEFORE anything closes. Past the close
        # there is no retry: the unit is done and the agent cannot reopen it.
        try:
            leases._check_then(a.get("then"))
        except ValueError as e:
            return {"ok": False, "finished": False, "error": "invalid_then",
                    "message": f"{e} The unit is STILL YOURS: fix `then` and "
                               f"call finish_job again."}
        row = self.conn.execute("SELECT kind FROM unit WHERE unit_id = ?",
                                (a["unit_id"],)).fetchone()
        sp = leases.spec(self.conn, row["kind"]) if row else None
        declared = (sp or {}).get("returns")
        if a.get("result") is None and shape.parse(declared) is not None:
            # A missing result is a shape violation. Skipping the check when
            # there was no result taught agents to drop the result rather than
            # fix its shape.
            return {"ok": False, "finished": False, "error": "result_missing",
                    "returns": declared,
                    "message": "This kind declares a result and you sent none. "
                               "The unit is STILL YOURS: call finish_job again "
                               "with `result`."}
        if a.get("result") is not None:
            problems = shape.describe(declared, a["result"])
            if problems:
                return {"ok": False, "finished": False,
                        "error": "result_shape",
                        "returns": sp["returns"],
                        "problems": problems[:10],
                        "message": "Your result does not match the shape this "
                                   "kind declares. The unit is STILL YOURS: fix "
                                   "the shape and call finish_job again."}
        ok = leases.finish(self.conn, a["unit_id"], worker=self.worker,
                           note=a.get("note"), result=a.get("result"),
                           then=a.get("then"), tokens_in=a.get("tokens_in"),
                           tokens_out=a.get("tokens_out"), cost=a.get("cost"))
        return {"finished": True} if ok else {
            "finished": False,
            "reason": "the lease expired and another worker holds this unit — "
                      "claim a different one rather than carrying on"}

    def release_job(self, a: dict) -> dict:
        return {"released": leases.release(self.conn, a["unit_id"],
                                           worker=self.worker, note=a.get("note"))}

    def fail_job(self, a: dict) -> dict:
        return {"failed": leases.fail(self.conn, a["unit_id"], note=a["note"],
                                      worker=self.worker)}

    def heartbeat_job(self, a: dict) -> dict:
        n = leases.heartbeat(self.conn, [a["unit_id"]], worker=self.worker,
                             lease=a.get("lease_seconds", leases.DEFAULT_LEASE))
        return {"extended": True} if n else {
            "extended": False,
            "reason": "this lease is no longer yours — it expired and was "
                      "reclaimed"}

    def job_status(self, a: dict) -> dict:
        prog = leases.progress(self.conn, a.get("kind"), run=a.get("run"))
        return {
            "by_kind": prog,
            "left": sum(s[leases.OPEN] + s[leases.LEASED] for s in prog.values()),
            "in_progress_elsewhere": [
                {"name": r["name"], "kind": r["kind"], "worker": r["worker"]}
                for r in leases.leased(self.conn) if r["worker"] != self.worker],
            "could_not_finish": [{"name": r["name"], "why": r["note"]}
                                 for r in leases.failures(self.conn)],
        }

    # -- transport ---------------------------------------------------------

    def handle(self, msg: Any) -> dict | None:
        # A JSON-RPC batch is a LIST, and the spec requires a server to accept
        # the frame even to refuse it. Anything that is not an object used to
        # reach `.get` and raise AttributeError, which killed the process --
        # and with it every lease this worker was holding. `echo null |
        # fleetwright serve` was the whole exploit.
        if isinstance(msg, list):
            replies = [r for r in (self.handle(m) for m in msg) if r is not None]
            # An empty batch, or one that was all notifications, gets no reply
            # at all rather than an empty array.
            return replies or None            # type: ignore[return-value]
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        mid, method = msg.get("id"), msg.get("method")
        # No `id` means notification, and the spec is explicit: the server MUST
        # NOT reply to one. Real clients send notifications/cancelled and
        # notifications/progress routinely, and every one of them used to draw
        # a -32601 that some clients read as a desync.
        if "id" not in msg and method not in (
                "notifications/initialized", "initialized"):
            return None
        if method == "initialize":
            from . import __version__
            # Echo the client's version when we speak it, rather than always
            # answering with ours.
            want = (msg.get("params") or {}).get("protocolVersion")
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": want if want in SPOKEN else PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fleetwright", "version": __version__}}}
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tools()}}
        if method == "tools/call":
            p = msg.get("params") or {}
            name = p.get("name", "")
            fn = getattr(self, name, None)
            if fn is None or name.startswith("_") or name not in {
                    t["name"] for t in _tools()}:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"no tool {name}"}}
            try:
                out = fn(p.get("arguments") or {})
            except Exception as e:  # noqa: BLE001 - an agent needs the message, not a crash
                out = {"ok": False, "error": type(e).__name__, "message": str(e)}
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False)}],
                "isError": bool(isinstance(out, dict) and out.get("ok") is False)}}
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"unknown method {method}"}}

    def serve(self, stdin: Any = None, stdout: Any = None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                reply = self.handle(json.loads(line))
            except json.JSONDecodeError:
                # Answer rather than drop it. A client waiting on a reply that
                # never comes hangs; one that gets -32700 knows to resend.
                reply = {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32700, "message": "Parse error"}}
            except Exception as e:  # noqa: BLE001
                # A worker's MCP server dying takes its leases with it, so any
                # unhandled error is worth a reply and a live process.
                reply = {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32603, "message": f"{type(e).__name__}: {e}"}}
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()
