"""Shared pytest fixtures.

Provides loaders for the recorded HL API responses under ``tests/fixtures/``.
These JSON files were captured from the public (no-auth)
``https://api.hyperliquid.xyz/info`` endpoint; see ``docs/api_spike_findings.md``
for provenance. Tests parse them against the ``HL*`` schemas to lock the API
contract without touching the network.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"
"""Directory holding the recorded HL API response JSON."""


def load_fixture(name: str) -> Any:
    """Load and JSON-decode a fixture file from ``tests/fixtures/``.

    Args:
        name: File name relative to the fixtures directory (e.g.
            ``"clearinghouse_whale.json"``).

    Returns:
        The decoded JSON value (typically a ``dict`` or ``list``).

    Raises:
        FileNotFoundError: If the named fixture does not exist.
    """
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture
def load_json() -> Callable[[str], Any]:
    """Return the :func:`load_fixture` loader for use inside tests.

    Returns:
        A callable mapping a fixture file name to its decoded JSON value.
    """
    return load_fixture
