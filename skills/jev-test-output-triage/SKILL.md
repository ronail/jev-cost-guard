---
name: jev-test-output-triage
description: "Run tests/logs through a summary reporter; only failures reach context."
version: 1.1.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "tests,ci,logs,economy"
---

# jev-test-output-triage

Test and log output is the #1 transcript bloat source — a 941-test run is megabytes
of text that collapses to one failing-assertion list. The steps name capabilities
(shell, code runner, range read, scratch dir); concrete names per harness are in
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md).

## Procedure

- **Use summary reporters.** vitest `--reporter=dot`, jest `--silent --summary`,
  pytest `-q`, go test without `-v`, cargo test without `--nocapture`. Send full
  output to a file (`--outputFile`, `-o junit_xml`, `> run.log`), not to stdout.
- **Print only the failure set** from that file:

  ```python
  fail = [l for l in open("run.log", errors="replace")
          if "✓" not in l and ("FAIL" in l or "✗" in l or "×" in l or "Error" in l)]
  print("\n".join(fail[:40]))
  ```

- **Never read a full log into context.** If a CI log is the debugging target, find
  the error token first (`grep -n "<token>" build.log`), then read a range around it
  (`sed -n '120,180p' build.log`).
- **Cap the tail.** If you must show a passing command's output, keep the last ~25
  lines.
- **Spill and link.** Write full output to the scratch dir and return only the
  digest; the failure set is usually <200 B, so it rarely needs truncation.

## Failure case

If a single shell call's result exceeds 4 KB, the skill failed that turn: that 4 KB
is ~1k tokens that could have been a 40-char summary.
