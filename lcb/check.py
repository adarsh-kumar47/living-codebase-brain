"""Change check: warn when a diff touches methods with a bug history.

    git diff -U0  ->  changed line ranges  ->  methods whose [line, line_end] span
    they overlap  ->  past fixes, callers, last author, risk  ->  warnings

Past fixes come from two places: GitHub issues linked through a PR (CAUSED_BY), and
commits whose own message says they fix something (FIXED, lcb/fixes.py) -- the
second needs no issue tracker. A fix commit that is already behind a listed issue
is not listed again.

No LLM is involved -- one diff, two Cypher queries. Meant to run from a git
pre-commit hook, an editor, an agent hook or CI; `--json` gives the same result
machine-readably.

**Old-side line numbers.** The graph's line spans describe the last compiled build,
i.e. (close to) HEAD. A diff's `-a,b` side is numbered against that same old version
of the file, so hunks are matched on it: an edit that shifts the rest of the file
down still lands on the method it was made in, not on whichever method now sits at
those line numbers in the working copy.

Each method also carries `line_now`/`line_end_now`: the same span on the diff's new
side (the working tree, by default), which is where an editor should mark it.

**Span heuristic.** A bytecode line table starts at a method's first *statement*,
not its signature, so the signature is found in the old-side source: scanning up from
the first statement for the line naming the method (`processFindForm(`), past comments
and continuation lines, stopping at the previous member's `}` or `;`. If that finds
nothing (a lambda body, an unusual layout), the line above the first statement is
used. Editing the parameter list therefore counts as touching the method. A pure insertion (`-a,0`)
counts only when it lands strictly inside that span, so adding a new method right
after an existing one does not flag its neighbour. New methods have no node and no
history, so they are never flagged -- correctly.

Exit status: 0 = checked (nothing flagged, or not `--strict`); 1 = `--strict` and
something was flagged; 2 = could not check (not a git repo, Neo4j unreachable). A hook
blocks only on 1 and lets the commit through on 2 -- a stopped database must never stop
anyone committing.

Usage:
    python -m lcb.check --repo path/to/repo                 # working tree vs HEAD
    python -m lcb.check --repo path/to/repo --staged        # what `git commit` would record
    python -m lcb.check --repo path/to/repo --base main     # everything since main (a PR)
    python -m lcb.check --repo path/to/repo --json
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable, AuthError

from lcb import risk
from lcb.git_ingest import SourceIndex, default_state_path, git, load_state

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# --- diff -> changed old-side line ranges -------------------------------------


def diff_args(staged, base):
    if staged:
        return ["diff", "--cached"]
    if base:
        return ["diff", f"{base}...HEAD"]
    return ["diff", "HEAD"]


def parse_diff(text):
    """{old path: [(old_start, old_count, new_start, new_count), ...]} from a -U0
    diff. old_count == 0 is a pure insertion *after* line old_start (and new_count
    == 0 a deletion after new_start). Files new in the diff have no old side and no
    methods in the graph, so they are left out."""
    hunks = {}
    path = None
    for line in text.splitlines():
        if line.startswith("--- "):
            old = line[4:].strip()
            path = old[2:] if old.startswith("a/") else None  # "/dev/null" for new files
        elif line.startswith("+++ "):
            continue
        elif path and line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:
                hunks.setdefault(path, []).append((int(m.group(1)), int(m.group(2) or 1),
                                                   int(m.group(3)), int(m.group(4) or 1)))
    return hunks


def changed_ranges(repo, staged=False, base=None):
    # Fails with git's own one-line "not a git repository" -- otherwise `git diff`
    # outside a repo falls back to --no-index mode and prints its whole usage text.
    git(repo, "rev-parse", "--git-dir")
    out = git(repo, *diff_args(staged, base), "-U0", "--no-color", "--no-ext-diff",
              "--no-renames", "--", *(f"*{ext}" for ext in (".java", ".kt", ".groovy", ".scala")))
    return parse_diff(out)


def line_now(old, hunks):
    """Where old-side line `old` sits on the diff's new side (the working tree by
    default), so an editor can mark the method where it is now. A line inside a
    changed hunk maps to that hunk's start; lines after it shift by the hunk's net
    size. -U0 numbers an empty side by the line *before* it, hence the +1s."""
    now = old
    for os_, oc, ns, nc in hunks:
        os_eff = os_ + 1 if oc == 0 else os_
        ns_eff = ns + 1 if nc == 0 else ns
        if old < os_eff:
            break
        if old < os_eff + oc:
            return max(ns_eff, 1)
        now = old + (ns_eff + nc) - (os_eff + oc)
    return now


def signature_line(source_lines, name, owner, body_line):
    """1-based line of the method's signature in `source_lines` (the old-side file),
    found by scanning up from its first statement -- see the module docstring."""
    if name == "<init>":
        name = owner.rsplit(".", 1)[-1].rsplit("$", 1)[-1]
    fallback = max(body_line - 1, 1)
    if not source_lines or name.startswith(("lambda$", "<")):
        return fallback
    pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
    for n in range(min(body_line, len(source_lines)), max(body_line - 30, 0), -1):
        text = source_lines[n - 1]
        if pattern.search(text):
            return n
        if n < body_line and text.strip().endswith(("}", ";")):
            break
    return fallback


def old_side_rev(repo, staged, base):
    """The revision the diff's old side is: HEAD, or for --base the merge base."""
    return git(repo, "merge-base", base, "HEAD").strip() if base else "HEAD"


