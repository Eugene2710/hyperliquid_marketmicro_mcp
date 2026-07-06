"""Respx-mocked tests for the Hyperliquid venue adapter.

These exercise the adapter's REAL logic (address normalization, dex validation,
fan-out, error mapping, rate limiting, concurrency capping) while faking only the
HTTP transport via ``respx``. They are deterministic and need no network -- the
live-API checks live in ``tests/integration/test_live_hl.py``.

Every HL info request is a POST to the same URL; the endpoint is selected by the
``type`` field in the JSON body. The ``_dispatcher`` helper below routes a single
respx side-effect on that field, and optionally records the payloads seen so a
test can assert *what* was sent (e.g. a normalized wallet) or *whether* a call
went out at all (e.g. that an invalid dex short-circuits before the network).
"""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from hlmcp.config import HLConfig
from hlmcp.schemas.hl_api import HLClearinghouseState, HLL2Book, HLPerpDexs
from hlmcp.venues.errors import HLAPIError
from hlmcp.venues.hyperliquid import NATIVE_HL_DEX, HyperliquidPublic, TokenBucket

BASE_URL: str = "https://api.hyperliquid.xyz/info"

# Two canonical throwaway wallets for tests that don't hit the live API.
WALLET_A: str = "0x" + "ab" * 20
WALLET_B: str = "0x" + "cd" * 20


def _fast_config(**overrides: Any) -> HLConfig:
    """Build an HLConfig whose limiter/timeouts never slow a mocked test.

    The rate limiter and burst are set far above any test's request count so the
    token bucket never sleeps; individual tests override these when they are
    specifically exercising throttling.

    Args:
        **overrides: Fields to override on top of the fast defaults.

    Returns:
        An :class:`HLConfig` tuned for fast, deterministic mocked tests.
    """
    defaults: dict[str, Any] = {
        "sustained_rate_per_sec": 10_000.0,
        "burst_capacity": 10_000.0,
        "request_timeout_s": 5.0,
    }
    defaults.update(overrides)
    return HLConfig(**defaults)


