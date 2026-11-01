"""Shared, host-agnostic decision logic for the jev gates.

Everything here is pure: no network, no filesystem, no subprocess, no host
knowledge beyond the payload shapes in `extract_command`. The hook scripts
(`hooks/pretool-gate.py`, `hooks/stop-gate.py`) own the I/O, the host decision
vocabulary, and the audit log; this module owns *what to ask jev* and *what to do
with the answer*, so the same thresholds and wording apply on every harness and a
unit test can exercise them without a key.

Two rules the prior art converged on and this module enforces:

1. **Jev may only narrow, never widen.** `allow` is reported as `allow` for
   logging, but a hook must translate it into "no decision" (pass through to the
   harness's own permission flow) unless an operator explicitly opts in.
2. **A hard action needs a decisive answer.** Below the confidence bar the
   decision degrades to `ask` — the human resolves it — instead of acting on a
   near-coin-flip probability.

Thresholds are seeded from published implementations (hookgate: confidence 0.70,
destructive 0.50, unverified 0.70; rea-jev: scope 0.30, irreversibility 0.80) and
must be re-fitted on each host's own audit log before anything enforces.
"""

from __future__ import annotations

import hashlib
import json
import re

# ── policy ────────────────────────────────────────────────────────────────────

DEFAULT_POLICY: dict = {
    # "audit" judges for real, logs one JSONL line, and never touches the turn.
    # "enforce" is the opt-in; ship in audit until the log has a false-positive rate.
    "mode": "audit",
    "thresholds": {
        "confidence": 0.70,   # below this, every answer becomes `ask`
        "destructive": 0.50,  # noul that forces at least `ask`
    },
    # A body this long cannot be judged by reading a head and a tail, so the
    # decision is made locally instead of spending a request on a bad sample.
    "maxStateChars": 12000,
    "cache": {"ttlMs": 3_600_000},
    # "passthrough" keeps `allow` from widening the harness's permissions.
    "allowMode": "passthrough",
}


def merge_policy(*layers: dict | None) -> dict:
    """Deep-merge policy layers left to right; `None` layers are skipped."""
    out = json.loads(json.dumps(DEFAULT_POLICY))
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key].update(value)
            else:
                out[key] = value
    return out


def pct(value: float | None) -> str:
    return "?" if value is None else f"{round(float(value) * 100)}%"


# ── answer reading ────────────────────────────────────────────────────────────


def confidence_of(answer) -> float | None:
    """Confidence in [0,1] for a typed jev answer.

    Uses the provider's `confidence` when present. Otherwise derives it, because
    every published implementation needs this number and the relay does not always
    send it:

      * noul  -> |2p - 1| — "how far from a coin flip", the band definition rea-jev
                 published; 0.0 means the model has no lean at all.
      * choice/score -> normalised margin over the uniform baseline, so a 3-way
                 answer whose probabilities are (0.4, 0.3, 0.3) scores 0.1 (barely
                 above uniform) rather than 0.4 (which would read as confident).
    """
    if not isinstance(answer, dict):
        return None
    raw = answer.get("confidence")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0.0 <= raw <= 1.0:
        return float(raw)
    probs = answer.get("probabilities")
    if isinstance(probs, dict):
        values = [v for v in probs.values() if isinstance(v, (int, float))]
    elif isinstance(probs, list):
        values = [v for v in probs if isinstance(v, (int, float))]
    else:
        values = []
    if len(values) >= 2:
        ordered = sorted((float(v) for v in values), reverse=True)
        top, second = ordered[0], ordered[1]
        uniform = 1.0 / len(values)
        if top <= uniform:
            return 0.0
        return max(0.0, min(1.0, (top - second) / (1.0 - uniform)))
    noul = answer.get("noul")
    if isinstance(noul, (int, float)) and not isinstance(noul, bool):
        return abs(2.0 * float(noul) - 1.0)
    return None


def noul_of(answer) -> float | None:
    if isinstance(answer, dict):
        raw = answer.get("noul")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
    return None


# ── command risk (the pre-tool gate) ─────────────────────────────────────────

RISK_QUESTION: dict = {
    "type": "choice",
    "instructions": (
        "A coding agent wants to run this shell command inside a software "
        "repository. Judge it as the maintainer of that repository would: routine "
        "development work should proceed; anything that could lose work, leave the "
        "repository, or cost money deserves a human glance; anything clearly "
        "destructive or exfiltrating should be refused."
    ),
    "criteria": {
        "allow": (
            "Routine and reversible inside the working tree: build, test, lint, "
            "read, list, or git operations that create or inspect history."
        ),
        "ask": (
            "Plausibly intended but with consequences worth a human glance: deletes "
            "or overwrites files, rewrites history, pushes, publishes, installs "
            "globally, changes system or shell configuration, sends data to a "
            "network endpoint."
        ),
        "deny": (
            "Destructive or exfiltrating with no plausible development purpose: "
            "recursive deletion outside the repository, wiping disks or home "
            "directories, downloading and executing a remote script, reading and "
            "sending credentials."
        ),
    },
}

