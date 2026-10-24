---
name: jev-skill-gate
description: "Trim the skills index to only what the task needs; keep the fixed prompt budget under 2.5 KB."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "context,skills,budget,economy"
---

# jev-skill-gate

This skill is the policy the rest of the bundle relies on. A skill can't prune
the `<available_skills>` index mid-turn (it is rendered once at session start),
so **every profile keeps its `skills.auto_load` to the bare minimum** and every
task that does not match that set opens a fresh session with exactly the skills
it needs (`hermes -p <p> chat -s A -s B`).

## Always — before replying

1. Read the skills index the session actually shipped:
   `hermes prompt-size --json | jq '.skills_breakdown[] | {name,bytes}'`.
2. Sum the bytes. If `.skills_index.bytes` is not under **2,500 B (~0.6k tokens/call)**,
   remove the off-remit skill: `skills.disabled: [name]` in `config.yaml`, or
   drop it from `skills.auto_load`, then re-measure.
3. If the current task doesn't map to a skill already in `auto_load`, declare the
   1–3 skills you need and run them in a throwaway scoped session — never let an
   unrelated skill ride in "just in case".

## Why this is the cost floor

`jev-context-economy`, `jev-context-firewall`, `jev-prompt-budget-audit`,
`jev-test-output-triage` and `jev-source-digest` are the only skills any of
the 8 profiles need pre-loaded. Anything else is pure overhead until it earns
back its ~25 tokens/call × calls-in-session.

## Related
- `jev-context-economy` — runtime token hygiene that complements this gate
- `jev-prompt-budget-audit` — audits toolsets, not skills
