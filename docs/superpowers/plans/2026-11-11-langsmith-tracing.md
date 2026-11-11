# LangSmith Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement native, multi-harness LangSmith tracing across coding agents (Claude Code, OpenAI Codex, OpenCode, Cursor, Antigravity) conforming to `coding-agent-v1` and the Trajectory View Integrations contract, with strict fail-open semantics (<1ms no-op when unconfigured).

**Architecture:** A lightweight, harness-agnostic library `hooks/lib/langsmith_tracer.py` manages credential discovery, harness detection, and cross-process turn state in `~/.cache/jev/langsmith_state/<session_id>.json`. Hook scripts (`pretool-gate.py`, `claude-codex-jev-verdict.py`, `antigravity-jev-verdict.py`, and `stop-gate.py`) invoke tracer methods to record user prompts, tool executions, model calls, and jev middleware checks (`ls_agent_type: "middleware"`), batch-posting the root `RunTree` (`exclude_child_runs=False`) and flushing on turn completion.

**Tech Stack:** Python 3 standard library (`os`, `json`, `sys`, `time`, `hashlib`, `pathlib`, `unittest`), optional `langsmith` SDK (`RunTree`, `Client`), mock testing.

**Spec:** `docs/superpowers/specs/2026-11-11-langsmith-tracing-design.md`

## Global Constraints
- `ls_trace_schema_version`: `"coding-agent-v1"`
- `ls_agent_purpose`: `"coding"`
- `ls_integration`: `"claude-code"`, `"openai-codex"`, `"opencode"`, `"cursor"`, fallback `"claude-code"`
- `ls_agent_type`: `"middleware"` on all jev lifecycle checks (`jev:pretool-gate`, `jev:verdict`, `jev:stop-gate`)
- Strict fail-open: If `langsmith` is not installed or API key is absent, return immediately in $<1\text{ms}$; never raise exceptions or block hook exit code 0.
- Safe batch posting: `root_run.post(exclude_child_runs=False)` and `client.flush()` on finalization.

## Review Focus
1. **Uninstalled `langsmith` module**: Tracer must immediately no-op without attempting network or file operations.
2. **Missing or empty API key**: Credential discovery returns `None` and tracer no-ops without error.
3. **Multi-tool sequential turns**: Subsequent `pretool-gate` and `posttool` events in the same turn must append to the existing turn tree rather than spawning disconnected root runs.
4. **Early exit in `stop-gate.py`**: Tracing finalization must execute before early `skip()` or `allow` returns.
5. **Stale/Interrupted session recovery**: If an agent crashed or was aborted before `stop-gate`, the next `pretool-gate` must flush the stale turn before initializing the new turn.

---

### Task 1: Credential Discovery & Harness Detection in `hooks/lib/langsmith_tracer.py`

**Files:**
- Create: `hooks/lib/langsmith_tracer.py`
- Create: `tests/test_langsmith_tracer.py`

**Interfaces:**
- Produces:
  - `discover_credentials(env: dict | None = None) -> tuple[str, str, str] | None`
  - `detect_harness(event: dict, env: dict | None = None) -> dict`
  - `is_available() -> bool`

- [ ] **Step 1: Write the failing tests for credential resolution and harness detection**

```python
# tests/test_langsmith_tracer.py
import os
import unittest
from unittest.mock import patch, mock_open
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))
from lib import langsmith_tracer

class TestLangSmithTracerDiscovery(unittest.TestCase):
    def test_discover_credentials_from_env(self):
        env = {
            "LANGSMITH_API_KEY": "lsv2_test_key",
            "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com",
            "LANGSMITH_PROJECT": "custom-project",
        }
        creds = langsmith_tracer.discover_credentials(env=env)
        self.assertEqual(creds, ("lsv2_test_key", "https://eu.api.smith.langchain.com", "custom-project"))

    def test_discover_credentials_missing_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            creds = langsmith_tracer.discover_credentials(env={})
            self.assertIsNone(creds)

    def test_detect_harness_claude_code(self):
        event = {"session_id": "sess_123"}
        env = {"CLAUDE_PLUGIN_ROOT": "/some/path"}
        info = langsmith_tracer.detect_harness(event, env=env)
        self.assertEqual(info["ls_integration"], "claude-code")
        self.assertEqual(info["session_id"], "sess_123")
        self.assertEqual(info["ls_trace_schema_version"], "coding-agent-v1")

    def test_detect_harness_codex(self):
        event = {"session_id": "codex_456"}
        env = {"CODEX_HOME": "/some/codex"}
        info = langsmith_tracer.detect_harness(event, env=env)
        self.assertEqual(info["ls_integration"], "openai-codex")

    def test_detect_harness_antigravity(self):
        event = {"conversationId": "agy_789", "toolCall": {"name": "bash"}}
        info = langsmith_tracer.detect_harness(event, env={})
        self.assertEqual(info["ls_integration"], "claude-code")
        self.assertEqual(info["ls_agent_runtime"], "Antigravity")
        self.assertEqual(info["session_id"], "agy_789")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_langsmith_tracer.py`
Expected: FAIL (module `langsmith_tracer` not found or functions missing)

