# MCP

```json
{
  "mcpServers": {
    "work": {"command": "superagentic", "args": ["serve", "--db", "work.db"]}
  }
}
```

Six tools:

| Tool | What it does |
|---|---|
| `claim_job` | take a unit nobody else holds, or be told the queue is empty |
| `finish_job` | mark it done; `false` means the lease was lost |
| `release_job` | hand it back unfinished, no attempt held against it |
| `fail_job` | report a unit that could not be done, with the reason |
| `heartbeat_job` | extend the lease on work still in progress |
| `job_status` | what is left, who else is working, what nobody could finish |

## The tool descriptions are the protocol

An agent will not read this file. It reads the tool descriptions, so those
carry the instructions that matter:

- **claim before starting anything** — a unit you did not claim is one two of
  you will do;
- **stop when the queue is empty**, rather than inventing work. This is the
  failure mode worth designing against: an agent with nothing to do reliably
  finds something, and what it finds is usually a unit somebody else has;
- **heartbeat** if the unit will outlive the lease;
- when `finish_job` says the lease expired, **claim a different unit** rather
  than carrying on with one you no longer own.

## The worker identity is the server's

Not the model's. An agent asked to supply its own worker id supplies a
different one every session, and the lease it took at 10:00 cannot be renewed
or closed at 10:20. One server process is one worker for as long as it lives.

## Running it beside another server

This composes rather than integrates. An agent that both records data and takes
work runs two servers:

```json
{"mcpServers": {
  "work":   {"command": "superagentic", "args": ["serve", "--db", "work.db"]},
  "claims": {"command": "xrad", "args": ["serve", "--db", "graph.db"]}
}}
```

Neither knows the other exists, which is the point — this library has no idea
what your work produces, and the store has no idea how the work was divided.
