"""
Funding-rate and carry analytics.

Derives the signals the ``funding_carry`` tool surfaces from raw HL funding data:

- **Current funding** (function: summarize_current): the predicted hourly rate for the
  in-progress interval from ``metaAndAssetCtxs``, annualized, with premium,
  mark/oracle basis and open interest.
- **Realized funding** (function: realized_stats): what actually settled over a
  lookback window from ``fundingHistory`` - mean, cumulative, range, and how
  persistently positive it was.
- **Carry by side** (function: carry_for_side): what a long or a short earns (+) or
  pays (-) at a given hourly rate, annualized.
- **Cross-venue** (function: cross_venue_rates): HL vs Binance/Bybit predicted rates,
  normalized to per-hour first because each venue quotes per its own interval.

Sign convention (HL's): a POSITIVE funding rate means longs pay shorts. So at rate
``r`` a long's carry is ``-r`` and a short's is ``+r`` (positive = receives).

Annualization is SIMPLE (``rate * 8760``, no compounding), the convention traders
quote for funding. Labels say "simple" so it is not mistaken for an APY.

Pure functions: no I/O, no async. HL decimal strings are parsed here, at the
analytics boundary, as in the other analytics modules.
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from hlmcp.analytics.utils import parse_float, parse_optional_float
from hlmcp.schemas.hl_api import HLAssetCtx, HLFundingHistoryEntry, HLVenueFunding

# HL settles funding hourly; one year of hourly settlements (365 * 24).
HOURS_PER_YEAR: int = 8760

# Milliseconds per hour: HL's settlement cadence, used to find the next settlement.
MS_PER_HOUR: int = 3_600_000

Side = Literal["long", "short"]


class CurrentFunding(BaseModel):
    """The live, in-progress funding picture for one market.

    Attributes:
        hourly_rate: Predicted funding rate for the current hourly interval.
        annualized_simple: ``hourly_rate * 8760`` (simple, not compounded).
        premium: Current premium input to funding, or ``None`` (delisted market).
        mark_px: Mark price.
        oracle_px: Oracle price.
        mark_oracle_basis_bps: ``(mark - oracle) / oracle * 1e4``.
        open_interest_coin: Open interest in coin units.
        open_interest_usd: ``open_interest_coin * mark_px``.
        day_notional_volume_usd: 24h notional volume, USD.
    """

    hourly_rate: float = Field(description="Predicted hourly funding rate, current interval.")
    annualized_simple: float = Field(description="hourly_rate * 8760 (simple, no compounding).")
    premium: float | None = Field(default=None, description="Current premium; None if delisted.")
    mark_px: float = Field(description="Mark price.")
    oracle_px: float = Field(description="Oracle price.")
    mark_oracle_basis_bps: float = Field(description="(mark - oracle) / oracle, in bps.")
    open_interest_coin: float = Field(description="Open interest, coin units.")
    open_interest_usd: float = Field(description="Open interest valued at mark, USD.")
    day_notional_volume_usd: float = Field(description="24h notional volume, USD.")


class RealizedFunding(BaseModel):
    """Statistics over settled hourly funding in a lookback window.

    All rate fields are ``None`` when the window holds no settlements (e.g. a
    freshly listed market).

    Attributes:
        n_settlements: Number of hourly settlements in the window.
        first_settlement_ms: Oldest settlement time in the window, or ``None``.
        last_settlement_ms: Newest settlement time in the window, or ``None``.
        mean_hourly_rate: Mean settled hourly rate.
        cumulative_rate: Sum of settled rates: total funding per 1 unit of
            notional over the window (positive = longs paid that fraction).
        min_hourly_rate: Lowest settled hourly rate.
        max_hourly_rate: Highest settled hourly rate.
        fraction_positive: Share of settlements with rate > 0 (longs paying).
        annualized_mean_simple: ``mean_hourly_rate * 8760``.
    """

    n_settlements: int = Field(ge=0, description="Hourly settlements in the window.")
    first_settlement_ms: int | None = Field(default=None, description="Oldest settlement time.")
    last_settlement_ms: int | None = Field(default=None, description="Newest settlement time.")
    mean_hourly_rate: float | None = Field(default=None, description="Mean settled hourly rate.")
    cumulative_rate: float | None = Field(
        default=None,
        description="Sum of settled rates = funding per 1 unit notional over the window.",
    )
    min_hourly_rate: float | None = Field(default=None, description="Lowest settled rate.")
    max_hourly_rate: float | None = Field(default=None, description="Highest settled rate.")
    fraction_positive: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Share of settlements with rate > 0."
    )
    annualized_mean_simple: float | None = Field(
        default=None, description="mean_hourly_rate * 8760 (simple)."
    )


class VenueRate(BaseModel):
    """One venue's predicted funding, normalized for comparison.

    Attributes:
        venue: Venue key as HL reports it (``"HlPerp"``, ``"BinPerp"``, ``"BybitPerp"``).
        rate: Raw predicted rate, per ``interval_hours``.
        interval_hours: The venue's funding interval, or ``None`` if unreported.
        hourly_rate: ``rate / interval_hours``; ``None`` if the interval is unknown.
        annualized_simple: ``hourly_rate * 8760``; ``None`` if the interval is unknown.
    """

    venue: str = Field(description="Venue key: HlPerp, BinPerp, BybitPerp.")
    rate: float = Field(description="Raw predicted rate per interval_hours.")
    interval_hours: int | None = Field(
        default=None, description="Venue funding interval; None if unreported."
    )
    hourly_rate: float | None = Field(
        default=None, description="rate / interval_hours; None if interval unknown."
    )
    annualized_simple: float | None = Field(
        default=None, description="hourly_rate * 8760; None if interval unknown."
    )


def annualize_hourly(hourly_rate: float) -> float:
    """Annualize an hourly funding rate, simple (no compounding).

    Mechanism: takes an hourly rate, returns ``hourly_rate * HOURS_PER_YEAR``.

    Args:
        hourly_rate: Funding rate per hour.

    Returns:
        The simple annualized rate (e.g. ``0.0000125`` -> ``0.1095``, i.e. 10.95%).
    """
    return hourly_rate * HOURS_PER_YEAR


def per_hour(rate: float, interval_hours: int) -> float:
    """Convert a rate quoted per ``interval_hours`` to a per-hour rate.

    Mechanism: takes a rate and its interval, returns ``rate / interval_hours``.

    Args:
        rate: Funding rate per interval.
        interval_hours: Interval length in hours, > 0.

    Returns:
        The equivalent hourly rate.

    Raises:
        ValueError: If ``interval_hours`` is not positive.
    """
    if interval_hours <= 0:
        raise ValueError(f"interval_hours must be > 0, got {interval_hours}")
    return rate / interval_hours


def carry_for_side(hourly_rate: float, side: Side) -> float:
    """Annualized carry a position earns at ``hourly_rate``, by side.

    Mechanism: positive funding means longs pay shorts, so a long's carry is
    ``-rate`` and a short's is ``+rate``; the result is annualized (simple).

    Args:
        hourly_rate: Funding rate per hour (HL sign convention).
        side: ``"long"`` or ``"short"``.

    Returns:
        Annualized simple carry as a fraction of notional; positive = the side
        RECEIVES funding, negative = it PAYS.
    """
    signed: float = -hourly_rate if side == "long" else hourly_rate
    return annualize_hourly(signed)


def next_settlement_ms(now_ms: int) -> int:
    """Return the next HL funding settlement time after ``now_ms``.

    Mechanism: HL settles on each whole hour, so round ``now_ms`` up to the next
    hour boundary. Computed locally because ``predictedFundings``'s own HL
    ``nextFundingTime`` equals the most recent settlement (lags one interval).

    Args:
        now_ms: Current time, ms since epoch.

    Returns:
        The next whole-hour boundary strictly after ``now_ms``, ms since epoch.
    """
    return (now_ms // MS_PER_HOUR + 1) * MS_PER_HOUR


def summarize_current(ctx: HLAssetCtx) -> CurrentFunding:
    """Materialize the live funding picture from an asset ctx.

    Mechanism: parse the ctx's decimal strings, annualize the predicted hourly
    rate, compute mark-vs-oracle basis in bps and USD open interest at mark.

    Args:
        ctx: One market's ``metaAndAssetCtxs`` ctx.

    Returns:
        A :class:`CurrentFunding`.

    Raises:
        ValueError: If a required numeric field is malformed, or ``oraclePx`` is 0
            (basis undefined).
    """
    hourly: float = parse_float(ctx.funding)
    mark: float = parse_float(ctx.markPx)
    oracle: float = parse_float(ctx.oraclePx)
    if oracle == 0.0:
        raise ValueError("oraclePx is 0; mark/oracle basis is undefined")
    oi_coin: float = parse_float(ctx.openInterest)
    return CurrentFunding(
        hourly_rate=hourly,
        annualized_simple=annualize_hourly(hourly),
        premium=parse_optional_float(ctx.premium),
        mark_px=mark,
        oracle_px=oracle,
        mark_oracle_basis_bps=(mark - oracle) / oracle * 1e4,
        open_interest_coin=oi_coin,
        open_interest_usd=oi_coin * mark,
        day_notional_volume_usd=parse_float(ctx.dayNtlVlm),
    )


def realized_stats(entries: Sequence[HLFundingHistoryEntry]) -> RealizedFunding:
    """Summarize settled hourly funding over a window.

    Mechanism: parse each settlement's rate, then take count, mean, sum, min,
    max and the share of positive settlements; annualize the mean (simple).
    An empty window returns a count of 0 and ``None`` for every rate field.

    Args:
        entries: Settlements in the window (any order).

    Returns:
        A :class:`RealizedFunding`.

    Raises:
        ValueError: If a ``fundingRate`` is malformed.
    """
    if not entries:
        return RealizedFunding(n_settlements=0)
    rates: list[float] = [parse_float(e.fundingRate) for e in entries]
    times: list[int] = [e.time for e in entries]
    n: int = len(rates)
    mean: float = sum(rates) / n
    return RealizedFunding(
        n_settlements=n,
        first_settlement_ms=min(times),
        last_settlement_ms=max(times),
        mean_hourly_rate=mean,
        cumulative_rate=sum(rates),
        min_hourly_rate=min(rates),
        max_hourly_rate=max(rates),
        fraction_positive=sum(1 for r in rates if r > 0.0) / n,
        annualized_mean_simple=annualize_hourly(mean),
    )


def cross_venue_rates(
    venues: Sequence[tuple[str, HLVenueFunding | None]],
) -> list[VenueRate]:
    """Normalize each venue's predicted funding to per-hour for comparison.

    Mechanism: skip venues that do not list the coin (``None`` entry); for the
    rest, divide by the venue's interval when known and annualize (simple).
    Venues with no reported interval keep the raw rate with ``None`` normalized
    fields rather than a guessed interval.

    Args:
        venues: ``(venue, funding | None)`` pairs from ``predictedFundings``.

    Returns:
        One :class:`VenueRate` per venue that lists the coin, in input order.

    Raises:
        ValueError: If a ``fundingRate`` is malformed.
    """
    out: list[VenueRate] = []
    for venue, funding in venues:
        if funding is None:
            continue
        rate: float = parse_float(funding.fundingRate)
        interval: int | None = funding.fundingIntervalHours
        hourly: float | None = per_hour(rate, interval) if interval else None
        out.append(
            VenueRate(
                venue=venue,
                rate=rate,
                interval_hours=interval,
                hourly_rate=hourly,
                annualized_simple=annualize_hourly(hourly) if hourly is not None else None,
            )
        )
    return out
