# Harness adapters

Every skill in this plugin is **harness-agnostic**: it states *capabilities*
("read a file range", "measure the fixed prompt"), never a command that only one
agent runtime provides. This file is the one place where capabilities are mapped
to concrete tools per harness.

Load it only when a skill step needs the concrete name. Nothing here is a
prerequisite: if your harness is not listed, use its nearest equivalent.

## The two hard rules

1. **Never drive another harness's CLI or state.** A Codex session must not run
   `hermes`, and a Hermes session must not run `codex`/`claude`. Another runtime's
   install lock, profile config, and databases are not yours to open — a command
   that needs one is a misconfiguration, not a blocker to report.
2. **Degrade, never block.** Every skill step has a fallback that uses only the
   current session's own context. Report the *estimate you produced* and how you
   produced it. Never answer "the audit was blocked, nothing was measured".

## Capability map

| capability | Hermes | Claude Code | Codex | Antigravity |
|---|---|---|---|---|
| run a shell command | `terminal` | Bash | shell tool | `run_command` |
| read a file / a range | `read_file(offset, limit)` | Read | read tool (or `sed -n '10,40p' f`) | `view_file` (`StartLine`/`EndLine`) |
| find text in files | `search_files` | Grep | `rg` / `grep` via shell | `grep_search` |
| run code, print only the aggregate | `execute_code` | Bash (`python3 - <<'PY'`) | shell (`python3 - <<'PY'`) | `run_command` |
| isolated child context | `delegate_task` | Task / subagent | separate `codex exec` run (no in-session subagent) | `invoke_subagent` (`TypeName: self` for full capability, `research` for read-only) |
| scratch dir for spilled bodies | `$BH_AGENT_WORKSPACE/cache/` | `/tmp` or `./.cache/` | `/tmp` or `./.cache/` | `/tmp` or `./.cache/` |
| fetch a URL with a byte bound | `web_extract(char_limit=…)` | WebFetch | fetch tool (else `curl -r 0-N`) | `read_url_content` / `search_web`, else `curl -r 0-N` |
| **native fixed-prompt measurement** | `hermes prompt-size --json` | *none* | *none* | *none* |
| fallback measurement (works everywhere) | `python3 scripts/prompt_budget.py` | same | same | same (`--harness antigravity` knows its roots) |
| prune the skills index | `skills.auto_load` / `skills.disabled` in the profile `config.yaml` | remove/disable the skill dir or its plugin | move the skill dir out of the skills root; disable the plugin | move the skill dir out of `.agents/skills`; or `agy plugin disable <name>` |
| start a scoped session with fewer skills | `hermes -p <p> chat -s A -s B` | point the skill dir at a scoped set | fresh `codex exec` after pruning | point `.agents/skills` at a scoped set |
| drop/compact the transcript | `/compress`, `/new` | `/compact` | `/new` | start a new conversation (no documented compact command) |
| auto-gate a large tool result | native companion hook (`native/skill-router`, optional) | `PostToolUse` hook | `PostToolUse` hook, declared in `config.toml` | `PreInvocation` hook + transcript read (its `PostToolUse` has no output) |

`hermes prompt-size --json` is authoritative **only when you are Hermes**. Every
other harness has no native prompt-size command, so the portable estimator is the
number you quote — labelled as an estimate.

### Antigravity specifics

- **No todo tool.** `manage_task` manages *background processes*
  (`list`/`kill`/`status`/`send_input`) — it is not a checklist. Track a
  multi-step task in a **task artifact**: `write_to_file` with `IsArtifact: true`
  and `ArtifactMetadata.ArtifactType: "task"`, then edit it with
  `replace_file_content` / `multi_replace_file_content` as steps complete.
- **Two skill scopes are live at once**: workspace `.agents/skills/<name>/SKILL.md`
  (`.agent/` is still read for back-compat) and the global roots
  `~/.gemini/config/skills` plus `~/.gemini/antigravity/skills` (IDE) and
  `~/.gemini/antigravity-cli/skills` (CLI). Plugin skills load from
  `~/.gemini/config/plugins/<plugin>/skills`.
- **A skill's folder name must equal its frontmatter `name:`**, so renaming a
  directory without editing `SKILL.md` silently drops the skill.
- Skills double as slash commands (`/jev-skill-gate`), which is how you invoke one
  explicitly instead of hoping the model picks it.
- **Hooks live in `hooks.json` at the plugin root** (workspace: `.agents/hooks.json`),
  as named groups; a plugin's file is trusted as soon as it is installed — unlike Codex,
  which needs the hook declared in a config layer and trusted by hash. `PreToolUse` and
  `PostToolUse` wrap handlers in `{matcher, hooks:[…]}`; the other four events are flat
  arrays.
- **Two hook gotchas:** `PreToolUse` must answer `{"decision": …}` (`{}` is a deny), and
  `PostToolUse` carries **no tool output** — the result is only in the transcript, which is
  why the verdict gate runs on `PreInvocation` instead.

## Skills index: why pruning is never mid-session

The skills block is rendered into the system prompt once, when the session
starts. No harness lets a skill rewrite it mid-turn. So the gate always ends in
one of two moves:

- **now**: keep the current session lean by not loading skills it does not need;
- **next session**: prune at install/config level, then start a new session so
  the renderer sees the smaller roster.

## Portable estimator

```sh
python3 scripts/prompt_budget.py --harness auto        # this session's harness
python3 scripts/prompt_budget.py --root ~/.codex/skills --budget 2500
python3 scripts/prompt_budget.py --harness antigravity # ~/.gemini roots + plugin skills
python3 scripts/prompt_budget.py --json                # machine-readable
```

It walks the skills roots that exist under `$HOME`, parses each `SKILL.md`
frontmatter, and sums `len(name) + 2 + len(description)` per skill — the part of
the index line a skill controls. Real renderers add separators and per-category
lines, so treat the total as a **lower bound**; it is exact for ranking and for
deciding what to cut, and it needs no harness CLI, no network, and no lock.

Two filters keep the number honest:

- a plugin cache usually holds plugins the harness does **not** load (disabled, or
  superseded versions), so Codex roots are filtered against `[plugins.*] enabled`
  in `~/.codex/config.toml` (`--all-plugins` counts them anyway) and Antigravity
  roots against `~/.gemini/config/config.json` plus the `plugin.json.disabled`
  marker `agy plugin disable` leaves behind; roots left out are reported as
  `excluded_roots`;
- duplicate skill names collapse to one entry, because one name renders once.
