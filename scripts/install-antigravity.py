#!/usr/bin/env python3
"""Wire this checkout into an Antigravity *workspace* (no plugin install needed).

The plugin route (`agy plugin install .`) is the default. This script is the other
use case: one repo, no plugin — the same server and gate declared in the
workspace-scoped files Antigravity reads:

    <workspace>/.agents/mcp_config.json   MCP servers
    <workspace>/.agents/hooks.json        lifecycle hooks

Both are merged, never replaced: other servers and hook groups in those files are
kept. Paths are absolute because `${PLUGIN_ROOT}` only resolves inside an installed
plugin.

    python3 scripts/install-antigravity.py                  # cwd as the workspace
    python3 scripts/install-antigravity.py --workspace ~/code/app
    python3 scripts/install-antigravity.py --no-hook        # servers only
    python3 scripts/install-antigravity.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "mcp", "jev", "server.py")
HOOK = os.path.join(REPO, "hooks", "antigravity-jev-verdict.py")

MCP_FILE = "mcp_config.json"
HOOK_FILE = "hooks.json"
HOOK_GROUP = "jev-verdict"


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"refusing to overwrite {path}: not readable JSON ({exc})")
    if not isinstance(data, dict):
        raise SystemExit(f"refusing to overwrite {path}: expected a JSON object")
    return data


def build_mcp(existing: dict) -> dict:
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["jev"] = {"command": "python3", "args": [SERVER]}
    existing["mcpServers"] = servers
    return existing


def build_hooks(existing: dict) -> dict:
    group = existing.get(HOOK_GROUP)
    if not isinstance(group, dict):
        group = {}
    group["PreInvocation"] = [
        {
            "type": "command",
            "command": f'python3 "{HOOK}"',
            "timeout": 30,
        }
    ]
    existing[HOOK_GROUP] = group
    return existing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write Antigravity workspace config for this checkout.")
    ap.add_argument("--workspace", default=os.getcwd(), help="workspace root (default: cwd)")
    ap.add_argument("--no-hook", action="store_true", help="declare the MCP server only")
    ap.add_argument("--dry-run", action="store_true", help="print what would be written")
    args = ap.parse_args(argv)

    if not os.path.isfile(SERVER):
        raise SystemExit(f"missing server: {SERVER}")
    if not args.no_hook and not os.path.isfile(HOOK):
        raise SystemExit(f"missing hook: {HOOK}")

    agents = os.path.join(os.path.abspath(os.path.expanduser(args.workspace)), ".agents")
    os.makedirs(agents, exist_ok=True)

    writes = {"mcp": (os.path.join(agents, MCP_FILE), build_mcp(_load(os.path.join(agents, MCP_FILE))))}
    if not args.no_hook:
        writes["hook"] = (
            os.path.join(agents, HOOK_FILE),
            build_hooks(_load(os.path.join(agents, HOOK_FILE))),
        )

    for label, (path, payload) in writes.items():
        text = json.dumps(payload, indent=2) + "\n"
        if args.dry_run:
            print(f"# {label} -> {path}\n{text}")
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {path}")

    print(
        "\nNext: open the workspace in Antigravity (or run `agy` in it). MCP servers and\n"
        "hooks are read at session start, so start a new session. Credentials come from\n"
        f"the environment or the server's own store: python3 {SERVER} --set-key --path <file>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
