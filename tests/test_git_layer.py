"""Tests for the git history layer (lcb/git_ingest.py) on a repository built to order.

The end-to-end test creates a small multi-module Java repo whose every commit,
author and touched line is chosen here, so the expected TOUCHED / LAST_MODIFIED
edges are known in advance rather than read back from git. It runs the real
pipeline -- javac, the extractor, lcb.load, lcb.git_ingest -- against a throwaway
Neo4j (tests/neo4j_sandbox.py), never the code graph on 7687.

    PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_git_layer -v

Needs JDK 21 (javac, java), git, and a local Neo4j install; the end-to-end test is
skipped when Neo4j or javac is missing. The other tests need git only.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from lcb.git_ingest import SourceIndex, default_state_path, usable_since_sha

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from neo4j_sandbox import Neo4jSandbox, find_neo4j  # noqa: E402

ALICE = ("Alice", "alice@acme.example")
BOB = ("Bob", "bob@acme.example")
CAROL_OLD = ("Carol", "carol@old.example")
CAROL = ("Carol", "carol@acme.example")
DAVE = ("Dave", "dave@acme.example")

# Isolate every git call from the developer's own config (signing, hooks, templates).
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example",
}


class FakeRepo:
    def __init__(self, path):
        self.path = path
        self.clock = 1_700_000_000
        self.sha = {}
        os.makedirs(path)
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.path, *args], check=True, capture_output=True,
                              text=True, env={**os.environ, **GIT_ENV}).stdout

    def write(self, rel, body):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(body).lstrip("\n"))

    def commit(self, label, author, message):
        self.clock += 3600  # strictly increasing, so "most recent" is never a tie
        date = f"@{self.clock} +0000"
        self.git("add", "-A")
        subprocess.run(
            ["git", "-C", self.path, "commit", "-q", "-m", message],
            check=True, capture_output=True,
            env={**os.environ, **GIT_ENV, "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
                 "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
        )
        self.sha[label] = self.git("rev-parse", "HEAD").strip()


# Calculator.add's body is lines 5-6, sub's (once added) lines 10-11. Line numbers
# matter: the extractor records each method's [first, last] body line, and git log -L
# follows exactly that range.
CALC_V1 = """
    package com.acme.core;

    public class Calculator {
        public int add(int a, int b) {
            int sum = a + b;
            return sum;
        }
    }
"""
CALC_V2 = CALC_V1.replace("int sum = a + b;", "int sum = b + a;")
CALC_V3 = CALC_V2.replace("""
            return sum;
        }
    }
""", """
            return sum;
        }

        public int sub(int a, int b) {
            int diff = a - b;
            return diff;
        }
    }
""")
CALC_V4 = CALC_V3.replace("int diff = a - b;", "int diff = -(b - a);")
CALC_V5 = CALC_V4.replace("int sum = b + a;", "int sum = Math.addExact(a, b);")

CONTROLLER_V1 = """
    package com.acme.web;

    import com.acme.core.Calculator;

    public class Controller {
        private final Calculator calc = new Calculator();

        public int handle(int x) {
            int y = calc.add(x, 1);
            return y;
        }
    }
"""
CONTROLLER_V2 = CONTROLLER_V1.replace("calc.add(x, 1)", "calc.add(x, 2)")

# Same file name in two packages and two modules -- only the package path tells them apart.
STRINGS = """
    package com.acme.{pkg}.util;

    public class Strings {{
        public static String id(String s) {{
            String r = s;
            return r;
        }}
    }}
