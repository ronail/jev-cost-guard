---
name: jev-source-digest
description: "Web/page sources: extract bounded, file, digest in code, cite by line range."
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  hermes:
    tags: [research, web, economy, digest]
---

# jev-source-digest

Profiles: researcher, product, planner, reviewer.

Whole pages pasted into context are the classic research-profile blowup. Turn
every source into a digest on disk.

## Procedure

1. **Bound the extract.** `web_extract(urls=[...], char_limit=8000)` — never the
   default whole document for research pages.
2. **File first.**
   ```python
   import os
   p = f"{os.environ.get('BH_AGENT_WORKSPACE','/tmp')}/cache/jev-source/{basename}.txt"
   Path(p).write_text(page_text)
   ```
3. **Digest in code, cite by range.**
   ```python
   digest = extract_headings(page_text)        # or key sentences, or a topic line list
   return f"{len(page_text)} chars -> {p}\n" + "\n".join(digest)[:600]
   ```
4. **Never re-fetch** a URL this session — re-cite from the cached file.
5. If the digest still doesn't answer the question, only then expand the relevant
   section with an `offset`/`limit` scan — still never the whole page.

## Failure case
Any `web_extract` result in context > 8 KB means the bound was ignored — the
plugin should have capped it, but this skill is the one that should have set
`char_limit` lower first.