def old_source(repo, rev, path):
    try:
        return git(repo, "show", f"{rev}:{path}").splitlines()
    except subprocess.CalledProcessError:
        return []


def touches(start, count, sig_line, line_end):
    lo = sig_line
    if count == 0:
        return lo <= start and start + 1 <= line_end
    return start <= line_end and start + count - 1 >= lo


# --- graph lookups -------------------------------------------------------------


def app_methods_by_path(session, repo, paths):
    """Application methods with a line span whose source file is one of `paths`."""
    sources = SourceIndex.from_repo(repo)
    rows = session.run(
        """
        MATCH (c:Class {is_external: false})-[:HAS_METHOD]->(m:Method)
        WHERE m.line IS NOT NULL AND m.line_end IS NOT NULL
        RETURN c.package AS package, c.source_file AS source_file,
               m.fqn AS fqn, m.owner AS owner, m.name AS name,
               m.line AS line, m.line_end AS line_end
        """
    ).data()
    by_path = {}
    resolved = {}
    for r in rows:
        key = (r["package"], r["source_file"])
        if key not in resolved:
            resolved[key] = sources.resolve(*key)
        if resolved[key] in paths:
            by_path.setdefault(resolved[key], []).append(r)
    return by_path


def history(session, fqns):
    """fqn -> {issues, fix_commits, callers, last_author} for the given methods."""
    out = {fqn: {"issues": [], "fix_commits": [], "callers": 0, "last_author": None} for fqn in fqns}
    for r in session.run(
        """
        UNWIND $fqns AS fqn
        MATCH (i:Issue)-[cb:CAUSED_BY]->(m:Method {fqn: fqn})
        OPTIONAL MATCH (i)<-[:FIXES]-(pr:PR)
        WITH fqn, i, cb, collect(DISTINCT pr.number) AS prs
        RETURN fqn, i.number AS number, i.title AS title, i.state AS state,
               i.closed_at AS closed_at, cb.confidence AS confidence, prs AS fixed_by
        ORDER BY confidence DESC
        """, fqns=fqns,
    ).data():
        out[r.pop("fqn")]["issues"].append(r)
    for r in session.run(
        """
        UNWIND $fqns AS fqn
        MATCH (cm:Commit)-[fx:FIXED]->(m:Method {fqn: fqn})
        WHERE NOT EXISTS { (cm)-[:PART_OF]->(:PR)-[:FIXES]->(:Issue)-[:CAUSED_BY]->(m) }
        OPTIONAL MATCH (a:Author)-[:AUTHORED]->(cm)
        OPTIONAL MATCH (ref:Issue) WHERE ref.number IN coalesce(cm.issue_refs, [])
        WITH fqn, cm, fx, a, collect(ref {.number, .title}) AS refs
        RETURN fqn, cm.sha AS sha, cm.message AS message, cm.timestamp AS timestamp,
               cm.fix_kind AS kind, fx.confidence AS confidence, a.name AS author, refs
        ORDER BY confidence DESC, timestamp DESC
        """, fqns=fqns,
    ).data():
        out[r.pop("fqn")]["fix_commits"].append(r)
    for r in session.run(
        """
        UNWIND $fqns AS fqn
        MATCH (m:Method {fqn: fqn})
        OPTIONAL MATCH (caller:Method)-[:CALLS]->(m)
        WITH fqn, m, count(DISTINCT caller) AS callers
        OPTIONAL MATCH (a:Author)-[:LAST_MODIFIED]->(m)
        RETURN fqn, callers, a.name AS last_author
        """, fqns=fqns,
    ).data():
        out[r["fqn"]].update(callers=r["callers"], last_author=r["last_author"])
    return out


