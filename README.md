# jev-cost-guard

Token-economy guard for agent harnesses — **Hermes-first, harness-agnostic**. It
attacks the two real token leak classes visible in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas + the skills index
   are the fat. → pruned/audited by `jev-skill-gate` / `jev-prompt-budget-audit`.
2. **Tool output bloats the transcript.** → auto-gated by **jev** (TypeSafe, or the
   OpenCode Zen free tier) through the
   `jev-cost-guard` hook plugin (replaces oversized results with a ~7-token `noul`
   verdict), or judged on-demand via the `mcp/jev` MCP.

> The deterministic head+tail *plugin* (`jev-context-guard`) was removed — it is not
> a jev-model feature. The model-based decision below is the only trimming lever.

---

## How to use

### Install the skills
```bash
# register the repo as a skill source (discovers every jev-* skill)
hermes skills tap add ronail/jev-cost-guard
# install + scan a single skill (fetches sibling references/ too)
hermes skills install ronail/jev-cost-guard/hermes/jev-context-economy --yes
```
`hermes skills tap add` is verified to register the source and list the `jev-*`
skills; `hermes skills install <owner/repo/path> --yes` then downloads, runs the
skills-guard scan, and installs the skill. Add the wanted skills to each profile's
`skills.auto_load` in `config.yaml` (see `SKILLS.md`).

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

### Mount the MCP (`mcp/jev`)
A zero-dependency (pure stdlib, no pip) MCP server exposing `jev_health` +
`jev_summarize` for **TypeSafe jev** (System One decision model). It talks to
either TypeSafe's own API or the OpenCode Zen relay directly — never the agent's
own provider — and uses **only jev models**.

```bash
hermes -p dev mcp add jev --command python3 --args <repo>/mcp/jev/server.py
hermes -p dev mcp list        # -> jev ... 2 tools  enabled
```

**Providers (auto-selected, same request shape).** With `TYPESAFE_API_KEY` set the
server calls TypeSafe directly — `https://api.typesafe.ai/v1/systemone`, model
`jev-latest` (also `jev-1.13.0` / `jev-1.14.0`) — using `Bearer $TYPESAFE_API_KEY`
and standard JSON headers; there is no free tier and no paid-fallback retry. With
no TypeSafe key it uses the OpenCode Zen relay: `JEV_MODEL` (default
`jev-1.13-free`) and `JEV_ENDPOINT` (default `https://opencode.ai/zen/v1/systemone`)
are optional, the key is **never** on the command line, and the MCP reads
`OPENCODE_ZEN_API_KEY` from `profiles/<profile>/.env` at runtime (Hermes does not
inject profile secrets into MCP subprocesses). Force either with
`JEV_PROVIDER=typesafe|opencode`. No browser/OAuth/device-flow. Set `JEV_DEBUG=1`
for redacted provider/key logs. Auth modes (real key, the relay free-tier
`Bearer public` no-key bearer, OpenCode desktop UA fingerprint, and failure modes)
are documented in `mcp/jev/env.example` and the server docstring.

| env | default | note |
|---|---|---|
| `JEV_PROVIDER` | *(auto)* | `typesafe` when `TYPESAFE_API_KEY` is set, else `opencode` (Zen relay); force either |
| `JEV_MODEL` | `jev-1.13-free` / `jev-latest` | optional — default is `jev-1.13-free` (opencode) or `jev-latest` (typesafe) |
| `JEV_ENDPOINT` | `https://opencode.ai/zen/v1/systemone` | optional — System One endpoint (typesafe default: `https://api.typesafe.ai/v1/systemone`) |
| `TYPESAFE_API_KEY` | (from `profiles/<profile>/.env`) | TypeSafe direct key — its presence selects `api.typesafe.ai` (`jev-latest`); no free tier, no fallback |
| `JEV_FALLBACK_TO_PAID` | `1` | `1` (default) on `jev-1.13-free` exhaustion (HTTP 429/403 with a real key) retry the paid model; `0` fails open; no-key never falls back |
| `JEV_FALLBACK_MODEL` | `jev-1.13` | paid model retried on free exhaustion (same key/endpoint/UA; bills your opencode account) |
| `JEV_DEBUG` | `0` | optional — `1` = print masked key prefix/suffix |
| `OPENCODE_ZEN_API_KEY` | (from `profiles/<profile>/.env`) | required for the real-key path |

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


