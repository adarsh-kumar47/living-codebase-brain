"""GitHub ingestion: issues, PRs, and the bug -> method linkage chain (phase 7).

Builds on the git layer from phase 6 (lcb/git_ingest.py). New node labels only --
nothing from the structural or git layers is touched.

    (:Issue {number, title, state, opened_at, closed_at, labels})
    (:PR    {number, title, merged_at, files_changed})
    (:Commit)-[:PART_OF]->(:PR)
    (:PR)-[:FIXES]->(:Issue)
    (:Issue)-[:CAUSED_BY {confidence}]->(:Method)

Inference chain (README Sec.10.3):

    Issue #482 --FIXES-- PR #510 --PART_OF-- commit abc123 --TOUCHED--> Method

PART_OF anchors each merged PR to its GitHub merge commit (`mergeCommit.oid`), not
the PR's pre-merge branch commits -- those are frequently squashed away and never
exist in `git log` on the default branch, whereas the merge commit always does
(when the branch hasn't been rewritten -- see the age caveat below). The merge
commit's TOUCHED edges were already computed in phase 6, so no new diff parsing is
needed here: CAUSED_BY is just that same per-method (added, removed) data, reshaped
into a confidence score.

FIXES comes from GitHub's own closing references (`closingIssuesReferences`, i.e.
"Fixes #N" parsed by GitHub itself from the merge metadata) with a regex fallback
over the PR title/body for repos or eras that didn't use that convention -- both
named as the intended approach in Sec.10.3.

**Confidence is a relative ranking heuristic, not a calibrated probability** -- in
the same spirit as CHA's deliberate over-approximation, it is designed to be honest
about what it is. It combines three signals named in Sec.10.3 (focus, line share,
refactor size), multiplied, so it only ranks fixes against each other:

    focus      = 1 / (distinct methods the commit touched)       -- focused fix -> higher
    line_share = this method's changed lines / the commit's total -- bigger hunk -> higher
    size       = 1 / (1 + pr.files_changed / 10)                 -- big PR -> lower

Known gap, reported rather than hidden: GitHub's GraphQL `mergeCommit.oid` for PRs
merged before ~2014 does not resolve against this clone's history -- the project's
early history was evidently rewritten at some point (squashed or re-based), so
those merge commits no longer exist as objects. PART_OF is simply not created for
those PRs (an unresolved merge commit is counted and reported, not invented).

Phase 9 (incremental ingestion): by default, a run after the first one only fetches
issues/PRs updated since the last run (GraphQL `orderBy: UPDATED_AT DESC`, paginating
only until an already-seen `updatedAt` is reached) and merges them into the existing
`out/github/*.json` cache by `number` -- everything else carries forward untouched.
`--full` bypasses this and re-fetches everything, same as the original phase 7 behavior.

The GitHub repository is read from the local clone's `origin` remote (`--repo`), or
given directly as `--repo-slug owner/name`. Cache and incremental state live under
out/github/<owner>__<name>/, one directory per repository.

Usage:
    python -m lcb.github_ingest --repo path/to/repo [--full] [--wipe]
    python -m lcb.github_ingest --repo-slug owner/name [--full] [--wipe]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

from neo4j import GraphDatabase

from lcb.fixes import attribution

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")

CONSTRAINTS = [
    "CREATE CONSTRAINT issue_number IF NOT EXISTS FOR (i:Issue) REQUIRE i.number IS UNIQUE",
    "CREATE CONSTRAINT pr_number IF NOT EXISTS FOR (p:PR) REQUIRE p.number IS UNIQUE",
]

# "Fixes #123", "closes: #45", "Resolved #7" -- case-insensitive, optional colon.
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*#(\d+)", re.IGNORECASE)

PR_FIELDS = """
        number title body mergedAt changedFiles updatedAt
        mergeCommit { oid }
        closingIssuesReferences(first: 10) { nodes { number } }
"""

ISSUE_FIELDS = """
        number title state createdAt closedAt updatedAt
        labels(first: 10) { nodes { name } }
