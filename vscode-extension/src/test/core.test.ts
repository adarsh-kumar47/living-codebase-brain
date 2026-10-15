// Unit tests for core.ts, plus two runs of the real lcb.check through the same
// command() the extension uses -- so the Python <-> extension contract (arguments,
// PYTHONPATH, exit codes, JSON shape) is exercised, not just assumed.
//
//   npm test

import * as assert from "node:assert/strict";
import { execFileSync, execFile } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";

import {
  CheckResult, command, findings, message, MethodResult, parseOutcome, pythonFor, userConfig, userConfigPath, withDefaults,
} from "../core";

const PROJECT_ROOT = path.resolve(__dirname, "..", "..", "..");

function method(overrides: Partial<MethodResult> = {}): MethodResult {
  return {
    method: "OwnerController.processFindForm",
    path: "src/main/java/org/acme/OwnerController.java",
    line: 97, line_end: 120, line_now: 100, line_end_now: 123,
    flagged: true,
    issues: [],
    fix_commits: [{
      sha: "bb37aad0000000000000000000000000000000000", message: "fix: normalize whitespace in owner search",
      timestamp: 1784851200, kind: "conventional", confidence: 0.83, author: "Luiz", refs: [],
    }],
    callers: 0, last_author: "Luiz", risk_rank: 24, risk_of: 121,
    ...overrides,
  };
}

function result(methods: MethodResult[]): CheckResult {
  return { files_changed: ["x"], methods, note: null, min_confidence: 0.2 };
}

test("findings: only flagged methods, at their current line, 0-based", () => {
  const r = result([method(), method({ method: "Owner.getPet", flagged: false })]);
  const f = findings(r, "/repo");
  assert.equal(f.length, 1);
  assert.equal(f[0].file, path.join("/repo", "src/main/java/org/acme/OwnerController.java"));
  assert.equal(f[0].line, 99); // line_now 100, not the old-side line 97
});

