---
name: jev-context-economy
description: "Per-call token hygiene: batch calls, grep-then-read, cap output, compress at boundaries."
version: 1.1.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "context,economy,batching,output"
---

# jev-context-economy

Runtime counterpart to `jev-skill-gate`. The fixed prompt is already trimmed by
the gate; this skill trims what you append *each turn*. Every rule names a
capability — use whatever your harness calls it (see
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md)).

## Rules (apply every turn)

- **Batch independent calls.** One tool block with every lookup that doesn't
  depend on another result — never one call per item. Batched calls run
  concurrently and cost one round-trip of history, not N.
- **Search first, read second.** Use the harness's text search in count/files-only
  mode to find candidates, then read only the ranges around real hits. Reading a
  whole file to answer a one-line question is the most common leak.
- **Never re-read what you just wrote.** If you wrote or patched a file this turn,
  the tool result already confirms it — do not spend a call reading it back.
- **Cap tool output in a script.** Pipe logs and large blobs through a code-runner
  step that prints only the aggregate (`len(...)`, failing lines, a top-N). Never
  put >12 KB of raw tool output into the transcript.
- **One task per session.** When context pressure rises, prefer a **fresh session**
  with a path reference to the old one over compaction: the summary call itself
  costs tokens. Compact at boundaries (after a finished sub-task) rather than
  mid-thought, and only if the harness offers a compaction command at all — many
  do not, in which case the fresh session is the only lever.

## Exit signal

If you caught yourself about to paste a full log, re-read a file you just edited,
or fire three separate searches, the skill paid for itself that turn (~25
tok/call).