"""

# Full-history pass: oldest first, paginate to the end. Used on first run / --full.
PR_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(states: MERGED, first: 100, after: $cursor,
                  orderBy: {{field: CREATED_AT, direction: ASC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{{PR_FIELDS}}}
    }}
  }}
}}
"""

ISSUE_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    issues(first: 100, after: $cursor, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{{ISSUE_FIELDS}}}
    }}
  }}
}}
"""

# Incremental pass (phase 9): most-recently-updated first, so paginate() can stop
# as soon as it reaches an item already covered by the last ingestion.
PR_QUERY_BY_UPDATED = f"""
query($owner: String!, $name: String!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(states: MERGED, first: 100, after: $cursor,
                  orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{{PR_FIELDS}}}
    }}
  }}
}}
"""

ISSUE_QUERY_BY_UPDATED = f"""
query($owner: String!, $name: String!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    issues(first: 100, after: $cursor, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{{ISSUE_FIELDS}}}
    }}
  }}
}}
"""


def gh_graphql(query, **fields):
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in fields.items():
        args += ["-f", f"{k}={v}"]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def paginate(query, owner, name, connection_path, stop_before_updated_at=None):
    """connection_path: e.g. ('pullRequests',) under repository.

    stop_before_updated_at: for the UPDATED_AT-DESC incremental queries, stop as
    soon as a node this old is reached -- everything after it in the (descending)
    order was already covered by the previous ingestion, so there is no need to
    keep paginating through the rest of the repo's history."""
    cursor = None
    while True:
        data = gh_graphql(query, owner=owner, name=name, **({"cursor": cursor} if cursor else {}))
        conn = data["data"]["repository"]
        for key in connection_path:
            conn = conn[key]
        for node in conn["nodes"]:
            if stop_before_updated_at and node["updatedAt"] <= stop_before_updated_at:
                return
            yield node
        if not conn["pageInfo"]["hasNextPage"]:
            return
        cursor = conn["pageInfo"]["endCursor"]


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# git@github.com:owner/name.git, https://github.com/owner/name(.git), ssh://git@github.com/owner/name
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def slug_from_remote(repo, remote="origin"):
    url = subprocess.run(["git", "-C", repo, "remote", "get-url", remote],
                         capture_output=True, text=True, check=True).stdout.strip()
    m = GITHUB_REMOTE_RE.search(url)
    if not m:
        raise SystemExit(f"remote '{remote}' of {repo} is not a GitHub URL ({url}); pass --repo-slug owner/name")
    return f"{m.group(1)}/{m.group(2)}"


def merge_by_number(cached, updates):
    """New/changed items replace their old entry (by `number`); everything else
    in the cache is carried forward untouched."""
    by_number = {item["number"]: item for item in (cached or [])}
    for item in updates:
        by_number[item["number"]] = item
    return list(by_number.values())


def fetch_prs(owner, name, stop_before_updated_at=None):
    query = PR_QUERY_BY_UPDATED if stop_before_updated_at else PR_QUERY
    return paginate(query, owner, name, ("pullRequests",), stop_before_updated_at)


def fetch_issues(owner, name, stop_before_updated_at=None):
    query = ISSUE_QUERY_BY_UPDATED if stop_before_updated_at else ISSUE_QUERY
    return paginate(query, owner, name, ("issues",), stop_before_updated_at)


def linked_issue_numbers(pr):
    """GitHub's own closing references, falling back to a title/body regex scan
    only when GitHub recorded none -- the authoritative source wins when present."""
    nums = {n["number"] for n in pr["closingIssuesReferences"]["nodes"]}
    if nums:
        return nums
    text = (pr.get("title") or "") + "\n" + (pr.get("body") or "")
    return {int(n) for n in CLOSES_RE.findall(text)}


# --- loading -----------------------------------------------------------------


