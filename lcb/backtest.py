"""Historical back-test for the risk score (phase 8, README Sec.10.5).

Procedure, as specified:
  1. Choose a cutoff T. Score every application method using only data dated at
     or before T (lcb.risk.compute(before_ts=T) -- fan_in is structural and has
     no historical cutoff; churn/bug_signal/author_departed do).
  2. Take the top-N highest-risk methods as of T.
  3. Count bugs actually attributed to each method (via CAUSED_BY) *after* T --
     dated by the fixing Issue's own `closed_at`.
  4. Compare flagged vs. unflagged, and against two baselines: fan-in alone, and
     churn alone -- the latter directly tests Sec.10.5's named confound ("busy
     code attracts more bug reports simply because it is touched more"; if the
     combined score doesn't beat churn-alone, it isn't earning its complexity).

This is not a rebuild of the structural graph as of T -- that would require
checking out and recompiling the historical revision (Sec.10.2's "expensive but
exact" option). fan_in is read from the current (single) compiled build at every
cutoff; only the historical signals (churn, bug_signal, author_departed) are
actually time-sliced. Stated as an approximation, not hidden.

Usage:
    python -m lcb.backtest [--cutoff 2023-01-01T00:00:00Z] [--top 15]
"""

import argparse
import datetime
import sys

from neo4j import GraphDatabase

from lcb.risk import URI, USER, PASSWORD, compute_raw_signals, score


def to_unix(iso):
    return int(datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=datetime.timezone.utc).timestamp())


def post_cutoff_bugs(session, cutoff_iso):
    """Ground truth: methods with a bug closed after the cutoff -- the thing the
    risk score, computed only from before-cutoff data, is trying to predict."""
    rows = session.run(
        """
        MATCH (i:Issue)-[c:CAUSED_BY]->(m:Method)
        WHERE i.closed_at IS NOT NULL AND i.closed_at > $cutoff_iso
        RETURN m.fqn AS fqn, sum(c.confidence) AS post_bug_signal,
               count(DISTINCT i) AS post_bug_count
        """,
        cutoff_iso=cutoff_iso,
    ).data()
    return {r["fqn"]: r for r in rows}


def recall_at_n(ranked_fqns, positives, n):
    top = set(ranked_fqns[:n])
    hits = top & positives
    return len(hits), hits


def rank_by(rows, key):
    return [r["fqn"] for r in sorted(rows, key=lambda r: -r[key])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2023-01-01T00:00:00Z")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    cutoff_ts = to_unix(args.cutoff)

    with GraphDatabase.driver(URI, auth=(USER, PASSWORD)) as d, d.session() as s:
        raw = compute_raw_signals(s, before_ts=cutoff_ts)
        risk_ranked = score([dict(r) for r in raw], weights=(0.25, 0.25, 0.25, 0.25))
        positives_data = post_cutoff_bugs(s, args.cutoff)

    positives = set(positives_data.keys())
    total_methods = len(raw)

    print(f"cutoff: {args.cutoff}  (risk scored using only data before this date)")
    print(f"application methods: {total_methods}")
    print(f"methods with a bug closed AFTER cutoff (ground truth positives): {len(positives)}")
    if len(positives) < 10:
        print("  ** too few positives for this comparison to be statistically "
              "meaningful -- reporting it anyway, honestly, not as a verdict. **")
    print()

    risk_order = [r["fqn"] for r in risk_ranked]
    fan_in_order = rank_by(raw, "fan_in")
    churn_order = rank_by(raw, "churn")

    by_fqn_raw = {r["fqn"]: r for r in raw}

    print(f"{'ranking':<20}{'recall@' + str(args.top):>12}{'/ positives':>14}   hits")
    print("-" * 70)
    for label, order in (("risk (combined)", risk_order),
                          ("fan_in only", fan_in_order),
                          ("churn only", churn_order)):
        hit_n, hits = recall_at_n(order, positives, args.top)
        names = ", ".join(f"{by_fqn_raw[h]['owner'].rsplit('.', 1)[-1]}.{by_fqn_raw[h]['name']}"
                           for h in hits) or "(none)"
        print(f"{label:<20}{hit_n:>12}{('/' + str(len(positives))):>14}   {names}")

    print(f"\ntop-{args.top} by combined risk, with their ground-truth outcome:")
    hdr = f"  {'class':<20}{'method':<22}{'risk':>7}  {'post-cutoff bug?'}"
    print(hdr)
    for r in risk_ranked[: args.top]:
        cls = r["owner"].rsplit(".", 1)[-1]
        hit = positives_data.get(r["fqn"])
        mark = f"YES ({hit['post_bug_count']} issue(s))" if hit else "no"
        print(f"  {cls:<20}{r['name']:<22}{r['risk']:>7.3f}  {mark}")

    missed = positives - set(risk_order[: args.top])
    if missed:
        print(f"\n{len(missed)} positive(s) NOT in the top-{args.top} by combined risk:")
        by_fqn = {r["fqn"]: r for r in risk_ranked}
        for fqn in sorted(missed, key=lambda f: risk_order.index(f)):
            r = by_fqn[fqn]
            rank = risk_order.index(fqn) + 1
            print(f"  {r['owner'].rsplit('.', 1)[-1]}.{r['name']}"
                  f" -- ranked #{rank} by risk, churn={r['churn']}, fan_in={r['fan_in']}")


if __name__ == "__main__":
    sys.exit(main())