def risk_ranks(session):
    """fqn -> (risk, rank, total) over all application methods. The score is
    relative (min-max normalized, unfitted weights -- see lcb/risk.py), so the rank
    is the meaningful part."""
    scored = risk.compute(session)
    return {r["fqn"]: (r["risk"], i + 1, len(scored)) for i, r in enumerate(scored)}


def stale_note(repo):
    """Says so when the history layer was ingested at a different commit than HEAD."""
    state = load_state(default_state_path(repo))
    if not state:
        return "history layer not ingested for this repo (run lcb.git_ingest); bug links may be missing"
    head = git(repo, "rev-parse", "HEAD").strip()
    if state.get("last_sha") != head:
        return (f"history layer was ingested at {state['last_sha'][:8]}, HEAD is {head[:8]}; "
                f"run lcb.git_ingest / lcb.github_ingest to refresh")
    return None


# --- the check ---------------------------------------------------------------------


def check(repo, staged=False, base=None, min_confidence=0.2):
    ranges = changed_ranges(repo, staged, base)
    result = {"files_changed": sorted(ranges), "methods": [], "note": stale_note(repo),
              "min_confidence": min_confidence}
    if not ranges:
        return result

    # Server notifications off: these queries are fixed, and on a graph without some
    # layer (no GitHub ingest -> no Issue label) the driver would otherwise print
    # "unknown label/property" warnings into every commit.
    with GraphDatabase.driver(URI, auth=(USER, PASSWORD), notifications_min_severity="OFF") as d, \
            d.session() as s:
        by_path = app_methods_by_path(s, repo, set(ranges))
        rev = old_side_rev(repo, staged, base)
        touched = []
        for path, hunks in ranges.items():
            source = old_source(repo, rev, path) if by_path.get(path) else []
            for m in by_path.get(path, []):
                sig = signature_line(source, m["name"], m["owner"], m["line"])
                if any(touches(os_, oc, sig, m["line_end"]) for os_, oc, _, _ in hunks):
                    touched.append({**m, "path": path, "sig": sig,
                                    "line_now": line_now(sig, hunks),
                                    "line_end_now": line_now(m["line_end"], hunks)})
        if not touched:
            return result
        hist = history(s, [m["fqn"] for m in touched])
        ranks = risk_ranks(s)

    for m in touched:
        h = hist[m["fqn"]]
        issues, fix_commits = h["issues"], h["fix_commits"]
        r = ranks.get(m["fqn"])
        result["methods"].append({
            "fqn": m["fqn"],
            "method": f"{m['owner'].rsplit('.', 1)[-1]}.{m['name']}",
            "path": m["path"],
            "line": m["sig"],
            "line_end": m["line_end"],
            "line_now": m["line_now"],
            "line_end_now": m["line_end_now"],
            "issues": issues,
            "fix_commits": fix_commits,
            "flagged": any((x["confidence"] or 0) >= min_confidence for x in issues + fix_commits),
            "callers": h["callers"],
            "last_author": h["last_author"],
            "risk": r[0] if r else None,
            "risk_rank": r[1] if r else None,
            "risk_of": r[2] if r else None,
        })
    result["methods"].sort(key=lambda m: (not m["flagged"], m["risk_rank"] or 1e9))
    return result


