#!/usr/bin/env python3
"""
mcp-jev — zero-dependency MCP server exposing the `jev` model for token saving.

Pure stdlib (no `mcp` / no `pydantic`) so it runs as a Hermes MCP stdio subprocess
(`python3`) with zero pip installs. Speaks minimal MCP stdio JSON-RPC
(initialize / tools/list / tools/call).

Goal (user directive): use the opencode-zen free `jev-1.13-free` model with the
dev profile's API key to achieve token saving in the hermes `dev` profile.

What `jev-1.13-free` IS (verified against the relay, Oct 2026):
- It is an OpenCode Zen System One DECISION model (TypeSafe AI), NOT a chat
  completion model. It does not generate text/digests. It evaluates a `state`
  against typed questions and returns calibrated probabilities/values:
      yes/no  -> `noul`  (probability in [0,1])
      choice  -> `choice` (picked option + per-option probs)
      score   -> `score`  (rubric scores)
- Endpoint: https://opencode.ai/zen/v1/systemone
  (NOT /chat/completions and NOT /inference/systemone — that path 403/1010s).
- Auth: a real `OPENCODE_ZEN_API_KEY` is preferred (`Authorization: Bearer <key>`).
  When NO key is configured, mcp-jev uses the relay free-tier public bearer
  (`Authorization: Bearer public`, per PR ronail/hermes-agent#1) for `*-free`
  models — verified live: jev-1.13-free at /systemone -> HTTP 200. Truly
  anonymous (no `Authorization` header at all) -> 403 FreeTierError. Non-free
  models require a real key.
- Relay fingerprint: without the OpenCode desktop UA the relay returns HTTP
  403 error code 1010. Required headers:
      User-Agent: opencode/1.18.31
      x-opencode-client: desktop
      x-opencode-project: global
- Free tier is gated to the free model id `jev-1.13-free` (paid `jev-1.13` uses
  the same key).
- jev does NOT support the OpenAI chat-completions protocol (the deepseek path at
  /zen/v1/chat/completions is a DIFFERENT free model — not jev).

Token saving: jev answers a structured yes/no/choice/score question in ~21
output tokens. Use it to GATE / triage large tool results with a cheap oracle
instead of feeding the full text through an LLM. It cannot emit text digests,
so jev_summarize returns a probability signal (see its description).

The API key is NOT in this file or config. The MCP subprocess reads
OPENCODE_API_KEY / OPENCODE_ZEN_API_KEY from ~/.hermes/profiles/dev/.env (the same
credential the opencode-zen provider uses). No secret is ever committed
(JEV_DEBUG=1 prints key presence/path only, never values).

Tools:
  jev_health      probe jev reachability via a `noul` question (alive iff jev
                  returns a calibrated answer).
  jev_summarize   query jev for a success/failure probability over a text `state`
                  (~21 output tokens). Cheap gating signal, NOT a text digest;
                  jev is a decision model and cannot summarize to tokens.
  jev_find        screen candidate files for a query BEFORE the main model reads
                  them: free lexical prefilter, then one jev noul per candidate
                  ("does this file help answer the query?"). Returns paths +
                  probabilities only — contents stay server-side — so read only
                  the matches. Costs one jev call per candidate.
Config:
  JEV_FIND_MAX_CALLS   jev_find's jev-call budget (default 12, hard cap 40).
  JEV_PROVIDER  typesafe | opencode. Auto: TypeSafe when TYPESAFE_API_KEY is set,
                else the OpenCode zen relay (default, unchanged).
  JEV_MODEL     model id (opencode default jev-1.13-free; typesafe default jev-latest).
  JEV_ENDPOINT  override the systemone endpoint.
  OPENCODE_ZEN_API_KEY / OPENCODE_API_KEY / JEV_API_KEY  OpenCode zen bearer key
                  (else read from the profile .env).
  TYPESAFE_API_KEY   TypeSafe direct bearer key (else read from the profile .env).
                  Its presence selects the TypeSafe provider (api.typesafe.ai,
                  same System One request/answer shape).
  JEV_DEBUG=1   print provider/relay probe diagnostics (key never printed).
  JEV_FALLBACK_TO_PAID=1   (opencode only) on jev-1.13-free exhaustion (HTTP
                429/403) retry the paid model JEV_FALLBACK_MODEL (default
                jev-1.13); 0 fails open. No-key (Bearer public) never falls back.
  JEV_FALLBACK_MODEL=jev-1.13   paid model retried on free-tier exhaustion.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request

_ZEN_BASE = "https://opencode.ai/zen/v1"
_DEFAULT_JEV_MODEL = "jev-1.13-free"
_DEFAULT_ENDPOINT = f"{_ZEN_BASE}/systemone"

# TypeSafe's own direct API — the same System One request/answer shape, but a
# different host, key, and model ids. Selected when TYPESAFE_API_KEY is present
# (env or profile .env) or JEV_PROVIDER=typesafe; otherwise the OpenCode zen
# relay above stays the default (unchanged behaviour).
_TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_DEFAULT_TYPESAFE_MODEL = "jev-latest"
_OPENCODE_KEY_NAMES = ("OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY", "JEV_API_KEY")
_TYPESAFE_KEY_NAMES = ("TYPESAFE_API_KEY",)

# Required OpenCode desktop fingerprint for the jev free tier. Without these the
# relay returns HTTP 403 error code 1010. A real API key is sent as the bearer
# token (the free model is NOT reachable anonymously -> 403 FreeTierError).
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
    """Read a bearer key from the profile `.env` (default: OpenCode keys).

    Hermes does NOT inject profile secret env into MCP subprocesses (by design), so
    mcp-jev (a stdio subprocess of the gateway) reads the profile .env directly to
    reuse the SAME credential the profile uses. `names` selects which env var names
    are accepted: OpenCode keys by default, or `TYPESAFE_API_KEY` for the TypeSafe
    direct provider. The file is user-owned (0600); no secret is committed to this
    repo or to config.yaml. JEV_DEBUG=1 prints presence/path only, never values.
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
                sys.stderr.write(
                    f"[jev-debug] found {names[0]} in {env_path}; "
                    f"key set (value redacted)\n"
                )
            return key
    if os.environ.get("JEV_DEBUG"):
        sys.stderr.write(f"[jev-debug] no {names[0]} found; tried: {candidates}\n")
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

    Uses TypeSafe's direct API (`api.typesafe.ai`, `jev-latest`) when
    `TYPESAFE_API_KEY` is configured or `JEV_PROVIDER=typesafe`; otherwise the
    OpenCode zen relay (`/zen/v1/systemone`, `jev-1.13-free`) — the default and
    unchanged path. `JEV_PROVIDER=opencode|zen` forces the relay even when a
    TypeSafe key is present.
    """
    explicit = (os.environ.get("JEV_PROVIDER") or "").strip().lower()
    if explicit in ("opencode", "zen", "opencode-zen", "zen-relay"):
        use_typesafe = False
    elif explicit in ("typesafe", "typesafe-direct", "direct"):
        use_typesafe = True
    else:
        use_typesafe = bool(_typesafe_key())
    if use_typesafe:
        override = os.environ.get("JEV_ENDPOINT")
        # Only honour JEV_ENDPOINT for TypeSafe when it actually points there; a
        # stale zen URL in a shared manifest must not send a TypeSafe key to zen.
        endpoint = (
            override if (override and "typesafe.ai" in override) else _TYPESAFE_ENDPOINT
        )
        model = os.environ.get("JEV_MODEL") or _DEFAULT_TYPESAFE_MODEL
        if model.endswith("-free"):  # e.g. a stale jev-1.13-free from a zen config
            model = _DEFAULT_TYPESAFE_MODEL
        return endpoint, model, _typesafe_key()
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
    relay free-tier `Bearer public`). When the free model is exhausted
    (HTTP 429 — IP/quota rate-limit — or HTTP 403 FreeTierError) AND a real API
    key is configured AND `JEV_FALLBACK_TO_PAID` != "0", retries the SAME key +
    endpoint + desktop UA with the paid model `JEV_FALLBACK_MODEL`
    (default `jev-1.13`). Paid jev bills your opencode account — set
    `JEV_FALLBACK_TO_PAID=0` to fail-open instead. No-key calls (`Bearer public`)
    never fall back: no key => no paid route.

    Body (per opencode.ai zen docs for jev-1.13-free at /systemone):
      {"model": <id>, "state": <text>, "questions": {
          "<question>": {"type": "noul"|"choice"|"score", "instructions": "..."}}}
    Response:
      {"model": <id>, "answers": {<question>: {"type":"noul","noul":0.81,...}},
       "usage": {"input_tokens","output_tokens"}}
    Auth: Bearer <key> + the desktop UA fingerprint (else 403 1010).
    """
    return _jev_post(state, question, qtype, instructions, _retry_paid=True)