> The repo also ships the **`jev-cost-guard` hook plugin**, which *automatically*
> gates oversized tool results (`transform_tool_result` → jev `noul` verdict, via the
> shared `server.py`) so bloat is trimmed without the agent asking. Install it
> inactive, tune first, then enable — see [jev-cost-guard plugin](#jev-cost-guard-plugin).

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
`profiles/dev/.env`), `Authorization: Bearer <key>`, desktop `User-Agent`, at
`https://opencode.ai/zen/v1/systemone`, model `jev-1.13-free` (primary; paid `jev-1.13`
is a fallback on free exhaustion — see `JEV_FALLBACK_TO_PAID`). Each row is one `jev_summarize` call: a `noul` "is the above
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
(`hermes/jev-mcp-model-server/templates/mcp-stdlib-skeleton.py` → `_ask`,
`Authorization: Bearer <key>` from `profiles/dev/.env`) — i.e. the same `noul` judgment
a dev profile drives through `jev_summarize` — the verdict the agent **persists** stays
constant as the body grows (the agent keeps just the `noul`, ~7 tokens, vs. re-sending
the whole body every later call; jev's full billed output is the ~24 in the table above):

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
| `mcp/jev/server.py` | the one jev implementation: MCP stdio server (dual-frame) + `--verdict` CLI; owns provider selection (TypeSafe / OpenCode Zen) and every fallback |
| `mcp/jev/mcp.json` | MCP manifest (provider autodetected; no hardcoded model/endpoint) |
| `mcp/jev/env.example` | env reference: TypeSafe + Zen auth, fallback knobs, failure modes |
| `hermes/` | the `jev-*` Hermes skills |
| `SKILLS.md` | index + per-profile wiring |
| `docs/token-savings.md` | token-accounting + cap-removal history |
| `LICENSE` | MIT |
| `plugin.yaml` | jev-cost-guard plugin manifest (repo-root) |
| `__init__.py` | jev-cost-guard hook: policy + delegation to `server.py --verdict` (~160 lines, fail-open) |

## jev-cost-guard plugin

An **opt-in** hook plugin (named to match the repo) that auto-gates oversized tool
results through **jev**, replacing the result in-transcript with a ~7-token `noul`
verdict so the body is not re-sent on later turns.

The hook contains **no jev client of its own** — it shells out to the same
implementation the MCP uses:

```
tool result → transform_tool_result → python3 mcp/jev/server.py --verdict → noul verdict
```

Provider selection (**TypeSafe** when `TYPESAFE_API_KEY` is set, else the OpenCode
Zen relay) and the entire fallback chain live in `mcp/jev/server.py`, shared with the
`jev_health` / `jev_summarize` MCP tools and the Claude Code / Codex hooks, so the
surfaces cannot drift apart. `__init__.py` is ~160 lines of policy + delegation.

Install (inactive — the hook does **not** fire until you enable it):

    hermes -p dev plugins install ronail/jev-cost-guard --no-deps

Enable (activates the hook — oversized results are REPLACED by the verdict, so the
original output is dropped from context):

    hermes -p dev plugins enable jev-cost-guard

Plugin tunables (env or `profiles/<profile>/.env`):

| var | default | meaning |
|---|---|---|
| `JEV_GATE_TOOLS` | *(empty)* | **allowlist — the only tools gated.** Empty ⇒ the hook is inert. Default-deny, because a verdict *replaces* the result: never list data-retrieval tools (`read_file`, `search_files`, `web_search`, `web_extract`, `browser_exec`) — only diagnostic-noise tools (`terminal`, batch runs) |
| `JEV_THRESHOLD` | `1000` | gate allowlisted results > N tokens (~4N chars) |
| `JEV_SKIP_TOOLS` | *(empty)* | optional second layer: deny these even when allowlisted |
| `JEV_DEBUG` | unset | any non-empty value logs per-call diagnostics (e.g. `verbose` — **not** `1`: MCP/config env values must be strings) |
| `JEV_SERVER_PATH` | `<plugin>/mcp/jev/server.py` | override the delegated server |
| `JEV_VERDICT_TIMEOUT` | `75` | subprocess timeout in seconds |

Tunables resolve from the process environment first, then `profiles/<profile>/.env`,
then their defaults (Hermes does not inject `.env` into the plugin host).
Provider/fallback knobs are **not** plugin settings — they belong to the server and
therefore apply to every surface: `TYPESAFE_API_KEY`, `OPENCODE_ZEN_API_KEY`,
`JEV_PROVIDER`, `JEV_MODEL`, `JEV_ENDPOINT`, `JEV_FALLBACK_TO_PAID`,
`JEV_FALLBACK_MODEL` (documented in `mcp/jev/env.example`).

**Fail-open** by construction: missing server, non-zero exit, unparseable JSON, or a
non-`ok` verdict all leave the original result untouched. The hook skips `jev_health`
/ `jev_summarize` and any already-judged `[jev]` verdict (re-entry guard).

> ⚠️ Enabling alone does nothing: the hook is **default-deny**, so until you set
> `JEV_GATE_TOOLS` it never fires. A gated result loses its body permanently, so
> start with a single noise-only tool (e.g. `JEV_GATE_TOOLS=terminal`) and a high
> `JEV_THRESHOLD`.

> 💸 Cost: with `TYPESAFE_API_KEY` set the hook bills TypeSafe on **every** gate.
> Use `JEV_PROVIDER=opencode` (or unset the TypeSafe key) to prefer the free Zen tier,
> and raise `JEV_THRESHOLD` to gate less often.

---

## Using jev in Claude Code / Codex (portable MCP)

The `mcp/jev/server.py` stdio server is **dual-frame**: it answers `Content-Length`
requests (Claude Code, Codex — strict MCP clients) and newline-delimited JSON
(Hermes' client), so the **same server loads in every harness** with no fork.
Expose `OPENCODE_ZEN_API_KEY` (from `profiles/dev/.env`, never committed) in the
harness env, then register `python3 <repo>/mcp/jev/server.py` as an MCP server.
jev stays the **only** oracle model; Claude Code/Codex are pure clients that call
`jev_health` / `jev_summarize` like any other tool.

### Claude Code

Add the server + hook to your Claude Code config (`~/.claude/.claude.json` or the
legacy `~/.claude/claude_desktop_config.json`) — `mcpServers` and `hooks` go in the
same file:

```json
{
  "mcpServers": {
    "jev": {
      "command": "python3",
      "args": ["/Users/ronail/Projects/jev-cost-guard/mcp/jev/server.py"],
      "env": {
        "TYPESAFE_API_KEY": "<your TypeSafe key; from profiles/dev/.env, never commit>"
      }
    }
  },
  "hooks": {
    "PostToolUse": [
      {
        "command": "python3",
        "args": ["/Users/ronail/Projects/jev-cost-guard/hooks/claude-codex-jev-verdict.py"],
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

Register the server with the verified CLI (writes `~/.codex/config.toml`):

```sh
codex mcp add jev -- python3 /Users/ronail/Projects/jev-cost-guard/mcp/jev/server.py \
  --env TYPESAFE_API_KEY=<your TypeSafe key>
```

(Use `--env OPENCODE_ZEN_API_KEY=<key>` instead to run on the Zen relay.)

Codex surfaces jev tools natively. For the **auto-gate**, add a tool-result hook
in `~/.codex/config.toml` that runs `hooks/claude-codex-jev-verdict.py` with the
same env (check `codex --help` for the exact `hooks` schema on your version). Same
soft-gate semantics: verdict logged, exits 0.

### Hook verification

```sh
printf '{"tool_name":"bash","tool_result":{"output":"%s"}}' "$(python3 -c "print('x'*8000)")" \
  | JEV_VERDICT_THRESHOLD_CHARS=1 python3 hooks/claude-codex-jev-verdict.py
```

Prints `[jev-gate] bash: ...` and exits 0 regardless of the jev outcome.
