"""L2 book aggregation helpers.

The Hyperliquid ``l2Book`` REST endpoint caps responses at 20 levels per side
regardless of how the data is requested. Two parameters control how those 20
levels are distributed across the price range: ``nSigFigs`` rounds prices to N
significant figures (bucket width = 1 unit in the Nth significant figure), and
``mantissa`` (valid ONLY at ``nSigFigs=5``) scales that base bucket width by 2x
or 5x.

The tradeoff is fundamental: fine buckets give high price precision near the
spread but a narrow total range; coarse buckets give a wide range at low
precision. A tool computing depth-weighted imbalance at, say, 100 bps from mid
needs the 20 returned levels to *reach* 100 bps, which means choosing a coarse
enough setting -- but no coarser than necessary.

``choose_aggregation`` solves this: given the deepest band a tool needs, it
returns the highest-precision API parameters whose 20-level range still covers
that band. The ladder breakpoints are calibrated against empirical measurements
on BTC at ~$65k (see ``docs/api_spike_findings.md`` Q1).

Calibration caveat: the bps-per-bucket relationship is PRICE-DEPENDENT
(``nSigFigs`` rounds by significant figures, not absolute price), so the same
setting produces different ranges on differently-priced coins. The default
ladder is BTC-shaped; coins at very different price magnitudes need
recalibration. A future runtime probe can derive the ladder per-coin from a
quick ``l2Book`` call. For v0 the BTC ladder is the default.

This module is PURE: no I/O, no async, no network (CLAUDE.md hard rule).
"""

import math
from typing import Literal, TypedDict

from hlmcp.analytics.utils import parse_float
from hlmcp.schemas.hl_api import HLL2Book


class L2BookParams(TypedDict, total=False):
    """REST-API-shaped parameters for an ``l2Book`` request, minus ``type``/``coin``.

    Used as the return type of :func:`choose_aggregation` so the caller can
    spread these into the full request payload::

        params = choose_aggregation(max_band_bps=100)
        payload = {"type": "l2Book", "coin": "BTC", **params}

    Both fields are optional (``total=False``): omitting both ``nSigFigs`` and
    ``mantissa`` produces a full-precision request. ``mantissa`` is only valid
    when ``nSigFigs == 5`` and may only be 2 or 5 -- ``mantissa=1`` returns
    HTTP 500 (api_spike_findings.md Q1), so it is never emitted.

    Attributes:
        nSigFigs: Significant figures to round prices to (2, 3, 4, or 5).
        mantissa: Bucket-width multiplier; only valid at ``nSigFigs=5``.
    """

    nSigFigs: int
    mantissa: Literal[2, 5]


# The BTC-calibrated aggregation ladder. Each entry is
# ``(max_band_bps_covered, api_params)``: the widest bps band that setting's
# 20-level response can span, paired with the params to request it. Sorted
# ascending by covered range; ``choose_aggregation`` walks it and returns the
# first (highest-precision) entry whose range still covers the required band.
#
# Calibrated against BTC @ ~$65k from the spike (api_spike_findings.md Q1).
# Note the granularity GAP between ``nSigFigs=4`` (~30 bps) and ``nSigFigs=3``
# (~296 bps): the API offers no intermediate setting because ``mantissa`` is
# only valid at ``nSigFigs=5``. Band requirements in (30, 296] bps fall back to
# ``nSigFigs=3`` and accept coarser-than-ideal buckets; the tool layer reports
# the actual achieved bucket width so callers can reason about resolution.
_BTC_AGGREGATION_LADDER: list[tuple[float, L2BookParams]] = [
    (6.0, {"nSigFigs": 5, "mantissa": 2}),  # ~$2 buckets, ~6 bps range
    (15.0, {"nSigFigs": 5, "mantissa": 5}),  # ~$5 buckets, ~15 bps range
    (30.0, {"nSigFigs": 4}),  # ~$10 buckets, ~30 bps range
    (296.0, {"nSigFigs": 3}),  # ~$100 buckets, ~296 bps range
    (2969.0, {"nSigFigs": 2}),  # ~$1000 buckets, ~2969 bps range
]


# The full set of VALID aggregation settings, finest -> coarsest, used by the
# price-aware selection path. Unlike ``_BTC_AGGREGATION_LADDER`` (which hardcodes
# BTC-shaped bps ranges), this list carries only the settings; the bps range each
# one yields is computed from the actual price via ``estimate_bucket_bps``. Note
# the finest entry is bare ``nSigFigs=5`` (the 1x base, == the null default);
# ``mantissa=1`` is never included because it returns HTTP 500 (Q1).
_PRICE_AWARE_CANDIDATES: list[L2BookParams] = [
    {"nSigFigs": 5},  # 1x base bucket
    {"nSigFigs": 5, "mantissa": 2},  # 2x
    {"nSigFigs": 5, "mantissa": 5},  # 5x
    {"nSigFigs": 4},  # 10x
    {"nSigFigs": 3},  # 100x
    {"nSigFigs": 2},  # 1000x
]