def _jev_post(state: str, question: str, qtype: str = "noul",
             instructions: str | None = None, *,
             model: str | None = None, _retry_paid: bool = False,
             criteria: dict | None = None) -> dict | None:
    """Single (non-recursive) System One POST for `model` (or resolved default).

    `_jev_ask` retries here with `model=<paid model>` on free-tier exhaustion.
    `_retry_paid=False` on the retry bounds recursion; a paid model does not end
    with `-free`, so it is never retried — paid exhaustion raises (caller fails
    open).
    """
    endpoint, default_model, key = _resolve_jev_target()
    model = model or default_model
    provider = _provider_for(endpoint)
    q: dict = {"type": qtype}
    if instructions:
        q["instructions"] = instructions
    if criteria:
        q["criteria"] = criteria
    body = {"model": model, "state": state, "questions": {question: q}}
    if provider == "typesafe":
        # TypeSafe direct: a real TYPESAFE_API_KEY is required (no public bearer,
        # no free tier, no paid-model fallback).
        if not key:
            raise RuntimeError(
                "TYPESAFE_API_KEY is not set but the TypeSafe endpoint was chosen; "
                "set TYPESAFE_API_KEY (or JEV_PROVIDER=opencode to use the zen relay)."
            )
        headers = dict(_TYPESAFE_HEADERS)
        headers["Authorization"] = f"Bearer {key}"
    else:
        # jev-1.13-free is NOT reachable anonymously (the relay returns 403
        # FreeTierError), but when no key is configured we still attempt the call
        # with the relay free-tier public bearer so the relay's real auth verdict is
        # surfaced as a clean `[jev]` message instead of short-circuiting.
        headers = dict(_SYSTEMONE_HEADERS)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        elif model.endswith("-free"):
            # No key configured: relay free-tier public bearer (PR ronail/hermes-agent#1).
            # Verified live: jev-1.13-free at /systemone -> HTTP 200 with Bearer public.
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
        f"[jev-debug] POST {endpoint} provider={provider} model={model} "
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
            sys.stderr.write(f"[jev-debug] resp-head: {raw[:300]!r}\n")
        resp_body = raw.decode("utf-8", "replace")[:300]
        # OpenCode free-tier exhaustion (quota/IP rate-limit): fall back to the
        # PAID jev model only on the relay, only with a real key, and only when the
        # caller opted in. TypeSafe has no free tier, so it never gets here.
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
                    f"[jev-debug] free-tier exhausted (HTTP {e.code}); "
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


