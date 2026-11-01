---
name: jev-mcp-model-server
description: Use when an MCP server must call a relay-gated model.
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "mcp,system-one,auth,token-economy"
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
instead — it resolves the key from the env the harness gives it and sends the correct fingerprint
+ auth. The primary model in scope is `jev-1.13-free`; on free
exhaustion (HTTP 429/403) with a real key, falls back to paid `jev-1.13` (same
key/endpoint/UA; `JEV_FALLBACK_TO_PAID` default 1, `=0` fails open, no-key never).

## Procedure (in order)

Harness-neutral: steps name a capability, and the concrete tool/registry name for
each runtime is in
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md).
Wire the server into the harness you are running in — never another runtime's CLI.

### 1. Stand up a stdio MCP server with zero dependencies
- Pure stdlib: json, sys, os, urllib.request, urllib.error. No `mcp` package (MCP subprocess python has no project venv).
- Methods: initialize -> serverInfo+tools; tools/list -> tool defs; tools/call -> {content:[{type,text}]}. Flush every write.

### 2. Resolve endpoint + model via env (JEV_ENDPOINT / JEV_MODEL)
- jev-1.13-free is a **System One DECISION model** (not chat): endpoint `https://opencode.ai/zen/v1/systemone`, body `{"model","state","questions"}` with `noul`/`choice`/`score` question types, response is probabilities + `usage` (~21 output tokens), NOT `choices`/`delta`.
- Do NOT use `/chat/completions` for jev — it 400s `ModelProtocolUnsupported`.

### 3. Reuse the harness's own secret WITHOUT committing it
- Key precedence: JEV_API_KEY -> OPENCODE_ZEN_API_KEY -> (Hermes only) the active profile `.env`, read directly, never echoed.
- Do not reach into another runtime's config for a key: if it isn't in your env, the harness you're in hasn't been configured yet.
- Debug logs print yes/no + masked prefix/suffix only.

### 4. Auth: real key OR free-tier public bearer (no key)
- Real key -> `Authorization: Bearer <key>`.
- No key -> `Authorization: Bearer public` (relay free-tier public bearer, PR ronail/hermes-agent#1) for `*-free` models ONLY. Verified live: jev-1.13-free at /systemone -> HTTP 200.
- Always send the OpenCode fingerprint: `x-opencode-project: global` + `User-Agent: opencode/1.18.31` (desktop) or `opencode/latest/2.0.29/cli` (cli) + matching `x-opencode-client`. Without an OpenCode UA -> 403 error code 1010. Truly anonymous (no Authorization) -> 403 FreeTierError. Non-free models need a real key.
- `jev` is a namespace prefix AND `jev-1.13-free` is a concrete relay model; the relay only accepts the systemone protocol for it.

### 5. Parse the response correctly
- Non-2xx -> read the body, surface it verbatim (FreeTierError / code 1010 tell you which guard tripped).
- Success body is JSON `{"model","answers":{<q>:{...}},"usage":{...}}` — NOT SSE. Read `answers[<question>]` (`noul` float, etc.).

### 6. Register + verify in your own harness
- Register the *same* `python3 <repo>/mcp/jev/server.py` with whatever MCP registry
  the harness you are running in provides — see the table below. Register it where
  you are; never drive another harness's CLI or write another harness's config.
- Verify with a real `tools/call` on `jev_health` (a `noul` reachability question) —
  a calibrated probability back means the endpoint is alive.
- Canonical working implementation: `mcp/jev/server.py` in https://github.com/ronail/jev-cost-guard (`<repo>` below = your clone).
- Auth matrix + failure modes: references/opencode-zen-free-bypass.md
- Starter file: templates/mcp-stdlib-skeleton.py (systemone stub; copy + modify).

## Registering the server per harness

The **same** `mcp/jev/server.py` loads in every harness because its stdio transport
auto-detects framing: `Content-Length` headers (strict MCP clients) **and**
newline-delimited JSON (line-oriented clients). No per-harness fork. The command is
always `python3 <repo>/mcp/jev/server.py`; only the registry differs:

| harness | register with | credentials |
|---|---|---|
| Hermes | `hermes -p <profile> mcp add jev --command python3 --args <repo>/mcp/jev/server.py`, or `mcp_servers` in the profile `config.yaml` | profile `.env`, or `--env JEV_MODEL=…` |
| Claude Code | `mcpServers.jev` in the Claude Code config (plus `hooks.PostToolUse` for the gate) | that config's `env`, or the OS environment |
| Codex | `codex mcp add jev -- python3 <repo>/mcp/jev/server.py`, or `[mcp_servers.jev]` in `config.toml` | `env_vars` / `[mcp_servers.jev.env]` |
| Antigravity | `agy plugin install .` (bundles `mcp_config.json`), or `.agents/mcp_config.json` for one workspace | that file's `env`, `$PLUGIN_DATA/api_key`, or the OS environment |
| anything else | its MCP registry / config file | its own environment |

Rules that keep this portable: register in **your** harness only; never run another
runtime's CLI or edit another runtime's config from here; and put the key in the
place that harness reads (Hermes: the profile `.env`; others: their own config or
the process env — never a file owned by a different harness).

### Auto-gate (soft, never blocks)

A `PostToolUse`-style hook fires after each tool with the tool's output on stdin.
`hooks/claude-codex-jev-verdict.py` reads
that envelope, and when the output exceeds `JEV_VERDICT_THRESHOLD_CHARS`
(default 4000) pipes it to `mcp/jev/server.py --verdict` (the **one-shot CLI**
baked into the server — the hook hosts no MCP server). jev answers the
`is_successful` noul question and the hook prints a JSON verdict line; it
**always exits 0**, so a jev slip (rate limit / no key / 5xx) never stalls a
session — this is a soft gate that *annotates* results, it does not mutate the
transcript. The verdict reuses `_jev_ask`, so the same auth + paid-jev fallback
(`JEV_FALLBACK_TO_PAID` / `JEV_FALLBACK_MODEL`) applies; no-key runs free-tier
`Bearer public` with no fallback. Register it the same way as the server — in your
own harness's hook registry (Claude Code: `hooks.PostToolUse` in its config; Codex: a
tool-result hook in its `config.toml`; anything else: its hook mechanism). The
script's contract is just stdin envelope → JSON verdict line on stdout, exit 0.

Antigravity is the exception that needs a second script: its `PostToolUse` payload has
no tool output (measured — only `toolCall{name,args}`, `stepIdx`, `error`,
`lastUserInput`), so `hooks/antigravity-jev-verdict.py` runs on **`PreInvocation`**,
reads the tool result out of the transcript the event points at, judges the largest
new one, and injects a single advisory `ephemeralMessage`. Same threshold, same
one-shot `--verdict` CLI, same fail-open, one jev call per model invocation.

## Pitfalls
- The key comes from the harness you are running in (Hermes: the active profile's `.env`, resolved by the loader; elsewhere: that harness's own config or the process env) — never committed, never read from another runtime's directory. See [`mcp/jev/env.example`](https://github.com/ronail/jev-cost-guard/blob/main/mcp/jev/env.example).
- Response is JSON (systemone), NOT SSE — do not assume chat-completions stream parsing for jev.
- No key is NOT "no Authorization": send `Bearer public` for the free tier; only a truly-missing bearer 403s.
- Relay 403 FreeTierError / code 1010 with all conditions met + key last_status ok is relay state or a missing UA, not a code bug; stop retrying.
- The deterministic cap plugin (jev-context-guard) was removed — the model path is the only trim lever; do not re-introduce it.
