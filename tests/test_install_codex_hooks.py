"""The Codex hook installer: plugins don't ship hooks there, so this declares them.

The failure it fixes is silent: reinstall the plugin, the dashboard still shows one hook,
and nothing says why. So the tests are about the merge being additive and idempotent —
a second run must not duplicate anything, and foreign hooks must survive.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def installer():
    spec = importlib.util.spec_from_file_location("install_codex_hooks", ROOT / "scripts" / "install-codex-hooks.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot load scripts/install-codex-hooks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_three_gates_are_declared(installer):
    config, changed = installer.merge({})
    assert sorted(changed) == ["PostToolUse", "PreToolUse", "Stop"]
    commands = json.dumps(config["hooks"])
    assert commands.count("pretool-gate.py") == 1
    assert commands.count("stop-gate.py") == 1
    assert commands.count("claude-codex-jev-verdict.py") == 1
    # Codex has no `ask`; both gates that can answer need the host hint.
    assert "JEV_GATE_HOST=codex" in commands


def test_rerunning_is_a_no_op(installer):
    once, _ = installer.merge({})
    twice, changed = installer.merge(json.loads(json.dumps(once)))
    assert changed == []
    assert once["hooks"] == twice["hooks"]


def test_foreign_hooks_survive(installer):
    existing = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 /somebody/else.py"}]}]}}
    merged, changed = installer.merge(existing)
    commands = [h["command"] for e in merged["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert any("somebody/else.py" in c for c in commands)      # kept
    assert any("claude-codex-jev-verdict.py" in c for c in commands)  # added
    assert sorted(changed) == ["PostToolUse", "PreToolUse", "Stop"]


def test_a_stale_path_is_repointed_not_duplicated(installer):
    merged, changed = installer.merge({})
    entries = merged["hooks"]["Stop"]
    entries[0]["hooks"][0]["command"] = "env JEV_GATE_HOST=codex python3 /old/checkout/hooks/stop-gate.py"
    repaired, changed = installer.merge(merged, repo="/new/checkout")
    stop_commands = [h["command"] for e in repaired["hooks"]["Stop"] for h in e["hooks"]]
    assert len(stop_commands) == 1
    assert stop_commands[0].endswith("/new/checkout/hooks/stop-gate.py")
    # every gate moves to the new checkout, and none is duplicated
    assert sorted(changed) == ["PostToolUse", "PreToolUse", "Stop"]
    for event in ("PreToolUse", "PostToolUse", "Stop"):
        assert len(merged["hooks"][event]) == len(repaired["hooks"][event]) == 1
