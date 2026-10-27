#!/usr/bin/env python3
"""Measure whether the jev skill-routing hook increases REAL jev MCP tool calls.

Two timestamped logs are joined:
  * hook decisions  -> settings.log_path      (one line per fresh turn)
  * MCP invocations -> JEV_CALL_LOG env var   (one line per real jev_* tool call)

The hook's own transport calls (`_systemone_post`) never pass through
`_handle_call`, so they are absent from the MCP log. That makes the MCP log a
clean "did the MODEL call a jev tool" signal — which is exactly the question.

Use it as an A/B:
  phase A  mode=shadow  (hook decides + logs, injects nothing)  -> control
  phase B  mode=live    (hook injects the advisory line)        -> treatment
Run comparable work in each phase, then:

  measure_hook_lift.py --hook <log> --mcp <log> [--window 180]

With both phases present it prints the LIFT; with one phase it prints the
absolute per-turn MCP-call rate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime

SKILL_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d) skill=(\S+) fits=([0-9.]+) mode=(\w+)")
NONE_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d) no skill matched \(mode=(\w+)\)")


def _lines(path: str):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as fh:
        yield from fh


def parse_hook(path: str):
    """[(datetime, mode, skill_or_None, fits_or_None)]"""
    turns = []
    for line in _lines(path):
        m = SKILL_RE.search(line)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
            turns.append((ts, m.group(4), m.group(2), float(m.group(3))))
            continue
        m = NONE_RE.search(line)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
            turns.append((ts, m.group(2), None, None))
    return turns


def parse_mcp(path: str):
    """[(datetime, tool)]"""
    calls = []
    for line in _lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            ts = datetime.strptime(d["iso"], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, KeyError, TypeError):
            continue
        calls.append((ts, d.get("tool")))
    return calls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hook", required=True, help="hook decision log (settings.log_path)")
    ap.add_argument("--mcp", required=True, help="MCP call log (JEV_CALL_LOG)")
    ap.add_argument("--window", type=int, default=180,
                    help="seconds after a turn in which a jev MCP call counts as its action")
    a = ap.parse_args()

    turns = parse_hook(a.hook)
    calls = parse_mcp(a.mcp)
    if not turns:
        print(f"no hook decisions in {a.hook} yet — run some turns (hook fires per fresh turn).")
        return

    per = defaultdict(lambda: {"turns": 0, "suggest": 0, "hit": 0, "tools": defaultdict(int)})
    for ts, mode, skill, _fits in turns:
        b = per[mode]
        b["turns"] += 1
        if skill:
            b["suggest"] += 1
        window_hits = [c for c in calls if 0 <= (c[0] - ts).total_seconds() <= a.window]
        if window_hits:
            b["hit"] += 1
            for _c_ts, tool in window_hits:
                b["tools"][tool] += 1

    print(f"window={a.window}s  total_mcp_calls={len(calls)}")
    for mode, b in sorted(per.items()):
        t = b["turns"] or 1
        print(f"[{mode}] turns={b['turns']} suggest={b['suggest']} "
              f"hit_rate={b['hit']/t:.2%}  tools={dict(b['tools'])}")

    if "shadow" in per and "live" in per:
        s, l = per["shadow"], per["live"]
        sr = s["hit"] / (s["turns"] or 1)
        lr = l["hit"] / (l["turns"] or 1)
        print(f"LIFT live-vs-shadow (all turns): {lr - sr:+.2%}")

    print("\nnote: for the sharpest test compare only skill-MATCHING turns "
          "(suggest>0) between shadow and live, on comparable work.")


if __name__ == "__main__":
    main()
