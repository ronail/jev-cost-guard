"""jev-context-guard — spill oversized tool results to disk, keep the transcript small.

Registered via plugin.yaml::provides_hooks. Hooks run OUTSIDE the cached system
prompt, so this costs the session no prompt tokens whatsoever — it only ever
replaces what *would* have been appended to the transcript.

Wires into:
  - transform_terminal_output (terminal tool / background process output)
  - transform_tool_result     (every other tool's final result string)
"""
import os
from pathlib import Path

# Defaults. Tune via env if needed: JEV_MAX_BYTES, JEV_HEAD, JEV_TAIL, JEV_CACHE_ROOT
DEFAULT_MAX = int(os.getenv("JEV_MAX_BYTES", 12_000))
HEAD = int(os.getenv("JEV_HEAD", 6_000))
TAIL = int(os.getenv("JEV_TAIL", 2_000))
WORKSPACE = os.getenv("BH_AGENT_WORKSPACE") or os.getenv("HERMES_WORKSPACE") or "/tmp"
CACHE_ROOT = Path(os.getenv("JEV_CACHE_ROOT", WORKSPACE)) / "cache" / "jev-context-guard"


def _spill(tool_name: str, result: str, budget: int) -> str:
    """Return head+tail with a link to the full body, or the original if small."""
    raw = result.encode("utf-8", errors="replace")
    if len(raw) <= budget:
        return result

    full = result
    cache_file = CACHE_ROOT / tool_name
    cache_file.mkdir(parents=True, exist_ok=True)
    dest = cache_file / "last.full"
    # single-slot cache keeps disk bounded per tool
    dest.write_text(full, encoding="utf-8")

    head = full[:HEAD]
    tail = full[-TAIL:] if len(full) > TAIL else ""

    summary = (
        f"[jev-context-guard] result {len(raw)} bytes exceeds the "
        f"{budget}-byte budget — truncated to head+tail.\n"
        f"Full output spilled to: {dest}\n"
        f"--- head ({len(head)} chars) ---\n{head}\n"
        f"--- tail ({len(tail)} chars) ---\n{tail}"
    )
    return summary


def register(ctx):
    # Hook signatures per Hermes docs:
    #   transform_terminal_output(command, output, returncode, task_id, env_type, **kwargs) -> str|None
    #   transform_tool_result(tool_name, args, result, task_id, **kwargs) -> str|None
    def on_terminal(command, output, returncode, task_id, env_type=None, **kwargs):
        return _spill("terminal", output or "", DEFAULT_MAX)

    def on_result(tool_name, args, result, task_id, **kwargs):
        if not isinstance(result, str):
            return None
        # only guard the noisy ones; read_file/web_extract already bounded by skills, but cap still applies
        return _spill(tool_name or "tool", result, DEFAULT_MAX)

    ctx.register_hook("transform_terminal_output", on_terminal)
    ctx.register_hook("transform_tool_result", on_result)
