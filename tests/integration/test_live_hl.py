"""Integration tests against the LIVE Hyperliquid info endpoint.

Marked ``@pytest.mark.integration`` (via ``pytestmark``) so they are excluded
from the default ``pytest -m "not integration"`` run. They hit the real,
no-auth public API, so they can be slow or rate-limited; run them deliberately
before releases and when changing the venue layer.

Wallet-dependent tests read a sample address from ``HL_SAMPLE_WALLETS`` (see
``.env.example``); they skip if it is unset rather than fail, so the suite is
runnable without that configuration.
"""

import pytest

from hlmcp.analytics.aggregation import choose_aggregation
from hlmcp.config import HLConfig, load_config
from hlmcp.schemas.hl_api import HLClearinghouseState, HLL2Book, HLPerpDexs
from hlmcp.venues.hyperliquid import HyperliquidPublic

pytestmark = pytest.mark.integration


def _first_sample_wallet() -> str:
    """Return the first configured sample wallet, or skip the test if none is set.

    Returns:
        The first address from ``HL_SAMPLE_WALLETS``.
    """
    config: HLConfig = load_config()
    if not config.sample_wallets:
        pytest.skip("HL_SAMPLE_WALLETS is not set; skipping wallet-dependent live test")
    return config.sample_wallets[0]


async def test_fetch_clearinghouse_state_live() -> None:
    """A real wallet's clearinghouseState parses and carries a server timestamp."""
    wallet: str = _first_sample_wallet()
    async with HyperliquidPublic() as venue:
        state: HLClearinghouseState = await venue.fetch_clearinghouse_state(wallet)

    assert isinstance(state, HLClearinghouseState)
    assert state.time > 0
    # accountValue is a decimal string; it must at least parse as a float.
    assert float(state.marginSummary.accountValue) >= 0.0


async def test_list_dexes_live_returns_hip3_deployments() -> None:
    """The live perpDexs list yields at least one HIP-3 deployment."""
    async with HyperliquidPublic() as venue:
        dexes: HLPerpDexs = await venue.list_dexes()

    assert isinstance(dexes, HLPerpDexs)
    assert len(dexes.dexes) >= 1
    assert all(d.name for d in dexes.dexes)  # every named dex has a non-empty name


async def test_l2_book_live_returns_20_levels_per_side() -> None:
    """A live BTC l2Book returns the full 20 levels on each side.

    BTC is deep enough that a band-sized aggregation fills all 20 levels; the
    20-per-side cap is the Q1 finding under test.
    """
    async with HyperliquidPublic() as venue:
        params = choose_aggregation(5.0)
        book: HLL2Book = await venue.fetch_l2_book("BTC", params)

    assert isinstance(book, HLL2Book)
    assert len(book.levels) == 2
    bids, asks = book.levels
    assert len(bids) == 20
    assert len(asks) == 20


async def test_unknown_dex_rejected_client_side_live() -> None:
    """A bogus dex is rejected client-side (ValueError), never sent to HL."""
    wallet: str = _first_sample_wallet()
    async with HyperliquidPublic() as venue:
        with pytest.raises(ValueError, match="dex"):
            await venue.fetch_clearinghouse_state(wallet, dex="definitely-not-a-real-dex")
