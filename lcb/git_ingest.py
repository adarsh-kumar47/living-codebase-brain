"""Git history ingestion: commits, authors, and per-method touch/last-modified edges.

Adds the socio-technical layer specified in README Sec.10, on top of the existing
structural graph. New node labels only -- Class and Method are untouched, history
attaches to them.

    (:Author {email, name})
    (:Commit {sha, message, timestamp, additions, deletions})
    (:Author)-[:AUTHORED]->(:Commit)
    (:Commit)-[:TOUCHED {added, removed}]->(:Method)
    (:Author)-[:LAST_MODIFIED]->(:Method)
    (:Commit)-[:FIXED {confidence}]->(:Method)

FIXED marks bug fixes found from commit messages alone (lcb/fixes.py) -- no issue
tracker needed. Each commit is classified as it is ingested (`fix_kind`, plus the
`issue_refs` its subject names); FIXED is then derived from those commits' TOUCHED
edges with the same attribution score the GitHub layer uses for CAUSED_BY.

Commits and authors are repo-wide (from `git log --numstat`) -- cheap, and every
commit is a potential future PART_OF/FIXES anchor once the PR/issue layer (phase 7)
lands. TOUCHED and LAST_MODIFIED are scoped to application methods only
(is_external:false, line/line_end present): dependency JARs have no git history in
this repo, so there is nothing honest to attach to them.

TOUCHED is built with `git log -L<line>,<line_end>:<path>` per method rather than by
intersecting raw diff hunks against the method's *current* line span. Git's -L mode
tracks a line range backwards through history itself, including renumbering as
earlier commits shift lines above it -- so this is exact for every commit, not an
approximation that degrades for older history (contrast the file-level fallback
noted for phase 5/Sec.10.2). LAST_MODIFIED comes from `git blame` on the current
file, which is exact by construction since it is run against the same source the
extractor's line numbers describe.

Works on any git repository whose compiled classes were loaded by lcb.load: source
files are located through `git ls-files` (see SourceIndex), not by assuming a
`src/main/java` layout, so multi-module Maven/Gradle builds and Kotlin/Groovy/Scala
sources resolve too. Incremental state is kept per repository (see default_state_path).

Usage:
    python -m lcb.git_ingest --repo path/to/repo [--wipe] [--full]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict

from neo4j import GraphDatabase

from lcb.fixes import attribution, classify, issue_refs
from lcb.generated import find_generated

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")

BATCH = 2000

# Where per-repo state lives. Anchored to the project, not the working directory, so
# a process started elsewhere -- an MCP server launched by an editor -- finds it too.
LCB_HOME = os.environ.get("LCB_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONSTRAINTS = [
    "CREATE CONSTRAINT author_email IF NOT EXISTS FOR (a:Author) REQUIRE a.email IS UNIQUE",
    "CREATE CONSTRAINT commit_sha IF NOT EXISTS FOR (c:Commit) REQUIRE c.sha IS UNIQUE",
]

# \x01 marks the start of each commit record, \x1f separates its fields -- both
# control characters, so they cannot collide with anything in a commit subject.
# %aE/%aN rather than %ae/%an: honours the repo's .mailmap, so one person committing
# under several addresses collapses to one Author where the project has mapped them.
LOG_FORMAT = "%x01%H%x1f%aE%x1f%aN%x1f%at%x1f%s"

# Source extensions a JVM build can record in a class file's SourceFile attribute.
SOURCE_EXTS = (".java", ".kt", ".groovy", ".scala")

COMMIT_LINE_RE = re.compile(r"^commit ([0-9a-f]{40})")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=True,
    ).stdout


def chunks(rows, n=BATCH):
    buf = []
    for r in rows:
        buf.append(r)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


# --- commits + authors ------------------------------------------------------


def parse_commits(repo, since_sha=None):
    """Every commit (or, with since_sha, only commits new since that point),
    its author, and its total added/deleted line count summed across every
    file it touched."""
    rev_range = f"{since_sha}..HEAD" if since_sha else "HEAD"
    out = git(repo, "log", f"--format={LOG_FORMAT}", "--numstat", rev_range)
    rows = []
    for chunk in out.split("\x01"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n\n")
        sha, email, name, ts, subject = head.split("\x1f", 4)
        added = deleted = files = 0
        for line in rest.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            files += 1
            a, d, _path = parts
            # Binary files report "-\t-\tpath" -- not countable, not a defect.
            if a.isdigit():
                added += int(a)
            if d.isdigit():
                deleted += int(d)
        rows.append({
            "sha": sha,
            "author_email": email,
            "author_name": name,
            "timestamp": int(ts),
            "message": subject,
            "additions": added,
            "deletions": deleted,
            "files_changed": files,
            "fix_kind": classify(subject),
            "issue_refs": issue_refs(subject),
        })
    return rows


def load_commits(session, rows):
    t0 = time.time()
    total = 0
    for chunk in chunks(rows):
        session.run(
            """
            UNWIND $rows AS row
            MERGE (a:Author {email: row.author_email})
            SET a.name = row.author_name
            MERGE (c:Commit {sha: row.sha})
            SET c.message = row.message,
                c.timestamp = row.timestamp,
                c.additions = row.additions,
                c.deletions = row.deletions,
                c.files_changed = row.files_changed,
                c.fix_kind = row.fix_kind,
                c.issue_refs = row.issue_refs
            MERGE (a)-[:AUTHORED]->(c)
            """,
            rows=chunk,
        )
        total += len(chunk)
        print(f"  commits: {total:,}", end="\r", flush=True)
    print(f"  commits: {total:,} in {time.time() - t0:.1f}s".ljust(60))
    return total


# --- generated code (lcb/generated.py) ----------------------------------------


def mark_generated(session, repo, sources):
    """Sets is_generated on every application Class and its Methods, and removes any
    history attached to generated ones (from runs before they were recognised).
    Returns the set of generated source paths."""
    classes = session.run(
        "MATCH (c:Class {is_external: false}) "
        "RETURN c.fqn AS fqn, c.package AS package, c.source_file AS source_file"
    ).data()
    files = {}
    for c in classes:
        c["path"] = sources.resolve(c["package"], c["source_file"])
        if c["path"]:
            files[c["path"]] = c["package"]
    generated = find_generated(repo, files)
    rows = [{"fqn": c["fqn"], "g": c["path"] in generated} for c in classes]
    for chunk in chunks(rows):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (c:Class {fqn: row.fqn})
            SET c.is_generated = row.g
            WITH c, row
            OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:Method)
            SET m.is_generated = row.g
            """,
            rows=chunk,
        )
    session.run("MATCH (:Method {is_generated: true})<-[r:TOUCHED|LAST_MODIFIED|FIXED]-() DELETE r")
    if generated:
        reasons = {}
        for why in generated.values():
            key = why.split(" is written")[0] if why.startswith("package ") else why
            reasons[key] = reasons.get(key, 0) + 1
        print(f"  {len(generated):,} generated source file(s): no history attached "
              f"({'; '.join(f'{n} {k}' for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:4])})")
    return set(generated)


