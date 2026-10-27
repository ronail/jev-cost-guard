---
name: jev-delegate-guard
description: "Delegate only when cheaper than in-context work, and to the lightest profile that covers the task."
version: 1.1.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "delegation,context,economy,profile,cost"
---

# jev-delegate-guard

A child session gets **its own** fixed prompt — tools + skills index — so a bad
child choice costs thousands of tokens per call before any work happens. Pick on
measured bytes, not on habit. Names per harness (child-session facility, profile
selection, measurement) are in
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md).

If your harness has no child-session facility, there is nothing to pick: reduce the
work in a code runner under `jev-context-firewall` instead.

## Decision gate (run before delegating)

1. **Estimate the inline cost** of doing it here:
   `tool_calls × 2 × fixed_prompt` (input + the result the model must re-read) +
   expected result bytes.
2. **Pick the lightest child that can do the job** — the smallest fixed prompt
   whose toolsets and skills cover the task. A child carrying a task board it will
   never use, or a 100+ entry skills index, is dead weight on every call it makes.
   Measure it with the portable estimator (`scripts/prompt_budget.py --root
   <child-skills-root>`) rather than guessing; when the harness lets you scope a
   child's skills, scope it.
3. **Child carries only its own jev skills** — not the parent's full roster.
   Inheriting a 121-entry index is ~3k tokens of dead weight per child call.
4. **Hard break-even:** if the task is <4 tool calls *or* the deliverable is
   <3 KB, **do not delegate** — a child round-trip always costs at least the
   parent's fixed prompt. Inline it under `jev-context-economy`.

## Why this is cheap to carry

One index entry ≈ 25 tokens/call. A single delegation into a too-heavy child wastes
5k+/call, so the skill pays for itself within a couple hundred delegated calls —
i.e. almost always.

## Related

- `jev-context-firewall` — the "when to hand work off the main transcript" rule this
  complements at the *result* level.
- `jev-skill-gate` — keeps the child's roster lean (same index budget).
