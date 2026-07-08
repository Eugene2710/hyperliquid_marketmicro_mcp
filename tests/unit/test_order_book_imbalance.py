"""Unit tests for the ``order_book_imbalance`` tool logic.

Exercise the tool's orchestration against a MOCKED venue -- no network. The fake
venue records the ``params`` of each ``fetch_l2_book`` call so we can assert the
price-aware probe/aggregation behavior (which setting was chosen, and whether the
full-precision probe book was reused instead of re-fetched). Book contents come
from recorded fixtures or small synthetic books.
"""

from collections.abc import Callable, Sequence
from typing import Any

import pytest

from hlmcp.analytics.aggregation import L2BookParams
from hlmcp.schemas.hl_api import HLL2Book, HLL2Level
from hlmcp.schemas.responses import OrderBookImbalanceResponse
from hlmcp.tools.order_book_imbalance import compute_order_book_imbalance


class _FakeVenue:
    """A stand-in for HyperliquidPublic that returns a fixed book and records calls.

    Attributes:
        calls: The ``params`` argument of every ``fetch_l2_book`` call, in order
            (``None`` for the full-precision probe).
    """

    def __init__(self, book: HLL2Book) -> None:
        """Store the book to return from every fetch and init the call log."""
        self._book: HLL2Book = book
        self.calls: list[L2BookParams | None] = []

    async def fetch_l2_book(self, coin: str, params: L2BookParams | None = None) -> HLL2Book:
        """Record the requested params and return the configured book."""
        self.calls.append(params)
        return self._book


def _make_book(
    bid_pxs: list[str], ask_pxs: list[str], *, coin: str = "TEST", time_ms: int = 1_700_000_000_000
) -> HLL2Book:
    """Build a minimal HLL2Book from bid/ask price lists (size/n are filler)."""
    bids = [HLL2Level(px=px, sz="1.0", n=1) for px in bid_pxs]
    asks = [HLL2Level(px=px, sz="1.0", n=1) for px in ask_pxs]
    return HLL2Book(coin=coin, time=time_ms, levels=[bids, asks])


async def test_probe_then_refetch_price_aware_low_price() -> None:
    """A ~$1 coin probes the price, then re-fetches at the price-appropriate setting.

    The probe (params=None) reads a mid of ~$1.125; for a 100 bps band the
    price-aware selection is nSigFigs=4 (not the BTC ladder's nSigFigs=3), so a
    second fetch goes out carrying exactly that setting.
    """
    xrp = _make_book(
        bid_pxs=["1.1250", "1.1249", "1.1248"],
        ask_pxs=["1.1251", "1.1252", "1.1253"],
        coin="XRP",
    )
    venue = _FakeVenue(xrp)

    resp = await compute_order_book_imbalance(venue, "XRP", [100.0])  # type: ignore[arg-type]

    assert venue.calls == [None, {"nSigFigs": 4}]
    assert resp.nsigfigs == 4
    assert resp.mantissa is None


async def test_reuses_probe_when_full_precision_reaches() -> None:
    """A tight band already covered by full precision is served from one fetch.

    For a $1 coin a 3 bps band needs only the finest setting (nSigFigs=5), which
    is what the probe already fetched -- so the probe book is reused and NO second
    request is made.
    """
    xrp = _make_book(bid_pxs=["1.1250", "1.1249"], ask_pxs=["1.1251", "1.1252"], coin="XRP")
    venue = _FakeVenue(xrp)

    resp = await compute_order_book_imbalance(venue, "XRP", [3.0])  # type: ignore[arg-type]

    assert venue.calls == [None]  # probe only; no re-fetch
    assert resp.nsigfigs == 5


async def test_btc_default_bands_full_response(load_json: Callable[[str], Any]) -> None:
    """A BTC book yields a well-formed response with the measured bucket width.

    Uses the recorded nSigFigs=3 fixture: mid ~$64,950, $100 buckets. The default
    four bands come back in order, and the achieved bucket width is measured from
    the response (not estimated).
    """
    book = HLL2Book.model_validate(load_json("l2book_btc_nsf3.json"))
    venue = _FakeVenue(book)

    resp = await compute_order_book_imbalance(venue, "BTC")  # type: ignore[arg-type]

    assert isinstance(resp, OrderBookImbalanceResponse)
    assert resp.coin == "BTC"
    assert resp.mid_price == pytest.approx(64_950.0)
    # Probe at $64,950 -> 100 bps deepest band -> nSigFigs=3 (re-fetched).
    assert venue.calls == [None, {"nSigFigs": 3}]
    assert resp.nsigfigs == 3
    assert resp.bucket_width_usd == pytest.approx(100.0)
    assert resp.bucket_width_bps == pytest.approx(100.0 / 64_950.0 * 1e4, abs=1e-6)
    assert resp.n_bid_levels == 20
    assert resp.n_ask_levels == 20
    assert [b.band_bps for b in resp.bands] == [10.0, 25.0, 50.0, 100.0]
    for band in resp.bands:
        assert -1.0 <= band.imbalance_ratio <= 1.0


async def test_bands_order_is_preserved(load_json: Callable[[str], Any]) -> None:
    """Bands are reported in the caller's order, not sorted."""
    book = HLL2Book.model_validate(load_json("l2book_btc_nsf3.json"))
    venue = _FakeVenue(book)
    bands: Sequence[float] = [50.0, 10.0, 100.0]

    resp = await compute_order_book_imbalance(venue, "BTC", bands)  # type: ignore[arg-type]

    assert [b.band_bps for b in resp.bands] == [50.0, 10.0, 100.0]


async def test_staleness_from_injected_now(load_json: Callable[[str], Any]) -> None:
    """Freshness is now_ms - book.time, with server/fetch timestamps materialized."""
    book = HLL2Book.model_validate(load_json("l2book_btc_nsf3.json"))
    venue = _FakeVenue(book)
    now_ms: int = book.time + 500

    resp = await compute_order_book_imbalance(venue, "BTC", [50.0], now_ms=now_ms)  # type: ignore[arg-type]

    assert resp.freshness.server_time_ms == book.time
    assert resp.freshness.fetched_at_ms == now_ms
    assert resp.freshness.staleness_ms == 500


async def test_empty_bands_raises_before_any_fetch() -> None:
    """An empty band list is a caller error, caught before any network call."""
    venue = _FakeVenue(_make_book(["1.0"], ["1.1"]))
    with pytest.raises(ValueError, match="at least one band"):
        await compute_order_book_imbalance(venue, "XRP", [])  # type: ignore[arg-type]
    assert venue.calls == []
