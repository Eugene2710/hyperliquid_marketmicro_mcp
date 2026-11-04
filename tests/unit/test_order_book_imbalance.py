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
from hlmcp.schemas.hl_api import HLL2Book, HLL2Level, HLMeta, HLMetaAndAssetCtxs
from hlmcp.schemas.responses import OrderBookImbalanceResponse
from hlmcp.tools.order_book_imbalance import compute_order_book_imbalance, is_spot_symbol
from tests.conftest import load_fixture


class _FakeVenue:
    """A stand-in for HyperliquidPublic that returns a fixed book and records calls.

    Market lists come from the recorded ``metaAndAssetCtxs`` fixtures (native:
    BTC, ETH, XRP trading, MATIC delisted; ``xyz``), so symbol checks run against
    real data.

    Attributes:
        calls: The ``params`` argument of every ``fetch_l2_book`` call, in order
            (``None`` for the full-precision probe).
        universe_calls: ``(dex, refresh)`` for every ``fetch_perp_universe`` call.
    """

    def __init__(self, book: HLL2Book, *, stale: dict[str, HLMeta] | None = None) -> None:
        """Store the book and market lists, and init the call logs.

        Args:
            book: The book every ``fetch_l2_book`` returns.
            stale: Optional per-dex list returned when ``refresh`` is False, to
                simulate a saved copy that predates a listing change. A
                ``refresh=True`` call always gets the fixture (the "fresh" list).
        """
        self._book: HLL2Book = book
        self._fresh: dict[str, HLMeta] = {
            "": HLMetaAndAssetCtxs.model_validate(load_fixture("meta_and_asset_ctxs.json")).meta,
            "xyz": HLMetaAndAssetCtxs.model_validate(
                load_fixture("meta_and_asset_ctxs_xyz.json")
            ).meta,
        }
        self._stale: dict[str, HLMeta] = stale or {}
        self.calls: list[L2BookParams | None] = []
        self.universe_calls: list[tuple[str, bool]] = []

    async def fetch_l2_book(self, coin: str, params: L2BookParams | None = None) -> HLL2Book:
        """Record the requested params and return the configured book."""
        self.calls.append(params)
        return self._book

    async def fetch_perp_universe(self, dex: str = "", *, refresh: bool = False) -> HLMeta:
        """Record the call; return the stale list if set and not refreshing, else the fixture.

        Raises:
            ValueError: For a dex with no fixture, as the real venue does.
        """
        self.universe_calls.append((dex, refresh))
        if dex not in self._fresh:
            raise ValueError(f"Unknown dex {dex!r}")
        if not refresh and dex in self._stale:
            return self._stale[dex]
        return self._fresh[dex]


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
    assert venue.universe_calls == []


# --------------------------------------------------------------------------
# Symbol validation (before any l2Book call)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("coin", ["btc", "NOT_A_COIN", "xyz:NOPE"])
async def test_unknown_perp_raises_before_any_book_fetch(coin: str) -> None:
    """Unknown symbols (incl. wrong case) raise ValueError; l2Book is never called.

    Live, an unknown symbol on l2Book is an HTTP 500 that the retry policy
    treats as transient, so it would cost three attempts and an opaque
    HLAPIError instead of a message naming the problem.
    """
    venue = _FakeVenue(_make_book(["1.0"], ["1.1"]))
    with pytest.raises(ValueError, match="not a listed perp"):
        await compute_order_book_imbalance(venue, coin)  # type: ignore[arg-type]
    assert venue.calls == []


async def test_delisted_perp_raises_before_any_book_fetch() -> None:
    """A delisted market (MATIC in the fixture) has no live book."""
    venue = _FakeVenue(_make_book(["1.0"], ["1.1"]))
    with pytest.raises(ValueError, match="delisted"):
        await compute_order_book_imbalance(venue, "MATIC")  # type: ignore[arg-type]
    assert venue.calls == []


