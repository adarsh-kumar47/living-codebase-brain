#!/usr/bin/env bash
# start.sh - bring the code graph up and prove it is answering.
#
# Safe to run repeatedly: if Neo4j is already listening it starts nothing and just
# re-runs the checks, so it doubles as a pre-demo health check.
#
#   ./demo/start.sh

set -uo pipefail
cd "$(dirname "$0")/.."

PY=""
for p in .venv-mac/bin/python .venv/bin/python; do
  [ -x "$p" ] && PY="$p" && break
done
if [ -z "$PY" ]; then
  echo "  [FAIL] no virtualenv found: python3 -m venv .venv && .venv/bin/pip install -e ."
  exit 1
fi

up() { nc -z localhost 7687 >/dev/null 2>&1; }

echo
echo "=============================================================="
echo "  Living Code Base Brain - startup check"
echo "=============================================================="

if up; then
  echo "  [ok]   Neo4j already listening on 7687"
else
  if command -v brew >/dev/null 2>&1 && brew list neo4j >/dev/null 2>&1; then
    echo "  [..]   starting Neo4j (brew services start neo4j)"
    brew services start neo4j >/dev/null
  elif command -v neo4j >/dev/null 2>&1; then
    echo "  [..]   starting Neo4j (neo4j start)"
    neo4j start >/dev/null
  else
    echo "  [FAIL] Neo4j is not running and no neo4j install was found"
    echo "         install it (macOS: brew install neo4j) and set the password once:"
    echo "         neo4j-admin dbms set-initial-password lcbpassword"
    exit 1
  fi
  # Bolt accepts connections a while after the process starts; the first start on a
  # cold page cache is the slow one, so allow a generous window.
  for _ in $(seq 1 60); do
    up && break
    sleep 2
  done
  if up; then
    echo "  [ok]   Neo4j is up"
  else
    echo "  [FAIL] Neo4j did not come up within 120s"
    echo "         check its log (Homebrew: /opt/homebrew/var/log/neo4j/neo4j.log)"
    exit 1
  fi
fi

echo
echo "  graph contents"
echo "  --------------"
PYTHONPATH=. "$PY" -m lcb.health || exit $?

echo
echo "  live answer check: who calls OwnerRepository.findById?"
echo "  -------------------------------------------------------"
./cq "MATCH (caller:Method)-[c:CALLS]->(m:Method) MATCH (cls:Class)-[:HAS_METHOD]->(m) WHERE cls.fqn ENDS WITH 'OwnerRepository' AND m.name = 'findById' AND caller.owner <> cls.fqn RETURN caller.owner AS caller, caller.name AS method, c.line AS line ORDER BY caller;" \
  | sed 's/^/  /'

echo
echo "  [ok]   ready. browser: http://localhost:7474/browser/  (neo4j / lcbpassword)"
echo
