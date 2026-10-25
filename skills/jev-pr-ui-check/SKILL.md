---
name: jev-pr-ui-check
description: Use when a user asks whether the latest merged PR contains UI changes and wants a jev-backed yes/no verdict without the file contents.
version: 1.0.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "ui,pr,github,jev-find,jev-screen"
category: agent
triggers:
  - "tell me if the last merged pr contains ui changes"
  - "did the latest merged pr add any ui changes"
  - "does the most recent merged PR have ui changes"
  - "last merged pr ui changes"
---

## When to Use

A user wants to know **whether the most recent merged PR touched UI** — *not* the diff contents. Route it through jev instead of pasting file bodies: resolve the PR's changed files, then let jev judge each file's UI-relevance with a single batched `noul` question ("is this file about UI rendering/styling/layout/markup/template/components?"). Only paths + probabilities reach the transcript; no file content is shown.

## Procedure (in order)

### 1. Resolve the latest merged PR (into `main`)
```
gh pr list --state merged --base main --limit 1
```
Take number + `mergeCommit`. If none, stop: "no merged PRs."

### 2. Get its changed files as **real** working-tree paths
```
gh pr view <n> --json files --jq '.files[].path'
```
Merged PRs are already in `main`, so the changed paths **exist locally**. Keep only `exists(path)`.

> **Do NOT point jev at `.git/refs/pull/<n>/merge`.** Those refs are frequently absent/pruned, and `jev_find` silently returns `screened=0 requests=0` (zero model calls) against them — exactly the "did the prompt do anything?" trap. Real files only.

### 3. Judge each file once with jev (`jgrep`)
```
python3 skills/jev-pr-ui-check/scripts/check_pr_ui.py
```
The script resolves the provider exactly like `mcp/jev/server.py`:
- `TYPESAFE_API_KEY` present → TypeSafe (`api.typesafe.ai`), model **always `jev-1.13.0`** (a `-free` model is zen-only and 400s against TypeSafe — this is the routing bug fixed in PR #8);
- else OpenCode Zen relay (`jev-1.13-free`, `Bearer public` free tier).

It runs `jgrep -o -p 0 --whole --json --max-chars 8000 "<UI query>" <files>` (one jev question per file, batched) and parses `p` per file.

### 4. Verdict
Per-file `p` is printed. Overall:
- any `p >= 0.7` (likely) → **UI CHANGES PRESENT** (list those files);
- else → **no UI changes**.

jev's bands: `>=0.7` likely / `0.3–0.7` ambiguous / `<0.3` unlikely. A 429/402 from jev is surfaced as `jev-call note:` (no content leaked) — the script never exits non-zero on a jev slip; it reports and finishes.

## Pitfalls
- Always pass real working-tree paths, never `.git/refs/*`.
- Never bake a `-free` model into TypeSafe; let the script resolve it.
- Don't re-paste file bodies to answer "is it UI?" — the `p` is the answer.
