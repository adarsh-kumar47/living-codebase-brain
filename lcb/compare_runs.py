"""Compare real token usage between two Claude Code sessions.

The A/B demo: ask the SAME question twice, once in a session where the code-graph
MCP server is available and once in a session where it is not, then read the actual
token usage Claude Code recorded for each run.

This measures real usage reported by the API, not an estimate.

    # 1. see recent sessions and pick the two runs
    python -m lcb.compare_runs --list

    # 2. compare them
    python -m lcb.compare_runs --graph <session-id> --baseline <session-id>
"""

import argparse
import glob
import json
import os
from datetime import datetime

PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def transcripts():
    return glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))


def parse(path):
    """Pull per-assistant-turn usage and tool calls out of one transcript."""
    turns, tools, first_prompt, final_text = [], [], None, ""

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            if ev.get("type") == "user" and first_prompt is None:
                content = ev.get("message", {}).get("content")
                if isinstance(content, str):
                    first_prompt = content[:160]
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            first_prompt = b["text"][:160]
                            break

            if ev.get("type") != "assistant":
                continue

            msg = ev.get("message", {}) or {}
            usage = msg.get("usage") or {}
            if usage:
                turns.append(
                    {
                        "input": usage.get("input_tokens", 0),
                        "cache_create": usage.get("cache_creation_input_tokens", 0),
                        "cache_read": usage.get("cache_read_input_tokens", 0),
                        "output": usage.get("output_tokens", 0),
                    }
                )

            for b in msg.get("content", []) or []:
                if isinstance(b, dict):
                    if b.get("type") == "tool_use":
                        tools.append(b.get("name", "?"))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        final_text = b["text"]

    return {
        "path": path,
        "session": os.path.splitext(os.path.basename(path))[0],
        "project": os.path.basename(os.path.dirname(path)),
        "mtime": os.path.getmtime(path),
        "prompt": first_prompt or "",
        "turns": turns,
        "tools": tools,
        "final_text": final_text,
    }


def totals(run):
    t = run["turns"]
    ctx = sum(x["input"] + x["cache_create"] + x["cache_read"] for x in t)
    return {
        "turns": len(t),
        "context_in": ctx,
        "fresh_in": sum(x["input"] + x["cache_create"] for x in t),
        "cache_read": sum(x["cache_read"] for x in t),
        "output": sum(x["output"] for x in t),
        "total": ctx + sum(x["output"] for x in t),
        "tool_calls": len(run["tools"]),
    }


def find(session_id):
    for p in transcripts():
        if os.path.splitext(os.path.basename(p))[0].startswith(session_id):
            return p
    raise SystemExit(f"no transcript found for session id starting '{session_id}'")


def do_list(limit):
    runs = sorted((parse(p) for p in transcripts()), key=lambda r: -r["mtime"])
    print(f"\n{'session':<38}{'when':<18}{'turns':>6}{'tokens':>11}  prompt")
    print("-" * 110)
    for r in runs[:limit]:
        t = totals(r)
        when = datetime.fromtimestamp(r["mtime"]).strftime("%m-%d %H:%M")
        prompt = " ".join(r["prompt"].split())[:44]
        print(f"{r['session']:<38}{when:<18}{t['turns']:>6}{t['total']:>11,}  {prompt}")
    print()


def bar(value, peak, width=34):
    return "#" * max(1, int(width * value / peak)) if value else ""


def do_compare(graph_id, baseline_id):
    g, b = parse(find(graph_id)), parse(find(baseline_id))
    tg, tb = totals(g), totals(b)

    print("\n" + "=" * 68)
    print("  A/B: same question, with the code graph vs without")
    print("=" * 68)
    for label, r in (("GRAPH   ", g), ("BASELINE", b)):
        print(f"\n  {label}  {r['session'][:8]}  ({r['project']})")
        print(f"    prompt: {' '.join(r['prompt'].split())[:70]}")
        used = {}
        for t in r["tools"]:
            used[t] = used.get(t, 0) + 1
        print(f"    tools:  {', '.join(f'{k}x{v}' for k, v in used.items()) or '(none)'}")

    print("\n" + "-" * 68)
    print(f"  {'metric':<26}{'graph':>13}{'baseline':>13}{'saved':>13}")
    print("-" * 68)
    rows = [
        ("assistant turns", "turns"),
        ("tool calls", "tool_calls"),
        ("input (fresh)", "fresh_in"),
        ("input (cache read)", "cache_read"),
        ("input total", "context_in"),
        ("output", "output"),
        ("TOTAL TOKENS", "total"),
    ]
    for label, key in rows:
        gv, bv = tg[key], tb[key]
        saved = f"{100.0 * (bv - gv) / bv:.1f}%" if bv else "-"
        print(f"  {label:<26}{gv:>13,}{bv:>13,}{saved:>13}")
    print("-" * 68)

    if tg["total"]:
        print(f"\n  baseline costs {tb['total'] / tg['total']:.1f}x the graph run")

    # Per-turn growth: the baseline re-sends its accumulated transcript every turn,
    # so its context climbs with each file it reads. The graph run stays flat.
    print("\n  per-turn input tokens (context re-sent each turn)\n")
    peak = max(
        [x["input"] + x["cache_create"] + x["cache_read"] for x in g["turns"] + b["turns"]]
        or [1]
    )
    for label, r in (("graph   ", g), ("baseline", b)):
        print(f"  {label}")
        for i, x in enumerate(r["turns"], 1):
            v = x["input"] + x["cache_create"] + x["cache_read"]
            print(f"    turn {i:>2}  {v:>9,}  {bar(v, peak)}")
        print()

    for label, r in (("GRAPH ANSWER", g), ("BASELINE ANSWER", b)):
        print(f"  --- {label} ---")
        print("  " + (" ".join(r["final_text"].split())[:400] or "(none)"))
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--graph")
    ap.add_argument("--baseline")
    args = ap.parse_args()

    if args.list or not (args.graph and args.baseline):
        do_list(args.limit)
        if not (args.graph and args.baseline):
            print("pick two sessions:  python -m lcb.compare_runs --graph <id> --baseline <id>")
        return
    do_compare(args.graph, args.baseline)


if __name__ == "__main__":
    main()
