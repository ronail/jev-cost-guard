# jev-cost-guard

Token/economy guard for agent harnesses — **Hermes-first, harness-agnostic**.

A small bundle of `jev-*` skills + one hook plugin that attack the two real token
leak classes measured in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas and the skills
   index are the fat. → prune and audit.
2. **Tool output bloats the transcript.** → cap and spill to disk at the hook level
   (outside the prompt), and force the agent to digest, not dump.

The repo is intentionally prefix-`jev` so every artifact is namespaced to this
project and survives copy into another harness.

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

Plugin
| plugin | harness | cost | what it does |
|---|---|---|---|
| `jev-context-guard` | Hermes | 0 prompt tokens | `transform_tool_result`/`transform_terminal_output` cap >12 KB results to head+tail, spill full text to `$BH_AGENT_WORKSPACE/cache/...full.json` |

Per skill: ~25 tokens/call are paid for the index entry. These pay off only if a
skill saves ≥1k tokens in a session — which the runtime skills do, and the
**plugin** pays off because it adds zero prompt tokens.

## Profile → skills matrix

Which `jev-*` skills apply to each profile (and why). Shared == every profile.

| profile | purpose | shared skills | domain skills | plugin |
|---|---|---|---|---|
| `default` | orchestrator (Anthropic Sonnet) | skill-gate · context-economy · prompt-budget-audit · context-firewall · **delegate-guard** | — | jev-context-guard |
| `dev` | local dev / story3d / DeepSeek | shared | test-output-triage | jev-context-guard |
| `devops` | deploys & CI | shared | test-output-triage | jev-context-guard |
| `planner` | decompose goals → steps, delegates | shared | source-digest | jev-context-guard |
| `product` | PRD/spec authoring, delegates research | shared | source-digest | jev-context-guard |
| `researcher` | research & summaries | shared | source-digest | jev-context-guard |
| `reviewer` | code review + test verification | shared | test-output-triage + source-digest | jev-context-guard |
| `tester` | test execution | shared | test-output-triage | jev-context-guard |

> Profiles that run **kanban daily** (devops/planner/reviewer by default) should re-add `kanban` to `agent.disabled_toolsets` only when not dispatching; it is the single biggest lever at 26 KB (~6.8k tokens) per call.

`reviewer` loads both domains because it runs tests and consumes research pages. Drop whichever half a profile never exercises.

## Install

### Hermes
```bash
# skills (Hermes loads each skill-name dir under hermes/)
hermes skills install ronail/jev-cost-guard

# plugin
hermes -p <profile> plugins install ./plugins/jev-context-guard
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
to a ~300-token digest via the configured model. It costs a tool round-trip, so it
is **opt-in** (`JEV_MCP=1`) — the plugin's deterministic cap stays the default
cheapest path.

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
`deepseek-v4.1-flash-free` now **works** through the source-faithful bypass (PR
`ronail/hermes-agent#1`, installed branch `fix/opencode-zen-freetier-bypass`
head `4d6b8a5`). Verified live in `dev`: `jev_health -> [...model=deepseek-v4.1-flash-free endpoint=https://opencode.ai/zen/v1/chat/completions -> alive]` — the relay returns HTTP 200 + an SSE stream and the model responds. mcp-jev reads the dev profile's `OPENCODE_ZEN_API_KEY` from `profiles/dev/.env` at runtime (never committed). Free-tier is live; no paid model needed.

