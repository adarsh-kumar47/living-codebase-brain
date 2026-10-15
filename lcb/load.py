"""Load extractor CSVs into Neo4j and run the CHA post-pass.

Usage:
    python -m lcb.load --in out/repos/<repo>-<hash>/csv [--wipe]
    (normally run for you by `lcb ingest <repo>`)

Call edges are only created when both endpoints were actually indexed. Calls into
the JDK or into classes outside the indexed set are counted and reported rather
than silently dropped -- that count is a result, not a footnote.

Incremental loading (phase 9): extraction always re-scans the full classpath (it is
already cheap, ~8s even for 171 JARs -- see README Sec.4.1). What used to be
expensive, and is now incremental, is the Neo4j WRITE: a --wipe reload rewrites
every one of ~1.25M edges even when a developer changed one method in one class.

Each class gets a content signature -- a hash of its own row plus every method,
call, inheritance, bean and injection row that belongs to it, all sorted so row
order never matters -- stored in `manifest.json` next to the CSVs. A rerun without
--wipe diffs the fresh signatures against that manifest: classes whose signature is
unchanged are skipped entirely (not re-sent to Neo4j at all); only new, changed, or
removed classes are written. Method NODES for an unchanged-identity method (same
owner, same fqn) are updated via MERGE rather than deleted and recreated, so any
Author/Commit/Issue history layered onto them by git_ingest/github_ingest survives
a structural reload -- only outgoing CALLS/EXTENDS/IMPLEMENTS/INJECTS edges for a
changed class are cleared and rewritten, since a method's body (hence its calls)
can change without its signature changing. CHA and the summary stats are always
recomputed in full; both are cheap (well under a second even at full graph size),
so there is no value in the complexity of making them incremental too.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time

from neo4j import GraphDatabase

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")

BATCH = 5000

CONSTRAINTS = [
    "CREATE CONSTRAINT class_fqn IF NOT EXISTS FOR (c:Class) REQUIRE c.fqn IS UNIQUE",
    "CREATE CONSTRAINT method_fqn IF NOT EXISTS FOR (m:Method) REQUIRE m.fqn IS UNIQUE",
    "CREATE CONSTRAINT package_name IF NOT EXISTS FOR (p:Package) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT artifact_name IF NOT EXISTS FOR (a:Artifact) REQUIRE a.name IS UNIQUE",
    # Not backed by any uniqueness constraint (many methods share an owner), but
    # clear_stale()/remove_deleted() match on it for every incremental load, and
    # without an index that is a full label scan over every Method node per
    # UNWIND row -- cheap for the few fqns a normal incremental run touches, but
    # catastrophic for the all-classes-changed fallback (no manifest found).
    "CREATE INDEX method_owner IF NOT EXISTS FOR (m:Method) ON (m.owner)",
]


def read_csv(path):
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def chunks(rows, n=BATCH):
    buf = []
    for r in rows:
        buf.append(r)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def fmt(n):
    return f"{n:,}"


def as_bool(v):
    return str(v).lower() == "true"


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def run_batches(session, query, rows, label, batch=BATCH):
    total = 0
    t0 = time.time()
    for chunk in chunks(rows, batch):
        session.run(query, rows=chunk)
        total += len(chunk)
        print(f"  {label}: {fmt(total)}", end="\r", flush=True)
    print(f"  {label}: {fmt(total)} in {time.time() - t0:.1f}s".ljust(60))
    return total


def resolve_inherited(call_rows, method_rows, extends_rows, implements_rows):
    """Points calls to inherited methods at the class that declares them.

    Bytecode names the class written at the call site, not the one declaring the
    method: `PageHelper.startPage(...)` compiles to `PageHelper#startPage`, but
    startPage is declared in PageHelper's superclass PageMethod. There is no
    `PageHelper#startPage` node, so the edge used to be dropped. This does what the
    JVM's own method resolution does: walk the superclass chain, then the
    superinterfaces, and take the first class that declares a method with the same
    name and descriptor (static interface methods are not inherited, so they are
    skipped). The class named at the call site is kept in `via`.

    Rows are rewritten in place. Returns how many were resolved."""
    methods = {r["fqn"]: r["is_static"] == "true" for r in method_rows}
    parent = {r["child"]: r["parent"] for r in extends_rows}
    ifaces = group_by(implements_rows, lambda r: r["child"])
    cache = {}

    def resolve(dst):
        owner, _, sig = dst.partition("#")
        chain, c = [], owner
        while c and c not in chain:
            chain.append(c)
            c = parent.get(c)
        for c in chain[1:]:
            if f"{c}#{sig}" in methods:
                return f"{c}#{sig}"
        seen, queue = set(), [i["iface"] for c in chain for i in ifaces.get(c, [])]
        while queue:
            i = queue.pop(0)
            if i in seen:
                continue
            seen.add(i)
            cand = f"{i}#{sig}"
            if cand in methods and not methods[cand]:
                return cand
            queue += [r["iface"] for r in ifaces.get(i, [])]
        return None

    resolved = 0
    for r in call_rows:
        if r["dst"] in methods:
            continue
        if r["dst"] not in cache:
            cache[r["dst"]] = resolve(r["dst"])
        target = cache[r["dst"]]
        if target:
            r["via"] = r["dst_owner"]
            r["dst"], r["dst_owner"] = target, target.partition("#")[0]
            resolved += 1
    return resolved


def owner_of(method_fqn):
    return method_fqn.split("#", 1)[0]


# --- per-class content signature, for incremental diffing -------------------


def row_sig(row):
    return "|".join(f"{k}={row[k]}" for k in sorted(row))


def group_sig(rows):
    return "\n".join(sorted(row_sig(r) for r in rows))


def group_by(rows, key_fn):
    out = {}
    for r in rows:
        out.setdefault(key_fn(r), []).append(r)
    return out


def manifest_path(indir):
    return os.path.join(indir, "manifest.json")


def load_manifest(indir):
    path = manifest_path(indir)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_manifest(indir, manifest):
    with open(manifest_path(indir), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)


def compute_manifest(class_rows, methods_by_owner, extends_by_child, implements_by_child,
                      calls_by_owner, beans_by_fqn, bean_methods_by_owner, injections_by_owner):
    """One signature per class: its own row, plus every row anywhere in the extracted
    CSVs that belongs to it -- methods, calls, inheritance, bean/injection data.
    Order-independent (every group is sorted before hashing), so re-extracting the
    same unchanged bytecode always yields the same signature even if ASM, jar
    iteration, or dict ordering happens to visit things in a different sequence."""
    manifest = {}
    for c in class_rows:
        fqn = c["fqn"]
        methods = methods_by_owner.get(fqn, [])
        parts = [
            row_sig(c),
            group_sig(methods),
            group_sig(extends_by_child.get(fqn, [])),
            group_sig(implements_by_child.get(fqn, [])),
            group_sig(calls_by_owner.get(fqn, [])),
            row_sig(beans_by_fqn[fqn]) if fqn in beans_by_fqn else "",
            group_sig(bean_methods_by_owner.get(fqn, [])),
            group_sig(injections_by_owner.get(fqn, [])),
        ]
        digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
        manifest[fqn] = {"hash": digest, "methods": sorted(m["fqn"] for m in methods)}
    return manifest


def diff_manifest(old, new):
    """(changed_or_new fqns, removed fqns, removed method fqns within a changed
    class). A class with no prior entry is "changed" -- there is nothing to diff
    it against, so it is written in full, same as a genuinely new class."""
    changed = {fqn for fqn, v in new.items() if old.get(fqn, {}).get("hash") != v["hash"]}
    removed = set(old) - set(new)
    removed_methods = set()
    for fqn in changed:
        old_methods = set(old.get(fqn, {}).get("methods", []))
        removed_methods |= old_methods - set(new[fqn]["methods"])
    return changed, removed, removed_methods


# --- loading ------------------------------------------------------------------


def clear_stale(session, changed_fqns):
    """For classes being rewritten (not wiped): remove whatever they could be
    stale about before the fresh rows land. Method/Class NODES are untouched here
    -- only their outgoing structural edges and bean/injection state -- so any
    history layered onto an unchanged-identity method by git_ingest/github_ingest
    survives. A method's body can change without its fqn changing, so CALLS and
    MAY_CALL from it cannot simply be left as-is the way the node itself can."""
    for chunk in chunks(sorted(changed_fqns), 5000):
        session.run(
            """
            UNWIND $fqns AS fqn
            MATCH (m:Method {owner: fqn})-[r:CALLS|MAY_CALL]->()
            DELETE r
            """,
            fqns=chunk,
        )
        session.run(
            """
            UNWIND $fqns AS fqn
            MATCH (c:Class {fqn: fqn})-[r:EXTENDS|IMPLEMENTS|INJECTS|INJECTS_BEAN_METHOD]->()
            DELETE r
            """,
            fqns=chunk,
        )
        session.run(
            """
            UNWIND $fqns AS fqn
            MATCH (c:Class {fqn: fqn})
            SET c.is_spring_bean = false, c.stereotype = null
            """,
            fqns=chunk,
        )
        session.run(
            """
            UNWIND $fqns AS fqn
            MATCH (m:Method {owner: fqn})
            SET m.is_bean_factory = false, m.bean_type = null
            """,
            fqns=chunk,
        )


def remove_deleted(session, removed_classes, removed_methods):
    """Classes/methods genuinely gone from the codebase. DETACH DELETE cascades
    away every edge touching them, history layer included -- correctly, since
    there is nothing left for that history to attach to."""
    for chunk in chunks(sorted(removed_methods), 5000):
        session.run(
            "UNWIND $fqns AS fqn MATCH (m:Method {fqn: fqn}) DETACH DELETE m",
            fqns=chunk,
        )
    for chunk in chunks(sorted(removed_classes), 5000):
        session.run(
            """
            UNWIND $fqns AS fqn
            MATCH (c:Class {fqn: fqn})
            OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:Method)
            DETACH DELETE c, m
            """,
            fqns=chunk,
        )


def load(indir, wipe):
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    stats = {}

    with driver.session() as s:
        if wipe:
            print("wiping graph...")
            # Batched delete keeps the transaction small enough to avoid heap pressure.
            while True:
                res = s.run(
                    "MATCH (n) WITH n LIMIT 50000 DETACH DELETE n RETURN count(n) AS c"
                ).single()
                if res["c"] == 0:
                    break

        print("constraints...")
        for c in CONSTRAINTS:
            s.run(c)

        # --- Read every CSV once, grouped by owning class -------------------
        class_rows = list(read_csv(os.path.join(indir, "classes.csv")))
        method_rows = list(read_csv(os.path.join(indir, "methods.csv")))
        extends_rows = list(read_csv(os.path.join(indir, "extends.csv")))
        implements_rows = list(read_csv(os.path.join(indir, "implements.csv")))
        beans_rows = list(read_csv(os.path.join(indir, "beans.csv")))
        bean_method_rows = list(read_csv(os.path.join(indir, "bean_methods.csv")))
        injection_rows = list(read_csv(os.path.join(indir, "injections.csv")))

        # Calls are deduplicated up front (same reasoning as before: MERGE would
        # need to scan existing relationships for every one of ~1.4M rows; CREATE
        # is an order of magnitude faster and is safe because every destination
        # below is either a freshly-wiped graph or a class whose edges were just
        # explicitly cleared).
        seen = set()
        call_rows = []
        raw_total = 0
        for r in read_csv(os.path.join(indir, "calls.csv")):
            raw_total += 1
            key = (r["src"], r["dst"], r["line"], r["opcode"])
            if key in seen:
                continue
            seen.add(key)
            call_rows.append(r)
        del seen
        stats["call_sites_total"] = raw_total
        stats["call_sites_deduped"] = len(call_rows)
        # Before the manifest is computed, so a class whose calls now resolve
        # differently (its superclass changed) counts as changed.
        stats["calls_resolved_inherited"] = resolve_inherited(
            call_rows, method_rows, extends_rows, implements_rows)

        methods_by_owner = group_by(method_rows, lambda r: r["owner"])
        extends_by_child = group_by(extends_rows, lambda r: r["child"])
        implements_by_child = group_by(implements_rows, lambda r: r["child"])
        calls_by_owner = group_by(call_rows, lambda r: owner_of(r["src"]))
        beans_by_fqn = {r["class_fqn"]: r for r in beans_rows}
        bean_methods_by_owner = group_by(bean_method_rows, lambda r: r["owner"])
        injections_by_owner = group_by(injection_rows, lambda r: r["owner_class"])

        # --- Decide what actually needs writing -----------------------------
        old_manifest = {} if wipe else load_manifest(indir)
        new_manifest = compute_manifest(
            class_rows, methods_by_owner, extends_by_child, implements_by_child,
            calls_by_owner, beans_by_fqn, bean_methods_by_owner, injections_by_owner,
        )

        if wipe or not old_manifest:
            changed = set(new_manifest)
            removed_classes, removed_methods = set(), set()
        else:
            changed, removed_classes, removed_methods = diff_manifest(old_manifest, new_manifest)

        stats["classes_total"] = len(new_manifest)
        stats["classes_changed"] = len(changed)
        stats["classes_removed"] = len(removed_classes)
        stats["classes_unchanged"] = len(new_manifest) - len(changed)

        if not wipe:
            # Scoped clearing is required here regardless of whether a prior
            # manifest was found. If the manifest is missing or was deleted but
            # the graph was NOT wiped, the graph can still hold real data from an
            # earlier run -- treating every class as "changed" without clearing
            # its stale edges first would let the blind CREATE in the calls step
            # below duplicate every one of those edges. clear_stale/remove_deleted
            # are safe no-ops when the graph genuinely has nothing yet (a true
            # first run), so there is no reason to skip them in that case either.
            if old_manifest:
                print(f"incremental: {len(changed):,} of {len(new_manifest):,} classes "
                      f"changed/new, {len(removed_classes):,} removed, "
                      f"{stats['classes_unchanged']:,} unchanged (skipped)")
            else:
                print(f"no manifest found for {indir} -- treating all "
                      f"{len(new_manifest):,} classes as changed (clearing any stale "
                      f"state first, in case the graph already holds data)")
            t0 = time.time()
            remove_deleted(s, removed_classes, removed_methods)
            clear_stale(s, changed)
            print(f"  cleared stale state in {time.time() - t0:.1f}s")
        else:
            print(f"full load: {len(new_manifest):,} classes")

        # Every row list below is filtered to `changed` -- an unchanged class's
        # rows are never sent to Neo4j at all, which is the entire saving.
        class_rows = [r for r in class_rows if r["fqn"] in changed]
        method_rows = [r for r in method_rows if r["owner"] in changed]
        extends_rows = [r for r in extends_rows if r["child"] in changed]
        implements_rows = [r for r in implements_rows if r["child"] in changed]
        call_rows = [r for r in call_rows if owner_of(r["src"]) in changed]
        beans_rows = [r for r in beans_rows if r["class_fqn"] in changed]
        bean_method_rows = [r for r in bean_method_rows if r["owner"] in changed]
        injection_rows = [r for r in injection_rows if r["owner_class"] in changed]

        # --- Classes -----------------------------------------------------
        rows = [
            {
                "fqn": r["fqn"],
                "name": r["name"],
                "package": r["package"],
                "is_interface": as_bool(r["is_interface"]),
                "is_abstract": as_bool(r["is_abstract"]),
                "is_external": as_bool(r["is_external"]),
                "source_file": r["source_file"],
                "artifact": r["artifact"],
            }
            for r in class_rows
        ]
        stats["classes"] = run_batches(
            s,
            """
            UNWIND $rows AS row
            MERGE (c:Class {fqn: row.fqn})
            SET c.name = row.name,
                c.package = row.package,
                c.is_interface = row.is_interface,
                c.is_abstract = row.is_abstract,
                c.is_external = row.is_external,
                c.source_file = row.source_file,
                c.artifact = row.artifact
            """,
            rows,
            "classes",
        )

        # --- Packages and Artifacts --------------------------------------
        run_batches(
            s,
            """
            UNWIND $rows AS row
            MATCH (c:Class {fqn: row.fqn})
            MERGE (p:Package {name: row.package})
            MERGE (p)-[:CONTAINS]->(c)
            MERGE (a:Artifact {name: row.artifact})
            SET a.is_external = row.is_external
            MERGE (a)-[:PROVIDES]->(c)
            """,
            rows,
            "package/artifact links",
        )

        # --- Methods ------------------------------------------------------
        mrows = [
            {
                "fqn": r["fqn"],
                "owner": r["owner"],
                "name": r["name"],
                "descriptor": r["descriptor"],
                "is_static": as_bool(r["is_static"]),
                "is_abstract": as_bool(r["is_abstract"]),
                "is_external": as_bool(r["is_external"]),
                "line": as_int(r["line"]),
                "line_end": as_int(r["line_end"]),
                "artifact": r["artifact"],
            }
            for r in method_rows
        ]
        stats["methods"] = run_batches(
            s,
            """
            UNWIND $rows AS row
            MERGE (m:Method {fqn: row.fqn})
            SET m.name = row.name,
                m.owner = row.owner,
                m.descriptor = row.descriptor,
                m.is_static = row.is_static,
                m.is_abstract = row.is_abstract,
                m.is_external = row.is_external,
                m.line = row.line,
                m.line_end = row.line_end,
                m.artifact = row.artifact
            WITH m, row
            MATCH (c:Class {fqn: row.owner})
            MERGE (c)-[:HAS_METHOD]->(m)
            """,
            mrows,
            "methods",
        )
        stats["methods_removed"] = len(removed_methods)

        # --- Inheritance ---------------------------------------------------
        # Only linked when the supertype is inside the indexed set; a PetClinic class
        # extending a JDK type has no parent node, and inventing one would be a lie.
        for src_rows, rel, a, b in [
            (extends_rows, "EXTENDS", "child", "parent"),
            (implements_rows, "IMPLEMENTS", "child", "iface"),
        ]:
            erows = [{"a": r[a], "b": r[b]} for r in src_rows]
            stats[rel.lower()] = run_batches(
                s,
                f"""
                UNWIND $rows AS row
                MATCH (x:Class {{fqn: row.a}})
                MATCH (y:Class {{fqn: row.b}})
                MERGE (x)-[:{rel}]->(y)
                """,
                erows,
                rel,
            )

        # --- Calls ----------------------------------------------------------
        crows = [
            {
                "src": r["src"],
                "dst": r["dst"],
                "opcode": r["opcode"],
                "exact": as_bool(r["exact"]),
                "line": as_int(r["line"]),
                "via": r.get("via"),
            }
            for r in call_rows
        ]
        run_batches(
            s,
            """
            UNWIND $rows AS row
            MATCH (a:Method {fqn: row.src})
            MATCH (b:Method {fqn: row.dst})
            CREATE (a)-[c:CALLS {line: row.line, opcode: row.opcode, exact: row.exact, via: row.via}]->(b)
            """,
            crows,
            "CALLS",
            batch=20000,
        )
        stats["calls_linked"] = s.run(
            "MATCH ()-[c:CALLS]->() RETURN count(c) AS c"
        ).single()["c"]
        stats["calls_outside_index"] = stats["call_sites_total"] - stats["calls_linked"]

        # --- CHA post-pass ---------------------------------------------------
        # INVOKEVIRTUAL/INVOKEINTERFACE name only the declared type; the runtime target
        # depends on dispatch. Class Hierarchy Analysis expands each such call to the
        # overrides reachable through the inheritance graph, matched on name+descriptor.
        #
        # Scope: expanding every virtual call inside the dependency set would produce
        # millions of edges within framework internals that no query ever traverses.
        # Only the two directions that carry analytic value are expanded:
        #   (1) app code -> any override        ("what might my call actually hit")
        #   (2) any caller -> an app override   ("where does the framework call into us")
        #
        # Always run in full, even on an incremental load: cheap (well under a
        # second at full graph size -- see README Sec.5), and re-deriving "what's
        # still valid" is simpler and safer than tracking which MAY_CALL edges an
        # incremental patch could have invalidated.
        print("CHA expansion (pass 1: calls originating in app code)...")
        t0 = time.time()
        s.run(
            """
            MATCH (src:Method)-[:CALLS {exact: false}]->(tgt:Method)
            WHERE src.is_external = false
            MATCH (base:Class)-[:HAS_METHOD]->(tgt)
            CALL (src, tgt, base) {
                MATCH (sub:Class)-[:EXTENDS|IMPLEMENTS*1..3]->(base)
                MATCH (sub)-[:HAS_METHOD]->(impl:Method)
                WHERE impl.name = tgt.name
                  AND impl.descriptor = tgt.descriptor
                  AND impl <> tgt
                MERGE (src)-[:MAY_CALL {via: 'CHA'}]->(impl)
            } IN TRANSACTIONS OF 500 ROWS
            """
        ).consume()
        print(f"  pass 1 done in {time.time() - t0:.1f}s")

        print("CHA expansion (pass 2: framework dispatch into app code)...")
        t0 = time.time()
        s.run(
            """
            MATCH (app:Class {is_external: false})-[:HAS_METHOD]->(impl:Method)
            MATCH (app)-[:EXTENDS|IMPLEMENTS*1..3]->(base:Class)-[:HAS_METHOD]->(tgt:Method)
            WHERE tgt.name = impl.name
              AND tgt.descriptor = impl.descriptor
              AND tgt <> impl
            CALL (impl, tgt) {
                MATCH (src:Method)-[:CALLS {exact: false}]->(tgt)
                MERGE (src)-[:MAY_CALL {via: 'CHA'}]->(impl)
            } IN TRANSACTIONS OF 500 ROWS
            """
        ).consume()
        print(f"  pass 2 done in {time.time() - t0:.1f}s")

        stats["may_call_edges"] = s.run(
            "MATCH ()-[r:MAY_CALL]->() RETURN count(r) AS c"
        ).single()["c"]

        # --- Spring beans / DI resolution (phase 10) -------------------------
        # New properties on existing Class/Method nodes, plus two new edge types
        # resolving each injection point to its candidate bean(s). Absent CSVs
        # (an extractor build predating phase 10) are skipped, not an error.
        if beans_rows:
            brows = [{"fqn": r["class_fqn"], "stereotype": r["stereotype"]} for r in beans_rows]
            s.run(
                """
                UNWIND $rows AS row
                MATCH (c:Class {fqn: row.fqn})
                SET c.is_spring_bean = true, c.stereotype = row.stereotype
                """,
                rows=brows,
            )
            stats["spring_beans"] = len(brows)

        if bean_method_rows:
            bmrows = [{"fqn": r["method_fqn"], "bean_type": r["return_type"]}
                       for r in bean_method_rows]
            s.run(
                """
                UNWIND $rows AS row
                MATCH (m:Method {fqn: row.fqn})
                SET m.is_bean_factory = true, m.bean_type = row.bean_type
                """,
                rows=bmrows,
            )
            stats["spring_bean_methods"] = len(bmrows)

        if injection_rows:
            irows = [
                {
                    "owner_class": r["owner_class"], "member_kind": r["member_kind"],
                    "member_name": r["member_name"], "param_index": as_int(r["param_index"]),
                    "declared_type": r["declared_type"], "qualifier": r["qualifier"],
                }
                for r in injection_rows
            ]
            stats["injection_points"] = len(irows)

            # Resolved to a bean CLASS: the declared type itself, or (the common
            # case for an interface injection) a bean reachable by walking
            # EXTENDS/IMPLEMENTS up to it. Multiple matches are kept, not
            # collapsed -- same over-approximation stance as MAY_CALL/CHA, for
            # the same reason: a blast radius (here, "what satisfies this
            # dependency") should not silently miss a candidate.
            stats["injects_class_edges"] = s.run(
                """
                UNWIND $rows AS row
                MATCH (owner:Class {fqn: row.owner_class})
                MATCH (target:Class {fqn: row.declared_type})
                OPTIONAL MATCH (b:Class {is_spring_bean: true})
                WHERE b = target OR (b)-[:EXTENDS|IMPLEMENTS*1..5]->(target)
                WITH owner, row, b WHERE b IS NOT NULL
                MERGE (owner)-[r:INJECTS]->(b)
                SET r.member_kind = row.member_kind, r.member_name = row.member_name,
                    r.param_index = row.param_index, r.qualifier = row.qualifier
                RETURN count(r) AS c
                """,
                rows=irows,
            ).single()["c"]

            # Resolved to a @Bean FACTORY METHOD: the declared type matches what
            # some @Bean method returns (exact match only -- unlike the class
            # case, walking the return type's own supertypes to find a
            # "compatible but not exact" @Bean match is not attempted; scoped
            # out as a further approximation on top of an already-approximate
            # recovery, not worth the added guesswork here).
            stats["injects_bean_method_edges"] = s.run(
                """
                UNWIND $rows AS row
                MATCH (owner:Class {fqn: row.owner_class})
                MATCH (m:Method {is_bean_factory: true, bean_type: row.declared_type})
                MERGE (owner)-[r:INJECTS_BEAN_METHOD]->(m)
                SET r.member_kind = row.member_kind, r.member_name = row.member_name,
                    r.param_index = row.param_index, r.qualifier = row.qualifier
                RETURN count(r) AS c
                """,
                rows=irows,
            ).single()["c"]

        # --- Summary ----------------------------------------------------------
        stats["classes_external"] = s.run(
            "MATCH (c:Class {is_external: true}) RETURN count(c) AS c"
        ).single()["c"]
        stats["classes_app"] = s.run(
            "MATCH (c:Class {is_external: false}) RETURN count(c) AS c"
        ).single()["c"]
        stats["artifacts"] = s.run(
            "MATCH (a:Artifact) RETURN count(a) AS c"
        ).single()["c"]
        stats["calls_exact"] = s.run(
            "MATCH ()-[c:CALLS {exact: true}]->() RETURN count(c) AS c"
        ).single()["c"]
        stats["calls_virtual"] = s.run(
            "MATCH ()-[c:CALLS {exact: false}]->() RETURN count(c) AS c"
        ).single()["c"]

        save_manifest(indir, new_manifest)

    driver.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="indir", required=True)
    ap.add_argument("--wipe", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    stats = load(args.indir, args.wipe)
    elapsed = time.time() - t0

    print("\n=== graph loaded ===")
    width = max(len(k) for k in stats)
    for k, v in stats.items():
        print(f"  {k.ljust(width)}  {v}")

    linked = stats.get("calls_linked", 0)
    total = stats.get("call_sites_total", 1)
    print(f"\n  call sites resolved inside index: {linked}/{total} "
          f"({100.0 * linked / max(total, 1):.1f}%)")
    print(f"  total time: {elapsed:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
