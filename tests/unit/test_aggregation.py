"""Unit tests for the L2 book aggregation ladder.

Covers :func:`choose_aggregation` across band sizes -- including the genuine
(30, 296] bps granularity gap that forces a fall-back to ``nSigFigs=3`` -- and
:func:`estimate_bucket_bps` against the BTC calibration in
``docs/api_spike_findings.md`` Q1.
"""

import pytest

from hlmcp.analytics.aggregation import (
    L2BookParams,
    choose_aggregation,
    estimate_bucket_bps,
)


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
