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

The jev call is a direct, verified POST (auth ported from mcp/jev/server.py's
`_jev_ask`). Provider order is COST-AWARE, because this hook fires automatically
on every oversized result:
  1. `JEV_PROVIDER=typesafe|opencode` (explicit) wins;
  2. else the OpenCode zen free tier whenever an OpenCode key exists —
     `jev-1.13-free` at https://opencode.ai/zen/v1/systemone, `Bearer <key>` or
     `Bearer public` when keyless; free-tier exhaustion (HTTP 429/403) with a key
     retries paid `jev-1.13` per `JEV_FALLBACK_TO_PAID` / `JEV_FALLBACK_MODEL`;
  3. else TypeSafe direct (https://api.typesafe.ai/v1/systemone, `jev-latest`,
     `Bearer TYPESAFE_API_KEY`) so the hook never goes dark when no zen key is
     configured; TypeSafe bills per call, which is why it is never preferred;
  4. else the keyless zen public bearer (legacy behaviour).
Keys are read from ~/.hermes/profiles/<profile>/.env; never committed and never
printed (`JEV_DEBUG=1` logs presence/path only). jev is a System One *decision*
model emitting noul/choice/score (~21 output tokens), not a chat model.
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

# TypeSafe's own direct API (same System One shape). Used only when explicitly
# requested (JEV_PROVIDER=typesafe) or as a LAST-RESORT fallback when no OpenCode
# key exists — never preferred over the free zen tier, because this hook fires
# automatically on every oversized result and TypeSafe bills per call.
_TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_DEFAULT_TYPESAFE_MODEL = "jev-latest"
_OPENCODE_KEY_NAMES = ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY", "JEV_API_KEY")
_TYPESAFE_KEY_NAMES = ("TYPESAFE_API_KEY",)

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

# TypeSafe direct needs only standard JSON headers (no OpenCode UA fingerprint).
_TYPESAFE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _load_profile_env_key(
    profile: str = "dev",
    names: tuple[str, ...] = _OPENCODE_KEY_NAMES,
) -> str | None:
    """Read a bearer key from the profile .env (default: OpenCode keys).

    Hermes does not inject profile secret env into the in-process plugin host,
    so we read profiles/<profile>/.env directly to reuse the SAME credential the
    profile uses. `names` selects which env var names are accepted. No secret is
    committed to this repo. JEV_DEBUG=1 prints presence/path only, never values.
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
                    if k.strip() in names:
                        key = v.strip().strip('"').strip("'")
                        break
        except OSError:
            return None
        if key:
            if os.environ.get("JEV_DEBUG"):
                # presence/path only — never the key value (not even a prefix)
                sys.stderr.write(
                    f"[jev-cost-guard] found {names[0]} in {env_path}; "
                    f"key set (value redacted)\n"
                )
            return key
    if os.environ.get("JEV_DEBUG"):
        sys.stderr.write(f"[jev-cost-guard] no {names[0]} found; tried: {candidates}\n")
    return None


def _provider_for(endpoint: str) -> str:
    """'typesafe' for api.typesafe.ai, else 'opencode' (zen relay)."""
    return "typesafe" if "typesafe.ai" in endpoint else "opencode"


def _typesafe_key() -> str | None:
    """TYPESAFE_API_KEY from the environment, else from the profile .env."""
    return os.environ.get("TYPESAFE_API_KEY") or _load_profile_env_key(
        names=_TYPESAFE_KEY_NAMES
    )


def _resolve_jev_target() -> tuple[str, str, str | None]:
    """Return (endpoint, model, api_key) for the System One call.

    Cost-aware provider order:
      1. explicit JEV_PROVIDER=typesafe|opencode;
      2. otherwise prefer the OpenCode zen free tier whenever an OpenCode key
         exists (free, and this hook fires automatically);
      3. else fall back to TypeSafe direct if TYPESAFE_API_KEY is present, so the
         hook never goes dark just because the OpenCode key was removed;
      4. else the keyless zen public bearer (unchanged legacy behaviour).
    """
    explicit = (os.environ.get("JEV_PROVIDER") or "").strip().lower()
    oc_key = (
        os.environ.get("JEV_API_KEY")
        or os.environ.get("OPENCODE_ZEN_API_KEY")
        or os.environ.get("OPENCODE_API_KEY")
        or _load_profile_env_key()
    )
    if explicit in ("opencode", "zen", "opencode-zen", "zen-relay"):
        use_typesafe = False
    elif explicit in ("typesafe", "typesafe-direct", "direct"):
        use_typesafe = True
    else:
        use_typesafe = not oc_key and bool(_typesafe_key())
    if use_typesafe:
        override = os.environ.get("JEV_ENDPOINT")
        # Only honour JEV_ENDPOINT when it actually points at TypeSafe.
        endpoint = (
            override if (override and "typesafe.ai" in override) else _TYPESAFE_ENDPOINT
        )
        model = os.environ.get("JEV_MODEL") or _DEFAULT_TYPESAFE_MODEL
        if model.endswith("-free"):  # stale jev-1.13-free from a zen config
            model = _DEFAULT_TYPESAFE_MODEL
        return endpoint, model, _typesafe_key()
    endpoint = os.environ.get("JEV_ENDPOINT") or _DEFAULT_ENDPOINT
    model = os.environ.get("JEV_MODEL") or _DEFAULT_JEV_MODEL
    return endpoint, model, oc_key


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
    provider = _provider_for(endpoint)
    q: dict = {"type": qtype}
    if instructions:
        q["instructions"] = instructions
    body = {"model": model, "state": state, "questions": {question: q}}
    if provider == "typesafe":
        # TypeSafe direct: real key required, no public bearer, no free tier, no
        # paid-model fallback.
        if not key:
            raise RuntimeError(
                "TYPESAFE_API_KEY is not set but the TypeSafe endpoint was chosen; "
                "set TYPESAFE_API_KEY (or JEV_PROVIDER=opencode to use the zen relay)."
            )
        headers = dict(_TYPESAFE_HEADERS)
        headers["Authorization"] = f"Bearer {key}"
    else:
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
    auth_tag = "key" if key else "public-bearer"
    dbg = (
        f"[jev-cost-guard] POST {endpoint} provider={provider} model={model} "
        f"auth={auth_tag} -> "
    )
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
        # free-tier exhaustion: fall back to paid jev only on the ZEN relay, only
        # with a real key, and only when opted in. TypeSafe has no free tier.
        if (
            provider == "opencode"
            and _retry_paid
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
            f"jev request failed: HTTP {e.code} "
            f"(provider={provider}, auth={auth_tag}). response: {resp_body}"
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
            f"(jev System One decision; ~7 output tokens, not a digest; "
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
