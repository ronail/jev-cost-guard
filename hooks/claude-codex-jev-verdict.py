#!/usr/bin/env python3
"""Claude Code / Codex hook: route large tool outputs through the jev oracle.

Soft auto-gate (NEVER blocks the harness). Reads the harness hook envelope on
stdin, extracts the tool output, and when it exceeds JEV_VERDICT_THRESHOLD_CHARS
(default 4000) pipes it to ../mcp/jev/server.py --verdict, which asks jev the
`is_successful` noul question and prints a JSON verdict line. The jev call
uses OPENCODE_ZEN_API_KEY (inherited from the harness env) with paid fallback
(JEV_FALLBACK_TO_PAID / JEV_FALLBACK_MODEL); no key => Bearer public (free tier,
no fallback). Prints the verdict to stdout (the harness surfaces it), always
exits 0 so a jev slip never stalls a session.

Wiring:
  Claude Code: ~/.claude/claude_desktop_config.json  ->  hooks.PostToolUse
      [{"command": "python3 /repo/hooks/claude-codex-jev-verdict.py"}]
  Codex:       ~/.codex/config.toml -> hooks (same script; envelope has tool_name + output)
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))  # this file's dir (hooks/)
REPO_ROOT = os.path.dirname(HERE)  # repo root (parent of hooks/)
SERVER = os.path.join(REPO_ROOT, "mcp", "jev", "server.py")
THRESHOLD = int(os.environ.get("JEV_VERDICT_THRESHOLD_CHARS", "4000"))


def _extract_output(event):
    """Pull the tool output text out of a Claude Code / Codex hook envelope."""
    res = event.get("tool_result") or {}
    out = res.get("output")
    if out is None and isinstance(res, dict):
        out = res.get("content")
    if out is None:
        out = event.get("tool_output", "")
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    if isinstance(out, dict) and out.get("content"):
        parts = out["content"]
        if isinstance(parts, dict):
            parts = [parts]
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p)
                       for p in parts)
    return str(out)


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(event, dict) or not event.get("tool_name"):
        sys.exit(0)
    output = _extract_output(event)
    n = len(output)
    if n < THRESHOLD:
        sys.exit(0)  # below gate threshold — nothing to judge
    try:
        proc = subprocess.run(
            [sys.executable, SERVER, "--verdict"],
            input=output, capture_output=True, text=True, timeout=90,
        )
        verdict = proc.stdout.strip()
        if verdict:
            try:
                v = json.loads(verdict)
                print(f"[jev-gate] {event['tool_name']}: output={n}c "
                      f"model={v.get('model')} noul={v.get('noul')} "
                      f"->{v.get('interpretation')} verdict={verdict}")
            except json.JSONDecodeError:
                print(f"[jev-gate] {event['tool_name']}: {verdict}")
        else:
            print(f"[jev-gate] {event['tool_name']}: output={n}c "
                  f"jev unavailable (exit={proc.returncode}) "
                  f"{proc.stderr.strip()[:200]}")
    except Exception as e:
        print(f"[jev-gate] {event['tool_name']}: error {e}")
    sys.exit(0)  # soft gate: never block the harness


if __name__ == "__main__":
    main()
