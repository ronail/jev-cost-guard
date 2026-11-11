#!/usr/bin/env python3
"""Backfill complete conversation transcripts into LangSmith threads.

Reads an Antigravity, Claude Code, or Codex transcript JSONL file, groups it into
clean conversational turns, and posts full RunTrees with inputs, outputs, model
invocations, tool runs, and Trajectory View metadata to LangSmith.

Usage:
    python3 scripts/backfill_langsmith.py
    python3 scripts/backfill_langsmith.py --transcript <path> --session-id <id>
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "hooks"))

from lib import langsmith_tracer


def parse_iso(ts_str: str | None) -> datetime:
    if not ts_str:
        return datetime.now(timezone.utc)
    try:
        # e.g. "2026-11-11T04:05:47Z"
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str)
    except Exception:
        return datetime.now(timezone.utc)


def clean_user_prompt(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|$)", text, re.DOTALL)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return text.strip()


def parse_transcript_turns(transcript_path: str) -> list[dict]:
    with open(os.path.expanduser(transcript_path), "r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    turns = []
    current_turn = None

    for r in rows:
        rtype = r.get("type")
        created_at = r.get("created_at")
        if rtype in ("USER_INPUT", "USER_MESSAGE", "USER"):
            if current_turn:
                turns.append(current_turn)
            current_turn = {
                "prompt": clean_user_prompt(str(r.get("content") or "")),
                "start_time": created_at,
                "end_time": created_at,
                "tool_calls": [],
                "assistant_messages": [],
            }
        elif current_turn:
            current_turn["end_time"] = created_at
            if rtype in ("PLANNER_RESPONSE", "MODEL", "ASSISTANT"):
                calls = r.get("tool_calls") or []
                for c in calls:
                    current_turn["tool_calls"].append({
                        "name": str(c.get("name") or "tool"),
                        "args": c.get("args") if isinstance(c.get("args"), dict) else {},
                        "output": "",
                        "time": created_at,
                    })
                text = r.get("content")
                if text and str(text).strip():
                    current_turn["assistant_messages"].append(str(text).strip())
            elif rtype in ("GENERIC", "TOOL_RESULT", "TOOL_OUTPUT"):
                if current_turn["tool_calls"]:
                    current_turn["tool_calls"][-1]["output"] = str(r.get("content") or "")

    if current_turn:
        turns.append(current_turn)

    return turns


def backfill(transcript_path: str, session_id: str, project: str | None = None) -> int:
    try:
        from langsmith.run_trees import RunTree  # type: ignore
        from langsmith import Client  # type: ignore
    except ImportError:
        print("ERROR: `langsmith` package is required to backfill. Run with python that has langsmith installed.", file=sys.stderr)
        return 1

    creds = langsmith_tracer.discover_credentials()
    if not creds:
        print("ERROR: No LangSmith credentials found in environment or configs.", file=sys.stderr)
        return 1

    api_key, endpoint, default_project = creds
    project_name = project or default_project
    client = Client(api_url=endpoint, api_key=api_key)

    print(f"Connecting to LangSmith at {endpoint} (Project: {project_name})")
    print(f"Reading transcript: {transcript_path}")
    print(f"Session / Thread ID: {session_id}")

    turns = parse_transcript_turns(transcript_path)
    print(f"Found {len(turns)} conversational turns in transcript.")

    posted_count = 0
    for idx, turn in enumerate(turns):
        prompt = turn["prompt"] or f"Turn {idx + 1}"
        start_dt = parse_iso(turn["start_time"])
        end_dt = parse_iso(turn["end_time"])

        final_msg = turn["assistant_messages"][-1] if turn["assistant_messages"] else "Turn completed."
        tools = turn["tool_calls"]

        tool_calls_schema = []
        for i, t in enumerate(tools):
            tool_calls_schema.append({
                "id": f"call_{idx}_{i}_{t['name']}",
                "type": "function",
                "function": {
                    "name": t["name"],
                    "arguments": json.dumps(t["args"]),
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
                    "content": final_msg,
                    "tool_calls": tool_calls_schema,
                }
            ],
            "output": final_msg,
        }

        # 1. Root Turn Run (chain)
        root_run = RunTree(
            name="Antigravity Turn",
            run_type="chain",
            inputs=root_inputs,
            outputs=root_outputs,
            start_time=start_dt,
            end_time=end_dt,
            extra={
                "metadata": {
                    "thread_id": session_id,
                    "session_id": session_id,
                    "ls_agent_type": "root",
                    "ls_integration": "claude-code",
                    "ls_trace_schema_version": "coding-agent-v1",
                    "ls_agent_purpose": "coding",
                    "ls_agent_runtime": "Antigravity",
                }
            },
            project_name=project_name,
            client=client,
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
                        "content": final_msg,
                        "tool_calls": tool_calls_schema,
                    }
                ]
            },
            start_time=start_dt,
            end_time=end_dt,
            extra={
                "metadata": {
                    "ls_message_format": "completions",
                    "ls_provider": "anthropic",
                    "ls_model_name": "Antigravity",
                }
            },
        )
        if hasattr(llm_run, "end"):
            llm_run.end(end_time=end_dt)

        # 3. Child Tool Runs (tool)
        for i, t in enumerate(tools):
            tool_dt = parse_iso(t.get("time"))
            child_tool = root_run.create_child(
                name=t["name"],
                run_type="tool",
                inputs=t["args"],
                outputs={"output": t["output"]},
                start_time=tool_dt,
                end_time=tool_dt,
                extra={
                    "metadata": {
                        "thread_id": session_id,
                        "ls_tool_name": t["name"],
                        "tool_call_id": f"call_{idx}_{i}_{t['name']}",
                    }
                },
            )
            if hasattr(child_tool, "end"):
                child_tool.end(outputs={"output": t["output"]}, end_time=tool_dt)

        if hasattr(root_run, "end"):
            root_run.end(outputs=root_outputs, end_time=end_dt)

        root_run.post(exclude_child_runs=False)
        posted_count += 1
        print(f"Posted Turn {idx + 1}/{len(turns)}: {len(tools)} tools | {prompt[:60]}...")

    client.flush()
    print(f"\nSuccessfully backfilled {posted_count} turns into thread {session_id} on {endpoint}!")
    return 0


def main() -> int:
    default_session = "09b1da0a-3aeb-4045-974a-0d013f630055"
    default_transcript = f"/Users/ronail/.gemini/antigravity/brain/{default_session}/.system_generated/logs/transcript_full.jsonl"

    parser = argparse.ArgumentParser(description="Backfill transcript turns into LangSmith threads.")
    parser.add_argument("--transcript", default=default_transcript, help="Path to transcript JSONL file")
    parser.add_argument("--session-id", default=default_session, help="Session / Thread ID")
    parser.add_argument("--project", default=None, help="LangSmith project name")
    args = parser.parse_args()

    return backfill(args.transcript, args.session_id, args.project)


if __name__ == "__main__":
    sys.exit(main())
