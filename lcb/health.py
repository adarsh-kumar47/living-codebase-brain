"""Verify the code graph is up and holds what the demo expects.

Exits non-zero with a hint if anything is off, so shell wrappers can just check
the status code.

    python -m lcb.health
"""

import sys

EXPECTED = [
    ("classes",    "MATCH (c:Class)  RETURN count(c) AS n",       53597),
    ("methods",    "MATCH (m:Method) RETURN count(m) AS n",      446071),
    ("call edges", "MATCH ()-[r:CALLS]->() RETURN count(r) AS n", 705057),
]


def main():
    try:
        from lcb.query import run
    except Exception as e:
        print(f"  [FAIL] cannot import lcb ({e})")
        return 1

    bad = False
    for label, cypher, expected in EXPECTED:
        try:
            n = run(cypher)[0]["n"]
        except Exception as e:
            print(f"  [FAIL] {label}: {e}")
            bad = True
            continue
        if n != expected:
            bad = True
        print(f"  [{'ok' if n == expected else '??'}]   {label:<12} {n:>9,}"
              f"   (expected {expected:,})")

    if bad:
        print("\n  graph is not in the expected (PetClinic) state - reload it:")
        print("  lcb ingest targets/petclinic --replace")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
