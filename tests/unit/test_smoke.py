"""Smoke tests: the package and each subpackage import cleanly.

These catch a broken ``__init__`` (syntax error, bad import) before any logic
exists, so every later step starts from a known-importable skeleton.
"""

import importlib


def test_import_hlmcp() -> None:
    """``import hlmcp`` succeeds."""
    import hlmcp

    assert hlmcp is not None


def test_import_subpackages() -> None:
    """Each subpackage imports, catching broken ``__init__`` files."""
    for name in (
        "hlmcp.schemas",
        "hlmcp.venues",
        "hlmcp.analytics",
        "hlmcp.tools",
    ):
        module = importlib.import_module(name)
        assert module is not None
