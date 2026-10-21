---
name: jev-context-firewall
description: "Push bulk extraction/analysis off the main context; only digests return."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  hermes:
    tags: [context, delegation, isolation, economy]
---

# jev-context-firewall

The fixed prompt is sunk cost; the variable cost is **what returns to the
transcript**. This skill confines bulk work to throwaway contexts.

## When to delegate (and where)

| work | route | why |
|---|---|---|
| multi-file code search + analysis across >5 files | `execute_code` (grep → filter → print counts) | raw output is 10–1000× the summary |
| full repo log / large CI artifacts | `execute_code` with `tail -n`; write full to `$BH_AGENT_WORKSPACE/cache/` | never paste logs inline |
| web pages / PDFs / transcripts | `jev-source-digest` (itself a subagent of this rule) | spill, don't paste |
| anything that could spawn >10 tool calls | `delegate_task` (isolated session, no shared context) | child's context cost ≤ main's context cost |

## Break-even litmus
A `delegate_task` round-trip costs the child's prompt (~parent fixed prompt) + the
summaries you read back. That's **not** worth it if the task is <3 tool calls or
the result is <2 KB. If you can't state the deliverable in one line ("3 failing
tests across 2 files" not "the whole repo"), don't delegate.

## The spill pattern (used everywhere this rule applies)
```python
# instead of: return read_file(...) verbatim
body = read_file(path, char_limit=...)
digest = summarize_in_code(body)        # e.g. parse, filter, count
# full body written to /.../cache/<id>.full if anyone needs it
return f"{len(body)} chars\n{digest}"
```

This skill enforces the spill-at-the-boundary decision: bulk work is delegated or
reduced to a digest before it touches the main transcript, rather than relying on a
post-hoc cap.
