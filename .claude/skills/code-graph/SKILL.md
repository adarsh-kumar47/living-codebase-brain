---
name: code-graph
description: Query the Living Code Base Brain Neo4j graph to answer questions about the Java codebase - who calls a method, blast radius of a change, class hierarchies, which dependency JAR provides a type, hotspots. Use this INSTEAD of grepping and reading .java files whenever the question is about code structure, callers, dependencies, or impact of a change.
---

# Code graph (Neo4j) — query this instead of reading source files

A Neo4j graph holds the **entire** Java codebase: application classes **and** all 171
dependency JARs, extracted from JVM bytecode.

**53,597 classes · 446,071 methods · 705,057 call edges**

Answer structural questions with one Cypher query rather than grepping and reading
files. Reading `.java` files to trace callers costs an order of magnitude more tokens
and cannot see inside dependency JARs at all.

## Connection

| | |
|---|---|
| Bolt | `bolt://localhost:7687` |
| Browser | `http://localhost:7474/browser/` (the bare `:7474` root returns discovery JSON, not the UI) |
| User | `neo4j` |
| Password | `lcbpassword` |

## Running a query

Use the `./cq` wrapper from the project root:

```bash
./cq "MATCH (c:Class {is_external:false}) RETURN count(c) AS app_classes;"
```

End the statement with a semicolon, and use **single quotes** for Cypher string
literals — the wrapper already uses double quotes:

```bash
./cq "MATCH (c:Class) WHERE c.fqn ENDS WITH 'OwnerRepository' RETURN c.fqn, c.source_file;"
```

`cq` uses `cypher-shell` when it is installed, and otherwise `python -m lcb.query`.
Overridable via env: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `CQ_FORMAT`.

Equivalent Python path (no shell wrapper, returns the same rows):

```bash
.venv-mac/bin/python -m lcb.query "MATCH (c:Class) RETURN count(c)"
```

## Schema

```
(:Class)    fqn, name, package, is_interface, is_abstract, is_external,
            source_file, artifact
(:Method)   fqn, owner, name, descriptor, is_static, is_abstract, is_external,
            line, line_end, artifact
(:Package)  name
(:Artifact) name, is_external
(:Author)   email, name
(:Commit)   sha, message, timestamp, additions, deletions
(:Issue)    number, title, state, opened_at, closed_at, labels
(:PR)       number, title, merged_at, files_changed

(:Package)-[:CONTAINS]->(:Class)
(:Artifact)-[:PROVIDES]->(:Class)
(:Class)-[:HAS_METHOD]->(:Method)
(:Class)-[:EXTENDS]->(:Class)
(:Class)-[:IMPLEMENTS]->(:Class)
(:Method)-[:CALLS {exact, opcode, line, via}]->(:Method)
(:Method)-[:MAY_CALL {via:'CHA'}]->(:Method)
(:Author)-[:AUTHORED]->(:Commit)
(:Commit)-[:TOUCHED {added, removed}]->(:Method)
(:Author)-[:LAST_MODIFIED]->(:Method)
(:Commit)-[:PART_OF]->(:PR)
(:PR)-[:FIXES]->(:Issue)
(:Issue)-[:CAUSED_BY {confidence}]->(:Method)
(:Commit)-[:FIXED {confidence}]->(:Method)
(:Class)-[:INJECTS {member_kind, member_name, param_index, qualifier}]->(:Class)
(:Class)-[:INJECTS_BEAN_METHOD {member_kind, member_name, param_index, qualifier}]->(:Method)
```

**Spring DI layer**: `Class.is_spring_bean`/`stereotype` and `Method.is_bean_factory`/
`bean_type` mark Spring-managed components and `@Bean` factories. `INJECTS`/
`INJECTS_BEAN_METHOD` resolve each injection point (constructor param, or an explicit
`@Autowired` field/setter) to its candidate bean(s) — recovering DI wiring the `CALLS`
graph cannot see. Multiple candidates are kept, not collapsed, same stance as `MAY_CALL`.
**A missing edge is not proof of no dependency**: PetClinic's own injections all target
Spring Data repository interfaces, which have no static implementation at all (Spring Data
proxies them at runtime) — correctly unresolved, not a gap in the extraction.