test("message: strong links listed, weak ones counted", () => {
  const m = method({
    issues: [{ number: 2333, title: "Vets not added to model", confidence: 0.91, fixed_by: [2491] }],
    fix_commits: [
      method().fix_commits[0],
      { ...method().fix_commits[0], sha: "dd552f4", message: "Fix #108 owner update", confidence: 0.01,
        refs: [{ number: 108, title: "Updating owner creates a new one" }] },
    ],
  });
  const text = message(m, 0.2);
  assert.match(text, /^OwnerController.processFindForm has been fixed before/);
  assert.match(text, /Issue #2333 \(91%, fixed by PR #2491\): Vets not added to model/);
  assert.match(text, /bb37aad by Luiz, 2026-07-24 \(83%\): "fix: normalize whitespace in owner search"/);
  assert.doesNotMatch(text, /Fix #108/);
  assert.match(text, /\+1 weaker link\n/);
  assert.match(text, /0 callers · last changed by Luiz · risk rank 24 of 121$/);
});

test("parseOutcome: exit codes", () => {
  assert.equal(parseOutcome(0, JSON.stringify(result([])), "").kind, "ok");
  assert.deepEqual(parseOutcome(2, "", "lcb check: cannot reach the code graph at bolt://x (ServiceUnavailable)\n"),
                   { kind: "unavailable", reason: "cannot reach the code graph at bolt://x (ServiceUnavailable)" });
  assert.deepEqual(parseOutcome(1, "Traceback...\nValueError: boom", "Traceback (most recent call last):\nValueError: boom"),
                   { kind: "unavailable", reason: "ValueError: boom" });
  assert.equal(parseOutcome(null, "", "").kind, "unavailable");
});

test("command: PYTHONPATH, Neo4j URI and Python come from settings", () => {
  const settings = { projectRoot: "/lcb", pythonPath: "/py", minConfidence: 0.5, neo4jUri: "bolt://graph:7687" };
  const cmd = command(settings, "/repo", { PYTHONPATH: "/other", HOME: "/h" });
  assert.equal(cmd.file, "/py");
  assert.deepEqual(cmd.args, ["-m", "lcb.check", "--repo", "/repo", "--json", "--min-confidence", "0.5"]);
  assert.equal(cmd.env.PYTHONPATH, `/lcb${path.delimiter}/other`);
  assert.equal(cmd.env.NEO4J_URI, "bolt://graph:7687");
  assert.equal(cmd.env.HOME, "/h");
});

test("pythonFor: finds the checkout's virtualenv", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lcb-ext-"));
  fs.mkdirSync(path.join(root, ".venv", "bin"), { recursive: true });
  fs.writeFileSync(path.join(root, ".venv", "bin", "python"), "");
  const s = { projectRoot: root, pythonPath: "", minConfidence: 0.2, neo4jUri: "" };
  assert.equal(pythonFor(s), path.join(root, ".venv", "bin", "python"));
  assert.equal(pythonFor({ ...s, projectRoot: "/nowhere" }), process.platform === "win32" ? "python" : "python3");
  fs.rmSync(root, { recursive: true });
});

// --- the real lcb.check, through command() ---

function runReal(repo: string): Promise<ReturnType<typeof parseOutcome>> {
  const cmd = command({ projectRoot: PROJECT_ROOT, pythonPath: "", minConfidence: 0.2, neo4jUri: "" },
                      repo, process.env);
  return new Promise((resolve) => {
    execFile(cmd.file, cmd.args, { env: cmd.env }, (err, stdout, stderr) => {
      resolve(parseOutcome(err ? (typeof err.code === "number" ? err.code : null) : 0, stdout, stderr));
    });
  });
}

test("real lcb.check: a folder that is not a git repo is 'unavailable', with git's reason", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lcb-ext-"));
  const outcome = await runReal(dir);
  assert.equal(outcome.kind, "unavailable");
  assert.match((outcome as { reason: string }).reason, /not a git repository/);
  fs.rmSync(dir, { recursive: true });
});

test("real lcb.check: a clean repo is 'ok' with nothing to mark", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lcb-ext-"));
  const git = (...args: string[]) => execFileSync("git", ["-C", dir, ...args],
    { env: { ...process.env, GIT_CONFIG_GLOBAL: os.devNull, GIT_CONFIG_NOSYSTEM: "1" } });
  git("init", "-q");
  fs.writeFileSync(path.join(dir, "A.java"), "class A {}\n");
  git("add", "-A");
  git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init");
  const outcome = await runReal(dir);
  assert.equal(outcome.kind, "ok");
  assert.deepEqual(findings((outcome as { result: CheckResult }).result, dir), []);
  fs.rmSync(dir, { recursive: true });
});

test("user config from `lcb setup` fills empty settings; explicit settings win", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lcb-cfg-"));
  const file = path.join(dir, "config.json");
  fs.writeFileSync(file, JSON.stringify({ python: "/venv/bin/python", project_root: "/lcb", neo4j_uri: "bolt://g:7687" }));
  const empty = { projectRoot: "", pythonPath: "", minConfidence: 0.2, neo4jUri: "" };
  assert.deepEqual(withDefaults(empty, userConfig(file)),
                   { projectRoot: "/lcb", pythonPath: "/venv/bin/python", minConfidence: 0.2, neo4jUri: "bolt://g:7687" });
  assert.equal(withDefaults({ ...empty, projectRoot: "/mine" }, userConfig(file)).projectRoot, "/mine");
  assert.deepEqual(userConfig(path.join(dir, "missing.json")), {});
  assert.equal(userConfigPath({ LCB_CONFIG: file }, "/home/u"), file);
  assert.equal(userConfigPath({}, "/home/u"), path.join("/home/u", ".lcb", "config.json"));
  fs.rmSync(dir, { recursive: true });
});
