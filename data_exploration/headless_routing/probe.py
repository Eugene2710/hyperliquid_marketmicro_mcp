"""Spike probe: run one prompt through headless ``claude -p`` and summarize it.

Reference only, not imported by the package. Answers the headless routing runner
spike's questions (see ``evals/README.md``, "Headless routing runner — spike
findings"): it launches ``claude -p`` with a scrubbed environment from an empty
temp cwd, saves the raw ``stream-json`` transcript verbatim, then prints the
fields the spike needs (auth source, model, available tools, tool attempts,
permission denials, timing).

Usage::

    uv run python data_exploration/headless_routing/probe.py \\
        --prompt "What's the carry on BTC?" --out /path/to/transcript.jsonl \\
        -- --permission-prompts none

Everything after ``--`` is appended to the ``claude`` command line, so the
permission and isolation flags can be varied per question.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

# Absolute path of the hlmcp-only MCP config next to this script.
MCP_CONFIG: Path = Path(__file__).resolve().parent / "mcp_config.json"

# The only variables passed to the child. The parent Claude Code session exports
# CLAUDE_EFFORT and CLAUDE_CODE_* variables that would otherwise leak in.
KEPT_ENV: tuple[str, ...] = ("HOME", "USER", "PATH", "TERM")

# Flags every probe shares: headless, raw event stream, hlmcp as the only MCP
# server, no built-in tools, no saved session.
BASE_ARGS: list[str] = [
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--strict-mcp-config",
    "--mcp-config",
    str(MCP_CONFIG),
    "--tools",
    "",
    "--no-session-persistence",
]


def run_probe(prompt: str, out: Path, model: str, effort: str, extra: list[str]) -> float:
    """Run one prompt through ``claude -p`` and save the raw transcript.

    Mechanism: takes a prompt and flags, runs ``claude`` with a scrubbed env in
    a fresh empty temp dir, writes stdout verbatim to ``out``, returns wall time.

    Args:
        prompt: The user prompt to send.
        out: File the raw stream-json transcript is written to.
        model: Value for ``--model``.
        effort: Value for ``--effort``.
        extra: Additional ``claude`` arguments appended after the base flags.

    Returns:
        Wall-clock seconds the ``claude`` process took.

    Raises:
        subprocess.TimeoutExpired: If the process runs longer than 300 s.
    """
    env: dict[str, str] = {k: os.environ[k] for k in KEPT_ENV if k in os.environ}
    cmd: list[str] = ["claude", *BASE_ARGS, "--model", model, "--effort", effort, *extra, prompt]
    with tempfile.TemporaryDirectory(prefix="hl-headless-") as cwd:
        start: float = time.monotonic()
        proc: subprocess.CompletedProcess[str] = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=300, check=False
        )
        elapsed: float = time.monotonic() - start
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(proc.stdout)
    print(f"exit={proc.returncode} wall_s={elapsed:.1f} cmd={cmd!r}")
    if proc.stderr.strip():
        print(f"stderr: {proc.stderr.strip()[:500]}")
    return elapsed


def summarize(transcript: Path) -> None:
    """Print the spike-relevant fields of a saved stream-json transcript.

    Mechanism: takes a transcript path, parses each JSON line, prints the init
    event's auth/model/tools, every tool_use and tool_result block in order,
    and the result event's denials, usage and timing.

    Args:
        transcript: Path to a raw stream-json transcript.

    Returns:
        None; output goes to stdout.
    """
    for raw in transcript.read_text().splitlines():
        try:
            event: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            print(f"NONJSON: {raw[:200]}")
            continue
        etype: str = str(event.get("type"))
        if etype == "system" and event.get("subtype") == "init":
            print(f"init: apiKeySource={event.get('apiKeySource')} model={event.get('model')}")
            print(f"init: tools={event.get('tools')}")
            print(f"init: mcp_servers={event.get('mcp_servers')}")
            print(f"init: memory_paths={event.get('memory_paths')}")
        elif etype in ("assistant", "user"):
            content: Any = event.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                kind: str = str(block.get("type"))
                if kind == "tool_use":
                    print(f"{etype}: tool_use name={block.get('name')} input={block.get('input')}")
                elif kind == "tool_result":
                    body: str = json.dumps(block.get("content"))[:160]
                    print(f"{etype}: tool_result is_error={block.get('is_error')} {body}")
                elif kind == "text":
                    print(f"{etype}: text {str(block.get('text'))[:160]!r}")
        elif etype == "result":
            print(f"result: is_error={event.get('is_error')} num_turns={event.get('num_turns')}")
            print(f"result: permission_denials={event.get('permission_denials')}")
            print(f"result: duration_ms={event.get('duration_ms')} usage={event.get('usage')}")


def main() -> None:
    """Parse CLI args, run one probe, and summarize its transcript.

    Mechanism: takes ``--prompt``/``--out``/``--model``/``--effort`` plus
    passthrough args after ``--``, runs :func:`run_probe`, then
    :func:`summarize`.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--effort", default="high")
    parser.add_argument("extra", nargs="*")
    args: argparse.Namespace = parser.parse_args()
    run_probe(args.prompt, args.out, args.model, args.effort, list(args.extra))
    summarize(args.out)


if __name__ == "__main__":
    main()
