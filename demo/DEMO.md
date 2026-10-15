# Demo script — Living Code Base Brain

Commands are for a macOS/Linux shell (zsh or bash), run from the project root. Every
number and line quoted below was re-checked against the live graph; if your run
differs, trust your run.

```bash
cd "/path/to/Living Codebase brain"
```

`./cq "<cypher>;"` runs a query. Use **single quotes** inside the Cypher, since the
argument is already in double quotes, and end with a semicolon.

---

## 0. Five minutes before: start it up

```bash
./demo/start.sh
```

It starts Neo4j if it isn't running (through Homebrew, or `neo4j start`). It then checks
that the graph holds the expected 53,597 classes, 446,071 methods and 705,057 call edges,
and runs a real query to prove it answers. It is safe to run twice.

If it prints `[FAIL]`, it says what to do. If the graph is empty or holds something else,
rebuild it with `.venv-mac/bin/python -m lcb ingest targets/petclinic --replace` (about 2 minutes, plus the Maven
build). Everything below assumes it printed `[ok] ready`.

---

## 1. The one-sentence pitch

> An AI coding assistant answers questions about a codebase by grepping and reading
> files, which is slow, expensive, and blind to compiled dependencies. This project
> compiles the whole codebase — application code **and all 171 dependency JARs** — plus
> its git and GitHub history into one Neo4j graph. The assistant answers with a query
> instead, and developers get warned when they touch code that has been fixed before.

---

## 2. Show the graph is real (1 min)

```bash
./cq "MATCH (c:Class) RETURN count(c) AS classes;"
./cq "MATCH ()-[r:CALLS]->() RETURN count(r) AS call_edges;"
```

Then open **http://localhost:7474/browser/** (`neo4j` / `lcbpassword`) and run this in
the browser. It draws an actual picture of the graph, which lands better than a number:

```cypher
MATCH p=(c:Class {is_external:false})-[:HAS_METHOD]->(:Method)-[:CALLS]->(:Method)
RETURN p LIMIT 40;
```

More verified browser queries, including dead-code pictures, are in
[`VISUAL.md`](VISUAL.md), along with the one rule that decides whether a query draws a
picture or a table.

---

## 3. The headline: same question, with and without the graph

It is deterministic: no API call, no variance, identical numbers every run.

```bash
.venv-mac/bin/python -m lcb.bench --repo targets/petclinic --out out/bench
```

For nine real questions it measures the context an LLM must read to answer each one two
ways. With the graph: the schema, the Cypher and the returned rows. Without it: the grep
hit list plus the full text of every matching source file.

```
question                              graph   baseline    saved   ratio
-----------------------------------------------------------------------
q1_callers                              986      4,628    78.7%    4.7x
q2_blast_radius                         999      4,132    75.8%    4.1x
q3_dependency_origin                    831      6,860    87.9%    8.3x  *
q4_hotspots                           1,215     15,521    92.2%   12.8x
q5_framework_surface                  1,003      7,201    86.1%    7.2x
q6_unused_methods                     1,629     16,506    90.1%   10.1x
q7_jar_usage                            949     14,116    93.3%   14.9x  *
q8_class_dependencies                   970      1,483    34.6%    1.5x
q9_jar_inventory                      1,020     17,529    94.2%   17.2x  *
-----------------------------------------------------------------------
TOTAL                                 9,602     87,976    89.1%    9.2x
```

**Headline: 9.2× cheaper, 89.1% saved across nine questions.** Per-question detail is
written to `out/bench/results.json`.

**Say this before you're asked.** The measurement is conservative, and each of these
choices *understates* the graph's advantage:

- The schema document (746 tokens) is charged to the graph on **every** question,
  though in practice an assistant reads it once per session.
- The baseline is scoped to `src/main/java`, the same code the graph indexes, rather
  than a whole-repo dump that would flatter the result.
- The baseline is charged a **single** read pass. A real agent loop re-sends its
  accumulated transcript every turn, so its true cost grows faster than linearly.

