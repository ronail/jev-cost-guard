#!/usr/bin/env python3
"""Stop gate: does the closing message claim work the turn did not do?

Runs on `Stop` in Claude Code / Codex and on `Stop` in Antigravity, where the
decision is "continue" instead of "block". The question is one `noul`:

    does the message claim completion in a way the run facts do not support?

The run facts are the point. A published calibration over 1,500 real stops put the
wording-only rule at AUROC 0.50 — a coin flip — while judging the same messages with
the turn's run facts in front of it reached 0.976. So this hook establishes the facts
locally first, in the same order every shipped implementation uses:

  1. no edits this turn            -> nothing to verify, stop is allowed (no request)
  2. a passing check after the last edit -> verified, stop is allowed (no request)
  3. the message claims nothing    -> not a completion claim, allowed (no request)
  4. otherwise                     -> one jev request, and a block only if the answer
                                      is both likely and decisive

Guards, because a completion gate that loops is worse than one that misses: a 60 s
cooldown and at most 3 blocks per session, tracked per conversation. Audit mode is
the default and never blocks. Every failure path allows the stop.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from typing import NoReturn

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from lib import gates, transcript  # noqa: E402  (hooks/ is on sys.path above)

SERVER = os.environ.get("JEV_SERVER_PATH") or os.path.join(
    REPO_ROOT, "mcp", "jev", "server.py"
)
POLICY_PATH = os.environ.get("JEV_GATE_POLICY") or os.path.join(HERE, "gate-policy.json")
STATE_DIR = os.environ.get("JEV_GATE_STATE_DIR") or os.path.join(
    tempfile.gettempdir(), "jev-gate"
)
TIMEOUT = float(os.environ.get("JEV_GATE_TIMEOUT_S", "20"))


def load_policy() -> dict:
    """Same policy file as the pre-tool gate, with the same env overrides."""
    raw: dict = {}
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            raw = loaded
    except Exception:
        raw = {}
    raw.pop("_comment", None)
    env_policy: dict = {}
    if os.environ.get("JEV_GATE_MODE"):
        env_policy["mode"] = os.environ["JEV_GATE_MODE"]
    thresholds = {}
    for key, env_name in (
        ("confidence", "JEV_GATE_THRESHOLD_CONFIDENCE"),
        ("unverified", "JEV_GATE_THRESHOLD_UNVERIFIED"),
    ):
        if os.environ.get(env_name):
            try:
                thresholds[key] = float(os.environ[env_name])
            except ValueError:
                pass
    if thresholds:
        env_policy["thresholds"] = thresholds
    return gates.merge_policy(raw, env_policy)


def _state_dir() -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return STATE_DIR


def _guard_path(session: str) -> str:
    safe = "".join(ch for ch in session if ch.isalnum() or ch in "-_")[:80] or "unknown"
    return os.path.join(_state_dir(), f"stop-{safe}.json")


def read_guard(session: str) -> dict:
    try:
        with open(_guard_path(session), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_guard(session: str, guard: dict) -> None:
    try:
        with open(_guard_path(session), "w", encoding="utf-8") as handle:
            json.dump(guard, handle)
    except Exception:
        pass


def ask_jev(state: str) -> dict | None:
    try:
        proc = subprocess.run(
            [sys.executable, SERVER, "--ask", "--questions", json.dumps(gates.COMPLETION_QUESTIONS)],
            input=state, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except Exception as error:  # timeout, missing server, unreadable exec
        sys.stderr.write(f"[jev-gate] stop: skipped ({error})\n")
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not payload.get("ok"):
        return None
    answers = payload.get("answers")
    return answers if isinstance(answers, dict) else None


def audit(record: dict) -> None:
    try:
        with open(os.path.join(_state_dir(), "decisions.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def emit(host: str, reason: str) -> None:
    """`block` on Claude Code / Codex, `continue` on Antigravity."""
    if host == "antigravity":
        print(json.dumps({"decision": "continue", "reason": f"jev: {reason}"}))
    else:
        print(json.dumps({"decision": "block", "reason": f"jev: {reason}"}))


DEBUG = os.environ.get("JEV_GATE_DEBUG") == "1"


def skip(reason: str) -> NoReturn:
    """Leave the stop alone, saying why when JEV_GATE_DEBUG=1.

    Silence is correct for the harness, but "the gate never fires" is the hardest
    failure to diagnose, so the reason is available on demand instead of inferred.
    """
    if DEBUG:
        sys.stderr.write(f"[jev-gate] stop: skipped ({reason})\n")
    sys.exit(0)


def main() -> None:
    started = time.time()
    try:
        event = json.load(sys.stdin)
    except Exception:
        skip("unreadable stdin")
    if not isinstance(event, dict):
        skip("stdin is not a hook envelope")

    host = "antigravity" if "terminationReason" in event or "transcriptPath" in event else \
        os.environ.get("JEV_GATE_HOST", "claude")
    if host == "antigravity":
        # A crashed or truncated run is not a completion claim, and a session that is
        # not idle still has work in flight.
        if str(event.get("terminationReason") or "").lower() in ("error", "max_steps_exceeded"):
            skip(f"terminationReason={event.get('terminationReason')}")
        if event.get("fullyIdle") is False:
            skip("session is not idle")
    elif event.get("stop_hook_active"):
        skip("stop_hook_active: a block already happened")  # never chain them

    session = str(event.get("session_id") or event.get("conversationId") or "unknown")
    path = str(event.get("transcript_path") or event.get("transcriptPath") or "")
    facts = transcript.turn_facts(transcript.read_events(path))
    if not facts["final_message"]:
        fallback = event.get("last_assistant_message")
        facts["final_message"] = str(fallback or "")
    if not facts["claims"]:
        skip("closing message claims no completion")
    if facts["file_changes"] == 0:
        skip("no edits this turn")
    if facts["verified"]:
        skip("a check passed after the last edit")

    policy = load_policy()
    stop_policy = policy.get("stop") or {}
    cooldown = float(stop_policy.get("cooldownMs", 60_000)) / 1000.0
    max_blocks = int(stop_policy.get("maxBlocksPerSession", 3))
    guard = read_guard(session)
    blocks = int(guard.get("blocks") or 0)
    if blocks >= max_blocks:
        skip(f"per-session cap reached ({blocks}/{max_blocks})")
    if time.time() - float(guard.get("last") or 0) < cooldown:
        skip("cooldown since the last block")

    state = gates.build_completion_state(facts["task"], facts["final_message"], {
        "file_changes": facts["file_changes"],
        "checks_run": [f"{c['command'][:80]} -> {c['outcome']}" for c in facts["checks"]],
    })
    answers = ask_jev(state)
    if answers is None:
        skip("jev gave no usable answer")  # fail open: an unreachable judge never traps a session
    decision, reason = gates.decide_completion(answers, policy)
    audit({
        "gate": "stop",
        "ts": round(time.time(), 3),
        "host": host,
        "session": session[:64],
        "decision": decision,
        "reason": reason,
        "mode": policy.get("mode", "audit"),
        "run": {"file_changes": facts["file_changes"],
                "checks": [f"{c['command'][:60]} -> {c['outcome']}" for c in facts["checks"]]},
        "answers": answers,
        "latency_ms": round((time.time() - started) * 1000),
    })
    if policy.get("mode", "audit") == "audit":
        skip(f"audit mode (would be {decision or 'allow'})")
    if decision != "block":
        skip(f"answer did not support a block ({reason})")
    write_guard(session, {"blocks": blocks + 1, "last": time.time()})
    emit(host, reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
