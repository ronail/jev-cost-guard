#!/usr/bin/env python3
"""Claude Code / Codex PostToolUse hook: judge large tool outputs with jev.

Soft gate — it never blocks the harness and never replaces the payload. When a
tool result exceeds JEV_VERDICT_THRESHOLD_CHARS (default 4000) the hook pipes it
to ../mcp/jev/server.py --verdict, which asks jev the `is_successful` noul
question, and returns one advisory line as `additionalContext`. The original
tool result still reaches the model, so nothing is lost when jev is wrong.

Both harness envelopes are accepted:

  Claude Code   {"tool_name": "Bash", "tool_result": {"output": ...}}
  Codex         {"tool_name": "Bash", "tool_response": {...}, "hook_event_name": ...}

Codex matcher names differ (shell calls arrive as `Bash`, edits as
`apply_patch` / `Edit` / `Write`), so hooks.json matches both vocabularies and
this script does not care which one it was handed.

Credential: inherited from the harness env (`TYPESAFE_API_KEY`, or
`OPENCODE_ZEN_API_KEY` / `JEV_API_KEY_FILE`). Codex scrubs that environment, so
as a last step it falls back to the Codex-owned credential file, `$CODEX_HOME/jev/api_key`
(see README). Without any key the server uses the keyless Zen relay.

Always exits 0: a jev slip must never stall a session.
"""
import json
import os
import subprocess
import sys

# Claude Code plugin: userConfig values arrive as CLAUDE_PLUGIN_OPTION_<KEY>
# (uppercased), not under the jev env names. Map them so the jev call can
# authenticate under a plugin install without reading a Hermes profile.
for _opt, _env in (
    ("CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY", "TYPESAFE_API_KEY"),
    ("CLAUDE_PLUGIN_OPTION_OPENCODE_ZEN_API_KEY", "OPENCODE_ZEN_API_KEY"),
):
    if os.environ.get(_opt) and not os.environ.get(_env):
        os.environ[_env] = os.environ[_opt]

HERE = os.path.dirname(os.path.abspath(__file__))  # this file's dir (hooks/)
REPO_ROOT = os.path.dirname(HERE)  # repo root (parent of hooks/)
SERVER = os.environ.get("JEV_SERVER_PATH") or os.path.join(
    REPO_ROOT, "mcp", "jev", "server.py"
)
THRESHOLD = int(os.environ.get("JEV_VERDICT_THRESHOLD_CHARS", "4000"))
TIMEOUT = float(os.environ.get("JEV_VERDICT_TIMEOUT_S", "20"))

sys.path.insert(0, HERE)
try:  # the shared decision core and the execution ledger
    from lib import approvals, gates
except Exception:  # a broken import must not break a hook
    approvals = gates = None  # type: ignore[assignment]

# Never gate jev's own tools: their output is the decision the model is reading,
# and a second jev call per result would cost more than it saves.
_JEV_PREFIXES = ("jev_", "mcp__jev__", "mcp__plugin_jev", "codex_jev")


def _is_jev_tool(name: str) -> bool:
    return any(name.startswith(p) for p in _JEV_PREFIXES)


def _flatten(value) -> str:
    """Text content of a tool response, whatever shape the harness used."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        parts = []
        for key in ("output", "stdout", "stderr", "content", "text", "result",
                    "formatted_output"):
            if key in value:
                text = _flatten(value.get(key))
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts)
        return "".join(_flatten(v) for v in value.values())
    return str(value)


def _tool_of(event: dict) -> str:
    """Tool name from either envelope: Claude Code/Codex, or Antigravity's toolCall."""
    return str(event.get("tool_name") or "").strip() or str((event.get("toolCall") or {}).get("name") or "")


def _host_of(event: dict) -> str:
    if "toolCall" in event or "conversationId" in event:
        return "antigravity"
    return os.environ.get("JEV_GATE_HOST", "claude")


def _record_execution(event: dict, tool: str) -> None:
    """Note that this exact command ran, so the pre-tool gate need not ask again.

    The hook cannot see the human's answer; it can see that the tool ran afterwards,
    which is the same fact from the agent's side. Both key shapes are written because
    the pre-tool gate picks one from `cache.scope`, and a stale note must not depend on
    an unrelated setting. Bookkeeping only — every failure is swallowed.
    """
    if approvals is None or gates is None:
        return
    try:
        args = event.get("tool_input")
        if not isinstance(args, dict):
            args = (event.get("toolCall") or {}).get("args") or {}   # Antigravity envelope
        if not gates.is_shell_tool(tool, args):
            return
        command = gates.extract_command(args)
        if not command:
            return
        host = _host_of(event)
        session = str(event.get("session_id") or event.get("conversationId") or "")
        cwd = str(event.get("cwd") or args.get("cwd") or "")
        mode = str(event.get("permission_mode") or "default")
        grant = approvals.grant_scope(approvals.last_permission(str(event.get("transcript_path") or "")))
        if grant == "once":
            return                     # explicitly one-shot: never remembered
        # None means the host records no grant scope (Codex, Antigravity) — the ledger keeps
        # the fact, and cache.approvalScope decides whether it may silence the gate.
        for key in (gates.cache_key(host, tool, command, cwd, session),
                    gates.cache_key(host, tool, command, cwd, "")):
            approvals.record_run(key, mode)
    except Exception:
        return


def _extract_output(event: dict) -> str:
    """Pull the tool output text out of a Claude Code / Codex hook envelope."""
    for key in ("tool_response", "tool_result", "tool_output"):
        if key in event:
            text = _flatten(event.get(key))
            if text:
                return text
    return ""


def _as_context(text: str) -> str:
    """The harness-agnostic stdout shape (both hosts read additionalContext)."""
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    })


def _ensure_credential() -> None:
    """Point the server at a Codex-owned key file when the env carries none."""
    if any(os.environ.get(k) for k in ("TYPESAFE_API_KEY", "OPENCODE_ZEN_API_KEY",
                                       "JEV_API_KEY_FILE", "JEV_PROVIDER")):
        return
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    candidate = os.path.join(home, "jev", "api_key")
    if os.path.isfile(candidate):
        os.environ["JEV_API_KEY_FILE"] = candidate


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(event, dict):
        sys.exit(0)
    tool = _tool_of(event)
    if not tool or _is_jev_tool(tool):
        sys.exit(0)
    _record_execution(event, tool)   # before the size check: an approval is not a payload
    output = _extract_output(event)
    n = len(output)
    if n < THRESHOLD:
        sys.exit(0)  # below the gate threshold — nothing worth judging
    _ensure_credential()
    try:
        proc = subprocess.run(
            [sys.executable, SERVER, "--verdict"],
            input=output, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except Exception as e:  # timeout, missing server, unreadable exec
        sys.stderr.write(f"[jev-gate] {tool}: skipped ({e})\n")
        sys.exit(0)
    raw = proc.stdout.strip()
    if not raw:
        sys.exit(0)
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)
    if not v.get("ok"):
        sys.exit(0)
    verdict = (f"noul={v.get('noul')} -> {v.get('interpretation')}"
               if v.get("noul") is not None
               else f"verdict={raw}")
    print(_as_context(
        f"[jev] {tool}: {n} chars judged, {verdict} "
        f"(model={v.get('model')}, endpoint={v.get('endpoint')}). "
        "Advisory only — the tool output above is unchanged; read it when the "
        "verdict is not enough, and say so if you disagree."
    ))
    sys.exit(0)


if __name__ == "__main__":
    main()
