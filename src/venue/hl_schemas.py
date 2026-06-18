from typing import Literal
from pydantic import BaseModel, Field


class Leverage(BaseModel):
    """
    Leverage configuration for a single position.

    Hyperliquid supports two margin modes:
      - `cross`: this position shares collateral with all other cross-margin
                 positions in the account. Their PnL pools defend against
                 each other's losses.
      - `isolated`: this position has its own dedicated chunk of margin;
                    cannot draw on the rest of the account if it's losing.
    """
    type: Literal["cross", "isolated"]
    value: int = Field(description="Current leverage multiplier (e.g. 5, 10, 20)")


class CumFunding(BaseModel):
    """
    Cumulative funding paid (negaitve) or received (positive) on a position.

    All values are USD amounts, serialized as strings.

    The three timeframes have subtle but important distinctions:
      - `allTime`: total funding accumulated on this coin across the wallet's entire history.
        Persists across position close/reopen cycles.
      - `sinceOpen`: funding accumulated since the *current* position was opened
        (i.e., since size went from 0 to non-zero).
      - `sinceChange`: funding accumulated since the *last size change* of current position(any add or trim resets this).

    A wallet that has closed and reopened a position will have `allTime` ≠ `sinceOpen`.
    """
    allTime: str
    sinceOpen: str
    sinceChange: str


class HLPosition(BaseModel):
    """
    A single open perpetuals position on Hyperliquid.

    All numeric fields are serialized as strings for decimal precision; callers that want floats must parse explicitly.
    `szi` is the *signed* size - negative values indicate shorts. Direction is encoded entirely in the sign of `szi`.
    """
    coin: str = Field(description="Trading symbol, e.g. 'BTC', 'ETH', 'xyz:MSTR'")
    szi: str = Field(description="Signed position size; negative = short")
    leverage: Leverage
    entryPx: str = Field(description="Volume-weighted average entry price")
    positionValue: str = Field(description="Current notional value in USD")
    unrealizedPnl: str = Field(description="Current unrealized PnL in USD")
    returnOnEquity: str = Field(description="PnL as a fraction of margin used")
    liquidationPx: str | None = Field(
        description=(
            "Price at which this position would liquidate, given the current "
            "state of the rest of the account. Null when over-collateralized."
        ),
    )
    marginUsed: str = Field(description="Margin allocated to this position")
    maxLeverage: int  = Field(
        description=(
            "The symbol's leverage ceiling (set by HL), not necessarily this "
            "position's current leverage setting. See `leverage.value` for that."
        ),
    )
    cumFunding: CumFunding


class HLAssetPosition(BaseModel):
    """
    One entry in a wallet's `assetPositions` list.

    Wraps an `HLPosition` with the position-direction model:
      - `oneWay`: the account has a single net position per symbol. Sells beyond size flip to a short. Most common mode.
      - `hedged`: the account can hold long and short on the same symbol simultaneously, as separate positions. Niche use.

    In `oneWay` mode, each symbol appears at most once in `assetPositions`.
    In `hedged` mode, a symbol may appear twice (one long, one short entry).
    """
    type: Literal["oneWay", "hedged"]
    position: HLPosition


class HLMarginSummary(BaseModel):
    """
    Account-level margin and exposure snapshot.

    All values are USD, serialized as strings.

    For cross-margin accounts, `marginSummary` and `crossMarginSummary` are typically identical.
    They diverge when some positions are in isolated mode: the `cross*` summary only reflects the cross-margin subset.
    """
    accountValue: str = Field(description="Total account value (collateral + unrealized PnL)")
    totalNtlPos: str = Field(description="Sum of |position notional| across all positions")
    totalRawUsd: str = Field(description="Raw USDC balance, ignoring unrealized PnL")
    totalMarginUsed: str = Field(description="Total margin locked behind positions")


class HLClearinghouseState(BaseModel):
    """
    Complete response from the `clearinghouseState` info endpoint.

    The top-level shape returned by querying a single wallet on a single dex
    (native HL by default, or a HIP-3 deployment via the `dex` field).

    Risk-monitoring note: for cross-margin accounts, the meaningful liquidation threshold is `accountValue
    - crossMaintenanceMarginUsed`, *not* the per-position `liquidationPx` fields.
    The per-position values are derived diagnostics; the account-level gap is the real trigger.
    """
    marginSummary: HLMarginSummary
    crossMarginSummary: HLMarginSummary
    crossMaintenanceMarginUsed: str = Field(
        description=(
            "Minimum margin the cross-margin pool must maintain to avoid "
            "liquidation. When accountValue falls toward this number, "
            "cross positions start force-closing."
        ),
    )
    withdrawable: str = Field(
        description=(
            "USDC immediately available for withdrawal — collateral not "
            "locked behind positions or open orders."
        ),
    )
    assetPositions: list[HLAssetPosition]
    time: int = Field(description="Server timestamp in milliseconds since epoch")