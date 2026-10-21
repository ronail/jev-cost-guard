# opencode-zen auth for jev-1.13-free (System One) — verified

jev-1.13-free is a **System One DECISION model** at
`https://opencode.ai/zen/v1/systemone` (NOT `/chat/completions` — that 400s
`ModelProtocolUnsupported`). Body: `{"model","state","questions"}` with
`noul`/`choice`/`score` question types. Response: `{"model","answers":{...},
"usage":{input_tokens,output_tokens}}` (~21 output tokens), NOT SSE.

## Auth matrix (all verified live, Oct 2026)

| condition | Authorization | result |
|---|---|---|
| real key | `Bearer <OPENCODE_ZEN_API_KEY>` | HTTP 200 |
| **no key (free tier)** | `Bearer public` | **HTTP 200** (jev free tier) |
| truly anonymous | (no Authorization header) | 403 FreeTierError |
| real key, no OpenCode UA | `Bearer <key>` | 403 error code 1010 |
| wrong path `/inference/systemone/v1/systemone` | any | 403 error code 1010 |
| OpenAI `/chat/completions` shape for jev | any | 400 ModelProtocolUnsupported |

## Required fingerprint (either UA works with `Bearer public`)

| header | value |
|---|---|
| x-opencode-project | `global` (validated) |
| User-Agent | `opencode/1.18.31` (desktop) OR `opencode/latest/2.0.29/cli` (cli) |
| x-opencode-client | `desktop` OR `cli` (match the UA) |
| Content-Type | application/json |

Session headers (`x-opencode-session` = `ses_<12hex><14base62>`, mirrored across
`x-opencode-session-id`/`x-session-affinity`/`x-session-id`) are **optional** for
`/systemone` — verified 200 with and without them.

## What NOT to do

- Do NOT use `/chat/completions` (OpenAI `messages`/`choices`) for jev — rejected
  with `ModelProtocolUnsupported`. jev is a System One decision model and only
  speaks the `state`/`questions` JSON protocol (no `choices`/`delta`).
- Do NOT send "no Authorization" for the no-key case — send `Bearer public`.
- Do NOT assume SSE for `/systemone`; the body is plain JSON with `answers`.