# --- per-method TOUCHED (git log -L) ----------------------------------------


def app_methods(session):
    """Every application method with a known source line span -- the only methods
    a git repo can say anything about."""
    result = session.run(
        """
        MATCH (c:Class {is_external: false})-[:HAS_METHOD]->(m:Method)
        WHERE m.line IS NOT NULL AND m.line_end IS NOT NULL
        RETURN c.package AS package, c.source_file AS source_file,
               m.fqn AS fqn, m.line AS line, m.line_end AS line_end
        """
    )
    return [r.data() for r in result]


class SourceIndex:
    """Maps a class's (package, source_file) to its path in the repository.

    Bytecode records only the bare file name (`Owner.java`), so the path is found
    among the files git actually tracks rather than assumed: a tracked file ending in
    `<package dir>/<file>` wins wherever it sits -- `src/main/java/...`,
    `service-a/src/main/java/...`, `src/main/kotlin/...`. Test sources lose a tie
    against main sources (only main classes are indexed). Kotlin/Groovy/Scala files
    need not live under their package directory, so for those a file name unique in
    the repo is accepted too; for .java that fallback is not taken, since a lone
    same-named file in another package is likelier a different class than a match.
    Anything still ambiguous or missing resolves to None and is reported."""

    def __init__(self, paths):
        self.by_name = defaultdict(list)
        for p in paths:
            self.by_name[p.rsplit("/", 1)[-1]].append(p)

    @classmethod
    def from_repo(cls, repo):
        out = git(repo, "ls-files", "-z", "--", *(f"*{ext}" for ext in SOURCE_EXTS))
        return cls(p for p in out.split("\0") if p)

    def resolve(self, package, source_file):
        if not source_file:
            return None
        named = self.by_name.get(source_file, [])
        pkg_dir = package.replace(".", "/") if package else ""
        suffix = f"{pkg_dir}/{source_file}" if pkg_dir else source_file
        cands = [p for p in named if p == suffix or p.endswith("/" + suffix)]
        if not cands and len(named) == 1 and not source_file.endswith(".java"):
            cands = named
        if len(cands) > 1:
            cands = [p for p in cands if "/src/test/" not in "/" + p] or cands
        return cands[0] if len(cands) == 1 else None


