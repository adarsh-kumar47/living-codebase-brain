# Living Codebase Brain: Architecture

This document explains how the system fits together and what has been built. The
[README](README.md) holds the full design rationale, measurements and the
cross-questions; this file is the map.

---

## 1. What the project is

A Java codebase, together with its git and GitHub history, is turned into **one Neo4j
graph**. The graph is then used in two ways:

1. **An AI assistant queries it** (over MCP) instead of grepping and reading files. It
   gets exact answers — who calls this, what breaks if I change it, which library provides
   this type, who changed it last, has it caused bugs — for roughly a tenth of the tokens.
2. **Developers are warned automatically** when a change touches a method that has been
   fixed before: in the terminal, on `git commit`, inside the AI agent, and as an
   underline in VS Code. No LLM is involved in this part.

Spring PetClinic (`targets/petclinic`) is the **example repository** used for testing
and demos. Nothing in the pipeline is written for PetClinic specifically — see
[§8](#8-does-it-work-for-any-repository) for what is generic today and what is not yet.

---

## 2. The big picture

```
 ┌──────────────────────── INGESTION (offline, per repository) ───────────────────────┐
 │                                                                                    │
 │  compiled classes + dependency JARs ──▶ Extractor (Java/ASM) ──▶ CSVs ──▶ lcb.load │
 │  .git history ─────────────────────────────────────────────────▶ lcb.git_ingest    │
 │       (commits, authors, blame, per-method history, fix commits via lcb.fixes)     │
 │  GitHub issues + PRs (gh CLI) ─────────────────────────────────▶ lcb.github_ingest │
 │                                                                                    │
 └──────────────────────────────────────┬─────────────────────────────────────────────┘
                                        ▼
                    ┌───────────────────────────────────────┐
                    │              Neo4j graph              │
                    │  code: Class, Method, CALLS, ...      │
                    │  history: Commit, Author, TOUCHED,... │
                    │  bugs: Issue, PR, CAUSED_BY, FIXED    │
                    └───────┬───────────────────────┬───────┘
                            │                       │
        ┌───────────────────▼──────────┐   ┌────────▼───────────────────────────────┐
        │  ASK  (LLM, on demand)       │   │  WARN  (automatic, no LLM)             │
        │                              │   │                                        │
        │  MCP server                  │   │  lcb.check: git diff → changed         │
        │   get_schema                 │   │  methods → past fixes, risk            │
        │   run_cypher  (read-only)    │   │   ├─ CLI           python -m lcb.check │
        │   check_changes ─────────────┼──▶│   ├─ MCP tool      check_changes       │
        │  code-graph skill            │   │   ├─ git hook      lcb.hook            │
        │                              │   │   └─ VS Code ext.  vscode-extension/   │
        └──────────────────────────────┘   └────────────────────────────────────────┘
```

**One rule holds everywhere: the graph is built only by ingestion.** Every consumer reads
from it. The MCP server rejects write queries, and the warning path never writes.

---

## 3. Ingestion layer

### 3.1 Structural layer: what the code *is*

| Step | File | What it does |
|---|---|---|
| Extract | `extractor/src/Extractor.java` | Reads **bytecode** with ASM, for application classes **and every dependency JAR**. Emits classes, methods (with `line`..`line_end` spans), call sites with opcode and line, inheritance, Spring beans and injection points. About 8 s for 171 JARs. |
| Load | `lcb/load.py` | CSV → Neo4j in batches. **Incremental:** per-class content hashes (`manifest.json`) mean a rerun writes only classes that changed. History attached to methods survives a reload. |
| CHA | `lcb/load.py` (post-pass) | Class Hierarchy Analysis: for calls through an interface or superclass, adds `MAY_CALL` edges to the possible overrides, so blast-radius answers don't miss callers. |
| DI | `lcb/load.py` | Resolves Spring injection points to beans (`INJECTS`, `INJECTS_BEAN_METHOD`). |

**Why bytecode, not source:** dependencies ship as JARs with no source; bytecode gives
resolved call targets and overload-exact method identities (the JVM descriptor) for free.

### 3.2 Git layer: who changed what, and when (`lcb/git_ingest.py`)

| Produces | How |
|---|---|
| `Commit`, `Author`, `AUTHORED` | `git log --numstat`, with author identities passed through the repo's `.mailmap` |
| `TOUCHED {added, removed}` | `git log -L<line>,<line_end>:<path>` **per method**. Git follows the line range back through history itself, so it stays exact after code moves. |
| `LAST_MODIFIED` | `git blame` at `HEAD` over the method's span (uncommitted edits are ignored) |
| `Commit.fix_kind`, `issue_refs`, `FIXED` | every commit subject classified by `lcb/fixes.py`; fix commits linked to the methods they changed |

- **Finding source files in any layout:** bytecode records only `Owner.java`, so
  `SourceIndex` finds the file among `git ls-files` by package path. This works for
  multi-module builds, same-named files in different modules, and Kotlin, Groovy and Scala.
- **Incremental:** state is kept per repository in `out/state/<repo>-<hash>/`. A rerun
  reads only new commits and re-scans only the files that changed. Saved state from
  another repository, or from rewritten history, is detected and triggers a full scan.

### 3.3 Bug-fix detection from commit messages (`lcb/fixes.py`)

Most bug fixes never go through a GitHub issue. On PetClinic, about 23 fix commits touch
application methods, and the issue chain reaches only 4 of them. So commit subjects are
classified:

- `fix:` / `fix(scope):` → **conventional**
- `fixed …`, `hotfix`, `bugfix`, `regression`… → **keyword**
- `Revert "…"` → **revert**

Excluded: merges, `docs:`/`test:`/`chore:` commits, text-only fixes (typos, comments,
links, readme), and fix words that only appear inside quotes (`Polish "Fix …"`).

**Attribution** (shared with the GitHub layer):
`focus (1/methods touched) × line share × 1/(1 + files_changed/10)`. A focused,
one-method fix scores about 0.91; a sweeping change scores near 0. It ranks candidates;
it is not a probability.

### 3.4 GitHub layer: bugs and their fixes (`lcb/github_ingest.py`)

- **Fetching:** GraphQL through the `gh` CLI → `Issue`, `PR`. Results are cached in
  `out/github/<owner>__<name>/`, and reruns fetch only issues and PRs updated since the
  last run. The repository is read from the clone's `origin` remote.
- **Links:** `PART_OF` connects a commit to its PR (via the merge commit). `FIXES`
  connects a PR to an issue (GitHub's closing references, with a regex fallback).
  `CAUSED_BY` connects an issue to a method by following
  `Issue ← PR ← Commit → TOUCHED → Method`, scored by the same attribution formula.

---

## 4. The graph

```
(:Artifact)-[:PROVIDES]->(:Class)<-[:CONTAINS]-(:Package)
(:Class)-[:HAS_METHOD]->(:Method)
(:Class)-[:EXTENDS|IMPLEMENTS]->(:Class)
(:Method)-[:CALLS {exact, opcode, line, via}]->(:Method)
(:Method)-[:MAY_CALL {via:'CHA'}]->(:Method)
(:Class)-[:INJECTS]->(:Class)        (:Class)-[:INJECTS_BEAN_METHOD]->(:Method)

(:Author)-[:AUTHORED]->(:Commit)
(:Commit)-[:TOUCHED {added, removed}]->(:Method)
(:Author)-[:LAST_MODIFIED]->(:Method)
(:Commit)-[:FIXED {confidence}]->(:Method)          ← fixes found from commit messages

(:Commit)-[:PART_OF]->(:PR)-[:FIXES]->(:Issue)
(:Issue)-[:CAUSED_BY {confidence}]->(:Method)       ← fixes found through GitHub issues
```

The key property: **history and bugs attach to the same `Method` nodes as the call
graph.** One query can therefore combine "who calls this", "how often has it changed"
and "has it been fixed before".

**PetClinic today:**

| | |
|---|---|
| Classes / methods | 53,597 / 446,071 (30 classes from PetClinic itself, the rest from dependencies) |
| Call edges | 705,057 |
| Commits / authors | 1,042 / 164 |
| Issues / merged PRs | 392 / 211 |
| Fix links | 23 `CAUSED_BY` (from issues), 119 `FIXED` (from 20 fix commits) |

---

## 5. Analysis

| File | Purpose |
|---|---|
| `lcb/risk.py` | Per-method risk: fan-in + bug signal (`CAUSED_BY` plus `FIXED` commits not already behind an issue) + churn + departed author. The weights are an unfitted placeholder. |
| `lcb/backtest.py` | Historical back-test of the risk score. On PetClinic it gets recall@15 = 2/13, against 1/13 for each baseline — a direction, not evidence, on 13 positives. |
| `lcb/bench.py`, `lcb/compare_runs.py` | Token benchmarks: graph vs grep-and-read (static), and live A/B runs from transcripts. The graph is about 9.2× cheaper across 9 questions (89% fewer tokens). |

---

## 6. Delivery layer

### 6.1 Ask: the MCP server (`lcb/mcp_server.py`)

Started by any MCP client (Claude Code, Cursor, Copilot agent mode, Claude Desktop) over
stdio, registered in `.mcp.json`.

| Tool | Purpose |
|---|---|
| `get_schema` | The compact schema (746 tokens): labels, relationships, conventions, examples. The LLM reads it before writing Cypher. `details=true` adds the longer notes (Spring DI, confidence scoring, history caveats). |
| `run_cypher` | Read-only Cypher. Write keywords are rejected, results capped at 200 rows, 30 s timeout, errors returned as text so the model can fix its query. |
| `check_changes` | The change check (§6.2) on the working tree. Its description tells the agent to call it after editing code. It reads the `repo` argument, else `LCB_REPO`, else its working directory. |

There are deliberately few tools: a Cypher endpoint answers questions nobody anticipated.
`check_changes` exists because it needs the git diff, which the graph does not hold.

The **`code-graph` skill** (`.claude/skills/code-graph/SKILL.md`) teaches Claude Code
when to use the graph instead of grep, query patterns, the graph's limits, and to run
`check_changes` after editing.

### 6.2 Warn: the change check (`lcb/check.py`)

```
git diff -U0 (old side = HEAD, or the index, or a base branch)
   │  hunks: (old_start, old_count, new_start, new_count)
   ▼
methods whose span [signature .. line_end] overlaps a hunk
   │  signature found in the old source by scanning up from the first statement
   │  for the method's name (past comments and wrapped parameters)
   ▼
CAUSED_BY issues + FIXED commits (minus ones already behind an issue),
callers, last author, risk rank
   ▼
text, or --json (with line_now: where the method sits in the working tree now)
```

- **Old-side line numbers** match the graph, which describes the last build. A method is
  therefore found correctly even after edits higher up in the file have shifted it.
- **`line_now`** maps each method to its current position, so editors mark the right line.
- **Exit codes:** `0` checked, `1` flagged under `--strict`, `2` could not check.
  `--quiet` stays silent unless something is flagged.

It is delivered in four ways:

| Channel | File | Behaviour |
|---|---|---|
| CLI | `python -m lcb.check --repo R [--staged \| --base main] [--json]` | Prints the warnings |
| MCP | `check_changes` | The AI agent sees the warning after editing code |
| git hook | `python -m lcb.hook install --repo R [--block]` | Warns on commit, or stops it with `--block`. Never blocks when the check can't run. Never overwrites someone else's hook. Honours `core.hooksPath`. |
| VS Code | `vscode-extension/` | On save, underlines the signature of each flagged method (yellow by default, red with `lcb.severity: error`), with hover details, Problems panel entries and a status-bar count |

The VS Code extension contains **no analysis**: `src/core.ts` turns `lcb.check --json`
into marks, and `src/extension.ts` wires that to the editor.

---

## 7. Testing

| Suite | Count | What it proves |
|---|---|---|
| `tests/test_git_layer.py` | 19 | A scripted multi-module repo with known authors and commits. Every `TOUCHED`, `LAST_MODIFIED` and `FIXED` edge is checked against the known history, along with `.mailmap`, uncommitted edits, incremental runs and per-repo state. |
| `tests/test_fixes.py` | 5 | The commit-message classifier (fixes vs. non-fixes), issue references, attribution |
| `tests/test_ingest.py` | 11 | Build-tool detection; Maven and Gradle classes-folder discovery; `lcb ingest` on a two-module repo (both modules as application code, a sibling module's JAR copy skipped, cross-module calls and history, the graph's repository record, incremental rerun, refusal and `--replace` for another repository) |
| `tests/test_check.py` | 32 | Diff → method mapping (shifted lines, insertions, signature edits, signature finding), flagging thresholds, `--staged`, deduplication, the MCP tool over the real stdio protocol, and the git hook through real `git commit` runs |
| `tests/test_setup.py` | 5 | `lcb setup` with fake `claude`/`code` programs: the MCP registration (scope, repository, environment), extension install, user config, hook, missing tools reported with instructions, a foreign hook left alone, reruns replacing rather than failing, nothing written into the repository |
| `vscode-extension` (`npm test`) | 8 | Output → marks, messages, exit codes, settings and the `lcb setup` user config, plus 2 runs of the real `lcb.check` through the extension's command builder |

The Python end-to-end tests run against a **throwaway Neo4j** (`tests/neo4j_sandbox.py`)
on random ports, so the real graph is never touched. Each fix was also mutation-checked:
reverting it makes the matching test fail.

```bash
PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_fixes tests.test_git_layer tests.test_check tests.test_ingest tests.test_setup
cd vscode-extension && npm test
```

**Not yet tested:** the extension running inside a real VS Code window. Its VS Code-facing
code compiles against the API types, but has not been exercised in the editor.

---

## 8. Does it work for any repository?

**Generic today:**
- The extractor and loader work on any compiled JVM project (Java, Kotlin, Scala, Groovy).
- The git layer finds source files in any layout, honours `.mailmap`, and keeps separate
  state per repository.
- The GitHub layer reads the repository from `origin`, with a separate cache per repository.
- Fix detection from commit messages works for any git repository, whatever issue tracker
  it uses.
- The check, MCP tool, hook and extension all take the repository as a parameter.

- **Onboarding is one command:** `lcb ingest <repo>` (`lcb/ingest.py`). It detects
  Maven or Gradle, builds every module, collects the dependencies, finds every module's
  compiled classes, then extracts, loads, and ingests git and GitHub history. Finally it
  prints the MCP, hook and VS Code setup for that repository.
- **Multi-module builds:** the extractor takes any number of `--classes`/`--jars`
  folders. When a sibling module's JAR appears among the dependencies, its classes are
  skipped there, so module code is never indexed twice.
- **Installable:** `pip install -e .` provides the `lcb` command (`ingest`, `check`,
  `hook`, `mcp-config`, `mcp`).

**Still open:**

| Gap | Effect | Fix |
|---|---|---|
| **One repository per Neo4j database.** Nodes carry no repository id; `(:LcbMeta)` records which repository is loaded. | `lcb ingest` refuses a second repository unless `--replace` (which wipes the first) | Tag nodes with a repository id, or one database per repository |
| **The Gradle path has not run against a real Gradle build** (this machine could not download Gradle). It is unit-tested, and the Maven path is verified end to end. | Gradle projects may hit an untested edge | Run `lcb ingest <gradle-repo>` on a machine with network access |
| **Editable install only.** The extractor lives outside the Python package, so a non-editable wheel would not find it. | Install with `pip install -e .` from a clone | Ship the extractor and ASM JARs as package data |
| Builds that need more than the defaults (custom profiles, private registries) | `lcb ingest` stops, showing the first errors and the log path | `--build-arg <flag>`, `--jdk <path>`, `--build-cmd "..."`, or build yourself and pass `--skip-build`. Java choice, download timeouts, progress and skipping non-essential steps (Docker, style checks…) are automatic. |
| `lcb.health` and `lcb.bench` default to PetClinic numbers | Cosmetic | Make them take the repository |

**Inherent limits** (they apply to every repository): a compiled build is required;
reflection, proxies and runtime wiring are invisible; JDK calls aren't indexed; bug links
are a ranking heuristic; new methods have no history.

---

## 9. Giving it to a tester

The tester needs a JDK 21, Python 3.10+, Neo4j (5.x or 2025+) running, git, and
optionally the `gh` CLI (for GitHub issues). Then:

```bash
git clone <this project> lcb-home && cd lcb-home
python3 -m venv .venv && .venv/bin/pip install -e .      # the `lcb` command
.venv/bin/lcb setup /path/to/their/repo
```

`lcb setup` (`lcb/setup.py`) does everything in one go:
1. **Ingest** the repository (`lcb ingest`).
2. **Record this install** in `~/.lcb/config.json` (Python, checkout, Neo4j address).
   The VS Code extension reads that file, so it needs **no settings**.
3. **Register the MCP server for that repository** in Claude Code
   (`claude mcp add --scope local`), or print the config for other MCP clients.
4. **Install the VS Code extension** (`code --install-extension`), building the `.vsix`
   first if needed.
5. **Install the git hook** if the tester says so (`--hook warn|block`, or asked
   interactively).

**Nothing machine-specific is written into the repository**, which matters when it is a
team's shared repo. The MCP registration lives in the tester's own Claude Code config,
the extension config in their home folder, and the hook in `.git/hooks`, which is never
committed. Any step that can't run (no `claude`, no `code`, no npm) prints what to do
instead without stopping the others, and a rerun replaces the earlier setup rather than
failing.

**Still manual:** installing Neo4j and the JDK, and cloning this project rather than
installing a published package (see §8).

---

## 10. Repository layout

```
extractor/src/Extractor.java    bytecode → CSV (ASM)
lcb/load.py                     CSV → Neo4j, incremental, CHA, DI resolution
lcb/git_ingest.py               git history → Commit/Author/TOUCHED/LAST_MODIFIED/FIXED
lcb/fixes.py                    fix detection from commit messages; shared attribution score
lcb/github_ingest.py            issues/PRs → Issue/PR/PART_OF/FIXES/CAUSED_BY
lcb/risk.py, lcb/backtest.py    risk score and its historical back-test
lcb/setup.py                    `lcb setup`: ingest, then connect MCP, VS Code and the git hook
lcb/ingest.py                   `lcb ingest`: build → extract → load → git → GitHub, one command
lcb/__main__.py, pyproject.toml the `lcb` command (ingest, check, hook, mcp-config, mcp)
lcb/check.py                    change check: diff → methods → past fixes
lcb/hook.py                     git pre-commit hook installer
lcb/mcp_server.py               MCP: get_schema, run_cypher, check_changes
lcb/bench.py, compare_runs.py   token benchmarks
lcb/query.py, cq, lcb/health.py ad-hoc querying and graph health check
.claude/skills/code-graph/      skill teaching Claude Code to use the graph
vscode-extension/               VS Code extension (TypeScript)
tests/                          Python test suites + throwaway Neo4j sandbox
targets/petclinic/              example repository (Spring PetClinic)
out/                            extracted CSVs, per-repo state, GitHub cache, benchmark output
demo/                           demo runbook and ready-made queries
```

---

## 11. Status

| Area | Status |
|---|---|
| Structural graph (bytecode, dependencies, CHA, Spring DI, incremental load) | ✅ |
| Git history per method, `.mailmap`, incremental, any source layout | ✅ |
| Fix detection from commit messages | ✅ |
| GitHub issues and PRs, bug → method links | ✅ |
| Risk score and back-test | ✅ built; ⚠️ unvalidated (PetClinic too small) |
| MCP server (3 tools) and skill | ✅ |
| Change check: CLI, MCP tool, git hook, VS Code extension | ✅ (extension not yet run in a real VS Code window) |
| One-command onboarding (`lcb ingest`), multi-module extraction, `lcb` command | ✅ Maven verified end to end on PetClinic; Gradle unit-tested only |
| Several repositories in one graph, published package | ⬜ ([§8](#8-does-it-work-for-any-repository)) |
| Validation on a larger, real repository | ⬜ |
