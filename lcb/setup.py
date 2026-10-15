"""One command to set everything up for a repository.

    lcb setup /path/to/repo [--hook warn|block|none] [--no-mcp] [--no-vscode] [ingest options]

1. **Ingest** the repository into the graph (`lcb ingest`; skip with --skip-ingest).
2. **Remember this install** in `~/.lcb/config.json` (Python, checkout, Neo4j address),
   so the VS Code extension needs no settings.
3. **MCP**: register the server for this repository in Claude Code
   (`claude mcp add --scope local`, run in the repository), or print the config for
   other clients.
4. **VS Code**: install the extension (`code --install-extension`), building the
   `.vsix` first if needed.
5. **Git hook**: install the pre-commit check if asked (`--hook`), or ask when run in a
   terminal.

Nothing is written into the repository except the optional git hook, which lives in
`.git/hooks` and is never committed. Machine-specific paths and the Neo4j password stay
in the user's own config, not in a file a team might commit.

Every step that cannot run (no `claude`, no `code`, no npm) is reported with what to do
instead; it never stops the others.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

from lcb.ingest import CODE_ROOT, PASSWORD, URI, USER, mcp_config

MCP_NAME = "living-codebase-brain"
VSCODE_CANDIDATES = (
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    os.path.expanduser("~/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
    "/usr/bin/code", "/usr/share/code/bin/code", "/snap/bin/code",
)


def user_config_path():
    return os.environ.get("LCB_CONFIG") or os.path.join(os.path.expanduser("~"), ".lcb", "config.json")


def find_cli(name, env_var, candidates=()):
    """An executable from an env override, the PATH, or known install locations. A set
    override is final -- even when it points nowhere, nothing else is tried -- so a
    test or a user can rule a tool out."""
    if env_var in os.environ:
        path = os.environ[env_var]
        return path if path and os.path.exists(path) else None
    for path in (shutil.which(name), *candidates):
        if path and os.path.exists(path):
            return path
    return None


# --- steps --------------------------------------------------------------------------


def write_user_config():
    path = user_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    config = {"python": sys.executable, "project_root": CODE_ROOT, "neo4j_uri": URI}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    return f"wrote {path}"


def register_mcp(repo_root):
    claude = find_cli("claude", "LCB_CLAUDE_BIN")
    mcp_json, claude_cmd = mcp_config(repo_root)
    if not claude:
        return False, ("Claude Code CLI not found. For Claude Code:\n      " + claude_cmd +
                       "\n    For other MCP clients, add to their config:\n" +
                       "\n".join("      " + ln for ln in json.dumps(mcp_json, indent=2).splitlines()))
    entry = mcp_json["mcpServers"][MCP_NAME]
    # Re-running setup must not fail on "already exists": replace the registration.
    subprocess.run([claude, "mcp", "remove", MCP_NAME, "--scope", "local"], cwd=repo_root,
                   capture_output=True, text=True)
    cmd = [claude, "mcp", "add", MCP_NAME, "--scope", "local"]
    for k, v in entry["env"].items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--", entry["command"], *entry["args"]]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, f"`claude mcp add` failed: {(proc.stderr or proc.stdout).strip()}\n      {claude_cmd}"
    return True, f"registered '{MCP_NAME}' in Claude Code for {repo_root} (run `claude` there)"


def find_or_build_vsix():
    if os.environ.get("LCB_VSIX"):
        return os.environ["LCB_VSIX"], None
    ext = os.path.join(CODE_ROOT, "vscode-extension")
    built = sorted(glob.glob(os.path.join(ext, "*.vsix")), key=os.path.getmtime)
    if built:
        return built[-1], None
    npm = shutil.which("npm")
    if not npm:
        return None, "no .vsix built and npm not found: install Node.js, then rerun"
    print("   building the VS Code extension (npm install && npm run package)", flush=True)
    for args in (["install", "--no-audit", "--no-fund"], ["run", "package"]):
        proc = subprocess.run([npm, *args], cwd=ext, capture_output=True, text=True)
        if proc.returncode != 0:
            return None, f"building the extension failed: {proc.stderr.strip()[-500:]}"
    built = sorted(glob.glob(os.path.join(ext, "*.vsix")), key=os.path.getmtime)
    return (built[-1], None) if built else (None, "npm run package produced no .vsix")


def install_vscode_extension():
    vsix, problem = find_or_build_vsix()
    if not vsix:
        return False, problem
    code = find_cli("code", "LCB_CODE_BIN", VSCODE_CANDIDATES)
    if not code:
        return False, (f"VS Code's `code` command not found. In VS Code: Extensions → … → "
                       f"Install from VSIX → {vsix}")
    proc = subprocess.run([code, "--install-extension", vsix, "--force"], capture_output=True, text=True)
    if proc.returncode != 0:
        return False, f"`code --install-extension` failed: {(proc.stderr or proc.stdout).strip()}"
    return True, f"installed {os.path.basename(vsix)} (reload VS Code windows that are already open)"


def choose_hook(flag):
    if flag:
        return flag
    if not sys.stdin.isatty():
        return "none"
    answer = input("\nInstall the git pre-commit check? [w]arn on flagged commits, "
                   "[b]lock them, or [n]o (default: warn): ").strip().lower()
    return {"b": "block", "block": "block", "n": "none", "no": "none"}.get(answer, "warn")


def install_hook(repo_root, mode):
    from lcb import hook
    try:
        path = hook.install(repo_root, block=(mode == "block"))
    except SystemExit as e:  # a hook lcb did not write: left alone, with the reason
        return False, str(e)
    return True, f"installed {path} ({'blocks' if mode == 'block' else 'warns on'} flagged commits)"


# --- entry point ----------------------------------------------------------------------


def setup(repo, ingest_args=(), skip_ingest=False, mcp=True, vscode=True, hook_mode=None):
    repo_root = os.path.realpath(repo)
    results = []

    if skip_ingest:
        results.append(("ingest", None, "skipped (--skip-ingest)"))
    else:
        from lcb import ingest
        ingest.main([repo_root, *ingest_args])
        results.append(("ingest", True, "graph loaded"))

    print("\n==> Connecting the tools", flush=True)
    results.append(("config", True, write_user_config()))
    results.append(("mcp", *(register_mcp(repo_root) if mcp else (None, "skipped (--no-mcp)"))))
    results.append(("vscode", *(install_vscode_extension() if vscode else (None, "skipped (--no-vscode)"))))
    mode = choose_hook(hook_mode)
    results.append(("git hook", *(install_hook(repo_root, mode) if mode != "none"
                                  else (None, f"not installed (add later: lcb hook install --repo {repo_root})"))))

    print("\n==> Setup summary")
    for name, ok, msg in results:
        mark = {True: "ok", False: "!!", None: "--"}[ok]
        print(f"   [{mark}] {name:<9} {msg}")
    print(f"\nOpen {repo_root} in VS Code: saving a changed method that was fixed before marks it.\n"
          f"Run `claude` in it: the AI can query the graph and check its own edits.")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lcb setup",
                                 description="Ingest a repository and connect MCP, VS Code and the git hook.")
    ap.add_argument("repo")
    ap.add_argument("--skip-ingest", action="store_true", help="the graph is already loaded")
    ap.add_argument("--hook", choices=["warn", "block", "none"],
                    help="git pre-commit check (default: ask in a terminal, else none)")
    ap.add_argument("--no-mcp", action="store_true", help="do not register the MCP server")
    ap.add_argument("--no-vscode", action="store_true", help="do not install the VS Code extension")
    args, ingest_args = ap.parse_known_args(argv)  # the rest goes to `lcb ingest`
    results = setup(args.repo, ingest_args, args.skip_ingest, not args.no_mcp, not args.no_vscode, args.hook)
    return 1 if any(ok is False for _, ok, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
