#!/usr/bin/env python3
"""One-command installer: register jev-cost-guard as a Claude plugin.

Writes the jev MCP server + the PostToolUse soft-gate into your
`claude_desktop_config.json` (in place, backing up the old file). It resolves the
repo clone path itself and keeps the plugin self-contained: the jev key comes from
`--key`, `--env-file`, this repo's own `.env`, or the shell env — never from a
Hermes profile. With no key it runs keyless on the Zen free tier.

Usage:
    python3 scripts/install-claude-plugin.py                 # keyless (Zen free tier)
    python3 scripts/install-claude-plugin.py --key ts_xxx    # TypeSafe key
    python3 scripts/install-claude-plugin.py --key xxx --provider opencode
    python3 scripts/install-claude-plugin.py --env-file /path/to/.env
    python3 scripts/install-claude-plugin.py --config /path/claude_desktop_config.json
    python3 scripts/install-claude-plugin.py --dry-run
    python3 scripts/install-claude-plugin.py --uninstall

The MCP server (`mcp/jev/server.py`) runs keyless with the OpenCode Zen free tier
(`Bearer public`, model `jev-1.13-free`) if no key is supplied — see README §224.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVER_ARGS = ["mcp/jev/server.py"]           # resolved under repo root by Claude Desktop CWD? no — made absolute below
HOOK_ARGS = ["hooks/claude-codex-jev-verdict.py"]


def _redact(s: str) -> str:
    if not s:
        return ""
    return s[:4] + "*" * max(0, len(s) - 8) + s[-4:] if len(s) > 8 else "*" * len(s)


def load_env(env_file: Path | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if env_file and env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def key_for_claude(jev_env: dict[str, str]) -> dict[str, str]:
    """Pick the auth env to expose to the Claude Desktop process."""
    out: dict[str, str] = {}
    if jev_env.get("TYPESAFE_API_KEY"):
        out["TYPESAFE_API_KEY"] = jev_env["TYPESAFE_API_KEY"]
    elif jev_env.get("OPENCODE_ZEN_API_KEY"):
        out["OPENCODE_ZEN_API_KEY"] = jev_env["OPENCODE_ZEN_API_KEY"]
        out.setdefault("JEV_MODEL", "jev-1.13-free")
    # keyless -> env stays {} so the server runs on the Zen free tier (Bearer public)
    return out


def config_path() -> Path:
    p = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if p:
        return Path(p)
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude" / "claude_desktop_config.json"
    # linux / other
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "claude-desktop" / "claude_desktop_config.json"


def abs_args(rel: list[str]) -> list[str]:
    return [str((REPO_ROOT / p).resolve()) for p in rel]


def merge_jev(cfg: dict, jev_env: dict, *, uninstall: bool) -> dict:
    """Idempotently set/replace the jev MCP server + PostToolUse gate."""
    cfg.setdefault("mcpServers", {})
    cfg.setdefault("hooks", {})

    if uninstall:
        cfg["mcpServers"].pop("jev", None)
        cfg["hooks"].setdefault("PostToolUse", [])
        cfg["hooks"]["PostToolUse"] = [
            h for h in cfg["hooks"]["PostToolUse"]
            if not (isinstance(h, dict) and h.get("args", []) == HOOK_ARGS) and h.get("command") != "python3"
        ]
        cfg["hooks"]["PostToolUse"] = [
            h for h in cfg["hooks"]["PostToolUse"]
            if not (isinstance(h, dict) and h.get("args", []) == abs_args(HOOK_ARGS))
        ]
        cfg["hooks"].setdefault("PostToolUse", [])
        cfg["hooks"]["PostToolUse"] = [h for h in cfg["hooks"]["PostToolUse"] if abs_args(HOOK_ARGS) not in (h.get("args"),)]
        cfg["hooks"]["PostToolUse"] = [h for h in cfg["hooks"]["PostToolUse"] if h.get("args") != abs_args(HOOK_ARGS)]
        return cfg

    auth = key_for_claude(jev_env)
    # build jev server entry
    server_env = dict(auth)
    cfg["mcpServers"]["jev"] = {
        "command": sys.executable or "python3",
        "args": abs_args(SERVER_ARGS),
        "env": server_env,
    }

    hook_env = dict(server_env)
    hook_env.setdefault("JEV_VERDICT_THRESHOLD_CHARS", "4000")
    hook_abs_args = abs_args(HOOK_ARGS)

    hooks = cfg["hooks"].setdefault("PostToolUse", [])
    # remove any prior jev gate entry (match by args), then append the fresh one
    hooks[:] = [h for h in hooks if h.get("args") != hook_abs_args]
    hooks.append({"command": sys.executable or "python3", "args": hook_abs_args, "env": hook_env})

    return cfg


def _key_var(key: str, provider: str | None) -> str:
    """Map a raw API key to the env var name it belongs in."""
    if provider == "typesafe":
        return "TYPESAFE_API_KEY"
    if provider == "opencode":
        return "OPENCODE_ZEN_API_KEY"
    return "TYPESAFE_API_KEY" if key.startswith("ts_") else "OPENCODE_ZEN_API_KEY"


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Register jev-cost-guard as a Claude plugin.")
    ap.add_argument("--key", default=None, help="jev provider API key (TypeSafe or OpenCode Zen).")
    ap.add_argument("--provider", choices=("typesafe", "opencode"), default=None,
                    help="Which provider --key belongs to (default: inferred; ts_ prefix = typesafe).")
    ap.add_argument("--env-file", default=None, help="Explicit .env file to read the key from.")
    ap.add_argument("--config", default=None, help="claude_desktop_config.json to write (default: auto-detect).")
    ap.add_argument("--uninstall", action="store_true", help="Remove the jev entries instead of adding them.")
    ap.add_argument("--dry-run", action="store_true", help="Print the resulting config without writing.")
    args = ap.parse_args(argv)

    jev_env: dict[str, str] = {}
    # Self-contained: NEVER read a Hermes profile. Precedence (low -> high):
    # repo-local .env, --env-file, shell env, --key.
    source = "none (keyless -> Zen free tier)"
    repo_env = REPO_ROOT / ".env"
    if repo_env.is_file():
        jev_env.update(load_env(repo_env))
        source = str(repo_env)
    if args.env_file:
        jev_env.update(load_env(Path(args.env_file)))
        source = args.env_file
    shell = {k: v for k, v in os.environ.items()
             if k in ("TYPESAFE_API_KEY", "OPENCODE_ZEN_API_KEY", "JEV_API_KEY")}
    if shell:
        jev_env.update(shell)
        source = "shell env"
    if args.key:
        var = _key_var(args.key, args.provider)
        jev_env[var] = args.key
        source = f"--key ({var})"

    cfg_path = Path(args.config) if args.config else config_path()
    cfg = {"mcpServers": {}, "hooks": {"PostToolUse": []}}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text())
        except json.JSONDecodeError as e:
            print(f"[claude-jev] WARNING: existing config is not valid JSON ({e}); merging into an empty config.", file=sys.stderr)

    cfg = merge_jev(cfg, jev_env, uninstall=args.uninstall)

    print(f"repo root : {_redact(str(REPO_ROOT)) if False else REPO_ROOT}")
    print(f"config    : {cfg_path}")
    print(f"env source: {source}")
    key = jev_env.get("TYPESAFE_API_KEY") or jev_env.get("OPENCODE_ZEN_API_KEY")
    print(f"key       : {'Bearer ' + _redact(key) if key else 'public (keyless)'}")
    print(f"server    : {sys.executable or 'python3'} {' '.join(abs_args(SERVER_ARGS))}")
    hook = next((h for h in cfg.get("hooks", {}).get("PostToolUse", []) if h.get("args") == abs_args(HOOK_ARGS)), None)
    if hook:
        print(f"hook      : {hook.get('command') or 'python3'} {' '.join(hook['args'])} (threshold=4000, exits 0)")
    else:
        print("hook      : removed")

    if args.dry_run:
        print("\n--- resulting claude_desktop_config.json ---")
        print(json.dumps(cfg, indent=2))
        return 0

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if cfg_path.is_file():
        shutil.copy2(cfg_path, str(cfg_path) + f".bak.{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"\nwrote {cfg_path}")
    print("restart Claude Desktop / Claude Code for changes to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