def _dispatcher(
    *,
    perpdexs: Any = None,
    clearinghouse: Any = None,
    l2book: Any = None,
    errors: dict[str, tuple[int, str]] | None = None,
    seen: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a respx side-effect that routes by the request body's ``type``.

    Args:
        perpdexs: JSON to return for a ``perpDexs`` request.
        clearinghouse: JSON to return for a ``clearinghouseState`` request.
        l2book: JSON to return for an ``l2Book`` request.
        errors: Optional map of request ``type`` -> ``(status, body)`` to return
            an error instead of the success payload for that type.
        seen: Optional list that every decoded request payload is appended to, so
            a test can assert what was sent (and in what order).

    Returns:
        A callable ``(httpx.Request) -> httpx.Response`` suitable for
        ``respx...mock(side_effect=...)``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = json.loads(request.content)
        req_type: str = payload["type"]
        if seen is not None:
            seen.append(payload)
        if errors and req_type in errors:
            status, body = errors[req_type]
            return httpx.Response(status, text=body)
        if req_type == "perpDexs":
            return httpx.Response(200, json=perpdexs)
        if req_type == "clearinghouseState":
            return httpx.Response(200, json=clearinghouse)
        if req_type == "l2Book":
            return httpx.Response(200, json=l2book)
        return httpx.Response(500, text="")

    return handler


# --------------------------------------------------------------------------- #
# TokenBucket (pure limiter logic, no network)                                #
# --------------------------------------------------------------------------- #


def test_token_bucket_rejects_invalid_params() -> None:
    """A non-positive rate or sub-1 capacity raises immediately."""
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(rate=0, capacity=5)
    with pytest.raises(ValueError, match="capacity"):
        TokenBucket(rate=5, capacity=0)


async def test_token_bucket_allows_burst_then_throttles() -> None:
    """The first ``capacity`` acquires are instant; further ones throttle to ``rate``.

    With capacity=5 and rate=50/s, 5 tokens are free and the next 10 must accrue
    at 50/s, i.e. ~0.2s total. We assert a lower bound with margin to avoid
    flakiness on a loaded CI box.
    """
    bucket = TokenBucket(rate=50.0, capacity=5.0)
    start: float = time.monotonic()
    for _ in range(15):
        await bucket.acquire()
    elapsed: float = time.monotonic() - start
    # 10 throttled tokens / 50 per sec = 0.2s ideal; assert >= 0.12s with margin.
    assert elapsed >= 0.12


async def test_token_bucket_first_capacity_is_immediate() -> None:
    """Acquiring exactly ``capacity`` tokens from a full bucket does not block."""
    bucket = TokenBucket(rate=1.0, capacity=5.0)  # slow refill; burst must carry it
    start: float = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.1


# --------------------------------------------------------------------------- #
# Address normalization applied before sending (Q2b)                          #
# --------------------------------------------------------------------------- #


async def test_wallet_normalized_before_send(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """A noncanonical wallet is canonicalized (0x + lowercase) before the request."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(
            perpdexs=load_json("perpdexs.json"),
            clearinghouse=load_json("clearinghouse_small.json"),
            seen=seen,
        )
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        # Uppercase, no 0x prefix -- exactly the form HL would silently coerce.
        await venue.fetch_clearinghouse_state("ABCDEF0000000000000000000000000000000001")

    ch_calls = [p for p in seen if p["type"] == "clearinghouseState"]
    assert len(ch_calls) == 1
    assert ch_calls[0]["user"] == "0xabcdef0000000000000000000000000000000001"


async def test_malformed_wallet_raises_before_network(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """A wallet that is not 40 hex chars raises ValueError with no request sent."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(perpdexs=load_json("perpdexs.json"), seen=seen)
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        with pytest.raises(ValueError):
            await venue.fetch_clearinghouse_state("not-a-wallet")
    # normalize_wallet raises before dex validation, so nothing hits the network.
    assert seen == []


# --------------------------------------------------------------------------- #
# Dex validation short-circuits before the network (Q2c)                      #
# --------------------------------------------------------------------------- #


async def test_unknown_dex_raises_before_clearinghouse_call(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """An unknown dex raises ValueError, and no clearinghouseState request goes out."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(perpdexs=load_json("perpdexs.json"), seen=seen)
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        with pytest.raises(ValueError, match="dex"):
            await venue.fetch_clearinghouse_state(WALLET_A, dex="does-not-exist")
    # perpDexs was fetched (for validation), but clearinghouseState never was.
    assert any(p["type"] == "perpDexs" for p in seen)
    assert all(p["type"] != "clearinghouseState" for p in seen)


async def test_native_and_known_dex_pass_validation(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """The native ("") dex and a real HIP-3 name both validate and route through."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(
            perpdexs=load_json("perpdexs.json"),
            clearinghouse=load_json("clearinghouse_small.json"),
            seen=seen,
        )
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        await venue.fetch_clearinghouse_state(WALLET_A, dex=NATIVE_HL_DEX)
        await venue.fetch_clearinghouse_state(WALLET_A, dex="xyz")

    ch_calls = [p for p in seen if p["type"] == "clearinghouseState"]
    assert [p["dex"] for p in ch_calls] == ["", "xyz"]


# --------------------------------------------------------------------------- #
# Error mapping: 4xx bodies are plain strings, not JSON (Q2b)                  #
# --------------------------------------------------------------------------- #


async def test_4xx_raises_hlapierror_with_plain_string_body(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """A 422 surfaces as HLAPIError carrying the raw plain-string body verbatim."""
    body: str = "Failed to deserialize the JSON body into the target type"
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(
            perpdexs=load_json("perpdexs.json"),
            errors={"clearinghouseState": (422, body)},
        )
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        with pytest.raises(HLAPIError) as excinfo:
            await venue.fetch_clearinghouse_state(WALLET_A)

    assert excinfo.value.status == 422
    assert excinfo.value.body == body
    assert excinfo.value.payload["type"] == "clearinghouseState"


# --------------------------------------------------------------------------- #
# Batch fan-out returns exceptions as values (Q5)                             #
# --------------------------------------------------------------------------- #


async def test_batch_returns_exceptions_as_values(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """One failing wallet is captured as a value; the others still return state."""
    perpdexs: Any = load_json("perpdexs.json")
    clearinghouse: Any = load_json("clearinghouse_small.json")

    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = json.loads(request.content)
        if payload["type"] == "perpDexs":
            return httpx.Response(200, json=perpdexs)
        if payload["type"] == "clearinghouseState":
            if payload["user"] == WALLET_B:  # this one fails
                return httpx.Response(422, text="bad wallet")
            return httpx.Response(200, json=clearinghouse)
        return httpx.Response(500, text="")

    respx_mock.post(BASE_URL).mock(side_effect=handler)
    async with HyperliquidPublic(config=_fast_config()) as venue:
        results = await venue.fetch_clearinghouse_states_batch([WALLET_A, WALLET_B])

    assert set(results) == {WALLET_A, WALLET_B}
    assert isinstance(results[WALLET_A], HLClearinghouseState)
    assert isinstance(results[WALLET_B], HLAPIError)


async def test_batch_unknown_dex_raises_once_before_fanout(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """An unknown dex raises up front; no per-wallet clearinghouse calls are made."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(perpdexs=load_json("perpdexs.json"), seen=seen)
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        with pytest.raises(ValueError, match="dex"):
            await venue.fetch_clearinghouse_states_batch([WALLET_A, WALLET_B], dex="nope")
    assert all(p["type"] != "clearinghouseState" for p in seen)


# --------------------------------------------------------------------------- #
# Semaphore caps concurrency (Q5)                                             #
# --------------------------------------------------------------------------- #


async def test_semaphore_caps_in_flight_requests() -> None:
    """No more than ``max_concurrency`` requests are in flight at once.

    We patch the HTTP client's ``post`` with an async stub that tracks the live
    in-flight count while it "runs". With 12 concurrent ``_post`` calls and a cap
    of 3 (and a burst large enough that the rate limiter never blocks), the
    observed peak must be exactly 3.
    """
    config = _fast_config(max_concurrency=3)
    venue = HyperliquidPublic(config=config)
    in_flight: int = 0
    peak: int = 0

    async def fake_post(url: str, json: dict[str, Any]) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, json={"ok": True})

    venue._http.post = fake_post  # type: ignore[method-assign]
    try:
        await asyncio.gather(*(venue._post({"type": "x"}) for _ in range(12)))
    finally:
        await venue.aclose()

    assert peak == 3


# --------------------------------------------------------------------------- #
# perpDexs discovery + caching                                                #
# --------------------------------------------------------------------------- #


async def test_list_dexes_parses_and_excludes_native_null(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """list_dexes parses the null-first response; .dexes drops the native slot."""
    respx_mock.post(BASE_URL).mock(side_effect=_dispatcher(perpdexs=load_json("perpdexs.json")))
    async with HyperliquidPublic(config=_fast_config()) as venue:
        dexes: HLPerpDexs = await venue.list_dexes()

    assert len(dexes.dexes) >= 1
    assert "xyz" in {d.name for d in dexes.dexes}
    # The leading null (native HL) is retained in .root but excluded from .dexes.
    assert dexes.root[0] is None


async def test_list_dexes_is_cached_within_ttl(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """A second list_dexes within the TTL is served from cache (one network call)."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(perpdexs=load_json("perpdexs.json"), seen=seen)
    )
    async with HyperliquidPublic(config=_fast_config(dex_cache_ttl_s=300.0)) as venue:
        await venue.list_dexes()
        await venue.list_dexes()

    assert sum(1 for p in seen if p["type"] == "perpDexs") == 1


# --------------------------------------------------------------------------- #
# l2Book fetch                                                                 #
# --------------------------------------------------------------------------- #


async def test_fetch_l2_book_sends_params_and_parses(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """Aggregation params are spread into the request and the book parses to HLL2Book."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(l2book=load_json("l2book_btc_nsf5.json"), seen=seen)
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        book: HLL2Book = await venue.fetch_l2_book("BTC", {"nSigFigs": 5, "mantissa": 2})

    assert isinstance(book, HLL2Book)
    assert len(book.levels) == 2
    l2_calls = [p for p in seen if p["type"] == "l2Book"]
    assert l2_calls[0]["coin"] == "BTC"
    assert l2_calls[0]["nSigFigs"] == 5
    assert l2_calls[0]["mantissa"] == 2


async def test_fetch_l2_book_without_params_omits_aggregation(
    respx_mock: respx.MockRouter, load_json: Callable[[str], Any]
) -> None:
    """Omitting params sends only type+coin (full-precision request)."""
    seen: list[dict[str, Any]] = []
    respx_mock.post(BASE_URL).mock(
        side_effect=_dispatcher(l2book=load_json("l2book_btc_nsf5.json"), seen=seen)
    )
    async with HyperliquidPublic(config=_fast_config()) as venue:
        await venue.fetch_l2_book("BTC")

    l2_calls = [p for p in seen if p["type"] == "l2Book"]
    assert set(l2_calls[0]) == {"type", "coin"}


# --------------------------------------------------------------------------- #
# HTTP client lifecycle (owned vs borrowed)                                   #
# --------------------------------------------------------------------------- #


async def test_aclose_closes_owned_client() -> None:
    """A client the adapter created is closed on aclose()."""
    venue = HyperliquidPublic(config=_fast_config())
    await venue.aclose()
    assert venue._http.is_closed


async def test_aclose_leaves_borrowed_client_open() -> None:
    """A caller-supplied client is NOT closed by the adapter (the caller owns it)."""
    client = httpx.AsyncClient()
    venue = HyperliquidPublic(config=_fast_config(), http_client=client)
    await venue.aclose()
    assert not client.is_closed
    await client.aclose()
