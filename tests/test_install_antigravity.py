"""tests for the no-plugin workspace installer (scripts/install-antigravity.py).

The point is merging: an existing `.agents/mcp_config.json` with other servers, and an
existing `hooks.json` with other hook groups, must survive a run.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "install_antigravity", REPO / "scripts" / "install-antigravity.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot load installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installer():
    return _load()


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".agents").mkdir(parents=True)
    return ws


def test_writes_server_and_gate(installer, workspace):
    assert installer.main(["--workspace", str(workspace)]) == 0
    mcp = json.loads((workspace / ".agents" / "mcp_config.json").read_text())
    hooks = json.loads((workspace / ".agents" / "hooks.json").read_text())

    jev = mcp["mcpServers"]["jev"]
    assert jev["command"] == "python3"
    assert jev["args"] == [str(REPO / "mcp" / "jev" / "server.py")]
    assert "${PLUGIN_ROOT}" not in jev["args"][0], "a workspace path must be absolute"

    handler = hooks["jev-verdict"]["PreInvocation"][0]
    assert handler["type"] == "command"
    assert str(REPO / "hooks" / "antigravity-jev-verdict.py") in handler["command"]
    assert handler["timeout"] == 30


def test_merges_and_is_idempotent(installer, workspace):
    (workspace / ".agents" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}})
    )
    (workspace / ".agents" / "hooks.json").write_text(
        json.dumps({"some-other-hook": {"Stop": []}})
    )
    assert installer.main(["--workspace", str(workspace)]) == 0
    assert installer.main(["--workspace", str(workspace)]) == 0

    mcp = json.loads((workspace / ".agents" / "mcp_config.json").read_text())
    hooks = json.loads((workspace / ".agents" / "hooks.json").read_text())
    assert set(mcp["mcpServers"]) == {"jev", "other"}
    assert set(hooks) == {"jev-verdict", "some-other-hook"}
    assert hooks["some-other-hook"] == {"Stop": []}
    assert len(hooks["jev-verdict"]["PreInvocation"]) == 1


def test_no_hook_leaves_the_hook_file_alone(installer, workspace):
    assert installer.main(["--workspace", str(workspace), "--no-hook"]) == 0
    assert (workspace / ".agents" / "mcp_config.json").is_file()
    assert not (workspace / ".agents" / "hooks.json").exists()


def test_dry_run_writes_nothing(installer, workspace):
    assert installer.main(["--workspace", str(workspace), "--dry-run"]) == 0
    assert not (workspace / ".agents" / "mcp_config.json").exists()


def test_refuses_to_clobber_unparseable_json(installer, workspace):
    (workspace / ".agents" / "mcp_config.json").write_text("{not json")
    with pytest.raises(SystemExit):
        installer.main(["--workspace", str(workspace)])
    assert (workspace / ".agents" / "mcp_config.json").read_text() == "{not json"
