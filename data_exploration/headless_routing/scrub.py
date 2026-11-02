"""Scrub machine-specific fields from recorded headless ``claude -p`` transcripts.

Reference only, not imported by the package. The ``system/init`` event of a
Claude Code transcript carries the recording machine's paths and its whole
installed skill/plugin setup. None of that is needed to parse routing, and it
does not belong in a public repo. Everything the transcript parser relies on
keeps its value: event types, ``tool_use`` blocks and their ids, ``message.id``,
``permission_denials``, ``init.tools``, ``init.mcp_servers``, ``init.model``,
``init.apiKeySource``, ``init.claude_code_version``.

Idempotent: scrubbing an already-scrubbed file changes nothing.

Usage::

    uv run python data_exploration/headless_routing/scrub.py tests/fixtures/headless_routing/*.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

# init-event fields that describe the recording machine, and what replaces each.
# Values are replaced (not deleted) so the event keeps the shape Claude Code emits;
# list fields stay lists so code iterating over them still works.
INIT_REPLACEMENTS: dict[str, Any] = {
    "cwd": "<scrubbed>",
    "original_cwd": "<scrubbed>",
    "memory_paths": "<scrubbed>",
    "messaging_socket_path": "<scrubbed>",
    "skills": [],
    "slash_commands": [],
    "plugins": [],
    "agents": [],
}

# macOS per-user temp roots look like /private/var/folders/<2 chars>/<id>/T or /var/folders/...
TMPDIR_RE: re.Pattern[str] = re.compile(r"(/private)?/var/folders/[^/\"]+/[^/\"]+/T")


def scrub_event(event: dict[str, Any], home: str) -> dict[str, Any]:
    """Return a copy of one transcript event with machine-specific data removed.

    Mechanism: takes a parsed event; if it is the ``system/init`` event, replaces
    each field in :data:`INIT_REPLACEMENTS` that is present; then rewrites any
    leftover home/temp path anywhere in the event via a JSON round-trip; returns
    the scrubbed copy.

    Args:
        event: One parsed stream-json event.
        home: The home directory to replace with ``<HOME>``.

    Returns:
        The scrubbed event (a new object; the input is not mutated).
    """
    scrubbed: dict[str, Any] = dict(event)
    if scrubbed.get("type") == "system" and scrubbed.get("subtype") == "init":
        for key, placeholder in INIT_REPLACEMENTS.items():
            if key in scrubbed:
                scrubbed[key] = placeholder
    text: str = json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":"))
    text = TMPDIR_RE.sub("<TMPDIR>", text).replace(home, "<HOME>")
    result: dict[str, Any] = json.loads(text)
    return result


def scrub_file(path: Path, home: str) -> bool:
    """Scrub one transcript file in place.

    Mechanism: takes a JSONL path, scrubs every event with :func:`scrub_event`,
    rewrites the file only if something changed, returns whether it did.

    Args:
        path: Transcript file (one JSON event per line).
        home: The home directory to replace with ``<HOME>``.

    Returns:
        True if the file was rewritten.

    Raises:
        json.JSONDecodeError: If a line is not valid JSON.
    """
    original: str = path.read_text()
    lines: list[str] = [
        json.dumps(scrub_event(json.loads(raw), home), ensure_ascii=False, separators=(",", ":"))
        for raw in original.splitlines()
        if raw.strip()
    ]
    new: str = "\n".join(lines) + "\n"
    if new == original:
        return False
    path.write_text(new)
    return True


def main() -> None:
    """Scrub every transcript named on the command line.

    Mechanism: takes file paths, runs :func:`scrub_file` on each with the
    current home directory, prints which files changed.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args: argparse.Namespace = parser.parse_args()
    home: str = os.path.expanduser("~")
    for path in args.files:
        print(f"{'scrubbed' if scrub_file(path, home) else 'unchanged'}: {path}")


if __name__ == "__main__":
    main()
