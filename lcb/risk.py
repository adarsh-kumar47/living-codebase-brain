"""Risk scoring over application methods (phase 8).

    risk(m) = w1*fan_in(m) + w2*bug_signal(m) + w3*churn(m) + w4*author_departed(m)
              - w5*test_coverage(m)

combining the structural layer (fan_in -- blast radius) with the historical layers
built in phases 6-7 (churn from TOUCHED, bug_signal from CAUSED_BY plus FIXED fix
commits not already behind an issue, author_departed
from AUTHORED timestamps).

**test_coverage has no w5 term here -- it is dropped, not faked.** The extractor
indexes compiled application classes only (`target/classes`, i.e. `src/main/java`);
test sources were never extracted, so there is no honest coverage signal to combine.
Inventing a text-matching proxy (e.g. grepping src/test for the method's name) would
be exactly the kind of fabricated node the rest of this project refuses to do.

**Weights are an equal-weight placeholder, not fitted.** README Sec.10.4 says weights
should come from the back-test, not be hand-tuned -- but Sec.10.6 already predicted,
correctly (see lcb/backtest.py), that PetClinic's bug signal is too small to fit them
meaningfully. w1=w2=w3=w4=0.25 by default; override with --weights to experiment.

Every raw signal is min-max normalized to [0,1] across application methods before
weighting, since fan_in (an integer count), bug_signal (a sum of confidences, usually
<1), churn (an integer count) and author_departed (0/1) live on incomparable scales.

`author_departed` is computed from TOUCHED + AUTHORED, not from LAST_MODIFIED/blame,
so the same function works at any historical cutoff for the back-test: blame only
ever describes the *current* file, which cannot describe "as of 2023".

Usage:
    python -m lcb.risk [--top 15] [--weights 0.25,0.25,0.25,0.25] [--out out/risk/scores.json]
"""

import argparse
import json
import os
import sys

from neo4j import GraphDatabase

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")

# A contributor with no commit in the last year, relative to whatever "now" the
# score is computed as of, counts as departed. Arbitrary but stated -- open-source
# contributor churn is high enough (164 authors on 121 PetClinic methods) that this
# mostly saturates to 1, which is itself a reportable finding, not a tuning target.
DEPARTED_THRESHOLD_SECONDS = 365 * 24 * 3600


def driver():
    return GraphDatabase.driver(URI, auth=(USER, PASSWORD))


