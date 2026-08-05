# Skills

`superagentic/SKILL.md` teaches an agent how to run a fleet: define the work,
enqueue the units, spawn workers with a generic prompt, collect the results.

## Installing it

```bash
superagentic install-skill          # this project, into .claude/skills/
superagentic install-skill --user   # every project on this machine
```

Then ask Claude in English: *"audit the 300 files in claims/ with 6 agents"*.
The skill tells it to define the work, enqueue it, spawn the workers in one
message, wait, and check the database rather than the agents' own reports.

## One copy, inside the package

The skill lives at `src/superagentic/skill/SKILL.md` and ships inside the
wheel, which is how `install-skill` can write it at runtime. It is deliberately
**not** duplicated here: a second copy is a copy that drifts from the CLI it
documents, and this project has already been bitten by that more than once.

## Why the skill and the tool descriptions both exist

They are read by different agents at different moments.

The **skill** is read by the orchestrator, before any work exists. It is about
setting a fleet up, and it is the only place that says to spawn workers in one
message rather than several.

The **MCP tool descriptions** are read by each worker, at the moment it calls
something, with no other context and no guarantee the skill was ever loaded.

Some duplication between them is deliberate. A worker that never saw the skill
still has to be told to stop when the queue is empty.
