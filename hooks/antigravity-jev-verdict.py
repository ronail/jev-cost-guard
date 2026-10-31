#!/usr/bin/env python3
"""Antigravity PreInvocation hook: judge a large tool output with jev.

Antigravity's `PostToolUse` does **not** carry the tool output (measured: the payload
is `toolCall{name,args}` + `stepIdx` + `error` + `lastUserInput`), so the Claude
Code / Codex gate cannot be ported as-is. The tool result does exist in the
conversation transcript the event points at, so this hook runs one step earlier, on
`PreInvocation` — before the model is called:

1. read the transcript, collect tool-result rows newer than the last judged step,
2. take the single largest one above `JEV_VERDICT_THRESHOLD_CHARS`,
3. ask jev the `is_successful` noul question via `mcp/jev/server.py --verdict`,
4. inject one advisory line as an `ephemeralMessage`.

Nothing is replaced and nothing is blocked: the tool output is already in the
transcript, the line is additive, and every failure path prints `{}` and exits 0.
At most one jev call per model invocation, because the judged step is remembered per
conversation.

Transcript shape (measured, `agy` 1.2.12): newline-delimited JSON rows with
`step_index`, `type`, `content`. A model turn is `PLANNER_RESPONSE` and carries
`tool_calls`; its result arrives as the next `GENERIC` row, whose `content` is the
output text. That pairing is how a result is told apart from model prose.

Env:
  JEV_VERDICT_THRESHOLD_CHARS  gate in chars (default 4000)
  JEV_VERDICT_TIMEOUT_S        server call budget (default 20)
  JEV_SERVER_PATH              override the server path (tests)
  JEV_AGY_STATE_DIR            where judged-step markers live (tests)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SERVER = os.environ.get("JEV_SERVER_PATH") or os.path.join(
    REPO_ROOT, "mcp", "jev", "server.py"
)
THRESHOLD = int(os.environ.get("JEV_VERDICT_THRESHOLD_CHARS", "4000"))
TIMEOUT = float(os.environ.get("JEV_VERDICT_TIMEOUT_S", "20"))

# Never judge jev's own answers: that is the decision the model is reading, and a
# second jev call per verdict costs more than it saves.
_JEV_SERVER_HINTS = ("jev", "typesafe")


def _state_dir() -> str:
    base = os.environ.get("JEV_AGY_STATE_DIR")
    if base:
        return base
    return os.path.join(
        os.environ.get("TMPDIR") or "/tmp", "jev-agy-verdict"
    )


def _last_judged(conversation_id: str) -> int:
    try:
        with open(os.path.join(_state_dir(), conversation_id), encoding="utf-8") as fh:
            return int((json.load(fh) or {}).get("last_step", -1))
    except (OSError, ValueError, TypeError):
        return -1


def _remember(conversation_id: str, step: int) -> None:
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        path = os.path.join(_state_dir(), conversation_id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"last_step": step}, fh)
    except OSError:
        pass  # fail open: re-judging once is cheaper than breaking the turn


def _rows(transcript_path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(os.path.expanduser(transcript_path), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _pending_calls(row: dict) -> list[tuple[str, str]]:
    """(tool_name, mcp_server) for each call in a model turn; server is '' when builtin."""
    calls = row.get("tool_calls")
    if not isinstance(calls, list):
        return []
    out: list[tuple[str, str]] = []
    for call in calls:
        if not isinstance(call, dict) or not call.get("name"):
            continue
        raw_args = call.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        server = str(args.get("ServerName") or args.get("serverName") or "")
        out.append((str(call["name"]), server))
    return out


def _is_jev_call(call_name: str, server: str) -> bool:
    haystack = f"{call_name} {server}".lower()
    return any(hint in haystack for hint in _JEV_SERVER_HINTS) or call_name.startswith("jev_")


def _largest_new_result(rows: list[dict], after_step: int) -> tuple[int, str, str] | None:
    """(step, tool_name, output) for the biggest unjudged tool result, or None.

    A `GENERIC` row is a tool result only when the model turn just before it carried
    `tool_calls`; without that pairing it is model prose and must not be judged.
    """
    best: tuple[int, str, str] | None = None
    pending: list[tuple[str, str]] = []
    for row in rows:
        try:
            step = int(row.get("step_index", -1))
        except (TypeError, ValueError):
            continue
        calls = _pending_calls(row)
        if calls:
            pending = calls
            continue
        if row.get("type") != "GENERIC" or step <= after_step:
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if not pending:
            continue
        tool, server = pending.pop(0)
        if _is_jev_call(tool, server):
            continue  # never judge our own verdicts
        if len(content) < THRESHOLD:
            continue
        if best is None or len(content) > len(best[2]):
            best = (step, tool, content)
    return best


def _ensure_credential() -> None:
    """Pick up the plugin data key file Antigravity exports to MCP servers."""
    if any(os.environ.get(k) for k in ("TYPESAFE_API_KEY", "OPENCODE_ZEN_API_KEY",
                                       "JEV_API_KEY_FILE", "JEV_PROVIDER")):
        return
    data = os.environ.get("PLUGIN_DATA")
    if data:
        candidate = os.path.join(data, "api_key")
        if os.path.isfile(candidate):
            os.environ["JEV_API_KEY_FILE"] = candidate


def _verdict(text: str) -> dict | None:
    try:
        proc = subprocess.run(
            [sys.executable, SERVER, "--verdict"],
            input=text, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except Exception as e:  # timeout, missing server, unreadable exec
        sys.stderr.write(f"[jev-agy-gate] skipped ({e})\n")
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return verdict if isinstance(verdict, dict) and verdict.get("ok") else None


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(event, dict):
        sys.exit(0)
    transcript = event.get("transcriptPath")
    conversation = str(event.get("conversationId") or "")
    if not transcript or not conversation:
        sys.exit(0)

    best = _largest_new_result(_rows(str(transcript)), _last_judged(conversation))
    if best is None:
        sys.exit(0)
    step, tool, output = best
    _ensure_credential()
    verdict = _verdict(output)
    _remember(conversation, step)  # judged either way: never retry a failed call
    if verdict is None:
        sys.exit(0)

    n = len(output)
    if verdict.get("noul") is not None:
        summary = f"noul={verdict.get('noul')} -> {verdict.get('interpretation')}"
    else:
        summary = f"verdict={json.dumps(verdict)}"
    line = (
        f"[jev] {tool}: {n} chars judged, {summary} "
        f"(model={verdict.get('model')}, endpoint={verdict.get('endpoint')}). "
        "Advisory only — the tool output above is unchanged; read it when the verdict "
        "is not enough, and say so if you disagree."
    )
    print(json.dumps({"injectSteps": [{"ephemeralMessage": line}]}))
    sys.exit(0)


if __name__ == "__main__":
    main()
