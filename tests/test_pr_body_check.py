"""The PR body checker: every defect it exists to catch, demonstrated.

Both defects that motivated it were mechanical — a spliced matrix row and a test count
that outlived the file it described — so the tests feed it exactly those shapes and assert
it refuses them, plus a clean body it must accept.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("pr_body_check", ROOT / "scripts" / "pr_body_check.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("cannot load scripts/pr_body_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def counts(checker, monkeypatch):
    """Deterministic counts: the file has 80, the suite has 137."""
    real = checker.collected
    monkeypatch.setattr(checker, "collected", lambda path=None: 80 if path else 137)
    return real


GOOD = """| host | gate |
|---|---|
| **Claude Code** | yes |
| **Codex** | yes |
| **Antigravity** | yes |
| **Hermes** | no |

| `tests/test_pretool_gate.py` | **80 passed** — everything |
| whole suite on this branch | **137 passed** |
"""


def test_a_body_that_matches_the_branch_passes(checker):
    problems, _lines, corrections = checker.audit(GOOD)
    assert problems == [] and corrections == {}


def test_a_stale_test_count_is_caught_and_rewritten(checker):
    body = GOOD.replace("**80 passed**", "**47 passed**")
    problems, lines, corrections = checker.audit(body)
    assert any("test_pretool_gate.py says 47" in p for p in problems)
    assert corrections["tests/test_pretool_gate.py:47"] == 80
    assert "**80 passed**" in "\n".join(lines)


def test_a_stale_suite_count_is_caught(checker):
    body = GOOD.replace("**137 passed**", "**151 passed**")
    problems, _lines, corrections = checker.audit(body)
    assert any("whole suite says 151" in p for p in problems)
    assert corrections["suite:151"] == 137


def test_a_truncated_host_matrix_is_refused(checker):
    """The real regression: an edit spliced the Antigravity row out of a 4-host table."""
    body = GOOD.replace("| **Antigravity** | yes |\n", "")
    problems, _lines, _corrections = checker.audit(body)
    assert any("3 of 4 hosts" in p and "Antigravity" in p for p in problems)


def test_a_retired_claim_is_refused(checker):
    body = GOOD + "\nIt is not remembered by default.\n"
    problems, _lines, _corrections = checker.audit(body)
    assert any("retired claim" in p for p in problems)


def test_a_body_without_a_host_matrix_is_not_penalised(checker):
    """Docs-only PRs never show a host table; the check must not invent one."""
    problems, _lines, _corrections = checker.audit("| `tests/test_pretool_gate.py` | **80 passed** |\n")
    assert problems == []
