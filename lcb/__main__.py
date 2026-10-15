"""The `lcb` command: one entry point for the tools a user runs.

    lcb setup <repo>           everything: ingest, then connect MCP, VS Code, git hook
    lcb ingest <repo>          build, extract, load, git + GitHub history
    lcb check [--repo R] ...   warn about changed methods that were fixed before
    lcb hook install ...       git pre-commit hook
    lcb mcp-config [--repo R]  how to register the MCP server
    lcb mcp                    run the MCP server (stdio)

Also runnable as `python -m lcb <command>`.
"""

import argparse
import json
import os
import sys

COMMANDS = {
    "setup": "lcb.setup",
    "ingest": "lcb.ingest",
    "check": "lcb.check",
    "hook": "lcb.hook",
}


def mcp_config(argv):
    from lcb.ingest import mcp_config as config
    ap = argparse.ArgumentParser(prog="lcb mcp-config")
    ap.add_argument("--repo", default=".", help="repository the check_changes tool looks at")
    args = ap.parse_args(argv)
    mcp_json, claude_cmd = config(os.path.realpath(args.repo))
    print("# Claude Code:\n" + claude_cmd + "\n\n# or .mcp.json:\n" + json.dumps(mcp_json, indent=2))
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if len(sys.argv) >= 2 else 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "mcp-config":
        return mcp_config(rest)
    if cmd == "mcp":
        from lcb.mcp_server import mcp
        mcp.run(transport="stdio")
        return 0
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n\n{__doc__.strip()}", file=sys.stderr)
        return 2
    module = __import__(COMMANDS[cmd], fromlist=["main"])
    sys.argv = [f"lcb {cmd}", *rest]
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
