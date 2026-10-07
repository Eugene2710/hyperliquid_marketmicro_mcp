"""Unit tests for the pure funding analytics (``hlmcp.analytics.funding``).

Expected values are hand-computed in each test so the arithmetic is auditable.
HL sign convention throughout: a positive funding rate means longs pay shorts.
"""

import pytest

from hlmcp.analytics.funding import (
    HOURS_PER_YEAR,
    annualize_hourly,
    carry_for_side,
    cross_venue_rates,
    next_settlement_ms,
    per_hour,
    realized_stats,
    summarize_current,
)
from hlmcp.schemas.hl_api import HLAssetCtx, HLFundingHistoryEntry, HLVenueFunding


def _ctx(
    *,
    funding: str = "0.0000125",
    mark: str = "100.5",
    oracle: str = "100.0",
    oi: str = "10.0",
    premium: str | None = "0.0001",
) -> HLAssetCtx:
    """Build a minimal HLAssetCtx; unused fields are filler."""
    return HLAssetCtx(
        funding=funding,
        openInterest=oi,
        prevDayPx="99.0",
        dayNtlVlm="1000.0",
        premium=premium,
        oraclePx=oracle,
        markPx=mark,
        midPx="100.4",
        impactPxs=["100.3", "100.6"],
        dayBaseVlm="10.0",
    )


def _row(rate: str, time_ms: int) -> HLFundingHistoryEntry:
    """Build one fundingHistory settlement."""
    return HLFundingHistoryEntry(coin="TEST", fundingRate=rate, premium="0.0", time=time_ms)


def test_annualize_hourly_floor_rate() -> None:
    """The 0.00125%/h interest floor annualizes to 10.95% simple."""
    assert HOURS_PER_YEAR == 8760
    assert annualize_hourly(0.0000125) == pytest.approx(0.1095)


def test_per_hour_converts_8h_to_1h() -> None:
    """An 8h rate of 0.0001 (Binance/Bybit style) is 0.0000125 per hour."""
    assert per_hour(0.0001, 8) == pytest.approx(0.0000125)
    assert per_hour(0.0001, 1) == pytest.approx(0.0001)


def test_per_hour_rejects_non_positive_interval() -> None:
    """A zero or negative interval is a programming error, not a silent inf."""
    with pytest.raises(ValueError):
        per_hour(0.0001, 0)


def test_carry_sign_convention_positive_rate() -> None:
    """Positive funding: the long pays (-), the short receives (+)."""
    assert carry_for_side(0.0000125, "long") == pytest.approx(-0.1095)
    assert carry_for_side(0.0000125, "short") == pytest.approx(0.1095)


def test_carry_sign_convention_negative_rate() -> None:
    """Negative funding: shorts pay longs, so the long receives (+)."""
    assert carry_for_side(-0.00002, "long") == pytest.approx(0.1752)
    assert carry_for_side(-0.00002, "short") == pytest.approx(-0.1752)


def test_next_settlement_rounds_up_to_next_hour() -> None:
    """Mid-hour rounds up; an exact boundary goes to the FOLLOWING hour."""
    hour = 3_600_000
    assert next_settlement_ms(10 * hour + 1) == 11 * hour
    assert next_settlement_ms(10 * hour + hour - 1) == 11 * hour
    assert next_settlement_ms(10 * hour) == 11 * hour


def test_summarize_current_hand_computed() -> None:
    """Basis = (100.5-100)/100*1e4 = 50 bps; OI USD = 10 * 100.5 = 1005."""
    cur = summarize_current(_ctx())

    assert cur.hourly_rate == pytest.approx(0.0000125)
    assert cur.annualized_simple == pytest.approx(0.1095)
    assert cur.mark_oracle_basis_bps == pytest.approx(50.0)
    assert cur.open_interest_usd == pytest.approx(1005.0)
    assert cur.premium == pytest.approx(0.0001)


def test_summarize_current_null_premium() -> None:
    """A null premium (delisted-style ctx) becomes None, not a parse error."""
    assert summarize_current(_ctx(premium=None)).premium is None


def test_summarize_current_zero_oracle_raises() -> None:
    """Basis is undefined with a zero oracle price."""
    with pytest.raises(ValueError):
        summarize_current(_ctx(oracle="0"))


def test_realized_stats_hand_computed() -> None:
    """Rates [+0.0001, -0.00005, +0.00002, +0.00003]: sum 0.0001, mean 0.000025."""
    rows = [
        _row("0.0001", 3_000),
        _row("-0.00005", 1_000),
        _row("0.00002", 4_000),
        _row("0.00003", 2_000),
    ]
    stats = realized_stats(rows)

    assert stats.n_settlements == 4
    assert stats.first_settlement_ms == 1_000  # order-independent
    assert stats.last_settlement_ms == 4_000
    assert stats.cumulative_rate == pytest.approx(0.0001)
    assert stats.mean_hourly_rate == pytest.approx(0.000025)
    assert stats.min_hourly_rate == pytest.approx(-0.00005)
    assert stats.max_hourly_rate == pytest.approx(0.0001)
    assert stats.fraction_positive == pytest.approx(0.75)
    assert stats.annualized_mean_simple == pytest.approx(0.000025 * 8760)


def test_realized_stats_zero_rate_is_not_positive() -> None:
    """A rate of exactly 0 does not count toward ``fraction_positive``."""
    assert realized_stats([_row("0.0", 1), _row("0.00001", 2)]).fraction_positive == 0.5


def test_realized_stats_empty_window() -> None:
    """No settlements: count 0 and every rate/time field None (no ZeroDivision)."""
    stats = realized_stats([])

    assert stats.n_settlements == 0
    assert stats.mean_hourly_rate is None
    assert stats.cumulative_rate is None
    assert stats.fraction_positive is None
    assert stats.last_settlement_ms is None


def test_cross_venue_normalizes_skips_null_and_keeps_unknown_interval() -> None:
    """8h rate is divided by 8; null venue skipped; missing interval -> None fields."""
    venues: list[tuple[str, HLVenueFunding | None]] = [
        (
            "BinPerp",
            HLVenueFunding(fundingRate="0.0001", nextFundingTime=0, fundingIntervalHours=8),
        ),
        (
            "HlPerp",
            HLVenueFunding(fundingRate="0.0000125", nextFundingTime=0, fundingIntervalHours=1),
        ),
        ("BybitPerp", None),
        ("OtherPerp", HLVenueFunding(fundingRate="0.0002", nextFundingTime=0)),
    ]
    out = cross_venue_rates(venues)

    assert [v.venue for v in out] == ["BinPerp", "HlPerp", "OtherPerp"]
    binance, hl, other = out
    assert binance.rate == pytest.approx(0.0001)
    assert binance.hourly_rate == pytest.approx(0.0000125)
    assert binance.annualized_simple == pytest.approx(0.1095)
    assert hl.hourly_rate == pytest.approx(0.0000125)
    assert other.rate == pytest.approx(0.0002)
    assert other.interval_hours is None
    assert other.hourly_rate is None
    assert other.annualized_simple is None
