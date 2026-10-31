#!/usr/bin/env python3
"""Antigravity PreInvocation hook: one advisory line naming the best-fitting skill.

This is the Antigravity form of the Hermes `pre_llm_call` skill router, and it is
deliberately **deterministic**: it ranks the live skill roster against the user's
request lexically and injects one line. No model call, no per-turn spend, no state
beyond "already hinted this skill in this conversation".

Why not jev here: a routing decision is a per-turn cost paid before anything is
known to be worth it, and the measured effect of an advisory line on whether the
model *calls* a tool is ~0. The gate (`antigravity-jev-verdict.py`) spends jev where
it buys a verdict; this one asks the model to consider a skill that textually
matches. `jev_find` / `jev_screen` still cover the semantic cases on demand.

Ships **disabled** in `hooks.json` (`"enabled": false`): flip that flag to use it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "this",
    "that", "it", "is", "are", "be", "my", "me", "you", "please", "can", "how",
    "what", "why", "when", "which", "do", "does", "should", "would", "i", "we",
}


def _budget():
    """The repo's own skill scanner, path-loaded so there is one roster implementation."""
    path = os.path.join(REPO_ROOT, "scripts", "prompt_budget.py")
    try:
        spec = importlib.util.spec_from_file_location("prompt_budget", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _state_dir() -> str:
    return os.environ.get("JEV_AGY_ROUTE_STATE_DIR") or os.path.join(
        os.environ.get("TMPDIR") or "/tmp", "jev-agy-route"
    )


def _already_hinted(conversation: str, skill: str) -> bool:
    try:
        with open(os.path.join(_state_dir(), conversation), encoding="utf-8") as fh:
            return skill in (json.load(fh) or {}).get("hinted", [])
    except (OSError, ValueError, TypeError):
        return False


def _remember(conversation: str, skill: str) -> None:
    path = os.path.join(_state_dir(), conversation)
    try:
        with open(path, encoding="utf-8") as fh:
            hinted = list((json.load(fh) or {}).get("hinted") or [])
    except (OSError, ValueError, TypeError):
        hinted = []
    hinted.append(skill)
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hinted": hinted[-8:]}, fh)
    except OSError:
        pass


def _last_user_input(transcript: str) -> str:
    text = ""
    try:
        with open(os.path.expanduser(transcript), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or "USER_INPUT" not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("type") == "USER_INPUT":
                    content = row.get("content")
                    if isinstance(content, str):
                        text = content
    except OSError:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return text.strip()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _STOP}


def _name_words(name: str) -> set[str]:
    """A slug's parts: `jev-test-output-triage` -> {jev, test, output, triage}."""
    return {w for w in re.split(r"[-_.]", name.lower()) if len(w) >= 3 and w not in _STOP}


def _roster(workspace_paths: list[str]) -> list[tuple[str, str]]:
    """(name, description) for every skill this session can actually load."""
    budget = _budget()
    if budget is None:
        return []
    roots: list[tuple[str, str]] = [("plugin", os.path.join(REPO_ROOT, "skills"))]
    for label, path in budget.default_roots("antigravity"):
        roots.append((label, path))
    for workspace in workspace_paths:
        for parts in ((".agents", "skills"), (".agent", "skills")):
            candidate = os.path.join(workspace, *parts)
            if os.path.isdir(candidate):
                roots.append(("workspace", candidate))

    seen: dict[str, str] = {}
    for _, root in roots:
        for path in budget.find_skill_files(root):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(8192)
            except OSError:
                continue
            fm = budget.parse_frontmatter(head)
            name = fm.get("name") or os.path.basename(os.path.dirname(path))
            if name not in seen:
                seen[name] = fm.get("description", "")
    return sorted(seen.items())


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(event, dict):
        sys.exit(0)
    # Session start only: one hint per conversation, not one per model call.
    try:
        if int(event.get("invocationNum", 0)) != 0:
            sys.exit(0)
    except (TypeError, ValueError):
        sys.exit(0)
    transcript = event.get("transcriptPath")
    conversation = str(event.get("conversationId") or "")
    if not transcript or not conversation:
        sys.exit(0)

    request = _words(_last_user_input(str(transcript)))
    if not request:
        sys.exit(0)

    paths = event.get("workspacePaths")
    workspace_paths = [str(p) for p in paths] if isinstance(paths, list) else []
    best: tuple[int, str] | None = None
    for name, description in _roster(workspace_paths):
        # A term in the skill's own name is worth more than one buried in prose.
        score = 2 * len(request & _name_words(name)) + len(request & _words(description))
        if score >= 2 and (best is None or score > best[0]):
            best = (score, name)
    if best is None or _already_hinted(conversation, best[1]):
        sys.exit(0)

    _remember(conversation, best[1])
    print(json.dumps({
        "injectSteps": [{
            "ephemeralMessage": (
                f"[jev] Skill hint: `{best[1]}` matches this request on "
                f"{best[0]} term(s). Load it if the task is what its description says; "
                "ignore this line otherwise."
            )
        }]
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
