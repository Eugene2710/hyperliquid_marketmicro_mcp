"""Spike batch: run tool_selection cases sequentially through headless ``claude -p``.

Reference only, not imported by the package. Answers the spike's cost/time
question and produces observation rows in the proposed format (see
``docs/headless_routing_spike.md``). Every case runs with the runner's
settled flags in ONE shared empty cwd (so only one Claude Code project folder
is created), one process per prompt.

Usage::

    uv run python -m data_exploration.headless_routing.batch \\
        --cases sel-006 sel-007 --out-dir /path/to/transcripts --cwd /path/to/empty-dir
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from data_exploration.headless_routing.probe import REPO_ROOT, run_probe

# Selection dataset the case ids are looked up in.
DATASET: Path = REPO_ROOT / "evals" / "datasets" / "tool_selection.jsonl"

# Isolation and permission flags settled by the spike, on top of probe.BASE_ARGS.
RUNNER_FLAGS: list[str] = [
    "--setting-sources",
    "",
    "--disable-slash-commands",
    "--permission-prompts",
    "none",
]

# MCP tool-name prefix for the server key "hlmcp" in the generated config.
TOOL_PREFIX: str = "mcp__hlmcp__"


def observe(lines: list[str]) -> dict[str, Any]:
    """Extract the routing observation and run metadata from one transcript.

    Mechanism: takes transcript lines, collects ``tool_use`` blocks from assistant
    messages in order and stops after the first message that had a denied call
    (the spike's observed-tools rule), and reads model/version/usage/rate-limit
    fields; returns them as one dict.

    Args:
        lines: Raw stream-json lines of one transcript.

    Returns:
        Dict with ``observed_tools``, ``observed_args``, ``reported_model``,
        ``claude_code_version``, ``duration_ms``, ``is_error`` and ``rate_limit``
        (window name -> utilization, for the windows present).
    """
    events: list[dict[str, Any]] = [json.loads(raw) for raw in lines if raw.strip()]
    denied_ids: set[str] = {
        str(e.get("tool_use_id"))
        for e in events
        if e.get("type") == "system" and e.get("subtype") == "permission_denied"
    }
    # tool_use blocks grouped by assistant message id, in order. One model reply
    # spans several events that share the id, so parallel calls land together.
    by_message: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        message: Any = e.get("message")
        if e.get("type") == "assistant" and isinstance(message, dict):
            uses: list[dict[str, Any]] = [
                b for b in message.get("content") or [] if b.get("type") == "tool_use"
            ]
            if uses:
                by_message.setdefault(str(message.get("id")), []).extend(uses)
    # Keep every call up to and including the first message containing a denial.
    kept: list[dict[str, Any]] = []
    for uses in by_message.values():
        kept.extend(uses)
        if any(u.get("id") in denied_ids for u in uses):
            break
    init: dict[str, Any] = next((e for e in events if e.get("subtype") == "init"), {})
    result: dict[str, Any] = next((e for e in events if e.get("type") == "result"), {})
    reported: str | None = next(
        (
            str(e["message"].get("model"))
            for e in events
            if e.get("type") == "assistant" and isinstance(e.get("message"), dict)
        ),
        None,
    )
    rate_limit: dict[str, Any] = {}
    for e in events:
        if e.get("type") == "rate_limit_event":
            windows: dict[str, Any] = (e.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            rate_limit = {name: w.get("utilization") for name, w in windows.items()}
    return {
        "observed_tools": [str(u.get("name")).removeprefix(TOOL_PREFIX) for u in kept],
        "observed_args": [dict(u.get("input") or {}) for u in kept],
        "reported_model": reported or init.get("model"),
        "claude_code_version": init.get("claude_code_version"),
        "duration_ms": result.get("duration_ms"),
        "is_error": result.get("is_error"),
        "rate_limit": rate_limit,
    }


def main() -> None:
    """Run the named cases sequentially and print one observation row each.

    Mechanism: takes case ids, an output dir and a shared cwd; for each case
    runs :func:`run_probe` with :data:`RUNNER_FLAGS`, parses the transcript with
    :func:`observe`, prints a JSON row; then prints wall-time totals.

    Returns:
        None.

    Raises:
        KeyError: If a case id is not in the dataset.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--tag", default="r1", help="suffix for transcript file names")
    args: argparse.Namespace = parser.parse_args()

    cases: dict[str, dict[str, Any]] = {
        c["id"]: c for c in (json.loads(raw) for raw in DATASET.read_text().splitlines())
    }
    args.cwd.mkdir(parents=True, exist_ok=True)
    walls: list[float] = []
    for case_id in args.cases:
        case: dict[str, Any] = cases[case_id]
        out: Path = args.out_dir / f"{case_id}-{args.tag}.jsonl"
        wall: float = run_probe(
            case["prompt"], out, args.model, args.effort, RUNNER_FLAGS, cwd=args.cwd
        )
        walls.append(wall)
        row: dict[str, Any] = {
            "id": case_id,
            **observe(out.read_text().splitlines()),
            "model": f"{args.model}/{args.effort}",
            "requested_model": args.model,
            "effort": args.effort,
            "runner": "claude-code-headless",
            "wall_s": round(wall, 1),
        }
        print(json.dumps(row))
    print(f"total_wall_s={sum(walls):.1f} mean_wall_s={sum(walls) / len(walls):.1f}")


if __name__ == "__main__":
    main()