DESTRUCTIVE_QUESTION: dict = {
    "type": "noul",
    "instructions": (
        "Does this command destroy or overwrite data, history, or state outside the "
        "repository working tree, or send something irreversibly to the outside world?"
    ),
}

COMMAND_QUESTIONS: dict = {"risk": RISK_QUESTION, "destructive": DESTRUCTIVE_QUESTION}

# Tier 0 — deterministic, free, and deliberately broad. A belt only decides whether
# the command is worth ONE jev call; it never decides the outcome, so a false hit
# costs a fraction of a cent and a false miss is the only real error.
RISK_BELTS: tuple[tuple[str, str], ...] = (
    ("recursive delete", r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+|--recursive\b)"),
    ("delete without recycle", r"\brm\s+-[a-zA-Z]*f|\brm\s+--force\b"),
    ("git history rewrite", r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f|push\s+(?:--force|-f)\b|filter-branch|reflog\s+expire|stash\s+(?:drop|clear))"),
    ("database drop", r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+(?:TABLE\s+)?\w+|DELETE\s+FROM\s+\w+|dropdb)\b"),
    ("bulk delete by find", r"\bfind\b[^|\n]*\s(?:-delete|-exec\s+(?:sudo\s+)?rm)\b"),
    ("process kill", r"\b(?:killall|pkill)\b|\bkill\s+-(?:[a-zA-Z]*\d|(?:TERM|KILL)\b)"),
    ("raw disk write", r"\bdd\s+[^|]*\bof=|\bmkfs(?:\.\w+)?\b|\bshred\b|>\s*/dev/[sh]d"),
    ("recursive permission change", r"\bch(?:mod|own|grp)\s+-[a-zA-Z]*R"),
    ("global install", r"\b(?:npm|pnpm|yarn|bun)\s+(?:i|install|add|remove)\s+(?:-g|--global)\b|\bpipx?\s+install\b[^\n]*--user\b"),
    ("publish or release", r"\b(?:npm\s+publish|twine\s+upload|cargo\s+publish|gh\s+release\s+(?:create|upload)|docker\s+push|gcloud\s+(?:app\s+)?deploy|vercel\s+(?:deploy|--prod)|netlify\s+deploy)\b"),
    ("remote copy", r"\b(?:scp|rsync|ssh|sftp)\b[^\n]*(?:@|://)"),
    ("network upload", r"\bcurl\b[^|]*\s(?:-X\s*(?:POST|PUT|PATCH)|--data(?:-binary|-raw|-urlencode)?\b|-F\s|--upload-file|-T\s)|\bwget\b[^|]*--(?:post-data|post-file|method=POST)"),
    ("download piped to shell", r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b"),
    ("netcat", r"\bnc(?:at)?\b[^\n]*\s-\w*e|\bncat\b"),
    ("privilege escalation", r"\bsudo\b|\bdoas\b|\bsu\s+-\b"),
    ("system or container wipe", r"\b(?:docker(?:\s+system)?\s+prune|kubectl\s+delete|systemctl\s+(?:stop|disable|mask)|launchctl\s+(?:unload|remove|bootout))"),
    ("credential read", r"\b(?:cat|less|more|head|tail|grep|strings|base64)\b[^\n]*(?:\.env\b|id_rsa|id_ed25519|credentials|\.aws/|\.ssh/|\.netrc\b)"),
)

_BELTS = tuple((label, re.compile(pattern, re.IGNORECASE)) for label, pattern in RISK_BELTS)

# Tools that run a shell command, per host vocabulary. A tool whose arguments carry a
# command string is treated as a shell tool even when the name is unknown, so a new
# host or a renamed tool does not silently skip the gate.
SHELL_TOOLS = frozenset({
    "bash", "sh", "zsh", "shell", "exec", "command", "run_command",
    "run_shell_command", "terminal", "execute", "powershell", "pwsh", "cmd",
})
COMMAND_KEYS = ("command", "CommandLine", "cmd", "commandLine", "script", "shell_command")
JEV_TOOL_PREFIXES = ("jev_", "mcp__jev__", "mcp__plugin_jev", "codex_jev", "jev-cost-guard_jev")


def is_jev_tool(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in JEV_TOOL_PREFIXES)


def is_shell_tool(name: str, args) -> bool:
    if not name:
        return False
    low = name.lower()
    if low in SHELL_TOOLS or "shell" in low or low.endswith("bash") or low.endswith("command"):
        return True
    return isinstance(args, dict) and any(k in args for k in COMMAND_KEYS)


def extract_command(args) -> str:
    """The command string out of a host's tool arguments, or "" when there is none."""
    if isinstance(args, str):
        return args.strip()
    if not isinstance(args, dict):
        return ""
    for key in COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return " ".join(value).strip()
    return ""


def command_reasons(command: str) -> list[str]:
    """Tier 0: labels of the belts this command trips (empty == not worth a call)."""
    return [label for label, rx in _BELTS if rx.search(command)]


def command_state(command: str, cwd: str = "", host: str = "") -> str:
    """The state a command-risk question is judged against.

    Deliberately tiny: the command plus the last path component of the working
    directory. No transcript, no file contents, no environment, no user identity.
    """
    cwd_tail = "/".join(str(cwd).split("/")[-2:]) if cwd else ""
    return json.dumps({"command": command, "working_directory": cwd_tail, "host": host})


def decide_command(answers: dict, policy: dict) -> tuple[str | None, str]:
    """Map jev's answers to a decision. `None` means "no opinion" — pass through.

    `None` on a malformed answer is deliberate: a provider that returns something
    unexpected must never be able to enforce anything.
    """
    thresholds = policy["thresholds"]
    risk = (answers or {}).get("risk")
    choice = risk.get("choice") if isinstance(risk, dict) else None
    if choice not in ("allow", "ask", "deny"):
        return None, "malformed answer (no usable risk choice)"
    confidence = confidence_of(risk)
    if confidence is None:
        return None, "malformed answer (no confidence)"
    destructive = noul_of((answers or {}).get("destructive"))
    if confidence < float(thresholds["confidence"]):
        return "ask", (
            f"jev leans \"{choice}\" at {pct(confidence)} confidence, below the "
            f"{pct(thresholds['confidence'])} bar — a human should look."
        )
    if choice == "allow" and destructive is not None and destructive >= float(thresholds["destructive"]):
        return "ask", (
            f"judged routine, but {pct(destructive)} likely to destroy or send "
            "something outside the repository — a human should look."
        )
    if choice == "deny":
        tail = f", {pct(destructive)} destructive or exfiltrating" if destructive is not None else ""
        return "deny", f"refused at {pct(confidence)} confidence{tail}."
    if choice == "ask":
        tail = f", {pct(destructive)} destructive" if destructive is not None else ""
        return "ask", f"worth a human glance ({pct(confidence)} confidence{tail})."
    return "allow", f"routine at {pct(confidence)} confidence."


def oversized_decision(command: str, policy: dict) -> tuple[str, str] | None:
    """Local `ask` for a command too long to judge, or None when it fits."""
    limit = int(policy.get("maxStateChars", 12000))
    if len(command) <= limit:
        return None
    return (
        "ask",
        f"command is {len(command)} chars, over the {limit}-char judging limit — "
        "reading a head and a tail is not judging the command, so no jev call was made.",
    )


def cache_key(host: str, tool: str, command: str, cwd: str = "", session: str = "") -> str:
    """Identity of a *decision*, not of a command string.

    A verdict is only reusable where it was reached: `rm -rf ./build` is a different
    risk in a different directory, and a fresh session inherits nothing from the last
    one. So the session and the working directory belong in the key, and the TTL is
    only a pruning backstop — never the thing that makes the cache correct.
    """
    digest = hashlib.sha256(
        f"{host}\x00{tool}\x00{command}\x00{cwd}\x00{session}".encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest[:32]}"


def command_prefix(command: str, limit: int = 60) -> str:
    """A short, log-safe excerpt — the audit log keeps a prefix, never the command."""
    flat = " ".join(command.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ── unverified completion (the stop gate) ────────────────────────────────────

UNVERIFIED_QUESTION: dict = {
    "type": "noul",
    "instructions": (
        "This is the final message of a coding agent ending its turn, followed by the "
        "run facts for that turn. Does the message claim that work is complete, "
        "tested, committed, pushed, or deployed in a way the run facts do not "
        "support — tests said to pass with no passing check shown, changes said to be "
        "committed with edits still unverified, work declared done with steps visibly "
        "missing?"
    ),
}

COMPLETION_QUESTIONS: dict = {"unverified": UNVERIFIED_QUESTION}

# Tier 0 for the stop gate: which tool calls changed something, and which of them
# actually ran a check. Belt matching is on the *runner's own summary line* as well
# as the command, because a failing suite is recorded without any error field.
MUTATING_TOOLS: tuple[str, ...] = (
    "write", "write_to_file", "edit", "multiedit", "str_replace", "str_replace_editor",
    "replace_file_content", "multi_replace_file_content", "apply_patch", "notebookedit",
    "create_file", "insert_content",
)
CHECK_COMMAND = re.compile(
    r"(?:\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|tests|check|lint|typecheck|build|verify|ci)\b"
    r"|\b(?:npx|pnpm|bunx)\s+(?:tsc|jest|vitest|mocha|eslint|biome|prettier\s+--check|playwright)\b"
    r"|\bpytest\b|\bjest\b|\bvitest\b|\bmocha\b|\btsc\b|\beslint\b|\bbiome\s+(?:check|ci)\b"
    r"|\bruff\b|\bmypy\b|\bflake8\b|\bpylint\b|\bblack\s+--check\b|\bgo\s+(?:test|vet|build)\b"
    r"|\bcargo\s+(?:test|check|build|clippy|nextest)\b|\bmake\s+(?:test|check|lint|build)\b"
    r"|\bmvn\s+(?:test|verify)\b|\bgradle\w*\s+(?:test|check|build)\b|\bswift\s+(?:test|build)\b"
    r"|\bxcodebuild\b|\bdotnet\s+(?:test|build)\b|\brspec\b|\bphpunit\b|\bctest\b)",
    re.IGNORECASE,
)
PASS_SUMMARY = re.compile(
    r"(?:\b(?:ok|pass|passed|passing|success|succeeded|green|clean|no errors)\b"
    r"|\b\d+\s+(?:passed|tests?\s+passed)\b|\bBUILD SUCCESS(?:FUL)?\b|\bAll tests passed\b)",
    re.IGNORECASE,
)
FAIL_SUMMARY = re.compile(
    r"(?:\b(?:fail|failed|failing|error|errors|broken|fatal)\b"
    r"|\b\d+\s+(?:failed|errors?)\b|\bBUILD FAILED\b|\bAssertionError\b|\bTraceback\b)",
    re.IGNORECASE,
)
DONE_HINT = re.compile(
    r"\b(done|did it|finished|fixed|resolved|implemented|complete[ds]?|completed|working|"
    r"works now|passing|passes|green|ready|all set|good to go|should work|in place|sorted|"
    r"verified|shipped)\b",
    re.IGNORECASE,
)


def is_mutating_tool(name: str) -> bool:
    low = (name or "").lower()
    return any(low == tool or low.endswith(tool) for tool in MUTATING_TOOLS)


def looks_like_check(command: str) -> bool:
    return bool(CHECK_COMMAND.search(command or ""))


def check_outcome(output: str) -> str:
    """`passed` / `failed` / `unknown` for one check's output.

    Failure wins over success: a suite that prints "3 failed, 40 passed" is a
    failure, and a run that prints a stack trace after a summary line is too.
    """
    text = output or ""
    if FAIL_SUMMARY.search(text):
        return "failed"
    if PASS_SUMMARY.search(text):
        return "passed"
    return "unknown"


def build_completion_state(task: str, final_message: str, run: dict,
                           task_cap: int = 1500, message_cap: int = 2000) -> str:
    """State for the unverified-completion question.

    Small by construction, and shaped like the one published implementation that
    measured itself (jev-belay: AUROC 0.976 with run facts vs 0.777 on the wording
    alone): the request keeps its head, the closing message keeps its tail, and the
    run facts are two small lists.
    """
    def head(text: str, limit: int) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    def tail(text: str, limit: int) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else "…" + text[-limit:]

    return json.dumps({
        "task": head(task, task_cap) or "(no user request recorded in this session)",
        "final_message": tail(final_message, message_cap),
        "run": {
            "file_changes": int(run.get("file_changes") or 0),
            "checks_run": list(run.get("checks_run") or [])[:8],
        },
    })


def decide_completion(answers: dict, policy: dict) -> tuple[str | None, str]:
    """`block` when the closing message is likely unverified, else None."""
    thresholds = policy["thresholds"]
    answer = (answers or {}).get("unverified")
    probability = noul_of(answer)
    confidence = confidence_of(answer)
    if probability is None or confidence is None:
        return None, "malformed answer (no usable unverified noul)"
    unverified_bar = float(thresholds.get("unverified", 0.70))
    if probability >= unverified_bar and confidence >= float(thresholds["confidence"]):
        return "block", (
            f"this message claims a completion the run facts do not show "
            f"({pct(probability)} likely, {pct(confidence)} confidence). Verify before "
            "stopping: run the check you cite, compare what you say changed with what "
            "did, and state what is still missing. If nothing can verify it, say so "
            "plainly instead of presenting it as done."
        )
    return None, f"claim looks supported ({pct(probability)} unverified, {pct(confidence)} confidence)."
