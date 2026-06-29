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


def choose_aggregation(max_band_bps: float) -> L2BookParams:
    """Pick ``l2Book`` API parameters so the 20-level response covers ``max_band_bps``.

    The endpoint returns at most 20 levels per side; their total price range
    depends on ``nSigFigs``/``mantissa``. This returns the *highest-precision*
    setting whose 20-level range still spans at least ``max_band_bps`` from the
    mid price, so a tool computing imbalance at several bands (e.g. 10/25/50/100
    bps) gets data that reaches the deepest band without wasting precision near
    the spread.

    Mechanism: walk ``_BTC_AGGREGATION_LADDER`` ascending by range and return the
    first (finest) entry whose covered range is >= ``max_band_bps``; clamp to
    ``nSigFigs=2`` for any band beyond the ladder.

    Calibration note: the ladder is BTC-shaped. ``nSigFigs`` rounds by
    significant figures, so the bps-per-bucket ratio shifts with price
    magnitude; coins very different from BTC's price need a per-coin probe.

    Args:
        max_band_bps: The deepest basis-point band the caller intends to compute
            against the returned book. Must be > 0.

    Returns:
        An :class:`L2BookParams` dict to spread into the request payload. Never
        empty: the widest setting (``nSigFigs=2``) is the fallback for any band
        beyond the ladder.

    Raises:
        ValueError: If ``max_band_bps`` is not strictly positive.

    Examples:
        >>> choose_aggregation(10.0)
        {'nSigFigs': 5, 'mantissa': 5}
        >>> choose_aggregation(100.0)
        {'nSigFigs': 3}
    """
    if max_band_bps <= 0:
        raise ValueError(f"max_band_bps must be > 0, got {max_band_bps}")

    for range_bps, params in _BTC_AGGREGATION_LADDER:
        if range_bps >= max_band_bps:
            return params

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
