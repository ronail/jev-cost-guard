"""Antigravity packaging: the manifest, the MCP filename, and the roster estimator.

The three files this covers are the ones a bad guess breaks silently: a
`mcp_config.json` that drifts from `mcp.json`, a manifest whose `name` stops
matching the root one, and a `prompt_budget.py` that counts plugins Antigravity
has switched off.
"""
from __future__ import annotations

import importlib.util
import json
import os
import struct
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {relpath}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def budget():
    return _load("prompt_budget", "scripts/prompt_budget.py")


@pytest.fixture(scope="module")
def agnostic():
    return _load("check_harness_agnostic", "scripts/check_harness_agnostic.py")


# ---- manifests --------------------------------------------------------------


def test_antigravity_manifest_matches_the_portable_one():
    root = json.loads((REPO / "plugin.json").read_text())
    ag = json.loads((REPO / ".antigravity-plugin" / "plugin.json").read_text())
    assert ag["name"] == root["name"], "Antigravity needs the same plugin name"
    assert ag["name"] == "jev-cost-guard"
    assert ag["name"] == ag["name"].lower()
    assert ag["name"].replace("-", "").isalnum(), "kebab-case identifier only"
    assert ag["version"] == root["version"]
    assert len(ag["suggestedPrompts"]) <= 3


def test_every_manifest_carries_one_version():
    versions = {
        str(path.relative_to(REPO)): json.loads(path.read_text())["version"]
        for path in (
            REPO / "plugin.json",
            REPO / ".claude-plugin" / "plugin.json",
            REPO / ".codex-plugin" / "plugin.json",
            REPO / ".antigravity-plugin" / "plugin.json",
        )
    }
    assert len(set(versions.values())) == 1, versions


def test_antigravity_logo_exists_and_is_square():
    ag = json.loads((REPO / ".antigravity-plugin" / "plugin.json").read_text())
    logo = REPO / ag["logo"]
    assert logo.is_file(), ag["logo"]
    head = logo.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "marketplace wants a raster or svg icon"
    width, height = struct.unpack(">II", head[16:24])
    assert width == height and width >= 128, (width, height)


def test_mcp_config_declares_the_same_server_as_the_portable_declaration():
    portable = json.loads((REPO / "mcp.json").read_text())["mcpServers"]
    antigravity = json.loads((REPO / "mcp_config.json").read_text())["mcpServers"]
    assert set(portable) == set(antigravity) == {"jev"}
    for name, server in antigravity.items():
        assert server["command"] == portable[name]["command"]
        assert server["args"] == portable[name]["args"], "the two must not drift"
        assert "${PLUGIN_ROOT}" in server["args"][0]


def test_skill_dirs_match_their_frontmatter_names():
    """Antigravity rejects (or silently drops) a skill whose folder name differs."""
    for skill in sorted((REPO / "skills").glob("*/SKILL.md")):
        head = skill.read_text(encoding="utf-8").splitlines()
        assert head[0].strip() == "---"
        name = next(line.split(":", 1)[1].strip() for line in head[1:] if line.startswith("name:"))
        assert name == skill.parent.name, (skill.parent.name, name)


# ---- estimator --------------------------------------------------------------


def _write_skill(root: Path, name: str, description: str) -> None:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
    )


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    return home


def test_detect_harness_sees_antigravity(monkeypatch):
    for key in ("HERMES_HOME", "HERMES_PROFILE", "HERMES_SESSION", "CLAUDECODE",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "CODEX_HOME",
                "CODEX_SANDBOX", "CODEX_SESSION_ID", "OPENAI_CODEX"):
        monkeypatch.delenv(key, raising=False)
    module = _load("prompt_budget_detect", "scripts/prompt_budget.py")
    monkeypatch.setenv("ANTIGRAVITY_AGENT", "1")
    assert module.detect_harness() == "antigravity"


def test_antigravity_roots_cover_global_workspace_and_enabled_plugins(fake_home, budget):
    _write_skill(fake_home / ".gemini" / "config" / "skills", "global-skill", "g")
    _write_skill(fake_home / ".gemini" / "antigravity-cli" / "skills", "cli-skill", "c")
    _write_skill(fake_home / ".gemini" / "config" / "plugins" / "on" / "skills", "on-skill", "o")
    _write_skill(fake_home / ".gemini" / "config" / "plugins" / "off" / "skills", "off-skill", "x")
    _write_skill(Path.cwd() / ".agents" / "skills", "workspace-skill", "w")
    (fake_home / ".gemini" / "config" / "config.json").write_text(
        json.dumps({"plugins": {"on": {"installedFrom": {"id": "x"}},
                                "off": {"enabled": False}}})
    )

    roots = budget.default_roots("antigravity")
    paths = [path for _, path in roots]
    assert any(p.endswith(os.path.join(".gemini", "config", "skills")) for p in paths)
    assert any(p.endswith(os.path.join("antigravity-cli", "skills")) for p in paths)
    assert any(os.path.join(".agents", "skills") in p for p in paths), "workspace roster"
    assert any(os.path.join("plugins", "on", "skills") in p for p in paths)
    assert not any(os.path.join("plugins", "off", "skills") in p for p in paths)
    assert budget.AG_FILTER_STATE == "applied"

    skills, _ = budget.collect(roots)
    names = {s["name"] for s in skills}
    assert {"global-skill", "cli-skill", "on-skill", "workspace-skill"} <= names
    assert "off-skill" not in names, "a disabled plugin's skills must not be counted"


def test_antigravity_skips_a_plugin_whose_manifest_was_renamed(fake_home, budget):
    plugin = fake_home / ".gemini" / "config" / "plugins" / "removed"
    _write_skill(plugin / "skills", "gone-skill", "d")
    (plugin / "plugin.json.disabled").write_text("{}")

    roots = budget.default_roots("antigravity")
    names = {s["name"] for s in budget.collect(roots)[0]}
    assert "gone-skill" not in names
    assert any("removed" in why for _, why in budget.SKIPPED)


def test_antigravity_reports_when_no_plugin_dir_is_readable(fake_home, budget):
    budget.default_roots("antigravity")
    assert budget.AG_FILTER_STATE == "unavailable", "no plugins dir -> quote an upper bound"


# ---- agnostic guard ---------------------------------------------------------


def test_agy_cli_is_scoped_like_the_other_runtimes(tmp_path, agnostic):
    unscoped = tmp_path / "skills" / "demo"
    unscoped.mkdir(parents=True)
    target = unscoped / "SKILL.md"
    target.write_text(
        "See docs/harness-adapters.md\n\nInstall it with agy plugin install .\n",
        encoding="utf-8",
    )
    assert agnostic.check(str(target)), "an unscoped agy command must be flagged"

    target.write_text(
        "See docs/harness-adapters.md\n\n| step | Antigravity |\n|---|---|\n"
        "| install the plugin | `agy plugin install .` |\n",
        encoding="utf-8",
    )
    assert agnostic.check(str(target)) == []
