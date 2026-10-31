#!/usr/bin/env python3
"""Portable fixed-prompt / skills-index estimator.

Works in any agent harness with a python3 on PATH: no harness CLI, no network,
no lock file, no writes. It walks the skills roots it can see, parses each
``SKILL.md`` frontmatter, and sums the part of the index line a skill controls:

    line_estimate = len(name) + 2 + len(description)

Real renderers add separators and a line per category, so the total is a LOWER
BOUND of the rendered skills block. That is enough to rank skills and decide
what to cut — which is all the gate needs. When the harness exposes a native
measurement (Hermes: ``hermes prompt-size --json``), use that as the final
number; elsewhere quote this estimate and say it is one.

Usage
-----
    python3 scripts/prompt_budget.py                    # auto-detect harness
    python3 scripts/prompt_budget.py --harness codex
    python3 scripts/prompt_budget.py --harness antigravity
    python3 scripts/prompt_budget.py --root ~/.codex/skills --root ./skills
    python3 scripts/prompt_budget.py --json --budget 2500

Exit status: 0 under budget (or no budget given), 1 over budget, 2 no skills
found. ``--no-budget-fail`` always exits 0 so it can be used in probes.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

CHARS_PER_TOKEN = 4.0
DEFAULT_BUDGET = 2500  # bytes of index line per skill roster, ~0.6k tokens
MAX_SCAN_DEPTH = 4
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv"}

# ---- harness detection ------------------------------------------------------

HERMES_ENV = ("HERMES_HOME", "HERMES_PROFILE", "HERMES_SESSION")
CLAUDE_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT")
CODEX_ENV = ("CODEX_HOME", "CODEX_SANDBOX", "CODEX_SESSION_ID", "OPENAI_CODEX")
ANTIGRAVITY_ENV = ("ANTIGRAVITY_AGENT", "ANTIGRAVITY_APP_DATA_DIR", "JETSKI_APP_DATA_DIR")


def detect_harness() -> str:
    env = os.environ
    if any(env.get(k) for k in HERMES_ENV) and not any(env.get(k) for k in CODEX_ENV):
        return "hermes"
    if any(env.get(k) for k in CODEX_ENV):
        return "codex"
    if any(env.get(k) for k in CLAUDE_ENV):
        return "claude"
    if any(env.get(k) for k in ANTIGRAVITY_ENV):
        return "antigravity"
    return "unknown"


def home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def newest_profile_skills() -> str | None:
    """The most recently used Hermes profile's skills dir (no lock, no CLI)."""
    pattern = home(".hermes", "profiles", "*", "skills")
    candidates = [p for p in glob.glob(pattern) if os.path.isdir(p)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: os.path.getmtime(p))


SKIPPED: list[tuple[str, str]] = []  # (path, reason) roots left out of the estimate
FILTER_STATE = "n/a"  # "applied" | "unavailable" (no tomllib/config) | "all-plugins" | "n/a"
AG_FILTER_STATE = "n/a"  # same vocabulary, for Antigravity's plugin roster


def codex_enabled_plugins() -> set[str] | None:
    """`{"plugin@marketplace"}` Codex actually loads, from its own config.toml.

    Returns None when the config cannot be read (then nothing is filtered and the
    caller is told the number is an upper bound).
    """
    path = home(".codex", "config.toml")
    try:
        import tomllib  # py3.11+
    except ImportError:
        return None
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, ValueError):
        return None
    plugins = cfg.get("plugins")
    if not isinstance(plugins, dict):
        return None
    return {name for name, spec in plugins.items() if not isinstance(spec, dict) or spec.get("enabled", True)}


def codex_plugin_key(path: str) -> str | None:
    """Map `<cache>/<marketplace>/<plugin>/<version>/skills` to `plugin@marketplace`."""
    cache = home(".codex", "plugins", "cache")
    rel = os.path.relpath(path, cache)
    if rel.startswith(".."):
        return None
    parts = rel.split(os.sep)
    if parts and parts[-1] == "skills":
        parts = parts[:-1]
    if len(parts) >= 3:
        marketplace, plugin = parts[0], parts[1]
        return f"{plugin}@{marketplace}"
    if len(parts) == 2:
        plugin = parts[0]
        return f"{plugin}@{plugin}"
    return None


# Antigravity global skills roots: App + IDE (`~/.gemini/config/skills`), the IDE's
# legacy path, and the CLI's own directory. A workspace roster adds `.agents/skills`
# (`.agent/skills` is the back-compat spelling).
ANTIGRAVITY_GLOBAL_SKILL_ROOTS = (
    (".gemini", "config", "skills"),
    (".gemini", "antigravity", "skills"),
    (".gemini", "antigravity-cli", "skills"),
)
ANTIGRAVITY_WORKSPACE_ROOTS = ((".agents", "skills"), (".agent", "skills"))