async def test_unknown_dex_propagates_before_any_book_fetch() -> None:
    """An unknown dex prefix surfaces the venue's ValueError."""
    venue = _FakeVenue(_make_book(["1.0"], ["1.1"]))
    with pytest.raises(ValueError, match="dex"):
        await compute_order_book_imbalance(venue, "nodex:BTC")  # type: ignore[arg-type]
    assert venue.calls == []


async def test_hip3_symbol_is_validated_on_its_own_dex() -> None:
    """A dex-prefixed symbol is looked up in that dex's list (saved copy, no refresh)."""
    venue = _FakeVenue(_make_book(["27950"], ["27951"], coin="xyz:XYZ100"))
    resp = await compute_order_book_imbalance(venue, "xyz:XYZ100", [3.0])  # type: ignore[arg-type]
    assert venue.universe_calls == [("xyz", False)]
    assert resp.coin == "xyz:XYZ100"


@pytest.mark.parametrize("coin", ["@150", "PURR/USDC"])
async def test_spot_symbols_skip_perp_validation(coin: str) -> None:
    """Spot books are not in the perp universe, so they go straight to l2Book."""
    venue = _FakeVenue(_make_book(["1.1250"], ["1.1251"], coin=coin))
    await compute_order_book_imbalance(venue, coin, [3.0])  # type: ignore[arg-type]
    assert venue.universe_calls == []
    assert venue.calls == [None]


@pytest.mark.parametrize(
    ("coin", "spot"),
    [("@150", True), ("PURR/USDC", True), ("BTC", False), ("xyz:XYZ100", False), ("kPEPE", False)],
)
def test_is_spot_symbol(coin: str, spot: bool) -> None:
    """Spot = '@<index>' or 'BASE/QUOTE'; native and HIP-3 perps are not spot."""
    assert is_spot_symbol(coin) is spot


# --------------------------------------------------------------------------
# Saved market list: refresh once before rejecting
# --------------------------------------------------------------------------


def _native_meta() -> HLMeta:
    """Return the recorded native market list."""
    return HLMetaAndAssetCtxs.model_validate(load_fixture("meta_and_asset_ctxs.json")).meta


async def test_listed_coin_uses_saved_list_without_refresh() -> None:
    """A coin found in the saved list costs one lookup and no fresh download."""
    venue = _FakeVenue(_make_book(["1.1250"], ["1.1251"], coin="XRP"))
    await compute_order_book_imbalance(venue, "XRP", [3.0])  # type: ignore[arg-type]
    assert venue.universe_calls == [("", False)]


async def test_unknown_coin_refreshes_once_then_raises() -> None:
    """A miss is re-checked against a fresh list exactly once before rejecting."""
    venue = _FakeVenue(_make_book(["1.0"], ["1.1"]))
    with pytest.raises(ValueError, match="not a listed perp"):
        await compute_order_book_imbalance(venue, "NOT_A_COIN")  # type: ignore[arg-type]
    assert venue.universe_calls == [("", False), ("", True)]


async def test_new_listing_missing_from_saved_list_is_accepted() -> None:
    """A market listed after the saved copy was taken is found on refresh, not rejected."""
    fresh = _native_meta()
    stale = HLMeta(universe=[a for a in fresh.universe if a.name != "XRP"])
    venue = _FakeVenue(_make_book(["1.1250"], ["1.1251"], coin="XRP"), stale={"": stale})

    resp = await compute_order_book_imbalance(venue, "XRP", [3.0])  # type: ignore[arg-type]

    assert venue.universe_calls == [("", False), ("", True)]
    assert resp.coin == "XRP"


async def test_relisting_marked_delisted_in_saved_list_is_accepted() -> None:
    """A saved copy saying 'delisted' is re-checked; the fresh list wins."""
    fresh = _native_meta()
    stale = HLMeta(
        universe=[
            a.model_copy(update={"isDelisted": True}) if a.name == "XRP" else a
            for a in fresh.universe
        ]
    )
    venue = _FakeVenue(_make_book(["1.1250"], ["1.1251"], coin="XRP"), stale={"": stale})

    await compute_order_book_imbalance(venue, "XRP", [3.0])  # type: ignore[arg-type]

    assert venue.universe_calls == [("", False), ("", True)]
