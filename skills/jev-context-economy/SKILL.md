---
name: jev-context-economy
description: "Per-call token hygiene: batch calls, grep-then-read, cap output, compress at boundaries."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "context,economy,batching,output"
---

# jev-context-economy

Runtime counterpart to `jev-skill-gate`. The fixed prompt is already trimmed by
the gate; this skill trims what you append *each turn*.

## Rules (apply every turn)

- **Batch independent calls.** One tool block with all `search_files`/`read_file`
  calls that don't depend on each other — never a call per item.
- **Grep first, read second.** `search_files` with `output_mode: "count"` or
  `files_only`; only read ranges with `offset`/`limit` around real hits.
- **Never re-read what you just wrote.** If you wrote/ patched a file this turn,
  do not read it back to confirm — trust the result object.
- **Cap tool output in code.** Pipe logs/large blobs through `execute_code` and
  print only the aggregate (e.g. `len(...)`, failing lines, a top-N). Never paste
  >12 KB of tool output raw into context.
- **One task per session.** When context pressure rises (/usage > 1.2k tokens)
  prefer `/new` with a path reference to the old session over /compress, because
  summary calls also cost tokens.
- **Compress at boundaries**, not just at the 50% threshold: after a completed
  sub-task, hit `/compress` before starting the next.

## Exit signal
If you caught yourself about to paste a full log, re-read a file you just edited,
or fire 3 separate grep calls, the skill paid for itself that turn (~25 tok/call).
