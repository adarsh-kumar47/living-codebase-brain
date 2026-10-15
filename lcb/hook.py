"""Git pre-commit hook that runs the change check on what is being committed.

    python -m lcb.hook install   --repo path/to/repo [--block]
    python -m lcb.hook uninstall --repo path/to/repo
    python -m lcb.hook print     [--block]     # the line to add to an existing hook

On `git commit`, the hook runs `lcb.check --staged --quiet`:

- nothing flagged: silent, the commit goes ahead;
- something flagged: the warning is printed, and the commit goes ahead (default) or
  is stopped (`--block`). `git commit --no-verify` always skips the hook;
- the check cannot run (Neo4j down, git error): one line saying it was skipped, and
  the commit goes ahead. A stopped database never stops anyone committing.

The hook is written to the directory git actually runs hooks from (honouring
`core.hooksPath`, as set by husky and similar tools). A pre-commit hook that this
module did not write is never overwritten: `install` refuses and `print` gives the
line to add to it instead.

The hook calls this checkout's Python and lcb package by absolute path, so it works
from any shell. NEO4J_URI, NEO4J_USER and LCB_HOME are baked in when set at install
time; the password is not written to disk -- it comes from NEO4J_PASSWORD at commit
time, or lcb's default.
"""

import argparse
import os
import shlex
import stat
import subprocess
import sys

MARKER = "# lcb-pre-commit-hook"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAKED_ENV = ("NEO4J_URI", "NEO4J_USER", "LCB_HOME")


def hooks_dir(repo):
    out = subprocess.run(["git", "-C", repo, "rev-parse", "--git-path", "hooks"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return out if os.path.isabs(out) else os.path.join(repo, out)


def check_command(block):
    """The shell lines that run the check and turn its exit status into the hook's."""
    q = shlex.quote
    lines = [
        f"LCB_PYTHON={q(sys.executable)}",
        f"PYTHONPATH={q(ROOT)}${{PYTHONPATH:+:$PYTHONPATH}}; export PYTHONPATH",
    ]
    for var in BAKED_ENV:
        if os.environ.get(var):
            lines.append(f"{var}=${{{var}:-{q(os.environ[var])}}}; export {var}")
    flags = "--staged --quiet" + (" --strict" if block else "")
    lines += [
        f'"$LCB_PYTHON" -m lcb.check --repo "$(git rev-parse --show-toplevel)" {flags} 1>&2',
        "lcb_status=$?",
        'if [ "$lcb_status" -eq 1 ]; then',
        '  echo "lcb: commit stopped -- check the past fixes above, or commit with --no-verify to skip." >&2',
        "  exit 1",
        "fi",
        'if [ "$lcb_status" -ne 0 ]; then',
        '  echo "lcb: change check skipped (exit $lcb_status); committing anyway." >&2',
        "fi",
    ]
    return "\n".join(lines)


def script(block):
    mode = "stops the commit" if block else "warns"
    return "\n".join([
        "#!/bin/sh",
        f"{MARKER} -- installed by `python -m lcb.hook install`; remove with `python -m lcb.hook uninstall`.",
        f"# Runs the Living Codebase Brain change check on staged changes and {mode} when they",
        "# touch methods with a bug history. `git commit --no-verify` skips it.",
        check_command(block),
        "exit 0",
        "",
    ])


def is_ours(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            return MARKER in fh.read()
    except FileNotFoundError:
        return False


def install(repo, block=False):
    hooks = hooks_dir(repo)
    path = os.path.join(hooks, "pre-commit")
    if os.path.exists(path) and not is_ours(path):
        raise SystemExit(
            f"{path} already exists and was not written by lcb; leaving it alone.\n"
            f"To run the check from it, add the output of `python -m lcb.hook print"
            f"{' --block' if block else ''}` to that hook.")
    os.makedirs(hooks, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(script(block))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def uninstall(repo):
    path = os.path.join(hooks_dir(repo), "pre-commit")
    if not os.path.exists(path):
        return None
    if not is_ours(path):
        raise SystemExit(f"{path} was not written by lcb; leaving it alone.")
    os.remove(path)
    return path


def main():
    ap = argparse.ArgumentParser(description="Install the lcb change check as a git pre-commit hook.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".", help="git repository (default: current directory)")
        if name == "install":
            p.add_argument("--block", action="store_true",
                           help="stop the commit when something is flagged (default: warn only)")
    p = sub.add_parser("print", help="print the lines to add to an existing pre-commit hook")
    p.add_argument("--block", action="store_true")
    args = ap.parse_args()

    if args.cmd == "print":
        print(check_command(args.block))
        return 0
    try:
        if args.cmd == "install":
            path = install(args.repo, args.block)
            print(f"installed {path} ({'blocks' if args.block else 'warns'} on flagged commits)")
        else:
            path = uninstall(args.repo)
            print(f"removed {path}" if path else "no pre-commit hook installed")
    except subprocess.CalledProcessError as e:
        print(f"git failed: {(e.stderr or '').strip()}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
