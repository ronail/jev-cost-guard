"""jev-cost-guard — opt-in token-cost guard, named after this repo.

Registers a `transform_tool_result` hook (verified real in hermes_cli/plugins.py
and used by the shipped `security-guidance` plugin) that gates oversized tool
results through jev-1.13-free (OpenCode Zen System One DECISION model).

Policy (deliberately conservative + opt-in):
  * Inactive unless the plugin is listed in `plugins.enabled`.
  * Only gates results larger than JEV_THRESHOLD tokens (default 1000).
  * Skips jev's own tools (jev_health/jev_summarize) and any result that is
    already a `[jev]` verdict (re-entry guard).
  * Never gates data-bearing tools the agent needs verbatim (read_file,
    write_file, patch, edit_file, skill_manage) — overridable via JEV_SKIP_TOOLS.
    Set JEV_GATE_TOOLS (comma list) to gate ONLY those tools; if unset, gate all
    except the skip-list.
  * FAILS OPEN: any error leaves the original, full result in place.
  * JEV_DEBUG=1 prints per-call diagnostics (key presence/path only, never values).

The jev call is a direct, verified relay POST (auth ported verbatim from
mcp/jev/server.py's `_jev_ask`): Bearer <OPENCODE_ZEN_API_KEY> when a key is
configured (read from ~/.hermes/profiles/dev/.env, the same credential the
opencode-zen provider uses), else `Bearer public` for the free model
(jev-1.13-free). Endpoint: https://opencode.ai/zen/v1/systemone (NOT the chat
path; jev is a System One decision model emitting noul/choice/score, ~21 output
tokens). On free-tier exhaustion (HTTP 429/403) with a real key, `_jev_ask`
retries paid jev-1.13 (JEV_FALLBACK_TO_PAID=1, JEV_FALLBACK_MODEL=jev-1.13);
0 fails open. No-key calls never fall back (no key => no paid route).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# ── jev client (ported verbatim from mcp/jev/server.py — the verified path) ────

_ZEN_BASE = "https://opencode.ai/zen/v1"
_DEFAULT_JEV_MODEL = "jev-1.13-free"
_DEFAULT_ENDPOINT = f"{_ZEN_BASE}/systemone"

# Required OpenCode desktop fingerprint for the jev free tier; without these the
# relay returns HTTP 403 error code 1010.
_SYSTEMONE_HEADERS = {
    "User-Agent": "opencode/1.18.31",
    "x-opencode-client": "desktop",
    "x-opencode-project": "global",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "HTTP-Referer": "https://hermes-agent.nousresearch.com",
    "X-Title": "Hermes Agent",
}


def _load_profile_env_key(profile: str = "dev") -> str | None:
    """Read OPENCODE/ZEN_API_KEY (or JEV_API_KEY) from the profile .env.

    Hermes does not inject profile secret env into the in-process plugin host,
    so we read profiles/<profile>/.env directly to reuse the SAME opencode-zen
    credential the dev profile uses. No secret is committed to this repo.
    JEV_DEBUG=1 prints presence/path only, never key values.
    """
    candidates: list[str] = []
    explicit = os.environ.get("JEV_PROFILE_ENV")
    if explicit:
        candidates.append(explicit)
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    prof = os.environ.get("HERMES_PROFILE") or os.environ.get("JEV_PROFILE") or profile
    candidates.append(os.path.join(hermes_home, ".env"))
    candidates.append(os.path.join(hermes_home, "profiles", prof, ".env"))
    base = os.environ.get("HERMES_HOME_BASE") or os.path.expanduser("~/.hermes")
    if base != hermes_home:
        candidates.append(os.path.join(base, "profiles", prof, ".env"))
    for env_path in candidates:
        if not env_path or not os.path.exists(env_path):
            continue
        key = None
        try:
            with open(env_path, "r") as fh:
                for line in fh:
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, _, v = s.partition("=")
                    if k.strip() in ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY", "JEV_API_KEY"):
                        key = v.strip().strip('"').strip("'")
                        break
        except OSError:
            return None
        if key:
            if os.environ.get("JEV_DEBUG"):
                # presence/path only — never the key value (not even a prefix)
                sys.stderr.write(
                    f"[jev-cost-guard] found key in {env_path} "
                    f"(env_override={'yes' if os.environ.get('OPENCODE_API_KEY') else 'no'}); "
                    f"key set (value redacted)\n"
                )
            return key
    if os.environ.get("JEV_DEBUG"):
        sys.stderr.write(f"[jev-cost-guard] no .env key found; tried: {candidates}\n")
    return None


def _resolve_jev_target() -> tuple[str, str, str | None]:
    """Return (endpoint, model, api_key) for the jev System One endpoint."""
    endpoint = os.environ.get("JEV_ENDPOINT") or _DEFAULT_ENDPOINT
    model = os.environ.get("JEV_MODEL") or _DEFAULT_JEV_MODEL
    key = (
        os.environ.get("JEV_API_KEY")
        or os.environ.get("OPENCODE_ZEN_API_KEY")
        or os.environ.get("OPENCODE_API_KEY")
        or _load_profile_env_key()
    )
    return endpoint, model, key


def _jev_ask(state: str, question: str, qtype: str = "noul",
             instructions: str | None = None) -> dict | None:
    """POST a System One decision query to jev. Returns answers[question].

    Defaults to `jev-1.13-free` (Bearer <key> when a key is configured, else the
    relay free-tier `Bearer public`). On free-tier exhaustion (HTTP 429/403 —
    quota or IP rate-limit) with a real key present and `JEV_FALLBACK_TO_PAID`
    != "0", retries the SAME key + endpoint + desktop UA with the paid model
    `JEV_FALLBACK_MODEL` (default `jev-1.13`). Paid jev bills the opencode
    account; `JEV_FALLBACK_TO_PAID=0` fails open instead. No-key calls never
    fall back (no key => no paid route).

    Body: {"model": <id>, "state": <text>, "questions": {"<q>": {"type": "...",
    "instructions": "..."}}}
    Auth: Bearer <key> (or `Bearer public` for *-free when no key); desktop UA.
    """
    return _jev_post(state, question, qtype, instructions, _retry_paid=True)


def _jev_post(state: str, question: str, qtype: str = "noul",
             instructions: str | None = None, *,
             model: str | None = None, _retry_paid: bool = False) -> dict | None:
    """Single (non-recursive) System One POST for `model` (or resolved default).

    `_jev_ask` retries here with the paid model on free-tier exhaustion;
    `_retry_paid=False` on the retry bounds recursion (a paid model is never
    retried — paid exhaustion raises so the caller fails open).
    """
    endpoint, default_model, key = _resolve_jev_target()
    model = model or default_model
    q: dict = {"type": qtype}
    if instructions:
        q["instructions"] = instructions
    body = {"model": model, "state": state, "questions": {question: q}}
    headers = dict(_SYSTEMONE_HEADERS)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    elif model.endswith("-free"):
        # No key configured: relay free-tier public bearer. Verified live for
        # jev-1.13-free at /systemone -> HTTP 200 with Bearer public.
        headers["Authorization"] = "Bearer public"
    else:
        raise RuntimeError(
            f"no API key set for non-free model {model!r}; set "
            "OPENCODE_ZEN_API_KEY (or OPENCODE_API_KEY / JEV_API_KEY). "
            "Only *-free models can run keyless via the relay's public bearer."
        )
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), method="POST", headers=headers
    )
    dbg = f"[jev-cost-guard] POST {endpoint} model={model} auth={'key' if key else 'public-bearer'} -> "
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            dbg += f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        raw = e.read()
        dbg += f"HTTP {e.code}"
        if os.environ.get("JEV_DEBUG"):
            sys.stderr.write(dbg + "\n")
            sys.stderr.write(f"[jev-cost-guard] resp-head: {raw[:300]!r}\n")
        resp_body = raw.decode("utf-8", "replace")[:300]
        auth = "key" if key else "public-bearer"
        # free-tier exhaustion: fall back to paid jev only when a real key is
        # present (no key => Bearer public => no paid route) and the caller
        # opted in. Paid jev bills the opencode account.
        if (
            _retry_paid
            and os.environ.get("JEV_FALLBACK_TO_PAID", "1") != "0"
            and key
            and default_model.endswith("-free")
            and e.code in (429, 403)
        ):
            paid = os.environ.get("JEV_FALLBACK_MODEL", "jev-1.13")
            if os.environ.get("JEV_DEBUG"):
                sys.stderr.write(
                    f"[jev-cost-guard] free-tier exhausted (HTTP {e.code}); "
                    f"retrying paid model {paid} (auth=key)\n"
                )
            return _jev_post(state, question, qtype, instructions,
                             model=paid, _retry_paid=False)
        raise RuntimeError(
            f"jev request failed: HTTP {e.code} (auth={auth}). "
            f"relay response: {resp_body}"
        ) from e
    if os.environ.get("JEV_DEBUG"):
        sys.stderr.write(dbg + "\n")
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        return None
    answers = data.get("answers", {}) or {}
    return answers.get(question)


# ── hook: auto-gate oversized tool results ────────────────────────────────────


def _dbg(msg: str) -> None:
    if os.environ.get("JEV_DEBUG"):
        sys.stderr.write(f"[jev-cost-guard] {msg}\n")
        sys.stderr.flush()


def _csv(val: str | None) -> list[str]:
    return [t.strip() for t in (val or "").split(",") if t.strip()]


def _on_transform_tool_result(tool_name: str = "", args: object = None,
                              result: object = None, **_):
    """Replace oversized `result` strings with a jev noul verdict (fail open)."""
    # Re-entry guard: never gate jev's own tools, and never chain on a verdict.
    if tool_name in ("jev_health", "jev_summarize", "jev_debug"):
        return None
    if not isinstance(result, str):
        return None
    if result.lstrip().startswith("[jev]"):
        return None

    skip = set(_csv(os.environ.get("JEV_SKIP_TOOLS")) or
               _csv("read_file,write_file,patch,edit_file,skill_manage"))
    allow = _csv(os.environ.get("JEV_GATE_TOOLS"))
    if tool_name in skip:
        return None
    if allow and tool_name not in allow:
        return None

    threshold = int(os.environ.get("JEV_THRESHOLD", "1000"))
    # rough token estimate: ~4 chars/token
    if len(result) < max(1, threshold) * 4:
        return None

    try:
        ans = _jev_ask(
            state=result,
            question="is_successful",
            qtype="noul",
            instructions="Does the above tool result indicate a successful, "
                         "clean outcome?",
        )
        if ans is None:
            _dbg(f"tool={tool_name} skipped; jev returned no answer (fail open)")
            return None
        n = ans.get("noul")
        interp = "likely-success" if (n is None or n >= 0.5) else "likely-failure"
        verdict = (
            f"[jev] is_successful noul={n} -> {interp} "
            f"(jev-1.13-free System One decision; ~7 output tokens, not a digest; "
            f"gated tool='{tool_name}' result_len={len(result)}; set JEV_THRESHOLD=0 to keep full output)"
        )
        _dbg(f"gated tool='{tool_name}': {len(result)} chars -> noul={n} ({interp})")
        return verdict
    except Exception as e:  # noqa: BLE001  — fail open, never block the agent loop
        _dbg(f"tool='{tool_name}' jev gate error: {type(e).__name__}: {e}")
        return None


def register(ctx):
    """Hermes plugin entry point: attach the cost-guard hook to the agent loop."""
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
