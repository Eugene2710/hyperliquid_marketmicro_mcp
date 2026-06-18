"""
L2 book aggregation helpers.

The Hyperliquid `l2Book` REST endpoint caps responses at 20 levels per side regardless of how the data is requested.
Two parameters control how those 20 levels are distributed across the price range: `nSigFigs` rounds prices to N
significant figures (bucket width = 1 unit in the Nth sig fig), and `mantissa` (only valid at `nSigFigs=5`) scales that
base bucket width by 2× or 5×.

The tradeoff this creates is fundamental: fine buckets give high price precision near the spread but narrow range;
coarse buckets give wide range at low precision. A tool computing depth-weighted imbalance at 100 bps from mid needs
the 20 levels to *reach* 100 bps, which means choosing a coarse enough setting — but no coarser than necessary.

`choose_aggregation` solves this: given the deepest band a tool needs to compute, it returns the highest-precision API
parameters whose 20-level range still covers that band. The ladder breakpoints are calibrated against empirical
measurements on BTC at ~$65k, the BTC price in June 2026 (see docs/api_spike_findings.md Q1).

Note that the bps-per-bucket relationship is price-dependent. The default ladder is BTC-shaped. For coins with very
different price magnitudes (small spot tokens under $1, large indices), the ladder may need recalibration.
A future runtime probe can derive the ladder per-coin from a quick `l2Book` call; for v0, the BTC ladder is the default
and per-coin overrides can be added as needed.
"""
from typing import Any, Literal, TypedDict
import math


class L2BookParams(TypedDict, total=False):
    """
    REST-API-shaped parameters for an `l2Book` request, minus `type` and `coin`.

    Used as the return type of `choose_aggregation` so the caller can spread these into the full request payload:

        params = choose_aggregation(max_band_bps=100)
        payload = {"type": "l2Book", "coin": "BTC", **params}

    All fields are optional: omitting both `nSigFigs` and `mantissa` produce a full-precision request.
    """
    nSigFigs: int
    mantissa: Literal[2, 5]


# Calibrated against BTC at ~$65k from the API spike (see docs/api_spike_findings.md Q1).
# Each entry maps "the maximum bps band this setting can serve" → "the params to use".
# Sorted ascending by range; `choose_aggregation` walks the ladder and picks  first entry whose range >= required band.
_BTC_AGGREGATION_LADDER: list[tuple[float, L2BookParams]] = [
    (6.0,    {"nSigFigs": 5, "mantissa": 2}),  # ~$2 buckets, ~6 bps range
    (15.0,   {"nSigFigs": 5, "mantissa": 5}),  # ~$5 buckets, ~15 bps range
    (30.0,   {"nSigFigs": 4}),                 # ~$10 buckets, ~30 bps range
    (296.0,  {"nSigFigs": 3}),                 # ~$100 buckets, ~296 bps range
    (2969.0, {"nSigFigs": 2}),                 # ~$1000 buckets, ~2969 bps range
]

"""
The aggregation ladder. Each entry is (max_band_bps_covered, api_params).

Note the gap between `nSigFigs=4` (~30 bps) and `nSigFigs=3` (~296 bps): the API provides no intermediate setting. 
Band requirements in (30, 296] bps will use nSigFigs=3 and get coarser-than-ideal bucket widths. 
The tool's response should report the actual bucket width achieved so callers can reason about resolution.
"""
def choose_aggregation(max_band_bps: float) -> L2BookParams:
    """
    Pick l2Book API parameters so the 20-level response covers `max_band_bps`.

    The Hyperliquid `l2Book` endpoint returns at most 20 levels per side. The 20 levels' total price range depends on
    the `nSigFigs` and `mantissa` parameters:
        tighter aggregation gives finer per-bucket precision but a narrower total range;
        looser aggregation gives wider range at the cost of precision.

    This function picks the *highest-precision* setting whose 20-level range still spans at least `max_band_bps` from
    the mid price. That way, a tool computing imbalance at multiple bands (e.g. 10, 25, 50, 100 bps) gets data that
    reaches the deepest band without wasting precision near the spread.

    Calibration note: the ladder is BTC-shaped. The bps-per-bucket ratio at a given `nSigFigs` depends on the coin's
    price magnitude (since `nSigFigs`rounds by significant figures, not absolute price), so the same setting produces
    different ranges on differently-priced coins. For coins very different from BTC's price magnitude,
    consider a runtime per-coin probe.

    Args:
        max_band_bps: The deepest basis-point band the caller intends to
                      compute against the returned book. Must be > 0.

    Returns:
        A `L2BookParams` dict to spread into the request payload. Empty dict
        is never returned; the widest setting (nSigFigs=2) is the fallback.

    Examples:
        >>> choose_aggregation(10.0)
        {'nSigFigs': 5, 'mantissa': 5}

        >>> choose_aggregation(100.0)
        {'nSigFigs': 3}
    """
    if max_band_bps <= 0:
        raise ValueError(f'max_band_bps must be > 0, got {max_band_bps}')

    for range_bps, params in _BTC_AGGREGATION_LADDER:
        if range_bps <= max_band_bps:
            return params

    # Widest available setting; clamp for any band beyond the ladder.
    return {"nSigFigs": 2}

def estimate_bucket_bps(params: L2BookParams, top_price: float) -> float:
    """
    Estimate the per-bucket width in basis points for a given setting.

    This is an *estimate* derived from the ladder calibration; the actual bucket width depends on the price's exact
    significant-figure boundaries. The tool layer should still extract the *real* bucket width from the response
    (by comparing the top two bid prices) and report that in the response metadata.
    This helper exists for sizing and capacity-planning purposes, not as a source of truth.

    Args:
        params: The aggregation parameters as returned by `choose_aggregation`.
        top_price: The current top-of-book price (used to convert significant
                   figures into bps).

    Returns:
        Approximate bucket width in basis points. Returns 0.0 if the params
        don't specify aggregation (full-precision response).
    """
    if "nSigFigs" not in params:
        # Full precision — bucket width is the coin's tick size, which we don't know without observing the book.
        # Return 0 to signal "unknown".
        return 0.0

    sig_figs: int = params["nSigFigs"]
    mantissa: int = params.get("mantissa", 1) # implicit 1× when not specified

    # Bucket width in dollars at N significant figures of `top_price` is `10 ** (floor(log10(top_price)) - N + 1)`.
    # For BTC at $65k (5 digits), nSigFigs=5 → 10^(5-5+1) = $1; nSigFigs=4 → $10; nSigFigs=3 → $100; etc.
    price_magnitude: int = int(math.floor(math.log10(top_price)))
    base_bucket_dollars: float = 10 ** (price_magnitude - sig_figs + 1)
    bucket_dollars: float = base_bucket_dollars * mantissa

    return (bucket_dollars / top_price) * 10_000