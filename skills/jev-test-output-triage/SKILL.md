---
name: jev-test-output-triage
description: "Run tests/logs through a summary reporter; only failures reach context."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "tests,ci,logs,economy"
---

# jev-test-output-triage

Profiles: dev, devops, tester, reviewer.

Test/log output is the #1 transcript bloat source — a 941-test run is megabytes
of text that collapses to a single failing-assertion list.

## Procedure

- **Use summary reporters.** vitest: `--reporter=dot`; jest: `--silent --summary`;
  pytest: `-q` (defaults to 1 line/test). Pass `--outputFile` or `-o junit_xml`
  so full output lands in a file, not stdout.
- **Print only the failure set.**
  ```python
  fail = [l for l in out.splitlines() if "✓" not in l and ("FAIL" in l or "✗" in l or "×" in l)]
  print("\n".join(fail[:40]))
  ```
- **Never read full logs.** If a CI log is the debugging target, open it with
  `read_file` range scans for the failure block only (use `search_files` for the
  error token first), or `grep -A60 "<fail>" build.log | tail -n 60`.
- **Cap the tail.** If you must show a passing command's output, pipe through
  `tail -n 25`.
- **Spill and link.** Prefer writing full output to `$BH_AGENT_WORKSPACE/cache/`
  and returning only a digest; the failure set is usually <200 B, so it rarely
  grows large enough to need truncation.

## Failure case
If a single `terminal` call's result exceeds 4 KB, the skill failed that turn.
That 4 KB is ~1k tokens that could have been a 40-char summary.
