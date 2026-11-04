"""``order_book_imbalance`` tool: depth-weighted L2 order-book imbalance.

Thin orchestration over the three layers (CLAUDE.md layering rule): validate a
perp symbol against its dex's universe, probe the coin's price, pick an
aggregation sized to that price, fetch the aggregated
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
from hlmcp.schemas.hl_api import HLL2Book, HLMeta, HLUniverseAsset
from hlmcp.schemas.responses import FreshnessMeta, OrderBookImbalanceResponse
from hlmcp.tools.funding_carry import dex_for_coin
from hlmcp.venues.hyperliquid import HyperliquidPublic

# Sensible default bands if the caller specifies none: a near-spread reading
# (10 bps) out to a wider-context reading (100 bps).
DEFAULT_BANDS_BPS: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)

# The finest valid aggregation setting. Q1: a params-less (full-precision)
# request and ``nSigFigs=5`` are identical ("null / nSigFigs=5" -> $1 buckets),
# so when ``choose_aggregation`` returns exactly this, the full-precision probe
# book already reaches the deepest band and can be reused without a re-fetch.
_FINEST_PARAMS: L2BookParams = {"nSigFigs": 5}


def is_spot_symbol(coin: str) -> bool:
    """Return whether ``coin`` names a spot market rather than a perp.

    Mechanism: spot books are addressed as ``"@<index>"`` or ``"BASE/QUOTE"``
    (``docs/api_spike_findings.md`` Q7); perps never contain ``@`` or ``/``.

    Args:
        coin: Symbol as passed to the tool.

    Returns:
        ``True`` for a spot symbol, ``False`` for a native or HIP-3 perp.
    """
    return coin.startswith("@") or "/" in coin


def _find_market(meta: HLMeta, coin: str) -> HLUniverseAsset | None:
    """Return the market named exactly ``coin`` from one dex's list, or ``None``.

    Mechanism: go through the list in order and return the first market whose
    name matches exactly (case-sensitive); if none matches, return ``None``.

    Args:
        meta: One dex's market list.
        coin: The symbol to find.

    Returns:
        The matching market, or ``None`` if the dex does not list it.
    """
    for asset in meta.universe:
        if asset.name == coin:
            return asset
    return None


async def _require_listed_perp(venue: HyperliquidPublic, coin: str) -> None:
    """Raise unless ``coin`` is a perp that is currently trading on its dex.

    Done before any ``l2Book`` call because HL answers an unknown symbol there
    with an HTTP 500, which the retry policy treats as temporary: a typo would
    cost three attempts (~2s) and end in an unclear ``HLAPIError``
    (``docs/api_spike_findings.md`` Q7).

    Mechanism:
        1. Work out the dex from the symbol (``"xyz:XYZ100"`` -> ``"xyz"``;
           no prefix -> native HL).
        2. Look the symbol up in that dex's saved market list
           (:meth:`~hlmcp.venues.hyperliquid.HyperliquidPublic.fetch_perp_universe`,
           reused for up to 5 minutes).
        3. If it is missing or marked delisted, the saved list may just be out
           of date, so download a fresh one and look again.
        4. Still missing or delisted: raise ``ValueError``.

    Args:
        venue: The read-only Hyperliquid adapter.
        coin: A perp symbol, e.g. ``"BTC"`` or ``"xyz:XYZ100"``.

    Raises:
        ValueError: If the dex is unknown, or the coin is not listed / is
            delisted on its dex.
        HLAPIError: If downloading the market list fails.
    """
    dex: str = dex_for_coin(coin)
    asset: HLUniverseAsset | None = _find_market(await venue.fetch_perp_universe(dex), coin)
    if asset is None or asset.isDelisted:
        asset = _find_market(await venue.fetch_perp_universe(dex, refresh=True), coin)
    if asset is None:
        raise ValueError(
            f"{coin!r} is not a listed perp on dex {dex!r}. Symbols are case-sensitive "
            "(e.g. 'BTC', 'kPEPE'); HIP-3 symbols are dex-prefixed (e.g. 'xyz:XYZ100'); "
            "spot books are '@<index>' or 'BASE/QUOTE'."
        )
    if asset.isDelisted:
        raise ValueError(f"{coin!r} is delisted on dex {dex!r}; it has no live book.")


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

    Mechanism: validate a perp symbol against its dex's universe (spot symbols
    skip this) -> fetch a full-precision probe book -> read its mid ->
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
        ValueError: If ``bands_bps`` is empty; if a perp symbol's dex is unknown
            or the symbol is not listed / is delisted (raised before any
            ``l2Book`` call); or, via the analytics layer, if any band is <= 0
            or a book is missing a side (no mid).
        HLAPIError: If a request fails (for a spot symbol, this includes an
            unknown one, which is not pre-validated).
    """
    if not bands_bps:
        raise ValueError("bands_bps must contain at least one band")

    if not is_spot_symbol(coin):
        await _require_listed_perp(venue, coin)

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
