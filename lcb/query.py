"""Ad-hoc Cypher runner against the code graph.

    python -m lcb.query "MATCH (c:Class) RETURN count(c)"
    python -m lcb.query --file queries/blast.cypher
"""

import argparse
import os
import sys

from neo4j import GraphDatabase

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")


_driver = None


def driver():
    # Reused across calls; constructing a driver costs ~2s of handshake, which
    # otherwise dominates the runtime of every short query.
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    return _driver


def run(cypher, params=None, limit=200):
    with driver().session() as s:
        res = s.run(cypher, params or {})
        return [r.data() for _, r in zip(range(limit), res)]


def render(rows):
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    out = ["  ".join(c.ljust(widths[c]) for c in cols)]
    out.append("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        out.append("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cypher", nargs="?")
    ap.add_argument("--file")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            cypher = fh.read()
    elif args.cypher:
        cypher = args.cypher
    else:
        cypher = sys.stdin.read()

    print(render(run(cypher, limit=args.limit)))


if __name__ == "__main__":
    main()
