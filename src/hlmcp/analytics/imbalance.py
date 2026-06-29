"""Depth-weighted order-book imbalance.

Given an L2 order book (bids and asks at aggregated price levels) and a set of
basis-point bands measured from the mid price, computes the bid/ask size and
notional imbalance within each band. Imbalance is a microstructure signal for
short-term order-flow pressure: a strong positive (bid-heavy) reading suggests
buying pressure; a strong negative (ask-heavy) reading suggests the opposite.

A "band" of ``band_bps`` is a price window ``band_bps`` basis points wide on
*each* side of mid. For BTC @ $65,000 a 50 bps band covers $64,675 -> $65,325.
Bids within ``[mid*(1 - bps/1e4), mid]`` and asks within ``[mid, mid*(1 +
bps/1e4)]`` are counted. Because ``l2Book`` returns at most 20 levels per side,
a band wider than the book's reach simply includes every available level (the
tool layer is responsible for choosing aggregation that reaches the deepest band
-- see :mod:`hlmcp.analytics.aggregation`).

The computation is a PURE function over already-fetched data: no I/O, no async,
no API calls (CLAUDE.md hard rule). That is what makes it testable from recorded
fixtures with hand-checked expected values.
"""

from collections.abc import Sequence

from pydantic import BaseModel, Field

from hlmcp.schemas.hl_api import HLL2Book, HLL2Level


class ImbalanceBand(BaseModel):
    """Imbalance metrics for one basis-point band around the mid price.

    A band is a price window ``band_bps`` basis points wide on each side of mid.
    ``imbalance_ratio`` is computed from *notional* (size x price) rather than
    raw size, so it is not skewed by the price difference between bids and asks
    within the band.

    Attributes:
        band_bps: Half-width of this band in basis points from mid.
        bid_size: Total bid size within the band, in coin units.
        ask_size: Total ask size within the band, in coin units.
        bid_notional_usd: Sum of (size x price) over bids within the band, USD.
        ask_notional_usd: Sum of (size x price) over asks within the band, USD.
        imbalance_ratio: ``(bid_notional - ask_notional) / (bid_notional +
            ask_notional)``, in ``[-1, 1]``. ``+1`` = all bid-side, ``-1`` = all
            ask-side, ``0`` = balanced (or no levels in band).
        levels_in_band: Count of bid + ask levels falling inside the band.
    """

    band_bps: float = Field(description="Half-width of this band in basis points from mid.")
    bid_size: float = Field(description="Total bid size within the band, in coin units.")
    ask_size: float = Field(description="Total ask size within the band, in coin units.")
    bid_notional_usd: float = Field(description="Sum of bid size x price within the band, USD.")
    ask_notional_usd: float = Field(description="Sum of ask size x price within the band, USD.")
    imbalance_ratio: float = Field(
        ge=-1.0,
        le=1.0,
        description=(
            "(bid_notional - ask_notional) / (bid_notional + ask_notional). "
            "+1 = all bid-side, -1 = all ask-side, 0 = balanced/empty."
        ),
    )
    levels_in_band: int = Field(
        ge=0, description="Count of bid + ask levels falling inside the band."
    )


def compute_mid_price(book: HLL2Book) -> float:
    """Compute the mid price from the top of book.

    Mid is the simple average of the best bid and best ask. This is the
    reference point every band is measured from.

    Args:
        book: A parsed :class:`HLL2Book` with at least one level on each side.

    Returns:
        The mid price as a float.

    Raises:
        ValueError: If either side of the book is empty (no best bid or best
            ask, so no meaningful mid).
    """
    bids, asks = book.levels[0], book.levels[1]
    if not bids or not asks:
        raise ValueError("cannot compute mid: book is missing a bid or ask side")
    return (float(bids[0].px) + float(asks[0].px)) / 2.0


def compute_imbalance(book: HLL2Book, bands_bps: Sequence[float]) -> list[ImbalanceBand]:
    """Compute depth-weighted imbalance for each requested band.

    For each ``band_bps`` in ``bands_bps``, sums bid and ask size/notional for
    the levels whose price falls within ``band_bps`` basis points of mid on the
    respective side, and reports the notional-weighted imbalance ratio.

    Mechanism: compute mid from the top of book; for each band, derive the price
    window ``mid * (1 +/- band/10_000)``, sum size and size*price for bids at/above
    the lower bound and asks at/below the upper bound, then report the ratio
    ``(bid_notional - ask_notional) / (bid_notional + ask_notional)`` (0 if the
    band is empty). One result per input band, same order.

    The order book is assumed already aggregated to a setting whose 20-level
    reach covers the deepest band (the tool layer arranges this via
    :func:`hlmcp.analytics.aggregation.choose_aggregation`). A band wider than
    the book's reach is not an error -- it simply includes all available levels.

    Args:
        book: A parsed :class:`HLL2Book` with at least one level on each side.
        bands_bps: Basis-point band half-widths to evaluate. Each must be > 0.
            Order is preserved in the result; duplicates are allowed.

    Returns:
        One :class:`ImbalanceBand` per entry in ``bands_bps``, in the same order.

    Raises:
        ValueError: If the book is missing a side (via :func:`compute_mid_price`)
            or any band value is not strictly positive.
    """
    for band in bands_bps:
        if band <= 0:
            raise ValueError(f"band_bps values must be > 0, got {band}")

    mid: float = compute_mid_price(book)
    bids: list[HLL2Level] = book.levels[0]
    asks: list[HLL2Level] = book.levels[1]

    results: list[ImbalanceBand] = []
    for band in bands_bps:
        # Band bounds: bids at/above the lower bound, asks at/below the upper.
        lower: float = mid * (1.0 - band / 10_000.0)
        upper: float = mid * (1.0 + band / 10_000.0)

        bid_size: float = 0.0
        bid_notional: float = 0.0
        bid_levels: int = 0
        for level in bids:
            px: float = float(level.px)
            if px >= lower:
                sz: float = float(level.sz)
                bid_size += sz
                bid_notional += sz * px
                bid_levels += 1

        ask_size: float = 0.0
        ask_notional: float = 0.0
        ask_levels: int = 0
        for level in asks:
            px = float(level.px)
            if px <= upper:
                sz = float(level.sz)
                ask_size += sz
                ask_notional += sz * px
                ask_levels += 1

        total_notional: float = bid_notional + ask_notional
        ratio: float = (bid_notional - ask_notional) / total_notional if total_notional > 0 else 0.0

        results.append(
            ImbalanceBand(
                band_bps=band,
                bid_size=bid_size,
                ask_size=ask_size,
                bid_notional_usd=bid_notional,
                ask_notional_usd=ask_notional,
                imbalance_ratio=ratio,
                levels_in_band=bid_levels + ask_levels,
            )
        )

    return results
