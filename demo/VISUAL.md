# Seeing the answer as a graph, not a table

Every query in this file was run against the live database and verified to
return **nodes and relationships** — the row/node/rel counts quoted under each
one are real output, not estimates.

## The rule that makes a query drawable

Neo4j Browser draws a picture only when the query returns **graph elements**.

| `RETURN` | Browser shows |
|---|---|
| `RETURN c.fqn, m.name, m.line` | a table — scalars cannot be drawn |
| `RETURN c, h, m` | a picture — nodes and the arrow between them |
| `RETURN p` where `p=(a)-[:CALLS]->(b)` | a picture — the whole path |

`./cq` is a text shell, so it always prints a table. **To see the graph, open
http://localhost:7474/browser/** (`neo4j` / `lcbpassword`) and paste the query there.

> **The URL trap.** Open the bare `http://localhost:7474` and Neo4j answers with
> discovery JSON — `{"bolt_routing":"neo4j://localhost:7687",...}` — which looks
> like the demo is broken. The UI is at **`/browser/`**. A full browser gets a
> `303` redirect there automatically; anything else (curl, VS Code's Simple
> Browser, a preview pane) shows you the JSON. Always present the `/browser/`
> URL directly.

Same data, two views: answer in the terminal, evidence on screen.

---

## Dead-code analysis — "which application methods are never called?"

### V1 — all 53 uncalled methods, grouped under their class
*53 rows · 68 nodes · 53 relationships*

```cypher
MATCH (c:Class {is_external:false})-[h:HAS_METHOD]->(m:Method)
WHERE NOT (m)<-[:CALLS]-() AND NOT (m)<-[:MAY_CALL]-()
  AND NOT m.name IN ['<init>','<clinit>']
RETURN c, h, m;
```

Draws 15 class nodes each fanning out to their orphaned methods — every method
node in the picture has exactly one arrow into it (`HAS_METHOD`) and **no
incoming `CALLS`**. That absence is the finding, and it is visible at a glance.

### V2 — the real dead method, next to its living overloads
*8 rows · 10 nodes · 10 relationships*

```cypher
MATCH (o:Class {fqn:'org.springframework.samples.petclinic.owner.Owner'})
      -[h:HAS_METHOD]->(m:Method {name:'getPet'})
OPTIONAL MATCH p=(:Method)-[:CALLS]->(m)
RETURN o, h, m, p;
```

The strongest single picture in this analysis. Three `getPet` nodes hang off
`Owner`; two are hit by inbound `CALLS` arrows (4 callers and 3 callers), and
`getPet(String)` sits there with none. The overload that is dead and the
overloads that are alive are the same shape in source and obviously different
here.

### V3 — `Vet`'s orphan island
*3 rows · 4 nodes · 3 relationships*

```cypher
MATCH (c:Class {is_external:false})-[h:HAS_METHOD]->(m:Method)
WHERE m.name IN ['addSpecialty','getNrOfSpecialties','getSpecialties']
OPTIONAL MATCH p=(:Method)-[:CALLS]->(m)
RETURN c, h, m, p;
```

4 nodes, 3 arrows, nothing else — `OPTIONAL MATCH` found no callers at all. Two
of these three are reached by `vetList.html` through Thymeleaf reflection, which
is exactly what a call graph cannot see. Good query to show *with* the caveat.

### V4 — why inherited methods look dead when they aren't
*5 rows · 8 nodes · 7 relationships*

```cypher
MATCH (sub:Class {is_external:false})-[e:EXTENDS]->(base:Class {is_external:false})
      -[h:HAS_METHOD]->(m:Method)
WHERE m.name IN ['isNew','getLastName']
RETURN sub, e, base, h, m;
```

`isNew` exists **once**, on `BaseEntity`. `Owner`, `Pet` and `Vet` reach it by
`EXTENDS`. Bytecode records `pet.isNew()` against the declared type `Pet`, and
there is no `Pet#isNew` node for the arrow to land on — so the edge is dropped
and the method reads as uncalled. The picture shows the gap the table cannot.

---

## Checking a query is drawable before presenting it

```bash
curl -s -u neo4j:lcbpassword -H "Content-Type: application/json" \
  -X POST http://localhost:7474/db/neo4j/tx/commit \
  -d '{"statements":[{"statement":"MATCH (c:Class {is_external:false})-[h:HAS_METHOD]->(m:Method) RETURN c,h,m LIMIT 5","resultDataContents":["graph"]}]}'
```

If `results[0].data[*].graph.nodes` is empty, the query returns scalars and the
browser will draw nothing. Fix the `RETURN`, not the browser.


---

## When localhost is not an option

Neo4j Browser needs the database running on the presenting machine. For a
screen-share, a projector, or a laptop that is not this one, there is a
self-contained page holding the same four subgraphs — real nodes and edges
exported from this database, with the Cypher and terminal output beside each:

**https://claude.ai/artifact/R2q4yS7Wc4JpQ4351f7M3H**

Rebuild it after reloading the graph by re-exporting the four views to JSON and
republishing; the data is embedded in the page, so it does not phone home to
`localhost` and keeps working with Neo4j stopped.
