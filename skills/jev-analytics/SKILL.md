---
name: jev-analytics
description: "Use when analytics output is on: report jev's per-turn token savings, what the verdict changed, and the final token diff."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "jev,analytics,cost,tokens,reporting"
---

# jev-analytics

A reporting overlay on top of the jev MCP, not a policy. When the switch is
**on**, jev's cost and the tokens it avoided become visible; when it is **off**,
this skill must produce zero output and zero extra calls. Concrete tool names per
harness are in [docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md).

## 1. Is it on? Ask the last jev response, don't poll

Every jev tool result ends in `[ja]` when analytics is on, because the MCP server
writes the ledger itself. That marker is the whole switch:

- response ends in `[ja]` → analytics is on → steps 2–6 apply;
- no marker → off → stop here, say nothing about cost, make no extra call.

Never run `status` to find out. The marker costs 1 token per jev call; a status
call costs a round-trip every turn.

## 2. Before acting: give jev the first attempt at anything decidable

jev is a System One *decision* model. It scores; it never writes prose. So "jev
first" means: put the question to jev before spending main-model context on it.

- **Files** — `jev_find` (which paths matter) and `jev_screen` (safe? relevant?)
  before reading bodies. The bodies never enter the transcript; that is where the
  saving comes from.
- **Tool output** — `jev_summarize` for "did this succeed?" instead of re-reading
  a large result.
- **Any other question jev can score** — a `choice` or `noul` framing of the next
  decision. Then interpret the score yourself: jev returns a probability, and the
  probability is not an instruction.

**Break-even, or the report goes negative.** A jev call costs ~300 input tokens
plus ~24 output. Below roughly **1,500 tokens (≈6 KB)** of body, a pre-check
costs more than it saves. Skip it, and if the turn ends up with a net loss, report
that honestly rather than hiding the row.

## 3. The main model stays the judge

The verdict is evidence, not an order. After a jev call, decide your next action
exactly as you normally would, and note — you will need it at step 4 — whether
the verdict actually:

- **changed** the action (you would have read the file / accepted the result, and
  the score moved you), or
- **confirmed** the action you had already chosen, or
- was **ignored** (the score was ambiguous and you judged from context anyway).

"Confirmed" and "ignored" are the common cases. Claiming a change that did not
happen is the one way this skill becomes a cost sink.

## 4. End of the turn: print the report, once

Only when at least one jev call happened this turn. Run the script and paste its
stdout **verbatim** — never recompute the arithmetic yourself, and never estimate
the numbers in prose:

```sh
python3 "${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-.}}/scripts/jev_analytics.py" \
  report --model <main-model-id>
```

It prints (about 45 tokens):

```
jev analytics · 2 calls (est.)
  screened  20,000 chars ≈ 5,000 tok → main model read 0
  jev       in 1,961 / out 24 tok (jev cost unpriced)
  saved     4,976 tok ≈ $0.00075 @ official DeepSeek deepseek-flash $0.15/$0.6 per M
  assumption: those bodies would otherwise have been read by the main model
```

`jev cost unpriced` appears whenever jev ran on TypeSafe or the paid fallback —
only the relay's `*-free` tier prints `free tier: $0`. Never substitute a jev
price of your own.

…then exactly one line of your own, in this shape:

```
effect: jev's 0.88 changed the plan — read config.py first, skipped the other 7.
```

Keep the block last, after your answer, so it never pushes the answer down. If
`report` prints nothing (exit 1), print nothing — that is the off case and the
"no rows this turn" case, and both are silent.

## 5. Retract a row that was not a real save

The one number nobody can measure is the counterfactual. If a body was **already
in your context** (you read it earlier, or the user pasted it), that call saved
nothing — retract it before reporting:

```sh
python3 "${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-.}}/scripts/jev_analytics.py" drop 2
```

Rows are 1-based and printed in call order. Dropping is always better than
inflating.

## 6. Prices: official DeepSeek list price only

The report prices saved tokens with the first-party DeepSeek rate for the
family (`deepseek-flash` `$0.15/$0.60` per 1M, `deepseek-thinking` `$0.66/$1.98`
per 1M). Resellers sell the same model id from `$0.02` to `$0.67` per 1M, so a
reseller guess would be off by up to 40×.

- not a DeepSeek model → the report prints `$ n/a` and tokens only. Leave it;
  do not substitute a price you happen to remember.
- you know the real rate → pass `--price-in` / `--price-out` (USD per 1M) and
  say in the `effect:` line that the price is an override.

## 7. Failure modes

| situation | response |
|---|---|
| script missing / not executable | say `jev analytics: script unavailable` once, stop reporting, keep working |
| `report` exits 1 with no output | print nothing — off, or no call this turn |
| jev unreachable | the tool already returned the error; there is no row, so no report |
| report would say `saved: -N tok` | print it. A loss is information: the body was too small for a jev call |

## Cost of this skill

~1.3 KB of index line, and a ~45-token block on turns where jev actually ran.
If you never enable it, prune it from the roster like any other unused skill.
