"""MCP server exposing the code graph to an LLM.

Deliberately few tools. Pre-baked queries (get_blast_radius, get_bug_history...)
would cap the model at questions we anticipated; giving it the schema and a Cypher
endpoint lets it answer questions we did not.

The one exception is check_changes, and it is not a pre-baked query: it needs the
working tree's `git diff`, mapped onto method line spans by old-side line number
(lcb/check.py), which Cypher alone cannot see. It exists so an agent that has just
edited code learns, unprompted, that it touched a method with a bug history.

The repository check_changes looks at is its `repo` argument, else LCB_REPO, else the
server's working directory.

Run:  python -m lcb.mcp_server
"""

import os
import subprocess

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from neo4j import GraphDatabase

from lcb import check

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")

MAX_ROWS = 200
TIMEOUT_S = 30

# Cypher clauses that mutate. The graph is a derived artifact rebuilt by the loader,
# so the query surface stays read-only; a write here would silently corrupt results
# that the model then reports as fact.
FORBIDDEN = (
    "create", "delete", "detach", "merge", "set ", "remove",
    "drop", "load csv", "call db.create", "foreach",
)

# Sent to the client when it connects; Claude Code and other MCP clients put it in the
# assistant's context. Without it, an assistant in a repository that has no
# code-graph skill sees the tools but keeps its habit of grepping files -- observed
# on the first real-repo trial. Kept short: it is read in every session.
INSTRUCTIONS = """\
Living Codebase Brain: a graph of this repository's compiled Java code (application \
classes and every dependency JAR) plus its git and GitHub history, attached to each \
method.

Prefer it over grep, reading files or running git log for: who calls a method or what \
it calls; the blast radius of a change; class hierarchies; which dependency JAR \
provides a class; who changed a method and when; whether a method was fixed before. \
Call get_schema once, then run_cypher. Answers are exact (resolved from bytecode) and \
cost far fewer tokens than reading files. Read source files only for the logic inside \
a method body.

After editing Java/Kotlin source, call check_changes before finishing. If it flags a \
method, read the past fix it cites, make sure the change does not undo it, and tell \
the user."""

mcp = MCPServer("living-codebase-brain", instructions=INSTRUCTIONS)

_driver = None


def driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    return _driver


@mcp.tool(
    description=(
        "Return the code-graph schema: node labels with their properties, the "
        "relationship types that connect them, key conventions and example queries. "
        "Call this before writing Cypher. Pass details=true for the longer notes on "
        "Spring dependency injection, how bug-link confidence is computed, and the "
        "limits of the history layer -- only needed for questions about those."
    )
)
def get_schema(details: bool = False) -> str:
    return SCHEMA_DOC + ("\n" + SCHEMA_NOTES if details else "")


@mcp.tool(
    description=(
        "Run a read-only Cypher query against the Java code graph and return the rows "
        "as text. Writes are rejected. Results are capped at 200 rows, so aggregate or "
        "LIMIT in the query itself. If the query errors, the error is returned so it "
        "can be corrected and retried."
    )
)
def run_cypher(query: str) -> str:
    lowered = query.lower()
    for kw in FORBIDDEN:
        if kw in lowered:
            return f"REJECTED: query contains '{kw.strip()}'. This endpoint is read-only."

    try:
        with driver().session() as s:
            result = s.run(query, timeout=TIMEOUT_S)
            rows = [r.data() for _, r in zip(range(MAX_ROWS), result)]
    except Exception as e:  # returned, not raised, so the model can self-correct
        return f"ERROR: {type(e).__name__}: {e}"

    if not rows:
        return "(0 rows)"

    cols = list(rows[0].keys())
    lines = [" | ".join(cols), "-" * 40]
    for r in rows:
        lines.append(" | ".join(str(r.get(c, "")) for c in cols))
    if len(rows) == MAX_ROWS:
        lines.append(f"... truncated at {MAX_ROWS} rows")
    return "\n".join(lines)


