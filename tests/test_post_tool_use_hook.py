"""The PostToolUse gate: same script, two harness envelopes.

Codex sends `tool_response`, Claude Code sends `tool_result`, and Codex names its
shell tool through the same `Bash` matcher while edits arrive as `apply_patch`.
The hook must produce one advisory line for both, skip jev's own tools, and never
break the turn when jev is unreachable.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "claude-codex-jev-verdict.py"

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


def _run(stub, event, *, env=None, mode=0o600):
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


def test_codex_envelope_yields_additional_context(stub_server):
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"output": [{"type": "input_text", "text": "x" * 5000}]},
    }
    proc, calls = _run(stub_server, event)
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "noul=0.07" in ctx and "likely-failure" in ctx and "5000 chars" in ctx
    assert calls is not None and calls["chars"] >= 5000  # the server really received it


def test_claude_envelope_still_works(stub_server):
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_result": {"output": "y" * 5000, "stderr": ""},
    }
    proc, calls = _run(stub_server, event)
    assert proc.returncode == 0
    assert "likely-failure" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert calls is not None and calls["chars"] >= 5000


def test_small_output_is_not_judged(stub_server):
    event = {"tool_name": "Bash", "tool_response": {"output": "ok" * 10}}
    proc, calls = _run(stub_server, event)
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    assert calls is None  # jev never called


def test_jev_own_tools_are_never_gated(stub_server):
    for name in ("mcp__jev__jev_find", "jev_summarize", "mcp__plugin_jev-cost-guard_jev__jev_screen"):
        event = {"tool_name": name, "tool_response": {"output": "z" * 9000}}
        proc, calls = _run(stub_server, event)
        assert proc.returncode == 0 and proc.stdout.strip() == ""
        assert calls is None, name


def test_reports_context_even_when_response_shape_is_unknown(stub_server):
    event = {"tool_name": "apply_patch", "tool_response": {"stdout": "w" * 4100, "exit_code": 1}}
    proc, _ = _run(stub_server, event)
    assert proc.returncode == 0
    assert "likely-failure" in proc.stdout


def test_failing_server_is_silent_and_harmless(tmp_path):
    broken = tmp_path / "broken.py"
    broken.write_text("import sys; sys.exit(3)\n")
    event = {"tool_name": "Bash", "tool_response": {"output": "x" * 9000}}
    proc, _ = _run((broken, tmp_path / "unused.json"), event)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_tool_name_exits_clean(stub_server):
    proc, calls = _run(stub_server, {"tool_response": {"output": "x" * 9000}})
    assert proc.returncode == 0 and proc.stdout.strip() == ""
    assert calls is None


def test_no_key_file_left_behind(tmp_path, stub_server):
    """The hook only reads a credential file; it never creates one."""
    proc, _ = _run(stub_server, {"tool_name": "Bash", "tool_response": {"output": "x" * 5000}},
                   env={"CODEX_HOME": str(tmp_path / "codexhome")})
    assert proc.returncode == 0
    assert not (tmp_path / "codexhome").exists()
