"""Unit tests for the L2 book aggregation ladder.

Covers :func:`choose_aggregation` across band sizes -- including the genuine
(30, 296] bps granularity gap that forces a fall-back to ``nSigFigs=3`` -- and
:func:`estimate_bucket_bps` against the BTC calibration in
``docs/api_spike_findings.md`` Q1.
"""

from collections.abc import Callable
from typing import Any

import pytest

from hlmcp.analytics.aggregation import (
    _PRICE_AWARE_CANDIDATES,
    L2BookParams,
    bucket_width_to_bps,
    choose_aggregation,
    estimate_bucket_bps,
    measure_bucket_width_usd,
)
from hlmcp.schemas.hl_api import HLL2Book, HLL2Level


def _make_book(bid_pxs: list[str], ask_pxs: list[str], *, coin: str = "TEST") -> HLL2Book:
    """Build a minimal HLL2Book from bid/ask price lists (size/n are filler)."""
    bids = [HLL2Level(px=px, sz="1.0", n=1) for px in bid_pxs]
    asks = [HLL2Level(px=px, sz="1.0", n=1) for px in ask_pxs]
    return HLL2Book(coin=coin, time=1_700_000_000_000, levels=[bids, asks])


@pytest.mark.parametrize(
    ("max_band_bps", "expected"),
    [
        # Tiny bands: finest non-full setting (nSigFigs=5, mantissa=2, ~6 bps).
        (0.5, {"nSigFigs": 5, "mantissa": 2}),
        (6.0, {"nSigFigs": 5, "mantissa": 2}),  # exactly the rung boundary
        # Just past the first rung -> next precision down.
        (6.0001, {"nSigFigs": 5, "mantissa": 5}),
        (10.0, {"nSigFigs": 5, "mantissa": 5}),
        (15.0, {"nSigFigs": 5, "mantissa": 5}),  # boundary
        (15.0001, {"nSigFigs": 4}),
        (30.0, {"nSigFigs": 4}),  # boundary -- still nSigFigs=4
        # The (30, 296] gap: no API setting between nSigFigs=4 and 3, so these
        # all fall back to nSigFigs=3 and accept coarser buckets.
        (30.0001, {"nSigFigs": 3}),
        (50.0, {"nSigFigs": 3}),
        (100.0, {"nSigFigs": 3}),
        (296.0, {"nSigFigs": 3}),  # boundary
        # Beyond nSigFigs=3 range -> coarsest meaningful rung.
        (296.0001, {"nSigFigs": 2}),
        (2969.0, {"nSigFigs": 2}),
        # Past the entire ladder -> clamp to the widest (nSigFigs=2).
        (5000.0, {"nSigFigs": 2}),
        (1_000_000.0, {"nSigFigs": 2}),
    ],
)
def test_choose_aggregation_ladder(max_band_bps: float, expected: L2BookParams) -> None:
    """Each band maps to the highest-precision setting whose range still covers it."""
    assert choose_aggregation(max_band_bps) == expected


def test_choose_aggregation_gap_falls_to_nsf3() -> None:
    """A band just inside the (30, 296] gap gets nSigFigs=3; just below stays nSigFigs=4.

    Documents the gap behavior: a 31 bps requirement cannot be served at
    nSigFigs=4 (~30 bps reach), so it falls to the strictly coarser nSigFigs=3.
    """
    assert choose_aggregation(31.0) == {"nSigFigs": 3}
    assert choose_aggregation(29.0) == {"nSigFigs": 4}


@pytest.mark.parametrize("bad", [0.0, -1.0, -0.001])
def test_choose_aggregation_rejects_nonpositive(bad: float) -> None:
    """A non-positive band is a caller error, not a clamp-to-something case."""
    with pytest.raises(ValueError, match="must be > 0"):
        choose_aggregation(bad)


def test_estimate_bucket_bps_btc_calibration() -> None:
    """Bucket-width estimates reproduce the BTC @ ~$65k spike table (Q1).

    From api_spike_findings.md at $65,515: nSigFigs=5 -> $1 bucket (0.15 bps),
    nSigFigs=4 -> $10 (1.53 bps), nSigFigs=3 -> $100 (15.27 bps),
    nSigFigs=5/mantissa=5 -> $5 (0.76 bps), nSigFigs=5/mantissa=2 -> $2 (0.31 bps).
    """
    top: float = 65_515.0
    assert estimate_bucket_bps({"nSigFigs": 5}, top) == pytest.approx(0.15263, abs=1e-4)
    assert estimate_bucket_bps({"nSigFigs": 4}, top) == pytest.approx(1.5263, abs=1e-3)
    assert estimate_bucket_bps({"nSigFigs": 3}, top) == pytest.approx(15.263, abs=1e-2)
    assert estimate_bucket_bps({"nSigFigs": 5, "mantissa": 5}, top) == pytest.approx(
        0.76317, abs=1e-4
    )
    assert estimate_bucket_bps({"nSigFigs": 5, "mantissa": 2}, top) == pytest.approx(
        0.30527, abs=1e-4
    )


