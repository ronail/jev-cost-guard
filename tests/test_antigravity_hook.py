"""The Antigravity gate: judge a tool result from the transcript, inject one line.

Antigravity's `PostToolUse` carries no tool output, so the gate runs on
`PreInvocation` and reads the transcript the event points at. What matters here:
the largest unjudged result is judged exactly once, a result is only taken when a
model turn carried `tool_calls` before it, jev's own verdicts are never judged,
and every failure path prints nothing and exits 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "antigravity-jev-verdict.py"

VERDICT = {
    "ok": True,
    "model": "jev-1.13.0",
    "endpoint": "https://api.typesafe.ai/v1/systemone",
    "noul": 0.07,
    "interpretation": "likely-failure",
}


@pytest.fixture()
def stub_server(tmp_path):
    """A stand-in for server.py --verdict that records what it was given."""
    script = tmp_path / "stub_server.py"
    log = tmp_path / "called.json"
    script.write_text(
        "import json,sys,pathlib\n"
        "data=sys.stdin.read()\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps({{'chars': len(data)}}))\n"
        f"print(json.dumps({VERDICT!r}))\n"
    )
    return script, log


def _model_turn(step: int, *calls: dict) -> dict:
    return {"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE",
            "status": "DONE", "tool_calls": list(calls)}


def _tool_result(step: int, text: str) -> dict:
    return {"step_index": step, "source": "MODEL", "type": "GENERIC",
            "status": "DONE", "content": text}


def _write_transcript(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _run(stub, event, *, env=None):
    srv, log = stub
    full_env = {k: v for k, v in os.environ.items()
                if not k.startswith(("JEV_", "TYPESAFE_", "OPENCODE_", "CLAUDE_PLUGIN_OPTION"))}
    full_env["JEV_SERVER_PATH"] = str(srv)
    full_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(event), capture_output=True, text=True, env=full_env, timeout=60,
    )
    calls = json.loads(log.read_text()) if log.exists() else None
    return proc, calls


def _event(transcript: Path, conversation: str = "conv-1") -> dict:
    return {
        "conversationId": conversation,
        "transcriptPath": str(transcript),
        "workspacePaths": ["/ws"],
        "invocationNum": 3,
        "initialNumSteps": 4,
        "modelName": "gemini-3.8-flash-high",
    }


def test_large_tool_result_is_judged_and_injected(tmp_path, stub_server):
    transcript = _write_transcript(tmp_path / "t.jsonl", [
        {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>run tests"},
        _model_turn(1, {"name": "run_command", "args": {"CommandLine": "pytest -q"}}),
        _tool_result(2, "x" * 5000),
        _model_turn(3),
    ])
    proc, calls = _run(stub_server, _event(transcript),
                       env={"JEV_AGY_STATE_DIR": str(tmp_path / "state")})
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    line = out["injectSteps"][0]["ephemeralMessage"]
    assert "noul=0.07" in line and "likely-failure" in line and "5000 chars" in line
    assert "run_command" in line
    assert "unchanged" in line, "the line must stay advisory"
    assert calls is not None and calls["chars"] >= 5000, "the server really saw the body"


def test_the_same_result_is_not_judged_twice(tmp_path, stub_server):
    """PreInvocation fires before every model call; the gate must not re-bill."""
    transcript = _write_transcript(tmp_path / "t.jsonl", [
        _model_turn(1, {"name": "run_command", "args": {}}),
        _tool_result(2, "y" * 5000),
    ])
    state = str(tmp_path / "state")
    first, calls_first = _run(stub_server, _event(transcript),
                              env={"JEV_AGY_STATE_DIR": state})
    assert json.loads(first.stdout)["injectSteps"]
    second, _ = _run(stub_server, _event(transcript), env={"JEV_AGY_STATE_DIR": state})
    assert second.returncode == 0 and second.stdout.strip() == ""


def test_small_result_is_not_judged(tmp_path, stub_server):
    transcript = _write_transcript(tmp_path / "t.jsonl", [
        _model_turn(1, {"name": "run_command", "args": {}}),
        _tool_result(2, "ok"),
    ])
    proc, calls = _run(stub_server, _event(transcript),
                       env={"JEV_AGY_STATE_DIR": str(tmp_path / "state")})
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    assert calls is None


def test_jev_own_mcp_results_are_never_judged(tmp_path, stub_server):
    transcript = _write_transcript(tmp_path / "t.jsonl", [
        _model_turn(1, {"name": "call_mcp_tool",
                        "args": {"ServerName": "jev-cost-guard_jev", "ToolName": "jev_find"}}),
        _tool_result(2, "z" * 9000),
    ])
    proc, calls = _run(stub_server, _event(transcript),
                       env={"JEV_AGY_STATE_DIR": str(tmp_path / "state")})
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    assert calls is None


def test_model_prose_is_not_treated_as_a_tool_result(tmp_path, stub_server):
    """A GENERIC row with no preceding tool call is text, not output."""
    transcript = _write_transcript(tmp_path / "t.jsonl", [
        _tool_result(2, "w" * 9000),
    ])
    proc, calls = _run(stub_server, _event(transcript),
                       env={"JEV_AGY_STATE_DIR": str(tmp_path / "state")})
    assert proc.stdout.strip() == ""
    assert calls is None


def test_biggest_of_several_results_wins_and_only_one_call_is_made(tmp_path, stub_server):
    transcript = _write_transcript(tmp_path / "t.jsonl", [
        _model_turn(1, {"name": "run_command", "args": {}}),
        _tool_result(2, "a" * 4100),
        _model_turn(3, {"name": "read_file", "args": {"Path": "/x"}}),
        _tool_result(4, "b" * 8000),
    ])
    proc, calls = _run(stub_server, _event(transcript),
                       env={"JEV_AGY_STATE_DIR": str(tmp_path / "state")})
    line = json.loads(proc.stdout)["injectSteps"][0]["ephemeralMessage"]
    assert "8000 chars" in line and "read_file" in line
    assert calls is not None and calls["chars"] == 8000, "exactly one jev call per invocation"


def test_missing_transcript_is_silent(tmp_path, stub_server):
    proc, calls = _run(stub_server, _event(tmp_path / "nope.jsonl"),
                       env={"JEV_AGY_STATE_DIR": str(tmp_path / "state")})
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    assert calls is None


def test_failing_server_is_silent_and_marks_the_step(tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text("import sys; sys.exit(3)\n")
    transcript = _write_transcript(tmp_path / "t.jsonl", [
        _model_turn(1, {"name": "run_command", "args": {}}),
        _tool_result(2, "x" * 9000),
    ])
    state = str(tmp_path / "state")
    proc, _ = _run((broken, tmp_path / "unused.json"), _event(transcript),
                   env={"JEV_AGY_STATE_DIR": state})
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    marker = json.loads((tmp_path / "state" / "conv-1").read_text())
    assert marker["last_step"] == 2, "a failed judgement must not be retried forever"


def test_junk_stdin_exits_clean(stub_server, tmp_path):
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("JEV_")}
    full_env["JEV_SERVER_PATH"] = str(stub_server[0])
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                          capture_output=True, text=True, env=full_env, timeout=60)
    assert proc.returncode == 0 and proc.stdout.strip() == ""
