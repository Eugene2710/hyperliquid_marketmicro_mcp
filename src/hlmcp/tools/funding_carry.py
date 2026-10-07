"""``funding_carry`` tool: current and realized funding, and carry by side, for one market.

Thin orchestration over the three layers (CLAUDE.md layering rule): fetch the
market's live ctx (predicted hourly funding, premium, prices, OI), then, in
parallel, its settled ``fundingHistory`` over the lookback and (native coins
only) the cross-venue ``predictedFundings``; run the pure funding analytics and
wrap the result into a :class:`FundingCarryResponse`.

Why the ctx is fetched first: ``fundingHistory`` answers an unknown coin with an
HTTP 500 that the venue's retry policy treats as transient (3 wasted attempts).
The ctx's universe is the authoritative symbol list, so an unknown or delisted
coin is rejected with a ``ValueError`` before any history request.

Data-age caveat: ``metaAndAssetCtxs`` has no server timestamp, so freshness is
anchored on the newest settled funding row (see :class:`FundingCarryResponse`).
Research/slow-loop grade, NOT HFT.
"""

import asyncio
import time

from hlmcp.analytics.funding import (
    MS_PER_HOUR,
    CurrentFunding,
    RealizedFunding,
    VenueRate,
    carry_for_side,
    cross_venue_rates,
    next_settlement_ms,
    realized_stats,
    summarize_current,
)
from hlmcp.schemas.hl_api import (
    HLAssetCtx,
    HLFundingHistoryEntry,
    HLMetaAndAssetCtxs,
    HLPredictedFundings,
    HLUniverseAsset,
    HLVenueFunding,
)
from hlmcp.schemas.responses import FreshnessMeta, FundingCarryResponse
from hlmcp.venues.hyperliquid import NATIVE_HL_DEX, HyperliquidPublic

# Default realized-funding window: one week of hourly settlements, which spans a
# full weekday/weekend cycle. [judgment call] 24h or 720h would also be defensible.
DEFAULT_LOOKBACK_HOURS: int = 168

# Longest allowed window: 30 days (720 settlements = 2 fundingHistory pages), which
# bounds a call to at most 4 HTTP requests.
MAX_LOOKBACK_HOURS: int = 720


def dex_for_coin(coin: str) -> str:
    """Return the dex a symbol lives on, from its prefix.

    Mechanism: HIP-3 symbols are ``"<dex>:<asset>"``; anything without a colon is
    a native HL symbol (dex ``""``).

    Args:
        coin: Symbol, e.g. ``"BTC"`` or ``"xyz:XYZ100"``.

    Returns:
        The dex name, or ``""`` for native HL.
    """
    return coin.split(":", 1)[0] if ":" in coin else NATIVE_HL_DEX


async def compute_funding_carry(
    venue: HyperliquidPublic,
    coin: str,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    *,
    now_ms: int | None = None,
) -> FundingCarryResponse:
    """Compute current funding, realized funding and carry by side for ``coin``.

    Mechanism: validate ``lookback_hours`` -> fetch the dex's
    ``metaAndAssetCtxs`` and find the coin (unknown/delisted -> ``ValueError``)
    -> concurrently fetch ``fundingHistory`` over ``[now - lookback, now]`` and,
    for native coins, ``predictedFundings`` -> ``summarize_current`` /
    ``realized_stats`` / ``carry_for_side`` / ``cross_venue_rates`` -> wrap with
    :class:`FreshnessMeta` anchored on the newest settlement.

    Args:
        venue: The read-only Hyperliquid adapter.
        coin: Perp symbol, e.g. ``"BTC"`` or ``"xyz:XYZ100"`` (HIP-3). Exact,
            case-sensitive, as HL lists it.
        lookback_hours: Realized-funding window in hours, 1..720. Defaults to
            :data:`DEFAULT_LOOKBACK_HOURS`.
        now_ms: Wall-clock (ms since epoch) for the window end, next-settlement
            and staleness; defaults to the current time. Injectable for tests.

    Returns:
        A :class:`FundingCarryResponse`.

    Raises:
        ValueError: If ``lookback_hours`` is out of range, the dex is unknown,
            or the coin is not listed / is delisted on its dex.
        HLAPIError: If an HL request fails.
    """
    if not 1 <= lookback_hours <= MAX_LOOKBACK_HOURS:
        raise ValueError(
            f"lookback_hours must be between 1 and {MAX_LOOKBACK_HOURS}, got {lookback_hours}"
        )
    fetched_at_ms: int = now_ms if now_ms is not None else int(time.time() * 1000)
    dex: str = dex_for_coin(coin)

    ctxs: HLMetaAndAssetCtxs = await venue.fetch_meta_and_asset_ctxs(dex)
    found: tuple[HLUniverseAsset, HLAssetCtx] | None = ctxs.ctx_for(coin)
    if found is None:
        raise ValueError(
            f"{coin!r} is not a listed perp on dex {dex!r}. Symbols are case-sensitive "
            "(e.g. 'BTC', 'kPEPE'); HIP-3 symbols are dex-prefixed (e.g. 'xyz:XYZ100')."
        )
    asset, ctx = found
    if asset.isDelisted:
        raise ValueError(f"{coin!r} is delisted on dex {dex!r}; it has no live funding.")

    start_ms: int = fetched_at_ms - lookback_hours * MS_PER_HOUR
    history: list[HLFundingHistoryEntry]
    predicted: HLPredictedFundings | None
    if dex == NATIVE_HL_DEX:
        history, predicted = await asyncio.gather(
            venue.fetch_funding_history(coin, start_ms, fetched_at_ms),
            venue.fetch_predicted_fundings(),
        )
    else:
        history = await venue.fetch_funding_history(coin, start_ms, fetched_at_ms)
        predicted = None

    current: CurrentFunding = summarize_current(ctx)
    realized: RealizedFunding = realized_stats(history)

    cross_venue: list[VenueRate] | None = None
    if predicted is not None:
        venues: list[tuple[str, HLVenueFunding | None]] | None = predicted.for_coin(coin)
        cross_venue = cross_venue_rates(venues) if venues is not None else None

    anchor_ms: int = (
        realized.last_settlement_ms if realized.last_settlement_ms is not None else fetched_at_ms
    )

    return FundingCarryResponse(
        coin=coin,
        dex=dex,
        current=current,
        next_settlement_ms=next_settlement_ms(fetched_at_ms),
        carry_annualized_long=carry_for_side(current.hourly_rate, "long"),
        carry_annualized_short=carry_for_side(current.hourly_rate, "short"),
        lookback_hours=lookback_hours,
        realized=realized,
        cross_venue=cross_venue,
        freshness=FreshnessMeta.from_times(server_time_ms=anchor_ms, fetched_at_ms=fetched_at_ms),
    )
