"""The Antigravity skill hint: deterministic roster ranking, one line, opt-in.

The hook ships disabled in `hooks.json`; these tests drive it directly so the logic is
covered even while the shipped default is off.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "antigravity-jev-route.py"


def _transcript(tmp_path: Path, request: str, name: str = "t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({
        "step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
        "status": "DONE",
        "content": f"<USER_REQUEST>\n{request}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nnow\n</ADDITIONAL_METADATA>",
    }) + "\n", encoding="utf-8")
    return path


def _run(transcript: Path, *, conversation: str = "conv-1", invocation: int = 0,
         state: Path | None = None, extra_env: dict | None = None):
    event = {
        "conversationId": conversation,
        "transcriptPath": str(transcript),
        "workspacePaths": [],
        "invocationNum": invocation,
        "initialNumSteps": 1,
        "modelName": "gemini-3.8-flash-high",
    }
    env = {k: v for k, v in os.environ.items() if not k.startswith("JEV_")}
    env["JEV_AGY_ROUTE_STATE_DIR"] = str(state or transcript.parent / "state")
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(event),
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_matching_request_gets_a_hint(tmp_path):
    # "triage test output bloat" should match the jev-test-output-triage skill.
    proc = _run(_transcript(tmp_path, "these test logs are huge, triage the output bloat"))
    assert proc.returncode == 0, proc.stderr
    line = json.loads(proc.stdout)["injectSteps"][0]["ephemeralMessage"]
    assert "`jev-test-output-triage`" in line
    assert "ignore this line otherwise" in line, "advisory only"


def test_unrelated_request_stays_silent(tmp_path):
    proc = _run(_transcript(tmp_path, "please translate this poem into french"))
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_one_hint_per_conversation(tmp_path):
    state = tmp_path / "state"
    transcript = _transcript(tmp_path, "triage the huge test output bloat")
    first = _run(transcript, state=state)
    assert json.loads(first.stdout)["injectSteps"]
    second = _run(transcript, state=state)
    assert second.returncode == 0 and second.stdout.strip() == ""


def test_later_invocations_are_left_alone(tmp_path):
    proc = _run(_transcript(tmp_path, "triage the huge test output bloat"), invocation=3)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_missing_transcript_is_silent(tmp_path):
    proc = _run(tmp_path / "nope.jsonl")
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_ships_disabled():
    hooks = json.loads((REPO / "hooks.json").read_text())
    assert hooks["jev-skill-hint"]["enabled"] is False, "costs a hint per session: opt in"
    assert hooks["jev-verdict"].get("enabled", True) is True, "the gate ships on"
    assert hooks["jev-verdict"]["PreInvocation"][0]["command"].endswith(
        "antigravity-jev-verdict.py\""
    )
