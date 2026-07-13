"""``list_hip3_dexes`` tool: the HIP-3 deployment catalog.

Thin orchestration over the venue's cached ``perpDexs`` discovery: fetch the
deployment list, project each entry into a user-facing :class:`DexInfo` (routing
key + human metadata + asset universe), and wrap in a
:class:`ListHip3DexesResponse`. Native HL (the null-first / empty-string dex) is
excluded — this surfaces only the named HIP-3 deployments a caller can route to
(via the ``dex`` argument of ``whale_position_monitor``).

Freshness caveat: the ``perpDexs`` response carries no server timestamp, so the
attached :class:`FreshnessMeta` reflects the local fetch time only (staleness 0).
The list changes rarely and is cached ~5 min at the venue (Q2c), so age is not a
meaningful signal here; the field is present for response-shape uniformity.
"""

import time

from hlmcp.schemas.hl_api import HLPerpDex, HLPerpDexs
from hlmcp.schemas.responses import DexInfo, FreshnessMeta, ListHip3DexesResponse
from hlmcp.venues.hyperliquid import HyperliquidPublic


def _dex_to_info(dex: HLPerpDex) -> DexInfo:
    """Project one raw :class:`HLPerpDex` into a user-facing :class:`DexInfo`.

    Mechanism: copy the metadata fields through and derive the asset universe from
    ``assetToStreamingOiCap`` (a list of ``[coin, cap]`` string pairs) by taking
    the first element of each pair; a missing/empty cap list yields no assets.

    Args:
        dex: A parsed :class:`~hlmcp.schemas.hl_api.HLPerpDex` from ``perpDexs``.

    Returns:
        A :class:`DexInfo` with the routing key, metadata, and market symbols.
    """
    # assetToStreamingOiCap is [[coin, cap], ...]; the coin is the first element.
    # Guard against a malformed empty inner pair so a bad entry can't crash the tool.
    assets: list[str] = [pair[0] for pair in (dex.assetToStreamingOiCap or []) if pair]
    return DexInfo(
        name=dex.name,
        full_name=dex.fullName,
        deployer=dex.deployer,
        oracle_updater=dex.oracleUpdater,
        fee_recipient=dex.feeRecipient,
        n_assets=len(assets),
        assets=assets,
    )


async def compute_list_hip3_dexes(
    venue: HyperliquidPublic, *, now_ms: int | None = None
) -> ListHip3DexesResponse:
    """List the named HIP-3 deployments known to Hyperliquid.

    Orchestrates venue -> response: fetch the (cached) ``perpDexs`` list, project
    each named deployment into a :class:`DexInfo`, and wrap with fetch-time
    :class:`FreshnessMeta`. Native HL (the null first element) is excluded.

    Mechanism: ``venue.list_dexes()`` -> ``.dexes`` (drops the native-HL null) ->
    ``_dex_to_info`` per entry -> :class:`ListHip3DexesResponse`. Freshness uses
    ``now_ms`` for both timestamps because ``perpDexs`` carries no server time.

    Args:
        venue: The read-only Hyperliquid adapter.
        now_ms: Wall-clock (ms since epoch) for the freshness stamp; defaults to
            the current time. Injectable so tests are deterministic.

    Returns:
        A :class:`ListHip3DexesResponse` with one :class:`DexInfo` per HIP-3
        deployment, in API order.

    Raises:
        HLAPIError: If the ``perpDexs`` discovery request fails.
    """
    dexes: HLPerpDexs = await venue.list_dexes()
    infos: list[DexInfo] = [_dex_to_info(dex) for dex in dexes.dexes]

    fetched_at_ms: int = now_ms if now_ms is not None else int(time.time() * 1000)
    # perpDexs has no server timestamp; freshness reflects local fetch time only.
    freshness: FreshnessMeta = FreshnessMeta.from_times(
        server_time_ms=fetched_at_ms, fetched_at_ms=fetched_at_ms
    )

    return ListHip3DexesResponse(dexes=infos, n_dexes=len(infos), freshness=freshness)
