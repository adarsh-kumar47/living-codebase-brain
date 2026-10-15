# What Cypher is, and why your user never writes it

Written for you, to read before presenting. Section 2 is the one your teacher is
most likely to probe, and it's the one that's easy to get wrong.

---

## 1. What Cypher is

**SQL is the query language for tables. Cypher is the query language for
graphs.** Neo4j is a graph database, so it speaks Cypher.

The reason Cypher looks strange at first is that it's deliberately shaped like a
picture of what you're looking for:

```
(caller)-[:CALLS]->(target)
```

Read that literally as ASCII art:

- `(caller)` — a circle. A **node**. Here, a method.
- `-[:CALLS]->` — an arrow labelled `CALLS`. A **relationship**.
- `(target)` — another node, the method being called.

So that fragment means *"some method that calls some other method."* You are
drawing the shape you want to find, and the database finds every place in the
graph that matches that shape.

### A real query, line by line

This is the one behind "who calls `findById`":

```cypher
MATCH (caller:Method)-[c:CALLS]->(m:Method)
MATCH (cls:Class)-[:HAS_METHOD]->(m)
WHERE cls.fqn ENDS WITH 'OwnerRepository' AND m.name = 'findById'
RETURN caller.owner, caller.name, c.line
ORDER BY caller.owner;
```

| line | what it does |
|---|---|
| `MATCH (caller:Method)-[c:CALLS]->(m:Method)` | Find every place one method calls another. `:Method` is the node's **label** (its type). `c` names the arrow so we can read data off it later. |
| `MATCH (cls:Class)-[:HAS_METHOD]->(m)` | Also require that `m` belongs to some class `cls`. |
| `WHERE ...` | Narrow it down: that class must be `OwnerRepository`, and the method must be named `findById`. |
| `RETURN caller.owner, caller.name, c.line` | Show three columns: the calling class, the calling method, and the line number — which was stored **on the arrow** when we extracted it. |
| `ORDER BY caller.owner` | Sort the output. |

If you only remember three words: **MATCH** finds the shape, **WHERE** filters
it, **RETURN** picks the columns. That's the same shape as SQL's
`FROM` / `WHERE` / `SELECT`.

### Why a graph instead of a table

"Who calls `findById`?" is one hop. "What would break if I change it?" is
*"follow CALLS arrows backwards up to 3 times"* — in Cypher that's `[:CALLS*1..3]`,
one extra character over a single hop. In SQL that's three self-joins, and at
unknown depth it isn't really expressible at all. Call graphs are naturally a
graph, so a graph database is the honest fit — that's the design argument.

---

## 2. Why the user never writes Cypher

**This is the question to be ready for, and the answer is: they don't.**

If your teacher walks away thinking your project requires developers to learn
Cypher, he's understood it as a database assignment rather than an AI one. The
actual flow has four steps:

```
  1. Developer types a question in plain English
         "Which methods call OwnerRepository.findById?"
                          |
                          v
  2. Claude writes the Cypher      <-- this is the part you built
         MATCH (caller:Method)-[c:CALLS]->(m:Method) ...
                          |
                          v
  3. Neo4j runs it and returns rows
         OwnerController.showOwner, line 172 ...
                          |
                          v
  4. Claude answers in plain English, citing file and line
```

The developer sees step 1 and step 4. Steps 2 and 3 are machinery.

### So what did you actually build?

The thing that makes step 2 possible. Claude cannot write Cypher against a
schema it has never seen — it would guess at label names and fail. Two files fix
that:

- **`.claude/skills/code-graph/SKILL.md`** — teaches Claude the schema: the node
  labels, the relationship types, the property names, worked query patterns, and
  the rules (always `LIMIT`, filter `is_external`, cite line numbers).
- **`.mcp.json` → `lcb/mcp_server.py`** — exposes `get_schema` and `run_cypher`
  as tools Claude can actually call, with writes rejected.

Together those turn "Claude can't answer this without reading 9 files" into
"Claude writes one query." **That translation layer is the project.** The graph
is the data; the skill and the MCP server are what make it usable by an AI.

### The obvious follow-up: "why not just build fixed functions?"

A fair question — why expose a whole query language instead of a few tools like
`getCallers(method)` and `getBlastRadius(method)`?

Because fixed functions can only answer the questions you thought of in advance.
The moment someone asks something you didn't pre-build — *"which of our classes
extend something from a JAR, and which JAR is it?"* — a fixed-function API is
stuck, and you're writing new code. Exposing Cypher means the system answers
questions nobody anticipated, including ones your teacher invents on the spot.

That's exactly why section 5 of the run sheet invites him to make one up. A
pre-baked demo can't survive that. This one can.

---

## 3. Vocabulary, if he asks

| term | meaning here |
|---|---|
| **node** | A thing. A class, a method, a package, a JAR. |
| **label** | A node's type: `:Class`, `:Method`, `:Artifact`. |
| **relationship** | A directed arrow between two nodes: `:CALLS`, `:EXTENDS`, `:HAS_METHOD`. |
| **property** | Data stored on a node or an arrow: `fqn`, `line`, `is_external`. |
| **graph database** | A database that stores relationships directly, instead of reconstructing them with joins. |
| **Cypher** | Neo4j's query language. |
| **bytecode** | Compiled `.class` output. The extractor reads this rather than `.java`, which is why it can see inside dependency JARs. |
| **MCP** | Model Context Protocol — the standard by which an AI assistant is given tools. Here it's how Claude gets `run_cypher`. |