def load_issues(session, issues):
    rows = [
        {
            "number": i["number"],
            "title": i["title"],
            "state": i["state"],
            "opened_at": i["createdAt"],
            "closed_at": i["closedAt"],
            "labels": [l["name"] for l in i["labels"]["nodes"]],
        }
        for i in issues
    ]
    session.run(
        """
        UNWIND $rows AS row
        MERGE (i:Issue {number: row.number})
        SET i.title = row.title, i.state = row.state,
            i.opened_at = row.opened_at, i.closed_at = row.closed_at,
            i.labels = row.labels
        """,
        rows=rows,
    )
    return len(rows)


def load_prs_and_fixes(session, prs):
    pr_rows = [
        {
            "number": p["number"],
            "title": p["title"],
            "merged_at": p["mergedAt"],
            "files_changed": p["changedFiles"],
        }
        for p in prs
    ]
    session.run(
        """
        UNWIND $rows AS row
        MERGE (p:PR {number: row.number})
        SET p.title = row.title, p.merged_at = row.merged_at,
            p.files_changed = row.files_changed
        """,
        rows=pr_rows,
    )

    part_of_rows = [
        {"pr": p["number"], "sha": p["mergeCommit"]["oid"]}
        for p in prs if p["mergeCommit"]
    ]
    part_of_result = session.run(
        """
        UNWIND $rows AS row
        MATCH (cm:Commit {sha: row.sha})
        MATCH (p:PR {number: row.pr})
        MERGE (cm)-[:PART_OF]->(p)
        RETURN count(*) AS c
        """,
        rows=part_of_rows,
    ).single()["c"]

    fixes_rows = [
        {"pr": p["number"], "issue": n}
        for p in prs for n in linked_issue_numbers(p)
    ]
    fixes_result = session.run(
        """
        UNWIND $rows AS row
        MATCH (p:PR {number: row.pr})
        MATCH (i:Issue {number: row.issue})
        MERGE (p)-[:FIXES]->(i)
        RETURN count(*) AS c
        """,
        rows=fixes_rows,
    ).single()["c"]

    return {
        "prs": len(pr_rows),
        "part_of_attempted": len(part_of_rows),
        "part_of_linked": part_of_result,
        "fixes_candidates": len(fixes_rows),
        "fixes_linked": fixes_result,
    }


def load_caused_by(session):
    """CAUSED_BY is derived entirely from data already in the graph (TOUCHED, from
    phase 6, plus the PART_OF/FIXES edges just loaded) -- no new GitHub or git call
    needed, just a confidence score layered on top."""
    rows = session.run(
        """
        MATCH (i:Issue)<-[:FIXES]-(p:PR)<-[:PART_OF]-(cm:Commit)-[t:TOUCHED]->(m:Method)
        RETURN i.number AS issue, p.files_changed AS files_changed,
               cm.sha AS sha, m.fqn AS fqn, t.added AS added, t.removed AS removed
        """
    ).data()
    if not rows:
        return 0

    # Per-commit totals, deduplicated on (sha, fqn) so a commit linked to several
    # issues at once (multiple closing references on one PR) isn't double-counted
    # when computing its own focus/line-share denominators.
    per_sha_methods = {}
    for r in rows:
        per_sha_methods.setdefault(r["sha"], {})[r["fqn"]] = (r["added"] or 0) + (r["removed"] or 0)

    sha_totals = {sha: sum(m.values()) for sha, m in per_sha_methods.items()}
    sha_counts = {sha: len(m) for sha, m in per_sha_methods.items()}

    out_rows = []
    seen = set()
    for r in rows:
        key = (r["issue"], r["fqn"])
        if key in seen:
            continue
        seen.add(key)
        sha = r["sha"]
        n_methods = sha_counts[sha]
        total_lines = sha_totals[sha]
        lines = per_sha_methods[sha][r["fqn"]]
        confidence = attribution(n_methods, lines, total_lines, r["files_changed"])
        out_rows.append({"issue": r["issue"], "fqn": r["fqn"], "confidence": confidence})

    session.run(
        """
        UNWIND $rows AS row
        MATCH (i:Issue {number: row.issue})
        MATCH (m:Method {fqn: row.fqn})
        MERGE (i)-[c:CAUSED_BY]->(m)
        SET c.confidence = row.confidence
        """,
        rows=out_rows,
    )
    return len(out_rows)