- [ ] **Step 3: Implement discovery, harness detection, and fail-open guard in `hooks/lib/langsmith_tracer.py`**

Implement `is_available()`, `discover_credentials()`, and `detect_harness()` following the spec's precedence order and metadata fields.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_langsmith_tracer.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/langsmith_tracer.py tests/test_langsmith_tracer.py
git commit -m "feat(tracing): add credential discovery and harness detection"
```

---

### Task 2: State Ledger & RunTree Construction in `hooks/lib/langsmith_tracer.py`

**Files:**
- Modify: `hooks/lib/langsmith_tracer.py`
- Modify: `tests/test_langsmith_tracer.py`

**Interfaces:**
- Consumes: `discover_credentials`, `detect_harness`, `is_available`
- Produces:
  - `LangSmithTracer` class
  - `tracer.record_pretool(tool_name: str, command: str, reasons: list[str], decision: str, confidence: float | None)`
  - `tracer.record_posttool(tool_name: str, tool_input: dict, tool_output: str, verdict: dict | None)`
  - `tracer.record_stop_and_flush(message: str, run_facts: dict, decision: str)`

- [ ] **Step 1: Write the failing tests for state ledger and tree assembly**

Test:
- Serialization and reconstitution of `RunTree` across simulated hook invocations.
- `ls_agent_type: "middleware"` on `jev:pretool-gate`, `jev:verdict`, `jev:stop-gate`.
- `root_run.post(exclude_child_runs=False)` and `client.flush()` called upon `record_stop_and_flush`.
- Multi-tool calls in a single turn grouped as siblings under the same root turn.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_langsmith_tracer.py`
Expected: FAIL (methods not yet implemented)

- [ ] **Step 3: Implement `LangSmithTracer` lifecycle recording in `hooks/lib/langsmith_tracer.py`**

Implement turn initialization, child span creation (`llm`, `tool`, middleware `tool`), state persistence in `~/.cache/jev/langsmith_state/<session_id>.json`, and safe finalization with batch posting and flushing.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_langsmith_tracer.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hooks/lib/langsmith_tracer.py tests/test_langsmith_tracer.py
git commit -m "feat(tracing): implement LangSmithTracer state ledger and tree assembly"
```

---

### Task 3: Hook Integrations (`pretool-gate.py`, `claude-codex-jev-verdict.py`, `antigravity-jev-verdict.py`, `stop-gate.py`)

**Files:**
- Modify: `hooks/pretool-gate.py`
- Modify: `hooks/claude-codex-jev-verdict.py`
- Modify: `hooks/antigravity-jev-verdict.py`
- Modify: `hooks/stop-gate.py`
- Modify: `tests/test_langsmith_tracer.py`

**Interfaces:**
- Consumes: `LangSmithTracer` from `hooks/lib/langsmith_tracer.py`

- [ ] **Step 1: Write integration tests verifying hooks call tracer without crashing on fail-open**

Test running `pretool-gate.py`, `claude-codex-jev-verdict.py`, and `stop-gate.py` via subprocess with various payloads, with `LANGSMITH_API_KEY` present and absent, confirming exit code 0 and valid JSON output.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_langsmith_tracer.py`
Expected: FAIL

- [ ] **Step 3: Integrate tracer into all lifecycle hooks**

- In `hooks/pretool-gate.py`: import `langsmith_tracer` safely; record pretool decision and tool input.
- In `hooks/claude-codex-jev-verdict.py`: record posttool output and verdict advisory.
- In `hooks/antigravity-jev-verdict.py`: record verdict advisory in Antigravity PreInvocation.
- In `hooks/stop-gate.py`: in a `finally` block before early-exit or final exit, record stop-gate middleware decision, post root run with `exclude_child_runs=False`, and flush client.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_langsmith_tracer.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hooks/ tests/test_langsmith_tracer.py
git commit -m "feat(tracing): integrate LangSmith tracing into all lifecycle hooks"
```

---

### Task 4: End-to-End Verification with Live / Mocked LangSmith Session

**Files:**
- Modify: `tests/test_langsmith_tracer.py`

- [ ] **Step 1: Write end-to-end integration test simulating a full coding agent session**

Simulate:
1. Turn 1: user prompt $\rightarrow$ pretool risk check $\rightarrow$ bash execution $\rightarrow$ posttool verdict $\rightarrow$ stop verification $\rightarrow$ trace posted.
2. Turn 2: new prompt in same session $\rightarrow$ read_file execution $\rightarrow$ stop verification $\rightarrow$ trace posted with same `thread_id`.
Verify thread continuity, metadata compliance, and clean state rotation.

- [ ] **Step 2: Run test to verify it passes**

Run: `python3 -m unittest tests/test_langsmith_tracer.py`
Expected: PASS

- [ ] **Step 3: Verify fail-open performance (<1ms when langsmith unconfigured)**

Run benchmark script verifying overhead is $<1\text{ms}$ when `langsmith` is unconfigured.

- [ ] **Step 4: Commit**

```bash
git add tests/test_langsmith_tracer.py
git commit -m "test(tracing): add full session lifecycle and fail-open performance tests"
```
