"""Read a session transcript into the few facts a completion gate needs.

Two formats, one normalized event list:

  Claude Code   JSONL of `{"type": "user"|"assistant", "message": {"role", "content"}}`
                where content is a list of `text`, `tool_use` and `tool_result` blocks.
  Antigravity   JSONL of `{"step_index", "type", "content"}` where a `PLANNER_RESPONSE`
                carries `tool_calls` and the next `GENERIC` row carries the output.

Both are read tolerantly: an unknown row is skipped, never an exception. A gate that
dies on a transcript shape it has not seen is worse than one that abstains, and the
hook's fail-open path depends on this function returning something.

Only the last turn matters — everything before the final user prompt is context the
question does not need, and not sending it is cheaper and leaks less.
"""

from __future__ import annotations

import json
import os

from . import gates


def _flatten(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(part for part in (_flatten(v) for v in value) if part)
    if isinstance(value, dict):
        for key in ("text", "content", "output", "stdout", "stderr", "result"):
            if key in value:
                text = _flatten(value[key])
                if text:
                    return text
        return "\n".join(part for part in (_flatten(v) for v in value.values()) if part)
    return str(value)


def read_rows(path: str) -> list[dict]:
    """JSONL rows from a transcript path; [] when it cannot be read."""
    if not path:
        return []
    rows: list[dict] = []
    try:
        with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as handle:
            for line in handle:
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


def _claude_events(row: dict) -> list[dict]:
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    role = str(message.get("role") or row.get("type") or "")
    content = message.get("content")
    blocks = content if isinstance(content, list) else (
        [{"type": "text", "text": content}] if isinstance(content, str) else []
    )
    events: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "tool_use":
            args = block.get("input")
            events.append({"kind": "tool_call", "name": str(block.get("name") or ""),
                           "args": args if isinstance(args, dict) else {}})
        elif kind == "tool_result":
            events.append({"kind": "tool_result", "output": _flatten(block.get("content"))})
        elif kind == "text" and str(block.get("text") or "").strip():
            events.append({"kind": "prompt" if role == "user" else "assistant_text",
                           "text": str(block["text"])})
    return events


def _antigravity_events(row: dict) -> list[dict]:
    row_type = str(row.get("type") or "").upper()
    events: list[dict] = []
    if row_type in ("USER_INPUT", "USER_MESSAGE", "USER"):
        text = str(row.get("content") or "")
        if text.strip():
            events.append({"kind": "prompt", "text": text})
    elif row_type in ("PLANNER_RESPONSE", "MODEL", "ASSISTANT"):
        calls = row.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict) or not call.get("name"):
                    continue
                args = call.get("args")
                events.append({"kind": "tool_call", "name": str(call["name"]),
                               "args": args if isinstance(args, dict) else {}})
        text = str(row.get("content") or "")
        if text.strip():
            events.append({"kind": "assistant_text", "text": text})
    elif row_type in ("GENERIC", "TOOL_RESULT", "TOOL_OUTPUT", "RUN_COMMAND"):
        events.append({"kind": "tool_result", "output": str(row.get("content") or "")})
    return events


def read_events(path: str) -> list[dict]:
    """Normalized events for the whole transcript, in order."""
    events: list[dict] = []
    for row in read_rows(path):
        events.extend(_claude_events(row))
        events.extend(_antigravity_events(row))
    return events


def turn_slice(events: list[dict]) -> tuple[str, list[dict]]:
    """The final user request and everything that happened after it.

    Exposed because a stop gate's honest baseline is *this* slice: the turn's own
    events, not the whole session file. Comparing one turn's state against a 46 MB
    session would flatter the gate by three orders of magnitude.
    """
    last_prompt = -1
    for index, event in enumerate(events):
        if event.get("kind") == "prompt":
            last_prompt = index
    if last_prompt < 0:
        return "", list(events)
    return str(events[last_prompt].get("text") or ""), events[last_prompt + 1:]


def turn_facts(events: list[dict]) -> dict:
    """What the final turn asked for, changed, checked, and claimed.

    `verified` is the one fact that makes the gate cheap: a passing check that ran
    after the last edit means there is nothing to ask, so most turns cost nothing.
    A check counts as passing only when its *output* says so — a failing suite is
    recorded with no error flag on both hosts, so exit status alone would lie.
    """
    task, turn = turn_slice(events)

    file_changes = 0
    last_mutation = -1
    checks: list[dict] = []
    final_message = ""
    for index, event in enumerate(turn):
        kind = event.get("kind")
        if kind == "tool_call":
            name = str(event.get("name") or "")
            command = gates.extract_command(event.get("args") or {})
            if gates.is_mutating_tool(name):
                file_changes += 1
                last_mutation = index
            elif command and gates.looks_like_check(command):
                output = ""
                for following in turn[index + 1:index + 4]:
                    if following.get("kind") == "tool_result":
                        output = str(following.get("output") or "")
                        break
                checks.append({"command": command,
                               "outcome": gates.check_outcome(output),
                               "index": index})
        elif kind == "assistant_text":
            final_message = str(event.get("text") or "")

    return {
        "task": task,
        "final_message": final_message,
        "file_changes": file_changes,
        "checks": checks,
        "verified": any(c["index"] > last_mutation and c["outcome"] == "passed"
                        for c in checks),
        "claims": bool(gates.DONE_HINT.search(final_message)),
    }
