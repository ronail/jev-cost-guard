# jev-cost-guard

Token-economy guard for agent harnesses — **Hermes-first, harness-agnostic**. It
attacks the two real token leak classes visible in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas + the skills index
   are the fat. → pruned/audited by `jev-skill-gate` / `jev-prompt-budget-audit`.
2. **Tool output bloats the transcript.** → auto-gated by `jev-1.13-free` through the
   `jev-cost-guard` hook plugin (replaces oversized results with a ~7-token `noul`
   verdict), or judged on-demand via the `mcp/jev` MCP.

> The deterministic head+tail *plugin* (`jev-context-guard`) was removed — it is not
> a jev-model feature. The model-based decision below is the only trimming lever.

---

## How to use

### Install the skills
```bash
# register the repo as a skill source (discovers every jev-* skill)
hermes skills tap add ronail/jev-cost-guard
# install + scan a single skill (fetches sibling references/ too)
hermes skills install ronail/jev-cost-guard/hermes/jev-context-economy --yes
```
`hermes skills tap add` is verified to register the source and list the `jev-*`
skills; `hermes skills install <owner/repo/path> --yes` then downloads, runs the
skills-guard scan, and installs the skill. Add the wanted skills to each profile's
`skills.auto_load` in `config.yaml` (see `SKILLS.md`).

| skill | purpose |
|---|---|
| `jev-skill-gate` | keep the skills index ≤ ~2.5 KB (fat is re-sent every call) |
| `jev-context-economy` | per-turn hygiene: batch calls, grep-then-read, cap output |
| `jev-prompt-budget-audit` | fixed-prompt audit loop |
| `jev-context-firewall` | delegate bulk tool output off the main transcript |
| `jev-delegate-guard` | pick the leanest child profile; break-even gate |
| `jev-test-output-triage` | triaging test/log bloat |
| `jev-source-digest` | triaging web/page bloat |
| **`jev-mcp-model-server`** | usage guide: how/when to drive `jev-1.13-free` via this MCP |

### Mount the MCP (`mcp/jev`)
A zero-dependency (pure stdlib, no pip) MCP server exposing `jev_health` +
`jev_summarize`. It talks to the opencode-zen relay directly — not the agent's own
provider — and uses **only `jev-1.13-free`**.

```bash
hermes -p dev mcp add jev --command python3 --args <repo>/mcp/jev/server.py
hermes -p dev mcp list        # -> jev ... 2 tools  enabled
```

`JEV_MODEL` (default `jev-1.13-free`) and `JEV_ENDPOINT` (default
`https://opencode.ai/zen/v1/systemone`) are **optional** — the server defaults them.
The bearer key is **never** passed on the command line: the MCP reads
`OPENCODE_ZEN_API_KEY` from `profiles/<profile>/.env` at runtime (Hermes does not
inject profile secrets into MCP subprocesses). No browser/OAuth/device-flow. Set
`JEV_DEBUG=1` for masked-key logs (prefix+suffix only). Auth modes (real key from
`.env` or the relay free-tier `Bearer public` no-key bearer; OpenCode desktop UA +
failure modes) are documented in `mcp/jev/env.example` and the server docstring.

| env | default | note |
|---|---|---|
| `JEV_MODEL` | `jev-1.13-free` | optional — default model in scope |
| `JEV_ENDPOINT` | `https://opencode.ai/zen/v1/systemone` | optional — System One endpoint |
| `JEV_FALLBACK_TO_PAID` | `1` | `1` (default) on `jev-1.13-free` exhaustion (HTTP 429/403 with a real key) retry the paid model; `0` fails open; no-key never falls back |
| `JEV_FALLBACK_MODEL` | `jev-1.13` | paid model retried on free exhaustion (same key/endpoint/UA; bills your opencode account) |
| `JEV_DEBUG` | `0` | optional — `1` = print masked key prefix/suffix |
| `OPENCODE_ZEN_API_KEY` | (from `profiles/<profile>/.env`) | required for the real-key path |

### `jev_health` / `jev_summarize`
`jev_health` probes the model with a `noul` reachability question (alive iff it
answers). `jev_summarize` does **not** generate prose — it asks jev a binary `noul`
question over a result `state` and returns a **success probability**, which is what
you `budget_tokens`-gate on. jev is a decision model, not a summarizer.

