"""jev-cost-guard — opt-in jev annotation for tool results (named after this repo).

Registers a `transform_tool_result` hook that APPENDS one line to oversized tool
results — a jev (TypeSafe System One DECISION model) `noul` verdict plus model and
latency — and writes one structured JSONL record. It never replaces the result.

Why annotate and not replace: jev returns typed decisions (noul / choice / score),
never text, so a verdict cannot stand in for the bytes it judged. TypeSafe's
"Harness Engineering" use case asks for exactly this shape — *detect tool-call
errors* and *log structured check results and probabilities* — and the context
managers that work (`jev-compaction`, `lossless-compaction`) keep every kept line
verbatim and move the rest behind a pointer. Appending one line keeps the payload
AND leaves a machine-readable trace.

Why delegate: provider selection (TypeSafe direct when `TYPESAFE_API_KEY` is set,
else the OpenCode zen relay) and *every* fallback rule live in the server — one
place. The Hermes hook, the `jev_*` MCP tools, and the Claude Code / Codex hooks
therefore cannot drift apart.

Policy — OPT-IN, and non-destructive:
  * Inactive unless the plugin is listed in `plugins.enabled`.
  * Annotates ONLY tools named in `JEV_VERIFY_TOOLS` (comma list; the legacy name
    `JEV_GATE_TOOLS` still works). Unset/empty => the hook does nothing at all.
  * Appends one line; every original byte is returned untouched.
  * Only annotates results larger than `JEV_THRESHOLD` tokens (default 1000, ~4 chars/token).
  * Writes one JSONL record per call (`JEV_VERIFY_LOG`, default
    `<profile>/logs/jev-verify.jsonl`): ts, tool, noul, band, model, ms, chars, provider.
  * `JEV_SKIP_TOOLS` is an optional second layer that skips a listed tool.
  * Skips every jev tool — native `jev_*` or MCP-namespaced `mcp__jev__*` — and any
    result already carrying a `[jev]` line (re-entry guard). Jev tool output is
    decision data, not noise.
  * FAILS OPEN: any error leaves the original, full result in place.
  * Tunables resolve from the process env first, then the profile `.env`,
    then their defaults (Hermes does not inject `.env` into the plugin host).

Tunables: `JEV_VERIFY_TOOLS` (or legacy `JEV_GATE_TOOLS`), `JEV_THRESHOLD`,
`JEV_SKIP_TOOLS`, `JEV_VERIFY_LOG`, `JEV_DEBUG`, `JEV_SERVER_PATH`,
`JEV_VERDICT_TIMEOUT`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# Marker appended to every annotated result; also the re-entry signature.
_MARK = "[jev] did this call achieve its ask?"


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


def _profile_dir() -> str:
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    prof = os.environ.get("HERMES_PROFILE") or os.environ.get("JEV_PROFILE") or "dev"
    return os.path.join(hermes_home, "profiles", prof)


def _log_path() -> str:
    """Where structured verdict records go (one JSON object per line)."""
    explicit = _setting("JEV_VERIFY_LOG")
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.join(_profile_dir(), "logs", "jev-verify.jsonl")


def _write_record(record: dict) -> None:
    """Append one JSONL record. Never raises: the log is observability, not control."""
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:  # noqa: BLE001 — logging must never break the loop
        _dbg(f"log write failed: {type(exc).__name__}: {exc}")


def _jev_verdict(state: str) -> dict | None:
    """Ask jev for a verdict via `mcp/jev/server.py --verdict`.

    Single source of truth for provider choice and fallbacks (no duplicated HTTP
    client here). Returns the parsed verdict dict, or None on any failure — the
    caller fails open and leaves the original tool result untouched.
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
    """Append one jev verdict line to `result`; every original byte is preserved."""
    # Re-entry guard: never gate jev's own tools — native (`jev_find`,
    # `jev_screen`, ...) or MCP-namespaced (`mcp__jev__jev_find`, ...). Their
    # output IS decision data; replacing it would destroy what the jev-* skills
    # are told to consume.
    if tool_name.startswith(("jev_", "mcp__jev__")):
        return None
    if not isinstance(result, str):
        return None
    # Never stack annotations. The marker sits in the appended tail, but an
    # upstream hook may have prefixed one, so check both ends.
    if "[jev]" in result[:300] or "[jev]" in result[-400:]:
        return None

    # Opt-in. Only tools explicitly named are annotated; empty => inert.
    allow = _csv(_setting("JEV_VERIFY_TOOLS") or _setting("JEV_GATE_TOOLS"))
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

    started = time.monotonic()
    verdict = _jev_verdict(result[:20000])
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not verdict:
        _dbg(f"tool={tool_name} not annotated (fail open)")
        return None

    noul = verdict.get("noul")
    band = verdict.get("interpretation") or (
        "likely-success" if (noul is None or noul >= 0.5) else "likely-failure"
    )
    model = verdict.get("model") or "jev"

    # `--verdict` reports the endpoint, not the provider name; derive it so the
    # JSONL trace says which backend actually answered (typesafe vs zen relay).
    endpoint = verdict.get("endpoint") or ""
    provider = verdict.get("provider") or (
        "typesafe" if "typesafe.ai" in endpoint else "opencode" if endpoint else None
    )

    _write_record({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool_name,
        "noul": noul,
        "band": band,
        "model": model,
        "ms": elapsed_ms,
        "chars": len(result),
        "provider": provider,
    })
    _dbg(f"annotated tool='{tool_name}': {len(result)} chars -> noul={noul} ({model}) {elapsed_ms}ms")

    line = (
        f"\n\n{_MARK} success={noul} ({band}) · {model} · {elapsed_ms}ms"
        f" · full output above, nothing removed (log: {_log_path()})"
    )
    return result + line


def register(ctx):
    """Hermes plugin entry point: attach the annotation hook to the agent loop."""
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
