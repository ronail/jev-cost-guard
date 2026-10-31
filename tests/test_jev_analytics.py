"""jev analytics: the switch, the ledger, the report, and the price rules.

The switch is a marker file because a skill cannot toggle itself, so these tests
pin three things that break silently otherwise: the script and the MCP server
must resolve the *same* state directory, an off install must write nothing, and
the report must never invent a price (official DeepSeek list price or `$ n/a`).
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "jev_analytics.py"
SERVER = REPO / "mcp" / "jev" / "server.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def state(monkeypatch, tmp_path):
    """Isolated state dir; every other path knob cleared."""
    for name in ("PLUGIN_DATA", "XDG_STATE_HOME", "JEV_CALL_LOG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JEV_ANALYTICS_DIR", str(tmp_path / "analytics"))
    return tmp_path / "analytics"


@pytest.fixture()
def script(state):
    return _load("jev_analytics_under_test", SCRIPT)


@pytest.fixture()
def srv(state):
    return _load("jev_server_under_test_analytics", SERVER)


def _row(**kw):
    row = {"ts": time.time(), "tool": "summarize", "model": "jev-1.13-free",
           "body_chars": 20000, "jev_in": 1961, "jev_out": 24,
           "verdict_chars": 120, "noul": 0.88}
    row.update(kw)
    return row


def _seed(script, rows):
    script.write_rows(rows)


# ── state parity between the script and the MCP server ────────────────────────


def test_dir_parity_explicit(script, srv, monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_ANALYTICS_DIR", str(tmp_path / "x"))
    assert script.analytics_dir() == srv._analytics_dir()


def test_dir_parity_plugin_data(script, srv, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_ANALYTICS_DIR", raising=False)
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "pd"))
    assert script.analytics_dir() == srv._analytics_dir()
    assert script.analytics_dir().endswith(os.path.join("pd", "analytics"))


def test_dir_parity_xdg_default(script, srv, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_ANALYTICS_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    assert script.analytics_dir() == srv._analytics_dir()


def test_ledger_honours_jev_call_log(script, srv, monkeypatch, tmp_path):
    target = tmp_path / "calls.jsonl"
    monkeypatch.setenv("JEV_CALL_LOG", str(target))
    assert script.ledger_path() == str(target)
    assert srv._analytics_ledger() == str(target)


# ── the switch ────────────────────────────────────────────────────────────────


def test_on_off_status(script, capsys):
    assert script.is_on() is False
    assert script.main(["on"]) == 0
    assert script.is_on() is True
    assert script.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["on"] is True and payload["rows"] == 0
    assert script.main(["off"]) == 0
    assert script.is_on() is False


def test_off_is_idempotent(script):
    assert script.main(["off"]) == 0
    assert script.is_on() is False


# ── the report ────────────────────────────────────────────────────────────────


def test_report_math_flash(script):
    text = script.build_report([_row()], model="deepseek-v4.1-flash")
    assert "1 call (est.)" in text
    assert "20,000 chars ≈ 5,000 tok" in text
    assert "in 1,961 / out 24 tok (free tier: $0)" in text
    # 5000 body - 30 kept = 4970 saved @ $0.15 / 1M
    assert "4,970 tok ≈ $0.00075" in text
    assert "official DeepSeek deepseek-flash" in text


def test_report_unknown_model_prints_na(script):
    text = script.build_report([_row()], model="qwen3.8-flash")
    assert "$ n/a" in text
    assert "official DeepSeek pricing only" in text


def test_report_pro_family_price(script):
    text = script.build_report([_row()], model="deepseek-v4-pro")
    assert "official DeepSeek deepseek-thinking" in text


def test_report_provider_prefix_is_stripped(script):
    text = script.build_report([_row()], model="opencode-go/deepseek-v4.1-flash")
    assert "official DeepSeek deepseek-flash" in text


def test_report_override_price_wins(script):
    text = script.build_report([_row()], model="gpt-5",
                               price_in=2.0, price_out=8.0)
    assert "override $2/$8 per M" in text
    assert "$0.00994" in text  # 4970 tok * $2 / 1M


def test_report_net_loss_is_stated(script):
    text = script.build_report([_row(body_chars=200, verdict_chars=1200)],
                               model="deepseek-v4.1-flash")
    assert "net loss" in text


def test_report_billable_jev_is_not_priced(script):
    text = script.build_report([_row(billable=True)], model="deepseek-v4.1-flash")
    assert "jev cost unpriced" in text
    assert "free tier: $0" not in text


def test_report_empty_is_empty(script):
    assert script.build_report([], model="deepseek-v4.1-flash") == ""


def test_cli_report_silent_when_off(script, capsys):
    _seed(script, [_row()])
    assert script.main(["report"]) == 1
    assert capsys.readouterr().out == ""


def test_cli_report_prints_then_clears(script, capsys):
    script.main(["on"])
    _seed(script, [_row()])
    assert script.main(["report", "--model", "deepseek-v4.1-flash"]) == 0
    out = capsys.readouterr().out
    assert "jev analytics · 1 call" in out
    assert script.read_rows() == []


def test_cli_report_keep_retains_rows(script):
    script.main(["on"])
    _seed(script, [_row()])
    assert script.main(["report", "--keep"]) == 0
    assert len(script.read_rows()) == 1


def test_stale_rows_are_not_this_turn(script, capsys):
    script.main(["on"])
    capsys.readouterr()  # discard the `on` banner
    _seed(script, [_row(ts=time.time() - 4000)])
    assert script.main(["report"]) == 1
    assert capsys.readouterr().out == ""
    assert script.read_rows() == []


def test_drop_retracts_a_row(script):
    script.main(["on"])
    _seed(script, [_row(tool="find"), _row(tool="summarize")])
    assert script.main(["drop", "1"]) == 0
    rows = script.read_rows()
    assert len(rows) == 1 and rows[0]["tool"] == "summarize"


def test_drop_out_of_range(script):
    script.main(["on"])
    _seed(script, [_row()])
    assert script.main(["drop", "7"]) == 2
    assert len(script.read_rows()) == 1


# ── the MCP server side ───────────────────────────────────────────────────────


def test_note_writes_a_row_when_on(script, srv):
    script.main(["on"])
    srv._USAGE.update({"in": 291, "out": 24, "model": "jev-1.13-free"})
    assert srv._analytics_note("summarize", 20000, "verdict!") == " [ja]"
    rows = script.read_rows()
    assert len(rows) == 1
    assert rows[0]["body_chars"] == 20000
    assert rows[0]["jev_in"] == 291 and rows[0]["jev_out"] == 24
    assert rows[0]["verdict_chars"] == len("verdict!")
    assert rows[0]["model"] == "jev-1.13-free"


def test_note_is_silent_and_writes_nothing_when_off(script, srv):
    assert script.is_on() is False
    assert srv._analytics_note("summarize", 20000, "verdict") == ""
    assert not os.path.exists(script.ledger_path())


def test_note_resets_usage_between_calls(script, srv):
    script.main(["on"])
    srv._USAGE.update({"in": 100, "out": 20, "model": "jev-1.13-free"})
    srv._analytics_note("find", 10, "x")
    srv._analytics_note("find", 10, "x")
    rows = script.read_rows()
    assert [r["jev_in"] for r in rows] == [100, 0]


def test_summarize_appends_marker_and_row(script, srv, monkeypatch):
    script.main(["on"])
    monkeypatch.setattr(srv, "_jev_ask", lambda **kw: {"noul": 0.88})
    text = srv._tool_summarize({"text": "x" * 4000})
    assert text.endswith("[ja]")
    rows = script.read_rows()
    assert len(rows) == 1 and rows[0]["body_chars"] == 4000
    assert rows[0]["noul"] == 0.88


def test_summarize_off_has_no_marker(srv, monkeypatch):
    monkeypatch.setattr(srv, "_jev_ask", lambda **kw: {"noul": 0.88})
    assert not srv._tool_summarize({"text": "x" * 4000}).endswith("[ja]")


def test_note_records_billable_flag(script, srv):
    script.main(["on"])
    srv._USAGE.update({"in": 5, "out": 1, "model": "jev-1.13.0", "billable": True})
    srv._analytics_note("summarize", 100, "v")
    assert script.read_rows()[0]["billable"] is True


def test_usage_add_ignores_garbage(srv):
    srv._usage_pop()
    srv._usage_add(None)
    srv._usage_add({"input_tokens": "nope", "output_tokens": 3})
    assert srv._USAGE["in"] == 0 and srv._USAGE["out"] == 3


# ── pricing ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model,family", [
    ("deepseek-v4.1-flash", "deepseek-flash"),
    ("deepseek-v4-flash", "deepseek-flash"),
    ("deepseek-v4-pro", "deepseek-thinking"),
    ("deepseek-reasoner", "deepseek-thinking"),
    ("deepseek-chat", None),
    ("qwen3.8-flash", None),
    ("gpt-5", None),
])
def test_family_inference(script, model, family):
    assert script.deepseek_family(model) == family


def test_price_table_is_official_only(script):
    assert script.price_for("deepseek-v4.1-flash") == (0.15, 0.60)
    assert script.price_for("deepseek-v4-pro") == (0.66, 1.98)
    assert script.price_for("openrouter/deepseek/deepseek-v4.1-flash") == (0.15, 0.60)
    assert script.price_for("gpt-5") is None
