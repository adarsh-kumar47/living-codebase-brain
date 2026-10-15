// Runs `lcb.check` when Java/Kotlin/Groovy/Scala files are saved, and marks the
// changed methods that have been fixed before. The decisions live in core.ts.

import { execFile } from "child_process";
import * as os from "os";
import * as vscode from "vscode";

import { command, findings, parseOutcome, Settings, userConfig, userConfigPath, withDefaults } from "./core";

const SOURCE_LANGUAGES = new Set(["java", "kotlin", "groovy", "scala"]);
const SEVERITY: Record<string, vscode.DiagnosticSeverity> = {
  error: vscode.DiagnosticSeverity.Error,
  warning: vscode.DiagnosticSeverity.Warning,
  information: vscode.DiagnosticSeverity.Information,
};

let diagnostics: vscode.DiagnosticCollection;
let status: vscode.StatusBarItem;
let running = false;
let rerun = false;
let timer: NodeJS.Timeout | undefined;

export function activate(context: vscode.ExtensionContext) {
  diagnostics = vscode.languages.createDiagnosticCollection("lcb");
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  status.command = "lcb.checkNow";
  context.subscriptions.push(
    diagnostics,
    status,
    vscode.commands.registerCommand("lcb.checkNow", () => checkAll()),
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (SOURCE_LANGUAGES.has(doc.languageId)) {
        schedule();
      }
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("lcb")) {
        schedule();
      }
    }),
  );
  schedule();
}

export function deactivate() {
  if (timer) {
    clearTimeout(timer);
  }
}

function settings(): Settings & { repo: string; severity: string } {
  const c = vscode.workspace.getConfiguration("lcb");
  const base = withDefaults({
    projectRoot: c.get<string>("projectRoot", ""),
    pythonPath: c.get<string>("pythonPath", ""),
    minConfidence: c.get<number>("minConfidence", 0.2),
    neo4jUri: c.get<string>("neo4jUri", ""),
  }, userConfig(userConfigPath(process.env, os.homedir())));
  return { ...base, repo: c.get<string>("repo", ""), severity: c.get<string>("severity", "warning") };
}

function schedule() {
  if (timer) {
    clearTimeout(timer);
  }
  timer = setTimeout(() => checkAll(), 400);
}

async function checkAll() {
  if (running) {
    rerun = true; // a save landed mid-check: run once more afterwards
    return;
  }
  running = true;
  try {
    const s = settings();
    const repos = s.repo ? [s.repo] : (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
    status.text = "$(sync~spin) LCB";
    status.show();

    const next = new Map<string, vscode.Diagnostic[]>();
    const problems: string[] = [];
    for (const repo of repos) {
      const outcome = await runCheck(s, repo);
      if (outcome.kind === "unavailable") {
        problems.push(`${repo}: ${outcome.reason}`);
        continue;
      }
      for (const f of findings(outcome.result, repo)) {
        const d = new vscode.Diagnostic(new vscode.Range(f.line, 0, f.line, Number.MAX_SAFE_INTEGER),
                                        f.message, SEVERITY[s.severity] ?? vscode.DiagnosticSeverity.Warning);
        d.source = "Living Codebase Brain";
        d.code = "fixed-before";
        next.set(f.file, [...(next.get(f.file) ?? []), d]);
      }
    }

    diagnostics.clear();
    for (const [file, ds] of next) {
      diagnostics.set(vscode.Uri.file(file), ds);
    }
    const marked = [...next.values()].reduce((n, ds) => n + ds.length, 0);
    if (problems.length && !marked) {
      status.text = "$(debug-disconnect) LCB";
      status.tooltip = `Change check could not run:\n${problems.join("\n")}`;
    } else {
      status.text = marked ? `$(warning) LCB ${marked}` : "$(check) LCB";
      status.tooltip = (marked
        ? `${marked} changed method${marked === 1 ? "" : "s"} fixed before — see Problems`
        : "No changed method has been fixed before") +
        (problems.length ? `\nCould not check:\n${problems.join("\n")}` : "") +
        "\nClick to check again";
    }
  } finally {
    running = false;
    if (rerun) {
      rerun = false;
      schedule();
    }
  }
}

function runCheck(s: Settings, repo: string) {
  const cmd = command(s, repo, process.env);
  return new Promise<ReturnType<typeof parseOutcome>>((resolve) => {
    execFile(cmd.file, cmd.args, { env: cmd.env, timeout: 60_000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => {
        const code = err ? (typeof err.code === "number" ? err.code : null) : 0;
        const reason = err && typeof err.code !== "number" ? `${err.message}` : stderr;
        resolve(parseOutcome(code, stdout, reason));
      });
  });
}