def test_estimate_bucket_bps_is_price_dependent() -> None:
    """The same setting yields a different bps width at a different price magnitude.

    nSigFigs=4 on SOL @ $150 ($0.10 buckets, ~6.67 bps) is far coarser than on
    BTC @ ~$65k ($10 buckets, ~1.53 bps) -- this is exactly why the ladder is
    BTC-calibrated and does not transfer across coins (architecture.md open Qs).
    """
    sol = estimate_bucket_bps({"nSigFigs": 4}, 150.0)
    btc = estimate_bucket_bps({"nSigFigs": 4}, 65_515.0)
    assert sol == pytest.approx(6.6667, abs=1e-3)
    assert sol > btc


def test_estimate_bucket_bps_full_precision_is_unknown() -> None:
    """Full-precision params (no nSigFigs) report 0.0 == 'unknown without the book'."""
    assert estimate_bucket_bps({}, 65_515.0) == 0.0


def test_estimate_bucket_bps_rejects_nonpositive_price() -> None:
    """A non-positive price has no log10; that is a caller error."""
    with pytest.raises(ValueError, match="top_price must be > 0"):
        estimate_bucket_bps({"nSigFigs": 5}, 0.0)


def test_choose_then_estimate_round_trip() -> None:
    """The setting chosen for a band yields a bucket width consistent with the ladder.

    Picking aggregation for a 100 bps band on BTC gives nSigFigs=3, whose
    estimated bucket width (~15 bps) is well under the 100 bps band -- i.e. the
    band spans several buckets, as intended.
    """
    params = choose_aggregation(100.0)
    bucket_bps = estimate_bucket_bps(params, 65_515.0)
    assert bucket_bps == pytest.approx(15.263, abs=1e-2)
    assert bucket_bps < 100.0


# --------------------------------------------------------------------------- #
# Price-aware choose_aggregation (the XRP-class fix)                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("band", "expected"),
    [
        # XRP @ ~$1.12: same bands as the defaults, sized to the real price.
        # Each setting is ~58x coarser in bps than on BTC, so the picks are far
        # finer than the BTC ladder would choose for the same band.
        (10.0, {"nSigFigs": 5}),
        (25.0, {"nSigFigs": 5, "mantissa": 2}),
        (50.0, {"nSigFigs": 5, "mantissa": 5}),
        (100.0, {"nSigFigs": 4}),
    ],
)
def test_choose_aggregation_price_aware_low_price(band: float, expected: L2BookParams) -> None:
    """A ~$1 coin gets price-appropriate settings, not the BTC ladder's picks.

    The headline bug: the BTC ladder maps a 100 bps band to nSigFigs=3, which on
    XRP is ~89 bps per bucket (band collapses into one bucket). Price-aware
    selection instead picks nSigFigs=4 (~8.9 bps buckets). Values validated
    against the live XRP l2Book.
    """
    assert choose_aggregation(band, price=1.12) == expected


# Representative mids spanning the magnitudes we care about, from 5-digit BTC
# down to ~$1 XRP. Approximate: the assertions below are the price-aware
# CONTRACT (finest setting whose 20-level reach still covers the band), not exact
# settings, so they hold as prices drift within their decade.
_REPRESENTATIVE_PRICES: dict[str, float] = {
    "BTC": 63_000.0,  # ~5-digit
    "ETH": 1_771.0,  # ~4-digit
    "BNB": 600.0,  # ~3-digit
    "SOL": 81.0,  # ~2-digit
    "XRP": 1.12,  # ~$1
}


