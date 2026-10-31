#!/usr/bin/env python3
"""jev analytics: a switchable per-turn cost report for the jev MCP tools.

There is no switch to flip here. The MCP server records every jev call and
appends an `in=/out=` suffix to the tool result, so the numbers always exist;
"turning analytics on" means asking for a report, and this script only formats
one. That keeps the cost of a jev call a property of the call rather than a
property of a reporting setting.

    python3 scripts/jev_analytics.py report --model deepseek-v4.1-flash
    python3 scripts/jev_analytics.py drop 1        # row 1 was not a real save
    python3 scripts/jev_analytics.py clear

The ledger lives in, in order:

    $JEV_ANALYTICS_DIR                 explicit
    $PLUGIN_DATA/analytics             host-provided plugin data dir
    $XDG_STATE_HOME/jev-cost-guard     portable default
    ~/.local/state/jev-cost-guard

The ledger itself honours `JEV_CALL_LOG` when that is set, so a host that
already names a call-log path (Hermes' `mcp_servers.jev.env`) gets the rows
there instead of a second file.

Numbers
-------
Every figure is an ESTIMATE built from things that were actually measured:

    body_tokens    = body_chars the MCP server sent to jev / 4
    kept_tokens    = the verdict line that entered the transcript / 4
    saved_tokens   = body_tokens - kept_tokens
    jev cost       = measured `usage` from jev's own response

The counterfactual ("the main model would have read that body") is the one
thing nobody can measure, so it is stated as an assumption in every report and
`drop N` exists to retract a row where it was false.

Pricing is OFFICIAL DEEPSEEK LIST PRICE ONLY. The same model id costs anywhere
from $0.02 to $0.67 per 1M across resellers, so guessing a provider's price
would be off by up to 40x. Anything that is not a DeepSeek model prints tokens
with `$ n/a`.

Exit status: 0 printed something, 1 nothing to print (off / empty), 2 usage
error.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

CHARS_PER_TOKEN = 4.0
LEDGER_NAME = "calls.jsonl"
DEFAULT_MAX_AGE = 1800  # rows older than 30 min are a leaked turn, not this one

# Official DeepSeek API list prices, USD per 1M tokens, keyed by model family.
# Source: the `deepseek` (first-party) provider entries in the models.dev
# catalog, snapshot 2026-10-31 — deepseek-flash / deepseek-v4-flash /
# deepseek-v4-flash-vision-exp all list 0.15 / 0.60 (family deepseek-flash) and
# deepseek-v4-pro lists 0.66 / 1.98 (family deepseek-thinking). Reseller prices
# (opencode-go, openrouter, vercel, ...) are deliberately NOT in this table.
OFFICIAL_DEEPSEEK = {
    "deepseek-flash": (0.15, 0.60),
    "deepseek-thinking": (0.66, 1.98),
}
# Name -> family. Only unambiguous markers are mapped: `deepseek-chat` and the
# bare `deepseek` ids resolve to nothing rather than to a guessed family.
FAMILY_MARKERS = (
    ("deepseek-thinking", ("pro", "reasoner", "thinking")),
    ("deepseek-flash", ("flash",)),
)


# ── state paths (must match mcp/jev/server.py; a test asserts they agree) ──────


def analytics_dir() -> str:
    env = (os.environ.get("JEV_ANALYTICS_DIR") or "").strip()
    if env:
        return os.path.expanduser(env)
    data = (os.environ.get("PLUGIN_DATA") or "").strip()
    if data:
        return os.path.join(os.path.expanduser(data), "analytics")
    state = (os.environ.get("XDG_STATE_HOME") or "").strip()
    base = os.path.expanduser(state) if state else os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "jev-cost-guard")


def ledger_path() -> str:
    explicit = (os.environ.get("JEV_CALL_LOG") or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    return os.path.join(analytics_dir(), LEDGER_NAME)


# ── ledger ────────────────────────────────────────────────────────────────────


def read_rows() -> list[dict]:
    path = ledger_path()
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        return []
    return rows


def write_rows(rows: list[dict]) -> None:
    path = ledger_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


# ── pricing (official DeepSeek only) ──────────────────────────────────────────


def bare_model(model: str) -> str:
    """`opencode-go/deepseek-v4.1-flash` -> `deepseek-v4.1-flash`."""
    return (model or "").strip().rsplit("/", 1)[-1].lower()


def deepseek_family(model: str) -> str | None:
    name = bare_model(model)
    if "deepseek" not in name:
        return None
    for family, markers in FAMILY_MARKERS:
        if any(m in name for m in markers):
            return family
    return None


def price_for(model: str) -> tuple[float, float] | None:
    family = deepseek_family(model)
    return OFFICIAL_DEEPSEEK.get(family) if family else None


def price_label(model: str) -> str:
    family = deepseek_family(model)
    if family:
        pin, pout = OFFICIAL_DEEPSEEK[family]
        return f"official DeepSeek {family} ${pin:g}/${pout:g} per M"
    return "n/a — official DeepSeek pricing only"


# ── report ────────────────────────────────────────────────────────────────────


def _tok(chars: int) -> int:
    return int(math.ceil(chars / CHARS_PER_TOKEN))


def _usd(amount: float) -> str:
    if amount == 0:
        return "$0"
    if abs(amount) < 0.01:
        return f"${amount:.5f}"
    return f"${amount:.2f}"


def stale(rows: list[dict], max_age: int) -> tuple[list[dict], int]:
    """Split this turn's rows from a leaked previous turn's."""
    cutoff = time.time() - max_age
    fresh, dropped = [], 0
    for row in rows:
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts < cutoff:
            dropped += 1
            continue
        fresh.append(row)
    return fresh, dropped


def build_report(rows: list[dict], model: str = "",
                 price_in: float | None = None,
                 price_out: float | None = None) -> str:
    if not rows:
        return ""
    body_chars = sum(int(r.get("body_chars") or 0) for r in rows)
    verdict_chars = sum(int(r.get("verdict_chars") or 0) for r in rows)
    jev_in = sum(int(r.get("jev_in") or 0) for r in rows)
    jev_out = sum(int(r.get("jev_out") or 0) for r in rows)

    body_tok, kept_tok = _tok(body_chars), _tok(verdict_chars)
    saved = body_tok - kept_tok

    # jev's own cost: only the relay's *-free tier is free. TypeSafe and the paid
    # fallback bill, and we refuse to price them (official DeepSeek rates only
    # cover the main model), so the report says "unpriced" rather than "$0".
    billable = any(bool(r.get("billable")) for r in rows)
    jev_note = "jev cost unpriced" if billable else "free tier: $0"

    if price_in is not None or price_out is not None:
        pin = price_in if price_in is not None else 0.0
        pout = price_out if price_out is not None else 0.0
        label = f"override ${pin:g}/${pout:g} per M"
    else:
        resolved = price_for(model) if model else None
        if resolved:
            pin, pout = resolved
            label = price_label(model)
        else:
            pin = pout = None
            label = price_label(model)

    calls = f"{len(rows)} call" + ("" if len(rows) == 1 else "s")
    lines = [f"jev analytics · {calls} (est.)"]
    lines.append(
        f"  screened  {body_chars:,} chars ≈ {body_tok:,} tok → main model read 0"
    )
    lines.append(f"  jev       in {jev_in:,} / out {jev_out:,} tok ({jev_note})")

    if pin is None:
        lines.append(f"  saved     {saved:,} tok ≈ $ n/a @ {label}")
    else:
        cost = saved / 1_000_000 * pin
        if saved < 0:
            lines.append(
                f"  saved     {saved:,} tok ≈ {_usd(-cost)} net loss "
                f"(verdict cost more than the body) @ {label}"
            )
        else:
            lines.append(f"  saved     {saved:,} tok ≈ {_usd(cost)} @ {label}")
    lines.append(
        "  assumption: those bodies would otherwise have been read by the main model"
    )
    return "\n".join(lines)


# ── cli ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="jev_analytics.py",
        description="Read jev's recorded per-turn cost telemetry.",
        epilog="Official DeepSeek list prices only; other models print $ n/a.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_report = sub.add_parser("report", help="print this turn's report")
    p_report.add_argument("--model", default="",
                          help="the main model, e.g. deepseek-v4.1-flash")
    p_report.add_argument("--price-in", type=float, default=None,
                          help="override: USD per 1M input tokens")
    p_report.add_argument("--price-out", type=float, default=None,
                          help="override: USD per 1M output tokens")
    p_report.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                          help=f"ignore rows older than N seconds "
                               f"(default {DEFAULT_MAX_AGE})")
    p_report.add_argument("--keep", action="store_true",
                          help="do not clear the rows after printing")

    p_drop = sub.add_parser("drop", help="retract row N of 1 (1-based)")
    p_drop.add_argument("index", type=int)

    sub.add_parser("clear", help="delete every recorded row")

    args = ap.parse_args(argv)

    if args.cmd == "clear":
        write_rows([])
        print(f"jev analytics: cleared {ledger_path()}")
        return 0

    if args.cmd == "drop":
        rows = read_rows()
        if not 1 <= args.index <= len(rows):
            sys.stderr.write(
                f"jev analytics: no row {args.index} (ledger has {len(rows)})\n"
            )
            return 2
        removed = rows.pop(args.index - 1)
        write_rows(rows)
        print(f"jev analytics: dropped row {args.index} "
              f"({removed.get('tool', '?')}, "
              f"{int(removed.get('body_chars') or 0):,} chars)")
        return 0

    rows, leaked = stale(read_rows(), args.max_age)
    text = build_report(rows, args.model, args.price_in, args.price_out)
    if not args.keep:
        write_rows([])
    if not text:
        if leaked:
            sys.stderr.write(
                f"jev analytics: dropped {leaked} stale row(s); nothing this turn\n"
            )
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
