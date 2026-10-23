"""jev-cost-guard — opt-in token-cost guard, named after this repo.

Registers a `transform_tool_result` hook that gates oversized tool results
through **jev** (TypeSafe System One DECISION model) by delegating to the SAME
implementation the MCP server uses: `mcp/jev/server.py --verdict`.

Why delegate: provider selection (TypeSafe direct when `TYPESAFE_API_KEY` is set,
else the OpenCode zen relay) and *every* fallback rule live in the server — one
place. The Hermes hook, the `jev_health`/`jev_summarize` MCP tools, and the
Claude Code / Codex hooks therefore cannot drift apart.

Chain inside the server (see server.py):
  jev-1.13-free (zen) -> paid JEV_FALLBACK_MODEL on 429/403 -> TypeSafe
  jev-latest on 402/403/429 when TYPESAFE_API_KEY exists. Each layer is
  individually switchable (JEV_FALLBACK_TO_PAID / JEV_FALLBACK_TO_TYPESAFE).

Policy (deliberately conservative + opt-in):
  * Inactive unless the plugin is listed in `plugins.enabled`.
  * Only gates results larger than JEV_THRESHOLD tokens (default 1000, ~4 chars/token).
  * Skips jev's own tools (jev_health/jev_summarize) and any result that is
    already a `[jev]` verdict (re-entry guard).
  * Never gates data-bearing tools the agent needs verbatim (read_file,
    write_file, patch, edit_file, skill_manage) — overridable via JEV_SKIP_TOOLS.
    Set JEV_GATE_TOOLS (comma list) to gate ONLY those tools.
  * FAILS OPEN: any error leaves the original, full result in place.
  * JEV_DEBUG=1 prints per-call diagnostics (never key material).

Tunables: JEV_THRESHOLD, JEV_GATE_TOOLS, JEV_SKIP_TOOLS, JEV_DEBUG,
JEV_SERVER_PATH (override the server location), JEV_VERDICT_TIMEOUT (seconds).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_SKIP_DEFAULT = "read_file,write_file,patch,edit_file,skill_manage"


def _dbg(msg: str) -> None:
    if os.environ.get("JEV_DEBUG"):
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
    explicit = os.environ.get("JEV_SERVER_PATH")
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
        timeout = float(os.environ.get("JEV_VERDICT_TIMEOUT", "75"))
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
    if err and os.environ.get("JEV_DEBUG"):
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
    # Re-entry guard: never gate jev's own tools, and never chain on a verdict.
    if tool_name in ("jev_health", "jev_summarize", "jev_debug"):
        return None
    if not isinstance(result, str):
        return None
    if result.lstrip().startswith("[jev]"):
        return None

    skip = set(_csv(os.environ.get("JEV_SKIP_TOOLS")) or _csv(_SKIP_DEFAULT))
    allow = _csv(os.environ.get("JEV_GATE_TOOLS"))
    if tool_name in skip:
        return None
    if allow and tool_name not in allow:
        return None

    try:
        threshold = int(os.environ.get("JEV_THRESHOLD", "1000"))
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
        f"set JEV_THRESHOLD=0 to keep full output)"
    )
    _dbg(f"gated tool='{tool_name}': {len(result)} chars -> noul={n} ({model})")
    return text


def register(ctx):
    """Hermes plugin entry point: attach the cost-guard hook to the agent loop."""
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
