"""Tests for `lcb setup` (lcb/setup.py).

`claude` and `code` are replaced by fake programs that only record how they were
called, so nothing is registered in the real Claude Code or installed into the real
VS Code. LCB_CLAUDE_BIN / LCB_CODE_BIN pointing nowhere stands for "not installed".
The graph step is skipped (--skip-ingest); `lcb ingest` has its own tests.

    PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_setup -v
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.test_git_layer import ALICE, ROOT, FakeRepo

FAKE = """#!/bin/sh
echo "$PWD|$*" >> "{log}"
"""


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lcb-setup-")
        self.repo = FakeRepo(os.path.join(self.tmp, "shop"))
        self.repo.write("A.java", "class A {}\n")
        self.repo.commit("a1", ALICE, "init")
        self.log = os.path.join(self.tmp, "calls.log")
        self.bins = {name: self.fake(name) for name in ("claude", "code")}
        self.vsix = os.path.join(self.tmp, "living-codebase-brain-0.1.0.vsix")
        open(self.vsix, "w").close()
        self.config = os.path.join(self.tmp, "home", ".lcb", "config.json")
        self.env = {"PYTHONPATH": ROOT, "LCB_CONFIG": self.config, "LCB_VSIX": self.vsix,
                    "LCB_CLAUDE_BIN": self.bins["claude"], "LCB_CODE_BIN": self.bins["code"],
                    "NEO4J_URI": "bolt://graph.example:7687"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, name):
        path = os.path.join(self.tmp, "bin", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(FAKE.format(log=self.log))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def setup_cmd(self, *flags, env=None):
        return subprocess.run([sys.executable, "-m", "lcb", "setup", self.repo.path, "--skip-ingest", *flags],
                              cwd=self.tmp, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              env={**os.environ, **self.env, **(env or {})})

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln.rstrip("\n").split("|", 1) for ln in fh]

    @property
    def hook_path(self):
        return os.path.join(self.repo.path, ".git", "hooks", "pre-commit")

    def test_connects_everything(self):
        proc = self.setup_cmd("--hook", "warn")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        repo = os.path.realpath(self.repo.path)

        with open(self.config) as fh:
            config = json.load(fh)
        self.assertEqual(config["python"], sys.executable)
        self.assertEqual(config["neo4j_uri"], "bolt://graph.example:7687")
        self.assertTrue(os.path.isdir(os.path.join(config["project_root"], "lcb")))

        (rm_cwd, rm), (add_cwd, add), (_, install) = self.calls()
        self.assertEqual(rm, "mcp remove living-codebase-brain --scope local")
        self.assertEqual(os.path.realpath(add_cwd), repo)  # local scope = this repository
        self.assertTrue(add.startswith("mcp add living-codebase-brain --scope local -e "))
        self.assertIn(f"-e LCB_REPO={repo}", add)
        self.assertIn("-e NEO4J_URI=bolt://graph.example:7687", add)
        self.assertTrue(add.endswith(f"-- {sys.executable} -m lcb.mcp_server"))
        self.assertEqual(install, f"--install-extension {self.vsix} --force")

        self.assertTrue(os.path.exists(self.hook_path))
        self.assertIn("[ok] git hook", proc.stdout)
        # Nothing machine-specific is written into the repository itself.
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_no_hook_unless_asked_when_not_in_a_terminal(self):
        proc = self.setup_cmd()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.hook_path))
        self.assertIn("[--] git hook  not installed", proc.stdout)

    def test_missing_tools_print_what_to_do_instead(self):
        proc = self.setup_cmd("--hook", "none", env={"LCB_CLAUDE_BIN": "", "LCB_CODE_BIN": ""})
        self.assertEqual(proc.returncode, 1)  # something could not be done: say so
        self.assertEqual(self.calls(), [])
        self.assertIn("[!!] mcp", proc.stdout)
        self.assertIn("claude mcp add living-codebase-brain", proc.stdout)
        self.assertIn('"mcpServers"', proc.stdout)
        self.assertIn("[!!] vscode", proc.stdout)
        self.assertIn(f"Install from VSIX → {self.vsix}", proc.stdout)
        self.assertTrue(os.path.exists(self.config))  # the rest still happened

    def test_skips_and_foreign_hook(self):
        os.makedirs(os.path.dirname(self.hook_path), exist_ok=True)
        with open(self.hook_path, "w") as fh:
            fh.write("#!/bin/sh\necho theirs\n")
        proc = self.setup_cmd("--no-mcp", "--no-vscode", "--hook", "block")
        self.assertEqual(self.calls(), [])
        self.assertIn("[--] mcp       skipped (--no-mcp)", proc.stdout)
        self.assertIn("[!!] git hook  ", proc.stdout)
        self.assertIn("not written by lcb", proc.stdout)
        with open(self.hook_path) as fh:
            self.assertEqual(fh.read(), "#!/bin/sh\necho theirs\n")

    def test_rerun_replaces_rather_than_fails(self):
        self.setup_cmd("--hook", "warn")
        proc = self.setup_cmd("--hook", "warn")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([c[1].split()[1] for c in self.calls() if c[1].startswith("mcp")],
                         ["remove", "add", "remove", "add"])


if __name__ == "__main__":
    unittest.main()
