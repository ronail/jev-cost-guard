"""Key resolution: env -> credential file -> Hermes profile -> keyless relay.

The file path matters because Codex scrubs the environment of the stdio MCP
servers it spawns, so a plugin-provided server can only hold a credential it
reads itself.
"""
from __future__ import annotations

import importlib.util
import json
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
    "PLUGIN_DATA",
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


def test_plugin_data_is_the_write_target(srv, monkeypatch, tmp_path):
    """A plugin-launched server stores the key where its host owns the dir."""
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    path = Path(srv._key_file_path())
    assert path == tmp_path / "plugin-data" / "api_key"
    srv._cli_set_key(["ts-plugin"])
    assert path.read_text().strip() == "ts-plugin"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert srv._typesafe_key() == "ts-plugin"


def test_plugin_data_is_the_only_candidate(srv, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    (tmp_path / "plugin-data").mkdir()
    (tmp_path / "plugin-data" / "api_key").write_text("plugin-key\n")
    assert srv._key_file_candidates() == [str(tmp_path / "plugin-data" / "api_key")]
    assert srv._typesafe_key() == "plugin-key"


def test_no_global_fallback_when_owner_is_unknown(srv, monkeypatch):
    """No PLUGIN_DATA and no explicit path means no file exists to read."""
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    assert srv._key_file_candidates() == []
    assert srv._key_file_path() is None
    assert srv._typesafe_key() is None


def test_legacy_xdg_file_is_reported_but_never_read(srv, monkeypatch, tmp_path):
    """A pre-0.6.3 ~/.config file is surfaced for migration, not trusted."""
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setattr(srv, "_LEGACY_KEY_FILE", str(tmp_path / "legacy" / "api_key"))
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "api_key").write_text("legacy-key\n")
    assert srv._legacy_key_file() == str(tmp_path / "legacy" / "api_key")
    assert srv._key_file_candidates() == []
    assert srv._typesafe_key() is None


def test_explicit_key_file_beats_plugin_data(srv, monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setenv("JEV_API_KEY_FILE", str(tmp_path / "explicit"))
    assert srv._key_file_candidates() == [str(tmp_path / "explicit")]


def test_set_key_refuses_without_an_owner(srv, monkeypatch, capsys):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    srv._cli_set_key(["ts-abc"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "PLUGIN_DATA" in out["error"]
    assert "--path" in out["error"]


def test_set_key_path_flag_names_the_file(srv, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    target = tmp_path / "explicit" / "api_key"
    srv._cli_set_key(["--path", str(target), "ts-explicit"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["path"] == str(target)
    assert target.read_text().strip() == "ts-explicit"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_codex_error_points_at_plugin_data_path(srv, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setenv("PLUGIN_ROOT", "/plugin")
    message = srv._typesafe_required_error()
    assert message is not None
    assert str(tmp_path / "plugin-data" / "api_key") in message
    assert "--set-key" in message


def test_error_without_owner_asks_for_a_path(srv, monkeypatch):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setenv("PLUGIN_ROOT", "/plugin")
    message = srv._typesafe_required_error()
    assert message is not None
    assert "PLUGIN_DATA" in message
    assert "--path" in message
    assert "~/.config" not in message


def test_key_status_reports_candidates_and_source(srv, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    srv._cli_set_key(["ts-status"])
    capsys.readouterr()
    srv._cli_key_status()
    out = json.loads(capsys.readouterr().out)
    assert out["key_file"] == str(tmp_path / "plugin-data" / "api_key")
    assert out["key_source"] == f"file:{tmp_path / 'plugin-data' / 'api_key'}"
    assert out["key_file_candidates"] == [str(tmp_path / "plugin-data" / "api_key")]
    assert out["provider"] == "typesafe"


def test_clear_key_removes_candidate_and_legacy(srv, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.setattr(srv, "_LEGACY_KEY_FILE", str(tmp_path / "legacy" / "api_key"))
    (tmp_path / "plugin-data").mkdir()
    (tmp_path / "plugin-data" / "api_key").write_text("plugin-key\n")
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "api_key").write_text("legacy-key\n")
    srv._cli_clear_key()
    assert not (tmp_path / "plugin-data" / "api_key").exists()
    assert not (tmp_path / "legacy" / "api_key").exists()
    assert srv._typesafe_key() is None
