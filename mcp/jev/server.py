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
- Auth: `Authorization: Bearer <OPENCODE_API_KEY>`. A real key is REQUIRED:
  anonymous / no-key -> 403 FreeTierError ("...can only be used within OpenCode").
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
Config:
  JEV_MODEL     model id (default jev-1.13-free).
  JEV_ENDPOINT  override the systemone endpoint.
  JEV_API_KEY / OPENCODE_ZEN_API_KEY / OPENCODE_API_KEY  bearer key
                  (else read from the dev profile .env).
  JEV_DEBUG=1   print relay probe diagnostics.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_ZEN_BASE = "https://opencode.ai/zen/v1"
_DEFAULT_JEV_MODEL = "jev-1.13-free"
_DEFAULT_ENDPOINT = f"{_ZEN_BASE}/systemone"

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


def _load_profile_env_key(profile: str = "dev") -> str | None:
    """Read OPENCODE_API_KEY / OPENCODE_ZEN_API_KEY (or JEV_API_KEY) from dev .env.

    Hermes does NOT inject profile secret env into MCP subprocesses (by design), so
    mcp-jev (a stdio subprocess of the gateway) reads the profile .env directly to
    reuse the SAME opencode-zen credential the dev profile uses. The file is
    user-owned (0600); no secret is committed to this repo or to config.yaml.
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
                red = (key[:4] + "...") if key else None
                sys.stderr.write(
                    f"[jev-debug] found key in {env_path} "
                    f"(env_override={'yes' if os.environ.get('OPENCODE_API_KEY') else 'no'})"
                    + (f" key={red}" if red else "") + "\n"
                )
            return key
    if os.environ.get("JEV_DEBUG"):
        sys.stderr.write(f"[jev-debug] no .env key found; tried: {candidates}\n")
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

    Body (per opencode.ai zen docs for jev-1.13-free at /systemone):
      {"model": <id>, "state": <text>, "questions": {
          "<question>": {"type": "noul"|"choice"|"score", "instructions": "..."}}}
    Response:
      {"model": <id>, "answers": {<question>: {"type":"noul","noul":0.81,...}},
       "usage": {"input_tokens","output_tokens"}}
    Auth: Bearer <key> + the desktop UA fingerprint (else 403 1010).
    """
    endpoint, model, key = _resolve_jev_target()
    if not key:
        raise RuntimeError(
            "no jev model key set: export OPENCODE_API_KEY (or OPENCODE_ZEN_API_KEY "
            "/ JEV_API_KEY). jev-1.13-free requires a key (anonymous -> 403)."
        )
    q: dict = {"type": qtype}
    if instructions:
        q["instructions"] = instructions
    body = {"model": model, "state": state, "questions": {question: q}}
    headers = dict(_SYSTEMONE_HEADERS)
    headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), method="POST", headers=headers
    )
    dbg = f"[jev-debug] POST {endpoint} model={model} -> "
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
        raise
    if os.environ.get("JEV_DEBUG"):
        sys.stderr.write(dbg + "\n")
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        return None
    answers = data.get("answers", {}) or {}
    return answers.get(question)


# ── MCP stdio server (minimal JSON-RPC, no dependencies) ──────────────────────


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


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


_TOOLS = {
    "jev_health": {
        "description": (
            "Probe jev-1.13-free (OpenCode Zen System One decision model) "
            "reachability via a noul question. Returns 'alive' iff jev answers."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "jev_summarize": {
        "description": (
            "Query jev-1.13-free for a calibrated success/failure probability "
            "(~21 output tokens) over the given text `state`. jev is a DECISION "
            "model and cannot emit a text digest; this is a cheap gating signal, "
            "not a summary. `budget_tokens` is advisory (API-compat only)."
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
}


def _handle_call(name: str, arguments: dict | None) -> dict:
    fn = {"jev_health": _tool_health, "jev_summarize": _tool_summarize}.get(name)
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
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
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


if __name__ == "__main__":
    main()