def changed_paths_since(repo, since_sha):
    """Files touched by any commit since since_sha -- the set of files whose
    methods might have new TOUCHED/LAST_MODIFIED edges to pick up. A file git log
    -L already covered correctly for older commits doesn't need re-running if
    nothing new happened to it."""
    out = git(repo, "diff", "--name-only", since_sha, "HEAD")
    return set(out.splitlines())


def touching_commits(repo, path, line, line_end):
    """Shas of every commit that touched this exact line range, anywhere in
    history. `git log -L` re-targets the range through renumbering itself, so
    this stays exact even for a method that has moved within its file many
    times over -- unlike re-deriving it from each commit's raw diff hunks."""
    try:
        out = git(repo, "log", f"-L{line},{line_end}:{path}")
    except subprocess.CalledProcessError:
        # Method's current file/range has no history under this path -- e.g. a
        # generated or since-renamed file the extractor saw but git log cannot
        # resolve. Reported, not hidden.
        return []

    shas, added, removed = [], 0, 0
    per_commit = []
    cur_sha = None
    for ln in out.splitlines():
        m = COMMIT_LINE_RE.match(ln)
        if m:
            if cur_sha is not None:
                per_commit.append((cur_sha, added, removed))
            cur_sha, added, removed = m.group(1), 0, 0
            continue
        if cur_sha is None:
            continue
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            added += 1
        elif ln.startswith("-"):
            removed += 1
    if cur_sha is not None:
        per_commit.append((cur_sha, added, removed))
    return per_commit


def load_touched(session, repo, methods):
    t0 = time.time()
    rows = []
    skipped = []
    for m in methods:
        path = m["path"]
        if path is None:
            skipped.append(f'{m["package"]}.{m["source_file"]}')
            continue
        for sha, added, removed in touching_commits(repo, path, m["line"], m["line_end"]):
            rows.append({"fqn": m["fqn"], "sha": sha, "added": added, "removed": removed})

    for chunk in chunks(rows):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (cm:Commit {sha: row.sha})
            MATCH (m:Method {fqn: row.fqn})
            MERGE (cm)-[t:TOUCHED]->(m)
            SET t.added = row.added, t.removed = row.removed
            """,
            rows=chunk,
        )
    print(f"  TOUCHED: {len(rows):,} edges across {len(methods) - len(skipped):,} methods"
          f" in {time.time() - t0:.1f}s")
    if skipped:
        print(f"  skipped {len(skipped)} methods -- source file not tracked by git, or"
              f" ambiguous (first: {skipped[0]})")
    return len(rows), skipped


# --- per-method LAST_MODIFIED (git blame) -----------------------------------


def blame_last_author(repo, path, line, line_end):
    """Author of whichever line in [line, line_end] was most recently changed,
    per `git blame` at HEAD -- the same revision `git log -L` walks back from, so
    uncommitted edits in the checkout are never attributed to "Not Committed Yet"."""
    try:
        out = git(repo, "blame", "--line-porcelain", f"-L{line},{line_end}", "HEAD", "--", path)
    except subprocess.CalledProcessError:
        return None

    best_time, best_email = -1, None
    email = None
    for ln in out.splitlines():
        if ln.startswith("author-mail "):
            email = ln[len("author-mail "):].strip("<>")
        elif ln.startswith("author-time ") and email is not None:
            t = int(ln.split(" ", 1)[1])
            if t > best_time:
                best_time, best_email = t, email
    return best_email


def load_last_modified(session, repo, methods):
    t0 = time.time()
    rows = []
    for m in methods:
        if m["path"] is None:
            continue
        email = blame_last_author(repo, m["path"], m["line"], m["line_end"])
        if email:
            rows.append({"fqn": m["fqn"], "email": email})

    # Cleared first (scoped to just these methods) so one whose last author
    # changed since a previous run doesn't keep an edge to the old one -- there
    # is exactly one "most recent" author per method, not a set that only grows.
    # Scoping matters for incremental runs: an untouched method's existing edge
    # must survive even though this call only rebuilds the affected subset.
    session.run(
        """
        UNWIND $fqns AS fqn
        MATCH (:Class {is_external:false})-[:HAS_METHOD]->(m:Method {fqn: fqn})<-[r:LAST_MODIFIED]-()
        DELETE r
        """,
        fqns=[m["fqn"] for m in methods],
    )
    for chunk in chunks(rows):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (a:Author {email: row.email})
            MATCH (m:Method {fqn: row.fqn})
            MERGE (a)-[:LAST_MODIFIED]->(m)
            """,
            rows=chunk,
        )
    print(f"  LAST_MODIFIED: {len(rows):,} edges in {time.time() - t0:.1f}s")
    return len(rows)


