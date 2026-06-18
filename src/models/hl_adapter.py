"""Hyperliquid public REST API adapter.

The venue layer of the MCP server: returns raw HL-shaped data types
(`HLClearinghouseState`, `HLAssetPosition`, etc.). Higher layers (analytics,
tool layer) transform these into user-facing schemas.

This adapter only handles READ operations against the public info endpoint.
No order placement, no signing, no exchange endpoint — that's a separate
adapter with its own auth surface and threat model.
"""

import asyncio
import time
from typing import Any

import httpx

from src.venue.hl_schemas import (
    HLClearinghouseState,
)

INFO_URL: str = "https://api.hyperliquid.xyz/info"
DEX_CACHE_TTL_SECONDS: int = 300
DEFAULT_MAX_CONCURRENCY: int = 20
DEFAULT_TIMEOUT_SECONDS: float = 10.0


def normalize_wallet(addr: str) -> str:
    """Normalize an Ethereum address to lowercase 0x-prefixed hex.

    The Hyperliquid API silently accepts addresses with mixed casing or without
    the `0x` prefix, returning a 200 with empty positions when the un-normalized
    form happens not to match a known account. This is a silent failure mode:
    a real wallet appears empty if queried in a non-canonical form.

    Normalizing client-side eliminates the bug. Malformed inputs raise loudly
    so the caller learns immediately rather than discovering empty results
    that look like valid data.

    Args:
        addr: An Ethereum-format address, with or without 0x prefix, any case.

    Returns:
        The same address in canonical lowercase, 0x-prefixed form.

    Raises:
        ValueError: If `addr` is not a valid 40-character hex string
                    (with or without 0x prefix).
    """
    s: str = addr.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if len(s) != 40 or not all(c in "0123456789abcdef" for c in s):
        raise ValueError(f"invalid Ethereum address: {addr!r}")
    return "0x" + s


class HLAPIError(Exception):
    """Raised when the Hyperliquid REST API returns a non-2xx status.

    Carries the HTTP status, the response body (as a plain string — HL returns
    plain-text error bodies, not JSON, per the Q2b finding), and the request
    payload that triggered the error. The payload is retained in full for
    debugging; the body is truncated in the str() representation to prevent
    log spam.
    """

    def __init__(self, status: int, body: str, payload: dict[str, Any]) -> None:
        self.status: int = status
        self.body: str = body
        self.payload: dict[str, Any] = payload
        super().__init__(f"HL API {status}: {body[:200]} (payload={payload})")