def antigravity_plugin_dirs() -> list[str]:
    return sorted(p for p in glob.glob(home(".gemini", "config", "plugins", "*")) if os.path.isdir(p))


def antigravity_disabled_plugins() -> set[str] | None:
    """Plugin names Antigravity does not load.

    Two independent signals, both local: an `enabled: false` entry in
    `~/.gemini/config/config.json`, and the marker `agy plugin disable` leaves
    behind (`plugin.json` renamed to `plugin.json.disabled`). Returns None when
    no plugin directory is readable at all, so the caller reports an upper bound
    instead of quietly filtering everything.
    """
    dirs = antigravity_plugin_dirs()
    if not dirs:
        return None
    disabled: set[str] = set()
    try:
        with open(home(".gemini", "config", "config.json"), encoding="utf-8") as fh:
            plugins = json.load(fh).get("plugins")
    except (OSError, ValueError):
        plugins = None
    if isinstance(plugins, dict):
        for name, spec in plugins.items():
            if isinstance(spec, dict) and spec.get("enabled") is False:
                disabled.add(name)
    for path in dirs:
        name = os.path.basename(path)
        if (os.path.isfile(os.path.join(path, "plugin.json.disabled"))
                and not os.path.isfile(os.path.join(path, "plugin.json"))):
            disabled.add(name)
    return disabled


def default_roots(
    harness: str, profile: str | None = None, all_plugins: bool = False
) -> list[tuple[str, str]]:
    """Return (label, path) skills roots for ONE roster of the given harness."""
    roots: list[tuple[str, str]] = []
    SKIPPED.clear()

    def add(label: str, path: str | None) -> None:
        if path and os.path.isdir(path):
            roots.append((label, path))

    def add_glob(label: str, pattern: str, enabled: set[str] | None = None) -> None:
        for path in sorted(glob.glob(pattern)):
            if enabled is not None and not all_plugins:
                key = codex_plugin_key(path)
                if key and key not in enabled:
                    SKIPPED.append((path, f"{key} is not enabled"))
                    continue
            add(label, path)

    if harness in ("hermes", "unknown"):
        hermes_home = os.environ.get("HERMES_HOME")
        if hermes_home:
            add("hermes:active-profile", os.path.join(hermes_home, "skills"))
        elif profile:
            add(f"hermes:{profile}", home(".hermes", "profiles", profile, "skills"))
        else:
            add("hermes:newest-profile", newest_profile_skills())
        add_glob("hermes:plugin", home(".hermes", "plugins", "*", "skills"))

    if harness in ("claude", "unknown"):
        add("claude:user", home(".claude", "skills"))
        add_glob("claude:plugin", home(".claude", "plugins", "*", "skills"))
        add_glob("claude:plugin", home(".claude", "plugins", "*", "*", "skills"))

    if harness in ("codex", "unknown"):
        add("codex:user", home(".codex", "skills"))
        global FILTER_STATE
        enabled = codex_enabled_plugins()
        if all_plugins:
            FILTER_STATE = "all-plugins"
        elif enabled is None:
            FILTER_STATE = "unavailable"
        else:
            FILTER_STATE = "applied"
        add_glob("codex:plugin", home(".codex", "plugins", "cache", "*", "*", "*", "skills"), enabled)
        add_glob("codex:plugin", home(".codex", "plugins", "*", "skills"), enabled)

    if harness in ("antigravity", "unknown"):
        for parts in ANTIGRAVITY_GLOBAL_SKILL_ROOTS:
            add(f"antigravity:{parts[-2]}", home(*parts))
        for parts in ANTIGRAVITY_WORKSPACE_ROOTS:
            add("antigravity:workspace", os.path.join(os.getcwd(), *parts))
        global AG_FILTER_STATE
        disabled = antigravity_disabled_plugins()
        if all_plugins:
            AG_FILTER_STATE = "all-plugins"
        elif disabled is None:
            AG_FILTER_STATE = "unavailable"
        else:
            AG_FILTER_STATE = "applied"
        for path in antigravity_plugin_dirs():
            name = os.path.basename(path)
            skills_dir = os.path.join(path, "skills")
            if not os.path.isdir(skills_dir):
                continue
            if disabled is not None and not all_plugins and name in disabled:
                SKIPPED.append((skills_dir, f"{name} is disabled"))
                continue
            add("antigravity:plugin", skills_dir)

    return roots


# ---- frontmatter ------------------------------------------------------------

_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter reader: name, description (plain or block)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    body: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        body.append(line)

    out: dict[str, str] = {}
    i = 0
    while i < len(body):
        m = _KEY_RE.match(body[i])
        i += 1
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip()
        if rest in ("|", ">", "|-", ">-", "|+", ">+"):
            block: list[str] = []
            while i < len(body) and (not body[i].strip() or body[i][:1] in " \t"):
                block.append(body[i].strip())
                i += 1
            joiner = "\n" if rest.startswith("|") else " "
            out[key] = joiner.join(part for part in block if part)
        elif rest == "":
            # nested mapping or list: skip its indented children
            while i < len(body) and (not body[i].strip() or body[i][:1] in " \t"):
                i += 1
        else:
            out[key] = _unquote(rest)
    return out


