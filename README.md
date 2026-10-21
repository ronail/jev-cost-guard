# jev-cost-guard

Token-economy guard for agent harnesses — **Hermes-first, harness-agnostic**.
It attacks the two real token leak classes visible in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas and the skills
   index are the fat. → pruned/audited by `jev-skill-gate` / `jev-prompt-budget-audit`.
2. **Tool output / model bloat grows the transcript.** → flagged by the jev model
   via the `mcp/jev` MCP server.

> The deterministic head+tail *plugin* (`jev-context-guard`) was removed — it is not
> a jev-model feature. The model-based path below is the only trimming lever.

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
skills-guard scan, and installs the skill. Add the desired skills to each profile's
`skills.auto_load` in `config.yaml` (see `SKILLS.md` for the shared + per-profile
lists).

| skill | purpose |
|---|---|
| `jev-skill-gate` | keeps the skills index ≤ ~2.5 KB (the fat is re-sent every call) |
| `jev-context-economy` | per-turn token hygiene: batch calls, grep-then-read, cap output |
| `jev-prompt-budget-audit` | fixed-prompt audit loop |
| `jev-context-firewall` | delegate bulk tool output off the main transcript |
| `jev-delegate-guard` | pick the leanest child profile; break-even gate |
| `jev-test-output-triage` | triaging test/log bloat |
| `jev-source-digest` | triaging web/page bloat |
| **`jev-mcp-model-server`** | usage guide for this MCP (how/when to drive `jev-1.13-free`) |

### Mount the MCP (`mcp/jev`)
A zero-dependency (pure stdlib, no pip) MCP server exposing `jev_health` +
`jev_summarize`. It talks to the opencode-zen relay directly — not the agent's own
provider — and uses **only `jev-1.13-free`**.

```bash
hermes -p dev mcp add jev \
  --command python3 \
  --env JEV_MODEL=jev-1.13-free \
  --env JEV_ENDPOINT=https://opencode.ai/zen/v1/systemone \
  --args <repo>/mcp/jev/server.py   # absolute path to your checkout of ronail/jev-cost-guard
hermes -p dev mcp list        # -> jev ... 2 tools  enabled
```

It reads that profile's `OPENCODE_ZEN_API_KEY` from `profiles/<profile>/.env` at
runtime — **never committed** — and needs no browser/OAuth/device-flow. Set
`JEV_DEBUG=1` for masked-key logs (prefix+suffix only).

| env | default | meaning |
|---|---|---|
| `JEV_MODEL` | `jev-1.13-free` | the only model in scope (no fallback) |
| `JEV_ENDPOINT` | `https://opencode.ai/zen/v1/systemone` | System One endpoint |
| `JEV_DEBUG` | `0` | `1` = print yes/no + masked key prefix/suffix |

### `jev_health` / `jev_summarize`
`jev_health` probes the model with a `noul` reachability question (alive iff it
answers). `jev_summarize` does **not** generate prose — it asks jev a binary
`noul` question over a result `state` and returns a **success probability**, which is
what you `budget_tokens`-gate on. jev is a decision model, not a summarizer.

### Auth (key OR no key)
jev-1.13-free is reached at `https://opencode.ai/zen/v1/systemone` only. Auth has two
modes — both verified live (HTTP 200):

1. **Real key (preferred):** the `Authorization` header carries the key from the
   profile `.env`.
2. **No key (free tier):** when no key is configured, the MCP sends the
   `Authorization` header = `Bearer public` — the relay's free-tier public bearer
   (PR `ronail/hermes-agent#1`).

In both modes the OpenCode desktop fingerprint is sent:
`User-Agent: opencode/1.18.31` + `x-opencode-client: desktop` +
`x-opencode-project: global`. The request body is System One shaped, e.g.
`{"model":"jev-1.13-free","state":"<text>","questions":{"is_successful":{"type":"noul","instructions":"..."}}}`,
and the response is probabilities + tiny `usage` — **not** an OpenAI
`choices`/`delta` stream.

---

## Token-savings methodology

`jev-1.13-free` is an opencode-zen **System One *decision* model**. It does not
generate text; it scores a question over a `state` and returns a `noul` (or
`choice`/`score`) probability plus a `usage` object.

| path | tokens spent | tokens saved |
|---|---|---|
| Re-paste a 2 KB result + regenerate to judge "did it succeed?" | ~2000 in | 0 |
| One jev `noul` decision instead | ~291 in + ~21 out | ~1.7 KB |

The crossover is one call: a ~312-token jev decision pays for itself versus
re-pasting any result ≥ ~400 tokens. That fixed ~312-token decision (probabilities, not
prose) is the **only** trimming lever — there is no other model in scope and no
deterministic cap plugin.

---

## Test results

Verified live against `https://opencode.ai/zen/v1/systemone` with the OpenCode
desktop UA (`User-Agent: opencode/1.18.31` + `x-opencode-client: desktop` +
`x-opencode-project: global`):

| attempt | result |
|---|---|
| no key → `Authorization` header = `Bearer public` + desktop UA | **HTTP 200** — `{"answers":{"is_reachable":{"type":"noul","noul":0.38}}, "usage":{"input_tokens":290,"output_tokens":23}}` |
| real key + desktop UA | **HTTP 200** — noul liveness confirmed (same call shape) |
| truly anonymous (no `Authorization`) | `403 FreeTierError: "...can only be used from within OpenCode"` |
| real key, no desktop UA | `403 error code: 1010` |
| wrong path `/inference/systemone/v1/systemone` | `403 error code: 1010` |
| OpenAI `/chat/completions` shape | `400 ModelProtocolUnsupported` |

Skeleton probe (`templates/mcp-stdlib-skeleton.py`), no key — full stdio MCP loop:

```jsonc
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"type\":\"noul\",\"noul\":0.34}"}]}}
```

Calibrated no-key reachability confirmed (no key set → `Bearer public` → 200). The
key path requires `OPENCODE_ZEN_API_KEY` in `profiles/dev/.env`.

---

## Files

| path | what |
|---|---|
| `mcp/jev/server.py` | the MCP server (stdlib, jev-1.13-free only) |
| `mcp/jev/mcp.json` | MCP manifest (`JEV_MODEL=jev-1.13-free`) |
| `mcp/jev/env.example` | env reference (real key + `Bearer public` no-key) |
| `hermes/` | the `jev-*` Hermes skills |
| `SKILLS.md` | index + per-profile wiring |
| `docs/token-savings.md` | token-accounting + cap history |
| `LICENSE` | MIT |
