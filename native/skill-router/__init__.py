"""jev-cost-guard native skill-routing hook.

Mirrors the `typesafe-skill-router` pattern, implemented INSIDE this repo so
there is no third-party plugin dependency:

* `pre_llm_call` fires once per fresh user turn.
* It asks Jev (`jev-1.13.0` on TypeSafe, `jev-1.13-free` on the zen relay — the
  provider/model is resolved per call by `mcp/jev/server._resolve_jev_target`,
  so nothing is pinned here and keyless `Bearer public` zen works) which
  installed skill, if any, fits the turn, using ONLY the live skill roster
  (name + first 200 chars of `description`, chunked; shortlisted candidates also
  send the first `excerpt` chars of their `SKILL.md`). Never sent: conversation
  history, files, memory, or credentials.
* If a skill fits (gate + fits thresholds, enforced in code), in `live` mode it
  injects one advisory line naming the skill. In `shadow` mode it logs the
  decision and injects nothing.
* The hook only *suggests*; the model still chooses to load a skill. Any error
  is logged as one line and returns `None` — a routing failure can never break a
  turn (fail-open).
* Transport is NOT duplicated: the hook locates this repo's `mcp/jev/server.py`
  (repo root, `JEV_SERVER_PATH`, or the installed `jev-cost-guard` plugin) and
  calls its `_systemone_post`; if it cannot be located the hook stays inert
  (fail-open) instead of shipping a second HTTP client.

This is a native (Python) plugin entry — a portable `plugin.json` cannot carry
`pre_llm_call`, which is exactly the seam that bridges a natural prompt to a jev
tool call.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("jev_skill_router")


# --- reuse this repo's transport (mcp/jev/server.py), never duplicate it ------
def _load_server() -> Any:
    """Path-load `mcp/jev/server.py` so auth/provider/model resolution is shared.

    Order: repo root (`native/skill-router` -> `../..`), `JEV_SERVER_PATH`, then
    any installed plugin that ships the portable package. Returns `None` when it
    cannot be found, in which case the hook stays inert (fail-open) instead of
    re-implementing HTTP here.
    """
    candidates: list[Path] = [
        Path(__file__).resolve().parents[2] / "mcp" / "jev" / "server.py",
    ]
    env_path = os.environ.get("JEV_SERVER_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    # sibling plugin in the SAME plugins directory (installed companion layout:
    # `<profile>/plugins/jev-cost-guard-skill-router/` sits beside
    # `<profile>/plugins/jev-cost-guard/mcp/jev/server.py`). This is the common
    # case once installed under a profile; the repo-root candidate above only
    # fires when running from the source checkout.
    candidates += sorted(Path(__file__).resolve().parents[1].glob("*/mcp/jev/server.py"))
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    candidates += sorted(home.glob("plugins/*/mcp/jev/server.py"))
    candidates += sorted(home.glob("profiles/*/plugins/*/mcp/jev/server.py"))
    candidates += sorted(home.glob("plugins/*/*/mcp/jev/server.py"))
    candidates += sorted(home.glob("profiles/*/plugins/*/*/mcp/jev/server.py"))
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("jev_mcp_server", cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "_systemone_post"):
                return mod
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("jev server load failed at %s: %s", cand, exc)
    return None


_SERVER = _load_server()  # may be None -> hook stays inert

GATE_QUESTIONS = (
    ("gate_material", "does this turn act on the user's own material or need a tool to act on it?",
     {"true": "yes, the turn needs a skill/tool to act", "false": "no, it is chatter or a question"}),
    ("gate_procedure", "does this turn follow a documented procedure from an installed skill?",
     {"true": "yes, a skill's procedure applies", "false": "no documented procedure fits"}),
    ("gate_prose", "would a prose-only answer (no tool) satisfy this turn?",
     {"true": "yes, prose alone suffices", "false": "no, something more is needed"}),
)


class Router:
    """Stateful routing decisioner. Pure methods are unit-testable."""

    def __init__(self, ctx: Any = None, settings: Optional[dict] = None):
        self._ctx = ctx
        self._settings = dict(settings or {})

    # -- config helpers --------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            if self._ctx is not None:
                v = self._ctx.get_config(key)  # type: ignore[attr-defined]
                if v is not None:
                    if isinstance(default, bool):
                        return bool(v)
                    if isinstance(default, (int, float)):
                        return type(default)(v)
                    return str(v)
        except Exception:
            pass
        v = self._settings.get(key, default)
        if v is None:
            return default
        return v

    enabled = property(lambda self: self._cfg("enabled", False))
    mode = property(lambda self: self._cfg("mode", "shadow"))
    gate = property(lambda self: float(self._cfg("gate", 0.30)))
    fits = property(lambda self: float(self._cfg("fits", 0.40)))
    shortlist = property(lambda self: int(self._cfg("shortlist", 3)))
    chunk = property(lambda self: int(self._cfg("chunk", 240)))
    excerpt = property(lambda self: int(self._cfg("excerpt", 700)))
    suggest_chars = property(lambda self: int(self._cfg("suggest_chars", 4000)))
    timeout = property(lambda self: float(self._cfg("timeout", 10.0)))
    # "" => let mcp/jev/server._resolve_jev_target pick the model per key
    model = property(lambda self: str(self._cfg("model", "")))

    def _roster_dirs(self) -> list[Path]:
        cfg = str(self._cfg("roster_dir", "") or "").strip()
        if cfg:
            return [Path(p) for p in cfg.split(",") if p.strip()]
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        dirs = [home / "skills"]
        for p in home.glob("profiles/*/skills"):
            dirs.append(p)
        for p in (home / "plugins").glob("*/skills"):
            dirs.append(p)
        return dirs

    def roster(self) -> list[tuple[str, str]]:
        """`[(name, description[:200])]` over every SKILL.md found."""
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for d in self._roster_dirs():
            if not d.is_dir():
                continue
            for md in d.rglob("SKILL.md"):
                try:
                    head = md.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                name = _fm(head, "name")
                desc = _fm(head, "description") or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                out.append((name, desc[:200]))
        return out

    # -- cache -----------------------------------------------------------------
    def _cache_key(self, user_message: str, roster_sig: str) -> str:
        h = hashlib.sha256()
        h.update(self.mode.encode()); h.update(self.model.encode())
        h.update(roster_sig.encode()); h.update(user_message.strip()[:self.suggest_chars].encode())
        return h.hexdigest()

    def _cached(self, key: str) -> Optional[tuple[str, float]]:
        p = str(self._cfg("cache_path", "") or "").strip()
        if not p:
            return None
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        rec = data.get(key)
        return (rec["skill"], rec["fits"]) if rec else None

    def _cache_put(self, key: str, skill: str, fits: float) -> None:
        p = str(self._cfg("cache_path", "") or "").strip()
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[key] = {"skill": skill, "fits": fits}
        Path(p).write_text(json.dumps(data), encoding="utf-8")

    # -- jev transport ---------------------------------------------------------
    def _post(self, state: str, questions: dict) -> dict:
        if _SERVER is None:
            raise RuntimeError("mcp/jev/server.py not located; install jev-cost-guard")
        # model=None => _resolve_jev_target picks TypeSafe jev-1.13.0 or keyless
        # zen jev-1.13-free; no authentication/client logic is duplicated here.
        return _SERVER._systemone_post(state, questions)

    # -- decision --------------------------------------------------------------
    def decide(self, user_message: str) -> Optional[tuple[str, float]]:
        roster = self.roster()
        if not roster:
            return None
        if len(user_message) > self.suggest_chars:
            return None
        roster_sig = json.dumps(roster, sort_keys=True)
        ckey = self._cache_key(user_message, roster_sig)
        if (hit := self._cached(ckey)) is not None:
            return hit

        state = user_message.strip()
        # wide: gate nouls + need-noul per skill, chunked (≤255 options each)
        best: dict[str, float] = {}
        for i in range(0, len(roster), self.chunk):
            skills = roster[i:i + self.chunk]
            q: dict[str, Any] = {
                k: {"type": "noul", "instructions": instr, "criteria": crit}
                for k, instr, crit in GATE_QUESTIONS
            }
            for name, desc in skills:
                q[f"need::{name}"] = {
                    "type": "noul",
                    "instructions": f"Does this turn specifically need the skill '{name}'?",
                    "criteria": {"true": f"yes, the turn needs '{name}'", "false": "no"},
                }
            try:
                ans = self._post(state, q)
            except Exception as exc:
                _log(self, f"wide request failed: {type(exc).__name__}: {exc}")
                return None
            g = [float((ans.get(k) or {}).get("noul", 0.0)) for k, *_ in GATE_QUESTIONS]
            gate_mean = (g[0] + g[1] + (1.0 - g[2])) / 3.0
            if gate_mean < self.gate:
                continue
            for (name, _desc), k in zip(skills, [f"need::{n}" for n, _ in skills]):
                best[name] = float((ans.get(k) or {}).get("noul", 0.0))
        if not best:
            return self._cache_and_return(ckey, None)

        # narrow: fits noul for the top `shortlist` candidates
        shortlist = sorted(best, key=best.get, reverse=True)[:self.shortlist]  # type: ignore[arg-type]
        narrow_state = state + "\n\nShortlisted skills and excerpts:\n"
        q2: dict[str, Any] = {}
        for name in shortlist:
            excerpt = _excerpt_for(name, roster, self.excerpt)
            if excerpt:
                narrow_state += f"### {name}\n{excerpt}\n\n"
            q2[f"fits::{name}"] = {
                "type": "noul",
                "instructions": f"Does the skill '{name}' do the specific thing this turn asks for?",
                "criteria": {"true": "yes, it directly does the asked thing", "false": "no"},
            }
        try:
            ans2 = self._post(narrow_state, q2)
        except Exception as exc:
            _log(self, f"narrow request failed: {type(exc).__name__}: {exc}")
            return None
        winner = None
        winner_fits = -1.0
        for name in shortlist:
            f = float((ans2.get(f"fits::{name}") or {}).get("noul", 0.0))
            if f > winner_fits:
                winner_fits, winner = f, name
        if winner is None or winner_fits < self.fits:
            return self._cache_and_return(ckey, None)
        return self._cache_and_return(ckey, (winner, winner_fits))

    def _cache_and_return(self, ckey, val):
        if val is not None:
            self._cache_put(ckey, val[0], val[1])
        return val

    def suggest(self, user_message: str) -> Optional[str]:
        """Return the advisory `<ctx>` line (live) or None (shadow/off/no-skill)."""
        if self.mode == "off" or not self.enabled:
            return None
        try:
            decision = self.decide(user_message)
        except Exception as exc:
            _log(self, f"fail-open: {type(exc).__name__}: {exc}")
            return None
        if decision is None:
            _log(self, f"no skill matched (mode={self.mode})")
            return None
        name, fits = decision
        _log(self, f"skill={name} fits={fits:.2f} mode={self.mode}")
        if self.mode == "shadow":
            return None  # decided, logged, injected nothing
        return (f"jev-skill-route: jev says `<code>{name}</code>` fits (p={fits:.2f}). "
                f"The model still chooses to load it.")


# -- small YAML-ish front-matter reader (no pyyaml dependency) ----------------
def _fm(text: str, key: str) -> str:
    if not text or not text.startswith("---"):
        return ""
    lines = text.splitlines()
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith(key + ":") or line.startswith(key + " "):
            val = line.split(":", 1)[1].strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            return val
    return ""


def _excerpt_for(name: str, roster: list[tuple[str, str]], n: int) -> str:
    """Return the first `n` chars of the SKILL.md for `name` (scans roster dirs)."""
    r = Router()
    for d in r._roster_dirs():
        if not d.is_dir():
            continue
        for md in d.rglob("SKILL.md"):
            if _fm(md.read_text("utf-8", "ignore"), "name") == name:
                try:
                    return md.read_text("utf-8", "ignore")[:n]
                except OSError:
                    return ""
    return ""


def _log(r: Router, msg: str) -> None:
    p = str(r._cfg("log_path", "") or "").strip()
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    log.info(line)
    if p:
        try:
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# -- host glue ----------------------------------------------------------------
def register(ctx: Any) -> None:
    """Register the pre_llm_call hook + on/off/status command with the host."""
    r = Router(ctx=ctx)
    ctx.register_hook("pre_llm_call", lambda **kw: r.suggest(kw.get("request") or kw.get("user_message") or ""))
    ctx.register_command("jev-skill-router", _command, description="jev-cost-guard skill router: on/off/status/suggest")


def _command(raw: str = "", *, session_id: str = "") -> str:
    parts = (raw or "").split()
    r = Router()
    head = parts[:1]
    if not head or head == ["status"]:
        return (f"mode={r.mode} gate={r.gate} fits={r.fits} enabled={'yes' if r.enabled else 'no'} "
                f"roster={len(r.roster())} skills server={'ok' if _SERVER is not None else 'missing'}")
    if head == ["on"]:
        return "set enabled=true mode=live in plugins.entries.jev-cost-guard-skill-router.settings"
    if head == ["off"]:
        return "set enabled=false in plugins.entries.jev-cost-guard-skill-router.settings"
    if head == ["shadow"]:
        return "set mode=shadow in plugins.entries.jev-cost-guard-skill-router.settings"
    if head == ["suggest"]:
        turn = " ".join(parts[1:])
        out = r.suggest(turn)
        return out or "(no skill matched / shadow)"
    return "usage: jev-skill-router [status|on|off|shadow|suggest \"<turn>\"]"