def compute_raw_signals(session, before_ts=None):
    """One row per application method with every risk input, computed using only
    data dated at or before `before_ts` (a unix timestamp) where the signal has a
    date at all. `before_ts=None` means "use everything" (the live score).

    fan_in is structural and has no historical cutoff -- the graph reflects one
    compiled build, not a sequence of them (README Sec.9's "requires a compiled
    build" limitation applies here too, and is why fan_in is the same at every
    cutoff in the back-test below).

    A bug is dated by its Issue's own `closed_at`, not by re-deriving a PR from
    CAUSED_BY -- an issue could in principle be referenced by more than one PR,
    so going back through FIXES would be ambiguous about *which* merge date
    applies to a given CAUSED_BY edge. closed_at is unambiguous and already on
    the node CAUSED_BY points from.

    `last_author`/`last_touch_ts` per method come from TOUCHED + AUTHORED, not
    LAST_MODIFIED (git blame) -- blame only ever describes the *current* file, so
    it cannot answer "who had touched this most recently as of 2023"."""
    before_iso = _iso(before_ts) if before_ts is not None else None

    rows = session.run(
        """
        MATCH (m:Method {is_external:false})
        WHERE NOT coalesce(m.is_generated, false)
        OPTIONAL MATCH (caller:Method)-[:CALLS]->(m)
        WITH m, count(DISTINCT caller) AS fan_in

        OPTIONAL MATCH (i:Issue)-[c:CAUSED_BY]->(m)
        WHERE $before_ts IS NULL
           OR (i.closed_at IS NOT NULL AND i.closed_at <= $before_iso)
        WITH m, fan_in, sum(coalesce(c.confidence, 0)) AS issue_signal

        // Fix commits found from messages (FIXED), minus any already counted
        // through an issue above, dated by the commit itself.
        OPTIONAL MATCH (fc:Commit)-[fx:FIXED]->(m)
        WHERE ($before_ts IS NULL OR fc.timestamp <= $before_ts)
          AND NOT EXISTS { (fc)-[:PART_OF]->(:PR)-[:FIXES]->(:Issue)-[:CAUSED_BY]->(m) }
        WITH m, fan_in, issue_signal, sum(coalesce(fx.confidence, 0)) AS fix_signal
        WITH m, fan_in, issue_signal + fix_signal AS bug_signal

        OPTIONAL MATCH (cm:Commit)-[:TOUCHED]->(m)
        WHERE $before_ts IS NULL OR cm.timestamp <= $before_ts
        WITH m, fan_in, bug_signal, cm
        ORDER BY cm.timestamp DESC
        WITH m, fan_in, bug_signal, count(cm) AS churn, collect(cm)[0] AS latest_commit
        OPTIONAL MATCH (a:Author)-[:AUTHORED]->(latest_commit)

        RETURN m.fqn AS fqn, m.owner AS owner, m.name AS name,
               fan_in, bug_signal, churn,
               a.email AS last_author, latest_commit.timestamp AS last_touch_ts
        """,
        before_ts=before_ts, before_iso=before_iso,
    ).data()

    repo_last_ts = session.run(
        "MATCH (cm:Commit) WHERE $before_ts IS NULL OR cm.timestamp <= $before_ts "
        "RETURN max(cm.timestamp) AS t",
        before_ts=before_ts,
    ).single()["t"]

    author_last_active = {
        r["email"]: r["last_active"]
        for r in session.run(
            "MATCH (a:Author)-[:AUTHORED]->(cm:Commit) "
            "WHERE $before_ts IS NULL OR cm.timestamp <= $before_ts "
            "RETURN a.email AS email, max(cm.timestamp) AS last_active",
            before_ts=before_ts,
        ).data()
    }

    out = []
    for r in rows:
        departed = 0
        if r["last_author"] and repo_last_ts is not None:
            last_active = author_last_active.get(r["last_author"], r["last_touch_ts"])
            if (repo_last_ts - last_active) > DEPARTED_THRESHOLD_SECONDS:
                departed = 1
        out.append({
            "fqn": r["fqn"], "owner": r["owner"], "name": r["name"],
            "fan_in": r["fan_in"], "bug_signal": r["bug_signal"],
            "churn": r["churn"], "author_departed": departed,
        })
    return out


def _iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minmax(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def score(rows, weights):
    w1, w2, w3, w4 = weights
    fan_in_n = _minmax([r["fan_in"] for r in rows])
    bug_n = _minmax([r["bug_signal"] for r in rows])
    churn_n = _minmax([r["churn"] for r in rows])
    # author_departed is already 0/1 -- normalizing it would just relabel 0/1 as
    # 0/1 again (or collapse to all-0 if every method is in the same state), so it
    # is used as-is rather than through _minmax.
    for i, r in enumerate(rows):
        r["fan_in_norm"] = fan_in_n[i]
        r["bug_signal_norm"] = bug_n[i]
        r["churn_norm"] = churn_n[i]
        r["risk"] = round(
            w1 * fan_in_n[i] + w2 * bug_n[i] + w3 * churn_n[i] + w4 * r["author_departed"], 4
        )
    return sorted(rows, key=lambda r: -r["risk"])


def compute(session, before_ts=None, weights=(0.25, 0.25, 0.25, 0.25)):
    return score(compute_raw_signals(session, before_ts), weights)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--weights", default="0.25,0.25,0.25,0.25",
                     help="w1,w2,w3,w4 for fan_in,bug_signal,churn,author_departed")
    ap.add_argument("--out", default="out/risk/scores.json")
    args = ap.parse_args()
    weights = tuple(float(x) for x in args.weights.split(","))

    with driver() as d, d.session() as s:
        ranked = compute(s, weights=weights)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(ranked, fh, indent=2)

    print(f"weights (fan_in, bug_signal, churn, author_departed): {weights}")
    print("  -- equal-weight placeholder, not fitted; see module docstring\n")
    hdr = f"{'class':<20}{'method':<24}{'fan_in':>7}{'bugs':>7}{'churn':>7}{'dep':>5}{'risk':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in ranked[: args.top]:
        cls = r["owner"].rsplit(".", 1)[-1]
        print(f"{cls:<20}{r['name']:<24}{r['fan_in']:>7}{r['bug_signal']:>7.2f}"
              f"{r['churn']:>7}{r['author_departed']:>5}{r['risk']:>8.3f}")
    print(f"\nfull ranking (full fqn) written to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
