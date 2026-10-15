#!/usr/bin/env bash
# ask.sh - answer "who calls this method?" for any method the audience names.
#
# The method name comes from *them*, not from us. The script prints the Cypher it
# builds before running it, so you can show that the query is assembled from their
# input and executed against a live database.
#
#   ./demo/ask.sh getPet      # answer for a named method
#   ./demo/ask.sh --random    # pick one at random, so nothing is cherry-picked
#   ./demo/ask.sh --list      # show what they can choose from

set -uo pipefail
cd "$(dirname "$0")/.."

CANDIDATES="MATCH (m:Method)<-[:CALLS]-() WHERE m.is_external = false AND NOT m.name STARTS WITH '<' AND NOT m.name STARTS WITH 'lambda\$'"

case "${1:-}" in
  --list)
    echo
    echo "  Application methods that something calls - pick any one:"
    echo "  ---------------------------------------------------------"
    ./cq "$CANDIDATES RETURN DISTINCT m.name AS method ORDER BY method;" | sed 's/^/  /'
    echo
    exit 0
    ;;
  --random)
    # The draw happens in its own WITH, after the DISTINCT: "ORDER BY rand()" straight
    # after a DISTINCT returns the same row every run.
    METHOD=$(./cq "$CANDIDATES WITH DISTINCT m.name AS method WITH method, rand() AS r ORDER BY r LIMIT 1 RETURN method;" \
             | tail -1 | tr -d '" ')
    echo
    echo "  drew at random from the graph: $METHOD"
    ;;
  "")
    echo
    echo "usage: ./demo/ask.sh <methodName> | --random | --list"
    echo
    echo "Ask your audience to name a method, then pass it here."
    exit 2
    ;;
  *)
    METHOD="$1"
    ;;
esac

# Their input goes into the query, so accept only a Java identifier.
if ! [[ "$METHOD" =~ ^[A-Za-z_$][A-Za-z0-9_$]*$ ]]; then
  echo "  not a Java method name: $METHOD" >&2
  exit 2
fi

QUERY="MATCH (caller:Method)-[c:CALLS]->(m:Method) WHERE m.name = '$METHOD' AND m.is_external = false RETURN caller.owner AS caller_class, caller.name AS caller_method, c.line AS line, m.owner AS target_class ORDER BY caller_class, line;"

echo
echo "=============================================================="
echo "  Who calls '$METHOD' ?"
echo "=============================================================="
echo
echo "  The Cypher being sent to Neo4j:"
echo
echo "$QUERY" | sed -E 's/ (WHERE|RETURN|ORDER BY) /\n\1 /g' | sed 's/^/    /'
echo
echo "  Live result from the database:"
echo
ROWS=$(./cq "$QUERY")
if [ "$(echo "$ROWS" | grep -c .)" -le 1 ]; then
  echo "    (nothing calls it - try ./demo/ask.sh --list for names that have callers)"
else
  echo "$ROWS" | sed 's/^/    /'
fi
echo
