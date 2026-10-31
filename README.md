# jev-cost-guard

Token-economy guard for agent harnesses — **Hermes-first, harness-agnostic** — as a
single **portable plugin** (skills + MCP, no Python hook). It attacks the two real
token leak classes visible in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas + the skills index
   are the fat. → pruned/audited by `jev-skill-gate` / `jev-prompt-budget-audit`.
2. **Tool output bloats the transcript.** → judged **on demand** by **jev** (TypeSafe,
   or the OpenCode Zen free tier) through the `jev` MCP tools — `jev_find` to rank
   files before you read them, `jev_screen` to admit/quarantine fetched content,
   `jev_summarize` to compress a result to a single ~7-token `noul` verdict.

> **Why judge, not replace.** jev returns typed decisions, never text, so a verdict
> cannot stand in for the bytes it judged. TypeSafe's *Harness Engineering* use case
> asks for exactly this — "detect tool-call errors" and "log structured check results
> and probabilities" — and context managers that work (`jev-compaction`,
> `lossless-compaction`) keep every kept line verbatim.

> The deterministic head+tail *plugin* (`jev-context-guard`) was removed — it is not
> a jev-model feature. The model-based decision below is the only trimming lever.

---

## How to use

### Install the portable plugin (skills + MCP together)

One package, one install, one enable — skills and the `jev` MCP server register in
the same step, so there is no second path to keep in sync.

```bash
hermes -p dev plugins install ronail/jev-cost-guard
hermes -p dev plugins enable jev-cost-guard
hermes -p dev mcp list        # -> jev  ... 4 tools  enabled
```

Nothing else to wire: the plugin ships `plugin.json`, `mcp.json` (which points at
`${PLUGIN_ROOT}/mcp/jev/server.py`), and the `jev-*` skills under `skills/`. There is
**no Python hook** and no `plugin.yaml` — the package runs anywhere a portable plugin
is accepted.

### Skills

| skill | purpose |
|---|---|
| `jev-skill-gate` | keep the skills index ≤ ~2.5 KB (fat is re-sent every call) |
| `jev-context-economy` | per-turn hygiene: batch calls, grep-then-read, cap output |
| `jev-prompt-budget-audit` | fixed-prompt audit loop |
| `jev-context-firewall` | delegate bulk tool output off the main transcript |
| `jev-delegate-guard` | pick the leanest child profile; break-even gate |
| `jev-test-output-triage` | triaging test/log bloat |
| `jev-source-digest` | triaging web/page bloat |
| **`jev-mcp-model-server`** | usage guide: how/when to drive jev (TypeSafe or zen) via this MCP |

Add the ones you want to each profile's `skills.auto_load` in `config.yaml`.

#### Harness-neutral by design

The skills are shipped to Hermes, Claude Code and Codex unchanged, so they never
name one runtime's CLI as a requirement. Each step is a **capability** ("measure
the fixed prompt", "run code and print only the aggregate"), and the concrete
tool/command per runtime lives in one table:
[`docs/harness-adapters.md`](docs/harness-adapters.md).

Two rules the whole bundle follows:

- **Never drive another harness's CLI or state.** A Codex session does not run
  `hermes`; a Hermes session does not run `codex`. Another runtime's install lock,
  profile config and databases are not yours to open — a step that needs one is a
  misconfiguration, not a "blocked audit".
- **Degrade, never block.** Where a harness has no native measurement, the skills
  fall back to a portable estimator and label the result an estimate, instead of
  reporting that nothing could be measured.

```sh
python3 scripts/prompt_budget.py --harness auto      # per-skill index cost, no CLI/lock/network
python3 scripts/check_harness_agnostic.py            # guards the invariant above in every skill
```

### MCP tools

A zero-dependency (pure stdlib, no pip) MCP server exposing four tools. It talks to
either TypeSafe's own API or the OpenCode Zen relay directly — never the agent's own
provider — and uses **only jev models**.

**Providers (auto-selected, same request shape).** With `TYPESAFE_API_KEY` set the
server calls TypeSafe directly — `https://api.typesafe.ai/v1/systemone`, model
`jev-latest` (also `jev-1.13.0` / `jev-1.14.0`) — using `Bearer $TYPESAFE_API_KEY`
and standard JSON headers; there is no free tier and no paid-fallback retry. With
no TypeSafe key it uses the OpenCode Zen relay: `JEV_MODEL` (default
`jev-1.13-free`) and `JEV_ENDPOINT` (default `https://opencode.ai/zen/v1/systemone`)
are optional and the key is **never** on the command line. Under Hermes the MCP
also falls back to `OPENCODE_ZEN_API_KEY` in `profiles/<profile>/.env`; a generic MCP
host (Claude, Codex) is self-contained and reads only its own config env. Force either with
`JEV_PROVIDER=typesafe|opencode`. No browser/OAuth/device-flow. Auth modes (real key,
the relay free-tier `Bearer public` no-key bearer, OpenCode desktop UA fingerprint,
and failure modes) are documented in `mcp/jev/env.example` and the server docstring.

| env | default | note |
|---|---|---|
| `JEV_PROVIDER` | *(auto)* | `typesafe` when `TYPESAFE_API_KEY` is set, else `opencode` (Zen relay); force either |
| `JEV_MODEL` | `jev-1.13-free` / `jev-latest` | optional — default is `jev-1.13-free` (opencode) or `jev-latest` (typesafe) |
| `JEV_ENDPOINT` | `https://opencode.ai/zen/v1/systemone` | optional — System One endpoint (typesafe default: `https://api.typesafe.ai/v1/systemone`) |
| `TYPESAFE_API_KEY` | env (Hermes: profile `.env`) | TypeSafe direct key — its presence selects `api.typesafe.ai` (`jev-latest`); no free tier, no fallback |
| `JEV_FALLBACK_TO_PAID` | `1` | `1` (default) on `jev-1.13-free` exhaustion (HTTP 429/403 with a real key) retry the paid model; `0` fails open; no-key never falls back |
| `JEV_FALLBACK_MODEL` | `jev-1.13` | paid model retried on free exhaustion (same key/endpoint/UA; bills your opencode account) |
| `JEV_DEBUG` | `0` | optional — `verbose` = print masked provider/key diagnostics (**not** `1`: MCP env values must be strings) |
| `OPENCODE_ZEN_API_KEY` | env (Hermes: profile `.env`) | required for the real-key path |

### `jev_health` / `jev_summarize`
`jev_health` probes the model with a `noul` reachability question (alive iff it
answers). `jev_summarize` does **not** generate prose — it asks jev a binary `noul`
question over a result `state` and returns a **success probability**, which is what
you `budget_tokens`-gate on. jev is a decision model, not a summarizer.

