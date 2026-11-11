import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "hooks"))
from lib import langsmith_tracer


class TestLangSmithTracerDiscovery(unittest.TestCase):
    def test_discover_credentials_from_env(self):
        env = {
            "LANGSMITH_API_KEY": "lsv2_test_key",
            "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com",
            "LANGSMITH_PROJECT": "custom-project",
        }
        creds = langsmith_tracer.discover_credentials(env=env)
        self.assertEqual(
            creds,
            ("lsv2_test_key", "https://eu.api.smith.langchain.com", "custom-project"),
        )

    def test_discover_credentials_langchain_fallback(self):
        env = {
            "LANGCHAIN_API_KEY": "lc_test_key",
            "LANGCHAIN_PROJECT": "lc-project",
        }
        creds = langsmith_tracer.discover_credentials(env=env)
        self.assertEqual(
            creds,
            ("lc_test_key", "https://api.smith.langchain.com", "lc-project"),
        )

    def test_discover_credentials_missing_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            creds = langsmith_tracer.discover_credentials(env={})
            self.assertIsNone(creds)

    def test_discover_credentials_whitespace_only_returns_none(self):
        env = {"LANGSMITH_API_KEY": "   "}
        creds = langsmith_tracer.discover_credentials(env=env)
        self.assertIsNone(creds)

    def test_detect_harness_claude_code(self):
        event = {"session_id": "sess_123"}
        env = {"CLAUDE_PLUGIN_ROOT": "/some/path"}
        info = langsmith_tracer.detect_harness(event, env=env)
        self.assertEqual(info["ls_integration"], "claude-code")
        self.assertEqual(info["session_id"], "sess_123")
        self.assertEqual(info["thread_id"], "sess_123")
        self.assertEqual(info["ls_trace_schema_version"], "coding-agent-v1")
        self.assertEqual(info["ls_agent_purpose"], "coding")

    def test_detect_harness_codex(self):
        event = {"session_id": "codex_456"}
        env = {"CODEX_HOME": "/some/codex"}
        info = langsmith_tracer.detect_harness(event, env=env)
        self.assertEqual(info["ls_integration"], "openai-codex")
        self.assertEqual(info["session_id"], "codex_456")

    def test_detect_harness_opencode(self):
        event = {"sessionId": "open_789"}
        env = {"OPENCODE_ZEN_API_KEY": "zen_key"}
        info = langsmith_tracer.detect_harness(event, env=env)
        self.assertEqual(info["ls_integration"], "opencode")
        self.assertEqual(info["session_id"], "open_789")

    def test_detect_harness_cursor(self):
        event = {"thread_id": "cur_999"}
        env = {"CURSOR_SESSION_ID": "cur_999"}
        info = langsmith_tracer.detect_harness(event, env=env)
        self.assertEqual(info["ls_integration"], "cursor")
        self.assertEqual(info["session_id"], "cur_999")

    def test_detect_harness_antigravity(self):
        event = {"conversationId": "agy_789", "toolCall": {"name": "bash"}}
        info = langsmith_tracer.detect_harness(event, env={})
        self.assertEqual(info["ls_integration"], "claude-code")
        self.assertEqual(info["ls_agent_runtime"], "Antigravity")
        self.assertEqual(info["session_id"], "agy_789")


class TestLangSmithTracerLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.env = {
            "LANGSMITH_API_KEY": "lsv2_test_key",
            "LANGSMITH_PROJECT": "test-project",
            "JEV_LANGSMITH_STATE_DIR": self.tmp_dir,
            "CLAUDE_PLUGIN_ROOT": "/path/to/plugin",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_fail_open_when_uninstalled(self):
        with patch.object(langsmith_tracer, "langsmith", None):
            tracer = langsmith_tracer.LangSmithTracer(event={"session_id": "s1"}, env=self.env)
            self.assertFalse(tracer.enabled)
            tracer.record_pretool("bash", "echo hello", [], "allow", 0.9)
            tracer.record_posttool("bash", {"command": "echo hello"}, "hello\n", None)
            tracer.record_stop_and_flush("All done", {}, "allow")

    def test_lifecycle_and_batch_flush_with_mock_sdk(self):
        mock_client = MagicMock()
        mock_run_tree_cls = MagicMock()
        mock_root_run = MagicMock()
        mock_child_run = MagicMock()
        mock_root_run.create_child.return_value = mock_child_run
        mock_run_tree_cls.return_value = mock_root_run

        with patch.object(langsmith_tracer, "langsmith", MagicMock()), \
             patch.object(langsmith_tracer, "RunTree", mock_run_tree_cls), \
             patch.object(langsmith_tracer, "Client", MagicMock(return_value=mock_client)):

            event = {"session_id": "sess_turn_1", "last_user_input": "Run tests and check git status"}
            tracer = langsmith_tracer.LangSmithTracer(event=event, env=self.env)
            self.assertTrue(tracer.enabled)

            # 1. PreTool 1: bash
            tracer.record_pretool(
                tool_name="bash",
                command="pytest tests/",
                reasons=["process kill"],
                decision="allow",
                confidence=0.85,
            )

