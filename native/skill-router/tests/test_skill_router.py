"""Offline probes for the native skill-routing hook (no key, no network).

The TypeSafe transport is replaced with a scripted callable so the gate/fits
thresholds, shadow mode, caching and fail-open behaviour are exercised exactly
as they run live."""
import json
import os
from pathlib import Path

import pytest

from jev_skill_router import Router, _fm


@pytest.fixture
def skills_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    (d / "jev-source-digest").mkdir()
    (d / "jev-source-digest" / "SKILL.md").write_text(
        "---\nname: jev-source-digest\n"
        "description: Triaging web/page bloat and deciding which sources to read.\n"
        "---\n# Source digest\n", encoding="utf-8")
    (d / "other-skill").mkdir()
    (d / "other-skill" / "SKILL.md").write_text(
        "---\nname: other-skill\n"
        "description: Does one thing and stops.\n"
        "---\nBody\n", encoding="utf-8")
    return d


def _router(skills_dir, mode="live", enabled=True, gate=0.30, fits=0.40,
            cache_path=None, post=None, **kw):
    settings = dict(mode=mode, enabled=enabled, gate=gate, fits=fits,
                    roster_dir=str(skills_dir), chunk=240, excerpt=700,
                    suggest_chars=4000, timeout=10.0, model="jev-1.13.0",
                    cache_path=cache_path or "")
    settings.update(kw)
    r = Router(ctx=None, settings=settings)
    if post is not None:
        r._post = post
    return r


def test_roster_parses_skills(skills_dir):
    r = Router(ctx=None, settings={"roster_dir": str(skills_dir)})
    names = [n for n, _ in r.roster()]
    assert "jev-source-digest" in names and "other-skill" in names
    desc = [d for n, d in r.roster() if n == "jev-source-digest"][0]
    assert desc.startswith("Triaging") and len(desc) <= 200


def test_no_roster_is_silent(skills_dir, tmp_path):
    # point roster_dir at an empty dir
    r = _router(tmp_path / "empty", post=lambda s, q: pytest.fail("should not call"))
    assert r.suggest("tell me if the latest merged PR contains UI changes?") is None


def _make_router_two_stage(skills_dir, cache_path, mode, gate=0.30, fits=0.40):
    """Router whose _post switches payload based on question keys: wide vs narrow."""
    state = {"calls": 0}

    def post(state_text, questions):
        state["calls"] += 1
        if any(k.startswith("need::") for k in questions):
            # wide: gates + need
            ans = {
                "gate_material": {"noul": 0.9},
                "gate_procedure": {"noul": 0.8},
                "gate_prose": {"noul": 0.8},  # inverted -> 0.2 ; gate_mean=(.9+.8+.2)/3=.63
                "need::jev-source-digest": {"noul": 0.9},
                "need::other-skill": {"noul": 0.1},
            }
        else:
            # narrow: fits
            ans = {
                "fits::jev-source-digest": {"noul": 0.91},
                "fits::other-skill": {"noul": 0.2},
            }
        return ans

    r = _router(skills_dir, mode=mode, gate=gate, fits=fits, cache_path=cache_path)
    r._post = post
    r._call_counter = state
    return r


def test_live_injects_skill_line(skills_dir, tmp_path):
    cache = tmp_path / "cache.json"
    r = _make_router_two_stage(skills_dir, str(cache), mode="live")
    out = r.suggest("tell me if the latest merged PR contains UI changes?")
    assert out is not None
    assert "jev-source-digest" in out
    assert "p=0.91" in out


def test_shadow_decides_but_injects_nothing(skills_dir, tmp_path):
    cache = tmp_path / "cache.json"
    r = _make_router_two_stage(skills_dir, str(cache), mode="shadow")
    assert r.suggest("tell me if the latest merged PR contains UI changes?") is None
    # decision still computed + cached
    assert cache.exists()


def test_gate_below_is_silent(skills_dir, tmp_path):
    cache = tmp_path / "cache.json"
    r = _make_router_two_stage(skills_dir, str(cache), mode="live", gate=0.95)
    # gates 0.63 < 0.95 -> no skill
    assert r.suggest("tell me if the latest merged PR contains UI changes?") is None
    assert r._call_counter["calls"] > 0


def test_fits_below_is_silent(skills_dir, tmp_path):
    cache = tmp_path / "cache.json"
    r = _make_router_two_stage(skills_dir, str(cache), mode="live", fits=0.99)
    # winner need 0.91 < fits 0.99 -> no injection
    assert r.suggest("tell me if the latest merged PR contains UI changes?") is None


def test_fail_open_does_not_break_turn(skills_dir, tmp_path):
    def boom(state, questions):
        raise TimeoutError("synthetic")
    r = _router(skills_dir, cache_path=str(tmp_path / "c.json"), post=boom)
    assert r.suggest("tell me if the latest merged PR contains UI changes?") is None


def test_cache_hit_skips_api(skills_dir, tmp_path):
    cache = tmp_path / "cache.json"
    r = _make_router_two_stage(skills_dir, str(cache), mode="live")
    turn = "tell me if the latest merged PR contains UI changes?"
    # first decision populates the cache via the live poster
    r.suggest(turn)
    # now replace _post with one that MUST NOT be called
    def must_not_call(state, questions):
        raise AssertionError("cache hit should not hit the API")
    r._post = must_not_call
    out = r.suggest(turn)
    assert out is not None and "jev-source-digest" in out


def test_fm_strips_quotes():
    md = "---\nname: \"my skill\"\ndescription: a `quoted` skill\n---\nbody"
    assert _fm(md, "name") == "my skill"
    assert _fm(md, "description") == "a `quoted` skill"