# The l2Book 20-level cap: the total price range a setting spans is ~20 buckets.
_L2_LEVELS_PER_SIDE: int = 20


def choose_aggregation(max_band_bps: float, price: float | None = None) -> L2BookParams:
    """Pick ``l2Book`` API parameters so the 20-level response covers ``max_band_bps``.

    The endpoint returns at most 20 levels per side; their total price range
    depends on ``nSigFigs``/``mantissa`` AND on the coin's price magnitude
    (``nSigFigs`` rounds by significant figures, so a given setting is far
    coarser in bps on a $1 coin than on BTC). This returns the *highest-precision*
    setting whose 20-level range still spans at least ``max_band_bps`` from mid.

    Two modes:

    - **Price-aware (``price`` given, preferred).** Walks the valid settings
      finest -> coarsest and returns the first whose reach
      (``20 * estimate_bucket_bps(setting, price)``) is >= ``max_band_bps``. This
      is correct for ANY coin — e.g. XRP @ ~$1.12 gets ``nSigFigs=4`` for a
      100 bps band (~8.9 bps buckets), where the BTC ladder wrongly picks
      ``nSigFigs=3`` (~89 bps buckets, band collapses into one bucket).

    - **BTC-ladder fallback (``price`` is ``None``).** Walks the hardcoded,
      BTC-calibrated ``_BTC_AGGREGATION_LADDER``. Retained for back-compat and
      for coins with no available mid (the tool probes ``allMids`` and passes the
      result; a missing coin falls back here). Only accurate near BTC's price.

    Mechanism: validate ``max_band_bps > 0``; if ``price`` is given (and > 0),
    scan ``_PRICE_AWARE_CANDIDATES`` and return the first whose computed reach
    covers the band; otherwise scan the BTC ladder. Either path clamps to
    ``nSigFigs=2`` for a band beyond the coarsest rung.

    Args:
        max_band_bps: The deepest basis-point band the caller intends to compute
            against the returned book. Must be > 0.
        price: The coin's current mid price, used to compute each setting's true
            bps reach. If ``None``, the BTC-calibrated ladder is used instead.
            Must be > 0 when provided.

    Returns:
        An :class:`L2BookParams` dict to spread into the request payload. Never
        empty: the widest setting (``nSigFigs=2``) is the fallback for any band
        beyond the ladder.

    Raises:
        ValueError: If ``max_band_bps`` is not strictly positive, or ``price`` is
            provided but not strictly positive.

    Examples:
        >>> choose_aggregation(100.0)  # BTC-ladder fallback
        {'nSigFigs': 3}
        >>> choose_aggregation(100.0, price=1.12)  # price-aware (XRP-magnitude)
        {'nSigFigs': 4}
    """
    if max_band_bps <= 0:
        raise ValueError(f"max_band_bps must be > 0, got {max_band_bps}")

    if price is not None:
        if price <= 0:
            raise ValueError(f"price must be > 0 when provided, got {price}")
        for params in _PRICE_AWARE_CANDIDATES:
            reach_bps: float = _L2_LEVELS_PER_SIDE * estimate_bucket_bps(params, price)
            if reach_bps >= max_band_bps:
                return params
        # Band exceeds even the coarsest setting's reach; clamp to it.
        return {"nSigFigs": 2}

    for range_bps, ladder_params in _BTC_AGGREGATION_LADDER:
        if range_bps >= max_band_bps:
            return ladder_params

    # Band exceeds the widest ladder rung; clamp to the coarsest setting.
    return {"nSigFigs": 2}