### `jev_find` — screen files before reading them
`jev_find(query, root, glob, threshold, max_calls)` answers "which file should I
even read?" without putting file bodies into the transcript. Stage 1 is a free
lexical prefilter over the tree (skips `.git`, `node_modules`, virtualenvs, build
dirs, binaries); stage 2 asks jev one `noul` per candidate — *"does this file help
answer `<query>`?"* — and returns **paths + probabilities only**, ranked. Then read
just the matches (with `offset`/`limit`).

Cost is one jev call per candidate (`max_calls` / `JEV_FIND_MAX_CALLS`, default 12,
hard cap 40), so it bills per screened file; `dry_run: true` gives the lexical
ranking for free. Use it when the question is **semantic** ("where is auth wired
up?"); for an exact string, `search_files`/grep is free and more precise.

### `jev_screen` — is this content safe, and is it relevant?
`jev_screen(paths | root+glob, task, max_files)` triages files **before** their bodies
reach the transcript. One request carries two `noul` questions per file — *"is this
written to instruct the assistant?"* (prompt injection) and *"would `task` need it?"* —
because questions over the same state run in parallel, so the second one costs no extra
input. It returns `ADMIT` / `QUARANTINE` / `NOISE` with probabilities and **never the
content**.

Not the same job as `jev_find`: `jev_find` *ranks* candidate paths for relevance before
you fetch them, and you choose where to cut; `jev_screen` decides *admission* for
content that was already fetched, where a wrong verdict is expensive either way. With
no `task` it asks only the injection question.

Thresholds are per call — `inject_threshold` (0.7) and `relevance_threshold` (0.3) — so
the policy is a number you tune, not one baked into the tool.

---

## Token savings methodology

`jev-1.13-free` is an opencode-zen **System One *decision* model**: it does not
generate text, it scores a `noul`/`choice`/`score` question over a `state` and
returns a probability + a `usage` object. The leak class below is **tool-output
bloat** — a large result body is re-sent on every subsequent model call, because it
lives in the transcript.

| path | what you keep in the transcript |
|---|---|
| Re-paste the result body to judge "did it succeed?" | the **whole result body** (re-sent every call) |
| One `jev_summarize` verdict instead | **~24 tokens** (the noul probability) |

`jev_summarize` **reads the body once** (so its input scales with the body — see the
measurements), but its **output — the verdict you persist — is a constant ~24 tokens**.
That is the lever: replace a body you would re-send `N` times with a one-time ~24-token
verdict. Break-even vs. keeping the body is ~2–3 subsequent calls for real result
sizes; the bigger the body, the faster it pays off.

---

## Test results (token-saving evidence)

Measured through the **dev profile** — real `OPENCODE_ZEN_API_KEY` (from
the active profile's `.env`, never committed), `Authorization: Bearer ***`, desktop
`User-Agent`, at `https://opencode.ai/zen/v1/systemone`, model `jev-1.13-free`
(primary; paid `jev-1.13` is a fallback on free exhaustion — see
`JEV_FALLBACK_TO_PAID`). Each row is one `jev_summarize` call: a `noul` "is the above
tool-result successful?" question over a synthetic tool-output body of the stated size.
The relay returns its own `usage`, so the full cost of one judgment is visible.

| body (chars) | ~body tokens | input_tokens | output_tokens | noul |
|---|---|---|---|---|
| 200 | ~50 | 366 | **24** | 0.87 |
| 1,000 | ~250 | 702 | **24** | 0.90 |
| 4,000 | ~1,000 | 1,961 | **24** | 0.89 |
| 20,000 | ~5,000 | 8,678 | **24** | 0.84 |

Two measured, non-negotiable facts:

1. **Input scales with the body** (~`1.7×body + ~280`) — jev reads and bills the full
   state. The "a jev decision costs a flat ~312 tokens" assumption is **false** for
   real results; ~312 was only the tiny `is_reachable` probe (no body).
2. **Output is constant ~24 tokens** — the noul verdict — and it is calibrated: the
   bodies here are all synthetic success output, so a high noul (0.84–0.90) is the
   correct answer; it is not stuck at 0 or 1.

### Reproduced via the `jev-mcp-model-server` skill (dev profile)

Running the skill's shipped reference client
(`skills/jev-mcp-model-server/templates/mcp-stdlib-skeleton.py` → `_ask`, key from the
active profile's `.env`) — i.e. the same `noul` judgment a dev profile drives through
`jev_summarize` — the verdict the agent **persists** stays constant as the body grows
(the agent keeps just the `noul`, ~7 tokens, vs. re-sending the whole body every later
call; jev's full billed output is the ~24 in the table above):

| body (chars) | ~body tokens | verdict kept | noul |
|---|---|---|---|
| 4,000 | ~1,000 | **~7 tokens** | 0.88 |
| 8,000 | ~2,000 | **~7 tokens** | 0.77 |
| 20,000 | ~5,000 | **~7 tokens** | 0.88 |

A ~5,000-token result collapses to a 7-token `noul` probability — the transcript no
longer re-sends the body on later calls.

Net saving: `jev_summarize` costs `~(1.7×body + ~300)` **once**; persisting the
~24-token verdict instead of the body saves `(body − 24)` tokens on **every subsequent
call**. For a 4,000-char (~1,000-token) body carried across 5 later model calls:
keep-body ≈ 5,000 tokens vs summarize-once + verdict ≈ 1,985 + 5×24 = ~2,105 →
**saves ~2,900 tokens**. Break-even is ~2 subsequent calls; the bigger the body, the
faster it pays off. No other model is involved.

---

## Files

| path | what |
|---|---|
| `plugin.json` | portable plugin manifest (Agent Plugins 1.0.0) |
| `mcp.json` | portable MCP declaration — `${PLUGIN_ROOT}/mcp/jev/server.py`, `JEV_MCP=1` |
| `mcp_config.json` | Antigravity plugin MCP declaration — same `jev` server, the filename `agy` reads |
| `.antigravity-plugin/plugin.json` | Antigravity marketplace metadata (`displayName`, `logo`, `suggestedPrompts`) |
| `skills/` | the `jev-*` skills (auto-discovered, one SKILL.md per dir), harness-neutral |
| `docs/harness-adapters.md` | capability → tool/command map for Hermes / Claude Code / Codex / Antigravity, plus the no-cross-harness rule |
| `scripts/prompt_budget.py` | portable fixed-prompt / skills-index estimator (no harness CLI, no lock, no network) |
| `scripts/check_harness_agnostic.py` | guard: no skill may present another runtime's CLI as an unscoped requirement |
| `mcp/jev/server.py` | the one jev implementation: MCP stdio server (dual-frame) + `--verdict` CLI; owns provider selection (TypeSafe / OpenCode Zen) and every fallback |
| `mcp/jev/env.example` | env reference: TypeSafe + Zen auth, fallback knobs, failure modes |
| `hooks/claude-codex-jev-verdict.py` | standalone soft-gate hook for Claude Code / Codex |
| `hooks/antigravity-jev-verdict.py` | Antigravity gate: reads the transcript on `PreInvocation`, injects one verdict line |
| `hooks/antigravity-jev-route.py` | Antigravity skill hint (opt-in, `"enabled": false`): one roster line per session |
| `hooks/pretool-gate.py` | pre-tool permission gate for Claude Code / Codex / Antigravity: belts → one jev request → ask/deny, audit by default |
| `hooks/stop-gate.py` | stop gate: run facts from the transcript, then one jev `noul`; blocks only on a likely *and* decisive answer |
| `hooks/lib/transcript.py` | normalizes Claude Code and Antigravity transcripts into prompts, tool calls, results and closing text |
| `hooks/lib/gates.py` | the pure half: question wording, thresholds, belts, `decide_command`, `decide_completion` |
| `hooks/gate-policy.json` | one policy for every gate and host: mode, thresholds, state cap, cache scope and pruning, allow mode |
| `hooks.json` | Antigravity plugin wiring for that gate (root — Antigravity reads no other path) |
| `scripts/install-antigravity.py` | no-plugin workspace install: `.agents/mcp_config.json` + `.agents/hooks.json`, merged |
| `hooks/hooks.json` | Claude Code plugin wiring for that gate (PostToolUse) |
| `.claude-plugin/marketplace.json` | Claude Code marketplace catalog (this repo lists itself) |
| `.claude-plugin/plugin.json` | Claude Code plugin manifest (skills + MCP + hook + `userConfig` keys) |
| `.codex-plugin/plugin.json` | Codex plugin manifest (skills + MCP + hook + `interface`) |
| `.mcp.json` | Claude Code plugin MCP declaration — `${CLAUDE_PLUGIN_ROOT}/mcp/jev/server.py` |
| `claude-code/index.json` | Claude Code skills trigger index: `name`/`trigger` → `skills/<name>/SKILL.md` |
| `claude_desktop_config.json.example` | manual (no-marketplace) Claude Desktop config template |
| `scripts/install-claude-plugin.py` | one-command manual installer: writes the config above |
| `docs/token-savings.md` | token-accounting + cap-removal history |
| `LICENSE` | MIT |

## What changed from the hook build

Earlier builds shipped a native Python plugin (`__init__.py` + `plugin.yaml`) whose
`transform_tool_result` hook auto-annotated oversized results. That required a Python
plugin runtime and a second install/enable path, and could not ship as a portable
package. This release is **portable-only**: skills + MCP, one install, one enable.
The on-demand tools replace the hook — `jev_find` before you read, `jev_screen` before
you admit, `jev_summarize` when you want a verdict. The hook code remains in git
history (`main` through the 0.4.x releases) if you want to run it as a separate
Python plugin.

---

## Using jev in Claude Code / Codex / Antigravity (portable MCP)

The `mcp/jev/server.py` stdio server is **dual-frame**: it answers `Content-Length`
requests (Claude Code, Codex — strict MCP clients) and newline-delimited JSON
(Hermes' client), so the **same server loads in every harness** with no fork.
Expose the jev key (`TYPESAFE_API_KEY` or `OPENCODE_ZEN_API_KEY`, never committed) in the
harness env, then register `python3 <repo>/mcp/jev/server.py` as an MCP server.
jev stays the **only** oracle model; Claude Code/Codex are pure clients that call
`jev_health` / `jev_summarize` like any other tool. This path is **self-contained**:
with no Hermes vars set, the server never reads a Hermes profile — only the env in
its own config.

`<repo>` below is your local clone of https://github.com/ronail/jev-cost-guard.

### Claude Code — plugin marketplace (recommended)

This repo is its own Claude Code marketplace (`.claude-plugin/marketplace.json`). From a Claude Code
session:

```text
/plugin marketplace add ronail/jev-cost-guard
/plugin install jev-cost-guard@jev-cost-guard
```

That installs the jev MCP server (`.mcp.json`), the eight `jev-*` skills (`skills/`), and the
`PostToolUse` soft gate (`hooks/hooks.json`) as one unit. On enable, Claude Code opens a
configuration dialog (see **Set your key**). Validate a checkout with `claude plugin validate .`.

#### Set your key

`typesafe_api_key` is marked `required` — that is what makes Claude Code show the dialog (it gates
prompting on `hasRequiredConfigMissing`). Enter either:

- your TypeSafe key (calls go to `api.typesafe.ai`), or
- the literal word `none` to stay on the OpenCode Zen relay.

`none` is a sentinel the server treats as "not using TypeSafe", so a required field doesn't trap
Zen-only users into sending a bogus key. The optional `opencode_zen_api_key` field is only consulted
when TypeSafe says `none`: leave it empty for the keyless free tier (`Bearer public`), or paste a Zen
key. Both are `sensitive`, so the values go to the OS keychain rather than `settings.json`. If you
dismiss the dialog, re-enable the plugin from `/plugin` → **Installed**.

Environment still works and takes precedence when you need it — `env` in `~/.claude/settings.json` is
inherited by the MCP server and the hook:

```json
{ "env": { "TYPESAFE_API_KEY": "ts-..." } }
```

An unfilled `${user_config.*}` placeholder is ignored by the server rather than sent as a
credential (`_clean_key`), so the optional field can't poison the required one.

### Claude Code — manual install (no marketplace)

Add the server + hook to your Claude Code config (`claude_desktop_config.json`) — `mcpServers` and `hooks` go in the
same file. The **recommended** install is one command — it resolves your clone path, takes the key from `--key` /
`--env-file` / this repo's `.env` / the shell env (never a Hermes profile), and writes the server + gate in place:

```sh
python3 scripts/install-claude-plugin.py                 # keyless (Zen free tier)
python3 scripts/install-claude-plugin.py --key ts_proj_xxx   # or --env-file .env
```

Prefer that over editing by hand. A ready-to-edit copy also ships as
`claude_desktop_config.json.example` — replace `<repo>` with your clone path, drop it at your Claude Desktop config
location, and restart Claude Code. The Claude Code skills triggers are indexed in `claude-code/index.json` (each
`file` points to the matching `skills/<name>/SKILL.md`). The config this installer writes looks like:

```json
{
  "mcpServers": {
    "jev": {
      "command": "python3",
      "args": ["<repo>/mcp/jev/server.py"],
      "env": {
        "TYPESAFE_API_KEY": "<your TypeSafe key; from the active profile's .env, never commit>"
      }
    }
  },
  "hooks": {
    "PostToolUse": [
      {
        "command": "python3",
        "args": ["<repo>/hooks/claude-codex-jev-verdict.py"],
        "env": {
          "TYPESAFE_API_KEY": "<your TypeSafe key>",
          "JEV_VERDICT_THRESHOLD_CHARS": "4000"
        }
      }
    ]
  }
}
```

Substitute `OPENCODE_ZEN_API_KEY` for `TYPESAFE_API_KEY` (and optionally
`JEV_MODEL=jev-1.13-free`) to run these on the Zen relay instead — the server
autodetects the provider from whichever key is present.

Restart Claude Code after editing. `PostToolUse` fires after each tool; the hook
reads the tool output and, when it exceeds `JEV_VERDICT_THRESHOLD_CHARS`, pipes it
to `mcp/jev/server.py --verdict` (a one-shot CLI — **no** MCP server is hosted in
the hook), prints a JSON `is_successful` noul verdict, and **always exits 0** so a
jev slip (rate limit / no key / 5xx / paid-exhaustion) never stalls a session.
The verdict reuses `_jev_ask`, so the same `Bearer public` / `Bearer <key>` auth
and paid-jev fallback (`JEV_FALLBACK_TO_PAID` / `JEV_FALLBACK_MODEL`) apply.

### Codex

Install as a Codex plugin. This repo is also a Codex marketplace — Codex reads the
legacy `.claude-plugin/marketplace.json`, so no extra catalog file is needed:

```sh
codex plugin marketplace add ronail/jev-cost-guard
codex plugin add jev-cost-guard@jev-cost-guard
codex plugin list     # jev-cost-guard   installed, enabled
codex mcp list        # jev -> mcp/jev/server.py
```

That brings in the four jev MCP tools, the eight `jev-*` skills (`skills/`), and the
`PostToolUse` gate (`hooks/hooks.json`).

The plugin manifest deliberately carries **no** `env_vars` and no `${…}` placeholders: Codex
validates the portable `mcp.json` against the plugin schema, and an `env_vars` key there makes it
drop the whole server silently (plugin installs, skills work, `codex mcp list` stays empty). It
also does not expand `${VAR}` inside a manifest `env` block — a placeholder arrives as a literal
string. Keys therefore belong in `~/.codex/config.toml`, as below.

#### Codex desktop app: Settings → Integrations → Plugins → MCPs

The ChatGPT desktop app, Codex CLI, and the IDE extension share one MCP configuration, and that
page is a form over `[mcp_servers.*]` in `~/.codex/config.toml`.

1. **Settings → Integrations → Plugins → MCPs**
2. Add a server: name `jev`, transport **STDIO**, command `python3`,
   argument `<repo>/mcp/jev/server.py`
3. Add the environment variables `TYPESAFE_API_KEY` (your key) and `JEV_REQUIRE_TYPESAFE` (`1`)
4. **Restart**

If that form has no env section, add the table to the same file and restart:

```toml
[mcp_servers.jev.env]
TYPESAFE_API_KEY = "ts-…"
JEV_REQUIRE_TYPESAFE = "1"
```

**Which bucket your server lands in.** A server declared by the plugin appears under
**MCPs → From plugins**, which is read-only — no env editing there. For an editable entry under
**MCPs → Servers**, register it in `~/.codex/config.toml` and switch the plugin's copy off so you
don't get two:

```toml
[plugins."jev-cost-guard@jev-cost-guard".mcp_servers.jev]
enabled = false

[mcp_servers.jev]
command = "python3"
args = ["<repo>/mcp/jev/server.py"]
env_vars = ["TYPESAFE_API_KEY"]

[mcp_servers.jev.env]
JEV_REQUIRE_TYPESAFE = "1"
```

Point `args` at the repo checkout (stable across plugin updates) or at
`~/.codex/plugins/cache/jev-cost-guard/jev-cost-guard/<version>/mcp/jev/server.py`. With this in
place `codex mcp list` shows a single `jev` carrying those env values.

Two caveats for the desktop app: re-saving the server from that page can drop hand-added tables
(re-paste them if the key stops working), and GUI-launched apps do not read your shell profile, so
`env_vars` forwarding is unreliable there — set the value inline. Verify with `codex mcp get jev`.

The TypeSafe key is required on every host, but Codex has no plugin configuration dialog — its
binary carries no `userConfig`, `CLAUDE_PLUGIN_OPTION_*`, or `pluginConfigs` — so the requirement is
enforced by the server at runtime instead. Without a key every jev tool returns:

```text
TYPESAFE_API_KEY is required by this plugin but is not set. Store it once with
`python3 <server.py> --set-key` (writes the credential file, 0600), …
```

**Store the key once.** The server resolves, in order: `TYPESAFE_API_KEY` → `JEV_API_KEY_FILE` →
`$PLUGIN_DATA/api_key` → Hermes profile `.env` → keyless relay.

`PLUGIN_DATA` is the directory every host hands a plugin subprocess (Agent Plugins spec §9.1): a
per-plugin, client-managed data directory that survives plugin updates. It is the only place this
package writes a credential, because it makes the file **attributable** — it sits under the
application that created it:

| Where | File |
| --- | --- |
| Codex | `$PLUGIN_DATA/api_key` — the host picks that directory and it can change on reinstall, so let `--key-status` (or the "key required" error) print the exact path. It is currently under `~/.codex/plugins/data/agent-plugins/<hash>/api_key`, **not** `…/data/jev-cost-guard-jev-cost-guard/` |
| a caller with no `PLUGIN_DATA` (hook, bare CLI) | name one with `--path`, or set `JEV_API_KEY_FILE` |

There is no global fallback on purpose. A key at `~/.config/jev/api_key` is owned by nobody an
operator can name, so this version never creates or reads one: if a file from an older install is
still on disk, `--key-status` reports it as `legacy_file` and `--clear-key` deletes it.

That is the part that makes Codex work: Codex **scrubs** the environment it hands to stdio MCP
servers (a spawned server sees only `HOME`, `PATH`, `SHELL`, `TMPDIR`, `USER`, `LOGNAME`, `LC_CTYPE`,
`__CF_USER_TEXT_ENCODING`), so a bare `export TYPESAFE_API_KEY=…` never arrives. A file the server
opens itself does.

The installed server can do the same for you, if you'd rather not echo the key:

```sh
codex mcp get jev      # prints the installed server path in its args

# a host-launched run already has PLUGIN_DATA; passing it makes the CLI behave the same.
# Codex masks that value in `codex mcp get jev`, so take it from --key-status or from the
# "key required" error, which both print the exact directory:
PLUGIN_DATA=<that directory> python3 ~/.codex/plugins/cache/jev-cost-guard/jev-cost-guard/<version>/mcp/jev/server.py --set-key

# no owner directory in this process? name the file yourself, under the app that should own it
python3 <server.py> --set-key --path ~/.codex/jev/api_key
```

`--set-key` prompts with hidden input and writes the file `0600` — and refuses when it cannot name
an owner; `--key-status` reports the key source, the candidate files, any legacy file, and the
endpoint and model that selection produces; `--clear-key` removes every candidate plus a legacy
file.

If you prefer Codex to own the credential instead, both of these also work:

```toml
[mcp_servers.jev.env]                    # inline value
TYPESAFE_API_KEY = "ts-…"
JEV_REQUIRE_TYPESAFE = "1"
```

```toml
[mcp_servers.jev]                        # or forward from the shell that launches Codex
command = "python3"
args = ["<repo>/mcp/jev/server.py"]
env_vars = ["TYPESAFE_API_KEY"]
```

`env_vars` is Codex's documented "allow and forward" list — verified: the value arrives in the
spawned server's environment while unlisted variables do not.

#### Tool calls are approval-gated, and only a config-declared server can opt out

Codex refuses an MCP tool call it cannot get approval for. In `codex exec` (policy `never`) every
call fails with:

```text
mcp: jev/jev_health (failed)
MCP tool call requires approval, but approval policy is never
```

`default_tools_approval_mode = "approve"` (values `auto | prompt | writes | approve`) lifts that per
server, or `[mcp_servers.jev.tools.<tool>] approval_mode = "approve"` per tool. Two things measured
on 0.153.4:

- Setting it on the **plugin-provided** table
  (`[plugins."jev-cost-guard@jev-cost-guard".mcp_servers.jev]`) has **no effect** — the call is still
  refused. Declare the server in `config.toml` instead.
- A **config-declared** server does **not** receive `PLUGIN_DATA`, so it cannot find the key file
  above. Give it the credential explicitly, or it silently falls back to the keyless Zen relay:

```toml
[plugins."jev-cost-guard@jev-cost-guard".mcp_servers.jev]
enabled = false                       # otherwise the plugin's copy and this one both exist

[mcp_servers.jev]
command = "python3"
args = ["<repo>/mcp/jev/server.py"]
default_tools_approval_mode = "approve"

[mcp_servers.jev.env]
JEV_API_KEY_FILE = "<the path --key-status printed>"
# or: TYPESAFE_API_KEY = "ts-…"
```

Verified end to end, with no approval and no bypass flag:

```text
[jev] ok: model=jev-1.13.0 endpoint=https://api.typesafe.ai/v1/systemone -> alive
```

#### The PostToolUse gate: the only trigger the model cannot skip

Nothing makes a model *choose* to call jev — measured: 62 sessions, zero `jev_*` calls, with the
skills advertised and an explicit `AGENTS.md` instruction in place. A hook is different: Codex runs
it whether or not the model wants it.

The hook script matches `Bash|exec|apply_patch|Edit|Write`, so it sees Codex's shell calls (`exec`
maps to `Bash`) and its edits. When an output exceeds `JEV_VERDICT_THRESHOLD_CHARS` (4000), it pipes
the text to `server.py --verdict` and injects one developer line; the original output is untouched,
and the hook always exits 0:

```text
[jev] Bash: 4100 chars judged, noul=0.1 -> likely-failure
      (model=jev-1.13.0, endpoint=https://api.typesafe.ai/v1/systemone).
      Advisory only — the tool output above is unchanged; …
```

That line arrives as `hooks.additional_context`, so it costs one jev request per large output and
never replaces the payload.

**In Codex the gate has to be declared, not bundled.** Claude Code discovers the plugin's own
`hooks/hooks.json`; Codex 0.153.4 does **not** (measured — `/hooks` lists no `jev-cost-guard` entry,
and nothing fires, for all three forms: the default `hooks/hooks.json`, a `hooks` entry in the root
`plugin.json`, and one in `.claude-plugin/plugin.json`). A hook declared in a config layer *does*
run, so install it there, pointing at the installed copy:

```toml
# ~/.codex/config.toml  (or ~/.codex/hooks.json with the JSON equivalent)
[[hooks.PostToolUse]]
matcher = "Bash|exec|apply_patch|Edit|Write"

[[hooks.PostToolUse.hooks]]
type = "command"
command = 'python3 "$HOME/.codex/plugins/cache/jev-cost-guard/jev-cost-guard/<version>/hooks/claude-codex-jev-verdict.py"'
statusMessage = "jev: judging tool output"
timeout = 30
```

**Three things are required for it to run:**

1. **Trust.** Installing or enabling a plugin does not trust its hooks, and a config-layer hook needs
   the same review — Codex records trust against the hook's hash and skips it until you accept it.
   Run `/hooks` once; any edit to the command, matcher, or script requires re-trusting. Automation
   that already vets its sources can pass `--dangerously-bypass-hook-trust`.
2. **A credential the hook can see.** Hooks do not inherit the declared server's `env` block, and
   Codex scrubs the subprocess environment, so the hook falls back to the Codex-owned file
   `$CODEX_HOME/jev/api_key`. Keep that file, or set `TYPESAFE_API_KEY` / `JEV_API_KEY_FILE` in the
   environment that launches Codex. A keyless hook still answers — on `jev-1.13-free` against
   `opencode.ai/zen`, which is why the line prints its endpoint.
3. **One owner.** Define the gate in exactly one config layer. Two copies both fire, and a duplicate
   doubles the jev requests per large output.

> Codex now also sets `CLAUDE_PLUGIN_ROOT` for plugin hooks, but the earlier bundled form still never
> registered, so the config-layer route above is the supported one.

To use the OpenCode Zen relay instead, opt out explicitly:

```sh
export JEV_PROVIDER=opencode          # or OPENCODE_ZEN_API_KEY=... for a paid Zen key
```

The switch is host-aware: Codex sets `PLUGIN_ROOT` and not `CLAUDE_PLUGIN_ROOT`, so the
requirement turns on automatically for plugin-loaded servers. `JEV_REQUIRE_TYPESAFE=0|1`
overrides it either way.

> Plugin **skills, hooks and MCP tools** all load in Codex: the `jev-*` skills appear in its
> context, and the four tools reach the model as `mcp__jev__jev_health` / `…_jev_find` /
> `…_jev_summarize` / `…_jev_screen` (verified on 0.153.4 by listing them out of the model's own tool
> registry, not from `codex mcp list`). What actually keeps them unused is the approval gate above
> plus a missing key — not availability.
>
> The skills themselves never shell out to `hermes`: measurement, pruning and scratch dirs
> all have a Codex-native path (see `docs/harness-adapters.md`), so a Codex session reports an
> estimate from `scripts/prompt_budget.py` rather than a locked-out audit.

Manual registration without the plugin still works:

```sh
codex mcp add jev -- python3 <repo>/mcp/jev/server.py
```

Codex surfaces jev tools natively. For the **auto-gate**, the plugin already wires
`hooks/hooks.json`; a manual install can point a tool-result hook at
`hooks/claude-codex-jev-verdict.py` with the same env. Same soft-gate semantics: verdict
logged, exits 0.

## The pre-tool gate: a permission judgement, not a verdict

`PostToolUse` answers "was that output good?" after the bytes are already paid for.
`PreToolUse` answers a different question — **should this command run at all** — and it
can stop it. `hooks/pretool-gate.py` runs there on Claude Code, Codex and Antigravity, in
each host's own vocabulary, and it is **audit-only by default**.

Three tiers, cheapest first:

| tier | what it is | cost |
|---|---|---|
| 0 | deterministic belts (`RISK_BELTS`: recursive delete, history rewrite, `DROP TABLE`, `dd of=`, global install, publish/deploy, remote copy, network upload, pipe-to-shell, `sudo`, container/system wipe, credential read) | free — a routine command (`npm test`, `git status`, `rg`) never leaves this tier |
| 1 | one jev request, two questions: a `choice` risk call (allow / ask / deny) + a `noul` "destroys state outside the working tree?" cross-check | ~1 s, one request, only for commands tier 0 flagged |
| 2 | session-scoped cache (`session` + `cwd` in the key, 24 h pruning) + a 12 000-char state cap | an `ask`/`deny` is not re-asked in the same place; an `allow` is judged again every time, because a command string says nothing about what it will now delete — and an oversized command is answered locally, since judging a head and a tail is not judging the command |

Two invariants, both taken from published implementations rather than invented here:

- **Jev may only narrow.** A confident `allow` returns *nothing*, so the harness's own
  permission flow still applies. `allowMode: "passthrough"` is the default and
  `"allow"` — which would genuinely grant a permission — is opt-in.
- **A hard action needs a decisive answer.** Below the confidence bar (0.70) every answer
  degrades to `ask`; a `noul destructive` of 0.50 or more forces `ask` even when the
  choice said `allow`.

Audit mode judges for real and writes one line, and never touches the turn:

```json
{"gate":"pre_tool","host":"claude","tool":"Bash","decision":"ask","source":"jev",
 "belts":["recursive delete","delete without recycle"],"command_prefix":"rm -rf ./build …",
 "reason":"worth a human glance (100% confidence, 95% destructive).","mode":"audit",
 "answers":{"risk":{"choice":"ask","confidence":1.0},"destructive":{"noul":0.95}}}
```

Flip to enforcement in `hooks/gate-policy.json` (`"mode": "enforce"`), or per shell with
`JEV_GATE_MODE=enforce`. Enforcement then answers natively:

```text
Claude Code / Codex   {"hookSpecificOutput":{"hookEventName":"PreToolUse",
                       "permissionDecision":"ask","permissionDecisionReason":"jev: …"}}
Antigravity           {"decision":"ask","reason":"jev: …"}
```

Codex has no `ask` on `PreToolUse`, so set `JEV_GATE_HOST=codex` there: `ask` becomes a
`systemMessage` (pass through with the concern surfaced) and `deny` stays a denial.

Every failure path is silent and exits 0 — no key, timeout, unreadable server, bad JSON,
malformed answer — because a permission gate that blocks by accident costs more trust than
one that misses a case. State lives in `$TMPDIR/jev-gate` (`JEV_GATE_STATE_DIR`):
`decisions.jsonl` is the audit trail, `cache.json` the per-session memory.

**An approval is honoured for exactly the scope the human granted.** The hook payload does
not carry the answer to its own `ask`, but the transcript does: Claude Code records one
`permissionDecision` object per tool call, and it names the grant — measured across 5 000+
rows of real transcripts on this machine:

| recorded in the transcript | meaning | reused? |
|---|---|---|
| `{"source":"user_temporary","grantScope":"once"}` | yes, this once | **no** — the repeat asks again |
| `{"source":"user_temporary"}` | yes, and don't ask again this session | **yes** — scoped to `session` + `cwd` + command |
| `{"source":"user_permanent","ruleSource":"localSettings"}` | always allow (persisted) | **yes** |
| `{"source":"config","reasonType":"classifier"}` | a rule or classifier said yes; nobody was asked | **no** — no human, no grant |
| `{"decision":"reject", …}` | no | never — a refusal cannot be laundered |

So `hooks/lib/approvals.py` reads the transcript tail after the tool runs, and records a
grant only for `user_temporary` (without `grantScope: once`) or `user_permanent`, keyed by
`(session, cwd, command)`. Anything unrecognised — an unreadable transcript, a new source
name, a hook-issued approval — records nothing and the gate keeps asking, which is the
failure mode that costs a prompt instead of a permission.

`grantScope` is an internal field, not documented API: if a future release renames it the
gate degrades to always-asking rather than to always-allowing.

**Which harnesses can tell the difference** — measured, not assumed:

| host | pre-tool `ask` | grant scope readable | behaviour |
|---|---|---|---|
| **Claude Code** | `PreToolUse` | **yes** — `permissionDecision.source` / `grantScope` | one-shot is never remembered; session/always is honoured |
| **Codex** | `PreToolUse` | no — session rollouts carry `approval_policy`, not a per-call grant | the run is recorded without a scope (`PostToolUse`, verified live) |
| **Antigravity** | `PreToolUse` | no — its transcript has no permission rows | recorded without a scope (`jev-post-tool`, verified live) |
| **Hermes** | none — `pre_tool_call` can block or approve, it cannot *ask* | n/a | nothing to remember; its gates stay advisory |

`cache.approvalScope` decides what a scope-less record means. **`"session"`** (shipped):
any recorded run counts, so every host with an `ask` gate behaves the same — on Claude Code
one-shot grants are still excluded, because they are never recorded at all. **`"host"`**:
only a grant the harness actually named counts, so Codex and Antigravity keep asking after
every run. `cache.honorApprovals: false` turns the mechanism off everywhere, and in
`bypassPermissions` nothing is recorded regardless.

## The stop gate: does the turn match what the closing message says?

`hooks/stop-gate.py` asks one `noul` — *does this closing message claim completion the run
facts do not support?* — and it establishes the run facts locally first, because the
wording alone is worthless: a published calibration over 1 500 real stops put the
message-only rule at AUROC 0.50 (a coin flip), and 0.976 once the turn's run facts were in
front of the model.

Four checks, cheapest first, and only the last one costs a request:

| order | check | outcome |
|---|---|---|
| 1 | no edits this turn | allow the stop |
| 2 | a check **passed after the last edit** | allow — verified |
| 3 | the message claims nothing (`DONE_HINT`) | allow — not a completion claim |
| 4 | otherwise | one jev `noul`; block only if `noul ≥ 0.70` **and** confidence `≥ 0.70` |

A check is judged by its **output**, not by exit status: both hosts record a failing suite
with no error field, so `check_outcome()` looks for the runner's own summary line and lets
failure win over success (`"40 passed, 3 failed"` is a failure).

Guards, because a completion gate that loops is worse than one that misses: `stop_hook_active`
(Claude Code), `terminationReason: error|max_steps_exceeded` and `fullyIdle: false`
(Antigravity), a 60 s cooldown, and at most 3 blocks per session.

Two live `agy` sessions, audit mode:

```json
{"gate":"stop","host":"antigravity","run":{"file_changes":1,"checks":[]},
 "answers":{"unverified":{"noul":0.4}},"decision":null,
 "reason":"claim looks supported (40% unverified, 20% confidence)."}

{"gate":"stop","host":"antigravity","run":{"file_changes":1,"checks":[]},
 "answers":{"unverified":{"noul":0.93}},"decision":"block",
 "reason":"this message claims a completion the run facts do not show (93% likely, 86% confidence) …"}
```

The second session created a file, ran nothing, and closed with *"Done — all tests pass and
the build is green."* The confidence there is derived (`|2p−1|`), because the relay sent no
`confidence` field — which is why `confidence_of()` derives it instead of trusting it.

Enforcement speaks `{"decision":"block","reason":…}` on Claude Code / Codex and
`{"decision":"continue","reason":…}` on Antigravity, where the reason arrives as a system
message and the agent gets one more step to run the check it claimed.

> **Hermes has no equivalent.** `agent_loop_stopped` is an observer — its return value is
> ignored — so this gate degrades to advice there (`pre_llm_call` on the next turn), not to
> a block. The capability table in `docs/harness-adapters.md` says so per host instead of
> claiming parity.

### Antigravity

The Antigravity App, the IDE, and the CLI load this repo as a single plugin:
`plugin.json` + `mcp_config.json` + `skills/`. Antigravity is also the one host that
ships a plugin validator, so a checkout can be checked before installing:

```sh
agy plugin validate .        # ✔ skills: 8 processed, ✔ mcpServers: 1 processed
agy plugin install .         # or: agy plugin install <path>@<marketplace>
agy plugin list --json       # components + install path under ~/.gemini/config/plugins/
```

Two measured details drive the file layout, both verified on `agy` 1.2.12:

- **the plugin MCP file is `mcp_config.json`, not `mcp.json`.** A checkout carrying
  only the portable `mcp.json` validates as `mcpServers: skipped (not found)`;
  renaming it makes the same server load (`✔ mcpServers: 1 processed`). Both files
  ship here and declare the same `jev` server — `mcp.json` for the portable
  (Hermes) path, `mcp_config.json` for Antigravity.
- **the root `plugin.json` is the manifest that CLI reads.** A manifest placed at
  `.antigravity-plugin/plugin.json` was ignored (an intentionally invalid `name`
  there produced no error), so the root manifest keeps the shared fields and
  `.antigravity-plugin/plugin.json` carries the App/IDE Marketplace metadata
  (`displayName`, `logo`, `suggestedPrompts`).

MCP tool names are namespaced by plugin, so the model sees
`jev-cost-guard_jev_jev_find` / `…_jev_summarize` / `…_jev_screen` / `…_jev_health`.
Skills appear as slash commands (`/jev-skill-gate`).

**The key.** Antigravity exports `PLUGIN_ROOT` and `PLUGIN_DATA` to every plugin MCP
server, so the credential file the server already supports is enough — no placeholder
expansion needed:

```sh
PLUGIN_DATA=<the plugin data directory> python3 <plugin-root>/mcp/jev/server.py --set-key
```

With no key at all the server stays on the keyless OpenCode Zen relay (`Bearer
public`, `jev-1.13-free`), so the tools answer before any key is configured.

Verified live in a `agy -p` session, not inferred from the install: the model reached
for the plugin's server as `call_mcp_tool` with `ServerName: "jev-cost-guard_jev"` and
`ToolName: "jev_health"`, and the tool answered

```text
[jev] ok: model=jev-1.13.0 endpoint=https://api.typesafe.ai/v1/systemone -> alive
```

#### Hooks run — a bundled `hooks.json` is enough

Measured with a probe plugin that logged every payload (`agy` 1.2.12, `agy -p`): all six
events fire, `SessionStart` → `PreInvocation` → `PreToolUse` → `PostToolUse` →
`PostInvocation` → `Stop`, from a plugin's own `hooks.json` — no trust step, no
config-layer declaration, no `/hooks` review. That is the opposite of Codex, where a
plugin-bundled hook never registers.

What each payload carries (real keys, nothing omitted):

| event | keys |
|---|---|
| `SessionStart` | `conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName` |
| `PreToolUse` | `toolCall{name,args}`, `stepIdx`, + common |
| `PostToolUse` | `toolCall{name,args}`, `stepIdx`, `error`, `lastUserInput`, + common |
| `PreInvocation`/`PostInvocation` | `invocationNum`, `initialNumSteps`, + common |
| `Stop` | `executionNum`, `terminationReason`, `fullyIdle`, `error`, + common |

Two traps found the hard way:

- **`PreToolUse` must answer `{"decision": …}`.** Returning `{}` is treated as a **deny**
  — the run failed with `tool call denied by pre-tool hook` and the model reported the
  blocked command instead.
- **Tool names are not the documented ones.** The CLI sent `name: "read_file"` with
  `args.Path` (the App/IDE vocabulary in the docs is `view_file` with `args.AbsolutePath`)
  and MCP through `name: "call_mcp_tool"` with `args.ServerName` + `args.ToolName`. A
  matcher written from the docs alone therefore misses calls; use `.*` or cover both.

#### The verdict gate — it ships, on `PreInvocation`

`PostToolUse` fires after the tool and carries **no output** (only `toolCall{name,args}`,
`error`, `lastUserInput`), so the Claude/Codex gate cannot be ported verbatim. The tool
result does exist in the transcript the event points at, so the gate runs one step
earlier and stays additive:

```text
hooks.json  jev-verdict → PreInvocation → python3 ./hooks/antigravity-jev-verdict.py
```

1. read `transcriptPath`, collect `GENERIC` tool-result rows newer than the last judged
   step (a `GENERIC` row counts only when the model turn before it carried `tool_calls`,
   so model prose is never judged),
2. pick the **single largest** one above `JEV_VERDICT_THRESHOLD_CHARS` (4000),
3. ask jev via the one-shot `mcp/jev/server.py --verdict` CLI,
4. inject one line as `injectSteps[].ephemeralMessage` — the tool output is already in
   the transcript and is never replaced, rewritten, or blocked.

One jev call per model invocation at most; the judged step is remembered per
conversation (under `$TMPDIR/jev-agy-verdict`, or `JEV_AGY_STATE_DIR`), so a turn with
several results does not re-bill. jev's own MCP results (`call_mcp_tool` on a
`jev…` server) are never judged. Every failure path — no transcript, unreadable row,
server error, timeout — prints nothing and exits 0.

Measured in a live `agy -p` session (`seq 1 3000` as the tool output) — the model quoted
the injected line back verbatim, proving it reached context:

```text
[jev] run_command: 10316 chars judged, noul=0.68 -> likely-success
      (model=jev-1.13.0, endpoint=https://api.typesafe.ai/v1/systemone). Advisory only — …
```

The transcript is an internal Antigravity format (it already renamed once, from
`transcript.jsonl` to `transcript_full.jsonl`), so treat this gate as best-effort: if a
release changes the row shape, the hook silently stops judging and nothing else breaks.
The `PreToolUse`/`Stop` gates (permission, completion) remain unbuilt — they see enough
to decide and are the next natural step if you want a *hard* gate.

#### Optional: one skill hint per session (opt-in)

`hooks/antigravity-jev-route.py` is the Antigravity form of the Hermes
`pre_llm_call` router: on `PreInvocation` #0 it reads the last `USER_INPUT` from the
transcript, ranks the live roster (reusing `prompt_budget.py`'s scanner, so there is one
roster implementation), and injects one line naming the best fit. Deterministic, no jev
call, one hint per conversation.

It ships **disabled**, because a per-session advisory line was measured to have ~0
effect on whether the model actually calls a tool — it must not spend anything by
default. Turn it on in `hooks.json`:

```json
"jev-skill-hint": { "enabled": true, "PreInvocation": [ … ] }
```

Verified with the flag on: a real session whose request was *"my test output is enormous
and I cannot find the failures"* got ``[jev] Skill hint: `jev-test-output-triage` matches
this request on 7 term(s)`` and quoted it back.

#### Install without the plugin (one workspace)

Antigravity also reads workspace-scoped `.agents/` files, which needs no plugin at all.
The installer merges into whatever is already there (other servers and hook groups are
kept):

```sh
python3 scripts/install-antigravity.py                    # cwd as the workspace
python3 scripts/install-antigravity.py --workspace ~/app --no-hook
```

It writes `.agents/mcp_config.json` (the `jev` server, absolute path — `${PLUGIN_ROOT}`
only resolves inside an installed plugin) and `.agents/hooks.json` (the gate). Verified
with the plugin disabled: the workspace server answered `jev_health` and the workspace
gate injected its line.

#### What `agy plugin validate` warns about here, and why

```text
⚠ .mcp.json: never loaded: this is Claude Code's file name
⚠ hooks/hooks.json: never loaded: plugin hooks are read only from hooks.json at the plugin root
⚠ mcp.json: never loaded: mcp.json is read only for Agent Plugins plugins
⚠ plugin.json: ignored: .antigravity-plugin/plugin.json takes precedence
```

All four are expected: this repo serves four hosts from one tree. `.mcp.json` and
`hooks/hooks.json` are Claude Code's, `mcp.json` is Hermes/portable, and the Antigravity
reads are `mcp_config.json`, root `hooks.json`, and `.antigravity-plugin/plugin.json`.
Validation still passes, with `skills 8 · hooks 1 · mcpServers 1`.

**Measure the roster** with the portable estimator, which knows Antigravity's roots
and skips plugins `agy plugin disable` took out — local files only, no `agy` call:

```sh
python3 scripts/prompt_budget.py --harness antigravity
```

### Hook verification

```sh
printf '{"tool_name":"bash","tool_result":{"output":"%s"}}' "$(python3 -c "print('x'*8000)")" \
  | JEV_VERDICT_THRESHOLD_CHARS=1 python3 hooks/claude-codex-jev-verdict.py
```

Prints `[jev-gate] bash: ...` and exits 0 regardless of the jev outcome.

---

## Native skill-routing hook (optional companion: `native/skill-router`)

The portable plugin above cannot see *when the model is about to answer* — it
only reacts to tool results. To gate a model call **before** it is made (so a
natural prompt like *"tell me if the latest merged PR contains UI changes"*
routes to the `jev` tools without a manual `/jev` invocation), jev-cost-guard
ships an optional **native** Python entry under `native/skill-router/`. It
mirrors the [`typesafe-skill-router`](https://github.com/DECRUX9812/typesafe-skill-router)
pattern and is **shadow-first, fail-open** — it never rewrites a turn and never
breaks one.

### How it runs

1. It registers a host `pre_llm_call` hook. (A portable `plugin.json` cannot do
   this — `provides_hooks: [pre_llm_call]` requires a native `__init__.py` +
   `plugin.yaml`.)
2. On each fresh user turn it asks **jev** via this repo's
   `mcp/jev/server._systemone_post` (the provider/model is resolved once:
   `jev-1.13.0` on TypeSafe, keyless `jev-1.13-free` on the Zen relay — nothing
   is pinned in the hook) which installed skill, if any, fits — sending **only**
   the live skill roster (name + first 200 chars of each `description`, chunked
   at 240; shortlisted candidates also send the first 700 chars of their
   `SKILL.md`). No history, files, or credentials are ever sent.
3. If a skill fits the `gate` + `fits` thresholds (enforced in code), it logs
   the decision; in `live` mode it injects **one advisory line** naming the
   skill. The model still chooses to load it.

### Install / enable

```sh
# from this repo checkout
hermes -p dev plugins install "$(pwd)/native/skill-router"
hermes -p dev plugins enable jev-cost-guard-skill-router
hermes -p dev plugins config jev-cost-guard-skill-router set enabled=true mode=live
```

No key is required for the Zen free tier (`Bearer public`, `jev-1.13-free`);
`TYPESAFE_API_KEY` in the profile env switches to TypeSafe (`jev-1.13.0`). The
hook reuses `mcp/jev/server.py` located at the repo root, `JEV_SERVER_PATH`, or
an installed `jev-cost-guard` plugin; if none is found it stays inert
(fail-open) — it never ships a second HTTP client.

### Config

| key | default | meaning |
|---|---|---|
| `enabled` | `false` | master switch; nothing is sent anywhere until `true` |
| `mode` | `shadow` | `shadow` = decide + log, inject nothing; `live` = inject the suggestion line; `off` = idle |
| `gate` | `0.30` | mean of the gate Nouls (one inverted); below this, nothing is suggested |
| `fits` | `0.40` | winner's "does this skill do the specific thing asked" Noul; below this, no suggestion |
| `shortlist` | `3` | candidates whose `SKILL.md` excerpt is sent in the narrow request |
| `chunk` | `240` | skills per TypeSafe request (API caps a question at 255 options) |
| `excerpt` | `700` | first N chars of `SKILL.md` for shortlisted candidates |
| `suggest_chars` | `4000` | turns longer than this are left alone |
| `timeout` | `10.0` | TypeSafe wall-clock budget for one routing decision |
| `model` | _auto_ | empty = `mcp/jev/server._resolve_jev_target` picks `jev-1.13.0` (TypeSafe) or `jev-1.13-free` (keyless zen); jev is a decision model, never a summarizer |
| `roster_dir` | _auto_ | comma-separated skill dirs; empty = scan `<hermes_home>/skills`, `profiles/*/skills`, `plugins/*/skills` |
| `cache_path` | _off_ | JSON cache keyed by `mode|model|roster|turn` to skip repeat API calls |
| `log_path` | _off_ | one line per routed turn |

### Verify (no network)

```sh
cd native/skill-router
python -m pytest tests -q            # 9/9 offline probes (transport mocked)
hermes plugins validate .            # manifest parses; register() runs isolated
hermes -p dev plugins call jev-skill-router status
```

> The portable plugin (`plugin.json` + `skills/` + `mcp/`) is the **default** path
> and is unaffected by this native entry — keep both enabled only if you want
> prompt-time auto-routing in addition to the on-demand MCP tools.
