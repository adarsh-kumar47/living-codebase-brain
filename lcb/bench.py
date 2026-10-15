"""Token benchmark: answering code questions via the graph vs via source files.

Measures the CONTEXT an LLM must read to answer a question, two ways:

  Arm A (graph)    schema document + the Cypher query + the returned rows
  Arm B (baseline) what a grep-and-read agent must load: the grep hit list, plus
                   the full text of every source file containing a match

Arm B models what Claude Code actually does -- grep for the symbol, then read the
matching files. It is deliberately the *fair* baseline rather than a whole-repo
dump, which would flatter the result without being honest.

Two ways this measurement is conservative (it understates the graph's advantage):
  - The schema is charged to arm A on every question, though in practice it is sent
    once per session.
  - Arm B is charged a single read pass. A real agent loop re-sends the accumulated
    transcript on every turn, so its true cost grows super-linearly.

Usage: python -m lcb.bench --repo targets/petclinic --out out/bench
"""

import argparse
import json
import os
import re

import tiktoken

from lcb.mcp_server import SCHEMA_DOC
from lcb.query import run

ENC = tiktoken.get_encoding("o200k_base")


def ntok(text):
    return len(ENC.encode(text or ""))


# Each question carries the Cypher the graph answers it with, and the symbol a
# grep-based agent would search for. `baseline_possible` records whether the answer
# is derivable from application source at all -- for dependency questions it is not,
# which is a result in itself rather than a number to fake.
QUESTIONS = [
    {
        "id": "q1_callers",
        "question": "Which methods call OwnerRepository.findById, and at what line?",
        "cypher": """
            MATCH (caller:Method)-[c:CALLS]->(m:Method)
            MATCH (cls:Class)-[:HAS_METHOD]->(m)
            WHERE cls.fqn ENDS WITH 'OwnerRepository' AND m.name = 'findById'
            RETURN caller.owner AS caller, caller.name AS method, c.line AS line
            ORDER BY caller, line
        """,
        "grep": r"findById",
        "baseline_possible": True,
    },
    {
        "id": "q2_blast_radius",
        "question": "Blast radius: what application code transitively calls Owner.getPet, up to 3 hops?",
        "cypher": """
            MATCH (m:Method)<-[:CALLS*1..3]-(caller:Method)
            MATCH (cls:Class)-[:HAS_METHOD]->(m)
            WHERE cls.fqn ENDS WITH '.Owner' AND m.name = 'getPet'
              AND caller.is_external = false
            RETURN DISTINCT caller.owner AS caller, caller.name AS method
            ORDER BY caller, method
        """,
        "grep": r"getPet",
        "baseline_possible": True,
    },
    {
        "id": "q3_dependency_origin",
        "question": "Which dependency JAR provides the Spring interface that OwnerRepository extends?",
        "cypher": """
            MATCH (c:Class)-[:EXTENDS|IMPLEMENTS]->(parent:Class)<-[:PROVIDES]-(a:Artifact)
            WHERE c.fqn ENDS WITH 'OwnerRepository'
            RETURN parent.fqn AS interface, a.name AS jar
        """,
        "grep": r"Repository",
        # The JAR a type comes from is not stated anywhere in the .java files; an agent
        # reading source can see the import but not which artifact supplies it.
        "baseline_possible": False,
    },
    {
        "id": "q4_hotspots",
        "question": "Which application methods have the most distinct callers?",
        "cypher": """
            MATCH (m:Method)<-[:CALLS]-(caller:Method)
            WHERE m.is_external = false
            RETURN m.owner AS owner, m.name AS method,
                   count(DISTINCT caller) AS callers
            ORDER BY callers DESC LIMIT 15
        """,
        "grep": r"public",
        "baseline_possible": True,
    },
    {
        "id": "q5_framework_surface",
        "question": "Which application classes extend or implement a type from a dependency?",
        "cypher": """
            MATCH (c:Class {is_external: false})-[:EXTENDS|IMPLEMENTS]->(p:Class {is_external: true})
            RETURN c.fqn AS app_class, p.fqn AS external_supertype
            ORDER BY app_class
        """,
        "grep": r"implements|extends",
        "baseline_possible": True,
    },
    {
        "id": "q6_unused_methods",
        "question": "Which application methods are never called anywhere - dead code candidates?",
        "cypher": """
            MATCH (m:Method)
            WHERE m.is_external = false
              AND NOT (m)<-[:CALLS]-()
              AND NOT m.name STARTS WITH '<'
            RETURN m.owner AS owner, m.name AS method
            ORDER BY owner, method LIMIT 40
        """,
        # To prove a method is never called you must inspect every method in the
        # codebase, so the honest baseline is a scan of all of it.
        "grep": r"public|protected|private",
        "baseline_possible": True,
    },
    {
        "id": "q7_jar_usage",
        "question": "Which dependency JARs does the application actually call into, and how heavily?",
        "cypher": """
            MATCH (caller:Method {is_external: false})-[:CALLS]->(t:Method {is_external: true})
            MATCH (c:Class)-[:HAS_METHOD]->(t)
            MATCH (a:Artifact)-[:PROVIDES]->(c)
            RETURN a.name AS jar, count(*) AS calls
            ORDER BY calls DESC LIMIT 10
        """,
        # Source shows imported package names, never which artifact supplies them,
        # and an import says nothing about whether the code is exercised at all.
        "grep": r"^import org\.springframework",
        "baseline_possible": False,
    },
    {
        "id": "q8_class_dependencies",
        "question": "What does OwnerController depend on, application types and framework types alike?",
        "cypher": """
            MATCH (c:Class {is_external: false})-[:HAS_METHOD]->(:Method)-[:CALLS]->(t:Method)
            MATCH (tc:Class)-[:HAS_METHOD]->(t)
            WHERE c.fqn ENDS WITH 'OwnerController' AND tc.fqn <> c.fqn
            RETURN DISTINCT tc.fqn AS depends_on, tc.is_external AS from_dependency
            ORDER BY from_dependency, depends_on LIMIT 20
        """,
        "grep": r"OwnerController",
        "baseline_possible": True,
    },
    {
        "id": "q9_jar_inventory",
        "question": "How many classes does each dependency JAR contribute to the indexed codebase?",
        "cypher": """
            MATCH (a:Artifact {is_external: true})-[:PROVIDES]->(c:Class)
            RETURN a.name AS jar, count(c) AS classes
            ORDER BY classes DESC LIMIT 10
        """,
        # Nothing in the application source enumerates the contents of a JAR.
        "grep": r"^import ",
        "baseline_possible": False,
    },
]