def estimate_bucket_bps(params: L2BookParams, top_price: float) -> float:
    """Estimate the per-bucket width in basis points for a given setting.

    This is an *estimate* derived from the significant-figure arithmetic; the
    real bucket width depends on the price's exact significant-figure boundaries
    and rounding. The tool layer should still extract the *actual* bucket width
    from the response (by differencing the top two prices on a side) and report
    that as the source of truth. This helper is for sizing/capacity planning.

    Mechanism: find the price's leading-digit power of ten
    (``floor(log10(top_price))``), step down ``nSigFigs - 1`` places to get the
    base bucket width in dollars, multiply by ``mantissa`` (1 if absent), then
    convert dollars to bps via ``/ top_price * 10_000``. Returns ``0.0`` when no
    ``nSigFigs`` is set (full precision -- bucket width unknowable from price).

    Args:
        params: Aggregation parameters as returned by :func:`choose_aggregation`.
        top_price: Current top-of-book price, used to convert significant figures
            into basis points. Must be > 0 when ``nSigFigs`` is present.

    Returns:
        Approximate bucket width in basis points. Returns ``0.0`` when ``params``
        specify no aggregation (a full-precision request), signalling "unknown
        without observing the book".

    Raises:
        ValueError: If ``nSigFigs`` is present but ``top_price`` is not > 0.
    """
    if "nSigFigs" not in params:
        # Full precision -- bucket width is the coin's tick size, unknowable
        # without observing the book. Signal "unknown" with 0.0.
        return 0.0

    if top_price <= 0:
        raise ValueError(f"top_price must be > 0, got {top_price}")

    sig_figs: int = params["nSigFigs"]
    mantissa: int = params.get("mantissa", 1)  # implicit 1x when not specified

    # Bucket width in dollars at N significant figures of ``top_price`` is
    # ``10 ** (floor(log10(top_price)) - N + 1)``. For BTC @ $65k (5 digits):
    # nSigFigs=5 -> 10^(5-5+1) = $1; nSigFigs=4 -> $10; nSigFigs=3 -> $100.
    price_magnitude: int = int(math.floor(math.log10(top_price)))
    base_bucket_dollars: float = 10.0 ** (price_magnitude - sig_figs + 1)
    bucket_dollars: float = base_bucket_dollars * mantissa

    return (bucket_dollars / top_price) * 10_000


def measure_bucket_width_usd(book: HLL2Book) -> float | None:
    """Measure the ACTUAL per-bucket width in USD from a returned book.

    The measured counterpart to :func:`estimate_bucket_bps`: where the estimate
    is a plan-time guess from significant-figure arithmetic, this reads the width
    the API actually delivered. Q1 is explicit that "tools should report the
    actual bucket width achieved" -- the response is the source of truth, because
    the exact dollar step is price-dependent and only approximated by the estimate.

    Mechanism: the API buckets prices at a fixed increment ``S`` (empirically
    uniform across a whole book, even when it crosses a power-of-ten boundary --
    verified live on XRP spanning $1.12 -> $0.93 with a constant $0.01 step). So
    every level price lies on the ``S``-grid and each consecutive gap is an
    integer multiple of ``S`` (an empty bucket -- no resting orders in that
    increment -- is omitted, producing a 2S+ gap; observed live). The *smallest*
    consecutive gap across both sides is therefore ``S`` exactly, as soon as any
    two adjacent buckets are both populated (near-certain among ~20 dense levels
    at the spread). Taking the min -- rather than differencing just the top two --
    is robust to those empty-bucket gaps at the same cost.

    Args:
        book: A parsed :class:`~hlmcp.schemas.hl_api.HLL2Book` from the venue.

    Returns:
        The bucket width ``S`` in USD, or ``None`` if it cannot be measured (no
        side has two levels to difference).
    """
    gaps: list[float] = []
    for side in book.levels:
        prices: list[float] = [parse_float(level.px) for level in side]
        gaps.extend(abs(prices[i] - prices[i + 1]) for i in range(len(prices) - 1))

    # Guard against a degenerate duplicate-price pair (0.0 gap) poisoning the min;
    # aggregated levels are strictly monotonic per side, so this is defensive.
    positive_gaps: list[float] = [gap for gap in gaps if gap > 0.0]
    if not positive_gaps:
        return None
    return min(positive_gaps)


def bucket_width_to_bps(width_usd: float | None, mid_price: float) -> float | None:
    """Express a USD bucket width in basis points of the mid price.

    The bps view is the price-independent way to compare resolution across coins
    (a $10 bucket is coarse on a $60 coin, fine on BTC). Pairs with
    :func:`measure_bucket_width_usd` to yield the achieved resolution in both
    units for the response layer.

    Mechanism: ``(width_usd / mid_price) * 10_000``; short-circuits to ``None``
    when the width is unmeasurable (``None``) or ``mid_price`` is not positive.

    Args:
        width_usd: A bucket width in USD (e.g. from
            :func:`measure_bucket_width_usd`), or ``None`` if not measurable.
        mid_price: The reference mid price to convert against. Must be > 0 for a
            meaningful result.

    Returns:
        The width in basis points, or ``None`` if ``width_usd`` is ``None`` or
        ``mid_price`` is not positive.
    """
    if width_usd is None or mid_price <= 0:
        return None
    return (width_usd / mid_price) * 10_000.0
