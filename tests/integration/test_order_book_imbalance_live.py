"""Integration tests for the ``order_book_imbalance`` tool against LIVE Hyperliquid.

Marked ``@pytest.mark.integration`` (via ``pytestmark``) so they are excluded
from the default ``pytest -m "not integration"`` run. They hit the real, no-auth
public API end-to-end (probe -> aggregate -> compute -> response), so they can be
slow or rate-limited; run them deliberately before releases.

Coverage spans the full price-magnitude range on purpose -- BTC (~$62k, 5 digits)
down through a sub-dollar coin (DOGE ~$0.07) -- because the whole point of the
price-aware probe is that resolution must hold at EVERY magnitude, not just BTC.
"""

import pytest

from hlmcp.schemas.responses import OrderBookImbalanceResponse
from hlmcp.tools.order_book_imbalance import compute_order_book_imbalance
from hlmcp.venues.hyperliquid import HyperliquidPublic

pytestmark = pytest.mark.integration

# Real HL native-perp symbols spanning magnitudes from 5-digit to sub-dollar.
# Verified present in the live ``allMids`` snapshot at authoring time.
_COINS_ACROSS_MAGNITUDES: list[str] = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE"]
_BANDS: list[float] = [10.0, 25.0, 50.0, 100.0]


@pytest.mark.parametrize("coin", _COINS_ACROSS_MAGNITUDES)
async def test_order_book_imbalance_well_formed_at_every_magnitude(coin: str) -> None:
    """The tool returns a self-consistent response for coins at every price magnitude.

    Universal properties (magnitude-independent): mid > 0, bands echoed in order,
    imbalance ratios in range, a measurable bucket that is finer than the deepest
    band (so the band spans more than one bucket), and internally consistent,
    recent freshness. The magnitude-specific regression guard for the XRP-class
    over-coarsening bug lives in the dedicated test below.
    """
    async with HyperliquidPublic() as venue:
        resp: OrderBookImbalanceResponse = await compute_order_book_imbalance(venue, coin, _BANDS)

    assert resp.coin == coin
    assert resp.mid_price > 0
    assert [b.band_bps for b in resp.bands] == _BANDS
    for band in resp.bands:
        assert -1.0 <= band.imbalance_ratio <= 1.0
    # A measurable bucket, finer than the deepest band: the price-aware probe must
    # never collapse the whole band into a single bucket at any magnitude.
    assert resp.bucket_width_bps is not None
    assert 0.0 < resp.bucket_width_bps < max(_BANDS)
    # Freshness is internally consistent and the snapshot is recent (< 1 min old).
    assert resp.freshness.staleness_ms == (
        resp.freshness.fetched_at_ms - resp.freshness.server_time_ms
    )
    assert resp.freshness.staleness_ms < 60_000


async def test_order_book_imbalance_low_price_regression_live() -> None:
    """Regression guard for the BTC-ladder bug on a ~$1 coin (XRP).

    A price-blind ladder maps a 100 bps band to nSigFigs=3, which on XRP is ~89
    bps per bucket -- the band nearly collapses into ONE bucket (and 89 < 100
    would sneak past the generic well-formedness check above). The price-aware
    probe must instead choose a strictly finer setting (nSigFigs >= 4, ~8.9 bps
    buckets), so this asserts the specific setting, not just "finer than the band".
    """
    async with HyperliquidPublic() as venue:
        resp: OrderBookImbalanceResponse = await compute_order_book_imbalance(venue, "XRP", _BANDS)

    assert 0.1 < resp.mid_price < 10.0  # sanity: XRP is a low-priced coin
    assert resp.nsigfigs is not None and resp.nsigfigs >= 4  # NOT the ladder's nSigFigs=3
    assert resp.bucket_width_bps is not None and resp.bucket_width_bps < 30.0
