#!/usr/bin/env python3
"""Check a PR body against the branch it claims to describe.

Two defects shipped in this repo because nothing read the body back:

  1. a host matrix advertised as covering four hosts listed three (an edit spliced the
     Antigravity row away), and
  2. a Files section still said "47 tests" after the file had 80, and omitted two files
     the branch had added.

Both are mechanical, so this script checks them mechanically:

    python3 scripts/pr_body_check.py 37           # audit the live body of PR #37
    python3 scripts/pr_body_check.py 37 --push     # write corrected counts back to GitHub

What it verifies, and refuses to guess about:

  * every "`tests/x.py` | **N passed**" claim equals that file's collected count,
  * every "whole suite ... **N passed**" claim equals the collected total,
  * a body that shows any host-matrix row shows all four (Claude Code, Codex,
    Antigravity, Hermes) — a truncated matrix is a lie by omission,
  * phrases that were true earlier and are false now ("not remembered by default",
    "honorApprovals: false (shipped)", "is not live-verified") are refused.

It only reads: `gh pr view`, `git branch`, and pytest's collector. No network beyond `gh`,
no writes unless `--push` is passed, and it refuses to run when the checked-out branch is
not the PR's head — comparing a body to the wrong tree is how a wrong count gets blessed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

def _repo_root() -> Path:
    """The checkout this script audits — from git, not from the script's own location."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except Exception:  # pragma: no cover - only outside a checkout
        return Path(__file__).resolve().parents[1]


ROOT = _repo_root()

# A claim about one test file, e.g. "| `tests/test_pretool_gate.py` | **80 passed** — ..."
FILE_CLAIM = re.compile(r"`(tests/[\w./-]+\.py)`[^\n]*?\*\*(\d+) passed\*\*")
SUITE_CLAIM = re.compile(r"whole suite[^\n]*?\*\*(\d+) passed\*\*", re.IGNORECASE)
HOST_ROWS = ("Claude Code", "Codex", "Antigravity", "Hermes")
RETIRED = (
    "not remembered by default",
    "honorApprovals: false (shipped)",
    "is not live-verified",
    "per-session cache (1 h ttl)",
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, **kw)


def pr_meta(pr: str) -> dict:
    out = run(["gh", "pr", "view", pr, "--json", "title,body,headRefName,baseRefName,state"])
    if out.returncode != 0:
        sys.exit(f"gh pr view {pr} failed: {out.stderr.strip()}")
    return json.loads(out.stdout)


def collected(path: str | None = None) -> int:
    """Collected test count for one file, or the whole suite."""
    target = [path] if path else ["tests"]
    out = run([sys.executable, "-m", "pytest", *target, "--collect-only", "-q"])
    for line in reversed(out.stdout.strip().splitlines()):
        match = re.match(r"(\d+) tests? collected", line.strip())
        if match:
            return int(match.group(1))
    sys.exit(f"could not count tests for {target}: {out.stdout[-400:]}{out.stderr[-400:]}")


def audit(body: str) -> tuple[list[str], list[str], dict[str, int]]:
    """Returns (problems, fixed_body_lines, corrections) without touching the network."""
    problems: list[str] = []
    corrections: dict[str, int] = {}
    lines = body.splitlines()

    for index, line in enumerate(lines):
        for name, claimed in FILE_CLAIM.findall(line):
            actual = collected(name)
            if int(claimed) != actual:
                problems.append(f"line {index + 1}: {name} says {claimed} passed, branch has {actual}")
                corrections[f"{name}:{claimed}"] = actual
                lines[index] = line.replace(f"**{claimed} passed**", f"**{actual} passed**")

        suite = SUITE_CLAIM.search(line)
        if suite:
            actual = collected()
            if int(suite.group(1)) != actual:
                problems.append(f"line {index + 1}: whole suite says {suite.group(1)} passed, branch has {actual}")
                corrections["suite:" + suite.group(1)] = actual
                lines[index] = line.replace(f"**{suite.group(1)} passed**", f"**{actual} passed**")

        lowered = line.lower()
        for phrase in RETIRED:
            if phrase.lower() in lowered:
                problems.append(f"line {index + 1}: retired claim still present — {phrase!r}")

    present = [host for host in HOST_ROWS if f"| **{host}**" in body]
    if present and len(present) != len(HOST_ROWS):
        missing = [h for h in HOST_ROWS if h not in present]
        problems.append(f"host matrix lists {len(present)} of {len(HOST_ROWS)} hosts — missing {missing}")

    return problems, lines, corrections


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", help="PR number")
    parser.add_argument("--push", action="store_true", help="write corrected counts back to the PR body")
    args = parser.parse_args()

    meta = pr_meta(args.pr)
    branch = run(["git", "branch", "--show-current"]).stdout.strip()
    if branch != meta["headRefName"]:
        sys.exit(
            f"refusing to audit: PR #{args.pr} is {meta['headRefName']!r}, "
            f"this checkout is {branch!r}. Counts must come from the PR's own tree."
        )

    problems, lines, corrections = audit(meta["body"])
    print(f"PR #{args.pr} — {meta['title']}")
    print(f"  {meta['headRefName']} -> {meta['baseRefName']}  ({meta['state'].lower()})")

    if not problems:
        print("  body matches the branch: counts, host matrix and retired claims all clean")
        return 0

    print(f"  {len(problems)} problem(s):")
    for problem in problems:
        print(f"    - {problem}")

    if not args.push or not corrections:
        if problems and not corrections:
            print("  (nothing this script can rewrite — edit the prose by hand)")
        return 1

    fixed = "\n".join(lines) + "\n"
    edited = run(["gh", "pr", "edit", args.pr, "--body-file", "-"], input=fixed)
    if edited.returncode != 0:
        sys.exit(f"gh pr edit failed: {edited.stderr.strip()}")
    print(f"  pushed corrected counts: {corrections}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
