# jev-cost-guard

Token-economy guard for agent harnesses — **Hermes-first, harness-agnostic**. It
attacks the two real token leak classes visible in `hermes prompt-size`:

1. **The fixed prompt is re-sent on every API call.** Tool schemas + the skills index
   are the fat. → pruned/audited by `jev-skill-gate` / `jev-prompt-budget-audit`.
2. **Tool output bloats the transcript.** → flagged by the `jev-1.13-free` model via
   the `mcp/jev` MCP server.

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
| **`jev-mcp-model-server`** | usage guide: how/when to drive `jev-1.13-free` via this MCP |

### Mount the MCP (`mcp/jev`)
A zero-dependency (pure stdlib, no pip) MCP server exposing `jev_health` +
`jev_summarize`. It talks to the opencode-zen relay directly — not the agent's own
provider — and uses **only `jev-1.13-free`**.

```bash
hermes -p dev mcp add jev --command python3 --args <repo>/mcp/jev/server.py
hermes -p dev mcp list        # -> jev ... 2 tools  enabled
```

`JEV_MODEL` (default `jev-1.13-free`) and `JEV_ENDPOINT` (default
`https://opencode.ai/zen/v1/systemone`) are **optional** — the server defaults them.
The bearer key is **never** passed on the command line: the MCP reads
`OPENCODE_ZEN_API_KEY` from `profiles/<profile>/.env` at runtime (Hermes does not
inject profile secrets into MCP subprocesses). No browser/OAuth/device-flow. Set
`JEV_DEBUG=1` for masked-key logs (prefix+suffix only). Auth modes (real key from
`.env` or the relay free-tier `Bearer public` no-key bearer; OpenCode desktop UA +
failure modes) are documented in `mcp/jev/env.example` and the server docstring.

| env | default | note |
|---|---|---|
| `JEV_MODEL` | `jev-1.13-free` | optional — the only model in scope (no fallback) |
| `JEV_ENDPOINT` | `https://opencode.ai/zen/v1/systemone` | optional — System One endpoint |
| `JEV_DEBUG` | `0` | optional — `1` = print masked key prefix/suffix |
| `OPENCODE_ZEN_API_KEY` | (from `profiles/<profile>/.env`) | required for the real-key path |

### `jev_health` / `jev_summarize`
`jev_health` probes the model with a `noul` reachability question (alive iff it
answers). `jev_summarize` does **not** generate prose — it asks jev a binary `noul`
question over a result `state` and returns a **success probability**, which is what
you `budget_tokens`-gate on. jev is a decision model, not a summarizer.

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
`https://opencode.ai/zen/v1/systemone`, model `jev-1.13-free` (jev is the only model in
scope — no fallback). Each row is one `jev_summarize` call: a `noul` "is the above
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
| `mcp/jev/server.py` | the MCP server (stdlib, jev-1.13-free only; auth/failure modes in its docstring) |
| `mcp/jev/mcp.json` | MCP manifest (defaults `JEV_MODEL`/`JEV_ENDPOINT`) |
| `mcp/jev/env.example` | env reference (real key from `.env` + `Bearer public` no-key bearer + failure modes) |
| `hermes/` | the `jev-*` Hermes skills |
| `SKILLS.md` | index + per-profile wiring |
| `docs/token-savings.md` | token-accounting + cap-removal history |
| `LICENSE` | MIT |