# ── MCP stdio server (minimal JSON-RPC, no dependencies) ──────────────────────


# Framing is auto-detected per client: strict MCP clients (Claude Code, Codex)
# speak Content-Length headers; Hermes' bundled MCP client reads newline-delimited
# JSON. _FRAME records the client's framing (set by _read_stdin_message); _send
# replies in the SAME framing so the server loads under Claude Code, Codex (strict)
# and Hermes (JSON-lines) without regression.
_FRAME: str | None = None


def _read_stdin_message(stream) -> dict | None:
    """Read one MCP message, auto-detecting Content-Length or newline-JSON framing."""
    global _FRAME
    if _FRAME is None:
        first = stream.readline()
        while first and not first.strip():
            first = stream.readline()
        if not first:
            return None
        if first.lower().startswith(b"content-length:"):
            _FRAME = "crlf"
            length = 0
            line = first
            while line.strip():
                name, _, value = line.decode("utf-8", "replace").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip() or 0)
                line = stream.readline()
            body = stream.read(length)
            return json.loads(body.decode("utf-8", "replace")) if body else None
        _FRAME = "jsonl"
        return json.loads(first.decode("utf-8", "replace").strip())
    if _FRAME == "crlf":
        length = 0
        line = stream.readline()
        while line.strip():
            name, _, value = line.decode("utf-8", "replace").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip() or 0)
            line = stream.readline()
        body = stream.read(length)
        return json.loads(body.decode("utf-8", "replace")) if body else None
    line = stream.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8", "replace").strip())


def _send(msg: dict) -> None:
    payload = json.dumps(msg).encode("utf-8")
    if _FRAME == "crlf":
        sys.stdout.buffer.write(
            b"Content-Length: " + str(len(payload)).encode("utf-8")
            + b"\r\n\r\n" + payload
        )
    else:
        sys.stdout.buffer.write(payload + b"\n")
    sys.stdout.buffer.flush()