def render(result, show_all):
    lines = []
    flagged = [m for m in result["methods"] if m["flagged"]]
    shown = result["methods"] if show_all else flagged
    for m in shown:
        mark = "⚠" if m["flagged"] else "·"
        lines.append(f"{mark} {m['method']}  ({m['path']}:{m['line']})")
        strong = lambda x: show_all or (x["confidence"] or 0) >= result["min_confidence"]  # noqa: E731
        weak = sum(not strong(x) for x in m["issues"] + m["fix_commits"])
        for i in filter(strong, m["issues"]):
            fixed = ", ".join(f"PR #{n}" for n in i["fixed_by"]) or "no PR"
            lines.append(f"    linked to issue #{i['number']} ({i['confidence']:.0%} confidence, "
                         f"fixed by {fixed}): {i['title']}")
        for c in filter(strong, m["fix_commits"]):
            when = datetime.datetime.fromtimestamp(c["timestamp"], datetime.timezone.utc).strftime("%Y-%m-%d")
            refs = "".join(f" [issue #{x['number']}: {x['title']}]" for x in c["refs"])
            lines.append(f"    fixed before in {c['sha'][:7]} by {c['author']}, {when} "
                         f"({c['confidence']:.0%} confidence): \"{c['message']}\"{refs}")
        if weak:
            lines.append(f"    +{weak} weaker link{'s' if weak != 1 else ''} (--all to show)")
        facts = [f"{m['callers']} caller{'s' if m['callers'] != 1 else ''}"]
        if m["last_author"]:
            facts.append(f"last changed by {m['last_author']}")
        if m["risk_rank"]:
            facts.append(f"risk rank {m['risk_rank']} of {m['risk_of']}")
        lines.append("    " + " · ".join(facts))
    n = len(result["methods"])
    summary = (f"{len(flagged)} of {n} changed method{'s' if n != 1 else ''} "
               f"with past bug fixes" if n else "no indexed methods changed")
    lines.append(summary)
    if result["note"]:
        lines.append(f"note: {result['note']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Warn when a change touches methods with a bug history.")
    ap.add_argument("--repo", default=".", help="git repository to check (default: current directory)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check staged changes only (pre-commit)")
    mode.add_argument("--base", help="check everything since this ref, e.g. main (pull request)")
    ap.add_argument("--min-confidence", type=float, default=0.2,
                    help="CAUSED_BY confidence needed to flag a method (default 0.2)")
    ap.add_argument("--all", action="store_true", help="also list changed methods with no bug history")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--strict", action="store_true", help="exit 1 when anything is flagged")
    ap.add_argument("--quiet", action="store_true", help="print nothing unless something is flagged (hooks)")
    args = ap.parse_args()

    try:
        result = check(args.repo, args.staged, args.base, args.min_confidence)
    except subprocess.CalledProcessError as e:
        print(f"lcb check: git failed: {(e.stderr or '').strip()}", file=sys.stderr)
        return 2
    except (ServiceUnavailable, AuthError, Neo4jError, OSError) as e:
        print(f"lcb check: cannot reach the code graph at {URI} ({type(e).__name__})", file=sys.stderr)
        return 2

    flagged = any(m["flagged"] for m in result["methods"])
    if args.json:
        print(json.dumps(result, indent=2))
    elif flagged or not args.quiet:
        print(render(result, args.all))
    return 1 if args.strict and flagged else 0


if __name__ == "__main__":
    sys.exit(main())
