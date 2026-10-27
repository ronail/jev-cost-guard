#!/usr/bin/env python3
"""Guard the harness-agnostic invariant of this plugin's skills.

Every skill states *capabilities*, never another runtime's CLI as an unconditional
requirement. This check enforces the three properties that broke before:

1. each skill links the shared adapter table (docs/harness-adapters.md), so a
   concrete tool name is never invented inside a skill;
2. a line that names a specific runtime's CLI (`hermes …`, `codex …`, `claude …`)
   must be scoped — a fenced block or table row that says which harness it is for;
3. a scratch-dir reference must carry a portable fallback, so a skill is not
   wedged when the harness does not define that variable.

Exit 0 = clean, 1 = violations (printed). No dependencies.

    python3 scripts/check_harness_agnostic.py            # skills/ + cursor/
    python3 scripts/check_harness_agnostic.py --fix-hint
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_DOC = "harness-adapters.md"

CLI_RE = re.compile(r"(?<![\w/.\-`])(hermes|codex|claude|gemini)\s+(?:-{1,2}[A-Za-z][\w-]*|[a-z][\w-]*)")
# A scope marker: the line or its enclosing fenced block says which runtime it is for.
SCOPE_RE = re.compile(
    r"(?i)(hermes only|codex only|claude only|per harness|your own harness|your harness"
    r"|harness you are running|only when you are|current harness|\|\s*(hermes|codex|claude|claude code|other)\b)"
)
SCRATCH_RE = re.compile(r"BH_AGENT_WORKSPACE")
SCRATCH_OK_RE = re.compile(r"TMPDIR|/tmp|scratch")


def targets(extra: list[str]) -> list[str]:
    if extra:
        files: list[str] = []
        for item in extra:
            path = os.path.expanduser(item)
            if os.path.isdir(path):
                files += glob.glob(os.path.join(path, "*", "SKILL.md"))
                files += glob.glob(os.path.join(path, "**", "SKILL.md"), recursive=True)
                files += glob.glob(os.path.join(path, "*.mdc"))
            else:
                files += glob.glob(path, recursive=True)
        return sorted(set(files))
    files = glob.glob(os.path.join(REPO, "skills", "*", "SKILL.md"))
    files += glob.glob(os.path.join(REPO, "cursor", "*.mdc"))
    return sorted(files)


def check(path: str) -> list[str]:
    rel = os.path.relpath(path, REPO)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    problems: list[str] = []

    if ADAPTER_DOC not in text and os.path.basename(os.path.dirname(path)) != "cursor":
        problems.append(f"{rel}: no pointer to docs/{ADAPTER_DOC} (capability names must be resolved there)")

    in_fence = False
    fence_scoped = False
    block: list[str] = []

    def scope_for(index: int) -> bool:
        """Line, enclosing fence, or the 3 lines above carry a scope marker."""
        if SCOPE_RE.search(lines[index]):
            return True
        window = lines[max(0, index - 3): index + 1]
        return any(SCOPE_RE.search(w) for w in window)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_scoped = any(SCOPE_RE.search(w) for w in block[-3:])
                block = []
            else:
                in_fence = False
                fence_scoped = False
            continue
        if in_fence:
            block.append(line)
        if CLI_RE.search(line) and not (scope_for(i) or (in_fence and fence_scoped)):
            problems.append(f"{rel}:{i + 1}: names a specific runtime CLI without a harness scope: {stripped[:110]}")
        if SCRATCH_RE.search(line) and not SCRATCH_OK_RE.search(line):
            problems.append(f"{rel}:{i + 1}: scratch dir without a portable fallback: {stripped[:110]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="extra skill dirs / globs to check (default: this repo)")
    ap.add_argument("--fix-hint", action="store_true", help="print how to fix each violation class")
    args = ap.parse_args(argv)

    files = targets(args.paths)
    if not files:
        print("no skill files found", file=sys.stderr)
        return 2
    problems: list[str] = []
    for path in files:
        problems.extend(check(path))

    for p in problems:
        print(p)
    if problems and args.fix_hint:
        print(
            "\nFix:\n"
            "  * state the capability ('measure the fixed prompt'), then point at docs/"
            f"{ADAPTER_DOC}\n"
            "  * put a runtime CLI only in a block/row scoped to that runtime\n"
            "  * give every scratch-dir reference a fallback (TMPDIR or /tmp)"
        )
    print(f"\n{len(files)} skill files checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