@mcp.tool(
    description=(
        "Check uncommitted code changes for methods with a bug history. Call this after "
        "editing Java/Kotlin source and before declaring the work done. It maps the git "
        "diff onto the methods it touches and reports any that were fixed before -- via a "
        "GitHub issue or a commit whose message says it fixed something -- with the issue "
        "or commit, confidence, callers and last author. A flagged method is not an error: "
        "re-read that fix to be sure the change does not reintroduce the bug, and tell "
        "the user. Arguments: repo (path to the git repository; default: the configured "
        "project), staged (only what is staged for commit), base (compare against a ref "
        "such as 'main' instead of HEAD), min_confidence (0-1, default 0.2), show_all "
        "(also list changed methods without history, and weaker links)."
    ),
    annotations=ToolAnnotations(readOnlyHint=True),
)
def check_changes(repo: str | None = None, staged: bool = False, base: str | None = None,
                  min_confidence: float = 0.2, show_all: bool = False) -> str:
    repo = repo or os.environ.get("LCB_REPO") or os.getcwd()
    if staged and base:
        return "ERROR: pass staged or base, not both."
    try:
        result = check.check(repo, staged=staged, base=base, min_confidence=min_confidence)
    except subprocess.CalledProcessError as e:
        return f"ERROR: git failed in {repo}: {(e.stderr or '').strip()}"
    except Exception as e:  # returned, not raised, so the model can react
        return f"ERROR: {type(e).__name__}: {e}"
    if not result["files_changed"]:
        return f"No changes to source files in {repo} ({'staged' if staged else base or 'vs HEAD'})."
    return check.render(result, show_all)


# What get_schema returns by default. It is read before nearly every query, so it is
# kept to what writing correct Cypher needs; the longer explanations are in
# SCHEMA_NOTES, returned with details=true. lcb.bench charges this text to every
# question, so growth here shows up directly as token cost.
SCHEMA_DOC = """\
Java code graph from JVM bytecode: application classes + dependency JARs, plus git
and GitHub history. Read-only.

NODES
  Class    fqn, name, package, is_interface, is_abstract, is_external, is_generated,
           source_file, artifact, is_spring_bean, stereotype
  Method   fqn, owner, name, descriptor, is_static, is_abstract, is_external,
           is_generated, line, line_end, artifact, is_bean_factory, bean_type
  Package  name          Artifact  name, is_external
  Author   email, name   Commit    sha, message, timestamp (epoch s), additions,
                                   deletions, files_changed, fix_kind, issue_refs
  Issue    number, title, state, opened_at, closed_at, labels
  PR       number, title, merged_at, files_changed
  LcbMeta  repo, ingested_at  (which repository the graph holds)

RELATIONSHIPS
  Package-CONTAINS->Class   Artifact-PROVIDES->Class   Class-HAS_METHOD->Method
  Class-EXTENDS->Class      Class-IMPLEMENTS->Class
  Method-CALLS{exact,opcode,line,via}->Method  Method-MAY_CALL{via:'CHA'}->Method
  Class-INJECTS->Class      Class-INJECTS_BEAN_METHOD->Method
  Author-AUTHORED->Commit   Commit-TOUCHED{added,removed}->Method
  Author-LAST_MODIFIED->Method
  Commit-FIXED{confidence}->Method          (fix found from the commit message)
  Commit-PART_OF->PR-FIXES->Issue-CAUSED_BY{confidence}->Method  (fix via an issue)

CONVENTIONS
  Method.fqn = "<class fqn>#<name><JVM descriptor>". Match Method.name to ignore
    overloads, Method.fqn for one exact overload.
  is_external: false = application code, true = dependency JAR. Filter on it.
  CALLS.exact: true = static/special call (provable target); false = virtual call to
    the declared type. MAY_CALL adds possible overrides (widens a blast radius).
  A call to an inherited method points at the class that declares it; CALLS.via is the
    class named in the code (PageHelper.startPage -> PageMethod#startPage, via PageHelper).
  is_generated: generated code (MyBatis models, OpenAPI clients...). Still in the call
    graph, but it has no git history.
  line..line_end = the method body's source lines.
  confidence (FIXED, CAUSED_BY) ranks candidates; it is not a probability. A commit
    behind an issue-fixing PR has both; skip it on the FIXED side with
    NOT EXISTS { (c)-[:PART_OF]->(:PR)-[:FIXES]->(:Issue) }.
  Not indexed: JDK calls (java.*, javax.*), reflection, runtime proxies, test code.
  History exists only for application methods. Always LIMIT; rows cap at 200.

EXAMPLES
  Callers:      MATCH (c:Method)-[:CALLS]->(m:Method {name:'findById'})
                RETURN c.owner, c.name, c.line
  Blast radius: MATCH (m:Method {name:'getPet'})<-[:CALLS*1..3]-(c:Method)
                WHERE c.is_external = false RETURN DISTINCT c.owner, c.name LIMIT 50
  JAR of type:  MATCH (a:Artifact)-[:PROVIDES]->(:Class {name:'JpaRepository'}) RETURN a.name
  Past fixes:   MATCH (m:Method {name:'addPet'})
                OPTIONAL MATCH (i:Issue)-[:CAUSED_BY]->(m)
                OPTIONAL MATCH (c:Commit)-[:FIXED]->(m)
                RETURN collect(DISTINCT i.title), collect(DISTINCT c.message)
"""

