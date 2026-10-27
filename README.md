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
| `skills/` | the `jev-*` Hermes skills (auto-discovered, one SKILL.md per dir) |
| `mcp/jev/server.py` | the one jev implementation: MCP stdio server (dual-frame) + `--verdict` CLI; owns provider selection (TypeSafe / OpenCode Zen) and every fallback |
| `mcp/jev/env.example` | env reference: TypeSafe + Zen auth, fallback knobs, failure modes |
| `hooks/claude-codex-jev-verdict.py` | standalone soft-gate hook for Claude Code / Codex |
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

## Using jev in Claude Code / Codex (portable MCP)

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

Two caveats for the desktop app: re-saving the server from that page can drop hand-added tables
(re-paste them if the key stops working), and GUI-launched apps do not read your shell profile, so
`env_vars` forwarding is unreliable there — set the value inline. Verify with `codex mcp get jev`.

**The TypeSafe key is required.** Codex has no plugin configuration dialog — its binary
carries no `userConfig`, `CLAUDE_PLUGIN_OPTION_*`, or `pluginConfigs` — so the requirement is
enforced by the server at runtime instead. Without a key every jev tool returns:

```text
TYPESAFE_API_KEY is required by this plugin but is not set. Provide it in the environment …
```

Codex also **scrubs** the environment it hands to stdio MCP servers: a server spawned by
`codex exec` sees only `HOME`, `PATH`, `SHELL`, `TMPDIR`, `USER`, `LOGNAME`, `LC_CTYPE`,
`__CF_USER_TEXT_ENCODING`. A bare `export TYPESAFE_API_KEY=…` therefore never reaches jev. The
key has to be declared for the server in `~/.codex/config.toml`:

```toml
[mcp_servers.jev]
command = "python3"
args = ["<repo>/mcp/jev/server.py"]
env_vars = ["TYPESAFE_API_KEY"]          # forward from Codex's own environment

[mcp_servers.jev.env]
JEV_REQUIRE_TYPESAFE = "1"
```

Then export the key in whatever launches Codex (`export TYPESAFE_API_KEY=ts-…`), or skip
`env_vars` and set the value inline:

```toml
[mcp_servers.jev.env]
TYPESAFE_API_KEY = "ts-…"
JEV_REQUIRE_TYPESAFE = "1"
```

`env_vars` is Codex's documented "allow and forward" list — verified: the value arrives in the
spawned server's environment, while unlisted variables do not. `codex mcp get jev` shows the
resolved command and the forwarded names.

To use the OpenCode Zen relay instead, opt out explicitly:

```sh
export JEV_PROVIDER=opencode          # or OPENCODE_ZEN_API_KEY=... for a paid Zen key
```

The switch is host-aware: Codex sets `PLUGIN_ROOT` and not `CLAUDE_PLUGIN_ROOT`, so the
requirement turns on automatically for plugin-loaded servers. `JEV_REQUIRE_TYPESAFE=0|1`
overrides it either way.

> Plugin **skills and hooks** load in Codex (the `jev-*` skills appear in its context), but
> `codex exec` 0.153.4 does not surface stdio MCP tools in the model's tool list — even for an
> explicitly registered server. Register the server as above and check `codex mcp list` /
> `codex mcp get jev`; the tools themselves are host-dependent.

Manual registration without the plugin still works:

```sh
codex mcp add jev -- python3 <repo>/mcp/jev/server.py
```

Codex surfaces jev tools natively. For the **auto-gate**, the plugin already wires
`hooks/hooks.json`; a manual install can point a tool-result hook at
`hooks/claude-codex-jev-verdict.py` with the same env. Same soft-gate semantics: verdict
logged, exits 0.

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
