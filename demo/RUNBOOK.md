# A/B demo — same question, with and without the code graph

This measures **real token usage** recorded by Claude Code, not an estimate.

## Why this is a fair test

Arm A runs from the project root, where two things give Claude the graph:

- `.claude/skills/code-graph/SKILL.md`: the schema, the `./cq` command and the connection
  details, plus the instruction to query rather than read files.
- `.mcp.json`: the `living-codebase-brain` MCP server (`get_schema`, `run_cypher`,
  `check_changes`). If it's missing or points at another machine's paths, regenerate it
  with `.venv-mac/bin/python -m lcb mcp-config --repo targets/petclinic`.

Arm B runs from `targets/petclinic`, which has **neither**: no skill, no `.mcp.json`. The
capability is genuinely absent from that session, not merely discouraged. Telling a
model not to use a tool achieves nothing; the difference here is in what exists.

Both arms can read and grep the Java source. Only arm A also has the graph.

---

## The question (paste it identically into both arms)

```
Which methods call OwnerRepository.findById? List every caller with its
class, method name, and the line number of the call.
```

It is answerable both ways, so this measures **cost**, not capability. Do not reword it
between arms; a changed prompt invalidates the comparison.

---

## Procedure

Run each arm in a **fresh** session. Do not use `--continue` or `--resume`: carried-over
context pollutes the token count. Run `./demo/start.sh` first, so the graph is up.

### Arm A: with the graph

```bash
cd "/path/to/Living Codebase brain"
claude
```

Before asking, check that both are live: `/mcp` should list `living-codebase-brain`, and
the `code-graph` skill should appear in the skills list. Paste the question, let it
finish, then `/exit`.

Expect one or two graph queries and an answer.

### Arm B: without the graph

```bash
cd "/path/to/Living Codebase brain/targets/petclinic"
claude
```

`/mcp` should show no graph server and no `code-graph` skill. Paste the **same**
question, let it finish, then `/exit`.

Expect a grep, then a file read per match, then the answer.

### Compare

```bash
cd "/path/to/Living Codebase brain"
.venv-mac/bin/python -m lcb.compare_runs --list
```

Find the two sessions you just ran (newest first; the prompt column identifies them),
then:

```bash
.venv-mac/bin/python -m lcb.compare_runs --graph <session-id> --baseline <session-id>
```

Session-id prefixes work; the first 8 characters are enough.

---

## What the output shows

- **TOTAL TOKENS** for each arm, and the percentage saved.
- **Tool calls.** Arm B typically makes several (a grep, then a read per matching
  file); arm A makes one or two.
- **Per-turn input growth**, the important one. Arm B re-sends its accumulated
  transcript every turn, so its context climbs with each file it reads. Arm A stays
  flat.
- **Both final answers**, so you can confirm they answered equivalently.

## Reading it honestly

- Claude Code caches aggressively, so most input arrives as `cache_read`. The table
  separates fresh input from cache reads; quote **TOTAL TOKENS** as the headline and be
  ready to explain the split.
- Session startup overhead (system prompt, tool definitions) is charged to both arms.
  On a short question it is a large fixed cost that **understates** the difference. Say
  so before someone points it out.
- One question is one data point. Run two or three different questions if time allows.

## If a reviewer asks to try their own question

Let them. The graph answers questions nobody pre-baked, which is the point of exposing
Cypher rather than fixed tools. Good ones:

- "What would break if I changed the signature of `Owner.getPet`?"
- "Which application classes extend something from a dependency JAR?"
- "Which JAR provides the interface `OwnerRepository` extends?" Arm B cannot answer this
  at all: the `.java` file names the import but never the artifact.
- "Has `processFindForm` been fixed before, and by whom?" This needs git history, which
  arm B would have to dig through with `git log` itself.