# --- FIXED (fix commits from messages, lcb/fixes.py) ------------------------


def reclassify_commits(session):
    """Re-applies the current fix classifier to every commit already in the graph.

    Commits are classified when first ingested, so an improved classifier (a new
    language, a new exclusion) would otherwise only reach old commits through a full
    rescan. Re-classifying from the stored subject is one query and some regexes."""
    rows = session.run(
        "MATCH (c:Commit) RETURN c.sha AS sha, c.message AS message, "
        "c.fix_kind AS kind, c.issue_refs AS refs"
    ).data()
    changed = []
    for r in rows:
        kind, refs = classify(r["message"]), issue_refs(r["message"])
        if kind != r["kind"] or refs != (r["refs"] or []):
            changed.append({"sha": r["sha"], "kind": kind, "refs": refs})
    for chunk in chunks(changed):
        session.run(
            "UNWIND $rows AS row MATCH (c:Commit {sha: row.sha}) "
            "SET c.fix_kind = row.kind, c.issue_refs = row.refs",
            rows=chunk,
        )
    if changed:
        print(f"  reclassified {len(changed):,} existing commit(s) with the current fix rules")
    return len(changed)


def load_fixed(session):
    """Rebuilt from scratch on every run -- it is derived from Commit.fix_kind and
    TOUCHED already in the graph, so it costs one query and cannot drift."""
    session.run("MATCH (:Commit)-[f:FIXED]->() DELETE f")
    rows = session.run(
        """
        MATCH (cm:Commit)-[t:TOUCHED]->(m:Method)
        WHERE cm.fix_kind IS NOT NULL
        RETURN cm.sha AS sha, cm.files_changed AS files_changed, m.fqn AS fqn,
               coalesce(t.added, 0) + coalesce(t.removed, 0) AS lines
        """
    ).data()
    per_sha = {}
    for r in rows:
        per_sha.setdefault(r["sha"], []).append(r)
    out = []
    for sha, methods in per_sha.items():
        total = sum(r["lines"] for r in methods)
        for r in methods:
            out.append({"sha": sha, "fqn": r["fqn"],
                        "confidence": attribution(len(methods), r["lines"], total, r["files_changed"])})
    for chunk in chunks(out):
        session.run(
            """
            UNWIND $rows AS row
            MATCH (cm:Commit {sha: row.sha})
            MATCH (m:Method {fqn: row.fqn})
            CREATE (cm)-[:FIXED {confidence: row.confidence}]->(m)
            """,
            rows=chunk,
        )
    print(f"  FIXED: {len(out):,} edges from {len(per_sha):,} fix commits")
    return len(out), len(per_sha)


# --- state (phase 9: incremental ingestion) ---------------------------------


def repo_key(repo):
    """`<name>-<hash of absolute path>`: a stable, per-repository folder name."""
    root = os.path.realpath(repo)
    return f"{os.path.basename(root)}-{hashlib.sha1(root.encode('utf-8')).hexdigest()[:8]}"


