---
name: jev-delegate-guard
description: "Delegate only when cheaper than in-context work, and to the lightest profile that covers the task."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "delegation,context,economy,profile,cost"
---

# jev-delegate-guard

Profiles: all (decision rule), but it matters most for the orchestrator
profiles (`planner`, `product`, `default`) that already spawn delegations.

A delegated subagent gets **its own** fixed prompt — so a bad profile pick costs
~5–6k tokens/call extra vs the leanest fit. Pick on bytes, not on habit.

## Decision gate (run before `delegate_task`)

1. **Estimate the inline cost** of doing it here: `tool_calls × 2 × fixed_prompt`
   (input + the result the model must re-read) + expected result bytes.
2. **Pick the child profile** = the profile with the **smallest** `prompt-size`
   whose toolsets `skill` the task. Don't reach for a profile that carries a big
   index or tools you won't use — `dev` (~18.9k) over `tester`-trimmed
   (~13.5k) is a 5.4k-token tax per call for no gain.
3. **Child must carry only its jev skills** — set its `skills.auto_load` to the
   same profile→matrix in `SKILLS.md`; inheriting the parent's full index (~121
   entries in `default`) is ~3k tokens of dead weight per child call.
4. **Hard break-even:** if the task is <4 tool calls *or* the deliverable is
   <3 KB, **do not delegate** — a child round-trip always costs ≥ the parent's
   fixed prompt. Inline it under `jev-context-economy`.

## Why this is cheap to carry
Index entry ≈ 25 tokens/call. A single delegation into a too-heavy profile wastes
5k+/call — the skill pays for itself once per ~200 delegated calls, i.e. almost
always.

## Related
- `jev-context-firewall` — the "when to delegate off the main transcript" rule
  this complements at the *result* level.
- `jev-skill-gate` — keeps the child index lean (same `auto_load` matrix).