**History layer** (`Author`/`Commit`/`AUTHORED`/`TOUCHED`/`LAST_MODIFIED`) only covers
application methods — dependency JARs have no git history in this repo. Loaded by
`python -m lcb.git_ingest --repo targets/petclinic`, separately from the structural
load; it reflects the repo at ingestion time, not live.

**Fix commits** (`FIXED`) are bug fixes found from commit messages alone — most fixes
never go through a GitHub issue. `Commit.fix_kind` is `conventional` / `keyword` /
`revert`, `Commit.issue_refs` lists `#N` numbers from the subject. For "has this method
had bugs?", query both `CAUSED_BY` and `FIXED`; skip fix commits already behind an issue
with `NOT EXISTS { (c)-[:PART_OF]->(:PR)-[:FIXES]->(:Issue) }`.

**GitHub layer** (`Issue`/`PR`/`FIXES`/`PART_OF`/`CAUSED_BY`) adds bug reports and the
inference chain `Issue --FIXES-- PR --PART_OF-- Commit --TOUCHED--> Method`. Loaded by
`python -m lcb.github_ingest --repo targets/petclinic`. Only covers
merged PRs whose merge commit still exists in this clone's history — some pre-2014 PRs
don't (the repo's early history was rewritten at some point), and that's reported, not
hidden. `CAUSED_BY.confidence` ranks candidate methods against each other for a given
issue; it is not a calibrated probability.

**Conventions**

- `Method.fqn` is `<class fqn>#<name><JVM descriptor>`. The descriptor disambiguates
  overloads — match on `Method.name` when you don't care which overload, `Method.fqn`
  when you do.
- `is_external = false` → application code. `true` → from a dependency JAR.
- `CALLS.exact = true` — `INVOKESTATIC`/`INVOKESPECIAL`, the target is provable.
- `CALLS.exact = false` — `INVOKEVIRTUAL`/`INVOKEINTERFACE`, the edge points at the
  **declared** type; the runtime target depends on dispatch.
- `MAY_CALL` — Class Hierarchy Analysis expansion to concrete overrides. Use it to
  widen a blast radius; it over-approximates by design.
- `Method.line` / `Method.line_end` — first and last source line of the method body.
  Use the span to tell whether a diff hunk touches a given method:
  `m.line <= hunk_end AND m.line_end >= hunk_start`.

## Query patterns

**Who calls this method**
```cypher
MATCH (caller:Method)-[c:CALLS]->(m:Method)
MATCH (cls:Class)-[:HAS_METHOD]->(m)
WHERE cls.fqn ENDS WITH 'OwnerRepository' AND m.name = 'findById'
RETURN caller.owner, caller.name, c.line ORDER BY caller.owner;
```

**Blast radius, application code only**
```cypher
MATCH (m:Method {name:'getPet'})<-[:CALLS*1..3]-(caller:Method)
WHERE caller.is_external = false
RETURN DISTINCT caller.owner, caller.name;
```

**Which JAR provides a type** — not answerable from source at all
```cypher
MATCH (c:Class)-[:EXTENDS|IMPLEMENTS]->(p:Class)<-[:PROVIDES]-(a:Artifact)
WHERE c.fqn ENDS WITH 'OwnerRepository'
RETURN p.fqn, a.name;
```

**Application surface onto the framework**
```cypher
MATCH (c:Class {is_external:false})-[:EXTENDS|IMPLEMENTS]->(p:Class {is_external:true})
RETURN c.fqn, p.fqn ORDER BY c.fqn;
```