@pytest.mark.parametrize("coin", list(_REPRESENTATIVE_PRICES))
@pytest.mark.parametrize("band", [10.0, 25.0, 50.0, 100.0, 250.0])
def test_choose_aggregation_price_aware_finest_covering_at_all_magnitudes(
    coin: str, band: float
) -> None:
    """Across BTC/ETH/BNB/SOL/XRP, the pick is the finest setting that reaches the band.

    This is the invariant price-awareness must hold at every price magnitude: the
    chosen setting's 20-level reach covers the band (unless clamped to the
    coarsest), and no finer setting would have covered it (so resolution is never
    needlessly thrown away). Verified against each coin's representative price.
    """
    price: float = _REPRESENTATIVE_PRICES[coin]
    params: L2BookParams = choose_aggregation(band, price=price)
    assert params in _PRICE_AWARE_CANDIDATES

    idx: int = _PRICE_AWARE_CANDIDATES.index(params)
    # Every finer candidate must fail to reach the band, else it'd have been chosen.
    for finer in _PRICE_AWARE_CANDIDATES[:idx]:
        assert 20.0 * estimate_bucket_bps(finer, price) < band
    # The chosen setting reaches the band, unless it is the coarsest (a clamp).
    if params != _PRICE_AWARE_CANDIDATES[-1]:
        assert 20.0 * estimate_bucket_bps(params, price) >= band


def test_choose_aggregation_price_aware_btc_matches_ladder() -> None:
    """At BTC's price magnitude, the price-aware path agrees with the BTC ladder."""
    for band in (10.0, 50.0, 100.0, 296.0):
        assert choose_aggregation(band, price=63_000.0) == choose_aggregation(band)


def test_choose_aggregation_price_aware_clamps_beyond_ladder() -> None:
    """A band beyond the coarsest setting's reach clamps to nSigFigs=2."""
    assert choose_aggregation(1_000_000.0, price=1.12) == {"nSigFigs": 2}


@pytest.mark.parametrize("bad_price", [0.0, -1.0])
def test_choose_aggregation_price_aware_rejects_nonpositive_price(bad_price: float) -> None:
    """A non-positive price (no log10) is a caller error when price is provided."""
    with pytest.raises(ValueError, match="price must be > 0"):
        choose_aggregation(100.0, price=bad_price)


# --------------------------------------------------------------------------- #
# measure_bucket_width_usd (actual achieved width, min-gap, gap-robust)        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fixture", "expected_usd"),
    [
        ("l2book_btc_nsf5.json", 1.0),
        ("l2book_btc_nsf5_m5.json", 5.0),
        ("l2book_btc_nsf4.json", 10.0),
        ("l2book_btc_nsf3.json", 100.0),
    ],
)
def test_measure_bucket_width_from_fixtures(
    load_json: Callable[[str], Any], fixture: str, expected_usd: float
) -> None:
    """The measured width matches the known bucket size of each recorded fixture."""
    book = HLL2Book.model_validate(load_json(fixture))
    assert measure_bucket_width_usd(book) == pytest.approx(expected_usd, abs=1e-6)


def test_measure_bucket_width_robust_to_empty_bucket_gap() -> None:
    """A skipped (empty) bucket at the top does not inflate the measured width.

    The top two bids straddle an empty $0.02 gap, but a deeper adjacent pair is
    $0.01 apart; min-of-gaps recovers the true $0.01 step where differencing the
    top two would wrongly report $0.02.
    """
    book = _make_book(
        bid_pxs=["100.00", "99.98", "99.97", "99.96"],  # first gap 0.02 (skip), rest 0.01
        ask_pxs=["100.01", "100.02"],
    )
    assert measure_bucket_width_usd(book) == pytest.approx(0.01, abs=1e-6)


def test_measure_bucket_width_none_when_no_pair() -> None:
    """A one-level-per-side book has no consecutive gap to difference -> None."""
    assert measure_bucket_width_usd(_make_book(["100.0"], ["100.1"])) is None


# --------------------------------------------------------------------------- #
# bucket_width_to_bps                                                          #
# --------------------------------------------------------------------------- #


def test_bucket_width_to_bps_converts() -> None:
    """A USD width is expressed as basis points of the mid (fraction x 10_000)."""
    assert bucket_width_to_bps(10.0, 64_950.0) == pytest.approx(10.0 / 64_950.0 * 1e4, abs=1e-9)


def test_bucket_width_to_bps_none_passthrough() -> None:
    """An unmeasurable (None) width converts to None, not an error."""
    assert bucket_width_to_bps(None, 64_950.0) is None


@pytest.mark.parametrize("bad_mid", [0.0, -5.0])
def test_bucket_width_to_bps_nonpositive_mid_is_none(bad_mid: float) -> None:
    """A non-positive mid has no meaningful bps conversion -> None (not a raise)."""
    assert bucket_width_to_bps(10.0, bad_mid) is None
