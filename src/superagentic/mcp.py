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

    $ superagentic serve --db work.db
    # or in an MCP client config:
    #   "work": {"command": "superagentic", "args": ["serve", "--db", "work.db"]}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import leases

PROTOCOL = "2024-11-05"


def _tools() -> list[dict]:
    # Written for an agent reading tool descriptions rather than documentation,
    # because nothing else will tell it the protocol. Every description says
    # what to do next and what the failure means.
    return [
        # -- the orchestrator's half. An agent that spawns a fleet defines the
        # work here first, so the workers it spawns need no prompt beyond
        # "claim work and do it".
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
                              "Which tools or MCP servers to use."}}},
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
                    "meta": {"type": "object"}}},
        },
        {
            "name": "job_results",
            "description": (
                "Collect what the fleet produced. For the agent that spawned "
                "the workers and now has to assemble their output."),
            "inputSchema": {"type": "object", "properties": {
                "kind": {"type": "string"}}},
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
                "kind": {"type": "string"}}},
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

    def define_kind(self, a: dict) -> dict:
        leases.define(self.conn, a["kind"], a["instructions"],
                      done_when=a.get("done_when"), returns=a.get("returns"),
                      tools=a.get("tools"))
        out = {"defined": a["kind"]}
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
                           meta=a.get("meta"))
        return {"added": added, "already_queued": len(a["names"]) - added}

    def job_results(self, a: dict) -> dict:
        rows = leases.results(self.conn, a.get("kind"))
        return {"count": len(rows), "results": rows}

    def claim_job(self, a: dict) -> dict:
        got = leases.claim(self.conn, a.get("kind"), worker=self.worker,
                           lease=a.get("lease_seconds", leases.DEFAULT_LEASE),
                           n=int(a.get("n", 1)))
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
        ok = leases.finish(self.conn, a["unit_id"], worker=self.worker,
                           note=a.get("note"), result=a.get("result"),
                           then=a.get("then"))
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
        prog = leases.progress(self.conn, a.get("kind"))
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

    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            from . import __version__
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "superagentic", "version": __version__}}}
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
                continue
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()
