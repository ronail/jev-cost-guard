---
name: jev-prompt-budget-audit
description: "Measure fixed prompt cost, cut one toolset/skill, re-measure under budget."
version: 1.1.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "context,audit,toolsets,budget"
---

# jev-prompt-budget-audit

The fixed prompt — system prompt + tool schemas + skills index — is re-sent on
**every** API call. This skill is the loop that keeps it from creeping back after
a good trim. It runs in any harness: every step is a capability, and the concrete
names per harness (including which one exposes a native measurement) live in
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md).

## Measure

**Step 1 — skills index (portable, always available).**

```sh
python3 "${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-.}}/scripts/prompt_budget.py" --json > budget.json
python3 "${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-.}}/scripts/prompt_budget.py" --top 10   # human view
```

**Step 2 — the rest of the fixed prompt.** Only some harnesses expose it. When
yours does, prefer its number and keep the estimator for the skills part:

```sh
# Hermes ONLY. Never from inside another harness, and never from a sandbox that
# cannot take the runtime's install lock — that is exactly the command that
# turns into a false "audit is blocked" report.
hermes -p <profile> prompt-size --json > ps.json
python3 - <<'PY'
import json
d = json.load(open("ps.json"))
print("fixed/call (B):", d["system_prompt"]["bytes"] + d["tools"]["json_bytes"])
print("tools:        ", [(t["toolset"], t["json_bytes"]) for t in
                         sorted(d["toolsets_breakdown"], key=lambda x: -x["json_bytes"])])
print("skills index: ", d["skills_index"]["bytes"], "B over", len(d["skills_breakdown"]), "skills")
PY
```

No native measurement in your harness? Quote the portable skills estimate **plus**
the tool-schema bytes you can count from your own tool list, and label the total an
estimate. Do not report "blocked": an estimate you produced beats a measurement
you refused to approximate.

## Skills index: measure it, never re-render it

Both the estimator and any native figure report a **per-skill** cost — use those.
Re-rendering the index from `SKILL.md` frontmatter by hand drifts, because real
renderers also emit a description line per category and may collapse whole
categories to a names-only line in coding contexts.

- **Quote per-skill index cost from a measurement, never from how long a
  `SKILL.md` description looks.** Frontmatter is fine for a rough ordering, not
  for a number you report.
- **Descriptions render in FULL — the common index has no truncation.** A
  148-skill roster measured ~13.4k chars (~3.3k tokens), about a third of the
  whole system prompt.
- **The index is a fixed prefix, so it is billed on every call** — but at
  cache-read rates once the prefix is warm. Quote the saving in tokens and say
  which rate you assumed; a raw `tokens × calls` figure overstates the money by
  roughly the cache discount.

## Which skills earn their slot (usage attribution)

Never prune from intuition; count real loads first. Each harness records tool
calls in its own session store — read **your** harness's store only:

| harness | store | shape |
|---|---|---|
| Hermes | `~/.hermes/profiles/<profile>/state.db` | SQLite, `messages.tool_calls` |
| Claude Code | `~/.claude/projects/**/*.jsonl` | one JSON object per message |
| Codex | `~/.codex/sessions/**/*.jsonl` | one JSON object per message |
| other | whatever the harness logs | grep for the skill-load call |

```python
# Hermes shape; the counting logic is the same in any store.
import sqlite3, re, json, collections
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
loads = collections.Counter()
for (tc,) in c.execute("select tool_calls from messages where tool_calls like '%skill_view%'"):
    for m in re.finditer(r'name[\'\"]?\s*[:=]\s*[\'\"]?([a-z0-9:_/-]+)', tc or ""):
        loads[m.group(1)] += 1
```

Pitfalls that cost a cycle here:

- **A too-loose regex returns zero silently** and looks like "nothing is ever
  loaded". Sanity-check the total against a raw
  `select count(*) ... where tool_calls like '%skill_view%'` (or a raw line count
  for a JSONL store).
- **Loaded names arrive both bare and category-prefixed** (`github:…`). Normalise
  before ranking or the counts split.
- **The usage store is not the prompt store.** Hermes' `sessions.db` is empty and
  `system_prompts` holds the prompt WITHOUT the skills block (it is assembled per
  turn); a Codex/Claude session JSONL is the transcript. Count loads from tool
  calls, measure size from the index.
- Deleting nothing is still the right first move: move never-loaded skill dirs to
  an attic path so the trim is reversible in one `mv`.

## Quantify a skill's saving before crediting it

Usage counts say a skill *fires*; they say nothing about what it saves. Pair the
count with the output volume that skill acts on, measured from the harness's own
request dumps or transcripts. `scripts/measure_tool_output.py` walks the newest
Hermes dumps (`sessions/request_dump_*.json`) and prints, per tool: calls / total
chars / median / p90 / max, plus the system prompt size — the same aggregation over
another harness's JSONL gives the same answer.

Tool results dominate a real profile (2.3M chars over 956 calls in 35 sessions) and
the **tail** is where the money is: median `read_file` 2.6k chars against a 102k
max, so a handful of calls carry most of the volume.

Cost model for one skill, in tokens (chars/4):

    index cost per turn = its line in the index measurement
    load cost           = SKILL.md bytes / 4
    saving              = (volume the skill acts on) x (reduction you can defend)

Report per skill: what it decides, the mechanism, **one worked example using real
numbers from this roster**, and a keep/prune verdict. Name the reduction factor as
an assumption — the denominator is measured, the ratio is modelled.

Rules that keep the report honest:

- **Levers overlap; never add them up.** A context-firewall and a
  context-economy skill both shrink the same tool output; quote the larger single
  lever and say the other is an alternative, or the total is double-counted.
- **Count the index as a cost, not a saving.** A skill that never fires is pure
  per-turn spend. Measure trigger volume per tool first; a handful of
  zero-volume skills can outweigh every small win combined.
- **A wrong load costs the full body** (~2.3k tokens median for a loaded skill),
  so wrong-load *rate* matters more than load count.
- **Convert to currency before quoting it.** At common input rates a 267k-token
  saving over 35 sessions is about one cent: the payoff is context headroom and
  fewer compaction cycles, not cost. Say so instead of implying the savings fund
  anything.

## Decision tree

- A task-board/toolset the profile never uses is the top entry → drop it from the
  harness's enabled toolsets. **(verified once: reviewer 40→26 tools, 92.5 KB→66.1
  KB per call).**
- A connector/MCP toolset is present but no such server is configured → disable.
- Two toolsets cover the same job (e.g. two browser stacks) → keep one.
- Skills index >2.5 KB → fall to `jev-skill-gate`.

## Commit the change

Cut one lever at a time, then re-measure with the same command you used before, so
the delta is comparable:

```sh
python3 scripts/prompt_budget.py --json | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['total_bytes'], 'B  ~', d['est_tokens'], 'tokens')"
```

Record the new baseline with the harness name and date so the next audit measures
the same thing. Re-audit monthly. Never hand-edit a *different* harness's toolset
config from where you are running.

## Why not model routing

This is pure configuration + measurement — no provider/model changes.