def source_files(repo):
    out = []
    for root, _, files in os.walk(os.path.join(repo, "src", "main", "java")):
        for f in files:
            if f.endswith(".java"):
                out.append(os.path.join(root, f))
    return sorted(out)


def grep_baseline(repo, pattern):
    """Simulate: grep for the symbol, then read every matching file in full."""
    rx = re.compile(pattern)
    hits, read_files, total = [], [], 0

    for path in source_files(repo):
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        matched = [
            f"{os.path.relpath(path, repo)}:{i}:{line.strip()}"
            for i, line in enumerate(text.splitlines(), 1)
            if rx.search(line)
        ]
        if matched:
            hits.extend(matched)
            read_files.append(os.path.relpath(path, repo))
            total += ntok(text)

    grep_out = "\n".join(hits)
    return {
        "grep_tokens": ntok(grep_out),
        "file_tokens": total,
        "total_tokens": ntok(grep_out) + total,
        "files_read": len(read_files),
        "grep_hits": len(hits),
        "files": read_files,
    }


def graph_arm(q):
    rows = run(q["cypher"])
    rendered = "\n".join(str(r) for r in rows)
    return {
        "schema_tokens": ntok(SCHEMA_DOC),
        "query_tokens": ntok(q["cypher"]),
        "result_tokens": ntok(rendered),
        "total_tokens": ntok(SCHEMA_DOC) + ntok(q["cypher"]) + ntok(rendered),
        "rows": len(rows),
        "answer": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="targets/petclinic")
    ap.add_argument("--out", default="out/bench")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    results = []

    for q in QUESTIONS:
        g = graph_arm(q)
        b = grep_baseline(args.repo, q["grep"])
        ratio = b["total_tokens"] / g["total_tokens"] if g["total_tokens"] else 0
        results.append({**q, "graph": g, "baseline": b, "ratio": ratio})

    with open(os.path.join(args.out, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)

    # ---- report ----
    print("\nTOKEN COST PER QUESTION (context an LLM must read to answer)\n")
    hdr = f"{'question':<34}{'graph':>9}{'baseline':>11}{'saved':>9}{'ratio':>8}"
    print(hdr)
    print("-" * len(hdr))

    tg = tb = 0
    for r in results:
        g, b = r["graph"]["total_tokens"], r["baseline"]["total_tokens"]
        tg += g
        tb += b
        saved = 100.0 * (b - g) / b if b else 0
        flag = "" if r["baseline_possible"] else "  *"
        print(f"{r['id']:<34}{g:>9,}{b:>11,}{saved:>8.1f}%{r['ratio']:>7.1f}x{flag}")

    print("-" * len(hdr))
    saved_all = 100.0 * (tb - tg) / tb if tb else 0
    print(f"{'TOTAL':<34}{tg:>9,}{tb:>11,}{saved_all:>8.1f}%{tb / max(tg, 1):>7.1f}x")

    print("\n  * baseline cannot actually answer this question from application source;")
    print("    the token figure shown is what it would spend before failing.")
    print(f"\n  schema overhead charged to the graph arm every question: "
          f"{ntok(SCHEMA_DOC):,} tokens")
    print(f"  results written to {os.path.join(args.out, 'results.json')}")


if __name__ == "__main__":
    main()
