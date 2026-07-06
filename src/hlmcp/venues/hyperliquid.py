"""
Read-only Hyperliquid public REST adapter.

The venue layer of the MCP server: the only place network I/O. It returns raw HL
schema types (HLClearinghouseState, HLL2Book, HLPerpDexs); higher layers
(analytics, tools) transform those into user-facing responses.

Read operations against public info endpoint only - no order placement, no
signing, no exchange endpoint.
That will be a separate adapter with its own auth surface and threat model, currently out of scope.

What this adapter owns:

- HTTP lifecycle: an ``httpx.AsyncClient`` it can own or borrow.
- Rate limiting: a TokenBucket class (sustained 7 req/sec, burst 15) wrapping every request.
- Concurrency capping: an ``asyncio.Semaphore`` (20) at ``_post``.
- Address normalization: every wallet is run through
  :func:`~hlmcp.analytics.utils.normalize_wallet` BEFORE sending, because HL
  silently coerces noncanonical addresses and returns an empty envelope that
  masquerades as "no positions".
- Dex validation: an unknown dex name returns an undiagnosable HTTP 500, so
  ``dex`` is validated against the live ``perpDexs`` list client-side before any
  ``clearinghouseState`` call.
- HIP-3 discovery + caching: ``perpDexs`` cached ~5 min.

All knobs come from an injected :class:`~hlmcp.config.HLConfig`.
"""

import asyncio
import time
from types import TracebackType
from typing import Any

import httpx

from hlmcp.analytics.aggregation import L2BookParams
from hlmcp.analytics.utils import normalize_wallet
from hlmcp.config import HLConfig
from hlmcp.schemas.hl_api import HLClearinghouseState, HLL2Book, HLPerpDexs
from hlmcp.venues.errors import HLAPIError

# The empty-string dex key targets native HL perps; HL treats an omitted ``dex``
# and ``dex: ""`` identically
NATIVE_HL_DEX: str = ""


class TokenBucket:
    """
    An asyncio token-bucket rate limiter.

    Models a bucket that holds at most ``capacity`` tokens and refills
    continuously at ``rate`` tokens/seconds. Each `acquire` consumes one token,
    waiting if none are available. This lets a short burst fire instantly (up to
    ``capacity`` requests) while the sustained throughput is bounded to ``rate`` -
    exactly the HL policy from Q4 (sustained 7/s , burst 15).

    The implementation holds an ``asyncio.Lock`` across the refill-and-wait so
    that concurrent acquirers are served in arrival order and never collectively
    overshoot the rate: if the bucket is empty, each waiter sleeps just long
    enough for its own single token to accrue before proceeding. Asyncio is
    single threaded, so the only contention is cooperative, which the lock
    serializes correctly.

    Attributes:
            rate: Refill rate in tokens/seconds.
            capacity: Max number of tokens bucket can hold.
    """

    def __init__(self, rate: float, capacity: float) -> None:
        """
        Initializes a full bucket.

        Bucket starts full (``capacity`` tokens) so the first burst is not artificially throttled.

        Args:
            rate: Tokens added per second (the sustained request rate). Must be > 0.
            capacity: Max tokens (the burst size). Must be >= 1.

        Raises:
            ValueError: If ``rate`` <= 0 or ``capacity`` < 1.
        """
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self.rate = rate
        self.capacity = capacity
        self._tokens: float = capacity
        self._updated: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self) -> None:
        """
        Consumes one token, waiting until one is available.

        Mechanism: under the lock, refill tokens for the elapsed wall-clock time
        (capped at ``capacity``); if at least one token is present, consume it and
        return immediately; otherwise sleep for exactly the time needed to accrue
        the one-time token deficit, then consume it. The lock is held across the
        sleep so waiters drain in order at the sustained ``rate``.
        """
        async with self._lock:
            now: float = time.monotonic()
            elapsed: float = now - self._updated
            self._updated = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

            if self._tokens < 1.0:
                deficit: float = 1.0 - self._tokens
                wait_s: float = deficit / self.rate
                await asyncio.sleep(wait_s)
                # After sleeping exactly enough for one token, consume it. We set
                # to 0.0 rather than subtract to avoid drift accumulating.
                self._tokens = 0.0
                self._updated = time.monotonic()
            else:
                self._tokens -= 1.0