**And say this one too:** on q8, where the answer sits in one small file, the graph only
just wins (1.5×), because the schema costs about half of what reading the file does. The
graph wins big where an answer is spread across many files or isn't in source at all. The `*` rows are
the second case: the baseline cannot answer them, and its number is what it spends
*before failing* (section 5).

---

## 4. Proving it is live, not hardcoded

Four escalating proofs. The last one is conclusive.

### Proof 1: they pick the method, not you

```bash
./demo/ask.sh getPet       # or any name they choose
./demo/ask.sh --list       # the names they can pick from
./demo/ask.sh --random     # let the database pick, so you cannot have chosen it
```

The script prints the Cypher it builds **from their input** before running it, then the
rows that came back. Run `--random` two or three times and you get a different method
each time.

### Proof 2: open the database's own UI

**http://localhost:7474/browser/** is Neo4j's own browser, not something in this
project. Hand over the keyboard and let them type:

```cypher
MATCH (c:Class) RETURN count(c);
```

### Proof 3: cross-check an answer against the real file

The graph says `OwnerController.showOwner` calls `findById` at **line 172**. Show that
line:

```bash
sed -n 169,176p targets/petclinic/src/main/java/org/springframework/samples/petclinic/owner/OwnerController.java
```

The call is on line 172, where the graph said. The graph never read that `.java` file; it
read the compiled `.class`, and still lands on the right line.

### Proof 4: turn the database off and watch it break

Hardcoded output does not care whether a database is running.

```bash
brew services stop neo4j
./cq "MATCH (c:Class) RETURN count(c) AS classes;"     # fails: cannot connect
./demo/start.sh                                        # brings it back
./cq "MATCH (c:Class) RETURN count(c) AS classes;"     # 53597 again
```

**Practise this once beforehand.** A restart takes about 20 s, and you don't want a
surprise here.

---

## 5. The question grep cannot answer at all

The strongest single moment, because it is about capability, not cost.

```bash
./cq "MATCH (c:Class)-[:EXTENDS|IMPLEMENTS]->(p:Class)<-[:PROVIDES]-(a:Artifact) WHERE c.fqn ENDS WITH 'OwnerRepository' RETURN p.fqn AS supertype, a.name AS provided_by;"
```

Answer: `JpaRepository`, provided by **`spring-data-jpa-4.1.0.jar`**. Now the source
file:

```bash
grep -n JpaRepository targets/petclinic/src/main/java/org/springframework/samples/petclinic/owner/OwnerRepository.java
```

The `.java` file names the imported *type*. It never names the *JAR that ships it*. That
fact only exists in compiled bytecode, which is what the extractor reads.

---

## 6. History: "has this broken before?" (2 min)

The graph also holds 1,042 commits, 164 authors, 392 issues and 211 PRs, attached to
the same methods. Two queries:

```bash
# methods with the most past fixes (links of at least 20% confidence), from
# commit messages and GitHub issues
./cq "MATCH (m:Method {is_external:false}) OPTIONAL MATCH (:Commit)-[f:FIXED]->(m) WHERE f.confidence >= 0.2 OPTIONAL MATCH (:Issue)-[c:CAUSED_BY]->(m) WHERE c.confidence >= 0.2 WITH m, count(DISTINCT f) + count(DISTINCT c) AS fixes WHERE fixes > 0 RETURN m.owner, m.name, fixes ORDER BY fixes DESC, m.name LIMIT 8;"

# who last changed a method
./cq "MATCH (a:Author)-[:LAST_MODIFIED]->(m:Method {name:'processFindForm'}) RETURN a.name, m.owner;"
```

Most bug fixes never go through an issue tracker. Fix commits are found from their
messages (`fix: …`, `fixed …`, `Revert "…"`), which works on any git repository.

---

## 7. The warning: touching code that has been fixed before (2 min)

This needs no LLM. Make a one-line change to a method with a past fix, and check it:

```bash
F=targets/petclinic/src/main/java/org/springframework/samples/petclinic/vet/VetController.java
sed -i '' '47s/$/ \/\/ demo edit/' "$F"      # inside showVetList (GNU sed: drop the '')
.venv-mac/bin/python -m lcb check --repo targets/petclinic
git -C targets/petclinic checkout -- src/main/java/org/springframework/samples/petclinic/vet/VetController.java
```

