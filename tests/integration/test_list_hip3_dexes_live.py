"""Integration tests for the ``list_hip3_dexes`` tool against LIVE Hyperliquid.

Marked ``@pytest.mark.integration`` (via ``pytestmark``) so they are excluded
from the default ``pytest -m "not integration"`` run. They hit the real, no-auth
public ``perpDexs`` endpoint end-to-end; run them deliberately before releases.
"""

import pytest

from hlmcp.schemas.responses import ListHip3DexesResponse
from hlmcp.tools.list_hip3_dexes import compute_list_hip3_dexes
from hlmcp.venues.hyperliquid import HyperliquidPublic

pytestmark = pytest.mark.integration


async def test_list_hip3_dexes_live_returns_deployments() -> None:
    """The live catalog returns >=1 HIP-3 deployment, native HL excluded.

    Every deployment has a non-empty routing key and an asset count consistent
    with its surfaced asset list; freshness reflects local fetch time (perpDexs
    carries no server timestamp, so staleness is 0 by construction).
    """
    async with HyperliquidPublic() as venue:
        resp: ListHip3DexesResponse = await compute_list_hip3_dexes(venue)

    assert resp.n_dexes >= 1
    assert resp.n_dexes == len(resp.dexes)
    assert all(dex.name for dex in resp.dexes)  # every dex has a non-empty routing key
    assert "" not in {dex.name for dex in resp.dexes}  # native HL is excluded
    for dex in resp.dexes:
        assert dex.n_assets == len(dex.assets)
    # perpDexs has no server timestamp -> staleness is 0 by construction.
    assert resp.freshness.staleness_ms == 0
