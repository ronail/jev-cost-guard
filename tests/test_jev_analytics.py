"""jev analytics: the ledger, the report, and the price rules.

There is no switch: the MCP server records every jev call and appends an
`in=/out=` suffix, so these tests pin the things that break silently — the script
and the server must resolve the *same* ledger path, an unmeasured call must not be
recorded as zero, the ledger must stay bounded without a `report`, and the report
must never invent a price (official DeepSeek list price or `$ n/a`).
"""
from __future__ import annotations

import importlib.util
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


# ── ledger path parity between the script and the MCP server ──────────────────


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


def test_cli_report_silent_with_no_rows(script, capsys):
    assert script.main(["report"]) == 1
    assert capsys.readouterr().out == ""


def test_cli_report_prints_then_clears(script, capsys):
    script.write_rows([_row()])
    assert script.main(["report", "--model", "deepseek-v4.1-flash"]) == 0
    out = capsys.readouterr().out
    assert "jev analytics · 1 call" in out
    assert script.read_rows() == []


def test_cli_report_keep_retains_rows(script):
    script.write_rows([_row()])
    assert script.main(["report", "--keep"]) == 0
    assert len(script.read_rows()) == 1


def test_stale_rows_are_not_this_turn(script, capsys):
    script.write_rows([_row(ts=time.time() - 4000)])
    assert script.main(["report"]) == 1
    assert capsys.readouterr().out == ""
    assert script.read_rows() == []


def test_drop_retracts_a_row(script):
    script.write_rows([_row(tool="find"), _row(tool="summarize")])
    assert script.main(["drop", "1"]) == 0
    rows = script.read_rows()
    assert len(rows) == 1 and rows[0]["tool"] == "summarize"


def test_drop_out_of_range(script):
    script.write_rows([_row()])
    assert script.main(["drop", "7"]) == 2
    assert len(script.read_rows()) == 1


def test_clear_empties_the_ledger(script):
    script.write_rows([_row(), _row()])
    assert script.main(["clear"]) == 0
    assert script.read_rows() == []


# ── the MCP server side ───────────────────────────────────────────────────────


def test_record_writes_a_row_and_returns_suffix(script, srv):
    srv._USAGE.update({"in": 291, "out": 24, "model": "jev-1.13-free"})
    suffix = srv._analytics_record("summarize", 20000, "verdict!")
    assert suffix == " [jev in=291 out=24]"
    rows = script.read_rows()
    assert len(rows) == 1
    assert rows[0]["body_chars"] == 20000
    assert rows[0]["jev_in"] == 291 and rows[0]["jev_out"] == 24
    assert rows[0]["verdict_chars"] == len("verdict!")
    assert rows[0]["model"] == "jev-1.13-free"


def test_unmeasured_call_records_nothing(script, srv):
    """A dry run / failed call has no usage: no row, no suffix, no fabricated zeros."""
    srv._usage_pop()
    assert srv._analytics_record("find", 9000, "rendered") == ""
    assert script.read_rows() == []


def test_record_resets_usage_between_calls(script, srv):
    srv._USAGE.update({"in": 100, "out": 20, "model": "jev-1.13-free"})
    srv._analytics_record("find", 10, "x")
    srv._analytics_record("find", 10, "x")
    rows = script.read_rows()
    assert [r["jev_in"] for r in rows] == [100]  # the second had no usage


def test_summarize_appends_suffix_and_row(script, srv, monkeypatch):
    monkeypatch.setattr(srv, "_jev_ask", lambda **kw: {"noul": 0.88})
    srv._USAGE.update({"in": 500, "out": 24, "model": "jev-1.13-free"})
    text = srv._tool_summarize({"text": "x" * 4000})
    assert text.endswith("[jev in=500 out=24]")
    rows = script.read_rows()
    assert len(rows) == 1 and rows[0]["body_chars"] == 4000
    assert rows[0]["noul"] == 0.88


def test_record_records_billable_flag(script, srv):
    srv._USAGE.update({"in": 5, "out": 1, "model": "jev-1.13.0", "billable": True})
    srv._analytics_record("summarize", 100, "v")
    assert script.read_rows()[0]["billable"] is True


def test_usage_add_ignores_garbage(srv):
    srv._usage_pop()
    srv._usage_add(None)
    srv._usage_add({"input_tokens": "nope", "output_tokens": 3})
    assert srv._USAGE["in"] == 0 and srv._USAGE["out"] == 3


def test_ledger_is_trimmed_when_it_grows(script, srv, monkeypatch):
    """With no report to clear it, the ledger must stay bounded on its own."""
    monkeypatch.setattr(srv, "_LEDGER_MAX_BYTES", 2000)
    monkeypatch.setattr(srv, "_LEDGER_KEEP_LINES", 5)
    for i in range(40):
        srv._USAGE.update({"in": i, "out": 1, "model": "jev-1.13-free"})
        srv._analytics_record("summarize", 100, "v")
    rows = script.read_rows()
    # 40 appends cannot all survive, and growth is capped at max_bytes + one row.
    assert 0 < len(rows) < 40
    assert rows[-1]["jev_in"] == 39  # the tail survives the trim
    assert os.path.getsize(script.ledger_path()) <= 2000 + 400


def test_trim_is_atomic_and_leaves_no_temp_file(script, srv, monkeypatch):
    """A crash mid-trim must not leave the ledger empty; os.replace only."""
    monkeypatch.setattr(srv, "_LEDGER_MAX_BYTES", 2000)
    monkeypatch.setattr(srv, "_LEDGER_KEEP_LINES", 5)
    for i in range(20):
        srv._USAGE.update({"in": i, "out": 1, "model": "jev-1.13-free"})
        srv._analytics_record("summarize", 100, "v")
    path = script.ledger_path()
    assert not list(Path(path).parent.glob(Path(path).name + ".*.tmp"))


def test_failed_replace_keeps_the_old_ledger(script, srv, monkeypatch):
    monkeypatch.setattr(srv, "_LEDGER_KEEP_LINES", 2)
    for i in range(4):
        srv._USAGE.update({"in": i, "out": 1, "model": "jev-1.13-free"})
        srv._analytics_record("summarize", 100, "v")
    before = script.read_rows()
    monkeypatch.setattr(srv.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    srv._analytics_trim(script.ledger_path())
    assert script.read_rows() == before  # nothing truncated
    assert not list(Path(script.ledger_path()).parent.glob("*.tmp"))


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
