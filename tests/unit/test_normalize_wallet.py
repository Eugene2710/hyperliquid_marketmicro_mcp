"""Unit tests for analytics utils: wallet normalization and decimal parsing.

``normalize_wallet`` is the client-side guard described in
``docs/api_spike_findings.md`` Q2b: HL silently coerces parseable-but-
noncanonical addresses and returns an empty envelope, so we must canonicalize
(0x + lowercase) BEFORE sending and reject non-addresses up front. The decimal
parse helpers mirror HL's string-serialized numerics, including the ``null``
case (e.g. an over-collateralized position's ``liquidationPx``).
"""

from decimal import Decimal

import pytest

from hlmcp.analytics.utils import (
    normalize_wallet,
    parse_decimal,
    parse_float,
    parse_optional_float,
)

# A canonical reference address and its noncanonical spellings.
_CANONICAL: str = "0xabcdef0000000000000000000000000000000001"
_BODY: str = "abcdef0000000000000000000000000000000001"


@pytest.mark.parametrize(
    "given",
    [
        _CANONICAL,  # already canonical
        _BODY,  # 40-char hex, NO 0x prefix (Q2b: HL accepts this)
        _BODY.upper(),  # pure uppercase body, no prefix
        "0x" + _BODY.upper(),  # uppercase body WITH prefix
        "0X" + _BODY,  # uppercase 0X prefix
        "0xAbCdEf0000000000000000000000000000000001",  # mixed case
        f"  {_CANONICAL}  ",  # surrounding whitespace
        f"\t{_BODY}\n",  # whitespace around a bare body
    ],
)
def test_normalize_wallet_accepts_and_canonicalizes(given: str) -> None:
    """Every noncanonical-but-valid spelling normalizes to 0x + lowercase."""
    assert normalize_wallet(given) == _CANONICAL


def test_normalize_wallet_is_idempotent() -> None:
    """Normalizing an already-canonical address returns it unchanged."""
    assert normalize_wallet(_CANONICAL) == _CANONICAL


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "   ",  # whitespace only
        "0x",  # prefix only, zero-length body
        _BODY[:-1],  # 39 chars -- too short
        _BODY + "0",  # 41 chars -- too long
        "0x" + _BODY[:-1],  # prefixed but too short
        "0x" + _BODY + "0",  # prefixed but too long
        "0xZZcdef0000000000000000000000000000000001",  # non-hex chars (Z)
        _BODY[:-2] + "gg",  # non-hex chars (g) in a bare body
        "not-an-address",  # obviously not an address
        "0x" + "g" * 40,  # right length, all non-hex
    ],
)
def test_normalize_wallet_rejects_invalid(bad: str) -> None:
    """Wrong length, non-hex, and non-address inputs raise ValueError, not slip through."""
    with pytest.raises(ValueError):
        normalize_wallet(bad)


def test_normalize_wallet_empty_message() -> None:
    """The empty-string rejection names the empty case specifically."""
    with pytest.raises(ValueError, match="empty"):
        normalize_wallet("")


# --------------------------------------------------------------------------- #
# decimal / float parse helpers                                               #
# --------------------------------------------------------------------------- #


def test_parse_decimal_preserves_precision() -> None:
    """parse_decimal returns an exact Decimal, not a lossy float."""
    assert parse_decimal("64900.12345678901234567890") == Decimal("64900.12345678901234567890")
    assert parse_decimal("-0.0") == Decimal("-0.0")


def test_parse_decimal_rejects_garbage() -> None:
    """A non-decimal string raises ValueError (normalized from InvalidOperation)."""
    with pytest.raises(ValueError, match="not a valid decimal"):
        parse_decimal("not-a-number")


@pytest.mark.parametrize(
    ("given", "expected"),
    [("12.40866", 12.40866), ("-3.5", -3.5), ("0", 0.0), ("64900.0", 64900.0)],
)
def test_parse_float(given: str, expected: float) -> None:
    """parse_float parses signed decimal strings to float."""
    assert parse_float(given) == pytest.approx(expected)


def test_parse_float_rejects_garbage() -> None:
    """A non-numeric string raises ValueError with a normalized message."""
    with pytest.raises(ValueError, match="not a valid float"):
        parse_float("abc")


def test_parse_optional_float_passes_none_through() -> None:
    """None in -> None out (mirrors a null liquidationPx)."""
    assert parse_optional_float(None) is None


def test_parse_optional_float_parses_value() -> None:
    """A non-None string is parsed like parse_float."""
    assert parse_optional_float("172.5") == pytest.approx(172.5)
