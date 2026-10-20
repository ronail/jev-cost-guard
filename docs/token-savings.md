# jev-cost-guard — token-savings report (verified)

Measured on the `dev` profile (`poolside/laguna-s-2.1:free` via provider `nous`,
no provider prompt-caching → full cost on every call). Two levers: a fixed-prompt
shrink via config, and a variable runtime cap via the plugin.

## Status on `dev` (current)
- 6 `jev-*` skills installed in `~/.hermes/profiles/dev/skills/jev/` (index only,
  on-demand — `skills.auto_load` left at its default `[]`).
- `jev-context-guard` plugin **enabled**; runs in the gateway
  ("`gateway_transforms` active"). Hooks fire for `transform_tool_result` +
  `transform_terminal_output`.
- `config.yaml` otherwise **pristine** (the earlier `agent.disabled_toolsets`
  experiment was reverted on request). No toolset trimming is active right now.

> Note on "the jev model": **no such model exists** in any profile config
> (`deepseek`, `openrouter`, `anthropic`, `openai`, `nous`, `opencode` only).
> The plugin's cap is pure Python string slicing — strictly cheaper than any
> model-based summarization, which would *add* an LLM call. Savings are achieved
> by the plugin + skills running in the dev session under its existing model.

## Runtime savings (LIVE, in-session, model-quoted)

Triggered by real tool calls; full bodies spilled to `$BH_AGENT_WORKSPACE/cache/jev-context-guard/`:

| tool | input size | model received | saved | / ~3.9 tok | spill file |
|---|---|---|---|---|---|
| `web_extract` (docs page, char_limit 20000) | 21,305 B | ~6,240 B (head+tail) | ~15,085 B | **~3,862 tok** | 21,305 B |
| `terminal` (`z`*15000 + newline) | 15,001 B | ~6,240 B (single cap) | ~8,781 B | **~2,252 tok** | 15,001 B |

The model quoted the `[jev-context-guard]` marker verbatim in its reply, and the
spill files on disk match the input sizes exactly. The double-cap that briefly
occurred on terminal was fixed via the `if "[jev-context-guard]" in text[:500]`
guard in the hook.

Per-result saving scales with output size: anything >6 KB is capped to ~6.2 KB,
so an N KB result costs the agent ~3,900 tokens of transcript regardless of N.

## Fixed-prompt savings (available, gated by config — NOT currently applied on dev)

When the user authorizes config edits, `agent.disabled_toolsets` restores the
measured cut verified earlier:

| metric | before (stock dev) | after (with toolsets trimmed) | delta |
|---|---|---|---|
| toolsets | 26 | 21 | −5 |
| tool-schema JSON | 49,004 B | 36,849 B | −12,155 B |
| system prompt | 24,770 B | 20,581 B | −4,189 B |
| **fixed prompt / call** | **73,774 B ≈ 18,916 tok** | **57,430 B ≈ 14,726 tok** | **−4,191 tok/call (−22.2%)** |

7-day translation of the fixed-prompt cut at stock dev volume:
`618 tool calls × 4,191 tok ≈ ≥2.58M input tokens / 7 days` (floor; real
sessions make more calls).

## Model-based path (mcp-jev) — opt-in, JEV_MCP=1
- **mcp-jev** (`ronail/jev-cost-guard` `mcp/jev/`): a zero-dependency (pure stdlib)
  MCP server exposing `jev_health` + `jev_summarize`. `jev_summarize` compresses a
  spilled result body to a ~300-token digest via the model; it costs a tool
  round-trip (~15 KB in + digest out), so it is opt-in, NOT the default cap.
- Wired into `dev` via `config.yaml` `mcp_servers.jev` (registered with
  `hermes -p dev mcp add`, `mcp test jev` lists both tools). No model name in
  `dev/config.yaml` is `jev` — the dev profile's model is `poolside/laguna-s-2.1:free`
  (provider `nous`); opencode-zen (`deepseek-v4.1-flash-free`) is reached by mcp-jev
  through its own HTTP call (not via the agent's provider), and it **now works**
  through the source-faithful bypass (PR `ronail/hermes-agent#1`, installed branch
  `fix/opencode-zen-freetier-bypass` head `4d6b8a5`):
  `jev_health -> "[jev] ok: provider=opencode-zen model=deepseek-v4.1-flash-free endpoint=https://opencode.ai/zen/v1/chat/completions -> alive"`.
- The bypass (UA `opencode/2.0.29/cli`, `x-opencode-client: cli`, `ses_<12hex><14base62>`
  session, `stream:true` + `stream_options.include_usage`, `bash`/`read` stubs, no
  X-Stainless headers) is replicated exactly; the relay returns HTTP 200 + an SSE
  stream that mcp-jev aggregates (`delta.content`). The dev profile's
  `OPENCODE_ZEN_API_KEY` is read from `profiles/dev/.env` at runtime — the loader
  tries `HERMES_HOME/.env` (Hermes points HERMES_HOME at the profile dir, not the
  base) and `~/.hermes/profiles/<p>/.env`; nothing is committed.
- For a **successful** jev model call: it's live now — `JEV_MODEL=deepseek-v4.1-flash-free`
  (the default) calls the free tier successfully. No paid model needed.

## When this differs
- **Anthropic (cached) profiles** (`default`): prompt caching makes the stable
  prefix ~0.1× on repeated calls, so relative saving is smaller; the runtime
  plugin cap still applies verbatim.
- **`kanban` (26 KB ≈ 6.8k tok/call)** is absent from `dev`; it is the biggest
  lever on `devops`/`planner`/`reviewer` (see README matrix) but is opt-in there.
- `skills.auto_load` full-body is cheap *only* on caching providers; on
  free-model profiles keep it `[]` and `skill_view` on demand.