class HyperliquidPublic:
    """
    Read-only client for the Hyperliquid public info endpoint.

    Construct with class `~hlmcp.config.HLConfig` and use as async context manager
    so the owned HTTP client is closed:
        async with HyperliquidPublic() as venue:
            state = await venue.fetch_clearinghouse_state("0x...")
            book = await venue.fetch_l2_book("BTC", {"nSigFigs": 5, "mantissa": 5}
            dexes = await venue.list_dexes()

    Thread-safety: intended for use within a single event loop. The rate limiter
    and semaphore coordinate concurrent coroutines on that loop; this class is not
    designed for use across OS threads.

    Attributes are private; behavior is exposed through the ``fetch_*`` and ``list_dexes`` methods.
    """

    def __init__(
        self,
        config: HLConfig | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """
        Initializes the adapter.

        Args:
            config: Operating envelope (endpoint, concurrency, rate limit, timeout, cache TTL).
            Defaults to  class `HLConfig` defaults - the spike-derived production envelope.
            http_client: Optional pre-configured async client, for sharing a
            connection pool or injecting a test double. If ``None``, a client is
            created with the config's per-request timeout and owned(and closed) by
            this instance.
        """
        self._config: HLConfig = config or HLConfig()
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient(
            timeout=self._config.request_timeout_s
        )
        # we only close clients we created; a borrowed client is the caller's.
        self._owns_http_client: bool = http_client is None
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self._config.max_concurrency)
        self._rate_limitter: TokenBucket = TokenBucket(
            rate=self._config.sustained_rate_per_sec,
            capacity=self._config.burst_capacity,
        )
        # perpDexs cache: the parsed response plus the monotonic timestamp it was fetched at.
        # ``None`` means "never fetched.
        self._dex_cache: HLPerpDexs | None = None
        self._dex_cache_ts: float = 0.0

    async def __aenter__(self) -> "HyperliquidPublic":
        """
        Enter the async context, returning ``self``.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit the async context, closing the owned HTTP client."""
        await self.aclose()

    async def aclose(self) -> None:
        """
        Close the underlying HTTP client if this instance owns it.

        Idempotent and a no-op for a borrowed client (the caller closes that).
        """
        if self._owns_http_client:  # self._owns_http_client is a boolean flag
            await self._http.aclose()

    async def _post(self, payload: dict[str, Any]) -> Any:
        """
        POST ``payload`` to the info endpoint, rate limited and concurrency capped.

        Every request, direct or fanned out, funnels through here, so the rate
        limiter and semaphore apply uniformly. A token is acquired first (which
        may wait), then the semaphore is held only for the duration of the actual
        network call so the in-flight count never exceeds ``max_concurrency``.

        Mechanism: ``await rate_limiter.acquire()`` -> ``async with semaphore`` ->
        ``http.post`` -> raise on non-2xx (body kept as a plain string) -> return
        decoded JSON.

        Args:
            payload: JSON request body (e.g.
            ``{
                "type": "clearinghouseState",
                "user": ... ,
                "dex": ...
            }``)

        Returns: Decoded JSON response (a ``dict`` or ``list`` depending on the endpoint).

        Raise: HLAPIError: On any 4xx/5xx response, carrying the status, the raw
        plain-string body, and the request payload. The body is NOT JSON-parsed.
        """
        await self._rate_limitter.acquire()
        async with self._semaphore:
            response: httpx.Response = await self._http.post(self._config.base_url, json=payload)
            if response.status_code >= 400:
                # Body is a plain string (serde error / empty); do not json-parse
                raise HLAPIError(response.status_code, response.text, payload)
            return response.json()

    async def list_dexes(self) -> HLPerpDexs:
        """
        Fetch the ``perpDexs`` HIP-3 discovery list, cached for the TIL.

        The response is null-first: its leading element is ``null`` (native HL,
        the emppty-string dex key) and the rest are HIP-3 deployments. The parsed
        :class:`HLPerpDexs` preserves that contract; use its ``.dexes`` property
        for just the named deployments.

        Cached for ``config.dex_cache_ttl_s`` because the list changes only when a
        new HIP-3 deployer comes online.

        Returns: Parsed :class:`HLPerpDexs`.

        Raise: HLAPIError if the ``perpDexs`` request fails.
        """
        now: float = time.monotonic()
        if (
            self._dex_cache is not None
            and (now - self._dex_cache_ts) < self._config.dex_cache_ttl_s
        ):
            return self._dex_cache

        raw: Any = await self._post({"type": "perpDexs"})
        parsed: HLPerpDexs = HLPerpDexs.model_validate(raw)
        self._dex_cache = parsed
        self._dex_cache_ts = now
        return parsed

    async def _known_dex_names(self) -> set[str]:
        """
        Return the set of valid ``dex`` values, including native HL.

        Mechanism: take the named HIP-3 deployments from ``list_dexes`` (which is
        cached) and add the empty string for native HL.

        Returns:
            A set containing "" (native HL) and every HIP-3 dex name.
        """
        dexes: HLPerpDexs = await self.list_dexes()
        names: set[str] = {dex.name for dex in dexes.dexes}
        names.add(NATIVE_HL_DEX)
        return names

    async def _validate_dex(self, dex: str) -> None:
        """
        Raise ``ValueError`` if ``dex`` is not a known dex name.

        An unknown dex name returns HTTP 500 with an empty body - indisgushable
        from a genuine server rttot so it MUST be caught client-dise before the
        call goes out. This runs before any network request in the ``fetch_*``
        methods, so an invalid dex never reaches HL.

        Args:
            dex: The dex name to validate ("" for native HL).

        Raises:
            ValueError IF ``dex`` is not in the live ``perpsDexs`` list (nor the
            native HL empty string).
        """
        known: set[str] = await self._known_dex_names()
        if dex not in known:
            # use !r to force repr() string representation for full string
            raise ValueError(
                f"Unkown dex {dex!r}; known {sorted(known)}."
                f"Use the empty string {NATIVE_HL_DEX!r} for native HL perps."
            )

    async def fetch_clearinghouse_state(
        self, user: str, dex: str = NATIVE_HL_DEX
    ) -> HLClearinghouseState:
        """
        Fetch one wallet's perpetuals account state on one dex.

        Mechanism: normalize the wallet -> validate the dex client-side -> POST
        ``clearinghouseState`` -> parse into class ``HLClearinghouseState``.

        Args:
            user: Wallet address in any common form (with/without ``0x``, any
            case); normalized before sending.
            dex: Dex name. "" (default) targets native HL perps; a HIP-3 name
            targets that deployment. Validated before the request.

        Raises:
            ValueError: IF ``user`` is not a valid address or ``dex`` is unknown
            (both raised before any network call).
            HLAPIError: On an API-level error.
        """
        normalized: str = normalize_wallet(user)
        await self._validate_dex(dex)
        raw: Any = await self._post({"type": "clearinghouseState", "user": normalized, "dex": dex})
        return HLClearinghouseState.model_validate(raw)

    async def fetch_clearinghouse_states_batch(
        self, users: list[str], dex: str = NATIVE_HL_DEX, per_request_timeout_s: float | None = None
    ) -> dict[str, HLClearinghouseState | Exception]:
        """
        Fan out ``clearinghouseState`` across many wallets on one dex.

        Concurrent fan-out scales cleanly; the semaphore in ``_post`` bounds
        in-flight requests no matter how wide the fan-out. Per-wallet timeouts keep
        one slow wallet from dragging the whole batch's wall-clock.

        Errors are returned as **values, not raised **: each wallet maps to either
        its state or the exception fetching it produced (timeout, ``HLAPIError``,
        validation error). The caller decides how to handle partial failure. The
        one exception is an unknown ``dex``, which raises once up front (before any
        fan-out) since it would fail every wallet identically.

        Args:
            users: Wallet address in any form (normaliized per-wallet).
            dex: Dex name, default native HL. Validated once before fan-out.
            per_request_timeout_s: Per-wallet timeout; defaults to the config's
            ``request_timeout_s``.

        Returns:
            A dict mapping each **normalized** wallet to its class
            ``HLClearinghouseState`` or the class ``Exception`` raised.

        Raises:
            ValueError: if ``dex`` is unknown (raised once, before fan-out).
        """
        await self._validate_dex(dex)
        normalized: list[str] = [normalize_wallet(user) for user in users]
        timeout_s: float = (
            per_request_timeout_s
            if per_request_timeout_s is not None
            else self._config.request_timeout_s
        )

        async def fetch_one(wallet: str) -> tuple[str, HLClearinghouseState | Exception]:
            """Fetch one wallet, capturing any error as the return value."""
            try:
                state: HLClearinghouseState = await asyncio.wait_for(
                    self.fetch_clearinghouse_state(wallet, dex=dex), timeout=timeout_s
                )
                return wallet, state
            except Exception as e:
                return wallet, e

        results: list[tuple[str, HLClearinghouseState | Exception]] = await asyncio.gather(
            *(fetch_one(wallet) for wallet in normalized)
        )
        return dict(results)  # this returns str: HLClearingHouseState | Exception

    async def fetch_all_dexes_for_user(
        self, user: str, per_request_timeout_s: float | None = None
    ) -> dict[str, HLClearinghouseState | Exception]:
        """
        Fetch one wallet's state across every known dex (native HL + all HIP-3).

        A wallet holds separate margin pools per dex, so a complete whale view
        requires querying all of them. Some exceptions-as-values contract as
        ``fetch_clearinghouse_states_batch``.

        Args:
            user: wallet address (normalized before sending).
            per_request_timeout_s: Per dex timeout; defaults to the config's request_timeout_s.

        Returns:
            A dict mapping each dex name ("" for native HL) to its
            ``HLClearinghouseState`` or the class ``Exception`` raised.

        Raises:
            ValueError: If ``user`` is not a valid address (raised before fan-out).
            HLAPIError: If the ``perpDexs`` discovery call itself fails.
        """
        normalized: str = normalize_wallet(user)
        timeout_s: float = (
            per_request_timeout_s
            if per_request_timeout_s is not None
            else self._config.request_timeout_s
        )
        dex_names: set[str] = await self._known_dex_names()

        async def fetch_one(dex: str) -> tuple[str, HLClearinghouseState | Exception]:
            """Fetch the wallet on one dex, capturing any error as the return value."""
            try:
                state: HLClearinghouseState = await asyncio.wait_for(
                    self.fetch_clearinghouse_state(normalized, dex=dex), timeout=timeout_s
                )
                return dex, state
            except Exception as e:
                return dex, e

        results: list[tuple[str, HLClearinghouseState | Exception]] = await asyncio.gather(
            *(fetch_one(dex_name) for dex_name in dex_names),
        )
        return dict(results)

    async def fetch_l2_book(self, coin: str, params: L2BookParams | None = None) -> HLL2Book:
        """
        Fetch the aggregated L2 order book for a symbol.

        The endpoint returns at most 20 levels per side, aggregated per ``params``
        (``nSigFigs``/``mantissa``). Pass the result of
        `~hlmcp.analytics.aggregation.choose_aggregation` as ``params`` so the 20
        levels reach the band the caller needs. Omitting ``params`` requests full
        precision (finest buckets, narrowest range).

        Note: ``mantissa=1`` is never valid (returns HTTP 500);
        ``choose_aggregation`` never emits it, so passing its output is safe.

        Args:
            coin: Symbol to fetch, e.g. ``"BTC"``, ``"ETH"``, ``"xyz:MSTR"`` (a
            HIP-3 market), or ``"@150"`` (a spot index).
            params: Aggregation parameters to spread into the request, or ``None``
            for full precision.

        Returns:
            HLAPIError: On an API-level error (e.g. an unknown symbol).
        """
        payload: dict[str, Any] = {
            "type": "l2Book",
            "coin": coin,
        }
        if params:
            payload.update(params)
        raw: Any = await self._post(payload)
        return HLL2Book.model_validate(raw)