# Returned by get_schema(details=true): the background behind the conventions above.
SCHEMA_NOTES = """\
NOTES

Spring dependency injection
  is_spring_bean/stereotype mark @Component/@Service/@Repository/@Controller/
  @RestController/@Configuration classes; is_bean_factory/bean_type mark @Bean methods.
  INJECTS / INJECTS_BEAN_METHOD (properties member_kind, member_name, param_index,
  qualifier) resolve each injection point -- constructor parameter, @Autowired field
  or setter -- to its candidate beans. Several candidates are kept, not collapsed.
  Spring Data repository interfaces have no static implementation (Spring generates it
  at runtime), so injections of them stay unresolved: a missing INJECTS edge is not
  proof that nothing is injected.
    MATCH (c:Class {name:'OwnerController'})-[r:INJECTS|INJECTS_BEAN_METHOD]->(t)
    RETURN r.member_name, r.param_index, t.fqn

Calls
  CALLS.opcode is the JVM invoke instruction. MAY_CALL is Class Hierarchy Analysis: for
  an inexact call, every subtype override with the same name and descriptor. It
  over-approximates on purpose -- a blast radius should not miss a caller -- and is
  computed for calls from application code and for calls into application overrides.

History (lcb.git_ingest)
  TOUCHED comes from `git log -L` per method, so it follows the method through
  history even after code moves. LAST_MODIFIED is `git blame` at HEAD. Authors go
  through the repository's .mailmap. All of it reflects the last ingest, not live.
    MATCH (cm:Commit)-[:TOUCHED]->(m:Method)
    RETURN m.owner, m.name, count(cm) AS commits ORDER BY commits DESC LIMIT 10

Fix commits (lcb.fixes)
  Commit.fix_kind is 'conventional' ("fix: ..."), 'keyword' ("fixed ...", "hotfix"),
  'revert' (Revert "...") or null. Merges, docs/test/chore commits and text-only fixes
  (typos, comments, links) are not fixes. Commit.issue_refs lists "#N" numbers from
  the subject; join them to Issue.number for pre-PR-era links.

Confidence
  FIXED and CAUSED_BY share one score per (fix commit, method):
    focus (1 / methods the commit touched) x line share (this method's changed lines
    / all changed lines) x size (1 / (1 + files_changed / 10)).
  A focused one-method fix scores about 0.9; a sweeping change scores near 0. Use 0.2
  as a sensible threshold.

GitHub (lcb.github_ingest)
  PART_OF links a PR's merge commit; FIXES comes from GitHub's closing references, with
  a "fixes #N" regex fallback. PRs whose merge commit is missing from the local
  history (rewritten early history) have no PART_OF -- a reported gap, not an error.
    MATCH (i:Issue {number: 2333})-[c:CAUSED_BY]->(m:Method)
    RETURN m.owner, m.name, c.confidence ORDER BY c.confidence DESC
"""


if __name__ == "__main__":
    mcp.run(transport="stdio")
