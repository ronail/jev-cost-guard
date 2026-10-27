---
name: jev-context-firewall
description: "Push bulk extraction/analysis off the main context; only digests return."
version: 1.1.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "context,delegation,isolation,economy"
---

# jev-context-firewall

The fixed prompt is sunk cost; the variable cost is **what returns to the
transcript**. This skill confines bulk work to throwaway contexts.

Capabilities below resolve to your harness's own tools — a sandboxed code runner,
an isolated child session, a scratch directory (see
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md)).
A harness without child sessions simply uses the code runner; never borrow another
runtime's delegation mechanism.

## When to route away from the main context

| work | route | why |
|---|---|---|
| multi-file code search + analysis across >5 files | code runner (search → filter → print counts) | raw output is 10–1000× the summary |
| full repo log / large CI artifacts | code runner with `tail -n`; write the full body to the scratch dir | never paste logs inline |
| web pages / PDFs / transcripts | `jev-source-digest` (itself an instance of this rule) | spill, don't paste |
| anything that could spawn >10 tool calls | an isolated child session | the child's context is discarded when it returns one summary |

## Break-even litmus

A child round-trip costs the child's whole fixed prompt (tools + skills index) plus
the summaries you read back. That is **not** worth it if the task is <3 tool calls
or the result is <2 KB. If you can't state the deliverable in one line ("3 failing
tests across 2 files", not "the whole repo"), don't delegate — and if your harness
has no child-session facility, do the reduction in the code runner instead.

## The spill pattern (used everywhere this rule applies)

```python
# instead of: return the tool result verbatim
body = fetch_or_read(path, limit=...)
digest = reduce_in_code(body)              # parse, filter, count
write(f"{scratch}/jev-source/{name}.full", body)   # full body, if anyone needs it
print(f"{len(body)} chars -> {scratch}/jev-source/{name}.full\n{digest[:600]}")
```

This skill enforces the spill-at-the-boundary decision: bulk work is reduced to a
digest (or handed to a child) before it touches the main transcript, rather than
relying on a post-hoc cap.
