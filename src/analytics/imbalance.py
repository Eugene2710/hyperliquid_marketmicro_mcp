"""
Depth-weighted orderbook imbalance

Given an L2 order book (bids and asks at various price levels) and a set of basis-point bands from mid,
computes the bid/ask size imbalance within each band.
Imbalance is a microstructure signal that captures short-term order flow pressure —
strong bid imbalance suggests buying pressure and potential upward movement; strong ask imbalance suggests the opposite.

The computation is a pure function over already-fetched data. No I/O, no API calls — that's the venue adapter's job.
Pure functions are testable from recorded fixtures and have no hidden dependencies.
"""
from pydantic import BaseModel, Field


class Imbalance(BaseModel):
    """
    Imbalance metrics for one basis-point band around the mid-price.

    A "band" is a price window of `band_bps` basis points on each side of mid.
    For example, a 50 bps band on BTC at $65,000 covers $64,675 → $65,325, a $650 window.
    The imbalance ratio compares bid size to ask size within that window.
    """
    band_bps: float = Field(description="The half-width of this band in basis points from mid")
    bid_size: float = Field(description="Total bid size within this band, in coin units")
    ask_size: float = Field(description="Total ask size within this band, in coin units")
    bid_notional_usd: float = Field(description="Bid size x bid price summed within band")
    ask_notional_usd: float = Field(description="Ask size x ask price summed within band")
    imbalance_ratio: float = Field(
        ge=-1.0, le=1.0,
        description=(
            "(bid_notional - ask_notional) / (bid_notional + ask_notional). +1.0 = all bid-side, -1.0 = all ask-side, 0 = balanced."
        ),
    )
    levels_in_band: int = Field(
        description="The levels of bid and ask within this band, in coin units"
    )