Expected output:

```
⚠ VetController.showVetList  (src/main/java/.../vet/VetController.java:45)
    linked to issue #2333 (91% confidence, fixed by PR #2491): [BUG] Vets object created but never added to model in VetController#showVetList
    +3 weaker links (--all to show)
    0 callers · last changed by simrin051 · risk rank 26 of 121
1 of 1 changed method with past bug fixes
```

The same check reaches people four ways. Show whichever fits the audience:

| Where | How |
|---|---|
| Terminal | `python -m lcb check --repo <repo>` |
| On `git commit` | `python -m lcb hook install --repo <repo>`: warns, or stops the commit with `--block` |
| Inside the AI assistant | the `check_changes` MCP tool. The agent calls it after editing code. |
| VS Code | the extension underlines the method's signature on save (`vscode-extension/`) |

The `0 callers` is worth a sentence: Spring calls controller methods through reflection,
which static analysis cannot see. The bug history is what flags this method, not the
call graph.

---

## 8. Live A/B with real Claude Code sessions (optional, ~5 min)

Section 3 is a model of cost. This measures what the API **actually charged**. The full
procedure is in [`RUNBOOK.md`](RUNBOOK.md). In short:

1. A fresh `claude` session in the project root (graph available), asked:
   *"Which methods call OwnerRepository.findById? List every caller with its class,
   method name, and the line number of the call."*
2. A fresh session in `targets/petclinic` (no graph), asked the identical question.
3. `.venv-mac/bin/python -m lcb.compare_runs --graph <id-A> --baseline <id-B>`

---

## 9. Say these before someone finds them

Volunteering limits reads as knowing your system.

- **Tests aren't indexed.** Only main compiled output is extracted, never test classes.
  So `grep -rl findById targets/petclinic/src` finds 9 files, while the graph reports 5
  application callers. The difference is test files plus the repository interface
  itself.
- **JDK calls aren't indexed.** `java.*` / `javax.*` targets have no node.
- **Runtime wiring is invisible to static analysis.** Reflection and dynamic proxies
  produce no edge. Spring DI is partly recovered (`INJECTS` edges), but Spring Data
  repositories are generated at runtime, so injections of them stay unresolved.
- **`MAY_CALL` over-approximates on purpose.** It is Class Hierarchy Analysis: every
  override that *could* be dispatched, which is the right bias for a blast radius.
- **The graph reflects the last ingest**, not unsaved editor buffers.
  `python -m lcb ingest <repo>` refreshes it, and runs incrementally.
- **Bug links are a ranking heuristic.** "91% confidence" ranks candidates; it is not a
  calibrated probability. The risk score is unvalidated, because PetClinic is too small.
- **Benchmark token counts use tiktoken** as a stand-in for Claude's tokenizer. The
  ratios are what's measured; section 8 has exact Claude-charged numbers.

---

## 10. If they ask to try their own question

Let them. That the graph answers questions nobody pre-baked is the point of exposing
Cypher rather than a few fixed tools. The schema and worked patterns are in
[`.claude/skills/code-graph/SKILL.md`](../.claude/skills/code-graph/SKILL.md) and
[`CYPHER.md`](CYPHER.md).

```bash
# most-depended-on application methods
./cq "MATCH (m:Method)<-[:CALLS]-(caller:Method) WHERE m.is_external = false RETURN m.owner, m.name, count(DISTINCT caller) AS callers ORDER BY callers DESC LIMIT 10;"

# where the application touches the framework
./cq "MATCH (c:Class {is_external:false})-[:EXTENDS|IMPLEMENTS]->(p:Class {is_external:true}) RETURN c.fqn, p.fqn ORDER BY c.fqn LIMIT 20;"

# blast radius of a change, application code only
./cq "MATCH (m:Method {name:'getPet'})<-[:CALLS*1..3]-(caller:Method) WHERE caller.is_external = false RETURN DISTINCT caller.owner, caller.name LIMIT 30;"
```
