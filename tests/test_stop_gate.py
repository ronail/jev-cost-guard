"""The stop gate: run facts first, one jev question second, a loop guard last.

The behaviour under test is the order, not the model. Three facts let the hook answer
locally and for free — nothing changed, a passing check after the last change, the
message claims nothing — and only the fourth case spends a request. Then the answer
must be both *likely* (noul >= 0.70) and *decisive* (confidence >= 0.70) to block, and
the guards (cooldown, per-session cap) must win over a repeated verdict.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "stop-gate.py"
sys.path.insert(0, str(ROOT / "hooks"))

from lib import gates, transcript  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def claude_transcript(path: Path, *, edit: bool = True, check: bool = False,
                      check_output: str = "12 passed in 0.4s",
                      claim: str = "Done. Renamed parseRows in both files.") -> Path:
    rows: list[dict] = [{"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "rename parseRows and make the tests pass"}]}}]
    if edit:
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.mjs"}}]}})
        rows.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "ok"}]}]}})
    if check:
        rows.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}]}})
        rows.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": check_output}]}]}})
    rows.append({"type": "assistant", "message": {"role": "assistant",
                                                 "content": [{"type": "text", "text": claim}]}})
    return write_jsonl(path, rows)


def antigravity_transcript(path: Path, *, edit: bool = True,
                           claim: str = "Fixed the bug. All done.") -> Path:
    rows = [{"step_index": 0, "type": "USER_INPUT", "content": "fix the parser bug"}]
    if edit:
        rows.append({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "",
                     "tool_calls": [{"name": "replace_file_content",
                                     "args": {"TargetFile": "/w/parser.py"}}]})
        rows.append({"step_index": 2, "type": "GENERIC", "content": "edited"})
    rows.append({"step_index": 3, "type": "PLANNER_RESPONSE", "content": claim})
    return write_jsonl(path, rows)


# ── the facts ───────────────────────────────────────────────────────────────


def test_claude_code_transcript_yields_the_turn_facts(tmp_path):
    facts = transcript.turn_facts(transcript.read_events(str(
        claude_transcript(tmp_path / "t.jsonl"))))
    assert facts["task"].startswith("rename parseRows")
    assert facts["file_changes"] == 1
    assert facts["checks"] == []
    assert facts["verified"] is False
    assert facts["claims"] is True


def test_a_passing_check_after_the_edit_verifies_the_turn(tmp_path):
    facts = transcript.turn_facts(transcript.read_events(str(
        claude_transcript(tmp_path / "t.jsonl", check=True))))
    assert facts["checks"][0]["outcome"] == "passed"
    assert facts["verified"] is True


def test_a_failing_check_does_not_verify_even_with_a_tidy_summary(tmp_path):
    facts = transcript.turn_facts(transcript.read_events(str(
        claude_transcript(tmp_path / "t.jsonl", check=True,
                          check_output="3 failed, 40 passed"))))
    assert facts["checks"][0]["outcome"] == "failed"
    assert facts["verified"] is False


def test_a_check_before_the_edit_does_not_count(tmp_path):
    path = write_jsonl(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "fix it"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "9 passed"}]}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"path": "a.py"}}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Fixed and done."}]}},
    ])
    facts = transcript.turn_facts(transcript.read_events(str(path)))
    assert facts["verified"] is False        # the pass predates the change


def test_antigravity_transcript_yields_the_turn_facts(tmp_path):
    facts = transcript.turn_facts(transcript.read_events(str(
        antigravity_transcript(tmp_path / "t.jsonl"))))
    assert facts["file_changes"] == 1
    assert facts["verified"] is False
    assert facts["claims"] is True
    assert "All done" in facts["final_message"]


def test_unreadable_or_unknown_rows_are_ignored(tmp_path):
    assert transcript.read_events(str(tmp_path / "missing.jsonl")) == []
    path = tmp_path / "t.jsonl"
    path.write_text('{"type":"whatever"}\nnot json\n')
    assert transcript.turn_facts(transcript.read_events(str(path)))["file_changes"] == 0


@pytest.mark.parametrize("name", ["Edit", "write_to_file", "replace_file_content",
                                  "apply_patch", "multi_replace_file_content"])
def test_mutating_tool_names_from_both_hosts(name):
    assert gates.is_mutating_tool(name)


def test_failure_in_a_mixed_summary_wins():
    assert gates.check_outcome("40 passed, 3 failed") == "failed"
    assert gates.check_outcome("BUILD SUCCESSFUL") == "passed"
    assert gates.check_outcome("something happened") == "unknown"


# ── the decision ────────────────────────────────────────────────────────────


def policy(**overrides):
    return gates.merge_policy(overrides or None)


def test_likely_and_decisive_blocks():
    answers = {"unverified": {"noul": 0.86, "confidence": 0.8}}
    decision, reason = gates.decide_completion(answers, policy())
    assert decision == "block"
    assert "Verify before stopping" in reason


def test_likely_but_not_decisive_does_not_block():
    assert gates.decide_completion({"unverified": {"noul": 0.86, "confidence": 0.55}},
                                   policy())[0] is None


def test_unlikely_does_not_block():
    assert gates.decide_completion({"unverified": {"noul": 0.4, "confidence": 0.9}},
                                   policy())[0] is None


def test_malformed_completion_answer_is_no_opinion():
    assert gates.decide_completion({}, policy())[0] is None
    assert gates.decide_completion({"unverified": {"noul": "yes"}}, policy())[0] is None


# ── the hook ────────────────────────────────────────────────────────────────


CLAIMS_UNVERIFIED = {"ok": True, "model": "jev-1.13.0",
                     "endpoint": "https://api.typesafe.ai/v1/systemone",
                     "answers": {"unverified": {"noul": 0.86, "confidence": 0.8}}}
LOOKS_FINE = {"ok": True, "model": "jev-1.13.0",
              "endpoint": "https://api.typesafe.ai/v1/systemone",
              "answers": {"unverified": {"noul": 0.2, "confidence": 0.9}}}


@pytest.fixture()
def stub(tmp_path):
    def make(payload: dict | None = None, exit_code: int = 0):
        script = tmp_path / "stub_ask.py"
        log = tmp_path / "calls.jsonl"
        body = json.dumps(payload) if payload is not None else "{}"
        script.write_text(
            "import json,sys,pathlib\n"
            "data=sys.stdin.read()\n"
            f"pathlib.Path({str(log)!r}).open('a').write(json.dumps({{'state':data}})+'\\n')\n"
            f"print({body!r})\n"
            f"sys.exit({exit_code})\n"
        )
        return script, log
    return make


def run_hook(event: dict, *, server: Path, state: Path, policy_file: Path | None = None,
             extra_env: dict | None = None):
    env = dict(os.environ)
    env["JEV_SERVER_PATH"] = str(server)
    env["JEV_GATE_STATE_DIR"] = str(state)
    env.pop("JEV_GATE_HOST", None)
    env.pop("JEV_GATE_MODE", None)
    if policy_file is not None:
        env["JEV_GATE_POLICY"] = str(policy_file)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event),
                          capture_output=True, text=True, env=env, timeout=60)


def enforce_policy(tmp_path: Path, **stop) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(gates.merge_policy({
        "mode": "enforce",
        "stop": {"cooldownMs": 0, "maxBlocksPerSession": 3, **stop},
    })))
    return path


def test_nothing_changed_is_never_asked(tmp_path, stub):
    server, log = stub(CLAIMS_UNVERIFIED)
    event = {"session_id": "s", "transcript_path": str(
        claude_transcript(tmp_path / "t.jsonl", edit=False))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert result.stdout == ""
    assert not log.exists()


def test_a_verified_turn_is_never_asked(tmp_path, stub):
    server, log = stub(CLAIMS_UNVERIFIED)
    event = {"session_id": "s", "transcript_path": str(
        claude_transcript(tmp_path / "t.jsonl", check=True))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert result.stdout == ""
    assert not log.exists()


def test_a_message_that_claims_nothing_is_never_asked(tmp_path, stub):
    server, log = stub(CLAIMS_UNVERIFIED)
    event = {"session_id": "s", "transcript_path": str(
        claude_transcript(tmp_path / "t.jsonl", claim="Which file should I open first?"))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert result.stdout == ""
    assert not log.exists()


def test_audit_mode_judges_and_logs_without_blocking(tmp_path, stub):
    server, log = stub(CLAIMS_UNVERIFIED)
    state = tmp_path / "st"
    event = {"session_id": "s1", "transcript_path": str(claude_transcript(tmp_path / "t.jsonl"))}
    result = run_hook(event, server=server, state=state, policy_file=tmp_path / "none.json")
    assert result.stdout == ""
    assert log.exists()
    record = json.loads((state / "decisions.jsonl").read_text().splitlines()[-1])
    assert record["gate"] == "stop"
    assert record["decision"] == "block"
    assert record["mode"] == "audit"
    assert record["run"]["file_changes"] == 1


def test_enforce_blocks_a_claude_code_stop(tmp_path, stub):
    server, _ = stub(CLAIMS_UNVERIFIED)
    event = {"session_id": "s2", "transcript_path": str(claude_transcript(tmp_path / "t.jsonl"))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert payload["reason"].startswith("jev:")


def test_enforce_continues_an_antigravity_stop(tmp_path, stub):
    server, _ = stub(CLAIMS_UNVERIFIED)
    event = {"conversationId": "c1", "terminationReason": "NO_TOOL_CALL", "fullyIdle": True,
             "transcriptPath": str(antigravity_transcript(tmp_path / "t.jsonl"))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert json.loads(result.stdout)["decision"] == "continue"


def test_a_supported_claim_is_left_alone(tmp_path, stub):
    server, _ = stub(LOOKS_FINE)
    event = {"session_id": "s3", "transcript_path": str(claude_transcript(tmp_path / "t.jsonl"))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert result.stdout == ""


def test_cooldown_suppresses_a_second_stop(tmp_path, stub):
    server, log = stub(CLAIMS_UNVERIFIED)
    policy_file = enforce_policy(tmp_path, cooldownMs=60000)
    state = tmp_path / "st"
    event = {"session_id": "s4", "transcript_path": str(claude_transcript(tmp_path / "t.jsonl"))}
    run_hook(event, server=server, state=state, policy_file=policy_file)
    run_hook(event, server=server, state=state, policy_file=policy_file)
    assert len(log.read_text().splitlines()) == 1


def test_per_session_cap_suppresses_further_stops(tmp_path, stub):
    server, log = stub(CLAIMS_UNVERIFIED)
    policy_file = enforce_policy(tmp_path, maxBlocksPerSession=1)
    state = tmp_path / "st"
    event = {"session_id": "s5", "transcript_path": str(claude_transcript(tmp_path / "t.jsonl"))}
    run_hook(event, server=server, state=state, policy_file=policy_file)
    run_hook(event, server=server, state=state, policy_file=policy_file)
    assert len(log.read_text().splitlines()) == 1


def test_an_active_stop_hook_is_respected(tmp_path, stub):
    server, log = stub(CLAIMS_UNVERIFIED)
    event = {"session_id": "s6", "stop_hook_active": True,
             "transcript_path": str(claude_transcript(tmp_path / "t.jsonl"))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert result.stdout == ""
    assert not log.exists()


@pytest.mark.parametrize("event_extra", [
    {"terminationReason": "error"},
    {"terminationReason": "max_steps_exceeded"},
    {"terminationReason": "NO_TOOL_CALL", "fullyIdle": False},
])
def test_antigravity_skips_stops_that_are_not_completions(tmp_path, stub, event_extra):
    server, log = stub(CLAIMS_UNVERIFIED)
    event = {"conversationId": "c9",
             "transcriptPath": str(antigravity_transcript(tmp_path / "t.jsonl")),
             **event_extra}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert result.stdout == ""
    assert not log.exists()


def test_dead_server_allows_the_stop(tmp_path, stub):
    server, _ = stub(None, exit_code=1)
    event = {"session_id": "s7", "transcript_path": str(claude_transcript(tmp_path / "t.jsonl"))}
    result = run_hook(event, server=server, state=tmp_path / "st",
                      policy_file=enforce_policy(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


# ── wiring ──────────────────────────────────────────────────────────────────


def test_claude_code_and_codex_wire_stop():
    wiring = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    commands = [h["command"] for entry in wiring["hooks"]["Stop"] for h in entry["hooks"]]
    assert any("stop-gate.py" in c for c in commands)


def test_antigravity_wires_stop_group():
    wiring = json.loads((ROOT / "hooks.json").read_text())
    # PreToolUse/PostToolUse are matcher-wrapped; Stop is a flat handler array.
    assert wiring["jev-stop-gate"]["Stop"][0]["command"].endswith('hooks/stop-gate.py"')


def test_the_observed_antigravity_write_tool_name_counts_as_a_mutation():
    """Measured on agy 1.2.12: the docs say write_to_file, the transcript says write_file."""
    assert gates.is_mutating_tool("write_file")
    assert gates.is_mutating_tool("default_api:replace_file_content")
    assert gates.is_mutating_tool("some_new_edit_tool")
    assert not gates.is_mutating_tool("read_file")
    assert not gates.is_mutating_tool("jev_find")


def test_turn_slice_is_the_baseline_the_gate_saves_against():
    """A stop gate's honest baseline is the turn, not the session file."""
    events = [
        {"kind": "prompt", "text": "first request"},
        {"kind": "assistant_text", "text": "a"},
        {"kind": "prompt", "text": "second request"},
        {"kind": "tool_call", "name": "write_file", "args": {"path": "a.txt"}},
        {"kind": "assistant_text", "text": "Done, it works."},
    ]
    task, turn = transcript.turn_slice(events)
    assert task == "second request"
    assert [e["kind"] for e in turn] == ["tool_call", "assistant_text"]


def test_turn_slice_without_a_prompt_takes_everything():
    task, turn = transcript.turn_slice([{"kind": "assistant_text", "text": "hi"}])
    assert task == "" and len(turn) == 1
