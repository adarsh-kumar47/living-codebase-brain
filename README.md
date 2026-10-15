      # Living Code Base Brain

**A live code knowledge graph for change-impact and risk analysis.**

A Neo4j graph built from JVM bytecode — application classes *and* every dependency JAR —
exposed to an LLM that writes its own Cypher. Answers "what breaks if I change this?"
with a traversable path and a citable line number, at roughly a tenth of the token cost
of reading the source.

---

## Contents

1. [The problem](#1-the-problem)
2. [High-level architecture](#2-high-level-architecture)
3. [Technology stack](#3-technology-stack)
4. [Low-level design](#4-low-level-design)
5. [Results](#5-results)
6. [Running the pipeline](#6-running-the-pipeline)
7. [Design decisions and rejected alternatives](#7-design-decisions-and-rejected-alternatives)
8. [Anticipated cross-questions](#8-anticipated-cross-questions)
9. [Known limitations](#9-known-limitations)
10. [The GitHub / history layer](#10-the-github--history-layer)
11. [Roadmap](#11-roadmap)

---

## 1. The problem

When a developer changes a method, two questions are hard to answer quickly:

1. **What else depends on this?** — structural impact
2. **Has this area historically been fragile?** — who understands it, what broke here before

Today those answers live in three disconnected places: the code, the git history, and the
issue tracker. Developers stitch them together by hand — reading code, running `git blame`,
searching old issues.

An LLM asked the same question without help does something expensive and unreliable: it
greps for a symbol, reads every file that matched, and infers the answer from what it saw.
On a Java codebase that misses everything inside dependency JARs, because there is no
source to grep.

**This project replaces that inference with a traversal.** The structure is extracted
once, exactly, into a graph. The LLM queries the graph.

**Current scope:** the structural layer is complete, including dependencies. The historical
layer ([§10](#10-the-github--history-layer)) is also built: git commits, authors and blame,
plus GitHub issues and PRs, are linked down to individual methods, and a risk score
combines them. The risk score runs end to end but is not yet validated, because PetClinic
is too small for the back-test to be conclusive.

---

## 2. High-level architecture

```
                    ┌──────────────────────────────────────┐
  Spring PetClinic  │  target/classes/*.class   (app)      │
     (Maven)   ────▶│  dependency JARs          (171)      │
                    └──────────────────┬───────────────────┘
                                       │
                            ┌──────────▼──────────┐
                            │  ASM Extractor      │  Java, ~350 LOC
                            │  bytecode → CSV     │  8.2 s
                            └──────────┬──────────┘
                                       │  classes / methods / calls
                                       │  extends / implements
                            ┌──────────▼──────────┐
                            │  Loader (Python)    │  batched UNWIND
                            │  + CHA post-pass    │  ~2 min
                            └──────────┬──────────┘
                                       │
                            ┌──────────▼──────────┐
                            │     Neo4j 5.26      │  53.6k classes
                            │       (Cypher)      │  446k methods
                            └──────────┬──────────┘  705k call edges
                                       │
                   ┌───────────────────┼───────────────────┐
                   │                   │                   │
         ┌─────────▼────────┐ ┌────────▼───────┐ ┌─────────▼────────┐
         │   MCP server     │ │  code-graph    │ │  Token benchmark │
         │ get_schema       │ │  skill + ./cq  │ │  A/B harness     │
         │ run_cypher       │ │  (cypher-shell)│ │                  │
         └─────────┬────────┘ └────────┬───────┘ └──────────────────┘
                   └───────────┬───────┘
                               ▼
                         LLM writes Cypher
```

**Four layers, matching the original proposal:**

| Layer | Status | What it does |
|---|---|---|
| Structural | ✅ built | Bytecode → classes, methods, calls, inheritance |
| Storage | ✅ built | Neo4j, one graph, Cypher traversal |
| Reasoning | ✅ built | MCP server + skill; LLM writes its own Cypher |
| Historical / social | 🟡 built, unvalidated | Git + GitHub layers + risk score built ([§10.3](#103-data-sources-and-linking--git-half--done-github-half--done)–[§10.5](#105-evaluation-historical-back-test---run-result-inconclusive-honestly)); back-test inconclusive on this small a repo |

---

## 3. Technology stack

### Core

| Component | Choice | Version | Why |
|---|---|---|---|
| Bytecode reader | **ASM** (OW2) | 9.7.1 | Streaming visitor API, industry standard (used by the JDK itself). Exact call sites with no type inference |
| Graph database | **Neo4j Community** | 5.26.0 | Native graph storage; variable-length traversal is a first-class query, not a recursive join |
| Query language | **Cypher** | — | Declarative path matching; an LLM writes it well |
| Extractor runtime | **Java** | JDK 21 | ASM is a Java library; no FFI boundary |
| Orchestration | **Python** | 3.13 | Neo4j driver, MCP SDK, tiktoken all first-class |
| Graph driver | `neo4j` | 5.26.0 | Official Bolt driver, batched transactions |
| LLM interface | **MCP** (`mcp`) | 2.2.0 | Standard protocol; works with Claude Code and any MCP client |
| Token counting | `tiktoken` | 0.8.0 | Deterministic offline counting for the static benchmark |
| Target build | **Maven wrapper** | — | `mvnw` bootstraps Maven; no global install needed |

### Why not the obvious alternatives

| Considered | Rejected because |
|---|---|
| **tree-sitter-java** | Syntax only, no type resolution. Cannot resolve `foo.bar()` to a declaration, which is the entire job |
| **Decompilers** (CFR, Vineflower, Procyon) | Lossy round-trip: bytecode → approximate source → re-parse. Adds failure modes on optimised classes and discards type information bytecode already carries |
| **JavaParser + SymbolSolver** | Good, but needs source. Dependencies ship as JARs — no source to parse |
| **Soot / WALA** | Full points-to analysis; heavyweight dependency for value we get from CHA |
| **jdeps** | Package-level granularity only; we need method-level |
| **SQL / document store** | Variable-length traversal (`*1..3`) becomes recursive CTEs or N round-trips |

---

## 4. Low-level design

### 4.1 Extraction — [`extractor/src/Extractor.java`](extractor/src/Extractor.java)

Walks a compiled classes directory and every JAR, feeding each `.class` stream to
`ClassReader.accept(visitor, SKIP_FRAMES)`.

**`GraphClassVisitor`** captures class shape:

| ASM callback | Produces |
|---|---|
| `visit(...)` | class identity, access flags, `EXTENDS`, `IMPLEMENTS` |
| `visitSource(...)` | source file name |
| `visitAnnotation(...)` | Spring stereotype, if present — phase 10 |
| `visitField(...)` | a `GraphFieldVisitor` per field, for `@Autowired` field injection — phase 10 |
| `visitMethod(...)` | a `GraphMethodVisitor` per method; registers constructor parameter types immediately (known from the descriptor alone) as injection candidates — phase 10 |
| `visitEnd()` | **writes the class row**, plus the bean row and resolved implicit/explicit constructor injection, once every constructor has been seen |

The class row is written at `visitEnd()`, not `visit()`, because ASM emits
`visitSource()` *after* `visit()` — writing early leaves `source_file` empty and destroys
evidence citation. Constructor injection waits for the same reason: which constructor (if
any) Spring would implicitly autowire is only decidable once all of a class's constructors
have been seen.

**`GraphMethodVisitor`** captures behaviour:

| ASM callback | Produces |
|---|---|
| `visitLineNumber(...)` | tracks current line; retains the minimum as `line` and the maximum as `line_end` |
| `visitAnnotation(...)` | `@Bean` factory methods; `@Autowired` setter injection (direct, self-contained — unlike the constructor case, no class-level disambiguation is needed); marks an explicitly-`@Autowired` constructor on the owning class visitor — phase 10 |
| `visitMethodInsn(...)` | one `CALLS` row per call site, with opcode and line |
| `visitInvokeDynamicInsn(...)` | lambda bodies via the bootstrap method handle |
| `visitEnd()` | **writes the method row** (needs the line, known only after the body); registers the `@Bean` return type, if any |

Array owners (`[I.clone()`) are skipped — they have no class node, and emitting the edge
would dangle.

**Output:** eight CSVs plus `extract_stats.json` (five structural, three from phase 10 —
`beans.csv`, `bean_methods.csv`, `injections.csv`). Every field is quoted and internal
quotes doubled, so JVM descriptors survive intact.

### 4.2 Node identity

```
Class.fqn     org.springframework.samples.petclinic.owner.Owner
Method.fqn    org.springframework.samples.petclinic.owner.Owner#getPet(Ljava/lang/String;)L…/Pet;
              └────────────── class fqn ──────────────┘ └ name ┘└──── JVM descriptor ────┘
```

The descriptor is the key design choice. Java allows overloads:

```java
public Pet getPet(String name)            // #getPet(Ljava/lang/String;)L…/Pet;
public Pet getPet(String name, boolean b) // #getPet(Ljava/lang/String;Z)L…/Pet;
```

Because the descriptor is part of the identity, these are **distinct nodes with no extra
work**. A source-parsing approach would have to resolve argument types itself to tell them
apart. Bytecode hands it over.

### 4.3 Graph schema

```cypher
(:Artifact {name, is_external})
(:Package  {name})
(:Class    {fqn, name, package, is_interface, is_abstract,
            is_external, source_file, artifact, is_spring_bean, stereotype})
(:Method   {fqn, owner, name, descriptor, is_static, is_abstract,
            is_external, line, line_end, artifact, is_bean_factory, bean_type})

(:Package)-[:CONTAINS]->(:Class)
(:Artifact)-[:PROVIDES]->(:Class)
(:Class)-[:HAS_METHOD]->(:Method)
(:Class)-[:EXTENDS]->(:Class)
(:Class)-[:IMPLEMENTS]->(:Class)
(:Method)-[:CALLS {exact, opcode, line, via}]->(:Method)
(:Method)-[:MAY_CALL {via:'CHA'}]->(:Method)
(:Class)-[:INJECTS {member_kind, member_name, param_index, qualifier}]->(:Class)
(:Class)-[:INJECTS_BEAN_METHOD {member_kind, member_name, param_index, qualifier}]->(:Method)
```

`is_spring_bean`/`stereotype`/`is_bean_factory`/`bean_type`/`INJECTS`/`INJECTS_BEAN_METHOD`
are phase 10 — [§4.6](#46-dependency-injection-recovery-phase-10).

Uniqueness constraints on `Class.fqn`, `Method.fqn`, `Package.name`, `Artifact.name`.
In Neo4j a uniqueness constraint creates its own backing index, so edge insertion — which
matches endpoints by `fqn` — is index-backed rather than a scan.

`is_external` is the pivot that makes "my code vs. the framework" a property filter
instead of a string prefix match.

### 4.4 Virtual dispatch — the accuracy centrepiece

Bytecode gives the **declared** target. What actually runs depends on dispatch.

| Opcode | Meaning | Edge |
|---|---|---|
| `INVOKESTATIC` | static method | `CALLS {exact: true}` |
| `INVOKESPECIAL` | constructor, `super.`, private | `CALLS {exact: true}` |
| `INVOKEVIRTUAL` | instance method on a class | `CALLS {exact: false}` |
| `INVOKEINTERFACE` | method on an interface | `CALLS {exact: false}` |
| `INVOKEDYNAMIC` | lambda / method reference | resolved via bootstrap handle |

`exact: true` means **provably this target**. `exact: false` means the edge points at the
declared type and the runtime target is one of its overrides.

**Class Hierarchy Analysis** closes the gap. For an inexact call to `Base.m()`, every
subtype of `Base` declaring a method with the same name *and descriptor* is a candidate:

```cypher
MATCH (src:Method)-[:CALLS {exact:false}]->(tgt:Method)
MATCH (base:Class)-[:HAS_METHOD]->(tgt)
CALL (src, tgt, base) {
    MATCH (sub:Class)-[:EXTENDS|IMPLEMENTS*1..3]->(base)
    MATCH (sub)-[:HAS_METHOD]->(impl:Method)
    WHERE impl.name = tgt.name AND impl.descriptor = tgt.descriptor AND impl <> tgt
    MERGE (src)-[:MAY_CALL {via:'CHA'}]->(impl)
} IN TRANSACTIONS OF 500 ROWS
```

**CHA is scoped deliberately to two directions:**

1. calls originating in application code — *"what might my call actually hit"*
2. any caller into an application override — *"where does the framework call into us"*

Expanding every virtual call inside 53k dependency classes would add millions of edges
inside Spring internals that no query traverses. The scoping is a stated design decision,
not an oversight — and it is why `MAY_CALL` is only 41 edges on PetClinic.

CHA **over-approximates**: it lists every override that *could* be dispatched, not those
that are. That is the correct direction of error for risk analysis — a blast radius should
not miss a caller.

### 4.5 Loading — [`lcb/load.py`](lcb/load.py)

Load order matters: classes → packages/artifacts → methods → inheritance → calls → CHA.
Edges can only be created once both endpoints exist.

Three optimisations, each load-bearing at 1.4M rows:

| Technique | Detail |
|---|---|
| **Batched `UNWIND`** | 5,000 rows/tx for nodes, 20,000 for call edges. One round trip per batch instead of per row |
| **`CREATE` not `MERGE` for calls** | `MERGE` scans existing relationships for every row. Rows are deduplicated in Python on `(src, dst, line, opcode)` first — 1,385,958 → 1,251,830 — then created blind. Safe because `--wipe` guarantees a clean graph |
| **`CALL … IN TRANSACTIONS`** | CHA writes in 500-row sub-transactions, so a global expansion never builds one enormous transaction |

**Edges are only created when both endpoints exist.** A call into `java.lang.String` has
no target node, so no edge is written — and the count is reported rather than hidden.
Inventing placeholder nodes for unindexed types would inflate the graph with data we
never extracted.

### 4.5a Incremental loading (phase 9)

A `--wipe` reload rewrites every one of ~1.25M edges even when a developer changed one
method in one class — on PetClinic that's **~70s** to reflect a one-line edit, which
defeats the point of a *living* codebase graph. Extraction itself stays full every run
(it is already cheap, ~8s even for 171 JARs); what becomes incremental is the Neo4j
**write**.

Each class gets a content signature: a hash of its own row plus every method, call,
inheritance, bean and injection row that belongs to it (each group sorted first, so row
order out of the extractor never matters), stored in `manifest.json` next to the CSVs. A
rerun without `--wipe` diffs fresh signatures against that manifest — an unchanged class
is never sent to Neo4j at all. On the real test (one line changed in `Owner.java`,
recompiled, re-extracted, reloaded): **1 of 53,597 classes flagged changed, 7.0s** instead
of 70s.

**Method node identity is preserved across a reload, edges are not.** A changed class's
Method *nodes* are updated via `MERGE` (same as always), not deleted and recreated — so
`TOUCHED`/`LAST_MODIFIED`/`CAUSED_BY` edges that `git_ingest`/`github_ingest` attached to
an unchanged-signature method survive a structural reload of its own class, verified
directly: after two incremental reloads of `Owner`, `getPet(Integer)`'s `LAST_MODIFIED`
edge (`Moritz Halbritter`) and 3 `TOUCHED` commits were untouched, because that specific
method's body didn't change even though its class did. Only a method's *outgoing*
`CALLS`/`MAY_CALL`/`EXTENDS`/`IMPLEMENTS`/`INJECTS` edges are cleared and rewritten for a
changed class, since a method's body — hence its calls — can change without its fqn
changing. A method genuinely removed from the class (tracked by diffing the manifest's
per-class method list, not just its hash) is `DETACH DELETE`d outright, correctly taking
any history with it, since there is nothing left for that history to describe.

CHA and the summary stats are always recomputed in full on every load, incremental or
not — both are cheap (well under a second — see §5), so there is no payoff for the extra
complexity of making them incremental too.

**A real bug, caught by testing the fallback path, not assumed safe:** if `manifest.json`
is missing or deleted but the graph was *not* wiped, every class must be treated as
"changed" — but the first version of this skipped the stale-edge-clearing step in exactly
that case (it only ran when a prior manifest was found), which would have let the
blind-`CREATE` calls step silently duplicate every one of ~1.25M existing edges. Fixed by
always clearing scoped stale state when `--wipe` wasn't passed, manifest or not — clearing
is a no-op on a genuinely empty graph, so there is no cost to always doing it. A second,
separate bug surfaced testing *that* fix: `clear_stale`/`remove_deleted` match on
`Method.owner`, which had no index — fine for the few classes a normal incremental run
touches, but a full label scan per `UNWIND` row at all-53,597-changed scale, which hung
for minutes before being killed. Fixed with `CREATE INDEX method_owner`. Both are stated
here rather than smoothed over, in the same spirit as the DI false-positive flood caught
in [§4.6](#46-dependency-injection-recovery-phase-10): testing the edge case is what found
the bug, not code review.

### 4.6 Dependency injection recovery (phase 10)

Reflection and dynamic proxies are invisible to static bytecode analysis by nature — a
bean wired at runtime has no call site. But annotations are themselves ordinary bytecode
attributes, visible to the same ASM visitor doing everything else, which recovers part of
that gap: not call edges, but **which concrete bean satisfies which injection point.**

**What's detected**, by literal annotation descriptor match rather than resolving Spring's
own meta-annotations (every major Spring static-analysis tool takes the same shortcut —
the alternative is parsing `@Service`'s own class file to confirm it is meta-annotated
`@Component`, for no payoff beyond the stereotype name itself):

| Signal | Detected via | Stored as |
|---|---|---|
| Bean classes | `@Component`/`@Service`/`@Repository`/`@Controller`/`@RestController`/`@Configuration` on a class | `Class.is_spring_bean`, `Class.stereotype` |
| `@Bean` factory methods | `@Bean` on a method | `Method.is_bean_factory`, `Method.bean_type` (its return type) |
| Constructor injection | the sole parameterized constructor of a bean class — **the common case, and the only one PetClinic itself uses**; Spring auto-wires it with no annotation needed at all | `injections.csv` → resolved below |
| Field / setter injection | an explicit `@Autowired` (`@Qualifier` value also captured on fields) | same |

**The gate that mattered in practice:** the constructor-injection heuristic is scoped to
classes with `stereotype != null`. Without that gate, the first version of this built
**38,733** spurious "injection points" — Spring only ever calls a class's constructor
itself for a class it manages as a bean, and the unscoped heuristic was firing for every
ordinary class anywhere in the 53k-class index with exactly one constructor, the vast
majority of them unrelated DTOs, exceptions and library classes that have nothing to do
with DI. Scoped, the real count is 91.

**Resolution**, in [`lcb/load.py`](lcb/load.py), mirrors CHA's stance exactly: an injection
point's declared type is matched against bean classes reachable via `EXTENDS`/`IMPLEMENTS`
(→ `INJECTS`) or against `@Bean` methods with a matching return type (→
`INJECTS_BEAN_METHOD`). **Multiple candidates are kept, not collapsed** — the same
over-approximation stance as `MAY_CALL`, for the same reason.

**Real result on PetClinic:** 192 beans (9 application, the rest Spring Boot
autoconfiguration classes also present in the indexed JARs), 506 `@Bean` methods, 91
injection points — of which **zero** resolved to a bean class, and 24 resolved to a `@Bean`
method. The zero is not a bug: every one of PetClinic's own injection points (verified
against source — `OwnerController(OwnerRepository)`, `PetController(OwnerRepository,
PetTypeRepository)`, etc.) targets a **Spring Data repository interface**, which has no
static implementation at all — Spring Data generates the implementing proxy at runtime,
which is exactly the kind of wiring this technique cannot see by construction, not a gap
in the extraction. The 24 resolved edges are real, just not from application code: e.g.
`AbstractTransactionalJUnit4SpringContextTests.setDataSource(DataSource)` resolves to
**four** different autoconfiguration classes' `dataSource()` `@Bean` methods — correctly
kept as four candidates, since which one wins at runtime depends on conditions
(`@ConditionalOnMissingBean`) this static view cannot evaluate.

**What this does not do:** it does not rewire the `CALLS` graph at the call-site level.
Where an interface has exactly one implementation, CHA already resolves that unambiguously
with no annotation knowledge required; DI resolution only adds information beyond CHA when
an injection point's declared type has *multiple* candidate beans and Spring's
configuration — not visible here — picks one. `INJECTS`/`INJECTS_BEAN_METHOD` expose that
ambiguity rather than silently resolving it, since resolving it correctly would require
information (condition evaluation, `@Primary`, active profiles) this project does not have.

### 4.7 Query surface

Three entry points over the same graph:

| Interface | File | For |
|---|---|---|
| MCP server | [`lcb/mcp_server.py`](lcb/mcp_server.py) | LLM clients — `get_schema`, `run_cypher`, `check_changes` |
| `code-graph` skill + `./cq` | [`.claude/skills/code-graph/SKILL.md`](.claude/skills/code-graph/SKILL.md) | Claude Code via `cypher-shell` |
| Python runner | [`lcb/query.py`](lcb/query.py) | scripts and the benchmark |

**Few MCP tools, deliberately.** Pre-baked tools (`get_blast_radius`,
`get_bug_history`) cap the model at questions we anticipated. Handing it the schema plus
a Cypher endpoint lets it answer questions we did not — which is the whole argument for
a graph over a search index.

The one addition, `check_changes`, is not a pre-baked query. It runs
[`lcb check`](#change-check-warn-before-a-bug-comes-back) on the working tree: it needs the
`git diff`, mapped onto method line spans, which Cypher alone cannot see. Its description
tells the agent to call it after editing source and before calling the work done, so an
agent that has just changed a method that was fixed before hears about it without being
asked. It reads the repository from its `repo` argument, else `LCB_REPO` (set in
`.mcp.json`), else the server's working directory. Like the other tools, it returns
errors as text.

**Guardrails:** write clauses rejected by keyword scan, 200-row cap, 30-second timeout,
and Cypher errors *returned as text* rather than raised — so the model reads the error
and corrects its query instead of failing the turn.

`./cq` is a small wrapper: `cypher-shell` when installed, else `python -m lcb.query`.

### 4.8 Benchmark methodology

Two independent measurements.

**Static** — [`lcb/bench.py`](lcb/bench.py). Deterministic, reproducible, no API key.
Counts the context each approach requires:

- *Graph arm*: schema document + Cypher query + returned rows
- *Baseline arm*: grep hit list + full text of every file containing a match

The baseline models what an agent actually does — grep, then read matching files —
**not** a whole-repo dump, which would inflate the ratio dishonestly.

**Live A/B** — [`lcb/compare_runs.py`](lcb/compare_runs.py). Parses Claude Code session
transcripts for real `usage` records (`input_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, `output_tokens`). Same question asked in two sessions:

- Arm A: project root — skill + MCP available
- Arm B: `targets/petclinic` — neither exists

The capability is **genuinely absent** in arm B rather than discouraged by prompt, so
there is no instruction-following confound. Procedure: [`demo/RUNBOOK.md`](demo/RUNBOOK.md).

---

## 5. Results

### Pipeline

| Stage | Measurement |
|---|---|
| Extraction (54,864 classes from 171 JARs + app) | **8.2 s** |
| Neo4j load (1.25M deduplicated edges) | ~2 min |
| Query latency | **12–137 ms** |
| Static benchmark run | 4.3 s |

### Graph contents

Verify with `MATCH (n) RETURN labels(n)[0], count(*)`.

| | |
|---|---|
| Classes | **53,597** (30 application, 53,567 dependency) |
| Methods | **446,071** |
| `CALLS` edges | **705,057** (270,527 exact · 434,530 virtual) |
| `EXTENDS` / `IMPLEMENTS` | 18,907 / 17,349 |
| `MAY_CALL` (CHA) | 41 |
| Packages / Artifacts | 2,726 / 145 |

The extractor emitted 54,864 class rows and 455,634 method rows; the graph holds fewer
nodes because the same class ships in more than one JAR (duplicated and shaded classes are
common on a Spring classpath) and those collapse on `MERGE`. The gap — 1,267 classes,
9,563 methods — is deduplication, not data loss.

### History layer contents

Full PetClinic history (back to 2013) plus GitHub issues and merged PRs — details in
[§10.3](#103-data-sources-and-linking--git-half--done-github-half--done).

| | |
|---|---|
| Commits / Authors | **1,042** / **164** |
| `TOUCHED` (commit → method) | 1,162, covering 116 of 121 application methods |
| `LAST_MODIFIED` (blame) | 116 |
| Issues / merged PRs | 392 / 211 |
| `PART_OF` (commit → PR) | 158 of 190 merge commits (the other 32 predate a history rewrite) |
| `FIXES` (PR → issue) | 16 |
| `CAUSED_BY` (issue → method) | 23 |
| Fix commits found from messages / `FIXED` (commit → method) | 20 commits touching application methods / 119 (vs. 4 fix commits reachable through issues) |

### Call-site resolution

| Outcome | Count | Share |
|---|---|---|
| Resolved inside index | 889,946 | 64.2% |
| Target in the JDK (`java.*`, `javax.*`) — deliberately not indexed | 479,758 | 34.6% |
| Target genuinely outside the index | 16,254 | **1.2%** |

**Resolution excluding the JDK: 98.2%.** The residual 1.2% are optional dependencies never
on PetClinic's classpath — Kotlin stdlib, protobuf, reactor — referenced by libraries that
support them conditionally.

Also captured: 32,041 `invokedynamic` sites, 20,444 resolved to lambda bodies.

### Token cost (static benchmark)

| Question | Graph | Baseline | Saved | Ratio |
|---|---:|---:|---:|---:|
| Callers of `OwnerRepository.findById` | 818 | 4,646 | 82.4% | 5.7× |
| Blast radius of `Owner.getPet` (3 hops) | 831 | 4,180 | 80.1% | 5.0× |
| Which JAR provides `OwnerRepository`'s supertype | 663 | 6,929 | 90.4% | 10.5× \* |
| Application methods with most callers | 1,045 | 15,841 | 93.4% | 15.2× |
| App classes extending a dependency type | 835 | 7,254 | 88.5% | 8.7× |
| **Total** | **4,192** | **38,850** | **89.2%** | **9.3×** |

This table is the original five-question run. `lcb.bench` now asks nine questions; the
current run is **9,602 vs 87,976 tokens: 89.1% saved, 9.2×** (per-question table in
[`demo/DEMO.md`](demo/DEMO.md) §3). The schema the graph arm is charged for grew with the
history layers. It is kept compact, at 746 tokens: `get_schema` returns only what writing
Cypher needs, and the longer explanations come back with `get_schema(details=true)`.

\* The baseline **cannot answer this at all** from application source. `OwnerRepository.java`
names the imported type but never the artifact supplying it. The figure is what it spends
before failing.

**The measurement is conservative and understates the advantage:**

- The schema (578 tokens in the original run, 746 now) is charged to the graph arm on *every* question, though in practice
  it is sent once per session.
- The baseline is charged one read pass. A real agent loop re-sends its accumulated
  transcript each turn, so its true cost grows super-linearly.
- The baseline is grep-and-read, not a whole-repo dump.

### Verified correctness

Answers were checked against source. `OwnerRepository.java:36` reads
`public interface OwnerRepository extends JpaRepository<Owner, Integer>` — the graph
returns exactly that supertype and attributes it to `spring-data-jpa-4.1.0.jar`.

---

## 6. Running the pipeline

### Quick start: one command per repository

Step-by-step instructions, including first-time installation and what to do when a
build fails: [SETUP.md](SETUP.md).

```bash
python3 -m venv .venv && .venv/bin/pip install -e .   # gives the `lcb` command
lcb setup /path/to/repo      # ingest, then connect MCP, VS Code and the git hook
lcb ingest /path/to/repo     # or: just (re)build the graph
```

[`lcb ingest`](lcb/ingest.py) runs every step below in one go:
1. **Build.** Detects Maven or Gradle, preferring the project's `mvnw`/`gradlew`. Compiles
   every module and collects the dependency JARs into one folder.
2. **Extract.** Finds every module's `target/classes` or `build/classes/<lang>/main`.
3. **Load** into Neo4j.
4. **Git history.**
5. **GitHub issues and PRs**, when `origin` is on GitHub and `gh` is logged in.

It then prints the exact MCP registration, the hook command and the VS Code settings for
that repository. A rerun with nothing changed takes about 14 s on PetClinic, because it is
incremental throughout. Options: `--skip-build`, `--build-cmd`, `--build-tool`, `--jars`,
`--no-git`, `--no-github`, `--build-arg`, `--jdk`.

**Builds on other people's machines.** The first real-repo trial (macrozheng/mall, 7 Maven
modules) failed three ways that PetClinic never showed. `lcb ingest` now handles each:

| Trial failure | What the build step does now |
|---|---|
| Homebrew's `mvn` picked Java 27, newer than the project's Lombok supports, so every Lombok getter went missing | Builds with an explicit `JAVA_HOME`: the **oldest installed JDK the build files allow** (`<java.version>17` → Java 21, not 27). It finds JDKs in Homebrew, `/Library/Java`, `/usr/lib/jvm`, SDKMAN and IntelliJ's `~/.jdks`. `--jdk` overrides. |
| A stalled download hung Maven silently, twice | Downloads give up after 60 s and retry. Progress is printed: each module, a download count, and a "still running" line after a minute of silence. The full output goes to `out/repos/<repo>/build.log`; on failure, the first errors are shown. |
| The project's Docker plugin tried to reach the author's own Docker host | Steps not needed for compiled classes are skipped: tests, Docker/Jib images, style and license checks, javadoc, signing, frontend bundles. `--build-arg` passes extra flags. |

Two gaps in the graph itself, also found on mall, are fixed too:

| Trial finding | Fix |
|---|---|
| 20,762 of 22,502 "application" methods were MyBatis Generator output: their history was 90% of the ingest time and pure noise | Generated code is detected ([`lcb/generated.py`](lcb/generated.py)) from generator configs (MyBatis, OpenAPI, jOOQ), markers (`@Generated`, "DO NOT EDIT"…) and `generated-sources` folders. It is marked `is_generated` and stays in the call graph, but gets no history and no risk rank. On mall: 228 files found from its `generatorConfig.xml`, the 2 hand-written files next to them kept. A full history rebuild went from 7 min to 36 s. |
| Calls to inherited methods were dropped: bytecode names the class at the call site (`PageHelper.startPage`), but the method is declared in a superclass (`PageMethod`), so no target node existed | Missing targets are resolved the way the JVM does it: superclass chain, then superinterfaces. The edge points at the declaring method, and `CALLS.via` keeps the class named in the code. On mall: +108,738 call edges (+9.6%), including all 28 `PageHelper.startPage` calls. |

Rerun on mall after a clean, with no manual steps: Java 21 chosen, Docker step skipped,
`BUILD SUCCESS`, the whole ingest in 29 s. PetClinic from a clean build still gives the
identical graph.

**One repository per graph.** A `(:LcbMeta)` node records which repository is loaded.
Ingesting a different one into an occupied graph is refused unless you pass `--replace`.
The other commands are `lcb check`, `lcb hook`, `lcb mcp-config` and `lcb mcp`.

On PetClinic, `lcb ingest` with a real Maven build into an empty database reproduces the
graph in §5 exactly (53,597 classes, 705,057 calls, 1,161 `TOUCHED`, 119 `FIXED`, 23
`CAUSED_BY`). The Gradle path is unit-tested but has not yet been run against a real
Gradle build. The steps below are what it runs, for doing them by hand.

### Prerequisites

JDK 21 (`javac` needed), Python 3.10+, git, and Neo4j (5.x or 2025+) running. The GitHub
layer ([`lcb/github_ingest.py`](lcb/github_ingest.py)) also needs the `gh` CLI,
authenticated (`gh auth status`); it reads through `gh api graphql`, not a managed token.

```bash
# --- Neo4j and Python (once; macOS shown, any Neo4j install works) ---
brew install openjdk@21 neo4j python@3.13            # also installs cypher-shell
neo4j-admin dbms set-initial-password lcbpassword     # before the first start only
brew services start neo4j                             # browser: localhost:7474/browser/
python3 -m venv .venv-mac && .venv-mac/bin/pip install -e .
```

### The steps by hand

This is what `lcb ingest` does, if you need to run one step on its own. `PY` is the
virtualenv's Python, run from this folder.

```bash
PY=.venv-mac/bin/python
REPO=targets/petclinic
WORK=out/repos/manual

# --- build the target and collect its dependency JARs ---
(cd $REPO && ./mvnw -q -DskipTests package dependency:copy-dependencies -DoutputDirectory="$PWD/../../$WORK/deps")

# --- extractor (compiled automatically by lcb ingest) ---
javac -cp extractor/lib/asm-9.7.1.jar -d extractor/build extractor/src/Extractor.java

# --- extract: --classes and --jars repeat, one per module / dependency folder ---
# (classpath separator is ':' on macOS/Linux, ';' on Windows)
java -cp "extractor/lib/asm-9.7.1.jar:extractor/build" Extractor \
  --classes $REPO/target/classes --jars $WORK/deps --out $WORK/csv

$PY -m lcb.load --in $WORK/csv --wipe     # first load; later runs without --wipe are incremental
$PY -m lcb.git_ingest --repo $REPO        # needs the real history: `git fetch --unshallow` on a shallow clone
$PY -m lcb.github_ingest --repo $REPO     # owner/name read from the origin remote
$PY -m lcb.bench --repo $REPO --out out/bench
```

`git_ingest` works on any repository layout: each class's source file is found among the
files `git ls-files` tracks (by package path, so multi-module builds and same-named files in
different modules resolve correctly), not assumed to be under `src/main/java`. Authors go
through the repo's `.mailmap`, and blame reads `HEAD`, so uncommitted edits are ignored.

### Testing the git layer

```bash
PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_git_layer -v
```

Builds a small multi-module Java repo whose commits and authors are scripted, runs the
real pipeline (javac → extractor → `lcb.load` → `lcb.git_ingest`) against a throwaway
Neo4j on random ports, and checks every `TOUCHED`/`LAST_MODIFIED` edge against the known
history, plus `.mailmap` folding, incremental reruns and per-repo state. It never
touches the graph on 7687. Takes about 20 s; needs JDK 21, git and a local Neo4j install.

### Change check: warn before a bug comes back

[`lcb/check.py`](lcb/check.py) takes the current diff, finds the methods it touches, and
flags any that were linked to a past bug (`CAUSED_BY`). It needs no LLM; it runs one diff
and two queries.

```bash
python -m lcb.check --repo targets/petclinic              # working tree vs HEAD
python -m lcb.check --repo targets/petclinic --staged     # what `git commit` would record
python -m lcb.check --repo targets/petclinic --base main  # a pull request
python -m lcb.check --repo targets/petclinic --json       # for editors / bots
```

Real output after editing one line of `VetController.showVetList` (a bug linked through a
GitHub issue) and of `OwnerController.processFindForm` (a fix found from a commit message):

```
⚠ VetController.showVetList  (src/main/java/org/springframework/samples/petclinic/vet/VetController.java:45)
    linked to issue #2333 (91% confidence, fixed by PR #2491): [BUG] Vets object created but never added to model in VetController#showVetList
    0 callers · last changed by simrin051 · risk rank 24 of 121
⚠ OwnerController.processFindForm  (src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java:97)
    fixed before in bb37aad by Luiz Fernando de Andrade Gadelha, 2026-07-24 (83% confidence): "fix: normalize whitespace in owner search"
    +2 weaker links (--all to show)
    0 callers · last changed by Luiz Fernando de Andrade Gadelha · risk rank 24 of 121
```

Past fixes come from both GitHub issues (`CAUSED_BY`) and fix commits (`FIXED`,
[§10.3](#103-data-sources-and-linking--git-half--done-github-half--done)); a fix commit
already behind a listed issue is shown once, as the issue.

Hunks are matched on the diff's **old-side** line numbers. Those number the same
version of the file the graph's `line`/`line_end` describe, so an edit still lands on the
right method after earlier edits have shifted the file. Bytecode line tables start at a
method's first statement, so the signature is found in the old-side source: scanning up for
the line that names the method, past comments and wrapped parameter lists. Editing the
parameter list counts as changing the method. A line inserted after a method's closing
brace (a new method) does not flag its neighbour. In `--json` output, each method also
carries `line_now`/`line_end_now`, its position in the working tree, for editors.

`--min-confidence` (default 0.2) sets how strong a `CAUSED_BY` link must be to flag a
method, and `--all` also lists changed methods with no bug history. `--strict` exits 1
when anything is flagged, so a pre-commit hook can block instead of only warning. If the
history layer was ingested at a different commit than `HEAD`, the output says so.

Exit status: `0` means checked, `1` means `--strict` and something was flagged, and `2`
means the check could not run (not a git repository, Neo4j unreachable). `--quiet` prints
nothing unless something is flagged.

Tests: `PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_check -v`.

### Pre-commit hook

[`lcb/hook.py`](lcb/hook.py) runs the same check on every `git commit`, against what is
staged:

```bash
python -m lcb.hook install   --repo path/to/repo           # warn on flagged commits
python -m lcb.hook install   --repo path/to/repo --block   # stop flagged commits
python -m lcb.hook uninstall --repo path/to/repo
python -m lcb.hook print                                   # lines to add to an existing hook
```

- **Nothing flagged:** silent.
- **Something flagged:** the warning is printed, and the commit goes ahead, or with
  `--block` it is stopped. `git commit --no-verify` always skips the hook.
- **The check cannot run** (Neo4j stopped, git error): one line saying it was skipped, and
  the commit goes ahead. A stopped database never stops anyone committing.

The hook is written where git actually runs hooks (it honours `core.hooksPath`, as used by
husky and similar tools). It never overwrites a pre-commit hook it did not write; use
`print` to add the check to that one instead. It calls this checkout's Python by absolute
path. `NEO4J_URI`, `NEO4J_USER` and `LCB_HOME` are baked in if set at install time; the
password is never written to disk.

### VS Code extension

[`vscode-extension/`](vscode-extension/) shows the same check in the editor. On every save
of a Java/Kotlin/Groovy/Scala file it runs `lcb.check --json`. It marks the signature of
each changed method that was fixed before, with a yellow underline by default
(`lcb.severity: error` makes it red) and an entry in the Problems panel; the hover text
names the issue or fix commit. A status-bar item counts the marked methods, or says why the
check could not run. Marks sit at the method's current position (`line_now`), so earlier
edits in the file don't misplace them.

```bash
cd vscode-extension && npm install && npm test && npm run package
code --install-extension living-codebase-brain-0.1.0.vsix
```

Then set `lcb.projectRoot` to this checkout (and `lcb.repo` if the folder you opened is not
the repository itself). The extension contains no analysis of its own: all decisions are in
`lcb.check`, and [`src/core.ts`](vscode-extension/src/core.ts) only turns its JSON into
marks. Settings and details: [`vscode-extension/README.md`](vscode-extension/README.md).

### Querying

```bash
./cq "MATCH (c:Class {is_external:false}) RETURN count(c);"
.venv-mac/bin/python -m lcb.query "MATCH (c:Class) RETURN count(c)"
```

Credentials: `bolt://localhost:7687`, `neo4j` / `lcbpassword`, browser on `7474`. `./cq`
uses `cypher-shell` when it is on the PATH (Homebrew's Neo4j installs it), and otherwise
falls back to `python -m lcb.query`.

---

## 7. Design decisions and rejected alternatives

### Bytecode over decompilation

The original plan was to decompile dependency JARs and parse the resulting Java. Rejected
after analysis. A `.class` file states each call target exactly:

```
INVOKEVIRTUAL  owner=org/…/OwnerRepository  name=findById  desc=(Ljava/lang/Integer;)…
```

Decompiling produces *approximate* source which must then be re-parsed and re-resolved —
losing the certainty bytecode already provides, and adding decompiler failures on
optimised or obfuscated classes. It is strictly more work for strictly worse data.

A decompiler still has a legitimate role: rendering a dependency method as readable source
*for display* during an explanation. That is an on-demand concern, not the ingestion path,
and `-sources.jar` usually beats a decompiler for it.

### Transitive dependencies over direct-only

The plan specified direct dependencies only. **Measurement invalidated it.** Every Spring
Boot starter JAR contains **zero classes** — they are POM aggregators:

```
1066  mysql-connector-j-9.7.0.jar
1066  h2-2.4.240.jar
 719  caffeine-3.2.4.jar
   0  spring-boot-starter-data-jpa-4.1.0.jar     ← all Spring starters
   0  spring-boot-starter-web-4.1.0.jar
```

Direct-only would have indexed database drivers and **not one Spring Framework class** —
no `JpaRepository`, no `@Controller`. All real Spring code is transitive. The full
classpath is 171 JARs / 54,834 classes, which loads comfortably.

**This is the most useful thing in the project to be able to explain.** The rule was
reasonable in the abstract and wrong in the specific, and measurement is what caught it.

### Few MCP tools, not a catalogue

See [§4.7](#47-query-surface). Pre-baked query tools would limit the model to anticipated
questions; a Cypher endpoint does not. `check_changes` is the exception because it needs
the working tree's diff, which the graph does not hold.

---

## 8. Anticipated cross-questions

**Q: How do you know the call edges are real?**
Bytecode states owner, name and descriptor explicitly — there is no inference step to get
wrong. 98.2% of call sites resolve to an indexed target once JDK calls are excluded.
Edges are further split: `exact: true` (`INVOKESTATIC`/`INVOKESPECIAL`) is provable;
`exact: false` points at the declared type with dispatch resolved separately by CHA.

**Q: Didn't building the graph cost tokens too?**
Zero. Extraction is ASM parsing and loading is Cypher writes — no model call anywhere in
the pipeline. The graph is not pre-paid context. And the cost is paid once while savings
recur per query.

**Q: Isn't your baseline a straw man?**
It is grep-then-read-matching-files — what an agent actually does. A whole-repo dump would
have produced a far bigger number and we did not use it. The schema is charged to the
graph arm on every question, and the baseline is charged only one read pass when a real
loop would re-send its transcript each turn. Both choices push the ratio *down*.

**Q: Why Neo4j instead of a relational database?**
The core query is variable-length reverse traversal — "everything that reaches this method
within 3 hops". In SQL that is a recursive CTE or N round trips; in Cypher it is
`<-[:CALLS*1..3]-`. Storage is also pointer-based, so hop cost does not grow with table
size.

**Q: Why not tree-sitter, as originally proposed?**
tree-sitter produces a syntax tree with no type information. For Java it cannot tell which
`findById` a call refers to, cannot distinguish overloads, and cannot see into JARs at all.
It was the right tool when the target was Python; it is the wrong tool for Java.

**Q: What happens with reflection or Spring dependency injection?**
Invisible — and this is inherent to static analysis, not a defect in the implementation.
A bean wired at runtime has no bytecode call site. The honest framing: the graph captures
everything the compiler knows, which is a large and precisely-bounded subset. Detecting
DI wiring would require parsing Spring annotations and XML — a separate feature, listed
in the roadmap.

**Q: Only 41 `MAY_CALL` edges — isn't CHA barely doing anything?**
That is the scoping working as designed. CHA runs only for calls originating in application
code and for framework dispatch into application code. PetClinic has 30 application classes
with few overrides, so the true number is small. Unscoped CHA across 53k dependency classes
would produce millions of edges inside Spring internals that no query traverses.

**Q: Your resolution is 50.9% by one measure and 98.2% by another. Which is it?**
Both, measuring different things. 64.2% of *all* call sites resolve to an indexed target;
34.6% point into the JDK, which we deliberately do not index; 1.2% point at genuinely
unindexed classes. Excluding the JDK by construction, 98.2% resolve. The 1.2% is the only
figure representing an actual gap.

**Q: Does this scale to a real codebase?**
PetClinic is small but its dependency closure is not — 54,864 classes and 1.39M call sites
is already a realistic enterprise-scale graph, extracted in 8.2 s. The scaling limit is
load time (~2 min), addressable with `neo4j-admin database import` for bulk CSV.

**Q: Why is the graph built from a compiled build rather than live source?**
Bytecode requires compilation, so the graph reflects last-compiled state. The proposal's
live-update premise is satisfied by watching build output rather than source files —
IDEs compile on save. That is a roadmap item, not a built feature.

**Q: What stops the LLM from writing a destructive query?**
Layered: write clauses are rejected by keyword scan before execution, results are capped
at 200 rows, transactions time out at 30 s, and the intended production posture is a
read-only Neo4j user so the guarantee is enforced by the database rather than by our
filter.

**Q: What is genuinely novel here versus CodeGraph / GitNexus / RepoGraph?**
Those graph code structure. Two differences. First, dependencies are first-class — the
graph contains Spring's own classes, so questions crossing the app/framework boundary are
answerable at all. Second, the socio-technical layer ([§10](#10-the-github--history-layer))
fuses bug and PR history into the same traversal. Both are built; what remains is
validating the risk score on a repository large enough to fit its weights.

---

## 9. Known limitations

| Limitation | Consequence | Mitigable? |
|---|---|---|
| Reflection, dynamic proxies invisible | Runtime-wired calls have no edge | No — inherent to static analysis |
| Spring DI partly recovered ([§4.6](#46-dependency-injection-recovery-phase-10)) | `INJECTS`/`INJECTS_BEAN_METHOD` only resolve to a *statically present* bean; Spring Data repository interfaces (PetClinic's own dominant case) have none | Fixed: would need runtime introspection, not static analysis |
| JDK not indexed | `java.*` targets have no node | Yes — index `java.base`, at graph-size cost |
| `MAY_CALL` over-approximates | Blast radius lists possible, not actual, targets | Only with points-to analysis (Soot/WALA) |
| Requires a compiled build | Graph reflects last build, not editor buffers | Watch build output |
| Full re-index on change | ✅ fixed — [§4.5a](#45a-incremental-loading-phase-9): per-class signature diffing, 70s → 7.0s on a one-line PetClinic edit | Done |
| Single language | Java only | Extractor is bytecode-based → any JVM language already works; others need new extractors |
| Static token benchmark uses `tiktoken` | Estimates, not live usage | Solved by the live A/B harness |
| Requires a running Neo4j | Not zero-config | Embedded store for production |

---

## 10. The GitHub / history layer

This is the project's core differentiator. It is built and loaded on PetClinic: git
history (phase 6), GitHub issues and PRs (phase 7), and a risk score with a historical
back-test (phase 8). What remains is validating the risk score on a larger repository
([§10.5](#105-evaluation-historical-back-test---run-result-inconclusive-honestly)).

### 10.1 Why it bolts on cleanly

The history layer adds **new node labels with no migration**. Existing `Class` and `Method`
nodes are unchanged; history attaches to them.

```cypher
(:Author {email, name})
(:Commit {sha, message, timestamp, additions, deletions})
(:Issue  {number, title, state, opened_at, closed_at, labels})
(:PR     {number, title, merged_at, files_changed})

(:Author)-[:AUTHORED]->(:Commit)
(:Commit)-[:TOUCHED {added, removed}]->(:Method)
(:Commit)-[:PART_OF]->(:PR)
(:PR)-[:FIXES]->(:Issue)
(:Issue)-[:CAUSED_BY {confidence}]->(:Method)
(:Author)-[:LAST_MODIFIED]->(:Method)
(:Commit)-[:FIXED {confidence}]->(:Method)
```

`Author`, `Commit`, `AUTHORED`, `TOUCHED` and `LAST_MODIFIED` are built —
[`lcb/git_ingest.py`](lcb/git_ingest.py), phase 6. `Issue`, `PR`, `PART_OF`, `FIXES` and
`CAUSED_BY` are also built — [`lcb/github_ingest.py`](lcb/github_ingest.py), phase 7
([§10.3](#103-data-sources-and-linking--git-half--done-github-half--done)).

### 10.2 The hard problem: mapping a diff to a method — ✅ done

A git diff gives **file + line ranges**. The graph stores **methods**. Connecting them
requires knowing each method's line span.

The extractor reads `LineNumberTable` from bytecode and now stores both ends of each
method's declaration: `firstLine` (minimum) as `line`, `lastLine` (maximum) as `line_end`:

```java
if (firstLine < 0 || lineNo < firstLine) firstLine = lineNo;
if (lineNo > lastLine) lastLine = lineNo;
```

Every `Method` node carries a `[line, line_end]` span in its source file. A diff hunk maps
to a method by interval overlap:

```cypher
MATCH (m:Method {is_external:false})
WHERE m.source_file = $file AND m.line <= $hunk_end AND m.line_end >= $hunk_start
```

Without this the history layer could only attach to files; with it, it attaches to
methods — which is what makes the risk signal meaningful. Phases 6–8 ([§11](#11-roadmap))
build on this span.

Caveat worth stating: bytecode line tables reflect the compiled build, so mapping historical
commits requires either checking out and rebuilding each revision (expensive but exact) or
accepting file-level granularity for older history.

**`TOUCHED` sidesteps this caveat rather than hitting it.** Instead of intersecting each
commit's raw diff hunks against the method's *current* line span — which drifts the further
back a file has been edited since — `git_ingest.py` runs
`git log -L$line,$line_end:$path` per application method. Git's `-L` mode re-targets the
range through history itself, renumbering it past every earlier commit that shifted lines
above it, so the result is exact for every commit, not just ones near HEAD. `LAST_MODIFIED`
has no such problem to begin with: `git blame` runs against the current file, the same
revision the extractor's `line`/`line_end` describe.

### 10.3 Data sources and linking — git half ✅ done, GitHub half ✅ done

| Source | Command / endpoint | Yields | Status |
|---|---|---|---|
| Commit history | `git log --numstat --format=...` | `Commit`, `Author`, `AUTHORED` | ✅ |
| Per-method history | `git log -L$line,$line_end:$path` | `TOUCHED {added, removed}` | ✅ |
| Per-line authorship | `git blame --line-porcelain` | `LAST_MODIFIED` per method span | ✅ |
| Fix commits | commit subject, classified by [`lcb/fixes.py`](lcb/fixes.py) | `Commit.fix_kind`, `issue_refs`, `FIXED` | ✅ |
| Issues | GraphQL `repository.issues` | `Issue` nodes, labels, timing | ✅ |
| Pull requests | GraphQL `repository.pullRequests(states:MERGED)` | `PR` nodes | ✅ |
| PR → issue links | GraphQL `closingIssuesReferences` + title/body regex fallback | `FIXES` | ✅ |
| PR → commit link | GraphQL `mergeCommit.oid` matched against existing `Commit` nodes | `PART_OF` | ✅ |

Git half run via `python -m lcb.git_ingest --repo targets/petclinic [--wipe]`. Scoped to
application methods only (`is_external:false`) — dependency JARs ship with no git history
in this repo, so there is nothing honest to attach `TOUCHED`/`LAST_MODIFIED` to for them.
On PetClinic's full history (1,042 commits back to 2013, unshallowed from the default
`--depth 1` clone): 164 distinct authors, 1,162 `TOUCHED` edges across 116 of the 121
application methods (5 have no `line`/`line_end` — abstract or body-less), 116
`LAST_MODIFIED` edges.

**Fix commits from messages.** The issue chain only sees fixes that went through an issue
*and* a PR naming it. On PetClinic, about 115 commit subjects mention a fix and 23 of those
change application methods, but the chain reaches only 4. So `git_ingest` also classifies
every commit subject ([`lcb/fixes.py`](lcb/fixes.py)):
- `fix:` / `fix(scope):` → `conventional`
- a fix word (`fixed`, `bugfix`, `hotfix`, `regression`…) → `keyword`
- git's `Revert "…"` → `revert`

Merge commits are skipped, since the fix commit itself is already counted. So are
`docs:`/`test:`/`chore:` types and text-only fixes (typos, comments, links, readme). Fix
words inside quotes are ignored, so `Polish "Fix …"` is a follow-up, not a fix. `#N`
references are kept as `issue_refs`, which links pre-PR-era commits like `Fix #108 owner
update` to the issue's title. `FIXED` edges are then derived from those commits' `TOUCHED`
edges with the same attribution score as `CAUSED_BY`. On PetClinic this gives 20 fix commits
on application methods and 119 `FIXED` edges. It needs only git, so it works whatever
tracker a project uses. It is a free-text heuristic: a "fix" that only renames a variable
counts, and a fix described as "handle empty list" does not.

GitHub half run via [`lcb/github_ingest.py`](lcb/github_ingest.py)
(`python -m lcb.github_ingest --repo targets/petclinic [--full] [--wipe]`, or `--repo-slug owner/name`),
via the `gh` CLI (already authenticated in this environment; GraphQL rather than REST per
the batching guidance in [§10.6](#106-engineering-concerns), one paginated query each for
issues and merged PRs, cached to `out/github/<owner>__<name>/*.json` so a rerun doesn't re-fetch). Results on
spring-petclinic: 392 issues, 211 merged PRs, 16 `FIXES` links (6 from `closingIssuesReferences`,
the rest from the regex fallback), 23 `CAUSED_BY` edges.

**`PART_OF` only linked 158 of 190 merge commits GitHub reported** (32 unresolved, reported
rather than silently dropped — same philosophy as the structural layer's
`calls_outside_index`). All 32 are old: PRs merged before ~2014, whose merge-commit SHAs no
longer exist as objects in this clone at all. The repository's early history was evidently
rewritten at some point (squashed or re-based) — a gap in the source data, not a bug in the
ingestion. `CAUSED_BY` is derived entirely from data already in the graph (no new GitHub or
git call): for each `Issue<-FIXES-PR<-PART_OF-Commit`, the commit's `TOUCHED` edges (already
computed in phase 6) become candidate methods, weighted by a confidence heuristic —
`focus (1/methods touched) × line_share (this method's lines / the commit's total) ×
size (1/(1 + files_changed/10))` — that only ranks candidates against each other, not a
calibrated probability. On the two PetClinic issues whose titles name a method explicitly,
that exact method came out on top: `showVetList` (confidence 0.91, a 1-file fix) and
`addPet` (0.29, a 4-file fix).

**Bug → method linkage** is the inference chain:

```
Issue #482  ──FIXES──  PR #510  ──PART_OF──  commit abc123
                                                   │ TOUCHED (file + hunk)
                                                   ▼
                                    Method  Owner#getPet(...)
```

PR → issue comes from GitHub's own closing references (`Fixes #N`, `Closes #N`) in the
merge metadata, which is more reliable than regex over commit messages — though a regex
fallback is needed for repositories without the convention.

**Attribution must be weighted, not binary.** A fixing commit touches unrelated files —
imports, tests, formatting. Attributing a bug equally to every touched method is the
obvious mistake. Proposed `confidence` on `CAUSED_BY`:

- higher when the commit touches few methods (a focused fix)
- higher for methods with more changed lines in that hunk
- lower for test files, generated code, and pure-formatting changes
- lower when the PR is a large refactor

### 10.4 Risk scoring — ✅ built, ⚠️ unvalidated (see §10.5)

Combine structural and historical signals — [`lcb/risk.py`](lcb/risk.py),
`python -m lcb.risk [--top 15] [--weights 0.25,0.25,0.25,0.25]`:

```
risk(m) = w1 · fan_in(m)                  # blast radius: who breaks
        + w2 · bug_signal(m)              # historical incidents (sum of CAUSED_BY confidence)
        + w3 · churn(m)                   # change frequency (distinct TOUCHED commits)
        + w4 · author_departed(m)         # knowledge loss
        - w5 · test_coverage(m)           # mitigating factor
```

Each raw signal is min-max normalized to [0,1] across application methods before
weighting (they live on incomparable scales otherwise — an integer call count next to a
0/1 flag next to a confidence sum). **`w5`/`test_coverage` is dropped, not faked**: the
extractor only ever indexed compiled application classes (`src/main/java`), never test
sources, so there is no honest coverage signal to combine — inventing a text-matching
proxy would be exactly the kind of fabricated data point the rest of this project
refuses to produce. `author_departed` is computed from `TOUCHED`+`AUTHORED` timestamps
rather than `LAST_MODIFIED` specifically so the same function works at a historical
cutoff: blame only ever describes the *current* file, never "as of 2023."

Weights should be **fitted against the back-test, not hand-tuned** — otherwise the
evaluation is circular. They are currently an un-fitted equal-weight placeholder
(`0.25` each) because §10.5's back-test found PetClinic's sample too small to fit them
meaningfully — see below, and the prediction already made about this in
[§10.6](#106-engineering-concerns).

### 10.5 Evaluation: historical back-test — ✅ run; result: inconclusive, honestly

The proposal's evaluation plan, run via [`lcb/backtest.py`](lcb/backtest.py)
(`python -m lcb.backtest --cutoff 2023-01-01T00:00:00Z --top 15`):

1. Choose a cutoff date `T`. ~~Build the graph from history up to `T` only~~ — approximated:
   `fan_in` is read from the current (single) compiled build at every cutoff (there is no
   historical build to rebuild from without checking out and recompiling that revision,
   Sec.10.2's "expensive but exact" option); `churn`/`bug_signal`/`author_departed` are
   genuinely time-sliced to data dated at or before `T`.
2. Emit the top-15 highest-risk methods as of `T=2023-01-01`.
3. Count bugs actually attributed to each method (via `CAUSED_BY`, dated by the fixing
   `Issue`'s own `closed_at`) **after** `T`.
4. Compare flagged vs. unflagged, against two baselines.

**The control that matters:** frequently-changed methods attract more bug reports simply
because they are touched more. Without normalising for churn, the model rediscovers
"busy code is busy". Report recall against a **churn-only** baseline ranking, and a
**fan-in-only** baseline — if the combined score doesn't beat structure-only, it isn't
earning its complexity.

**Update, with fix commits from messages counted in `bug_signal`:** combined risk reaches
recall@15 of **2/13** (`VetController.showVetList`, `PetValidator.validate`), against 1/13
for each baseline. The ground truth is unchanged (issue-linked bugs after the cutoff). On 13
positives this is a direction, not evidence. The original run follows.

**Result, run for real on spring-petclinic:** 13 application methods had a bug closed
after the 2023-01-01 cutoff — the ground-truth positive set, out of 121 total. All three
rankings (combined risk, fan-in-only, churn-only) achieved **the same recall@15: 1/13** —
and, tellingly, each one's single hit was a *different* method (`VetController.showVetList`
for combined risk, `Owner.addPet` for fan-in-only, `OwnerController.<init>` for
churn-only). The combined score did not beat either baseline here. Of the 12 positives
missed by the risk ranking, several — `WebConfiguration.addInterceptors`,
`.localeResolver`, `VisitController.minVisitDate` — had `churn=0, fan_in=0` as of the
cutoff: that code was added *after* 2023, so no score computed from before-cutoff history
could ever have flagged it. That is a limitation of what the back-test can measure at all
on a 121-method/13-event sample, not a weighting problem — exactly the "too small to be
meaningful" outcome [§10.6](#106-engineering-concerns) predicted before this was built,
now demonstrated rather than assumed. The instrument works; the repository is too small
and too calm to validate it. A medium-sized, actively-maintained project with a real
backlog remains the right next step for fitting `w1..w4` for real.

### 10.6 Engineering concerns

- **Rate limits** — ✅ addressed. 5,000 req/hr authenticated (via the `gh` CLI, already
  logged in rather than managing a token directly); GraphQL batches issues and merged PRs
  into ~6 paginated round trips total for 392 + 211 items; raw responses cache to
  `out/github/<owner>__<name>/*.json` so a rerun doesn't re-fetch at all (`--full` forces it).
- **Incremental ingestion** — ✅ built, phase 9. `lcb/git_ingest.py` stores the last-ingested
  commit sha (`out/state/<repo>-<hash>/git_state.json`, one per repository, and ignored if it
  belongs to another repo or its sha is no longer in history); a rerun does `git log <sha>..HEAD` for new commits only,
  then refreshes `TOUCHED`/`LAST_MODIFIED` for just the application methods whose *file*
  changed since (`git diff --name-only`) — everything else is left as-is, correctly, since
  nothing about it could have changed. `lcb/github_ingest.py` stores the last-ingested
  `updatedAt` (`out/github/<owner>__<name>/state.json`); a rerun paginates GraphQL by `UPDATED_AT DESC` and
  stops as soon as it reaches an already-seen item, merging the (usually tiny) update set
  into the existing cache by `number`. Both default to incremental once state exists;
  `--full`/`--wipe` forces a complete rescan. Verified on real data, not just no-op: a
  synthetic one-line commit was correctly picked up by git_ingest (exactly 1 new `TOUCHED`
  edge, correct `LAST_MODIFIED` reassignment, 13 sibling methods in the same file correctly
  left unchanged) then cleanly reverted; github_ingest was pointed at a real past cutoff and
  correctly bounded its fetch to the few genuinely-updated issues/PRs while still merging
  back to the exact same full totals as a `--full` run.
- **Bot noise** — ⬜ not filtered. Dependabot/CI authors weren't prominent in PetClinic's
  164 authors, so it wasn't forced, but a larger repo would need this before trusting churn.
- **Repository choice** — turned out less bleak than predicted: PetClinic *did* produce a
  real, if small, bug signal — 16 `FIXES` links and 23 `CAUSED_BY` edges, including two
  issues whose titles name a method explicitly, both correctly ranked top candidate
  ([§10.3](#103-data-sources-and-linking--git-half--done-github-half--done)). Still too small
  for the back-test itself ([§10.5](#105-evaluation-historical-back-test---run-result-inconclusive-honestly)) to mean much
  statistically — a medium-sized, actively-maintained project remains the right choice for
  phase 8.

---

## 11. Roadmap

| Phase | Work | Status |
|---|---|---|
| 1 | Bytecode extraction, dependency ingestion | ✅ done |
| 2 | Neo4j schema, batched loading, CHA | ✅ done |
| 3 | MCP server, skill, Cypher interface | ✅ done |
| 4 | Token benchmark, static + live A/B | ✅ done |
| 5 | Method line spans (`line_end`) — [§10.2](#102-the-hard-problem-mapping-a-diff-to-a-method---done) | ✅ done |
| 6 | Git ingestion: commits, authors, blame — [§10.3](#103-data-sources-and-linking--git-half--done-github-half--done) | ✅ done |
| 7 | GitHub ingestion: issues, PRs, linkage — [§10.3](#103-data-sources-and-linking--git-half--done-github-half--done) | ✅ done |
| 8 | Risk scoring + historical back-test — [§10.5](#105-evaluation-historical-back-test---run-result-inconclusive-honestly) | ✅ built, result inconclusive (sample too small) |
| 9 | Incremental re-index on build output — [§4.5a](#45a-incremental-loading-phase-9): per-class content signature, unchanged classes never sent to Neo4j; one-line edit on PetClinic goes from 70s to 7.0s | ✅ done |
| — | *Related*: incremental **history** ingestion ([§10.6](#106-engineering-concerns)) — `git_ingest`/`github_ingest` both fetch and recompute only what changed since the last run | ✅ done |
| 10 | Spring annotation parsing for DI edges — [§4.6](#46-dependency-injection-recovery-phase-10) | ✅ done (stretch) |
| 11 | Change check: warn when a diff touches a method linked to a past bug — [§6](#change-check-warn-before-a-bug-comes-back) | ✅ CLI (`lcb.check`), MCP tool (`check_changes`), pre-commit hook (`lcb.hook`) and VS Code extension (`vscode-extension/`) done |

Phase 5 is deliberately first: it is small, it is a prerequisite for phases 6–8, and
without it the history layer degrades to file-level granularity.

---

## Repository layout

```
extractor/src/Extractor.java      ASM bytecode → CSV
extractor/lib/                    ASM 9.7.1 jars
lcb/load.py                       CSV → Neo4j, batched, + CHA, incremental via manifest.json
lcb/git_ingest.py                 git log/blame → Commit, Author, TOUCHED, LAST_MODIFIED (incremental)
lcb/github_ingest.py              gh/GraphQL → Issue, PR, FIXES, PART_OF, CAUSED_BY (incremental)
lcb/query.py                      ad-hoc Cypher runner
lcb/mcp_server.py                 MCP: get_schema, run_cypher, check_changes
lcb/bench.py                      static token benchmark
lcb/compare_runs.py               live A/B from Claude Code transcripts
.claude/skills/code-graph/        skill teaching Claude to query the graph
.mcp.json                         MCP server registration (this machine's paths; `lcb mcp-config` prints one)
cq                                Cypher from the shell (cypher-shell, or lcb.query)
docs/Project_Doc.docx             original project proposal
demo/RUNBOOK.md                   A/B demo procedure
out/repos/<repo>-<hash>/          lcb ingest working data: deps/ (dependency JARs), csv/ (extracted CSVs + stats)
out/repos/<repo>-<hash>/csv/manifest.json   per-class content signature, for incremental loading
out/bench/results.json            benchmark output
out/github/<owner>__<name>/        cached raw issues/PRs + last-ingested updatedAt, per repository
out/state/<repo>-<hash>/          last-ingested commit sha, per repository, for incremental reruns
tests/test_git_layer.py           git layer tests: a scripted multi-module repo with known history
tests/neo4j_sandbox.py            throwaway Neo4j (own data dir and ports) for the tests
lcb/check.py                      change check: diff → touched methods → past bugs, risk
lcb/hook.py                       installs the change check as a git pre-commit hook
vscode-extension/                 VS Code extension: marks changed methods that were fixed before
tests/test_check.py               change check tests on the same scripted repo
lcb/risk.py                       risk scoring over application methods
lcb/backtest.py                   historical back-test of the risk score
out/risk/scores.json              full risk ranking output
```