def default_state_path(repo):
    """One state file per repository, keyed by its absolute path, so ingesting a
    second repo never resumes from the first one's last commit."""
    return os.path.join(LCB_HOME, "out", "state", repo_key(repo), "git_state.json")


def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def usable_since_sha(repo, state):
    """The saved sha, if it can seed an incremental run of *this* repo: recorded
    for the same repository, and still an ancestor of HEAD (not rewritten away)."""
    if not state or not state.get("last_sha"):
        return None
    if state.get("repo") and state["repo"] != os.path.realpath(repo):
        print(f"  saved state belongs to {state['repo']}; doing a full scan")
        return None
    try:
        git(repo, "merge-base", "--is-ancestor", state["last_sha"], "HEAD")
    except subprocess.CalledProcessError:
        print(f"  saved sha {state['last_sha'][:8]} is not in this repo's history; doing a full scan")
        return None
    return state["last_sha"]


def save_state(path, state):
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


# --- entry point -------------------------------------------------------------


def ingest(repo, wipe, state_path, full):
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    stats = {}
    with driver.session() as s:
        if wipe:
            print("wiping history layer (Author, Commit)...")
            s.run("MATCH (n) WHERE n:Author OR n:Commit DETACH DELETE n")

        print("constraints...")
        for c in CONSTRAINTS:
            s.run(c)

        state = None if (wipe or full) else load_state(state_path)
        since_sha = usable_since_sha(repo, state)

        print("parsing git log..." if not since_sha else f"parsing git log since {since_sha[:8]}...")
        commit_rows = parse_commits(repo, since_sha=since_sha)
        stats["new_commits"] = load_commits(s, commit_rows)
        stats["authors"] = s.run("MATCH (a:Author) RETURN count(a) AS c").single()["c"]

        sources = SourceIndex.from_repo(repo)
        print("generated code...")
        generated = mark_generated(s, repo, sources)
        all_methods = app_methods(s)
        for m in all_methods:
            m["path"] = sources.resolve(m["package"], m["source_file"])
        stats["generated_methods"] = sum(1 for m in all_methods if m["path"] in generated)
        all_methods = [m for m in all_methods if m["path"] not in generated]
        if since_sha and not commit_rows:
            # No new commits since last run -- nothing could have changed.
            methods = []
            print("no new commits since last ingest; TOUCHED/LAST_MODIFIED untouched")
        elif since_sha:
            changed = changed_paths_since(repo, since_sha)
            methods = [m for m in all_methods if m["path"] in changed]
            print(f"incremental: {len(changed)} file(s) changed since {since_sha[:8]}, "
                  f"affecting {len(methods)}/{len(all_methods)} application methods")
        else:
            methods = all_methods
            print(f"full scan: {len(methods)} application methods with a known line span")

        print("TOUCHED (git log -L per affected method)...")
        stats["touched_edges_written"], skipped = load_touched(s, repo, methods)
        stats["methods_unresolved"] = len(skipped)

        print("LAST_MODIFIED (git blame per affected method)...")
        stats["last_modified_edges_written"] = load_last_modified(s, repo, methods)

        print("FIXED (fix commits from messages)...")
        stats["commits_reclassified"] = reclassify_commits(s)
        stats["fixed_edges"], stats["fix_commits_touching_methods"] = load_fixed(s)

        stats["touched_edges_total"] = s.run(
            "MATCH (:Commit)-[r:TOUCHED]->() RETURN count(r) AS c"
        ).single()["c"]
        stats["last_modified_edges_total"] = s.run(
            "MATCH (:Author)-[r:LAST_MODIFIED]->() RETURN count(r) AS c"
        ).single()["c"]

        head_sha = git(repo, "rev-parse", "HEAD").strip()
        save_state(state_path, {"repo": os.path.realpath(repo), "last_sha": head_sha})

    driver.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to the git repository to ingest")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--state", default=None,
                     help="where the last-ingested commit sha is remembered "
                          "(default: $LCB_HOME/out/state/<repo>-<hash>/git_state.json)")
    ap.add_argument("--full", action="store_true",
                     help="ignore saved state and rescan all history/methods")
    args = ap.parse_args()

    stats = ingest(args.repo, args.wipe, args.state or default_state_path(args.repo), args.full)

    print("\n=== history layer loaded ===")
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k.ljust(width)}  {v}")


if __name__ == "__main__":
    sys.exit(main())
