// Everything the extension decides, without the vscode API: which command to run,
// how to read its result, and what to mark where. extension.ts only wires this to
// the editor, so this file is what the unit tests cover.

import * as fs from "fs";
import * as path from "path";

export interface Issue {
  number: number;
  title: string;
  confidence: number | null;
  fixed_by: number[];
}

export interface FixCommit {
  sha: string;
  message: string;
  timestamp: number;
  kind: string;
  confidence: number | null;
  author: string | null;
  refs: { number: number; title: string }[];
}

export interface MethodResult {
  method: string;
  path: string;
  line: number;
  line_end: number;
  line_now: number;
  line_end_now: number;
  flagged: boolean;
  issues: Issue[];
  fix_commits: FixCommit[];
  callers: number;
  last_author: string | null;
  risk_rank: number | null;
  risk_of: number | null;
}

export interface CheckResult {
  files_changed: string[];
  methods: MethodResult[];
  note: string | null;
  min_confidence: number;
}

export interface Settings {
  projectRoot: string;
  pythonPath: string;
  minConfidence: number;
  neo4jUri: string;
}

/** `lcb setup` records the install in ~/.lcb/config.json (or $LCB_CONFIG), so the
 *  extension works with no settings at all. Unreadable or missing: empty. */
export function userConfig(file: string): { python?: string; project_root?: string; neo4j_uri?: string } {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return {};
  }
}

export function userConfigPath(env: NodeJS.ProcessEnv, home: string): string {
  return env.LCB_CONFIG || path.join(home, ".lcb", "config.json");
}

/** Explicit VS Code settings win; anything left empty comes from the user config. */
export function withDefaults(s: Settings, cfg: ReturnType<typeof userConfig>): Settings {
  return {
    ...s,
    projectRoot: s.projectRoot || cfg.project_root || "",
    pythonPath: s.pythonPath || cfg.python || "",
    neo4jUri: s.neo4jUri || cfg.neo4j_uri || "",
  };
}

export type Outcome =
  | { kind: "ok"; result: CheckResult }
  | { kind: "unavailable"; reason: string };

/** A method to mark: its file, its signature line in the current buffer (0-based),
 *  and the text shown on hover and in the Problems panel. */
export interface Finding {
  file: string;
  line: number;
  method: string;
  message: string;
}

export function pythonFor(settings: Settings): string {
  if (settings.pythonPath) {
    return settings.pythonPath;
  }
  const candidates = [
    ".venv-mac/bin/python",
    ".venv/bin/python",
    ".venv/Scripts/python.exe",
  ];
  for (const rel of candidates) {
    const p = path.join(settings.projectRoot, rel);
    if (settings.projectRoot && fs.existsSync(p)) {
      return p;
    }
  }
  return process.platform === "win32" ? "python" : "python3";
}

export function command(settings: Settings, repo: string, baseEnv: NodeJS.ProcessEnv) {
  const args = ["-m", "lcb.check", "--repo", repo, "--json",
                "--min-confidence", String(settings.minConfidence)];
  const env: NodeJS.ProcessEnv = { ...baseEnv };
  if (settings.projectRoot) {
    env.PYTHONPATH = baseEnv.PYTHONPATH
      ? settings.projectRoot + path.delimiter + baseEnv.PYTHONPATH
      : settings.projectRoot;
  }
  if (settings.neo4jUri) {
    env.NEO4J_URI = settings.neo4jUri;
  }
  return { file: pythonFor(settings), args, env };
}

/** lcb.check exit codes: 0 checked, 1 flagged under --strict (not used here),
 *  2 could not check. Anything else, or unparseable output, is also "unavailable". */
export function parseOutcome(code: number | null, stdout: string, stderr: string): Outcome {
  if (code === 0 || code === 1) {
    try {
      return { kind: "ok", result: JSON.parse(stdout) as CheckResult };
    } catch {
      return { kind: "unavailable", reason: firstLine(stderr) || "unexpected output from lcb.check" };
    }
  }
  return { kind: "unavailable", reason: firstLine(stderr) || `lcb.check exited with ${code}` };
}

function firstLine(text: string): string {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  // A Python traceback ends with the exception, not starts with it.
  return lines.length ? lines[lines.length - 1].replace(/^lcb check:\s*/, "") : "";
}

function pct(c: number | null): string {
  return `${Math.round((c ?? 0) * 100)}%`;
}

function day(ts: number): string {
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

export function message(m: MethodResult, minConfidence: number): string {
  const strong = (c: number | null) => (c ?? 0) >= minConfidence;
  const issues = m.issues.filter((i) => strong(i.confidence));
  const commits = m.fix_commits.filter((c) => strong(c.confidence));
  const lines = [`${m.method} has been fixed before — check this change doesn't undo it.`];
  for (const i of issues) {
    const by = i.fixed_by.length ? `PR ${i.fixed_by.map((n) => `#${n}`).join(", ")}` : "no PR";
    lines.push(`• Issue #${i.number} (${pct(i.confidence)}, fixed by ${by}): ${i.title}`);
  }
  for (const c of commits) {
    const refs = c.refs.map((r) => ` [issue #${r.number}: ${r.title}]`).join("");
    lines.push(`• ${c.sha.slice(0, 7)} by ${c.author ?? "unknown"}, ${day(c.timestamp)} ` +
               `(${pct(c.confidence)}): "${c.message}"${refs}`);
  }
  const weaker = m.issues.length + m.fix_commits.length - issues.length - commits.length;
  if (weaker > 0) {
    lines.push(`• +${weaker} weaker link${weaker === 1 ? "" : "s"}`);
  }
  const facts = [`${m.callers} caller${m.callers === 1 ? "" : "s"}`];
  if (m.last_author) {
    facts.push(`last changed by ${m.last_author}`);
  }
  if (m.risk_rank) {
    facts.push(`risk rank ${m.risk_rank} of ${m.risk_of}`);
  }
  lines.push(facts.join(" · "));
  return lines.join("\n");
}

/** Flagged methods only, keyed to the file as it is now (line_now, 1-based in the
 *  result, 0-based here). */
export function findings(result: CheckResult, repo: string): Finding[] {
  return result.methods
    .filter((m) => m.flagged)
    .map((m) => ({
      file: path.join(repo, m.path),
      line: Math.max(m.line_now - 1, 0),
      method: m.method,
      message: message(m, result.min_confidence),
    }));
}
