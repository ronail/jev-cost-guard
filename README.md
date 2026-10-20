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
