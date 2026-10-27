---
name: jev-skill-gate
description: "Use when choosing which skills a turn needs. Load what fits the task, cut the rest, keep the index under 2.5 KB."
version: 1.1.0
author: ronail/jev-cost-guard
license: MIT
metadata:
  tags: "context,skills,budget,economy"
---

# jev-skill-gate

This skill is the policy the rest of the bundle relies on. The skills index is a
**fixed prefix** — it rides in every API call — and it is rendered once when the
session starts. No harness lets a skill rewrite it mid-turn, so the gate always
ends in one of two moves:

- **this session** stays lean: load nothing the task does not need;
- **the next session** starts after pruning at install/config level, so the
  renderer sees the smaller roster.

Every step below is a *capability*. Concrete tool and command names per harness
(none of which you may run cross-harness) are in
[docs/harness-adapters.md](https://github.com/ronail/jev-cost-guard/blob/main/docs/harness-adapters.md).

## Always — before replying

1. **Measure the roster this session shipped.** Prefer the harness's own
   measurement when it has one (Hermes: `prompt-size --json`) — **only if you are
   running in that harness**. Otherwise use the portable estimator:

   ```sh
   python3 "${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-.}}/scripts/prompt_budget.py"
   # i.e. from a checkout: python3 scripts/prompt_budget.py
   ```

   (`PLUGIN_ROOT` is set by Codex for plugin-provided files, `CLAUDE_PLUGIN_ROOT` by
   Claude Code; with neither set, run it from the plugin checkout.)

   or, with no checkout available (any python3, no harness CLI):

   ```sh
   python3 - <<'PY'
   import glob, os, re
   roots = [p for p in (os.path.expanduser("~/.codex/skills"),
                        os.path.expanduser("~/.claude/skills"),
                        os.path.join(os.environ.get("HERMES_HOME") or "", "skills"))
            if os.path.isdir(p)]
   total = 0
   for root in roots:
       for f in glob.glob(root + "/**/SKILL.md", recursive=True):
           head = open(f, encoding="utf-8", errors="replace").read(4096)
           total += sum(len(v) + 2 for v in
                        re.findall(r"^(?:name|description):\s*(.+)$", head, re.M))
   print(total, "B  ~", total // 4, "tokens/call")
   PY
   ```

   Both numbers are a **lower bound** (renderers add separators and category
   lines). Quote them as an estimate anywhere the harness has no native figure.

2. **Under 2,500 B (~0.6k tokens/call)?** Done — spend the rest of the turn on
   the task. Over it, name the 1–3 fattest skills the *current* task cannot use
   and remove them from that harness's roster:
   - a `skills`/`auto_load` style config key, when the harness has one;
   - otherwise the skill directory itself — move it to an attic path
     (`<root>/../attic-<skill>`) so the trim is reversible in one `mv`;
   - a plugin that ships the skills: disable or remove it.

3. **Never edit another harness's config.** A Codex session prunes Codex's
   skills root; a Claude Code session prunes Claude Code's; a Hermes session
   prunes its profile config. None of them runs another runtime's CLI, and none
   opens another runtime's lock, database, or profile directory — that is what
   "blocked" turns are made of.

4. **Re-measure, then start a fresh session** so the renderer picks up the
   smaller roster. A pruned config changes nothing until the next session.

5. **If the task maps to no skill already in the roster**, load only the 1–3
   skills it needs and nothing else — never let an unrelated skill ride in
   "just in case".

## Degrade, never report blocked

If the measurement tool, the config file, or a lock is unavailable **in your own
harness**, fall back to the estimator above, label the number an estimate, and
continue. "The audit is blocked, so nothing was measured" is a failed turn, not
a result — the estimate needs no CLI, no network, and no write access.

## Why this is the cost floor

`jev-context-economy`, `jev-context-firewall`, `jev-prompt-budget-audit`,
`jev-test-output-triage` and `jev-source-digest` are the only skills any profile
needs pre-loaded. Anything else is pure per-call overhead until it earns back its
index line. The eight skills in this plugin cost **~800 B (~200 tokens/call)**
together — a roster as a whole, not one skill at a time, is what has to fit the
budget.

## Related

- `jev-context-economy` — runtime token hygiene that complements this gate
- `jev-prompt-budget-audit` — audits toolsets and the whole fixed prompt
