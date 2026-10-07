"""Integration tests for the ``funding_carry`` tool against LIVE Hyperliquid.

Marked ``@pytest.mark.integration`` (via ``pytestmark``) so they are excluded
from the default ``pytest -m "not integration"`` run. Live funding moves every
hour, so these assert INVARIANTS only (annualization arithmetic, settlement
counts, freshness range), never values.
"""

import pytest

from hlmcp.analytics.funding import HOURS_PER_YEAR, MS_PER_HOUR
from hlmcp.schemas.responses import FundingCarryResponse
from hlmcp.tools.funding_carry import compute_funding_carry
from hlmcp.venues.hyperliquid import HyperliquidPublic

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(("coin", "dex"), [("BTC", ""), ("xyz:XYZ100", "xyz")])
async def test_funding_carry_invariants(coin: str, dex: str) -> None:
    """A native and a HIP-3 coin return self-consistent funding responses.

    Annualized = hourly * 8760; long/short carry are mirror images; ~one
    settlement per lookback hour; freshness anchored on a settlement no more than
    ~1h old; cross-venue present only for the native coin.
    """
    lookback: int = 24
    async with HyperliquidPublic() as venue:
        resp: FundingCarryResponse = await compute_funding_carry(venue, coin, lookback)

    assert resp.coin == coin
    assert resp.dex == dex
    assert resp.current.annualized_simple == pytest.approx(
        resp.current.hourly_rate * HOURS_PER_YEAR
    )
    assert resp.carry_annualized_long == pytest.approx(-resp.carry_annualized_short)
    assert resp.current.mark_px > 0 and resp.current.oracle_px > 0
    assert lookback - 1 <= resp.realized.n_settlements <= lookback + 1
    assert resp.realized.last_settlement_ms == resp.freshness.server_time_ms
    # Settled data is at most ~1h old (plus slack for settlement publish lag).
    assert 0 <= resp.freshness.staleness_ms <= MS_PER_HOUR + 5 * 60_000
    assert 0 < resp.next_settlement_ms - resp.freshness.fetched_at_ms <= MS_PER_HOUR
    if dex == "":
        assert resp.cross_venue is not None
        hl = next(v for v in resp.cross_venue if v.venue == "HlPerp")
        assert hl.interval_hours == 1
    else:
        assert resp.cross_venue is None


async def test_funding_carry_max_lookback_pages() -> None:
    """720h needs two fundingHistory pages (500 + 220) and returns ~720 rows."""
    async with HyperliquidPublic() as venue:
        resp = await compute_funding_carry(venue, "BTC", 720)

    assert 719 <= resp.realized.n_settlements <= 721


@pytest.mark.parametrize("coin", ["btc", "MATIC"])
async def test_funding_carry_rejects_bad_coin_live(coin: str) -> None:
    """Wrong-case and delisted symbols raise ValueError against the live universe."""
    async with HyperliquidPublic() as venue:
        with pytest.raises(ValueError):
            await compute_funding_carry(venue, coin)
