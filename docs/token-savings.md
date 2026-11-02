# jev-cost-guard — token-savings report (verified)

Measured on the `dev` profile (`poolside/laguna-s-2.1:free` via provider `nous`,
no provider prompt-caching → full cost on every call). Two levers: a fixed-prompt
shrink via config, and a variable runtime trim via the `jev` model (`mcp/jev`).

## Status on `dev` (current)
- 6 `jev-*` skills installed in `https://github.com/ronail/jev-cost-guard/tree/main/hermes` (index only,
  on-demand — `skills.auto_load` left at its default `[]`).
- `mcp/jev` MCP server registered in `dev` (`mcp_servers.jev`, `jev_health` +
  `jev_summarize`, model `jev-1.13-free`).
- `config.yaml` otherwise **pristine** (the earlier `agent.disabled_toolsets`
  experiment was reverted on request). No toolset trimming is active right now.

> Note on "the jev model": `jev` is a namespace prefix for this project's
> artifacts, **and** `jev-1.13-free` is a real opencode-zen model — a System One
> *decision* model reached at `https://opencode.ai/zen/v1/systemone`. It is not a
> text-generation model; one call costs ~291 in + ~21 out tokens (measured) and
> returns a probability signal (`noul` / `choice` / `score`). There is no
> `deepseek-v4.1-flash-free` in this MCP — `jev-1.13-free` is the only model.
> (On `jev-1.13-free` exhaustion with a real key, retries paid `jev-1.13` — same
> jev family, not a different model; `JEV_FALLBACK_TO_PAID=0` disables.)

## Runtime savings (model path)
mcp-jev (`JEV_MCP=1`, default on) trims oversized results instead of the agent
re-pasting raw tool output. A `jev_summarize` call asks jev-1.13-free a binary
`noul` success question over the result body and returns a probability — ~291 in
+ ~21 out tokens measured. That pays off whenever the spilled result is ≥ a few
hundred tokens (typical for `web_extract` / `terminal` / `read_file`). The full
body is still reachable by the caller from the original tool; only the jev
probability signal enters the transcript. A live `jev_health` probe hit jev-1.13-
free at `/systemone` and returned a reachability `noul` (HTTP 200); see the
jev-1.13-free auth note below.

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

## Model-based path (mcp-jev) — `JEV_MCP=1` (default on)

- **mcp-jev** (`ronail/jev-cost-guard` `mcp/jev/`): a zero-dependency (pure stdlib)
  MCP server exposing `jev_health` + `jev_summarize`. `jev_summarize` asks jev-1.13-
  free a binary success question over a spilled result body and returns a `noul`
  probability (~291 in + ~21 out tokens measured); it is on by default — set
  `JEV_MCP=0` to disable.
- jev-1.13-free is an opencode-zen **System One decision model** reached *only* at
  `https://opencode.ai/zen/v1/systemone`. A real `OPENCODE_ZEN_API_KEY`
  (`Authorization: Bearer <key>` from the active profile's `.env`) is preferred; when none
  is set, mcp-jev falls back to the relay free-tier **public bearer**
  (`Authorization: Bearer public`, PR `ronail/hermes-agent#1`) — verified live
  (HTTP 200) — so the no-key case is genuinely supported, not an error. The OpenCode
  desktop fingerprint is sent in both modes
  (`User-Agent: opencode/1.18.31`, `x-opencode-client: desktop`,
  `x-opencode-project: global`). `JEV_MODEL` is fixed to `jev-1.13-free` — on free
  exhaustion (HTTP 429/403) with a real key, retries paid `jev-1.13` once (same
  key/endpoint/UA; `JEV_FALLBACK_TO_PAID` default 1, `=0` fails open; no-key
  `Bearer public` never falls back).
- Truly anonymous (no `Authorization` header at all) -> `403 FreeTierError`; a
  real key w/o the desktop UA -> `403 error code: 1010`; wrong path -> 403 1010.
  Paid retry is bounded (`_retry_paid=False`); a second failure raises so the
  caller fails open. Relay load is one call per trim, at most one paid retry.
- The key is read from the active profile's `.env` at runtime (Hermes does not
  inject profile secrets into MCP subprocesses — see
  [`mcp/jev/env.example`](https://github.com/ronail/jev-cost-guard/blob/main/mcp/jev/env.example)); nothing is committed.

## Per-turn analytics (opt-in, `jev-analytics`)

Every jev tool call appends one measured row (`body_chars`, jev's own `usage`,
the verdict size) and its result ends with a compact `[jev in=… out=…]` suffix, so
a tool result is self-describing in any harness. Asking for a report is the only
opt-in; the turn then ends with ~45 tokens:

```
jev analytics · 1 call (est.)
  screened  7,120 chars ≈ 1,780 tok → main model read 0
  jev       in 2,038 / out 24 tok (jev cost unpriced)
  saved     1,743 tok ≈ $0.00026 @ official DeepSeek deepseek-flash $0.15/$0.6 per M
  assumption: those bodies would otherwise have been read by the main model
```

### How `report` finds the ledger

The server and the report script are separate processes with different
environments: only the server sees `JEV_CALL_LOG` / `PLUGIN_DATA` from the host's
MCP config. Resolving the path independently made the server write one file and
`report` read another, so every report came back empty — with a tool result that
looked perfectly healthy. The server now publishes its resolved path to
`<state>/jev-cost-guard/ledger-path` on every append, and the script follows it.
An explicit `JEV_CALL_LOG` still wins.

Three limits are deliberate, because a cost report that flatters itself is worse
than none: `saved` is `body_tokens − kept_tokens` and the counterfactual is
stated as an assumption; bodies under ~1.5k tokens are a net loss and are printed
as one; and `$` uses **official DeepSeek list prices only** — the same model id
ranges from `$0.02` to `$0.67` per 1M across resellers, so anything else prints
`$ n/a` instead of a guess.

## When this differs
- **Anthropic (cached) profiles** (`default`): prompt caching makes the stable
  prefix ~0.1× on repeated calls, so relative saving is smaller; the fixed-prompt
  audit (skills) still applies verbatim.
- **`kanban` (26 KB ≈ 6.8k tok/call)** is absent from `dev`; it is the biggest
  lever on `devops`/`planner`/`reviewer` (see README matrix) but is opt-in there.
- `skills.auto_load` full-body is cheap *only* on caching providers; on
  free-model profiles keep it `[]` and `skill_view` on demand.
