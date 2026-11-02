"""What already ran in this session, so a human is not asked twice.

The gate asks once per (session, cwd, command). If that command then executed, the human
allowed it — and re-asking on a retry is nagging, precisely when an agent is most likely
to repeat itself. If it never executed, the human declined or the run was interrupted, and
the gate keeps asking.

That distinction needs a PostToolUse observation, not a guess: the hook cannot see the
human's answer, only whether the tool ran afterwards. Recording "it ran" is therefore the
only evidence available, and it is enough — silence on the retry defers to the harness's
own permission system, which either applies the approval the human granted or prompts
again. It never widens: no approval, no silence.
"""
from __future__ import annotations

import json
import os
import time

STATE_DIR = os.environ.get("JEV_GATE_STATE_DIR") or os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "jev-gate"
)
PRUNE_AFTER_S = 24 * 3600


def _path() -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, "executed.json")


def _load() -> dict:
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# Modes in which the host answers its own prompts instead of asking a person. Recording
# an "approval" there would be a misnomer: nobody was asked, so nothing was granted.
NO_PROMPT_MODES = ("bypassPermissions", "dangerouslySkipPermissions")


def record_run(key: str, permission_mode: str = "default", scope: str | None = None) -> None:
    """PostToolUse: this command ran here, and with which grant if the host records one.

    `scope` is "session"/"always" when the host names the grant (Claude Code does) and
    None when it does not (Codex, Antigravity, Hermes). Recording None is deliberate: the
    pre-tool gate can then decide, per `cache.approvalScope`, whether a host without scope
    evidence may be treated as a session approval.
    """
    if str(permission_mode or "").strip() in NO_PROMPT_MODES:
        return
    try:
        data = _load()
        now = time.time()
        data = {k: v for k, v in data.items()
                if isinstance(v, dict) and now - float(v.get("ts", 0)) < PRUNE_AFTER_S}
        data[key] = {"ts": now, "scope": scope}
        if len(data) > 2_000:
            data = dict(sorted(data.items(), key=lambda kv: float(kv[1]), reverse=True)[:2_000])
        with open(_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except Exception:
        pass  # a permission gate must never fail because bookkeeping failed


def last_permission(path: str) -> dict:
    """The harness's own record of the last permission decision in this session.

    Claude Code writes one `permissionDecision` object per tool call into the user row of
    the transcript, and it carries the *scope* of the grant — the thing no hook payload
    exposes. Measured on 5 000+ rows of real transcripts:

      {"decision":"accept","source":"user_temporary","grantScope":"once"}  -> this once
      {"decision":"accept","source":"user_temporary"}                     -> this session
      {"decision":"accept","source":"user_permanent","ruleSource":"..."}  -> always
      {"decision":"accept","source":"config","reasonType":"classifier"}   -> nobody asked

    Read backwards from the end; unreadable or absent means {} — the caller then treats it
    as "no grant", which is the behaviour that keeps asking.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except Exception:
        return {}
    for line in reversed(lines[-400:]):
        if "permissionDecision" not in line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        decision = row.get("permissionDecision") if isinstance(row, dict) else None
        if isinstance(decision, dict):
            return decision
    return {}


def grant_scope(decision: dict) -> str | None:
    """Which grant the human gave, according to the harness.

    Only `session` and `always` are worth remembering: `once` is exactly what it says, and
    `config`/`hook` mean no person was involved, so nothing was granted in anyone's name.
    Anything unrecognised returns None and the gate keeps asking.
    """
    if not isinstance(decision, dict) or decision.get("decision") != "accept":
        return None
    source = str(decision.get("source") or "")
    if source == "user_permanent":
        return "always"
    if source != "user_temporary":
        return None
    return "once" if decision.get("grantScope") == "once" else "session"


def entry(key: str) -> dict:
    """What we know about this exact command: when it ran, and the grant the host named."""
    record = _load().get(key)
    if not isinstance(record, dict):
        return {}
    if time.time() - float(record.get("ts", 0)) >= PRUNE_AFTER_S:
        return {}
    return record


def was_executed(key: str) -> bool:
    return bool(entry(key))


def honoured(key: str, approval_scope: str = "host") -> bool:
    """May this recorded run silence the gate?

    `host` (default): only when the harness named a session or permanent human grant —
    Claude Code's transcript does, the others do not, so they keep asking.
    `session`: any recorded run counts, which is what makes the behaviour identical on
    Codex, Antigravity and Hermes, at the cost of remembering a one-shot yes there.
    """
    record = entry(key)
    if not record:
        return False
    if str(record.get("scope") or "") in ("session", "always"):
        return True
    return str(approval_scope or "host").lower() == "session"
