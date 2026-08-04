# Skills

`superagentic/SKILL.md` teaches an agent how to run a fleet: define the work,
enqueue the units, spawn workers with a generic prompt, collect the results.

## Installing it

For one project:

```bash
mkdir -p .claude/skills
cp -r "$(python -c 'import superagentic,pathlib;print(pathlib.Path(superagentic.__file__).parent)')/../../skills/superagentic" .claude/skills/
```

Or, from a clone:

```bash
cp -r skills/superagentic /path/to/your/project/.claude/skills/
```

For every project on this machine:

```bash
mkdir -p ~/.claude/skills
cp -r skills/superagentic ~/.claude/skills/
```

Then `/superagentic` in Claude Code, or just describe the task — the
`description` in the frontmatter is written so the skill is offered when
someone is about to spawn several agents over a list.

## Why the skill and the tool descriptions both exist

They are read by different agents at different moments.

The **skill** is read by the agent doing the orchestrating, *before* any work
exists — it is about setting a fleet up, and it is the only place the worker
prompt template lives.

The **MCP tool descriptions** are read by each worker, at the moment it calls
something, with no other context. They cannot assume the skill was ever loaded.

Some duplication between them is deliberate. A worker that never saw the skill
still has to be told to stop when the queue is empty.
