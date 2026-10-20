# jev-cost-guard — token-savings report (verified)

Measured with `hermes prompt-size --json` on the `dev` profile (DeepSeek
`deepseek-v4.1-flash-free`, **no** provider prompt-caching → full fixed prompt
is charged every call). 7-day baseline from `hermes -p dev insights --days 7`.

## Setup applied to `dev` (test run)
- Installed 7 `jev-*` skills into `~/.hermes/profiles/dev/skills/jev/` (index
  now lists them).
- `agent.disabled_toolsets: [connections, tts, vision, clarify, browser-use]`
- `skills.auto_load: []` — deliberately empty: on a non-cached/free-model
  profile, auto-loading full skill bodies bloats every call. The jev skills stay
  on-demand (`skill_view` from the index), which is what `jev-skill-gate`
  prescribes.
- `jev-context-guard` plugin enabled in `dev`.

## Fixed-prompt delta (bytes = chars ≈ tokens × 3.9 for English)

| metric | before | after | delta |
|---|---:|---:|---:|
| toolsets visible | 26 | 21 | −5 |
| tool-schema JSON | 49,004 B | 36,849 B | **−12,155 B** |
| system prompt | 24,770 B | 20,581 B | −4,189 B |
| skills index | 7,379 B (82 skills) | 8,002 B (89 skills) | +623 B |
| **fixed prompt / call** | **73,774 B ≈ 18,916 tok** | **57,430 B ≈ 14,726 tok** | **−16,344 B ≈ 4,191 tok/call** |

**Net: −22.2% fixed tokens per API call on `dev`.**

Notes on the small index growth (+623 B): adding 7 skill entries (~89 B each)
is the ~25-token/call "rent" for the skills. Each `jev-context-firewall` or
`jev-test-output-triage` call pays this back within one real session.

## 7-day translation (`dev`)
`hermes -p dev insights --days 7` → 58 sessions / 618 tool calls.
Conservative floor at one fixed-prompt per tool call:

> 618 × 4,191 ≈ **2.58M input tokens saved / 7 days** — before any runtime
> spill savings.

The `jev-context-guard` plugin adds a *variable* saving on top: any tool result
> 12 KB is reduced to a 6 KB + 2 KB head/tail plus a file link. Proven by direct
> invocation: a 20,000-char result → 8,220 chars returned, full body spilled.

## Caveats / when this differs
- **Anthropic (cached) profiles** (`default`): prompt caching makes the stable
  prefix ~0.1× on repeated calls, so the relative saving is smaller; prefer the
  plugin + index-gate there.
- **`kanban` (26 KB ≈ 6.8k tok/call)** is absent from `dev`; it is the biggest
  lever on `devops`/`planner`/`reviewer` (see README matrix) but is opt-in there
  for those profiles' work.
- `skills.auto_load` full-body is cheap *only* on caching providers; on
  free-model profiles keep it `[]` and skill_view on demand.
