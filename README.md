# jev-cost-guard

Token/economy guard for agent harnesses — **Hermes-first, harness-agnostic**.

A small bundle of `jev-*` skills plus an MCP server (`mcp/jev`) that attack the two
real token leak classes measured in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas and the skills
   index are the fat. → prune and audit (the `jev-skill-gate` /
   `jev-prompt-budget-audit` skills).
2. **Tool output / model bloats the transcript.** → compress large results to a
   digest via the `jev` model (`mcp/jev` MCP server, opt-in `JEV_MCP=1`).

The repo is intentionally prefix-`jev` so every artifact is namespaced to this
project and survives copy into another harness.

> Note: the deterministic head+tail *plugin* (`jev-context-guard`) was removed — it
> is not a `jev` model feature. The model-based path below is the only compression
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
| `mcp/jev` | any profile via `mcp_servers.jev` | 0 prompt tokens + 1 model round-trip (opt-in) | `jev_health` probes the configured jev model; `jev_summarize` compresses a large result body to a ~300-token digest. Pure-stdlib MCP (no pip). |

Per skill: ~25 tokens/call are paid for the index entry. These pay off only if a
skill saves ≥1k tokens in a session — which the runtime skills do.

## Profile → skills matrix

Which `jev-*` skills apply to each profile (and why). Shared == every profile.

| profile | purpose | shared skills | domain skills |
|---|---|---|---|
| `default` | orchestrator (Anthropic Sonnet) | skill-gate · context-economy · prompt-budget-audit · context-firewall · **delegate-guard** | — |
| `dev` | local dev / story3d / DeepSeek | shared | test-output-triage |
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
`jev_health` and `jev_summarize`. `jev_summarize` compresses a large result body
to a ~300-token digest via the model; it costs a tool round-trip, so it is
**opt-in** (`JEV_MCP=1`).

Mount it in a profile (reads that profile's own opencode-zen key from
`profiles/<profile>/.env` at runtime — **never committed**):

```bash
hermes -p dev mcp add jev \
  --command python3 \
  --env JEV_PROVIDER=opencode-zen \
  --env JEV_MODEL=deepseek-v4.1-flash-free \
  --args /Users/ronail/Projects/jev-cost-guard/mcp/jev/server.py
hermes -p dev mcp list      # -> jev ... 2 tools  enabled
```

### opencode-zen free-tier status
`deepseek-v4.1-flash-free` is called via the source-faithful bypass (PR
`ronail/hermes-agent#1` v2, commit `c557431`): `Authorization: Bearer public`
(the relay flags real API keys as abusive traffic), UA `opencode/latest/2.0.29/cli`,
`x-opencode-project: global`, session-mirror headers (`x-opencode-session`=`x-opencode-session-id`=`x-session-affinity`=`x-session-id`=`ses_<12hex><14base62>`), `stream:true` + `include_usage`, `shell`/`glob`/`grep`/`read` stubs. Verified live: `jev_health -> [...deepseek-v4.1-flash-free ... -> alive]` (HTTP 200 + SSE aggregated). On IPs flagged for abusive free-tier traffic the relay returns `403 FreeTierError: "…abusive traffic from this IP address… not related to your account… turn off VPN/proxy or subscribe"` — an IP-reputation block (not a bypass gap); disable VPN/proxy, wait for the flag to clear, or use a paid opencode-zen model (the bypass auto-skips for non-`-free` models). mcp-jev reads the dev profile's `OPENCODE_ZEN_API_KEY` from `profiles/dev/.env` at runtime (never committed). Free-tier is live on a clean IP; no paid model needed.

