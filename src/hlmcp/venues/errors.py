"""Venue-layer error types.

Only one error type lives here for v0: :class:`HLAPIError`, raised when the
Hyperliquid REST API returns a non-2xx status. It is deliberately separate from
the adapter module so tools and tests can ``except HLAPIError`` without importing
the whole venue.
"""

from typing import Any


class HLAPIError(Exception):
    """Raised when the Hyperliquid info endpoint returns a non-2xx status.

    The body is carried as a **plain string**, never JSON-parsed: HL's backend is
    Rust and returns plain-text error bodies (e.g. a ``serde`` deserialization
    message on a 422, an empty body on an unknown-dex 500). Attempting to
    ``json.loads`` a 4xx body would itself raise and mask the real error
    (api_spike_findings.md Q2b).

    Attributes:
        status: The HTTP status code (>= 400).
        body: The raw response body as a string (may be empty, e.g. unknown-dex
            500s return an empty body — Q2c).
        payload: The request payload that triggered the error, retained in full
            for debugging.
    """

    def __init__(self, status: int, body: str, payload: dict[str, Any]) -> None:
        """Initialize the error with the status, raw body, and request payload.

        Args:
            status: HTTP status code returned by HL (>= 400).
            body: Raw response body, kept as an unparsed string.
            payload: The JSON request payload that produced this error.
        """
        self.status: int = status
        self.body: str = body
        self.payload: dict[str, Any] = payload
        # Truncate the body in the message to keep logs readable; the full body
        # is preserved on ``self.body`` for callers that need it.
        super().__init__(f"HL API {status}: {body[:200]!r} (payload={payload})")
