"""Unit tests for the ``list_hip3_dexes`` tool logic.

Exercise the projection of a recorded ``perpDexs`` response into the user-facing
:class:`ListHip3DexesResponse` against a mocked venue (no network): native HL is
excluded, metadata and the asset universe are carried through, and freshness
reflects local fetch time (perpDexs has no server timestamp).
"""

from collections.abc import Callable
from typing import Any

from hlmcp.schemas.hl_api import HLPerpDexs
from hlmcp.schemas.responses import ListHip3DexesResponse
from hlmcp.tools.list_hip3_dexes import compute_list_hip3_dexes


class _FakeVenue:
    """Stand-in for HyperliquidPublic returning a fixed parsed perpDexs list."""

    def __init__(self, dexes: HLPerpDexs) -> None:
        """Store the parsed perpDexs to return from list_dexes."""
        self._dexes: HLPerpDexs = dexes

    async def list_dexes(self) -> HLPerpDexs:
        """Return the configured perpDexs list."""
        return self._dexes


async def test_projects_named_dexes_excluding_native(load_json: Callable[[str], Any]) -> None:
    """Every named HIP-3 dex is projected; the native (null) slot is excluded."""
    dexes = HLPerpDexs.model_validate(load_json("perpdexs.json"))
    venue = _FakeVenue(dexes)

    resp = await compute_list_hip3_dexes(venue, now_ms=1_700_000_000_000)  # type: ignore[arg-type]

    assert isinstance(resp, ListHip3DexesResponse)
    # n_dexes == number of non-null entries (native HL null excluded).
    assert resp.n_dexes == len(dexes.dexes)
    assert resp.n_dexes == len(resp.dexes)
    assert "" not in {d.name for d in resp.dexes}  # native HL never appears
    assert "xyz" in {d.name for d in resp.dexes}


async def test_dex_metadata_and_assets_carried_through(load_json: Callable[[str], Any]) -> None:
    """The xyz dex's metadata and asset universe are surfaced from perpDexs."""
    dexes = HLPerpDexs.model_validate(load_json("perpdexs.json"))
    venue = _FakeVenue(dexes)

    resp = await compute_list_hip3_dexes(venue, now_ms=1_700_000_000_000)  # type: ignore[arg-type]
    xyz = next(d for d in resp.dexes if d.name == "xyz")

    assert xyz.full_name == "XYZ"
    assert xyz.deployer == "0x88806a71d74ad0a510b350545c9ae490912f0888"
    assert xyz.n_assets == len(xyz.assets)
    assert xyz.n_assets > 0
    assert "xyz:MSTR" in xyz.assets  # first element of each [coin, cap] pair


async def test_freshness_reflects_local_fetch_time(load_json: Callable[[str], Any]) -> None:
    """perpDexs has no server timestamp, so staleness is 0 by construction."""
    dexes = HLPerpDexs.model_validate(load_json("perpdexs.json"))
    venue = _FakeVenue(dexes)
    now_ms = 1_700_000_000_000

    resp = await compute_list_hip3_dexes(venue, now_ms=now_ms)  # type: ignore[arg-type]

    assert resp.freshness.server_time_ms == now_ms
    assert resp.freshness.fetched_at_ms == now_ms
    assert resp.freshness.staleness_ms == 0
