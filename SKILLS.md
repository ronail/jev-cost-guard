# jev-cost-guard — index & wiring

One source of truth for where each `jev-*` artifact loads and what its cost is.
The cost figures are from `hermes prompt-size --json` on the 8 stock profiles
(bytes / 3.9 ≈ tokens, ~90 B per skills-index entry ≈ ~25 tokens/call).

## Hermes skills (each is `hermes/jev-<name>/SKILL.md`)

| skill | index bytes | loaded by (auto_load) | domain |
|---|---|---|---|
| `jev-skill-gate` | ~35 | ALL 8 | the policy that keeps the index under ~2.5 KB |
| `jev-context-economy` | ~40 | ALL 8 | every-call token hygiene |
| `jev-prompt-budget-audit` | ~35 | ALL 8 | fixed-prompt audit loop |
| `jev-context-firewall` | ~45 | ALL 8 | delegate bulk output off the main transcript |
| `jev-delegate-guard` | ~40 | ALL 8 (orchestrators) | pick leanest child profile + break-even gate |
| `jev-test-output-triage` | ~50 | dev, devops, tester, reviewer | test/log bloat |
| `jev-source-digest` | ~50 | researcher, product, planner, reviewer | web/page bloat |



## Companion: the jev-mcp-model-server skill

This is the usage guide for the MCP itself — not an auto-loaded cost guard. It ships
under `hermes/jev-mcp-model-server/`: `SKILL.md` (systemone procedure + auth),
`templates/mcp-stdlib-skeleton.py` (a working, stdlib-only systemone stub to copy),
and `references/opencode-zen-free-bypass.md` (auth + failure-mode matrix). Install it
into a profile when the agent must drive `jev-1.13-free` via `mcp/jev/server.py`
rather than a direct relay call.

## Wiring per profile (config.yaml)

### Shared block — every profile
```yaml
skills:
  auto_load:
    - jev-skill-gate
    - jev-context-economy
    - jev-prompt-budget-audit
    - jev-context-firewall
    - jev-delegate-guard
agent:
  # cut the fat the audit skill finds; kanban only if this profile runs kanban tasks
  disabled_toolsets: [kanban, connections, tts, vision, browser-use]
```

### Domain additions
```yaml
# dev / devops / tester / reviewer (append under skills.auto_load)
    - jev-test-output-triage

# researcher / product / planner / reviewer  — note reviewer gets BOTH domains
    - jev-test-output-triage
    - jev-source-digest
```

`reviewer` loads both because it runs tests *and* consumes research diffs/pages.
Remove whichever half that profile doesn't exercise.

## Multi-harness

- **Claude Code**: `claude-code/index.json` lists each skill's trigger + body.
- **Cursor**: `cursor/*` are `.mdc` rule files mapping trigger → skill body.

## Verify
```bash
hermes -p <profile> prompt-size --json | jq '.skills_index.bytes'
# gate: < 2500 bytes (~0.6k tokens/call). Run once per profile after wiring.
```
