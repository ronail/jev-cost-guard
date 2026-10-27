---
name: jev-source-digest
description: "Web/page sources: extract bounded, file, digest in code, cite by line range."
version: 1.1.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "research,web,economy,digest"
---

# jev-source-digest

Whole pages pasted into context are the classic research blowup. Turn every source
into a digest on disk. The steps below are capabilities — bounded fetch tool,
code runner, scratch directory — resolved per harness in
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md).
No harness has a built-in "cap it for me": this skill sets the bound.

## Procedure

1. **Bound the extract.** Ask the fetch tool for ≤8,000 chars, or fetch a byte range
   (`curl -r 0-30000`) and cut in code. Never the default whole document.
2. **File first.**

   ```python
   import os, pathlib
   scratch = os.environ.get("BH_AGENT_WORKSPACE") or os.environ.get("TMPDIR") or "/tmp"
   p = f"{scratch}/cache/jev-source/{slug}.txt"
   pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
   pathlib.Path(p).write_text(page_text, encoding="utf-8")
   ```

3. **Digest in code, cite by range.**

   ```python
   digest = extract_headings(page_text)     # or key sentences, or a topic line list
   print(f"{len(page_text)} chars -> {p}\n" + "\n".join(digest)[:600])
   ```

4. **Never re-fetch** a URL this session — re-cite from the cached file.
5. If the digest doesn't answer the question, expand only the relevant section with a
   line-range read — still never the whole page.

## Failure case

Any fetched page > 8 KB sitting in context means the bound was ignored. The
plugin does not cap for you; this skill is the one that should have set the limit
first.
