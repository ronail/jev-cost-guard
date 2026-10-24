---
name: jev-prompt-budget-audit
description: "Measure fixed prompt cost, cut one toolset/skill, re-measure under budget."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  hermes:
    tags: [context, audit, toolsets, budget]
---

# jev-prompt-budget-audit

The fixed prompt is ~15–25k tokens and is re-sent on **every** API call. This
skill is the loop that keeps it from creeping back after a good trim.

## One-shot procedure

```bash
hermes -p <profile> prompt-size --json > result.json
python3 - <<'EOF'
import json
d=json.load(open("result.json"))
print("fixed/call (B):", d["system_prompt"]["bytes"]+d["tools"]["json_bytes"])
print("tools:         ", [(t["toolset"],t["json_bytes"]) for t in sorted(d["toolsets_breakdown"], key=lambda x: -x["json_bytes"])])
print("skills index:  ", d["skills_index"]["bytes"], "B over", len(d["skills_breakdown"]), "skills")
EOF
```

## Decision tree

- Top entry is `kanban` (26 KB, ~6.8k tokens) and this profile doesn't dispatch
  kanban tasks → `agent.disabled_toolsets += [kanban]`. **(verified: reviewer
  40→26 tools, 92.5 KB→66.1 KB per call).**
- `connections` (4.2 KB) present but no connector MCP configured → disable.
- `tts`, `vision`, `clarify` (>4 KB combined) unused on this surface → disable.
- `browser` (4.4 KB) AND `browser-use` (3.5 KB) both loaded → keep one.
- skills index >2.5 KB → fall to `jev-skill-gate`.

## Commit the change
Edit `the active profile's `config.yaml`` (not via `hermes tools`, which
needs a TTY and writes to the active profile), then:
```bash
hermes -p <profile> prompt-size --json | jq '.tools.json_bytes + .system_prompt.bytes'
```
Record the new baseline. Re-audit monthly.

## Why not model routing
This is pure configuration + measurement — no provider/model changes.