> The repo also ships the **`jev-cost-guard` hook plugin**, which *automatically*
> gates oversized tool results (`transform_tool_result` → `jev-1.13-free` verdict)
> so bloat is trimmed without the agent asking. Install it inactive, tune first,
> then enable — see [jev-cost-guard plugin](#jev-cost-guard-plugin).

---

## Token savings methodology

`jev-1.13-free` is an opencode-zen **System One *decision* model**: it does not
generate text, it scores a `noul`/`choice`/`score` question over a `state` and
returns a probability + a `usage` object. The leak class below is **tool-output
bloat** — a large result body is re-sent on every subsequent model call, because it
lives in the transcript.

| path | what you keep in the transcript |
|---|---|
| Re-paste the result body to judge "did it succeed?" | the **whole result body** (re-sent every call) |
| One `jev_summarize` verdict instead | **~24 tokens** (the noul probability) |

`jev_summarize` **reads the body once** (so its input scales with the body — see the
measurements), but its **output — the verdict you persist — is a constant ~24 tokens**.
That is the lever: replace a body you would re-send `N` times with a one-time ~24-token
verdict. Break-even vs. keeping the body is ~2–3 subsequent calls for real result
sizes; the bigger the body, the faster it pays off.

---

## Test results (token-saving evidence)

Measured through the **dev profile** — real `OPENCODE_ZEN_API_KEY` (from
`profiles/dev/.env`), `Authorization: Bearer <key>`, desktop `User-Agent`, at
`https://opencode.ai/zen/v1/systemone`, model `jev-1.13-free` (primary; paid `jev-1.13`
is a fallback on free exhaustion — see `JEV_FALLBACK_TO_PAID`). Each row is one `jev_summarize` call: a `noul` "is the above
tool-result successful?" question over a synthetic tool-output body of the stated size.
The relay returns its own `usage`, so the full cost of one judgment is visible.

| body (chars) | ~body tokens | input_tokens | output_tokens | noul |
|---|---|---|---|---|
| 200 | ~50 | 366 | **24** | 0.87 |
| 1,000 | ~250 | 702 | **24** | 0.90 |
| 4,000 | ~1,000 | 1,961 | **24** | 0.89 |
| 20,000 | ~5,000 | 8,678 | **24** | 0.84 |

Two measured, non-negotiable facts:

1. **Input scales with the body** (~`1.7×body + ~280`) — jev reads and bills the full
   state. The "a jev decision costs a flat ~312 tokens" assumption is **false** for
   real results; ~312 was only the tiny `is_reachable` probe (no body).
2. **Output is constant ~24 tokens** — the noul verdict — and it is calibrated: the
   bodies here are all synthetic success output, so a high noul (0.84–0.90) is the
   correct answer; it is not stuck at 0 or 1.

### Reproduced via the `jev-mcp-model-server` skill (dev profile)

Running the skill's shipped reference client
(`hermes/jev-mcp-model-server/templates/mcp-stdlib-skeleton.py` → `_ask`,
`Authorization: Bearer <key>` from `profiles/dev/.env`) — i.e. the same `noul` judgment
a dev profile drives through `jev_summarize` — the verdict the agent **persists** stays
constant as the body grows (the agent keeps just the `noul`, ~7 tokens, vs. re-sending
the whole body every later call; jev's full billed output is the ~24 in the table above):

| body (chars) | ~body tokens | verdict kept | noul |
|---|---|---|---|
| 4,000 | ~1,000 | **~7 tokens** | 0.88 |
| 8,000 | ~2,000 | **~7 tokens** | 0.77 |
| 20,000 | ~5,000 | **~7 tokens** | 0.88 |

A ~5,000-token result collapses to a 7-token `noul` probability — the transcript no
longer re-sends the body on later calls.

Net saving: `jev_summarize` costs `~(1.7×body + ~300)` **once**; persisting the
~24-token verdict instead of the body saves `(body − 24)` tokens on **every subsequent
call**. For a 4,000-char (~1,000-token) body carried across 5 later model calls:
keep-body ≈ 5,000 tokens vs summarize-once + verdict ≈ 1,985 + 5×24 = ~2,105 →
**saves ~2,900 tokens**. Break-even is ~2 subsequent calls; the bigger the body, the
faster it pays off. No other model is involved.

---

## Files

| path | what |
|---|---|
| `mcp/jev/server.py` | the MCP server (stdlib, jev-1.13-free only; auth/failure modes in its docstring) |
| `mcp/jev/mcp.json` | MCP manifest (defaults `JEV_MODEL`/`JEV_ENDPOINT`) |
| `mcp/jev/env.example` | env reference (real key from `.env` + `Bearer public` no-key bearer + failure modes) |
| `hermes/` | the `jev-*` Hermes skills |
| `SKILLS.md` | index + per-profile wiring |
| `docs/token-savings.md` | token-accounting + cap-removal history |
|| `LICENSE` | MIT |
|| `plugin.yaml` | jev-cost-guard plugin manifest (repo-root) |
|| `__init__.py` | jev-cost-guard plugin: register(ctx) + transform_tool_result hook |

## jev-cost-guard plugin

An **opt-in** hook plugin (named to match the repo) that auto-gates oversized tool
results through `jev-1.13-free`, replacing the result in-transcript with a ~7-token
`noul` verdict so the body is not re-sent on later turns.

Install (inactive — the hook does **not** fire until you enable it):

    hermes -p dev plugins install ronail/jev-cost-guard --no-deps

Enable (activates the hook — oversized results are REPLACED by the verdict, so the
original output is dropped from context):

    hermes -p dev plugins enable jev-cost-guard

Tunables (env or `profiles/dev/.env`):

| var | default | meaning |
|---|---|---|
| `JEV_THRESHOLD` | `1000` | gate results > N tokens (~4N chars) |
| `JEV_GATE_TOOLS` | *(empty)* | comma-list to gate only these tools |
| `JEV_SKIP_TOOLS` | `read_file,write_file,patch,edit_file,skill_manage` | never gate these |
| `JEV_DEBUG` | `0` | `1` logs per-call diagnostics |
| `JEV_MODEL` / `JEV_ENDPOINT` | `jev-1.13-free` / `https://opencode.ai/zen/v1/systemone` | the jev target |
| `JEV_FALLBACK_TO_PAID` | `1` | on `jev-1.13-free` exhaustion (HTTP 429/403) with a real key, retry paid `jev-1.13` (same key/endpoint/UA; bills your opencode account); `0` fails open; no-key never falls back |
| `JEV_FALLBACK_MODEL` | `jev-1.13` | paid model retried on free exhaustion |

Auth reuses the dev credential the `jev` MCP uses (`OPENCODE_ZEN_API_KEY` from
`profiles/dev/.env`); `Bearer public` is used for the no-key free tier. **Fail-open**
on any jev error (the original result is kept). The hook skips `jev_health` /
`jev_summarize` and any already-judged `[jev]` verdict (re-entry guard).

> ⚠️ Enabling is systemic: a gated result loses its body. Start with a high
> `JEV_THRESHOLD` and a narrow `JEV_GATE_TOOLS` (e.g. `bash`) to tune before
> broadening.

---

## Using jev in Claude Code / Codex (portable MCP)

The `mcp/jev/server.py` stdio server is **dual-frame**: it answers `Content-Length`
requests (Claude Code, Codex — strict MCP clients) and newline-delimited JSON
(Hermes' client), so the **same server loads in every harness** with no fork.
Expose `OPENCODE_ZEN_API_KEY` (from `profiles/dev/.env`, never committed) in the
harness env, then register `python3 <repo>/mcp/jev/server.py` in the harness's MCP
registry. jev stays the **only** oracle model; Claude Code/Codex are pure clients.

- **Claude Code**: `mcpServers.jev` in `~/.claude/claude_desktop_config.json`;
  the server's tools appear natively — call `jev_summarize` on big outputs.
- **Codex**: `codex mcp add jev -- python3 <repo>/mcp/jev/server.py` (env in
  `codex.yml` `mcp_env` or the shell).

### Auto-gate via hooks (soft — never blocks)

`hooks/claude-codex-jev-verdict.py` is a `PostToolUse`-style hook for Claude Code
and an equivalent hook point for Codex. It reads the tool output; if it exceeds
`JEV_VERDICT_THRESHOLD_CHARS` (default 4000) it pipes it to
`mcp/jev/server.py --verdict` (the server's one-shot CLI — **no** MCP server is
hosted inside the hook), prints a JSON `is_successful` noul verdict, and
**always exits 0** so a jev slip (rate limit / no key / 5xx / paid-exhaustion)
never stalls a session. The verdict reuses `_jev_ask`, so the same `Bearer public`
/ `Bearer <key>` auth and paid-jev fallback (`JEV_FALLBACK_TO_PAID` /
`JEV_FALLBACK_MODEL`) apply; no-key runs free-tier only. Full per-harness wiring
(config snippets) is in the `jev-mcp-model-server` skill under
**Using jev in Claude Code and Codex**.
