"""jev-context-guard — spill oversized tool results to disk, keep the transcript small.

Registered via plugin.yaml::provides_hooks. Hooks run OUTSIDE the cached system
prompt, so this costs the session no prompt tokens whatsoever — it only ever
replaces what *would* have been appended to the transcript.

Wires into:
  - transform_terminal_output (terminal tool / background process output)
  - transform_tool_result     (every tool's final result, before context)

Contract note (from model_tools._apply_transform_tool_result_hook):
a hook may only replace a result with a *str*; non-str returns are ignored.
So structured (dict/list) results are JSON-stringified and capped the same way
string results are — this is the docs' own "wrap long JSON tool responses" use
case.
"""
import os
import json
from pathlib import Path

# Defaults. Tune via env if needed: JEV_MAX_BYTES, JEV_HEAD, JEV_TAIL, JEV_CACHE_ROOT
DEFAULT_MAX = int(os.getenv("JEV_MAX_BYTES", 6_000))
HEAD = int(os.getenv("JEV_HEAD", 4_000))
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


def _as_text(result) -> str | None:
    """Normalize a tool result to text for sizing. None if not representable."""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
    return None


def register(ctx):
    # Hook signatures per Hermes docs:
    #   transform_terminal_output(command, output, returncode, task_id, env_type, **kwargs) -> str|None
    #   transform_tool_result(tool_name, args, result, task_id, **kwargs) -> str|None
    def on_terminal(command, output, returncode, task_id, env_type=None, **kwargs):
        return _spill("terminal", output or "", DEFAULT_MAX)

    def on_result(tool_name, args, result, task_id, **kwargs):
        text = _as_text(result)
        if text is None:
            # not stringifiable (e.g. binary); leave it to Hermes' own handling
            return None
        if "[jev-context-guard]" in text[:500]:
            # already capped by transform_terminal_output (or an earlier pass) — don't re-cap
            return None
        capped = _spill(tool_name or "tool", text, DEFAULT_MAX)
        if capped == text:
            # under budget -> leave the original (dict) result type intact
            return None
        return capped

    ctx.register_hook("transform_terminal_output", on_terminal)
    ctx.register_hook("transform_tool_result", on_result)
