---
name: jev-mcp-model-server
description: Use when an MCP server must call a relay-gated model.
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  hermes:
    tags: [mcp, system-one, auth, token-economy]
category: agent
triggers:
  - use when the jev model must be invoked through an MCP server rather than the agent's own provider path
  - use when the relay gates the external endpoint and an auth bypass is required
---

## When to Use

Use this MCP server when **direct use of the jev model is not feasible** — the
agent cannot call jev directly because `https://opencode.ai/zen/v1/systemone` is
relay-gated (requires an OpenCode UA + `Authorization`; a bare/missing bearer yields
403 FreeTierError, or 403/1010 without an OpenCode UA). Whenever a skill would
otherwise call the jev model directly, route it through this MCP (`mcp/jev/server.py`)
instead — it resolves the key from the profile `.env` and sends the correct fingerprint
+ auth. The only model in scope is `jev-1.13-free` (no fallbacks).

## Procedure (in order)

### 1. Stand up a stdio MCP server with zero dependencies
- Pure stdlib: json, sys, os, urllib.request, urllib.error. No `mcp` package (MCP subprocess python has no project venv).
- Methods: initialize -> serverInfo+tools; tools/list -> tool defs; tools/call -> {content:[{type,text}]}. Flush every write.

### 2. Resolve endpoint + model via env (JEV_ENDPOINT / JEV_MODEL)
- jev-1.13-free is a **System One DECISION model** (not chat): endpoint `https://opencode.ai/zen/v1/systemone`, body `{"model","state","questions"}` with `noul`/`choice`/`score` question types, response is probabilities + `usage` (~21 output tokens), NOT `choices`/`delta`.
- Do NOT use `/chat/completions` for jev — it 400s `ModelProtocolUnsupported`.

### 3. Reuse the profile secret WITHOUT committing it
- Key precedence: JEV_API_KEY -> OPENCODE_ZEN_API_KEY -> profile .env (read directly, never echoed).
- Debug logs print yes/no + masked prefix/suffix only.

### 4. Auth: real key OR free-tier public bearer (no key)
- Real key -> `Authorization: Bearer <key>`.
- No key -> `Authorization: Bearer public` (relay free-tier public bearer, PR ronail/hermes-agent#1) for `*-free` models ONLY. Verified live: jev-1.13-free at /systemone -> HTTP 200.
- Always send the OpenCode fingerprint: `x-opencode-project: global` + `User-Agent: opencode/1.18.31` (desktop) or `opencode/latest/2.0.29/cli` (cli) + matching `x-opencode-client`. Without an OpenCode UA -> 403 error code 1010. Truly anonymous (no Authorization) -> 403 FreeTierError. Non-free models need a real key.
- `jev` is a namespace prefix AND `jev-1.13-free` is a concrete relay model; the relay only accepts the systemone protocol for it.

### 5. Parse the response correctly
- Non-2xx -> read the body, surface it verbatim (FreeTierError / code 1010 tell you which guard tripped).
- Success body is JSON `{"model","answers":{<q>:{...}},"usage":{...}}` — NOT SSE. Read `answers[<question>]` (`noul` float, etc.).

### 6. Wire + verify
- Enable: `hermes config set mcp_servers.jev.enabled true` (then `hermes mcp list` shows `jev ✓ enabled`), or `hermes -p <profile> mcp add jev --command python3 --env JEV_MODEL=jev-1.13-free --args <repo>/mcp/jev/server.py`.
- Verify with a real tools/call on `jev_health` (a `noul` reachability question) — a calibrated probability back means the endpoint is alive.
- Canonical working implementation: `mcp/jev/server.py` in `ronail/jev-cost-guard`.
- Auth matrix + failure modes: references/opencode-zen-free-bypass.md
- Starter file: templates/mcp-stdlib-skeleton.py (systemone stub; copy + modify).

## Pitfalls
- HERMES_HOME is the profile dir under Hermes, not ~/.hermes. Try candidates: HERMES_HOME/.env, HERMES_HOME/profiles/<p>/.env, ~/.hermes/profiles/<p>/.env.
- Response is JSON (systemone), NOT SSE — do not assume chat-completions stream parsing for jev.
- No key is NOT "no Authorization": send `Bearer public` for the free tier; only a truly-missing bearer 403s.
- Relay 403 FreeTierError / code 1010 with all conditions met + key last_status ok is relay state or a missing UA, not a code bug; stop retrying.
- The deterministic cap plugin (jev-context-guard) was removed — the model path is the only trim lever; do not re-introduce it.
