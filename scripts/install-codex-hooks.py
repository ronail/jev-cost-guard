#!/usr/bin/env python3
"""Declare the three jev gates in Codex's own hook config.

Codex does **not** discover hooks bundled in a plugin — `hooks/hooks.json` inside an
installed plugin is a Claude Code manifest. Codex reads `~/.codex/hooks.json` (and a
project `.codex/hooks.json`), so installing the plugin adds files but no hooks, and the
dashboard keeps showing however many events that file happened to declare. That is how a
user reinstalls, sees one hook, and concludes the plugin is broken.

This writes the missing events, merging rather than replacing: existing events and any
foreign hooks inside them are kept, and re-running is a no-op.

    python3 scripts/install-codex-hooks.py                 # ~/.codex/hooks.json
    python3 scripts/install-codex-hooks.py --dry-run
    python3 scripts/install-codex-hooks.py --path ./proj/.codex/hooks.json

Hooks are trusted by hash in `config.toml`. After this runs, approve them once in
`/hooks` (or run Codex with `--dangerously-bypass-hook-trust`); editing a command later
requires re-approval, so the commands here are deliberately stable — they point at one
script path per gate, with no version in the path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# event -> (matcher, script, extra env)
GATES = {
    "PreToolUse": ("Bash|shell|exec|run_command", "hooks/pretool-gate.py", "env JEV_GATE_HOST=codex "),
    "PostToolUse": ("Bash|exec|apply_patch|Edit|Write", "hooks/claude-codex-jev-verdict.py", ""),
    # Codex has no `ask` on PreToolUse and no transcript of its own shape; the gate maps
    # `ask` to a systemMessage there, which is why JEV_GATE_HOST is part of the command.
    "Stop": (None, "hooks/stop-gate.py", "env JEV_GATE_HOST=codex "),
}


def command_for(event: str) -> str:
    _matcher, script, prefix = GATES[event]
    return f"{prefix}python3 {os.path.join(REPO, script)}"


def entry_for(event: str, existing: list | None, repo: str) -> tuple[list, bool]:
    """The event's hook list with this repo's gate present exactly once."""
    matcher, _script, _prefix = GATES[event]
    wanted = command_for(event).replace(REPO, repo) if repo != REPO else command_for(event)
    entries = json.loads(json.dumps(existing or []))
    for entry in entries:
        for hook in entry.get("hooks") or []:
            if str(hook.get("command") or "").endswith(GATES[event][1]):
                if hook.get("command") == wanted:
                    return entries, False          # already declared, same form
                hook["command"] = wanted           # stale path: repoint, do not duplicate
                return entries, True
    new = {"hooks": [{"type": "command", "command": wanted, "timeout": 30}]}
    if matcher:
        new["matcher"] = matcher
    entries.append(new)
    return entries, True


def merge(config: dict, repo: str = REPO) -> tuple[dict, list[str]]:
    hooks = config.setdefault("hooks", {})
    changed: list[str] = []
    for event in GATES:
        entries, updated = entry_for(event, hooks.get(event), repo)
        hooks[event] = entries
        if updated:
            changed.append(event)
    config.setdefault(
        "description",
        "jev gates — PreToolUse risk gate, PostToolUse verdict, Stop completion gate. "
        "Audit-only until hooks/gate-policy.json sets mode=enforce.",
    )
    return config, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default=os.path.expanduser("~/.codex/hooks.json"))
    parser.add_argument("--repo", default=REPO, help="plugin checkout the hooks should point at")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        with open(args.path, encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        config = {}
    except json.JSONDecodeError as error:
        sys.exit(f"{args.path} is not valid JSON: {error}")

    config, changed = merge(config, args.repo)
    if not changed:
        print(f"{args.path}: all three gates already declared — nothing to do")
        return 0

    if args.dry_run:
        print(json.dumps(config, indent=2))
        return 0

    os.makedirs(os.path.dirname(args.path) or ".", exist_ok=True)
    with open(args.path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    print(f"{args.path}: declared {', '.join(changed)}")
    print("Approve them once in Codex (/hooks) — hooks are trusted by hash.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