class HyperliquidPublic:
    """Read-only client for the Hyperliquid public info endpoint.

    Manages:
      - HTTP client lifecycle (connection pooling via httpx.AsyncClient)
      - Concurrency capping via an asyncio.Semaphore at the request level
      - HIP-3 dex discovery and metadata caching (5-minute TTL)
      - Address normalization and dex validation before each API call
      - Per-request timeouts and exception-as-value error handling for batches

    Usage:
        async with HyperliquidPublic() as venue:
            # Single wallet on native HL
            state = await venue.fetch_clearinghouse_state("0xabc...")

            # Batch fan-out across wallets
            results = await venue.fetch_clearinghouse_states_batch(
                ["0xabc...", "0xdef..."]
            )

            # Comprehensive view across native HL + all HIP-3 dexes
            all_dex_states = await venue.fetch_all_dexes_for_user("0xabc...")
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the adapter.

        Args:
            http_client: Optional pre-configured httpx client. Useful for sharing
                         a connection pool across adapters or injecting test doubles.
                         If None, a default client is created and owned by this instance.
            max_concurrency: Maximum number of in-flight API requests at any time.
                             Q5 measurements confirmed 20 is comfortable; higher
                             values untested. Acts as backpressure against any tool
                             that accidentally fans out unboundedly.
            timeout_s: Per-request HTTP timeout. Generous default given Q3 p99
                       of ~900ms; tighter timeouts risk false-positive failures.
        """
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_http_client: bool = http_client is None
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrency)
        self._dex_metadata_cache: dict[str, dict[str, Any] | None] | None = None
        self._dex_cache_ts: float = 0.0

    async def __aenter__(self) -> "HyperliquidPublic":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned by this instance."""
        if self._owns_http_client:
            await self._http.aclose()

    async def _post(self, payload: dict[str, Any]) -> Any:
        """Issue a POST to the HL info endpoint, capped by the concurrency semaphore.

        All API calls — direct or via fan-out — go through here. The semaphore
        ensures no individual tool or batch can exceed `max_concurrency` in-flight
        requests, no matter how aggressively the caller fans out.

        Raises:
            HLAPIError: On any 4xx or 5xx response. Body is preserved as a plain
                        string; do not attempt JSON parsing.
        """
        async with self._semaphore:
            r = await self._http.post(INFO_URL, json=payload)
            if r.status_code >= 400:
                raise HLAPIError(r.status_code, r.text, payload)
            return r.json()

    async def list_dexes(self) -> dict[str, dict[str, Any] | None]:
        """List operational perp DEXes on Hyperliquid, with cached metadata.

        Returns a mapping of dex name to its metadata dict. The native HL dex
        is represented by an empty string key with a `None` value (it has no
        HIP-3 metadata; it's the default). HIP-3 deployments map their name
        to the full metadata returned by the `perpDexs` info endpoint
        (deployer address, fee recipient, OI caps, etc.).

        Cached for `DEX_CACHE_TTL_SECONDS` because the list changes only when
        new HIP-3 deployers come online, which is rare.

        Returns:
            Dict mapping dex_name → metadata. The native dex is `{"": None}`.
        """
        if (
            self._dex_metadata_cache is not None
            and (time.time() - self._dex_cache_ts) < DEX_CACHE_TTL_SECONDS
        ):
            return self._dex_metadata_cache

        raw: list[Any] = await self._post({"type": "perpDexs"})
        result: dict[str, dict[str, Any] | None] = {"": None}
        for entry in raw:
            if isinstance(entry, dict) and "name" in entry:
                result[entry["name"]] = entry

        self._dex_metadata_cache = result
        self._dex_cache_ts = time.time()
        return result

    async def _validate_dex(self, dex: str) -> None:
        """Raise ValueError if `dex` is not a known dex name.

        Per Q2c-1, unknown dex names return HTTP 500 from the API — indistinguishable
        from real server errors. Client-side validation eliminates that ambiguity.
        """
        known: set[str] = set((await self.list_dexes()).keys())
        if dex not in known:
            raise ValueError(
                f"unknown dex {dex!r}. Known: {sorted(known)}. "
                f"Use empty string for native HL perps."
            )

    async def fetch_clearinghouse_state(
        self,
        user: str,
        dex: str = "",
    ) -> HLClearinghouseState:
        """Fetch a single wallet's perpetuals account state on one dex.

        Args:
            user: Wallet address. Normalized before sending; can be passed in
                  any common form (with/without 0x, any case).
            dex: Dex name. Empty string (default) targets the native HL perps.
                 Validated against the live `perpDexs` list before sending.

        Returns:
            Validated `HLClearinghouseState` with margin summaries, maintenance
            margin, withdrawable balance, and all open positions.

        Raises:
            ValueError: On malformed wallet or unknown dex.
            HLAPIError: On API-level errors.
        """
        normalized: str = normalize_wallet(user)
        await self._validate_dex(dex)
        raw: dict[str, Any] = await self._post({
            "type": "clearinghouseState",
            "user": normalized,
            "dex": dex,
        })
        return HLClearinghouseState.model_validate(raw)

    async def fetch_clearinghouse_states_batch(
        self,
        users: list[str],
        dex: str = "",
        per_request_timeout_s: float = 2.0,
    ) -> dict[str, HLClearinghouseState | Exception]:
        """Fan out `clearinghouseState` queries across many wallets on one dex.

        Per Q5, concurrent fan-out scales cleanly up to at least 20 wallets with
        no throttling. The semaphore in `_post` ensures unbounded callers don't
        overwhelm the API. Per-wallet timeouts bound individual call latency so
        one slow wallet doesn't drag the batch's wall-clock time.

        Args:
            users: Wallet addresses, in any form (normalized client-side).
            dex: Dex name, default empty (native HL).
            per_request_timeout_s: Per-wallet timeout. Generous vs Q3 p99 of ~900ms.

        Returns:
            Dict mapping each normalized wallet → either its state or the exception
            that fetching it raised (timeout, HLAPIError, validation error). Caller
            decides how to handle partial failures.

        Raises:
            ValueError: If `dex` is unknown (raised once, before any fan-out).
        """
        await self._validate_dex(dex)
        normalized: list[str] = [normalize_wallet(u) for u in users]

        async def one(user: str) -> tuple[str, HLClearinghouseState | Exception]:
            try:
                state: HLClearinghouseState = await asyncio.wait_for(
                    self.fetch_clearinghouse_state(user, dex=dex),
                    timeout=per_request_timeout_s,
                )
                return user, state
            except Exception as e:
                return user, e

        results: list[tuple[str, HLClearinghouseState | Exception]] = await asyncio.gather(
            *(one(u) for u in normalized)
        )
        return dict(results)

    async def fetch_all_dexes_for_user(
        self,
        user: str,
        per_request_timeout_s: float = 2.0,
    ) -> dict[str, HLClearinghouseState | Exception]:
        """Fan out across every known dex for one wallet's complete ecosystem view.

        For comprehensive whale monitoring: a user with positions on both native HL
        and one or more HIP-3 deployments has separate margin pools per dex; this
        method retrieves all of them in parallel.

        Args:
            user: Wallet address.
            per_request_timeout_s: Per-dex timeout.

        Returns:
            Dict mapping each dex_name → either its state or the exception raised.
            Native HL appears under the empty-string key.
        """
        normalized: str = normalize_wallet(user)
        dexes: dict[str, dict[str, Any] | None] = await self.list_dexes()

        async def one(dex_name: str) -> tuple[str, HLClearinghouseState | Exception]:
            try:
                state: HLClearinghouseState = await asyncio.wait_for(
                    self.fetch_clearinghouse_state(normalized, dex=dex_name),
                    timeout=per_request_timeout_s,
                )
                return dex_name, state
            except Exception as e:
                return dex_name, e

        results: list[tuple[str, HLClearinghouseState | Exception]] = await asyncio.gather(
            *(one(d) for d in dexes.keys())
        )
        return dict(results)