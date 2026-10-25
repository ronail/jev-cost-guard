#!/usr/bin/env python3
"""jev-pr-ui-check procedure.

Resolves the latest PR merged into `main`, screens its *real* changed files
(those exist in the working tree since the PR is merged) with the jev decision
model via `jgrep`, and prints a yes/no UI-change verdict + per-file probability.
No file contents enter context — only paths + probabilities (jev_find principle).

Run from the repo root:
    python3 skills/jev-pr-ui-check/scripts/check_pr_ui.py
"""
import json, os, shutil, subprocess, sys


def load_typesafe_key() -> str | None:
    """TYPESAFE_API_KEY from env, else the dev profile .env."""
    k = os.environ.get("TYPESAFE_API_KEY")
    if k:
        return k
    env_path = os.environ.get(
        "JEV_PROFILE_ENV",
        os.path.expanduser("~/.hermes/profiles/dev/.env"),
    )
    if os.path.exists(env_path):
        for line in open(env_path):
            s = line.strip()
            if s.startswith("TYPESAFE_API_KEY="):
                return s.split("=", 1)[1].strip().strip("\"'")
    return None


def latest_merged_pr() -> dict | None:
    out = subprocess.run(
        ["gh", "pr", "list", "--state", "merged", "--base", "main",
         "--limit", "1", "--json", "number,mergeCommit,url,title"],
        capture_output=True, text=True,
    )
    if out.returncode:
        print("gh error:", (out.stderr or "").strip()[:300])
        sys.exit(1)
    items = json.loads(out.stdout or "[]")
    return items[0] if items else None


def changed_files(pr_number: int) -> list[str]:
    out = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "files",
         "--jq", ".files[].path"],
        capture_output=True, text=True,
    )
    if out.returncode:
        print("gh error:", (out.stderr or "").strip()[:300])
        sys.exit(1)
    return [ln for ln in out.stdout.splitlines() if ln and os.path.exists(ln)]


UI_QUERY = "UI rendering styling layout markup template component frontend presentational"


def main() -> None:
    pr = latest_merged_pr()
    if not pr:
        print("No merged PRs found.")
        sys.exit(0)

    files = changed_files(pr["number"])
    if not files:
        print(f"PR #{pr['number']} {pr.get('title','')}: changed files not in the working tree "
              f"(e.g. deleted files) — nothing to screen.")
        sys.exit(0)

    jgrep = shutil.which("jgrep")
    if not jgrep:
        print("jgrep not on PATH — install: `uv tool install jev-grep`")
        sys.exit(1)

    key = load_typesafe_key()
    env = dict(os.environ)
    env.pop("JEV_MODEL", None)
    env.pop("JEV_PROVIDER", None)
    env.pop("JEV_ENDPOINT", None)
    if key:
        # Resolve onto TypeSafe with a typesafe-valid model; never send a
        # zen-only `-free` model to api.typesafe.ai (→ 400). jgrep reads its
        # own key from this env (never argv).
        env["TYPESAFE_API_KEY"] = key
        env["JEV_PROVIDER"] = "typesafe"
        env["JEV_MODEL"] = "jev-1.13.0"
        env.pop("OPENCODE_ZEN_API_KEY", None)
        provider = "typesafe (jev-1.13.0)"
    else:
        provider = "opencode zen (jev-1.13-free, public bearer)"

    proc = subprocess.run(
        [jgrep, "-o", "-p", "0", "--whole", "--json",
         "--max-chars=8000", UI_QUERY, *files],
        capture_output=True, text=True, env=env, timeout=180,
    )

    probs: list[tuple[str, float]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if "p" in rec and "file" in rec:
            probs.append((rec["file"], float(rec["p"])))

    if not probs and proc.returncode not in (0, 1):
        print(f"jev call failed: {(proc.stderr or '').strip().splitlines()[-1:]}".splitlines()[0]
              if proc.stderr else "jev call failed")
        sys.exit(2)

    ui_files = [f for f, p in probs if p >= 0.7]
    print(f"PR #{pr['number']}: {pr.get('title','')}  "
          f"(merge {(pr.get('mergeCommit') or {}).get('oid','')[:12]})")
    print(f"jev (decision model) over {len(files)} changed file(s): "
          f"{provider} — 1 batched request via jgrep")
    print("UI-relevance per file (p >= 0.7 = UI):")
    for f, p in probs:
        flag = "  <- UI" if p >= 0.7 else ""
        print(f"  {p:.2f}  {f}{flag}")
    verdict = "UI CHANGES PRESENT" if ui_files else "no UI changes"
    print("VERDICT:", verdict)
    if (proc.stderr or "").strip():
        # surface jev's own HTTP status lines (e.g. 429/402) without leaking bodies
        tail = proc.stderr.strip().splitlines()[-1][:200]
        print("jev-call note:", tail)


if __name__ == "__main__":
    main()