"""
WEB_STRINGS_V2 = STRINGS.format(pkg="web").replace("String r = s;", "String r = s.trim();")

CALC = "core/src/main/java/com/acme/core/Calculator.java"
CTRL = "web/src/main/java/com/acme/web/Controller.java"
CORE_STR = "core/src/main/java/com/acme/core/util/Strings.java"
WEB_STR = "web/src/main/java/com/acme/web/util/Strings.java"


def build_history(repo):
    repo.write(CALC, CALC_V1)
    repo.write(CTRL, CONTROLLER_V1)
    repo.write(CORE_STR, STRINGS.format(pkg="core"))
    repo.write(WEB_STR, STRINGS.format(pkg="web"))
    repo.commit("c1", ALICE, "initial modules")
    repo.write(CALC, CALC_V2)
    repo.commit("c2", BOB, "swap operands in add")
    repo.write(CALC, CALC_V3)
    repo.commit("c3", ALICE, "add sub")
    repo.write(CTRL, CONTROLLER_V2)
    repo.commit("c4", CAROL_OLD, "handle adds 2")
    repo.write(".mailmap", f"{CAROL[0]} <{CAROL[1]}> <{CAROL_OLD[1]}>\n")
    repo.commit("c5", ALICE, "mailmap: Carol's old address")
    repo.write(CALC, CALC_V4)
    repo.commit("c6", CAROL, "Fix #1: sub returned the wrong sign")
    repo.write(WEB_STR, WEB_STRINGS_V2)
    repo.commit("c7", DAVE, "fix: trim input in web Strings.id")


def run(*cmd, env=None, cwd=ROOT):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


def load_structure(tmp, repo, env):
    """javac -> extractor -> lcb.load for the fake repo's four source files."""
    from lcb.ingest import ensure_extractor
    ensure_extractor()  # extractor/build is not in git; compile it on a fresh clone
    classes = os.path.join(tmp, "classes")
    run("javac", "-d", classes, *(os.path.join(repo.path, p) for p in (CALC, CTRL, CORE_STR, WEB_STR)))
    extractor_cp = os.pathsep.join([os.path.join(ROOT, "extractor/lib/asm-9.7.1.jar"),
                                    os.path.join(ROOT, "extractor/build")])
    csv_dir = os.path.join(tmp, "csv")
    run("java", "-cp", extractor_cp, "Extractor", "--classes", classes, "--out", csv_dir)
    run(sys.executable, "-m", "lcb.load", "--in", csv_dir, "--wipe", env=env)


# --- no Neo4j needed ----------------------------------------------------------