def find_skill_files(root: str) -> list[str]:
    found: list[str] = []
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= MAX_SCAN_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if "SKILL.md" in filenames:
            found.append(os.path.join(dirpath, "SKILL.md"))
    return found


def collect(roots: list[tuple[str, str]]) -> tuple[list[dict], int]:
    """Return (skills sorted by cost, number of duplicate names collapsed).

    One skill name renders once in the index, so multiple copies of the same
    name (a plugin cache holding several versions, a repo checkout beside the
    installed copy) collapse to the fattest copy instead of inflating the total.
    """
    skills: dict[str, dict] = {}
    collapsed = 0
    for label, root in roots:
        for path in find_skill_files(root):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    head = fh.read(8192)
            except OSError:
                continue
            fm = parse_frontmatter(head)
            name = fm.get("name") or os.path.basename(os.path.dirname(path))
            desc = fm.get("description", "")
            entry = {
                "name": name,
                "description_chars": len(desc),
                "bytes": len(name.encode()) + 2 + len(desc.encode()),
                "source": label,
                "path": path,
            }
            prev = skills.get(name)
            if prev is None:
                skills[name] = entry
            else:
                collapsed += 1
                if entry["bytes"] > prev["bytes"]:
                    skills[name] = entry
    return sorted(skills.values(), key=lambda s: -s["bytes"]), collapsed


# ---- main -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Estimate the skills-index cost of a harness.")
    ap.add_argument("--harness", default="auto", choices=["auto", "hermes", "claude", "codex", "antigravity", "unknown", "all"])
    ap.add_argument("--profile", default=None, help="Hermes profile name to measure (default: newest)")
    ap.add_argument("--root", action="append", default=[], help="skills root to scan (repeatable)")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="index budget in bytes (default 2500)")
    ap.add_argument("--no-budget-fail", action="store_true", help="always exit 0")
    ap.add_argument("--all-plugins", action="store_true", help="count every plugin cache, even disabled ones")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--top", type=int, default=0, help="only print the N most expensive skills")
    args = ap.parse_args(argv)

    harness = detect_harness() if args.harness == "auto" else args.harness
    if harness == "all":
        harness = "unknown"

    roots = [("cli", os.path.expanduser(r)) for r in args.root] or default_roots(
        harness, args.profile, args.all_plugins
    )
    excluded = [] if args.root else [{"path": p, "reason": why} for p, why in SKIPPED]
    skills, collapsed = collect(roots)
    total = sum(s["bytes"] for s in skills)
    over = total > args.budget

    payload = {
        "harness": harness,
        "native_measurement": "hermes prompt-size --json" if harness == "hermes" else None,
        "roots": [{"label": label, "path": root, "exists": os.path.isdir(root)} for label, root in roots],
        "skills": skills,
        "count": len(skills),
        "duplicate_names_collapsed": collapsed,
        "excluded_roots": excluded,
        "total_bytes": total,
        "est_tokens": round(total / CHARS_PER_TOKEN, 1),
        "budget_bytes": args.budget,
        "status": "over" if over else "under",
        "plugin_filter": FILTER_STATE,
        "plugin_filter_antigravity": AG_FILTER_STATE,
        "note": "lower bound: renderer adds separators and per-category lines",
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        shown = skills[: args.top] if args.top else skills
        width = max([len(s["name"]) for s in shown] + [9])
        print(f"harness: {harness}   (roots: {len(roots)} scanned)")
        for s in shown:
            print(f"  {s['bytes']:>7}  {s['name']:<{width}}  {s['source']}")
        print(f"  {total:>7}  TOTAL over {len(skills)} skills  ~{total / CHARS_PER_TOKEN:.0f} tokens/call")
        verdict = "OVER" if over else "under"
        print(f"budget {args.budget} B -> {verdict}")
        if excluded:
            print(f"excluded {len(excluded)} plugin skill root(s) the harness does not load "
                  f"(use --all-plugins to count them)")
        elif FILTER_STATE == "unavailable":
            print("plugin caches NOT filtered (no tomllib or no ~/.codex/config.toml): "
                  "treat the total as an upper bound, or pass --root")
        if AG_FILTER_STATE == "unavailable":
            print("antigravity plugin caches NOT filtered (no plugin dir under ~/.gemini/config): "
                  "treat the total as an upper bound, or pass --root")
        if not skills:
            print("no SKILL.md found under the scanned roots", file=sys.stderr)
        if harness != "hermes":
            print("note: lower bound; no native prompt-size in this harness — quote it as an estimate")

    if not skills:
        return 2
    if over and not args.no_budget_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
