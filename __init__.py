"""jev-cost-guard — opt-in token-cost guard, named after this repo.

Registers a `transform_tool_result` hook that gates oversized tool results
through **jev** (TypeSafe System One DECISION model) by delegating to the SAME
implementation the MCP server uses: `mcp/jev/server.py --verdict`.

Why delegate: provider selection (TypeSafe direct when `TYPESAFE_API_KEY` is set,
else the OpenCode zen relay) and *every* fallback rule live in the server — one
place. The Hermes hook, the `jev_health`/`jev_summarize` MCP tools, and the
Claude Code / Codex hooks therefore cannot drift apart.

Policy — DEFAULT-DENY, because a verdict REPLACES the result (it is lossy):
  * Inactive unless the plugin is listed in `plugins.enabled`.
  * Gates ONLY tools explicitly named in `JEV_GATE_TOOLS` (comma list).
    Unset/empty => the hook does nothing at all.
  * Never allowlist data-retrieval tools — the payload IS the answer
    (read_file, search_files, web_search, web_extract, browser_exec, ...).
    A verdict there destroys information instead of compressing it. Only
    diagnostic-noise tools belong on the list (`terminal` build/test logs,
    batch runs), and even `terminal` can carry data (`cat`, `curl`).
  * `JEV_SKIP_TOOLS` is an optional second layer that denies an allowlisted tool.
  * Only gates results larger than `JEV_THRESHOLD` tokens (default 1000, ~4 chars/token).
  * Skips every jev tool — native `jev_*` or MCP-namespaced `mcp__jev__*` — and
    any result that is already a `[jev]` verdict (re-entry guard). Jev tool
    output is decision data, not noise.
  * FAILS OPEN: any error leaves the original, full result in place.
  * Tunables resolve from the process env first, then the profile `.env`,
    then their defaults (Hermes does not inject `.env` into the plugin host).

Tunables: `JEV_GATE_TOOLS` (required to activate), `JEV_THRESHOLD`,
`JEV_SKIP_TOOLS`, `JEV_DEBUG`, `JEV_SERVER_PATH`, `JEV_VERDICT_TIMEOUT`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def _env_candidates() -> list[str]:
    """Profile .env paths (Hermes does not inject .env into the plugin host)."""
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    prof = os.environ.get("HERMES_PROFILE") or os.environ.get("JEV_PROFILE") or "dev"
    paths = [
        os.path.join(hermes_home, ".env"),
        os.path.join(hermes_home, "profiles", prof, ".env"),
    ]
    base = os.environ.get("HERMES_HOME_BASE") or os.path.expanduser("~/.hermes")
    if base != hermes_home:
        paths.append(os.path.join(base, "profiles", prof, ".env"))
    return paths


def _setting(name: str, default: str | None = None) -> str | None:
    """Resolve a tunable: process env first, then the profile .env, then default."""
    val = os.environ.get(name)
    if val:
        return val
    for path in _env_candidates():
        try:
            with open(path, "r") as fh:
                for line in fh:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    if k.strip() == name:
                        return v.strip().strip('"').strip("'")
        except OSError:
            continue
    return default


def _dbg(msg: str) -> None:
    if _setting("JEV_DEBUG"):
        sys.stderr.write(f"[jev-cost-guard] {msg}\n")
        sys.stderr.flush()


def _csv(val: str | None) -> list[str]:
    return [t.strip() for t in (val or "").split(",") if t.strip()]


def _server_path() -> str:
    """Locate the shared jev implementation (the MCP server, `--verdict` mode).

    Prefers JEV_SERVER_PATH when set; otherwise the copy shipped inside this
    plugin directory (`<plugin>/mcp/jev/server.py`), which is what a git install
    of this repo provides.
    """
    explicit = _setting("JEV_SERVER_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mcp", "jev", "server.py"
    )


def _jev_verdict(state: str) -> dict | None:
    """Ask jev for a verdict via `mcp/jev/server.py --verdict`.

    Single source of truth for provider choice and fallbacks (no duplicated HTTP
    client here). Returns the parsed verdict dict, or None on any failure — the
    caller fails open and keeps the original tool result.
    """
    server = _server_path()
    if not os.path.exists(server):
        _dbg(f"server missing at {server!r}; fail open")
        return None
    try:
        timeout = float(_setting("JEV_VERDICT_TIMEOUT") or "75")
    except ValueError:
        timeout = 75.0
    try:
        proc = subprocess.run(
            [sys.executable, server, "--verdict"],
            input=state.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — fail open, never block the loop
        _dbg(f"verdict subprocess failed: {type(exc).__name__}: {exc}")
        return None
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if err and _setting("JEV_DEBUG"):
        for line in err.splitlines()[-4:]:
            _dbg(f"server: {line}")
    if proc.returncode != 0 or not out:
        _dbg(f"verdict rc={proc.returncode}; fail open")
        return None
    try:
        data = json.loads(out.splitlines()[-1])
    except Exception:  # noqa: BLE001
        _dbg(f"unparseable verdict: {out[:160]!r}")
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        _dbg(f"verdict not ok: {data.get('error', 'unknown') if isinstance(data, dict) else data}")
        return None
    return data


def _on_transform_tool_result(tool_name: str = "", args: object = None,
                              result: object = None, **_):
    """Replace oversized `result` strings with a jev noul verdict (fail open)."""
    # Re-entry guard: never gate jev's own tools — native (`jev_find`,
    # `jev_screen`, ...) or MCP-namespaced (`mcp__jev__jev_find`, ...). Their
    # output IS decision data; replacing it would destroy what the jev-* skills
    # are told to consume.
    if tool_name.startswith(("jev_", "mcp__jev__")):
        return None
    if not isinstance(result, str):
        return None
    if result.lstrip().startswith("[jev]"):
        return None

    # DEFAULT-DENY. Only tools explicitly named in JEV_GATE_TOOLS are gated;
    # an empty list means the hook is inert. JEV_SKIP_TOOLS is a second layer.
    allow = _csv(_setting("JEV_GATE_TOOLS"))
    if not allow or tool_name not in allow:
        return None
    if tool_name in set(_csv(_setting("JEV_SKIP_TOOLS"))):
        return None

    try:
        threshold = int(_setting("JEV_THRESHOLD") or "1000")
    except ValueError:
        threshold = 1000
    # rough token estimate: ~4 chars/token
    if len(result) < max(1, threshold) * 4:
        return None

    verdict = _jev_verdict(result[:20000])
    if not verdict:
        _dbg(f"tool={tool_name} not gated (fail open)")
        return None

    n = verdict.get("noul")
    interp = verdict.get("interpretation") or (
        "likely-success" if (n is None or n >= 0.5) else "likely-failure"
    )
    model = verdict.get("model") or "jev"
    text = (
        f"[jev] is_successful noul={n} -> {interp} "
        f"(jev System One decision via {model}; ~7 output tokens, not a digest; "
        f"gated tool='{tool_name}' result_len={len(result)}; "
        f"remove '{tool_name}' from JEV_GATE_TOOLS to keep full output)"
    )
    _dbg(f"gated tool='{tool_name}': {len(result)} chars -> noul={n} ({model})")
    return text


def register(ctx):
    """Hermes plugin entry point: attach the cost-guard hook to the agent loop."""
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
