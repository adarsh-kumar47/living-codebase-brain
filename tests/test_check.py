"""Tests for lcb/check.py: does a diff get mapped to the right methods, and are the
ones with a bug history flagged?

Reuses the scripted repo from test_git_layer (same known history), loads it into a
throwaway Neo4j, adds two hand-made issues -- one strongly linked to Calculator.sub
(and to commit c6, which fixed it), one weakly to Controller.handle -- and then checks
a series of working-tree edits. Commit c7 ("fix: trim input in web Strings.id") is a
fix found from its message alone, with no issue behind it.

    PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_check -v
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from lcb.check import line_now, parse_diff, signature_line, touches

from tests.test_git_layer import (
    CALC, CTRL, GIT_ENV, WEB_STR, ROOT, FakeRepo, build_history, find_neo4j, load_structure, run, BOB,
)
from tests.neo4j_sandbox import Neo4jSandbox

# Calculator.java at HEAD of build_history():
#   4  public int add(int a, int b) {     <- add: body 5-6, signature 4
#   5      int sum = b + a;
#   6      return sum;
#   7  }
#   9  public int sub(int a, int b) {     <- sub: body 10-11, signature 9
#  10      int diff = -(b - a);
#  11      return diff;
#  12  }
#  13 }


class ParseDiffTest(unittest.TestCase):
    def test_hunks_use_old_side_and_skip_new_files(self):
        diff = "\n".join([
            "diff --git a/A.java b/A.java",
            "--- a/A.java",
            "+++ b/A.java",
            "@@ -10 +10 @@ class A {",
            "@@ -20,3 +22,0 @@",
            "@@ -30,0 +31,2 @@",
            "diff --git a/New.java b/New.java",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/New.java",
            "@@ -0,0 +1,5 @@",
        ])
        self.assertEqual(parse_diff(diff), {"A.java": [(10, 1, 10, 1), (20, 3, 22, 0), (30, 0, 31, 2)]})


class LineNowTest(unittest.TestCase):
    def test_lines_shift_past_insertions_and_deletions(self):
        # 3 lines inserted after line 1, line 10 modified, lines 20-21 deleted.
        hunks = [(1, 0, 2, 3), (10, 1, 13, 1), (20, 2, 22, 0)]
        self.assertEqual(line_now(1, hunks), 1)    # above everything
        self.assertEqual(line_now(5, hunks), 8)    # after the insertion
        self.assertEqual(line_now(10, hunks), 13)  # inside the modified hunk
        self.assertEqual(line_now(15, hunks), 18)
        self.assertEqual(line_now(21, hunks), 23)  # deleted: maps to where it was
        self.assertEqual(line_now(25, hunks), 26)  # after the deletion: +3 -2

    def test_no_hunks(self):
        self.assertEqual(line_now(7, []), 7)


class TouchesTest(unittest.TestCase):
    # sub: signature line 9, body 10..11
    def test_body_and_signature_edits(self):
        self.assertTrue(touches(10, 1, 9, 11))
        self.assertTrue(touches(9, 1, 9, 11))       # parameter list changed
        self.assertFalse(touches(12, 1, 9, 11))     # closing brace line is outside the span
        self.assertTrue(touches(1, 20, 9, 11))      # a hunk swallowing the whole method

    def test_insertions(self):
        self.assertTrue(touches(10, 0, 9, 11))      # between the two body lines
        self.assertTrue(touches(9, 0, 9, 11))       # right after the signature
        self.assertFalse(touches(12, 0, 9, 11))     # new method after the closing brace
        self.assertFalse(touches(8, 0, 9, 11))      # before the signature


class SignatureLineTest(unittest.TestCase):
    # The shape of PetClinic's OwnerController.processFindForm: an annotation, a
    # signature wrapped over two lines, then a comment before the first statement.
    SOURCE = [
        "    public String initFindForm() {",          # 1
        "        return \"owners/findOwners\";",       # 2
        "    }",                                        # 3
        "",                                             # 4
        "    @GetMapping(\"/owners\")",                 # 5
        "    public String processFindForm(int page, Owner owner,",  # 6
        "            Model model) {",                   # 7
        "        // allow parameterless GET",           # 8
        "        String lastName = owner.getLastName();",  # 9
    ]

    def test_skips_comments_and_wrapped_parameters(self):
        self.assertEqual(signature_line(self.SOURCE, "processFindForm", "a.OwnerController", 9), 6)

    def test_does_not_cross_into_the_previous_method(self):
        self.assertEqual(signature_line(self.SOURCE, "missing", "a.OwnerController", 9), 8)

    def test_constructor_uses_the_simple_class_name(self):
        src = ["class Outer {", "  static class Inner {", "    Inner(int x) {", "      this.x = x;"]
        self.assertEqual(signature_line(src, "<init>", "p.Outer$Inner", 4), 3)

    def test_lambdas_and_missing_source_fall_back(self):
        self.assertEqual(signature_line(self.SOURCE, "lambda$processFindForm$0", "a.OwnerController", 9), 8)
        self.assertEqual(signature_line([], "processFindForm", "a.OwnerController", 9), 8)


@unittest.skipUnless(find_neo4j() and shutil.which("javac"), "needs a local Neo4j install and javac")
class CheckEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lcb-check-")
        cls.repo = FakeRepo(os.path.join(cls.tmp, "repo"))
        build_history(cls.repo)
        cls.sandbox = Neo4jSandbox().start()
        # LCB_HOME keeps the default state path inside tmp, where check's staleness
        # note looks for it -- and out of the real project's out/state.
        cls.env = {"NEO4J_URI": cls.sandbox.uri, "PYTHONPATH": ROOT, "LCB_HOME": cls.tmp}
        load_structure(cls.tmp, cls.repo, cls.env)
        run(sys.executable, "-m", "lcb.git_ingest", "--repo", cls.repo.path, "--wipe",
            env=cls.env, cwd=cls.tmp)
        from neo4j import GraphDatabase
        with GraphDatabase.driver(cls.sandbox.uri, auth=("neo4j", "unused")) as d:
            d.execute_query("""
                MATCH (sub:Method {owner: 'com.acme.core.Calculator', name: 'sub'})
                MATCH (handle:Method {owner: 'com.acme.web.Controller', name: 'handle'})
                CREATE (i1:Issue {number: 1, title: 'sub returns wrong sign', state: 'CLOSED'})
                CREATE (i2:Issue {number: 2, title: 'typo in log', state: 'CLOSED'})
                CREATE (pr:PR {number: 10, title: 'fix sub sign'})-[:FIXES]->(i1)
                WITH sub, handle, i1, i2, pr
                MATCH (c6:Commit {sha: $c6})
                CREATE (c6)-[:PART_OF]->(pr)
                CREATE (i1)-[:CAUSED_BY {confidence: 0.8}]->(sub)
                CREATE (i2)-[:CAUSED_BY {confidence: 0.05}]->(handle)
            """, c6=cls.repo.sha["c6"])
        with open(os.path.join(cls.repo.path, CALC), encoding="utf-8") as fh:
            cls.calc_lines = fh.read().splitlines(keepends=True)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "sandbox", None):
            cls.sandbox.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        self.repo.git("reset", "-q")
        self.repo.git("checkout", "--", ".")

    def edit_calc(self, replace=None, insert_after=None):
        """replace: {line number: new text}; insert_after: {line number: [new lines]}."""
        lines = list(self.calc_lines)
        for n, text in sorted((insert_after or {}).items(), reverse=True):
            lines[n:n] = [f"{t}\n" for t in text]
        for n, text in (replace or {}).items():
            shift = sum(len(t) for k, t in (insert_after or {}).items() if k < n)
            lines[n - 1 + shift] = text + "\n"
        with open(os.path.join(self.repo.path, CALC), "w", encoding="utf-8") as fh:
            fh.writelines(lines)

    def check(self, *flags):
        proc = subprocess.run([sys.executable, "-m", "lcb.check", "--repo", self.repo.path, "--json", *flags],
                              cwd=self.tmp, capture_output=True, text=True, env={**os.environ, **self.env})
        # 1 is a legitimate answer (--strict, something flagged) but also what a
        # crash exits with -- so a crash is told apart by having no output.
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        self.assertTrue(proc.stdout.strip(), f"lcb.check printed nothing:\n{proc.stderr}")
        return json.loads(proc.stdout), proc.returncode

    def changed(self, result):
        return {m["method"]: m["flagged"] for m in result["methods"]}

    def test_editing_a_method_with_a_bug_flags_it(self):
        self.edit_calc(replace={10: "        int diff = a - b;"})
        result, code = self.check()
        self.assertEqual(self.changed(result), {"Calculator.sub": True})
        issue = result["methods"][0]["issues"][0]
        self.assertEqual((issue["number"], issue["fixed_by"]), (1, [10]))
        # c6 is also a fix by its message, but it is the commit behind issue #1:
        # shown once, as the issue.
        self.assertEqual(result["methods"][0]["fix_commits"], [])
        self.assertEqual(result["methods"][0]["last_author"], "Carol")
        self.assertEqual(code, 0)

    def test_strict_exits_nonzero_only_when_flagged(self):
        self.edit_calc(replace={10: "        int diff = a - b;"})
        self.assertEqual(self.check("--strict")[1], 1)
        self.tearDown()
        self.edit_calc(replace={5: "        int sum = a + b;"})
        self.assertEqual(self.check("--strict")[1], 0)

    def test_fix_found_from_commit_message_alone_flags(self):
        path = os.path.join(self.repo.path, WEB_STR)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace("s.trim()", "s.strip()"))
        result, _ = self.check()
        self.assertEqual(self.changed(result), {"Strings.id": True})
        m = result["methods"][0]
        self.assertEqual(m["issues"], [])
        fix = m["fix_commits"][0]
        self.assertEqual((fix["sha"], fix["kind"], fix["author"], fix["confidence"]),
                         (self.repo.sha["c7"], "conventional", "Dave", 0.9091))

    def test_method_without_history_is_listed_but_not_flagged(self):
        self.edit_calc(replace={5: "        int sum = a + b;"})
        self.assertEqual(self.changed(self.check()[0]), {"Calculator.add": False})

    def test_signature_edit_counts(self):
        self.edit_calc(replace={9: "    public int sub(int a, int c) {"})
        self.assertEqual(self.changed(self.check()[0]), {"Calculator.sub": True})

    def test_new_method_after_a_flagged_one_flags_nothing(self):
        self.edit_calc(insert_after={12: ["", "    public int neg(int a) {", "        return -a;", "    }"]})
        self.assertEqual(self.check()[0]["methods"], [])

    def test_line_inserted_inside_a_method_counts(self):
        self.edit_calc(insert_after={10: ["        diff = diff + 0;"]})
        self.assertEqual(self.changed(self.check()[0]), {"Calculator.sub": True})

    def test_shifted_lines_still_map_to_the_right_method(self):
        # Three lines added at the top push sub's body from line 10 to 13 in the
        # working copy; only the old-side numbering still matches the graph.
        self.edit_calc(insert_after={1: ["// a", "// b", "// c"]},
                       replace={10: "        int diff = a - b;"})
        result = self.check()[0]
        self.assertEqual(self.changed(result), {"Calculator.sub": True})
        # ...and is reported where it now sits in the working tree: 9-11 -> 12-14.
        m = result["methods"][0]
        self.assertEqual((m["line"], m["line_end"], m["line_now"], m["line_end_now"]), (9, 11, 12, 14))

    def test_low_confidence_link_needs_a_lower_threshold(self):
        path = os.path.join(self.repo.path, CTRL)
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace("calc.add(x, 2)", "calc.add(x, 5)"))
        self.assertEqual(self.changed(self.check()[0]), {"Controller.handle": False})
        self.assertEqual(self.changed(self.check("--min-confidence", "0.01")[0]), {"Controller.handle": True})

    def test_staged_mode_ignores_unstaged_edits(self):
        self.edit_calc(replace={10: "        int diff = a - b;"})
        self.repo.git("add", CALC)
        self.edit_calc(replace={10: "        int diff = a - b;", 5: "        int sum = a + b;"})
        self.assertEqual(self.changed(self.check("--staged")[0]), {"Calculator.sub": True})
        self.assertEqual(set(self.changed(self.check()[0])), {"Calculator.sub", "Calculator.add"})

    # --- the same check through the MCP server, over the real stdio protocol ---

    def mcp(self, tool=None, arguments=None, env=None):
        """Starts lcb.mcp_server as an MCP client would; returns the tool list, or the
        text of one tool call."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "lcb.mcp_server"], cwd=self.tmp,
            env={**os.environ, **self.env, "LCB_REPO": self.repo.path, **(env or {})})

        async def go():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    if tool == "__instructions__":
                        return init.instructions
                    if tool is None:
                        return (await session.list_tools()).tools
                    result = await session.call_tool(tool, arguments or {})
                    return result.content[0].text

        return asyncio.run(go())

    def test_mcp_lists_check_changes_as_read_only(self):
        tools = {t.name: t for t in self.mcp()}
        self.assertEqual(set(tools), {"get_schema", "run_cypher", "check_changes"})
        self.assertTrue(tools["check_changes"].annotations.read_only_hint)

    def test_mcp_server_tells_the_assistant_when_to_use_it(self):
        text = self.mcp("__instructions__")
        self.assertIn("Prefer it over grep", text)
        self.assertIn("call check_changes before finishing", text)

    def test_mcp_get_schema_is_compact_unless_details_asked(self):
        compact = self.mcp("get_schema")
        detailed = self.mcp("get_schema", {"details": True})
        self.assertIn("CALLS{exact,opcode,line,via}", compact)
        self.assertNotIn("Spring dependency injection", compact)
        self.assertTrue(detailed.startswith(compact))
        self.assertIn("Spring dependency injection", detailed)

    def test_mcp_check_changes_flags_an_edited_method(self):
        self.edit_calc(replace={10: "        int diff = a - b;"})
        text = self.mcp("check_changes")
        self.assertIn("⚠ Calculator.sub", text)
        self.assertIn("linked to issue #1", text)

    def test_mcp_check_changes_takes_repo_and_mode_arguments(self):
        self.edit_calc(replace={5: "        int sum = a + b;"})
        # Nothing staged -> nothing to check; an explicit repo overrides LCB_REPO.
        self.assertIn("No changes", self.mcp("check_changes", {"staged": True}))
        text = self.mcp("check_changes", {"repo": self.repo.path, "show_all": True},
                        env={"LCB_REPO": self.tmp})
        self.assertIn("· Calculator.add", text)

    def test_mcp_check_changes_reports_errors_as_text(self):
        self.assertIn("ERROR: git failed", self.mcp("check_changes", {"repo": self.tmp}))
        self.assertIn("ERROR: pass staged or base", self.mcp("check_changes", {"staged": True, "base": "main"}))

    # --- the pre-commit hook, driven by real `git commit` runs ---

    def hook(self, *args, env=None):
        return run(sys.executable, "-m", "lcb.hook", *args, env={**self.env, **(env or {})})

    def install_hook(self, *flags, env=None):
        self.hook("install", "--repo", self.repo.path, *flags, env=env)
        self.addCleanup(lambda: os.path.exists(self.hook_path) and os.remove(self.hook_path))

    @property
    def hook_path(self):
        return os.path.join(self.repo.path, ".git", "hooks", "pre-commit")

    def git_commit(self, *flags):
        """Commits whatever is staged; returns (exit status, hook output). A commit
        that lands is undone again, so later tests see the original history."""
        head = self.repo.git("rev-parse", "HEAD").strip()
        proc = subprocess.run(
            ["git", "-C", self.repo.path, "commit", "-q", "-m", "wip", *flags],
            capture_output=True, text=True,
            env={**os.environ, **GIT_ENV, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example"})
        if self.repo.git("rev-parse", "HEAD").strip() != head:
            self.repo.git("reset", "-q", "--soft", head)
        return proc.returncode, proc.stdout + proc.stderr

    def stage_calc(self, **edit):
        self.edit_calc(**edit)
        self.repo.git("add", CALC)

    def test_hook_warns_but_lets_the_commit_through(self):
        self.install_hook()
        self.stage_calc(replace={10: "        int diff = a - b;"})
        code, out = self.git_commit()
        self.assertEqual(code, 0, out)
        self.assertIn("⚠ Calculator.sub", out)
        self.assertIn("linked to issue #1", out)

    def test_hook_is_silent_when_nothing_is_flagged(self):
        self.install_hook()
        self.stage_calc(replace={5: "        int sum = a + b;"})
        self.assertEqual(self.git_commit(), (0, ""))

    def test_hook_checks_only_what_is_staged(self):
        self.install_hook("--block")
        self.stage_calc(replace={5: "        int sum = a + b;"})
        self.edit_calc(replace={5: "        int sum = a + b;", 10: "        int diff = a - b;"})  # not staged
        self.assertEqual(self.git_commit(), (0, ""))

    def test_block_mode_stops_the_commit_and_no_verify_skips_it(self):
        self.install_hook("--block")
        self.stage_calc(replace={10: "        int diff = a - b;"})
        code, out = self.git_commit()
        self.assertEqual(code, 1)
        self.assertIn("commit stopped", out)
        self.assertEqual(self.git_commit("--no-verify")[0], 0)

    def test_hook_never_blocks_when_the_graph_is_down(self):
        self.install_hook("--block", env={"NEO4J_URI": "bolt://127.0.0.1:1"})
        self.stage_calc(replace={10: "        int diff = a - b;"})
        code, out = self.git_commit()
        self.assertEqual(code, 0, out)
        self.assertIn("change check skipped", out)

    def test_hook_leaves_a_foreign_hook_alone(self):
        with open(self.hook_path, "w") as fh:
            fh.write("#!/bin/sh\necho theirs\n")
        self.addCleanup(os.remove, self.hook_path)
        for cmd in ("install", "uninstall"):
            with self.assertRaises(AssertionError) as err:
                self.hook(cmd, "--repo", self.repo.path)
            self.assertIn("not written by lcb", str(err.exception))
        with open(self.hook_path) as fh:
            self.assertEqual(fh.read(), "#!/bin/sh\necho theirs\n")
        self.assertIn("lcb.check --repo", self.hook("print"))

    def test_hook_uninstall_removes_only_its_own(self):
        self.install_hook()
        self.assertIn("removed", self.hook("uninstall", "--repo", self.repo.path))
        self.assertFalse(os.path.exists(self.hook_path))
        self.assertIn("no pre-commit hook", self.hook("uninstall", "--repo", self.repo.path))

    def test_hook_follows_core_hooks_path(self):
        self.repo.git("config", "core.hooksPath", ".husky")
        self.addCleanup(self.repo.git, "config", "--unset", "core.hooksPath")
        self.hook("install", "--repo", self.repo.path)
        husky = os.path.join(self.repo.path, ".husky", "pre-commit")
        self.addCleanup(shutil.rmtree, os.path.join(self.repo.path, ".husky"))
        self.assertTrue(os.path.exists(husky))
        self.assertFalse(os.path.exists(self.hook_path))

    def test_z_note_when_history_is_older_than_head(self):
        self.assertIsNone(self.check()[0]["note"])
        self.repo.write("README.md", "docs\n")
        self.repo.commit("c9", BOB, "docs")
        self.assertIn("HEAD is", self.check()[0]["note"])


if __name__ == "__main__":
    unittest.main()
