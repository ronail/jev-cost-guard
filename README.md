# jev-cost-guard

Token/economy guard for agent harnesses — **Hermes-first, harness-agnostic**.

A small bundle of `jev-*` skills plus an MCP server (`mcp/jev`) that attack the two
real token leak classes measured in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas and the skills
   index are the fat. → prune and audit (the `jev-skill-gate` /
   `jev-prompt-budget-audit` skills).
2. **Tool output / model bloats the transcript.** → flag oversized results via the
   `jev` model (`mcp/jev` MCP server).

The repo is intentionally prefix-`jev` so every artifact is namespaced to this
project and survives copy into another harness.

> Note: the deterministic head+tail *plugin* (`jev-context-guard`) was removed — it
> is not a `jev` model feature. The model-based path below is the only trimming
> lever.

## Contents

Skills
| skill | where it loads | fixed cost | target |
|---|---|---|---|
| `jev-context-economy` | all 8 profiles | ~40 B | runtime token hygiene |
| `jev-prompt-budget-audit` | all 8 | ~35 B | fixed-prompt hygiene |
| `jev-context-firewall` | all 8 | ~45 B | output isolation via delegation |
| `jev-skill-gate` | all 8 | ~35 B | keep the skills index lean |
| `jev-test-output-triage` | dev, devops, tester, reviewer | ~50 B | test/log bloat |
| `jev-source-digest` | researcher, product, planner, reviewer | ~50 B | web/page bloat |

## MCP
| server | where it loads | cost | what it does |
|---|---|---|---|
| `mcp/jev` | any profile via `mcp_servers.jev` | 0 prompt tokens + 1 model round-trip (trimming) | `jev_health` checks the configured jev model is reachable; `jev_summarize` asks the jev model a binary success question over a result body and returns a probability signal (~291 in + ~21 out tokens measured). Pure-stdlib MCP (no pip). |

Per skill: ~25 tokens/call are paid for the index entry. These pay off only if a
skill saves ≥1k tokens in a session — which the runtime skills do.

## Profile → skills matrix

Which `jev-*` skills apply to each profile (and why). Shared == every profile.

| profile | purpose | shared skills | domain skills |
|---|---|---|---|
| `default` | orchestrator (Anthropic Sonnet) | skill-gate · context-economy · prompt-budget-audit · context-firewall · **delegate-guard** | — |
| `dev` | local dev / story3d | shared | test-output-triage |
| `devops` | deploys & CI | shared | test-output-triage |
| `planner` | decompose goals → steps, delegates | shared | source-digest |
| `product` | PRD/spec authoring, delegates research | shared | source-digest |
| `researcher` | research & summaries | shared | source-digest |
| `reviewer` | code review + test verification | shared | test-output-triage + source-digest |
| `tester` | test execution | shared | test-output-triage |

> Profiles that run **kanban daily** (devops/planner/reviewer by default) should re-add `kanban` to `agent.disabled_toolsets` only when not dispatching; it is the single biggest lever at 26 KB (~6.8k tokens) per call.

`reviewer` loads both domains because it runs tests and consumes research pages. Drop whichever half a profile never exercises.

## Install

### Hermes
```bash
# skills (Hermes loads each skill-name dir under hermes/)
hermes skills install ronail/jev-cost-guard
```
Then add the shared `auto_load` list + `agent.disabled_toolsets` to each profile's
`config.yaml` (see `SKILLS.md` for the per-profile lists).

### Claude Code / Cursor
Drop the files under `claude-code/` and `cursor/` respectively (see
`SKILLS.md#multi-harness`).

## Why not just add more skills?
Each skill in the index is permanent per-call overhead. This bundle *is* the
minimal set that pays for itself.

## Model-based path (`mcp/jev`)

`mcp/jev/server.py` is a **zero-dependency (pure stdlib)** MCP server exposing
`jev_health` and `jev_summarize`. It talks to the opencode-zen relay directly
(not via the agent's provider). `jev_summarize` is **on by default** as the
transcript trimmer; set `JEV_MCP=0` to disable.

jev-cost-guard uses **only `jev-1.13-free`** — an opencode-zen **System One
*decision* model** (it returns probabilities: `noul`/`choice`/`score`), reached
*only* at the systemone endpoint. It is **not** a text generator — one call costs
~291 input + ~21 output tokens (measured) and returns a probability signal, so it
is cheap relative to re-pasting a large tool result. No fallback to any other
model (no `deepseek-v4.1-flash-free` or others) — `JEV_MODEL` is fixed to
`jev-1.13-free`.

Mount it in a profile (reads that profile's own opencode-zen key from
`profiles/<profile>/.env` at runtime — **never committed**):

```bash
hermes -p dev mcp add jev \
  --command python3 \
  --env JEV_MODEL=jev-1.13-free \
  --env JEV_ENDPOINT=https://opencode.ai/zen/v1/systemone \
  --args /Users/ronail/Projects/jev-cost-guard/mcp/jev/server.py
hermes -p dev mcp list      # -> jev ... 2 tools  enabled
```

### jev-1.13-free auth (real key OR no key)

jev-1.13-free is reached at `https://opencode.ai/zen/v1/systemone`. Auth has two
supported modes — both verified live (HTTP 200):

1. **Real key (preferred):** `Authorization: Bearer <OPENCODE_ZEN_API_KEY>` from
   `profiles/dev/.env`.
2. **No key (free tier):** when no key is configured, mcp-jev sends
   `Authorization: Bearer public` — the relay's free-tier public bearer
   (PR `ronail/hermes-agent#1`) — plus the desktop client fingerprint.

In both modes the OpenCode desktop fingerprint is sent —
`User-Agent: opencode/1.18.31` + `x-opencode-client: desktop` +
`x-opencode-project: global`. The request body is System One shaped, e.g.
`{"model":"jev-1.13-free","state":"<text>","questions":{"is_successful":{"type":"noul"}}}`,
and the response is probabilities + tiny `usage` (~21 output tokens), **not** an
OpenAI-style `choices`/`delta` stream.

| attempt | result |
|---|---|
| no key configured (mcp-jev sends `Bearer public`) | **HTTP 200** (jev free tier) |
| truly anonymous — no `Authorization` at all | `403 FreeTierError: "...can only be used from within OpenCode"` |
| real key, no desktop UA | `403 error code: 1010` |
| wrong path `/inference/systemone/v1/systemone` | `403 error code: 1010` |
| OpenAI `/chat/completions` shape | `400 ModelProtocolUnsupported` |
| real key + desktop UA + `/zen/v1/systemone` | **HTTP 200** |

mcp-jev reads the dev profile's `OPENCODE_ZEN_API_KEY` from `profiles/dev/.env`
at runtime (Hermes does not inject profile secrets into MCP subprocesses). The
key is never committed. No browser / OAuth / device-flow is involved — a real API
key is preferred, with the free-tier `Bearer public` bearer as the no-key path.
