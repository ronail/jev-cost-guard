#!/usr/bin/env python3
"""Pre-tool permission gate: ask jev whether a shell command should just run.

Runs on `PreToolUse` in Claude Code, Codex and Antigravity, and answers in each
host's own vocabulary. Three layers, cheapest first:

  tier 0  deterministic belts (`hooks/lib/gates.py: RISK_BELTS`) decide whether the
          command is worth asking about at all. Most commands never reach tier 1,
          which is why this hook costs nothing on a normal turn.
  tier 1  one jev request carrying two questions: a `choice` risk judgement
          (allow / ask / deny) and a `noul` "destroys state outside the working
          tree?" cross-check.
  tier 2  a per-session cache keyed by the command, plus a hard cap on the state
          size — an oversized command is answered `ask` locally, because judging a
          head and a tail is not judging the command.

Two invariants, both taken from published implementations rather than invented here:

  * **Jev may only narrow.** A confident `allow` passes through to the harness's
    own permission flow; it never grants a permission the harness would have
    prompted for. `allowMode: "allow"` opts in to widening and should stay off.
  * **`mode: "audit"` (the default) never touches the turn.** It judges for real,
    writes one JSONL line, and lets the tool run. Enforce only after the log shows
    a false-positive rate you accept.

Fail-open on every path: no key, timeout, bad JSON, malformed answer, missing
server — the hook exits 0 with no output and the harness's own rules apply.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from lib import gates  # noqa: E402  (hooks/ is on sys.path above)

SERVER = os.environ.get("JEV_SERVER_PATH") or os.path.join(
    REPO_ROOT, "mcp", "jev", "server.py"
)
POLICY_PATH = os.environ.get("JEV_GATE_POLICY") or os.path.join(HERE, "gate-policy.json")
STATE_DIR = os.environ.get("JEV_GATE_STATE_DIR") or os.path.join(
    tempfile.gettempdir(), "jev-gate"
)
TIMEOUT = float(os.environ.get("JEV_GATE_TIMEOUT_S", "20"))


def load_policy() -> dict:
    """gate-policy.json, then env overrides (one file for every gate and host)."""
    raw: dict = {}
    try:
        with open(POLICY_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            raw = loaded
    except Exception:
        raw = {}
    # The policy file carries a comment key for humans; it is not a setting.
    raw.pop("_comment", None)
    env_policy: dict = {}
    mode = os.environ.get("JEV_GATE_MODE")
    if mode:
        env_policy["mode"] = mode
    thresholds = {}
    for key, env_name in (
        ("confidence", "JEV_GATE_THRESHOLD_CONFIDENCE"),
        ("destructive", "JEV_GATE_THRESHOLD_DESTRUCTIVE"),
        ("unverified", "JEV_GATE_THRESHOLD_UNVERIFIED"),
    ):
        if os.environ.get(env_name):
            try:
                thresholds[key] = float(os.environ[env_name])
            except ValueError:
                pass
    if thresholds:
        env_policy["thresholds"] = thresholds
    if os.environ.get("JEV_GATE_MAX_STATE_CHARS"):
        try:
            env_policy["maxStateChars"] = int(os.environ["JEV_GATE_MAX_STATE_CHARS"])
        except ValueError:
            pass
    return gates.merge_policy(raw, env_policy)


def detect(event: dict) -> tuple[str, str, dict, str, str]:
    """Host, tool name, tool args, session id, cwd out of a hook envelope."""
    call = event.get("toolCall")
    if isinstance(call, dict):  # Antigravity
        args = call.get("args")
        args = args if isinstance(args, dict) else {}
        return (
            "antigravity",
            str(call.get("name") or ""),
            args,
            str(event.get("conversationId") or ""),
            str(args.get("Cwd") or args.get("cwd") or ""),
        )
    # Claude Code and Codex share this half of the envelope.
    args = event.get("tool_input")
    args = args if isinstance(args, dict) else {}
    return (
        os.environ.get("JEV_GATE_HOST", "claude"),
        str(event.get("tool_name") or ""),
        args,
        str(event.get("session_id") or ""),
        str(event.get("cwd") or args.get("cwd") or ""),
    )


def _state_dir() -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return STATE_DIR


def read_cache(policy: dict) -> dict:
    try:
        with open(os.path.join(_state_dir(), "cache.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_cache(cache: dict, policy: dict) -> None:
    cache_policy = policy.get("cache") or {}
    prune_after = float(cache_policy.get("pruneAfterMs", cache_policy.get("ttlMs", 86_400_000))) / 1000.0
    max_entries = int(cache_policy.get("maxEntries", 2_000))
    now = time.time()
    fresh = {k: v for k, v in cache.items()
             if isinstance(v, dict) and now - float(v.get("ts", 0)) < prune_after}
    if len(fresh) > max_entries:  # keep the newest, bounded
        fresh = dict(sorted(fresh.items(), key=lambda kv: float(kv[1].get("ts", 0)), reverse=True)[:max_entries])

    try:
        with open(os.path.join(_state_dir(), "cache.json"), "w", encoding="utf-8") as handle:
            json.dump(fresh, handle)
    except Exception:
        pass


def ask_jev(state: str) -> dict | None:
    """One `--ask` request; None on any failure (the caller then fails open)."""
    try:
        proc = subprocess.run(
            [sys.executable, SERVER, "--ask", "--questions", json.dumps(gates.COMMAND_QUESTIONS)],
            input=state, capture_output=True, text=True, timeout=TIMEOUT,
        )
    except Exception as error:  # timeout, missing server, unreadable exec
        sys.stderr.write(f"[jev-gate] pre-tool: skipped ({error})\n")
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
    """One JSONL line per judged call. Prefix only — never the whole command."""
    try:
        with open(os.path.join(_state_dir(), "decisions.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def emit(host: str, decision: str | None, reason: str, policy: dict) -> None:
    """Say nothing unless there is something to say — silence means pass through."""
    if decision in (None, "allow"):
        if decision == "allow" and policy.get("allowMode") == "allow":
            if host == "antigravity":
                print(json.dumps({"decision": "allow", "reason": f"jev: {reason}"}))
            else:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": f"jev: {reason}",
                }}))
        return
    if host == "antigravity":
        print(json.dumps({
            "decision": "deny" if decision == "deny" else "ask",
            "reason": f"jev: {reason}",
        }))
        return
    if os.environ.get("JEV_GATE_HOST") == "codex":
        # Codex has no `ask` on PreToolUse: pass through with the concern surfaced
        # rather than turning a soft judgement into a refusal.
        if decision == "deny":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"jev: {reason}",
            }}))
        else:
            print(json.dumps({"systemMessage": f"jev: {reason}"}))
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny" if decision == "deny" else "ask",
        "permissionDecisionReason": f"jev: {reason}",
    }}))


def main() -> None:
    started = time.time()
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(event, dict):
        sys.exit(0)
    host, tool, args, session, cwd = detect(event)
    if not tool or gates.is_jev_tool(tool) or not gates.is_shell_tool(tool, args):
        sys.exit(0)  # read-only tools and jev's own calls are never judged
    command = gates.extract_command(args)
    if not command:
        sys.exit(0)
    reasons = gates.command_reasons(command)
    if not reasons:
        sys.exit(0)  # tier 0 says this is routine: no request, no log line
    policy = load_policy()
    scope = (policy.get("cache") or {}).get("scope", "session")
    key = gates.cache_key(host, tool, command, cwd, session if scope == "session" else "")
    cache = read_cache(policy)
    cached = cache.get(key)
    source = "jev"
    answers: dict | None = None
    if isinstance(cached, dict) and isinstance(cached.get("d"), str):
        decision, reason, source = cached["d"], str(cached.get("r", "")), "cache"
    else:
        oversized = gates.oversized_decision(command, policy)
        if oversized is not None:
            decision, reason, source = oversized[0], oversized[1], "local"
        else:
            answers = ask_jev(gates.command_state(command, cwd, host))
            if answers is None:
                sys.exit(0)  # fail open: no answer means the harness decides
            decision, reason = gates.decide_command(answers, policy)
            cache[key] = {"d": decision, "r": reason, "ts": time.time()}
            write_cache(cache, policy)
    audit({
        "gate": "pre_tool",
        "ts": round(time.time(), 3),
        "host": host,
        "tool": tool,
        "session": session[:64],
        "command_hash": key,
        "command_prefix": gates.command_prefix(command),
        "belts": reasons,
        "decision": decision,
        "reason": reason,
        "source": source,
        "mode": policy.get("mode", "audit"),
        "answers": answers,
        "latency_ms": round((time.time() - started) * 1000),
    })
    if policy.get("mode", "audit") == "audit":
        sys.exit(0)  # judged and logged, never enforced
    emit(host, decision, reason, policy)
    sys.exit(0)


if __name__ == "__main__":
    main()