            # 2. PostTool 1: bash
            tracer.record_posttool(
                tool_name="bash",
                tool_input={"command": "pytest tests/"},
                tool_output="10 passed in 0.5s",
                verdict={"noul": 0.05, "advisory": "tests passed"},
            )

            # 3. PreTool 2: git status (multi-tool in same turn)
            tracer.record_pretool(
                tool_name="bash",
                command="git status",
                reasons=[],
                decision="allow",
                confidence=0.99,
            )

            # 4. PostTool 2: git status
            tracer.record_posttool(
                tool_name="bash",
                tool_input={"command": "git status"},
                tool_output="clean",
                verdict=None,
            )

            # 5. Stop Gate: finish turn
            tracer.record_stop_and_flush(
                message="Completed test and status check",
                run_facts={"edits": 0, "verified": True},
                decision="allow",
            )

            mock_run_tree_cls.assert_called_once()
            call_kwargs = mock_run_tree_cls.call_args[1]
            self.assertEqual(call_kwargs["run_type"], "chain")
            self.assertEqual(call_kwargs["name"], "Claude Code Turn")
            self.assertEqual(
                call_kwargs["extra"]["metadata"]["ls_trace_schema_version"],
                "coding-agent-v1",
            )
            self.assertEqual(
                call_kwargs["extra"]["metadata"]["ls_agent_type"],
                "root",
            )
            self.assertEqual(
                call_kwargs["extra"]["metadata"]["ls_integration"],
                "claude-code",
            )

            child_calls = mock_root_run.create_child.call_args_list
            created_names = [c[1]["name"] for c in child_calls]
            self.assertIn("jev:pretool-gate", created_names)
            self.assertIn("jev:verdict", created_names)
            self.assertIn("jev:stop-gate", created_names)
            self.assertIn("bash", created_names)

            for c in child_calls:
                if str(c[1]["name"]).startswith("jev:"):
                    self.assertEqual(
                        c[1]["extra"]["metadata"]["ls_agent_type"],
                        "middleware",
                    )

            mock_root_run.post.assert_called_once_with(exclude_child_runs=False)
            mock_client.flush.assert_called_once()

    def test_multi_turn_session_simulation(self):
        mock_client = MagicMock()
        mock_run_tree_cls = MagicMock()
        mock_root_runs = []

        def create_mock_root(**kwargs):
            m = MagicMock()
            m.kwargs = kwargs
            m.child_runs = []
            m.create_child.side_effect = lambda **ckwargs: m.child_runs.append(ckwargs)
            mock_root_runs.append(m)
            return m

        mock_run_tree_cls.side_effect = create_mock_root

        with patch.object(langsmith_tracer, "langsmith", MagicMock()), \
             patch.object(langsmith_tracer, "RunTree", mock_run_tree_cls), \
             patch.object(langsmith_tracer, "Client", MagicMock(return_value=mock_client)):

            shared_session_id = "session_xyz_777"

            # --- TURN 1 ---
            event_turn1 = {
                "session_id": shared_session_id,
                "last_user_input": "First user instruction: fix the bug",
            }
            tracer1 = langsmith_tracer.LangSmithTracer(event=event_turn1, env=self.env)
            tracer1.record_pretool("bash", "git diff", [], "allow", 1.0)
            tracer1.record_posttool("bash", {"command": "git diff"}, "+ fixed bug", None)
            tracer1.record_stop_and_flush("Fixed bug in module", {"file_changes": 1, "verified": True}, "allow")

            # Verify Turn 1 posted
            self.assertEqual(len(mock_root_runs), 1)
            turn1_run = mock_root_runs[0]
            self.assertEqual(turn1_run.kwargs["extra"]["metadata"]["thread_id"], shared_session_id)
            self.assertEqual(turn1_run.kwargs["inputs"]["messages"][0]["content"], "First user instruction: fix the bug")

            # --- TURN 2 --- (Same session, new user prompt)
            event_turn2 = {
                "session_id": shared_session_id,
                "last_user_input": "Second user instruction: add documentation",
            }
            tracer2 = langsmith_tracer.LangSmithTracer(event=event_turn2, env=self.env)
            tracer2.record_pretool("read_file", "", [], "allow", 1.0)
            tracer2.record_posttool("read_file", {"path": "README.md"}, "docs content", None)
            tracer2.record_stop_and_flush("Docs added.", {"file_changes": 1, "verified": True}, "allow")

            # Verify Turn 2 posted
            self.assertEqual(len(mock_root_runs), 2)
            turn2_run = mock_root_runs[1]
            self.assertEqual(turn2_run.kwargs["extra"]["metadata"]["thread_id"], shared_session_id)
            self.assertEqual(turn2_run.kwargs["inputs"]["messages"][0]["content"], "Second user instruction: add documentation")

            # Ensure Turn 2 did not leak Turn 1's tools
            turn2_tool_names = [c["name"] for c in turn2_run.child_runs if c["run_type"] == "tool" and not c["name"].startswith("jev:")]
            self.assertEqual(turn2_tool_names, ["read_file"])
            self.assertNotIn("bash", turn2_tool_names)