**Hotspots — most-called application methods**
```cypher
MATCH (m:Method)<-[:CALLS]-(caller:Method)
WHERE m.is_external = false
RETURN m.owner, m.name, count(DISTINCT caller) AS callers
ORDER BY callers DESC LIMIT 15;
```

**Churn hotspots — most-touched application methods, by commit history**
```cypher
MATCH (cm:Commit)-[:TOUCHED]->(m:Method)
RETURN m.owner, m.name, count(cm) AS commits
ORDER BY commits DESC LIMIT 15;
```

**Who last touched a method**
```cypher
MATCH (a:Author)-[:LAST_MODIFIED]->(m:Method {name:'getPet'})
RETURN a.name, a.email;
```

**Which methods a bug report is most likely rooted in**
```cypher
MATCH (i:Issue {number:2600})-[c:CAUSED_BY]->(m:Method)
RETURN m.owner, m.name, c.confidence ORDER BY c.confidence DESC;
```

**What a Spring component actually depends on**
```cypher
MATCH (c:Class {name:'OwnerController'})-[r:INJECTS|INJECTS_BEAN_METHOD]->(target)
RETURN r.member_name, r.param_index, target.fqn;
```

## Visualizing an answer (do not skip this)

Answers about structure land far better as a picture. `cq` is a text shell and
always prints a table, so alongside the terminal answer, offer a **browser
query** the user can paste into http://localhost:7474/browser/ (`neo4j` / `lcbpassword`).

The only rule: Neo4j Browser draws a picture when the query returns **graph
elements**, and a table when it returns scalars.

```cypher
RETURN c.fqn, m.name          // table - cannot be drawn
RETURN c, h, m                // picture - nodes plus the arrow between them
RETURN p                      // picture - where p = (a)-[:CALLS]->(b)
```

Use `OPTIONAL MATCH` for the caller half when absence is the point - a method
node with no inbound `CALLS` arrow is visible proof of dead code.

```cypher
MATCH (o:Class {fqn:'...Owner'})-[h:HAS_METHOD]->(m:Method {name:'getPet'})
OPTIONAL MATCH p=(:Method)-[:CALLS]->(m)
RETURN o, h, m, p;
```

Verified, ready-to-paste examples live in `demo/VISUAL.md`.

## Rules

1. **Always `LIMIT`** on anything that could return many rows. The graph has 705k call
   edges; an unbounded `MATCH (a)-[:CALLS*1..3]->(b) RETURN a,b` will hang.
2. **Filter `is_external = false`** when the question is about application code.
   Without it, results are dominated by Spring internals.
3. **Cite `source_file` and `line`** in answers so the user can verify the claim.
4. **Read-only.** Never `CREATE`, `MERGE`, `SET` or `DELETE` — the graph is a derived
   artifact rebuilt by `python -m lcb.load`.
5. If a query errors, read the message and fix the Cypher; don't fall back to grepping.
6. **After editing Java source, run the `check_changes` MCP tool** (or
   `python -m lcb.check --repo <repo>`) before calling the work done. If it flags a
   method, read the past fix it cites, make sure the change doesn't undo it, and tell
   the user.

## What the graph cannot tell you

State these rather than guessing around them:

- **JDK calls are not indexed** — `java.*` and `javax.*` targets have no node.
- **Reflection and dynamic proxies are invisible** to static analysis; a bean wired at
  runtime via either has no edge. Spring DI is now *partly* recovered (`INJECTS`/
  `INJECTS_BEAN_METHOD`, from stereotype annotations and constructor/field/setter
  injection points) — but only where a concrete, statically-present implementation
  exists. Spring Data repository interfaces (proxied at runtime, no static impl at all)
  and anything wired via XML/arbitrary non-stereotyped classes remain invisible.
- **`MAY_CALL` over-approximates** — it lists every override that *could* be dispatched,
  not the ones that actually are.
- The graph reflects the **last compiled build**, not unsaved editor buffers.

For implementation logic *inside* a method body, read the source file — the graph stores
structure and relationships, not statements.
