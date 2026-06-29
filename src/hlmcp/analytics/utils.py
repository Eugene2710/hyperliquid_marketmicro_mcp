"""Pure helpers for the analytics layer: wallet normalization and decimal parsing.

This module is PURE: no I/O, no async, no network (CLAUDE.md hard rule).

Two concerns live here:

1. **Wallet normalization** (:func:`normalize_wallet`). The Hyperliquid API
   silently normalizes *parseable-but-noncanonical* addresses server-side and
   returns a 200 with an empty envelope -- which looks like "valid wallet, no
   positions" but may actually be the wrong wallet. We must therefore normalize
   to canonical ``0x`` + lowercase BEFORE sending, and reject anything that is
   not a 40-hex-digit address up front (api_spike_findings.md Q2b).

2. **Decimal parsing** (:func:`parse_decimal`, :func:`parse_float`,
   :func:`parse_optional_float`). The HL API serializes every monetary/size/price
   value as a decimal *string* for precision. These helpers parse them
   explicitly and predictably, including the ``null`` case (e.g. an
   over-collateralized position's ``liquidationPx``).
"""

import re
from decimal import Decimal, InvalidOperation

# A bare Ethereum-style address body: exactly 40 hexadecimal characters, no
# ``0x`` prefix. Case-insensitive -- HL accepts upper/lower/mixed and we
# canonicalize to lowercase. We do NOT enforce EIP-55 checksum casing because
# HL itself does not require it (api_spike_findings.md Q2b).
_ADDRESS_BODY_RE: re.Pattern[str] = re.compile(r"[0-9a-fA-F]{40}")


def normalize_wallet(address: str) -> str:
    """Normalize a wallet address to canonical ``0x`` + lowercase form.

    Accepts the noncanonical inputs HL would silently coerce -- a 40-char hex
    body WITHOUT the ``0x`` prefix, pure-uppercase, and mixed-case addresses --
    and returns the canonical ``0x``-prefixed lowercase form. Surrounding
    whitespace is stripped. Normalizing client-side matters because a real
    wallet queried noncanonically can return an empty envelope that masquerades
    as "no positions" (api_spike_findings.md Q2b).

    Mechanism: strip whitespace and an optional ``0x``/``0X`` prefix; require the
    remaining body to be exactly 40 hex characters (else raise); return
    ``"0x" + body.lower()``.

    Args:
        address: A wallet address, with or without the ``0x`` prefix, in any
            hex letter case, possibly surrounded by whitespace.

    Returns:
        The canonical address: ``"0x"`` followed by 40 lowercase hex characters.

    Raises:
        ValueError: If ``address`` is empty/whitespace, has the wrong length,
            contains non-hexadecimal characters, or is otherwise not a 40-hex
            address. These are rejected here rather than sent to HL (which would
            return an undiagnosable 422 with a plain-string body).

    Examples:
        >>> normalize_wallet("0xABCdef0000000000000000000000000000000001")
        '0xabcdef0000000000000000000000000000000001'
        >>> normalize_wallet("abcDEF0000000000000000000000000000000001")
        '0xabcdef0000000000000000000000000000000001'
    """
    stripped: str = address.strip()
    if not stripped:
        raise ValueError("wallet address is empty")

    # Strip an optional 0x/0X prefix; the remaining body must be 40 hex chars.
    body: str = stripped[2:] if stripped[:2].lower() == "0x" else stripped

    if len(body) != 40:
        raise ValueError(f"wallet address must be 40 hex chars (got {len(body)}): {address!r}")
    if _ADDRESS_BODY_RE.fullmatch(body) is None:
        raise ValueError(f"wallet address contains non-hex characters: {address!r}")

    return "0x" + body.lower()


def parse_decimal(value: str) -> Decimal:
    """Parse an HL decimal-string value into a :class:`~decimal.Decimal`.

    Use this when exact decimal precision matters (money to the cent, sizes).
    The HL API serializes these as strings precisely so precision is not lost to
    binary float; :class:`Decimal` preserves that.

    Args:
        value: A decimal string from an HL response, e.g. ``"64900.0"``.

    Returns:
        The parsed :class:`Decimal`.

    Raises:
        ValueError: If ``value`` is not a valid decimal string. (Re-raised from
            :class:`decimal.InvalidOperation` for a uniform error type.)
    """
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a valid decimal string: {value!r}") from exc


def parse_float(value: str) -> float:
    """Parse an HL decimal-string value into a ``float``.

    Use this for signal/analytics math (imbalance ratios, bps) where float is
    adequate and faster than :class:`Decimal`. For exact money arithmetic prefer
    :func:`parse_decimal`.

    Args:
        value: A decimal string from an HL response, e.g. ``"12.40866"``.

    Returns:
        The parsed ``float``.

    Raises:
        ValueError: If ``value`` cannot be parsed as a float. (``float`` already
            raises :class:`ValueError`; the message is normalized here.)
    """
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a valid float string: {value!r}") from exc


def parse_optional_float(value: str | None) -> float | None:
    """Parse an HL decimal string that may be ``null`` into ``float | None``.

    Mirrors fields like a position's ``liquidationPx``, which is JSON ``null``
    when the position is over-collateralized to impossibility
    (api_spike_findings.md Q2). ``None`` in, ``None`` out; otherwise delegates
    to :func:`parse_float`.

    Args:
        value: A decimal string, or ``None``.

    Returns:
        The parsed ``float``, or ``None`` if ``value`` was ``None``.

    Raises:
        ValueError: If ``value`` is a non-``None`` value that is not a valid
            float string.
    """
    if value is None:
        return None
    return parse_float(value)
