"""Shared head/tail spill helper used by the Hermes plugin and any harness.

Kept dependency-free so a non-Python harness can port it trivially.
"""
from pathlib import Path


def truncate(result: str, max_bytes: int = 12_000, head: int = 6_000, tail: int = 2_000) -> str:
    raw = result.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return result
    h = result[:head]
    t = result[-tail:] if len(result) > tail else ""
    return (
        f"[spill] result {len(raw)} bytes exceeds {max_bytes}-byte budget.\n"
        f"--- head ({len(h)} chars) ---\n{h}\n"
        f"--- tail ({len(t)} chars) ---\n{t}"
    )