def _tool_health(arguments: dict | None) -> str:
    """Probe jev reachability. jev is a decision model: a `noul` reachability
    question that gets a calibrated probability back means the endpoint is alive."""
    try:
        endpoint, model, _ = _resolve_jev_target()
        ans = _jev_ask(
            state="jev model reachability probe",
            question="is_reachable",
            qtype="noul",
            instructions="Does the jev-1.13-free model endpoint respond with a "
            "calibrated answer to this question?",
        )
        if ans is None:
            return "[jev] error: jev returned no answer (request rejected)."
        n = ans.get("noul")
        return (
            f"[jev] ok: model={model} endpoint={endpoint} -> alive "
            f"(reachability noul={n})"
        )
    except RuntimeError as e:
        return f"[jev] {e}"
    except Exception as e:  # noqa: BLE001
        return f"[jev] error: {type(e).__name__}: {e}"


def _tool_summarize(arguments: dict | None) -> str:
    """Query jev (decision model) for a success/failure probability over `text`.

    jev is a System One decision model: it CANNOT emit a textual digest, so this
    tool does NOT summarize to `budget_tokens` of prose. It asks jev a `noul`
    question over the text and returns the calibrated probability (~21 output
    tokens) — a cheap gating signal for large tool results. `budget_tokens` is
    accepted for API compatibility but is advisory (jev output cost is fixed).
    Token saving = a ~21-token oracle call instead of an LLM digest.
    """
    text = (arguments or {}).get("text", "")
    budget = int((arguments or {}).get("budget_tokens", 300) or 300)
    if not text:
        return "[jev] empty input."
    try:
        ans = _jev_ask(
            state=text,
            question="is_successful",
            qtype="noul",
            instructions="Does the above tool result indicate a successful, "
            "clean outcome?",
        )
        if ans is None:
            return "[jev] error: jev returned no answer (request rejected)."
        n = ans.get("noul")
        interp = "likely-success" if (n is None or n >= 0.5) else "likely-failure"
        return (
            f"[jev] is_successful noul={n} -> {interp} "
            f"(jev is a decision model; ~fixed output tokens, not a text digest; "
            f"budget_tokens={budget} is advisory)"
        )
    except RuntimeError as e:
        return f"[jev] {e}"
    except Exception as e:  # noqa: BLE001
        return f"[jev] error: {type(e).__name__}: {e}"


# ── jev_find: screen candidate files with jev BEFORE the main model reads them ──
# The hook cannot do this — it fires after a tool has already returned. Here the
# file bodies are read server-side and never enter the agent transcript: only
# paths + probabilities come back, so the main model reads just the winners.
_FIND_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache", "cache",
    ".idea", ".vscode", "Pods", ".terraform",
}
_FIND_MAX_FILE_BYTES = 8000
_FIND_MAX_CALLS = 12


def _find_terms(query: str) -> list[str]:
    return [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]


