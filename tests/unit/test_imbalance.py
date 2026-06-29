"""Unit tests for depth-weighted order-book imbalance.

The ``test_*_fixture`` cases assert HAND-CHECKED expected values computed from
the recorded ``l2Book`` fixtures (the same arithmetic the function implements,
verified independently). The synthetic-book cases lock the exact band-window and
ratio arithmetic on numbers small enough to check by eye.
"""

from collections.abc import Callable
from typing import Any

import pytest

from hlmcp.analytics.imbalance import (
    ImbalanceBand,
    compute_imbalance,
    compute_mid_price,
)
from hlmcp.schemas.hl_api import HLL2Book, HLL2Level


def _book(bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> HLL2Book:
    """Build a minimal :class:`HLL2Book` from (px, sz) string pairs.

    Mechanism: wrap each pair in an :class:`HLL2Level` (n=1) and assemble the
    two-sided ``levels`` list the schema expects.

    Args:
        bids: (px, sz) string pairs, expected in descending price order.
        asks: (px, sz) string pairs, expected in ascending price order.

    Returns:
        A parsed :class:`HLL2Book` for ``"BTC"`` with a fixed timestamp.
    """
    return HLL2Book(
        coin="BTC",
        time=1,
        levels=[
            [HLL2Level(px=px, sz=sz, n=1) for px, sz in bids],
            [HLL2Level(px=px, sz=sz, n=1) for px, sz in asks],
        ],
    )


# --------------------------------------------------------------------------- #
# synthetic books -- exact, eyeball-checkable arithmetic                       #
# --------------------------------------------------------------------------- #


def test_synthetic_two_bands() -> None:
    """A hand-computed book at mid 64950 with bands [10, 50].

    bids: 64900x5, 64800x3, 64700x10 ; asks: 65000x4, 65100x2, 65200x8.
    mid = (64900 + 65000) / 2 = 64950.

    10 bps -> window [64885.05, 65014.95]: bid 64900x5 only, ask 65000x4 only.
      bid_ntl = 324500, ask_ntl = 260000, ratio = 64500/584500 = 0.110350...
    50 bps -> window [64625.25, 65274.75]: all three each side.
      bid_ntl = 1165900, ask_ntl = 911800, ratio = 254100/2077700 = 0.122299...
    """
    book = _book(
        bids=[("64900", "5"), ("64800", "3"), ("64700", "10")],
        asks=[("65000", "4"), ("65100", "2"), ("65200", "8")],
    )
    out = compute_imbalance(book, [10.0, 50.0])

    assert [b.band_bps for b in out] == [10.0, 50.0]

    near = out[0]
    assert near.bid_size == pytest.approx(5.0)
    assert near.ask_size == pytest.approx(4.0)
    assert near.bid_notional_usd == pytest.approx(324_500.0)
    assert near.ask_notional_usd == pytest.approx(260_000.0)
    assert near.imbalance_ratio == pytest.approx(64_500.0 / 584_500.0)
    assert near.levels_in_band == 2

    far = out[1]
    assert far.bid_size == pytest.approx(18.0)
    assert far.ask_size == pytest.approx(14.0)
    assert far.bid_notional_usd == pytest.approx(1_165_900.0)
    assert far.ask_notional_usd == pytest.approx(911_800.0)
    assert far.imbalance_ratio == pytest.approx(254_100.0 / 2_077_700.0)
    assert far.levels_in_band == 6


def test_band_reaching_top_includes_both_sides() -> None:
    """Any band wide enough to reach the top bid also reaches the top ask.

    The window is symmetric around mid and the best bid/ask are equidistant from
    it, so a non-empty band always contains >=1 level on each side. Imbalance is
    therefore driven by depth asymmetry, not by one side being entirely absent.
    Here deep bid size dwarfs ask size, giving a strong (but sub-1.0) ratio.
    """
    book = _book(bids=[("100", "50"), ("99.9", "100")], asks=[("100.1", "1")])
    out = compute_imbalance(book, [50.0])  # mid 100.05; +/-0.5 reaches all levels
    band = out[0]
    assert band.levels_in_band == 3  # 2 bids + 1 ask
    assert band.imbalance_ratio > 0.9  # heavily bid-skewed...
    assert band.imbalance_ratio < 1.0  # ...but never saturates, an ask is present


def test_empty_band_is_zero_not_error() -> None:
    """A band too tight to contain any level yields a zeroed, ratio-0 result."""
    book = _book(bids=[("64000", "5")], asks=[("66000", "4")])
    out = compute_imbalance(book, [1.0])  # mid 65000, +/-6.5 -> nothing inside
    band = out[0]
    assert band.bid_size == 0.0
    assert band.ask_size == 0.0
    assert band.imbalance_ratio == 0.0
    assert band.levels_in_band == 0


# --------------------------------------------------------------------------- #
# recorded fixtures -- hand-checked expected values                            #
# --------------------------------------------------------------------------- #


def test_nsf5_fixture_bands(load_json: Callable[[str], Any]) -> None:
    """compute_imbalance on the recorded nSigFigs=5 BTC book (mid 64900.5).

    Hand-checked values (independently summed from the fixture):
      1 bps: bid_sz 20.92406, ask_sz 10.40644, ratio +0.3356664
      2 bps: bid_sz 38.37236, ask_sz 68.97834, ratio -0.2852017
      5 bps: bid_sz 100.59178, ask_sz 110.82321, ratio -0.0485777
    """
    book = HLL2Book.model_validate(load_json("l2book_btc_nsf5.json"))
    assert compute_mid_price(book) == pytest.approx(64_900.5)

    out = compute_imbalance(book, [1.0, 2.0, 5.0])

    assert out[0].bid_size == pytest.approx(20.92406)
    assert out[0].ask_size == pytest.approx(10.40644)
    assert out[0].imbalance_ratio == pytest.approx(0.33566644538, abs=1e-9)
    assert out[0].levels_in_band == 12

    assert out[1].bid_size == pytest.approx(38.37236)
    assert out[1].ask_size == pytest.approx(68.97834)
    assert out[1].imbalance_ratio == pytest.approx(-0.28520167818, abs=1e-9)
    assert out[1].levels_in_band == 26

    assert out[2].bid_size == pytest.approx(100.59178)
    assert out[2].ask_size == pytest.approx(110.82321)
    assert out[2].imbalance_ratio == pytest.approx(-0.04857770197, abs=1e-9)
    # The nSigFigs=5 book only spans ~3 bps/side, so a 5 bps band captures all
    # 20 levels on each side.
    assert out[2].levels_in_band == 40


def test_nsf3_fixture_bands(load_json: Callable[[str], Any]) -> None:
    """compute_imbalance on the recorded nSigFigs=3 BTC book (mid 64950).

    The coarse book reaches deep, so 50/100 bps bands capture multiple levels.
    Hand-checked:
      50 bps:  bid_sz 810.49261,  ask_sz 1327.73877, ratio -0.2440990, levels 6
      100 bps: bid_sz 1493.97306, ask_sz 1915.57468, ratio -0.1276302, levels 12
    """
    book = HLL2Book.model_validate(load_json("l2book_btc_nsf3.json"))
    assert compute_mid_price(book) == pytest.approx(64_950.0)

    out = compute_imbalance(book, [50.0, 100.0])

    assert out[0].bid_size == pytest.approx(810.49261)
    assert out[0].ask_size == pytest.approx(1327.73877)
    assert out[0].imbalance_ratio == pytest.approx(-0.24409898888, abs=1e-9)
    assert out[0].levels_in_band == 6

    assert out[1].bid_size == pytest.approx(1493.97306)
    assert out[1].ask_size == pytest.approx(1915.57468)
    assert out[1].imbalance_ratio == pytest.approx(-0.12763022433, abs=1e-9)
    assert out[1].levels_in_band == 12


def test_ratio_within_bounds_across_fixtures(load_json: Callable[[str], Any]) -> None:
    """imbalance_ratio stays within [-1, 1] for every band on every recorded book."""
    for name in (
        "l2book_btc_nsf5.json",
        "l2book_btc_nsf5_m5.json",
        "l2book_btc_nsf4.json",
        "l2book_btc_nsf3.json",
    ):
        book = HLL2Book.model_validate(load_json(name))
        for band in compute_imbalance(book, [1.0, 5.0, 25.0, 100.0, 290.0]):
            assert -1.0 <= band.imbalance_ratio <= 1.0


# --------------------------------------------------------------------------- #
# input validation / structure                                                 #
# --------------------------------------------------------------------------- #


def test_result_order_matches_input() -> None:
    """Results come back one-per-band in the same order, duplicates preserved."""
    book = _book(bids=[("100", "1")], asks=[("101", "1")])
    out = compute_imbalance(book, [5.0, 50.0, 5.0])
    assert [b.band_bps for b in out] == [5.0, 50.0, 5.0]
    assert all(isinstance(b, ImbalanceBand) for b in out)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_rejects_nonpositive_band(bad: float) -> None:
    """A non-positive band value is a caller error."""
    book = _book(bids=[("100", "1")], asks=[("101", "1")])
    with pytest.raises(ValueError, match="must be > 0"):
        compute_imbalance(book, [10.0, bad])


def test_mid_price_requires_both_sides() -> None:
    """A one-sided book has no meaningful mid; compute_mid_price raises."""
    one_sided = _book(bids=[("100", "1")], asks=[])
    with pytest.raises(ValueError, match="missing a bid or ask"):
        compute_mid_price(one_sided)
    with pytest.raises(ValueError, match="missing a bid or ask"):
        compute_imbalance(one_sided, [10.0])
