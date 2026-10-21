# jev-cost-guard

Token-economy guard for agent harnesses — **Hermes-first, harness-agnostic**. It
attacks the two real token leak classes visible in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas + the skills index
   are the fat. → pruned/audited by `jev-skill-gate` / `jev-prompt-budget-audit`.
2. **Tool output bloats the transcript.** → flagged by the `jev-1.13-free` model via
   the `mcp/jev` MCP server.

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
`hermes skills tap add` was verified to register the source and list the `jev-*`
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
`https://opencode.ai/zen/v1/systemone`) are **optional** — the server defaults them,
so you only pass them to override. The bearer key is **never** passed on the
command line: the MCP reads `OPENCODE_ZEN_API_KEY` from `profiles/<profile>/.env` at
runtime (Hermes does not inject profile secrets into MCP subprocesses). No
browser/OAuth/device-flow. Set `JEV_DEBUG=1` for masked-key logs (prefix+suffix only).

| env | default | note |
|---|---|---|
| `JEV_MODEL` | `jev-1.13-free` | optional — the only model in scope (no fallback) |
| `JEV_ENDPOINT` | `https://opencode.ai/zen/v1/systemone` | optional — System One endpoint |
| `JEV_DEBUG` | `0` | optional — `1` = print masked key prefix/suffix |
| `OPENCODE_ZEN_API_KEY` | (from `profiles/<profile>/.env`) | required for the real-key path |

### `jev_health` / `jev_summarize`
`jev_health` probes the model with a `noul` reachability question (alive iff it
answers). `jev_summarize` does **not** generate prose — it asks jev a binary `noul`
question over a result `state` and returns a **success probability**, which is what
you `budget_tokens`-gate on. jev is a decision model, not a summarizer.

### Auth — key OR no key (verified live)
jev-1.13-free is reached at `https://opencode.ai/zen/v1/systemone` only. Auth has two
modes, both returning HTTP 200, with the OpenCode desktop fingerprint
(`User-Agent: opencode/1.18.31` + `x-opencode-client: desktop` +
`x-opencode-project: global`):

1. **Real key (preferred):** `Authorization` header carries `OPENCODE_ZEN_API_KEY`
   from `profiles/dev/.env`.
2. **No key (free tier):** when no key is configured, the MCP sends the
   `Authorization` header = `Bearer public` — the relay's free-tier public bearer
   (PR `ronail/hermes-agent#1`).

Proof the auth/protocol is correct (live, no key, `Bearer public`, desktop UA):

| attempt | result |
|---|---|
| no key → `Authorization: Bearer public` + desktop UA | **HTTP 200** — `{"answers":{"is_reachable":{"type":"noul","noul":0.38}}, "usage":{"input_tokens":290,"output_tokens":23}}` |
| real key + desktop UA | **HTTP 200** — noul liveness confirmed (same call shape) |
| truly anonymous (no `Authorization` at all) | `403 FreeTierError: "...can only be used from within OpenCode"` |
| real key, no desktop UA | `403 error code: 1010` |
| wrong path `/inference/systemone/v1/systemone` | `403 error code: 1010` |
| OpenAI `/chat/completions` shape | `400 ModelProtocolUnsupported` |

End-to-end no-key probe (`templates/mcp-stdlib-skeleton.py`, full stdio loop):
`{"noul": 0.34}` — calibrated reachability via the public bearer, no key.

---

## Token-savings methodology

`jev-1.13-free` is an opencode-zen **System One *decision* model**: it does not
generate text, it scores a `questions` set over a `state` and returns `noul` /
`choice` / `score` probabilities plus a `usage` object.

| path | cost |
|---|---|
| Re-paste the result body + regenerate, to judge "did it succeed?" | the **whole result body** re-sent as history |
| One jev `noul` decision instead | **~312 tokens** (measured usage, see below) |

The crossover is one call: a ~312-token decision pays for itself against any result
≥ ~313 tokens.

---

## Test results (token-saving evidence)

Measured live against `https://opencode.ai/zen/v1/systemone` (no key,
`Bearer public`, desktop UA), each System One decision returns a probability **plus
its own `usage`** — that is the full token cost of one judgment:

| jev call | input | output | total |
|---|---|---|---|
| `noul` reachability (`is_reachable`) | 290 | 23 | ~313 |
| `noul` liveness (`is_alive`) | 291 | 21 | ~312 |

So a single `jev_summarize` replaces the "re-paste the result + ask again" path with a
fixed ~312-token decision. Savings per judgment = `(re-pasted result size) − (~312)`,
realized once, for any result worth a judgment (≥ ~400 tokens, the common case for
tool output). No other model is involved.

---

## Files

| path | what |
|---|---|
| `mcp/jev/server.py` | the MCP server (stdlib, jev-1.13-free only) |
| `mcp/jev/mcp.json` | MCP manifest (defaults `JEV_MODEL`/`JEV_ENDPOINT`) |
| `mcp/jev/env.example` | env reference (real key from `.env` + `Bearer public` no-key) |
| `hermes/` | the `jev-*` Hermes skills |
| `SKILLS.md` | index + per-profile wiring |
| `docs/token-savings.md` | token-accounting + cap-removal history |
| `LICENSE` | MIT |