# --- entry point ---------------------------------------------------------


def fetch_section(cache_dir, name, fetch_fn, since_iso, full):
    """Loads one of issues/prs, either fresh (first run or --full) or merged
    on top of the existing cache (incremental, phase 9): fetch only items
    updated since the last run, keep everything else from the cache as-is."""
    path = os.path.join(cache_dir, f"{name}.json")
    cached = load_json(path)
    if full or cached is None or since_iso is None:
        items = list(fetch_fn(stop_before_updated_at=None))
        print(f"  {name}: {len(items)} fetched (full)")
    else:
        updates = list(fetch_fn(stop_before_updated_at=since_iso))
        items = merge_by_number(cached, updates)
        print(f"  {name}: {len(updates)} updated since {since_iso}, {len(items)} total")
    save_json(path, items)
    return items


def ingest(repo_slug, cache_dir, wipe, state_path, full):
    owner, name = repo_slug.split("/", 1)
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    stats = {}
    with driver.session() as s:
        if wipe:
            print("wiping GitHub layer (Issue, PR)...")
            s.run("MATCH (n) WHERE n:Issue OR n:PR DETACH DELETE n")

        print("constraints...")
        for c in CONSTRAINTS:
            s.run(c)

        state = None if (wipe or full) else load_json(state_path)
        since_iso = state["last_ingested_at"] if state else None

        print("fetching issues...")
        t0 = time.time()
        issues = fetch_section(cache_dir, "issues",
                                lambda stop_before_updated_at: fetch_issues(owner, name, stop_before_updated_at),
                                since_iso, full)
        print(f"  done in {time.time() - t0:.1f}s")
        stats["issues"] = load_issues(s, issues)

        print("fetching merged PRs...")
        t0 = time.time()
        prs = fetch_section(cache_dir, "prs",
                             lambda stop_before_updated_at: fetch_prs(owner, name, stop_before_updated_at),
                             since_iso, full)
        print(f"  done in {time.time() - t0:.1f}s")
        stats.update(load_prs_and_fixes(s, prs))

        print("CAUSED_BY (derived from TOUCHED + PART_OF + FIXES already in graph)...")
        stats["caused_by_edges"] = load_caused_by(s)

        newest = max([i["updatedAt"] for i in issues] + [p["updatedAt"] for p in prs], default=None)
        if newest:
            save_json(state_path, {"last_ingested_at": newest})

    driver.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="local clone; the GitHub repo is read from its 'origin' remote")
    src.add_argument("--repo-slug", help="owner/name on GitHub")
    ap.add_argument("--cache", default=None,
                     help="raw issue/PR cache dir (default: out/github/<owner>__<name>)")
    ap.add_argument("--state", default=None,
                     help="where the last-ingested updatedAt is remembered "
                          "(default: <cache>/state.json)")
    ap.add_argument("--full", action="store_true",
                     help="ignore saved state and the incremental path; fetch everything")
    ap.add_argument("--wipe", action="store_true")
    args = ap.parse_args()

    slug = args.repo_slug or slug_from_remote(args.repo)
    cache = args.cache or os.path.join("out", "github", slug.replace("/", "__"))
    state = args.state or os.path.join(cache, "state.json")
    print(f"GitHub repository: {slug}")

    stats = ingest(slug, cache, args.wipe, state, args.full)

    print("\n=== GitHub layer loaded ===")
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k.ljust(width)}  {v}")

    unresolved = stats.get("part_of_attempted", 0) - stats.get("part_of_linked", 0)
    if unresolved:
        print(f"\n  {unresolved} merged PR(s) had a merge commit GitHub knows about "
              f"but this local clone's history does not (see module docstring).")


if __name__ == "__main__":
    sys.exit(main())
