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


def record_run(key: str, permission_mode: str = "default") -> None:
    """PostToolUse: this exact command ran here, so a human (or a rule) allowed it."""
    if str(permission_mode or "").strip() in NO_PROMPT_MODES:
        return
    try:
        data = _load()
        now = time.time()
        data = {k: v for k, v in data.items()
                if isinstance(v, (int, float)) and now - float(v) < PRUNE_AFTER_S}
        data[key] = now
        if len(data) > 2_000:
            data = dict(sorted(data.items(), key=lambda kv: float(kv[1]), reverse=True)[:2_000])
        with open(_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except Exception:
        pass  # a permission gate must never fail because bookkeeping failed


def was_executed(key: str) -> bool:
    stamp = _load().get(key)
    if not isinstance(stamp, (int, float)):
        return False
    return time.time() - float(stamp) < PRUNE_AFTER_S
