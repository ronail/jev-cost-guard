"""LangSmith tracing integration for AI coding agent harnesses.

Complies with:
- LangSmith Trajectory View Integrations contract
- Coding Agent Metadata Contract (coding-agent-v1)

Strict fail-open: If `langsmith` is not installed or API key is not configured,
no-ops immediately within <1ms. Network/payload errors are caught silently.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

# Fail-open import check
try:
    import langsmith  # type: ignore
    from langsmith.run_trees import RunTree  # type: ignore
    from langsmith import Client  # type: ignore
except ImportError:
    langsmith = None
    RunTree = None
    Client = None

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS_DIR = os.path.dirname(HERE)
PLUGIN_ROOT = os.path.dirname(HOOKS_DIR)

DEFAULT_ENDPOINT = "https://api.smith.langchain.com"
DEFAULT_PROJECT = "coding-agents"
IDLE_TIMEOUT_SECONDS = 300.0


def _read_json_file(path: str) -> dict:
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def discover_credentials(
    env: dict[str, str] | None = None,
) -> tuple[str, str, str] | None:
    """Discover LangSmith credentials in strict precedence order:

    1. Environment variables (LANGSMITH_* or LANGCHAIN_*)
    2. Global/User config: ~/.config/langsmith/config.json
    3. Harness-local configs: ~/.claude/langsmith.json, ~/.codex/langsmith.json, etc.
    4. Plugin-local config: config.json at plugin root

    Returns (api_key, endpoint, project) or None if no API key is found.
    """
    active_env = os.environ if env is None else env

    raw_key = active_env.get("LANGSMITH_API_KEY") or active_env.get("LANGCHAIN_API_KEY")
    api_key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None

    raw_ep = active_env.get("LANGSMITH_ENDPOINT") or active_env.get("LANGCHAIN_ENDPOINT")
    endpoint = raw_ep.strip() if isinstance(raw_ep, str) and raw_ep.strip() else None

    raw_proj = active_env.get("LANGSMITH_PROJECT") or active_env.get("LANGCHAIN_PROJECT")
    project = raw_proj.strip() if isinstance(raw_proj, str) and raw_proj.strip() else None

    # If API key not in env, check config files in order
    if not api_key:
        candidate_paths = [
            "~/.config/langsmith/config.json",
            "~/.claude/langsmith.json",
            "~/.codex/langsmith.json",
            "~/.opencode/langsmith.json",
            os.path.join(PLUGIN_ROOT, "config.json"),
        ]
        for path in candidate_paths:
            cfg = _read_json_file(path)
            key = (
                cfg.get("api_key")
                or cfg.get("LANGSMITH_API_KEY")
                or cfg.get("LANGCHAIN_API_KEY")
            )
            if key and isinstance(key, str) and key.strip():
                api_key = key.strip()
                if not endpoint:
                    ep = (
                        cfg.get("endpoint")
                        or cfg.get("api_url")
                        or cfg.get("LANGSMITH_ENDPOINT")
                        or cfg.get("LANGCHAIN_ENDPOINT")
                    )
                    endpoint = ep.strip() if isinstance(ep, str) and ep.strip() else None
                if not project:
                    proj = (
                        cfg.get("project")
                        or cfg.get("LANGSMITH_PROJECT")
                        or cfg.get("LANGCHAIN_PROJECT")
                    )
                    project = proj.strip() if isinstance(proj, str) and proj.strip() else None
                break

    if not api_key:
        return None

    final_endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
    final_project = project or DEFAULT_PROJECT
    return api_key, final_endpoint, final_project


def is_available(env: dict[str, str] | None = None) -> bool:
    """Return True only if langsmith SDK is installed and credentials exist."""
    if langsmith is None:
        return False
    return discover_credentials(env=env) is not None


def detect_harness(
    event: dict[str, Any], env: dict[str, str] | None = None
) -> dict[str, str]:
    """Detect the active coding agent host and return standard metadata."""
    active_env = os.environ if env is None else env

    # 1. Session / Thread ID
    session_id = (
        event.get("session_id")
        or event.get("sessionId")
        or event.get("conversationId")
        or event.get("thread_id")
        or active_env.get("CLAUDE_CONVERSATION_ID")
        or active_env.get("CODEX_SESSION_ID")
        or active_env.get("OPENCODE_SESSION_ID")
        or active_env.get("CURSOR_SESSION_ID")
        or active_env.get("AGY_CONVERSATION_ID")
        or "unknown-session"
    )

    # 2. Host detection
    gate_host = active_env.get("JEV_GATE_HOST", "").lower()
    if (
        "toolCall" in event
        or "conversationId" in event
        or active_env.get("AGY_CONVERSATION_ID")
        or active_env.get("ANTIGRAVITY_STATE_DIR")
    ):
        host = "antigravity"
        integration = "claude-code"  # Trajectory View parser compatibility fallback
        runtime = "Antigravity"
    elif active_env.get("CODEX_HOME") or "codex" in gate_host:
        host = "openai-codex"
        integration = "openai-codex"
        runtime = "OpenAI Codex"
    elif (
        active_env.get("OPENCODE_ZEN_API_KEY")
        or active_env.get("OPENCODE_SESSION_ID")
        or "opencode" in gate_host
    ):
        host = "opencode"
        integration = "opencode"
        runtime = "OpenCode"
    elif active_env.get("CURSOR_SESSION_ID") or "cursor" in gate_host:
        host = "cursor"
        integration = "cursor"
        runtime = "Cursor"
    elif active_env.get("CLAUDE_PLUGIN_ROOT") or "claude" in gate_host:
        host = "claude-code"
        integration = "claude-code"
        runtime = "Claude Code"
    else:
        host = "claude-code"
        integration = "claude-code"
        runtime = "Coding Agent"

    return {
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "ls_integration": integration,
        "ls_agent_runtime": runtime,
        "ls_agent_purpose": "coding",
        "ls_trace_schema_version": "coding-agent-v1",
        "host": host,
    }


class LangSmithTracer:
    """Manages the session state ledger and emits LangSmith traces."""

    def __init__(
        self,
        event: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.env = os.environ if env is None else env
        self.event = event or {}
        self.enabled = False

        try:
            self.meta = detect_harness(self.event, self.env)
            self.session_id = self.meta["session_id"]
            self.state_dir = self.env.get("JEV_LANGSMITH_STATE_DIR") or os.path.expanduser(
                "~/.cache/jev/langsmith_state"
            )

            if langsmith is None:
                return

            creds = discover_credentials(self.env)
            if creds is None:
                return

            self.api_key, self.endpoint, self.project = creds
            self.client = Client(api_url=self.endpoint, api_key=self.api_key)
            self.enabled = True
        except Exception:
            self.enabled = False

    def _state_file(self) -> str:
        os.makedirs(self.state_dir, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.session_id)
        return os.path.join(self.state_dir, f"{safe_id}.json")

    def _load_state(self) -> dict:
        path = self._state_file()
        if not os.path.exists(path):
            return {"session_id": self.session_id, "active_turn": None, "completed_turns": 0}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {"session_id": self.session_id, "active_turn": None, "completed_turns": 0}
        except Exception:
            return {"session_id": self.session_id, "active_turn": None, "completed_turns": 0}

    def _save_state(self, state: dict) -> None:
        path = self._state_file()
        tmp_path = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _extract_prompt(self) -> str:
        prompt = (
            self.event.get("last_user_input")
            or self.event.get("prompt")
            or self.event.get("lastUserInput")
        )
        if not prompt or not isinstance(prompt, str) or not prompt.strip():
            transcript_path = self.event.get("transcript_path") or self.event.get("transcriptPath")
            if transcript_path and os.path.exists(transcript_path):
                try:
                    from lib import transcript  # type: ignore
                    task, _ = transcript.turn_slice(transcript.read_events(transcript_path))
                    if task and isinstance(task, str) and task.strip():
                        prompt = task.strip()
                except Exception:
                    pass

        if prompt and isinstance(prompt, str) and prompt.strip():
            text = prompt.strip()
            if "<USER_REQUEST>" in text:
                import re
                m = re.search(r"<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|$)", text, re.DOTALL)
                if m and m.group(1).strip():
                    text = m.group(1).strip()
            return text

        return f"Turn in {self.meta['ls_agent_runtime']}"

    def _get_active_turn(self, state: dict, now: float) -> dict:
        active = state.get("active_turn")
        # Check staleness against updated_at (fallback created_at)
        if active and (now - active.get("updated_at", active.get("created_at", 0)) > IDLE_TIMEOUT_SECONDS):
            try:
                self._finalize_turn(active)
            except Exception:
                pass
            active = None

        if not active:
            active = {
                "turn_id": uuid.uuid4().hex,
                "prompt": self._extract_prompt(),
                "created_at": now,
                "updated_at": now,
                "tools": [],
                "middleware": [],
            }
            state["active_turn"] = active
        else:
            active["updated_at"] = now
        return active

    def record_pretool(
        self,
        tool_name: str,
        command: str = "",
        reasons: list[str] | None = None,
        decision: str = "allow",
        confidence: float | None = None,
    ) -> None:
        """Record pre-tool risk check (middleware) and pending tool invocation."""
        if not self.enabled:
            return
        try:
            state = self._load_state()
            now = time.time()
            active = self._get_active_turn(state, now)

            # Append middleware
            active["middleware"].append({
                "name": "jev:pretool-gate",
                "inputs": {
                    "tool_name": tool_name,
                    "command": command,
                    "reasons": reasons or [],
                },
                "outputs": {
                    "decision": decision,
                    "confidence": confidence,
                },
            })

            # Append pending tool
            tool_input = {"command": command} if command else {}
            active["tools"].append({
                "name": tool_name,
                "inputs": tool_input,
                "outputs": None,
            })

            self._save_state(state)
        except Exception:
            pass

    def record_posttool(
        self,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        tool_output: str = "",
        verdict: dict[str, Any] | None = None,
    ) -> None:
        """Record tool output and output verdict check (middleware)."""
        if not self.enabled:
            return
        try:
            state = self._load_state()
            now = time.time()
            active = self._get_active_turn(state, now)

            # Match pending tool or update already matched tool without duplicating
            found = False
            for t in reversed(active["tools"]):
                if t.get("name") == tool_name:
                    if t.get("outputs") is None:
                        t["outputs"] = {"output": tool_output}
                        if tool_input and not t.get("inputs"):
                            t["inputs"] = tool_input
                        found = True
                        break
                    elif t.get("outputs") == {"output": tool_output}:
                        found = True  # Already recorded in this hook
                        break
            if not found:
                active["tools"].append({
                    "name": tool_name,
                    "inputs": tool_input or {},
                    "outputs": {"output": tool_output},
                })

            # Record verdict if present
            if verdict:
                active["middleware"].append({
                    "name": "jev:verdict",
                    "inputs": {
                        "tool_name": tool_name,
                        "output_length": len(tool_output),
                    },
                    "outputs": verdict,
                })

            self._save_state(state)
        except Exception:
            pass

    def record_stop_and_flush(
        self,
        message: str = "",
        run_facts: dict[str, Any] | None = None,
        decision: str | None = None,
    ) -> None:
        """Record stop gate (middleware), finalize root RunTree, post batch, and flush."""
        if not self.enabled:
            return
        try:
            state = self._load_state()
            active = state.get("active_turn")
            if not active:
                return

            if decision is not None:
                active["middleware"].append({
                    "name": "jev:stop-gate",
                    "inputs": {
                        "message": message,
                        "run_facts": run_facts or {},
                    },
                    "outputs": {
                        "decision": decision,
                    },
                })

            try:
                self._finalize_turn(active, closing_message=message)
            finally:
                state["active_turn"] = None
                state["completed_turns"] = state.get("completed_turns", 0) + 1
                self._save_state(state)
        except Exception:
            pass

    def _finalize_turn(
        self, active_turn: dict[str, Any], closing_message: str = ""
    ) -> None:
        """Construct the RunTree hierarchy and submit batch."""
        if RunTree is None or not self.enabled:
            return

        prompt = active_turn.get("prompt") or f"Turn in {self.meta['ls_agent_runtime']}"
        harness_title = self.meta["ls_agent_runtime"]
        tools = active_turn.get("tools") or []

        # If closing_message is empty, try to get last assistant text from transcript if available
        if not closing_message:
            transcript_path = self.event.get("transcript_path") or self.event.get("transcriptPath")
            if transcript_path and os.path.exists(transcript_path):
                try:
                    from lib import transcript  # type: ignore
                    facts = transcript.turn_facts(transcript.read_events(transcript_path))
                    if facts.get("final_message"):
                        closing_message = facts["final_message"]
                except Exception:
                    pass

        assistant_text = closing_message or "Turn completed."

        tool_calls = []
        for i, t in enumerate(tools):
            tool_calls.append({
                "id": f"call_{i}_{t.get('name', 'tool')}",
                "type": "function",
                "function": {
                    "name": t.get("name", "tool"),
                    "arguments": json.dumps(t.get("inputs") or {}),
                },
            })

        root_inputs = {
            "messages": [{"role": "user", "content": prompt}],
            "input": prompt,
        }
        root_outputs = {
            "messages": [
                {
                    "role": "assistant",
                    "content": assistant_text,
                    "tool_calls": tool_calls,
                }
            ],
            "output": assistant_text,
        }

        # 1. Root Turn Run (chain)
        root_run = RunTree(
            name=f"{harness_title} Turn",
            run_type="chain",
            inputs=root_inputs,
            outputs=root_outputs,
            extra={
                "metadata": {
                    "thread_id": self.session_id,
                    "session_id": self.session_id,
                    "ls_agent_type": "root",
                    "ls_integration": self.meta["ls_integration"],
                    "ls_trace_schema_version": self.meta["ls_trace_schema_version"],
                    "ls_agent_purpose": self.meta["ls_agent_purpose"],
                    "ls_agent_runtime": self.meta["ls_agent_runtime"],
                }
            },
            project_name=self.project,
            client=self.client,
        )

        # 2. Child LLM Run (llm)
        llm_run = root_run.create_child(
            name="Model Invocation",
            run_type="llm",
            inputs={"messages": [{"role": "user", "content": prompt}]},
            outputs={
                "messages": [
                    {
                        "role": "assistant",
                        "content": assistant_text,
                        "tool_calls": tool_calls,
                    }
                ]
            },
            extra={
                "metadata": {
                    "ls_message_format": "completions",
                    "ls_provider": "anthropic",
                    "ls_model_name": self.meta["ls_agent_runtime"],
                }
            },
        )
        if hasattr(llm_run, "end"):
            try:
                llm_run.end()
            except Exception:
                pass

        # 3. Child Tool Runs (tool)
        for i, t in enumerate(tools):
            child_tool = root_run.create_child(
                name=t.get("name") or "tool",
                run_type="tool",
                inputs=t.get("inputs") or {},
                outputs=t.get("outputs") or {"output": ""},
                extra={
                    "metadata": {
                        "thread_id": self.session_id,
                        "ls_tool_name": t.get("name") or "tool",
                        "tool_call_id": f"call_{i}_{t.get('name', 'tool')}",
                    }
                },
            )
            if hasattr(child_tool, "end"):
                try:
                    child_tool.end(outputs=t.get("outputs") or {"output": ""})
                except Exception:
                    pass

        # 4. Child Middleware Runs (tool with ls_agent_type: "middleware")
        for mw in active_turn.get("middleware") or []:
            child_mw = root_run.create_child(
                name=mw.get("name") or "jev:middleware",
                run_type="tool",
                inputs=mw.get("inputs") or {},
                outputs=mw.get("outputs") or {},
                extra={
                    "metadata": {
                        "thread_id": self.session_id,
                        "ls_agent_type": "middleware",
                    }
                },
            )
            if hasattr(child_mw, "end"):
                try:
                    child_mw.end(outputs=mw.get("outputs") or {})
                except Exception:
                    pass

        if hasattr(root_run, "end"):
            try:
                root_run.end(outputs=root_outputs)
            except Exception:
                pass

        # 5. Batch Post & Flush
        root_run.post(exclude_child_runs=False)
        self.client.flush()