class SourceIndexTest(unittest.TestCase):
    def test_package_path_picks_module_and_package(self):
        idx = SourceIndex([CALC, CTRL, CORE_STR, WEB_STR])
        self.assertEqual(idx.resolve("com.acme.core.util", "Strings.java"), CORE_STR)
        self.assertEqual(idx.resolve("com.acme.web.util", "Strings.java"), WEB_STR)
        self.assertEqual(idx.resolve("com.acme.web", "Controller.java"), CTRL)

    def test_main_source_beats_test_source(self):
        main = "app/src/main/java/a/Foo.java"
        idx = SourceIndex([main, "app/src/test/java/a/Foo.java"])
        self.assertEqual(idx.resolve("a", "Foo.java"), main)

    def test_kotlin_file_outside_its_package_dir(self):
        idx = SourceIndex(["src/main/kotlin/Extensions.kt"])
        self.assertEqual(idx.resolve("com.acme.ext", "Extensions.kt"), "src/main/kotlin/Extensions.kt")

    def test_java_file_in_wrong_package_is_not_guessed(self):
        idx = SourceIndex(["src/main/java/other/Foo.java"])
        self.assertIsNone(idx.resolve("com.acme", "Foo.java"))

    def test_genuinely_ambiguous_is_none(self):
        idx = SourceIndex(["a/src/main/java/p/Foo.java", "b/src/main/java/p/Foo.java"])
        self.assertIsNone(idx.resolve("p", "Foo.java"))

    def test_default_package_and_missing_source(self):
        idx = SourceIndex(["src/main/java/Main.java"])
        self.assertEqual(idx.resolve("", "Main.java"), "src/main/java/Main.java")
        self.assertIsNone(idx.resolve("p", None))


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lcb-state-")
        self.a = FakeRepo(os.path.join(self.tmp, "a"))
        self.a.write("A.java", "class A {}\n")
        self.a.commit("a1", ALICE, "a1")
        self.b = FakeRepo(os.path.join(self.tmp, "b"))
        self.b.write("B.java", "class B {}\n")
        self.b.commit("b1", BOB, "b1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_repo_gets_its_own_state_file(self):
        self.assertNotEqual(default_state_path(self.a.path), default_state_path(self.b.path))
        self.assertEqual(default_state_path(self.a.path), default_state_path(self.a.path + "/"))

    def test_state_from_same_repo_is_used(self):
        state = {"repo": os.path.realpath(self.a.path), "last_sha": self.a.sha["a1"]}
        self.assertEqual(usable_since_sha(self.a.path, state), self.a.sha["a1"])

    def test_state_from_another_repo_is_ignored(self):
        state = {"repo": os.path.realpath(self.a.path), "last_sha": self.a.sha["a1"]}
        self.assertIsNone(usable_since_sha(self.b.path, state))

    def test_legacy_state_with_foreign_sha_is_ignored(self):
        # Pre-fix state files carry no "repo" key; the sha check still catches a mix-up.
        self.assertIsNone(usable_since_sha(self.b.path, {"last_sha": self.a.sha["a1"]}))

    def test_no_state(self):
        self.assertIsNone(usable_since_sha(self.a.path, None))


# --- end to end ---------------------------------------------------------------


@unittest.skipUnless(find_neo4j() and shutil.which("javac"), "needs a local Neo4j install and javac")
class GitIngestEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lcb-e2e-")
        cls.repo = FakeRepo(os.path.join(cls.tmp, "repo"))
        build_history(cls.repo)
        cls.sandbox = Neo4jSandbox().start()
        cls.env = {"NEO4J_URI": cls.sandbox.uri, "PYTHONPATH": ROOT}
        cls.state = os.path.join(cls.tmp, "git_state.json")
        load_structure(cls.tmp, cls.repo, cls.env)
        cls.first = cls.ingest("--wipe")
        from neo4j import GraphDatabase
        cls.driver = GraphDatabase.driver(cls.sandbox.uri, auth=("neo4j", "unused"))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "driver", None):
            cls.driver.close()
        if getattr(cls, "sandbox", None):
            cls.sandbox.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def ingest(cls, *flags):
        out = run(sys.executable, "-m", "lcb.git_ingest", "--repo", cls.repo.path,
                  "--state", cls.state, *flags, env=cls.env)
        stats = {}
        for line in out.split("=== history layer loaded ===")[-1].splitlines():
            parts = line.split()
            if len(parts) == 2:
                stats[parts[0]] = int(parts[1])
        return stats

    def query(self, cypher, **params):
        return self.driver.execute_query(cypher, params).records

    def touched(self, owner, name):
        rows = self.query(
            "MATCH (c:Commit)-[:TOUCHED]->(m:Method {owner: $owner, name: $name}) RETURN c.sha AS sha",
            owner=owner, name=name)
        return {r["sha"] for r in rows}

    def last_modified(self, owner, name):
        rows = self.query(
            "MATCH (a:Author)-[:LAST_MODIFIED]->(m:Method {owner: $owner, name: $name}) RETURN a.email AS e",
            owner=owner, name=name)
        return [r["e"] for r in rows]

    def shas(self, *labels):
        return {self.repo.sha[label] for label in labels}

    # The tests below run in alphabetical order; test_z_incremental mutates the repo
    # and graph, so it is named to run last.

    def test_every_method_resolves_to_its_source_file(self):
        self.assertEqual(self.first["methods_unresolved"], 0)

    def test_commits_and_mailmapped_authors(self):
        self.assertEqual(self.first["new_commits"], 7)
        emails = {r["e"] for r in self.query("MATCH (a:Author) RETURN a.email AS e")}
        self.assertEqual(emails, {ALICE[1], BOB[1], CAROL[1], DAVE[1]})
        carol = self.query("MATCH (:Author {email: $e})-[:AUTHORED]->(c) RETURN count(c) AS n", e=CAROL[1])
        self.assertEqual(carol[0]["n"], 2)

    def test_touched_follows_only_commits_that_changed_the_method(self):
        self.assertEqual(self.touched("com.acme.core.Calculator", "add"), self.shas("c1", "c2"))
        self.assertEqual(self.touched("com.acme.core.Calculator", "sub"), self.shas("c3", "c6"))
        self.assertEqual(self.touched("com.acme.web.Controller", "handle"), self.shas("c1", "c4"))

    def test_same_file_name_in_two_modules_kept_apart(self):
        self.assertEqual(self.touched("com.acme.core.util.Strings", "id"), self.shas("c1"))
        self.assertEqual(self.touched("com.acme.web.util.Strings", "id"), self.shas("c1", "c7"))
        self.assertEqual(self.last_modified("com.acme.core.util.Strings", "id"), [ALICE[1]])
        self.assertEqual(self.last_modified("com.acme.web.util.Strings", "id"), [DAVE[1]])

    def test_last_modified_is_most_recent_author(self):
        self.assertEqual(self.last_modified("com.acme.core.Calculator", "add"), [BOB[1]])
        self.assertEqual(self.last_modified("com.acme.core.Calculator", "sub"), [CAROL[1]])
        # c4 was committed under Carol's old address; .mailmap folds it into the new one.
        self.assertEqual(self.last_modified("com.acme.web.Controller", "handle"), [CAROL[1]])

    def test_fix_commits_found_from_messages(self):
        kinds = {r["sha"]: (r["kind"], r["refs"]) for r in self.query(
            "MATCH (c:Commit) RETURN c.sha AS sha, c.fix_kind AS kind, c.issue_refs AS refs")}
        self.assertEqual(kinds[self.repo.sha["c6"]], ("keyword", [1]))
        self.assertEqual(kinds[self.repo.sha["c7"]], ("conventional", []))
        self.assertEqual(kinds[self.repo.sha["c2"]], (None, []))
        fixed = {(r["sha"], r["owner"], r["name"]): r["conf"] for r in self.query(
            "MATCH (c:Commit)-[f:FIXED]->(m:Method) "
            "RETURN c.sha AS sha, m.owner AS owner, m.name AS name, f.confidence AS conf")}
        # Each fix touched exactly one method in one file: focus 1, line share 1,
        # size 1/(1 + 1/10).
        self.assertEqual(fixed, {
            (self.repo.sha["c6"], "com.acme.core.Calculator", "sub"): 0.9091,
            (self.repo.sha["c7"], "com.acme.web.util.Strings", "id"): 0.9091,
        })

    def test_uncommitted_edits_do_not_count(self):
        path = os.path.join(self.repo.path, CTRL)
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original.replace("calc.add(x, 2)", "calc.add(x, 3)"))
            self.ingest("--full")
            self.assertEqual(self.last_modified("com.acme.web.Controller", "handle"), [CAROL[1]])
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)

    def test_y_improved_classifier_reaches_old_commits_without_a_rescan(self):
        # Simulate a commit classified by an older rule set: no fix kind, no FIXED edge.
        self.query("MATCH (c:Commit {sha: $sha}) SET c.fix_kind = null "
                   "WITH c MATCH (c)-[f:FIXED]->() DELETE f", sha=self.repo.sha["c7"])
        stats = self.ingest()  # incremental: no new commits
        self.assertEqual(stats["new_commits"], 0)
        self.assertEqual(stats["commits_reclassified"], 1)
        self.assertEqual(self.query("MATCH (c:Commit {sha: $sha})-[:FIXED]->(m) RETURN m.name AS n",
                                    sha=self.repo.sha["c7"])[0]["n"], "id")

    def test_z_incremental_run_picks_up_only_the_new_commit(self):
        self.repo.write(CALC, CALC_V5)
        self.repo.commit("c8", BOB, "overflow-safe add")
        stats = self.ingest()
        self.assertEqual(stats["new_commits"], 1)
        self.assertEqual(self.touched("com.acme.core.Calculator", "add"), self.shas("c1", "c2", "c8"))
        self.assertEqual(self.last_modified("com.acme.core.Calculator", "add"), [BOB[1]])
        # Untouched file: edges unchanged.
        self.assertEqual(self.touched("com.acme.web.Controller", "handle"), self.shas("c1", "c4"))
        self.assertEqual(self.ingest()["new_commits"], 0)


if __name__ == "__main__":
    unittest.main()
