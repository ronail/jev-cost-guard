"""Key resolution: env -> credential file -> Hermes profile -> keyless relay.

The file path matters because Codex scrubs the environment of the stdio MCP
servers it spawns, so a plugin-provided server can only hold a credential it
reads itself.
"""
from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "mcp" / "jev" / "server.py"

_CLEARED = (
    "TYPESAFE_API_KEY",
    "JEV_API_KEY",
    "OPENCODE_ZEN_API_KEY",
    "OPENCODE_API_KEY",
    "JEV_PROVIDER",
    "JEV_REQUIRE_TYPESAFE",
    "HERMES_HOME",
    "HERMES_PROFILE",
    "JEV_PROFILE",
    "JEV_MCP",
    "PLUGIN_ROOT",
    "CLAUDE_PLUGIN_ROOT",
)


@pytest.fixture()
def srv(monkeypatch, tmp_path):
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JEV_API_KEY_FILE", str(tmp_path / "api_key"))
    spec = importlib.util.spec_from_file_location("jev_server_under_test", SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(srv, text: str) -> Path:
    path = Path(srv._key_file_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_no_key_anywhere(srv):
    assert srv._typesafe_key() is None
    assert srv._typesafe_opt_out() is False


def test_env_key_wins_over_file(srv, monkeypatch):
    _write(srv, "file-key\n")
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    assert srv._typesafe_key() == "env-key"


def test_file_key_used_when_env_absent(srv, monkeypatch):
    path = _write(srv, "  file-key\n")
    assert srv._typesafe_key() == "file-key"
    endpoint, model, key = srv._resolve_jev_target()
    assert key == "file-key"
    assert endpoint == "https://api.typesafe.ai/v1/systemone"
    assert model.startswith("jev-")
    assert path.exists()


def test_sentinel_in_file_selects_relay(srv):
    _write(srv, "none\n")
    assert srv._typesafe_key() is None
    assert srv._typesafe_opt_out() is True
    endpoint, model, key = srv._resolve_jev_target()
    assert "opencode.ai" in endpoint
    assert model.endswith("-free")
    assert key is None


def test_placeholder_and_blank_never_become_credentials(srv, monkeypatch):
    _write(srv, "${user_config.typesafe_api_key}\n")
    assert srv._typesafe_key() is None
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
    assert srv._typesafe_key() is None


def test_codex_host_requires_key_and_points_at_set_key(srv, monkeypatch):
    monkeypatch.setenv("PLUGIN_ROOT", "/plugin")
    assert srv._require_typesafe_flag() is True
    message = srv._typesafe_required_error()
    assert message and "--set-key" in message
    assert srv._key_file_path() in message


def test_set_key_writes_0600(srv, capsys):
    srv._cli_set_key(["ts-abc"])
    out = capsys.readouterr().out
    path = Path(srv._key_file_path())
    assert path.read_text().strip() == "ts-abc"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert '"mode": "600"' in out
    assert srv._typesafe_key() == "ts-abc"


def test_clear_key_removes_file(srv):
    _write(srv, "ts-abc\n")
    srv._cli_clear_key()
    assert not Path(srv._key_file_path()).exists()
    assert srv._typesafe_key() is None