            # Both runs called post and flush
            self.assertEqual(mock_client.flush.call_count, 2)

    def test_record_posttool_deduplication_on_verdict(self):
        tracer = langsmith_tracer.LangSmithTracer(
            event={"session_id": "dedup_sess", "last_user_input": "run tests"},
            env=self.env,
        )
        tracer.enabled = True
        # PreTool records bash
        tracer.record_pretool("bash", "pytest", [], "allow", 1.0)

        # First posttool: tool completes before verdict check
        tracer.record_posttool("bash", {"command": "pytest"}, "5 passed", verdict=None)

        # Second posttool: verdict computed and attached
        tracer.record_posttool(
            "bash",
            {"command": "pytest"},
            "5 passed",
            verdict={"noul": 0.02, "interpretation": "success"},
        )

        state = tracer._load_state()
        tools = state["active_turn"]["tools"]
        # There should only be 1 tool record, NOT 2 duplicate records
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["outputs"], {"output": "5 passed"})
        # Middleware should have pretool and verdict
        mw = state["active_turn"]["middleware"]
        self.assertEqual(len(mw), 2)
        self.assertEqual(mw[0]["name"], "jev:pretool-gate")
        self.assertEqual(mw[1]["name"], "jev:verdict")

    def test_extract_prompt_with_transcript_turn_slice(self):
        transcript_file = os.path.join(self.tmp_dir, "transcript.jsonl")
        with open(transcript_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "USER_INPUT", "content": "Please implement feature X"}) + "\n")
            f.write(json.dumps({"type": "PLANNER_RESPONSE", "content": "Working on it"}) + "\n")

        event = {"session_id": "trans_sess", "transcript_path": transcript_file}
        tracer = langsmith_tracer.LangSmithTracer(event=event, env=self.env)
        prompt = tracer._extract_prompt()
        self.assertEqual(prompt, "Please implement feature X")

    def test_finalize_turn_error_resilience(self):
        mock_client = MagicMock()
        mock_root_run = MagicMock()
        mock_root_run.post.side_effect = RuntimeError("Network timeout or 500 error from LangSmith")

        with patch.object(langsmith_tracer, "langsmith", MagicMock()), \
             patch.object(langsmith_tracer, "RunTree", MagicMock(return_value=mock_root_run)), \
             patch.object(langsmith_tracer, "Client", MagicMock(return_value=mock_client)):

            tracer = langsmith_tracer.LangSmithTracer(
                event={"session_id": "err_sess", "last_user_input": "test err"},
                env=self.env,
            )
            tracer.record_pretool("bash", "ls", [], "allow", 1.0)
            # This triggers _finalize_turn which will raise RuntimeError inside post()
            tracer.record_stop_and_flush("done", {}, "allow")

            # Verify that active_turn in state was STILL cleared, so session is not permanently poisoned
            state = tracer._load_state()
            self.assertIsNone(state["active_turn"])


class TestFailOpenPerformance(unittest.TestCase):
    def test_unconfigured_overhead_under_one_millisecond(self):
        # When langsmith is unconfigured, 1000 calls must average < 1ms (1000us) each
        event = {"session_id": "bench_sess", "last_user_input": "test bench"}
        empty_env = {}

        start = time.perf_counter()
        iterations = 1000
        for _ in range(iterations):
            tracer = langsmith_tracer.LangSmithTracer(event=event, env=empty_env)
            tracer.record_pretool("bash", "echo 1", [], "allow", 1.0)
            tracer.record_posttool("bash", {}, "1", None)
            tracer.record_stop_and_flush("done", {}, "allow")
        elapsed = time.perf_counter() - start

        avg_ms_per_call = (elapsed / iterations) * 1000.0
        # Assert average execution time is well under 1ms
        self.assertLess(avg_ms_per_call, 1.0)


class TestHookSubprocessIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.env = dict(os.environ)
        self.env.update({
            "JEV_GATE_MODE": "audit",
            "JEV_GATE_STATE_DIR": self.tmp_dir,
            "JEV_LANGSMITH_STATE_DIR": self.tmp_dir,
            "LANGSMITH_API_KEY": "lsv2_subproc_test",
            "PYTHONPATH": os.path.join(REPO_ROOT, "hooks"),
        })

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_pretool_gate_subprocess_runs_cleanly(self):
        payload = json.dumps({
            "session_id": "test_subproc_session",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "cwd": REPO_ROOT,
        })
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "hooks", "pretool-gate.py")],
            input=payload,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(proc.returncode, 0)

    def test_posttool_verdict_subprocess_runs_cleanly(self):
        payload = json.dumps({
            "session_id": "test_subproc_session",
            "tool_name": "Bash",
            "tool_input": {"command": "echo test"},
            "tool_result": {"output": "small output"},
        })
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "hooks", "claude-codex-jev-verdict.py")],
            input=payload,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(proc.returncode, 0)

    def test_stop_gate_subprocess_runs_cleanly(self):
        payload = json.dumps({
            "session_id": "test_subproc_session",
            "last_assistant_message": "All done and tests pass.",
        })
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "hooks", "stop-gate.py")],
            input=payload,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