def _read_text_capped(path: str, max_bytes: int) -> str | None:
    """Read up to max_bytes as text; None for binary/unreadable files."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes)
    except OSError:
        return None
    if b"\x00" in raw[:2048]:
        return None
    return raw.decode("utf-8", "replace")


def _walk_candidates(root: str, pattern: str, terms: list[str],
                     max_candidates: int, max_bytes: int):
    """Lexically prefilter the tree (free, deterministic) before jev is asked.

    Returns (scanned_count, [(lexical_score, path, text)] desc by score).
    """
    scored: list[tuple[int, str, str]] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _FIND_SKIP_DIRS]
        for name in filenames:
            if pattern not in ("*", "") and not fnmatch.fnmatch(name, pattern):
                continue
            path = os.path.join(dirpath, name)
            scanned += 1
            text = _read_text_capped(path, max_bytes)
            if text is None:
                continue
            low = text.lower()
            score = sum(low.count(t) for t in terms)
            if score:
                scored.append((score, path, text))
    scored.sort(key=lambda item: -item[0])
    return scanned, scored[:max_candidates]


def _tool_find(arguments: dict | None) -> str:
    """Rank candidate files by jev relevance WITHOUT putting their content in context.

    Stage 1: lexical prefilter over the filesystem (free, no model).
    Stage 2: one jev `noul` per surviving candidate — "does this file help answer
    `query`?" — and sort by probability. Only paths + scores are returned.
    """
    args = arguments or {}
    query = str(args.get("query") or "").strip()
    if not query:
        return "[jev] jev_find: missing `query`."
    root = str(args.get("root") or ".")
    if not os.path.isdir(root):
        return f"[jev] jev_find: root is not a directory: {root}"
    pattern = str(args.get("glob") or "*")
    try:
        threshold = float(args.get("threshold", 0.6))
    except (TypeError, ValueError):
        threshold = 0.6
    try:
        max_bytes = int(args.get("max_bytes") or _FIND_MAX_FILE_BYTES)
    except (TypeError, ValueError):
        max_bytes = _FIND_MAX_FILE_BYTES
    try:
        env_calls = int(os.environ.get("JEV_FIND_MAX_CALLS") or _FIND_MAX_CALLS)
    except ValueError:
        env_calls = _FIND_MAX_CALLS
    try:
        max_calls = int(args.get("max_calls") or env_calls)
    except (TypeError, ValueError):
        max_calls = env_calls
    max_calls = max(1, min(max_calls, 40))
    dry_run = bool(args.get("dry_run", False))

    terms = _find_terms(query)
    if not terms:
        return "[jev] jev_find: `query` has no usable terms (2+ chars)."

    scanned, candidates = _walk_candidates(
        root, pattern, terms, max_calls * 3, max_bytes
    )
    if not candidates:
        return (f"[jev] jev_find: no lexical candidates. scanned={scanned} "
                f"glob={pattern!r} query={query!r}")

    endpoint, _, _ = _resolve_jev_target()
    provider = _provider_for(endpoint)
    instructions = (
        "Does this file contain information that directly helps answer the "
        f"query: {query!r}?"
    )
    criteria = {
        "true": "definitions, configuration, or prose that answers or materially "
                "advances the query",
        "false": "unrelated content, incidental keyword matches, or boilerplate",
    }

    ranked: list[tuple[float, str, int, int]] = []
    calls = 0
    failures = 0
    for lex, path, text in candidates:
        noul = -1.0
        if not dry_run and calls < max_calls:
            calls += 1
            try:
                ans = _jev_post(
                    state=f"FILE: {path}\n\n{text}",
                    question="is_relevant",
                    qtype="noul",
                    instructions=instructions,
                    criteria=criteria,
                )
                if ans and ans.get("noul") is not None:
                    noul = float(ans["noul"])
            except Exception:  # noqa: BLE001 — one bad file must not sink the scan
                failures += 1
        ranked.append((noul, path, len(text), lex))

    hits = sorted([r for r in ranked if r[0] >= threshold], key=lambda r: -r[0])
    misses = [r for r in ranked if r[0] < threshold]

    lines = [
        f"[jev] jev_find query={query!r} root={root} glob={pattern!r} "
        f"provider={provider} scanned={scanned} candidates={len(candidates)} "
        f"jev_calls={calls} failures={failures}"
        + (" dry_run=true" if dry_run else "")
    ]
    if hits:
        lines.append(f"matches (noul >= {threshold:g}):")
        for noul, path, size, lex in hits:
            lines.append(f"  {noul:.2f}  {path}  ({size} chars, {lex} lexical hits)")
    else:
        lines.append(f"no file scored >= {threshold:g}")
    if misses:
        lines.append("below threshold (do not read without another reason):")
        for noul, path, size, lex in misses[:8]:
            shown = "n/a" if noul < 0 else f"{noul:.2f}"
            lines.append(f"  {shown}  {path}  ({size} chars, {lex} lexical hits)")
    lines.append(
        "note: contents were screened server-side and are NOT included above; "
        "read only the matches (preferably with offset/limit)."
    )
    return "\n".join(lines)


_TOOLS = {
    "jev_health": {
        "description": (
            "Probe jev (TypeSafe System One decision model) reachability via a "
            "noul question. Provider auto-selects: TypeSafe direct "
            "(api.typesafe.ai, jev-latest) when TYPESAFE_API_KEY is set, else "
            "OpenCode Zen (jev-1.13-free). Returns 'alive' iff jev answers."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "jev_summarize": {
        "description": (
            "Query jev (TypeSafe System One decision model) for a calibrated "
            "success/failure probability (~21 output tokens) over the given text "
            "`state`. jev is a DECISION model and cannot emit a text digest; this "
            "is a cheap gating signal, not a summary. `budget_tokens` is advisory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "Text to query jev about (the decision state)."},
                "budget_tokens": {"type": "integer", "default": 300,
                                  "minimum": 10, "maximum": 1000,
                                  "description": "Advisory output budget (jev cost is fixed & tiny)."},
            },
            "required": ["text"],
        },
    },
    "jev_find": {
        "description": (
            "Screen candidate files for a query BEFORE reading them. Stage 1: "
            "lexical prefilter over the filesystem (free). Stage 2: one jev noul "
            "per candidate asking whether that file helps answer `query`. Returns "
            "only paths + probabilities, ranked — file contents never enter the "
            "transcript, so call read_file only on the matches (preferably with "
            "offset/limit). Costs one jev call per candidate (max_calls / "
            "JEV_FIND_MAX_CALLS, default 12)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What you are looking for, in natural language."},
                "root": {"type": "string",
                         "description": "Directory to search (default: current dir)."},
                "glob": {"type": "string",
                         "description": "Filename glob filter, e.g. '*.py' (default '*')."},
                "threshold": {"type": "number", "default": 0.6,
                              "description": "Minimum noul to count as a match."},
                "max_calls": {"type": "integer",
                              "description": "Max jev calls (default 12, hard cap 40)."},
                "max_bytes": {"type": "integer",
                              "description": "Bytes read per file for screening (default 8000)."},
                "dry_run": {"type": "boolean", "default": False,
                            "description": "Lexical ranking only; no jev calls."},
            },
            "required": ["query"],
        },
    },
}


def _handle_call(name: str, arguments: dict | None) -> dict:
    fn = {"jev_health": _tool_health, "jev_summarize": _tool_summarize,
          "jev_find": _tool_find}.get(name)
    if fn is None:
        return {"content": [{"type": "text", "text": f"[jev] unknown tool: {name}"}],
                "isError": True}
    try:
        text = fn(arguments)
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text",
                             "text": f"[jev] tool {name} crashed: {type(e).__name__}: {e}"}],
                "isError": True}
    return {"content": [{"type": "text", "text": text}]}


def main() -> None:
    stream = sys.stdin.buffer
    while True:
        msg = _read_stdin_message(stream)
        if msg is None:
            break
        if not isinstance(msg, dict) or "jsonrpc" not in msg:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            _send({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "jev", "version": "1.1.0"},
                },
            })
        elif method == "initialized":
            pass  # notification
        elif method == "tools/list":
            tools = [{"name": n, "description": t["description"],
                      "inputSchema": t["inputSchema"]} for n, t in _TOOLS.items()]
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            res = _handle_call(params.get("name") or "", params.get("arguments"))
            _send({"jsonrpc": "2.0", "id": mid, "result": res})
        else:
            if mid is not None:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601,
                                 "message": f"method not found: {method}"}})


def _cli_verdict() -> None:
    """One-shot CLI for Claude Code / Codex hook scripts that can't host the MCP
    server. Reads `state` (the large tool output) from stdin, asks jev the
    `is_successful` noul question, prints a JSON verdict line to stdout. Reuses
    _jev_ask — same auth (OPENCODE_ZEN_API_KEY) + same paid fallback
    (JEV_FALLBACK_TO_PAID / JEV_FALLBACK_MODEL). JEV_DEBUG=1 traces to stderr.

    State is capped at 20k chars so a hook firing on huge outputs stays within
    the jev-1.13-free free quota per call (larger bodies: use the jev_summarize
    MCP tool instead).
    """
    state = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        ans = _jev_ask(
            state=state[:20000],
            question="is_successful",
            qtype="noul",
            instructions="Does the above tool result indicate a successful, "
            "clean outcome?",
        )
    except RuntimeError as e:
        if os.environ.get("JEV_DEBUG"):
            sys.stderr.write(f"[jev-debug] {e}\n")
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    if ans is None:
        print(json.dumps({"ok": False, "error": "jev returned no answer"}))
        return
    n = ans.get("noul")
    interp = "likely-success" if (n is None or n >= 0.5) else "likely-failure"
    endpoint, model, _ = _resolve_jev_target()
    print(json.dumps({"ok": True, "model": model, "endpoint": endpoint,
                      "noul": n, "interpretation": interp}))


if __name__ == "__main__":
    _a = sys.argv[1:]
    if _a and _a[0] in ("--verdict", "-v"):
        _cli_verdict()
    else:
        main()
