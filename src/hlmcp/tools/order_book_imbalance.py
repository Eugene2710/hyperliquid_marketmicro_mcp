"""``order_book_imbalance`` tool: depth-weighted L2 order-book imbalance.

Thin orchestration over the three layers (CLAUDE.md layering rule): probe the
coin's price, pick an aggregation sized to that price, fetch the aggregated
``l2Book``, run the pure imbalance computation, and wrap the result with
freshness + achieved-resolution metadata into an
:class:`OrderBookImbalanceResponse`. No computation lives in the venue; no I/O
lives in analytics; this file is the only place the two meet.

Why the probe (the price-aware fix): ``nSigFigs`` rounds by *significant
figures*, so a given aggregation setting is far coarser in bps on a $1 coin than
on BTC. A price-blind ladder mis-sizes low-priced coins badly — e.g. XRP @ ~$1.12
would get ~89 bps buckets for a 100 bps band, collapsing the band into a single
bucket. So we fetch one full-precision book first, read the mid off the top of
book, and size the aggregation from the coin's actual price. When that
full-precision book already reaches the deepest requested band (common for tight
bands), it is reused directly and no second fetch happens.

Data-age caveat: HL REST ``l2Book`` snapshots are ~500ms stale at the median
before network latency (``docs/api_spike_findings.md`` Q3). Every response
carries :class:`FreshnessMeta`. This tool is research/slow-loop grade, NOT for
HFT or sub-second loops.
"""

import time
from collections.abc import Sequence

from hlmcp.analytics.aggregation import (
    L2BookParams,
    bucket_width_to_bps,
    choose_aggregation,
    measure_bucket_width_usd,
)
from hlmcp.analytics.imbalance import compute_imbalance, compute_mid_price
from hlmcp.schemas.hl_api import HLL2Book
from hlmcp.schemas.responses import FreshnessMeta, OrderBookImbalanceResponse
from hlmcp.venues.hyperliquid import HyperliquidPublic

# Sensible default bands if the caller specifies none: a near-spread reading
# (10 bps) out to a wider-context reading (100 bps).
DEFAULT_BANDS_BPS: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)

# The finest valid aggregation setting. Q1: a params-less (full-precision)
# request and ``nSigFigs=5`` are identical ("null / nSigFigs=5" -> $1 buckets),
# so when ``choose_aggregation`` returns exactly this, the full-precision probe
# book already reaches the deepest band and can be reused without a re-fetch.
_FINEST_PARAMS: L2BookParams = {"nSigFigs": 5}


async def compute_order_book_imbalance(
    venue: HyperliquidPublic,
    coin: str,
    bands_bps: Sequence[float] = DEFAULT_BANDS_BPS,
    *,
    now_ms: int | None = None,
) -> OrderBookImbalanceResponse:
    """Compute depth-weighted order-book imbalance for ``coin`` across ``bands_bps``.

    Orchestrates venue -> analytics -> response: probes the price to size the
    aggregation to the coin's magnitude, fetches the aggregated book, computes
    imbalance for every band from that single snapshot, and reports the ACTUAL
    bucket width achieved (measured from the response) alongside snapshot
    staleness.

    Mechanism: fetch a full-precision probe book -> read its mid ->
    ``choose_aggregation(max(bands), price=mid)`` -> reuse the probe book if that
    is already the finest setting, else re-fetch at the chosen setting ->
    ``compute_mid_price`` + ``compute_imbalance`` -> measure the delivered bucket
    width (``measure_bucket_width_usd`` / ``bucket_width_to_bps``) -> wrap with
    :class:`FreshnessMeta` (``now - book.time``) into an
    :class:`OrderBookImbalanceResponse`.

    Args:
        venue: The read-only Hyperliquid adapter to fetch books from.
        coin: Symbol, e.g. ``"BTC"``, ``"ETH"``, ``"xyz:MSTR"`` (HIP-3), ``"@150"``
            (spot index).
        bands_bps: Basis-point band half-widths to evaluate, each > 0. Order is
            preserved in the result. Defaults to :data:`DEFAULT_BANDS_BPS`.
        now_ms: Wall-clock (ms since epoch) to measure staleness against; defaults
            to the current time. Injectable so tests are deterministic.

    Returns:
        An :class:`OrderBookImbalanceResponse` with per-band imbalance, mid price,
        the achieved bucket width, level counts, and freshness metadata.

    Raises:
        ValueError: If ``bands_bps`` is empty, any band is <= 0, or a book is
            missing a side (no mid) — the last two via the analytics layer.
        HLAPIError: If an ``l2Book`` request fails (e.g. an unknown symbol).
    """
    if not bands_bps:
        raise ValueError("bands_bps must contain at least one band")

    bands: list[float] = list(bands_bps)
    max_band: float = max(bands)

    # Probe: one full-precision fetch yields the mid, which sizes the aggregation
    # to the coin's actual price (fixes price-blind over-coarsening on low-priced
    # coins like XRP). See the module docstring.
    probe_book: HLL2Book = await venue.fetch_l2_book(coin)
    probe_mid: float = compute_mid_price(probe_book)

    params: L2BookParams = choose_aggregation(max_band, price=probe_mid)

    # Reuse the probe book when full precision already reaches the deepest band;
    # otherwise re-fetch at the coarser setting the band actually needs.
    if params == _FINEST_PARAMS:
        book: HLL2Book = probe_book
    else:
        book = await venue.fetch_l2_book(coin, params)

    mid: float = compute_mid_price(book)
    imbalance_bands = compute_imbalance(book, bands)

    bucket_usd: float | None = measure_bucket_width_usd(book)
    bucket_bps: float | None = bucket_width_to_bps(bucket_usd, mid)

    fetched_at_ms: int = now_ms if now_ms is not None else int(time.time() * 1000)

    return OrderBookImbalanceResponse(
        coin=book.coin,
        mid_price=mid,
        nsigfigs=params.get("nSigFigs"),
        mantissa=params.get("mantissa"),
        bucket_width_usd=bucket_usd,
        bucket_width_bps=bucket_bps,
        n_bid_levels=len(book.levels[0]),
        n_ask_levels=len(book.levels[1]),
        bands=imbalance_bands,
        freshness=FreshnessMeta.from_times(server_time_ms=book.time, fetched_at_ms=fetched_at_ms),
    )
