"""
Position aggregation and account-level liquidation risk.

Given a wallet's parsed ``clearinghouseState``, this module derives the signals the ``whale_position_monitor`` tool surfaces.

- **Per-position summary** (`summarize_position`) - direction (from the sign of ``szi``), materialized size/notional/PnL,
and a supplementary carry yield.

- **Cross-position aggregate** (function: aggregate_postions): long/short split, gross and net notional,
and a net-directional bias rtion in [-1,1] (analagous to order-book imbalance)

- **Account-level risk** (function: derive_account_risk): the HEADLINE metric. For cross-margin accounts,
the meaningful liquidation is NOT the per-position ``liquidatonPx`` (a whole-account snapshot that is often practically
menaingless - e.g a $172 liquidation price on a $2 short) but the account-level buffer
``accountValue - crossMaintenanceMarginUsed``

These are pure functions: no I/O, no async, no network. Consistent with `hlmcp.analytics.imbalance` (which materializes
floats in its ``ImbalanceBand`` result), parsing of HL's decimal *strings* happens HERE, at the analytics boundary -
never in the venue or the raw ``HL*`` schema layer. Money is parsed with hlmcp.analytics.utils.parse_decimal function
before the liquidation-buffer subtraction, then cast to ``float`` for the result models; signal ratios use
hlmcp.analytics.utils.parse_float function.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from hlmcp.analytics.utils import parse_decimal, parse_float, parse_optional_float
from hlmcp.schemas.hl_api import HLClearinghouseState, HLPosition

# Direction is encoded ENTIRELY in the sign of ``szi`` (negative = short); there is no seperate side field
Direction = Literal["long", "short"]


class PositionSummary(BaseModel):
    """
    Materialized view of one open perpetuals postion.

    The user-dacing counterpart to a raw hlmcp.schemas.hl_api.HLPosition class:
    decimal strings parsed to floats, direction resolved from the sign of ``szi``, and a carry yield derived.
    ``liquidation_px`` is retained but is supplementary for cross-margin positions - see ``AccountRisk`` class for real trigger.

    Attributes:
        coin: Trading symbol, e.g. ``"BTC"``
        direction: ``"long"`` (``szi > 0``) or ``"short"`` (``szi < 0``).
        ...
    """
    coin: str = Field(description="Trading symbol, e.g. 'BTC', 'xyz:MSTR'.")
    direction: Direction = Field(description="'long' (szi>0) or 'short' (szi<0).")
    signed_size: float = Field(description="Raw signed size szi; negative = short.")
    size: float = Field(ge=0.0, description="Absolute position size in coin units.")
    entry_px: float = Field(description="Volume-weighted average entry price.")
    notional_usd: float = Field(description="Current position notional in USD.")
    unrealized_pnl_usd: float = Field(description="Current unrealized PnL in USD.")
    return_on_equity: float = Field(description="PnL as a fraction of margin used.")
    leverage: int = Field(description="Position's current leverage multiplier.")
    leverage_type: Literal["cross", "isolated"] = Field(description="Margin mode.")
    max_leverage: int = Field(description="Symbol's leverage ceiling (not this position's).")
    margin_used_usd: float = Field(description="Margin allocated to this position, USD.")
    liquidation_px: float | None = Field(
        default=None,
        description="Per-position liquidation price; None when over-collateralized. "
        "Supplementary for cross margin — see AccountRisk for the real trigger.",
    )
    funding_since_open_usd: float = Field(
        description="Cumulative funding since open, USD (sign per HL's cumFunding convention)."
    )
    funding_yield: float | None = Field(
        default=None,
        description="funding_since_open_usd / notional_usd; None when notional is zero.",
    )


class PositionAggregate(BaseModel):
    """
    Directional aggregate across all of an account's positions.

    Splits exposure by direction (from the sign ``szi``) and reports a net directional-bias ratio analogous to
    order-book imbalance: ``+1`` = all long, ``-1`` = all short, ``0`` = notional-balanced (or no position).

    Attributes:
        n_positions: Total number of open positions aggregated.
        n_long: Count of long positions (``szi > 0``).
        n_short: Count of short positions (``szi < 0``).
        ...
    """
    n_positions: int = Field(description="Total number of open positions aggregated.")
    n_long: int = Field(ge=0, description="Count of long positions (szi>0).")
    n_short: int = Field(ge=0, description="Count of short positions (szi<0).")
    long_notional_usd: float = Field(ge=0.0, description="Sum of long-position notional, USD.")
    short_notional_usd: float = Field(ge=0.0, description="Sum of short-position notional, USD.")
    gross_notional_usd: float = Field(ge=0.0, description="Total exposure regardless of direction.")
    net_notional_usd: float = Field(description="long_notional_usd - short_notional_usd (signed).")
    net_bias: float = Field(
        ge = -1.0,
        le = 1.0,
        description = "net/gross in [-1,1]; +1 all long, -1 all short, 0 balanced/flat.",
    )

class AccountRisk(BaseModel):
    """
    Account-level liquidation risk: the HEADLINE risk metric for a wallet.

    For cross-margin accounts, per-position ``liquidation_px`` is derived from a whole-account snapshot and is often
    practically meaningless. The real trigger is when ``accountValue`` falls to ``crossMaintenanceMarginUsed``; the gap
    between them (``liquidation_buffer_usd``) is how much adverse PnL the cross pool can absorb force-closing begins.

    Basis: computed from ``crossMarginSummary`` (the cross pool that ``crossMaintenanceMarginUsed`` actually defends),
    which equals ``marginSummary`` for a pure-cross account; they diverge only when isolated positions exist.

    Attributes:
        account_value_usd: Cross-pool account value (collateral + unrealized PnL).
        ...
    """
    account_value_usd: float = Field(description="Cross-pool account value, USD.")
    cross_maintenance_margin_used_usd: float = Field(
        description="Minimum margin the cross pool must maintain, USD."
    )
    liquidation_buffer_usd: float = Field(
        description="account_value - cross_maintenance_margin_used; the headline risk metric."
    )
    liquidation_buffer_frac: float | None = Field(
        default=None,
        description="buffer / account_value; None if account value <= 0. Higher = safer.",
    )
    maintenance_margin_frac: float | None = Field(
        default=None,
        description="cross_maintenance_margin_used / account_value; None if account value <= 0.",
    )
    total_notional_usd: float = Field(description="Sum of |position notional| (cross pool), USD.")
    total_margin_used_usd: float = Field(description="Total initial margin behind positions, USD.")
    withdrawable_usd: float = Field(description="USDC immediately withdrawable, USD.")


def funding_yield(position: HLPosition) -> float | None:
    """
    Compute a position's funding carry as a fraction of its current notional.

    A rough carry signal: cumulative funding since the position opened divided by the current position notional.
    Sign follows HL's ``cumFunding`` convention (as documented on class hlmcp.schemas.hl_api.CumFunding);
    this helper does not reinterpret it. Uses ``sinceOpen`` (funding on the current position) rather than ``allTime``
    (which persists across close/reopen cycles).

    Mechanism: parse ``cumFunding.sinceOpen`` and ``positionValue`` as floats; return ``sinceOpen / positionValue``,
    or ``None`` when notional is zero (no meaningful denominator).

    Args:
        position: A parsed class `~hlmcp.schemas.hl_api.HLPosition`.

    Returns
        Funding since open as a fraction of current notional, or ``None`` if the position notional is zero.
    """
    notional: float = parse_float(position.positionValue)
    if notional == 0.0:
        return None
    return parse_float(position.cumFunding.sinceOpen) / notional

def summarize_position(position: HLPosition) -> PositionSummary:
    """
    Materialize one raw class `HLPosition` into a class `PositionSummary`.

    Mechanism: parse the decimal strings to floats; resolve direction from the sign of `szi` (negative = short);
    derive absolute size and the carry yield (function `funding_yield`); carry ``liquidation_px`` through as
    supplementary (``None`` when over-collateralized).

    Args:
        position: A parsed class `HLPosition`.

    Returns:
        A class `PositionSummary` with all numerics materialized.

    Raises:
        ValueError: If a required numeric string fails to parse (via the parse helpers) - signalling unexpected API shape.
    """
    szi: float = parse_float(position.szi)
    direction: Direction = "short" if szi < 0 else "long"

    return PositionSummary(
        coin=position.coin,
        direction=direction,
        signed_size=szi,
        size=abs(szi),
        entry_px=parse_float(position.entryPx),
        notional_usd=parse_float(position.positionValue),
        unrealized_pnl_usd=parse_float(position.unrealizedPnl),
        return_on_equity=parse_float(position.returnOnEquity),
        leverage=position.leverage.value,
        leverage_type=position.leverage.type,
        max_leverage=position.maxLeverage,
        margin_used_usd=parse_float(position.marginUsed),
        liquidation_px=parse_optional_float(position.liquidationPx), # parse_optional_float parses into Optional[float]
        funding_since_open_usd=parse_float(position.cumFunding.sinceOpen),
        funding_yield=funding_yield(position),
    )

def aggregate_positions(positions: Sequence[HLPosition]) -> PositionAggregate:
    """
    Aggregate positions into a long/short split and net directional bias.

    Mechanism: for each position, add its notional (``positionValue``) to the long or short bucket by the sign of ``szi``
    ; sum to gross an net; report ``net_bias = net/gross`` ( ``0.0`` when there is no exposure). Notional is the API's
    ``positionValue`` (already an absolute magnitude), so buckets are non-negative regardless of direction.

    Args:
        positions: The account's open positions (possibly empty). An empty sequence yields an all-zerp aggregate with
        ``net_bias = 0.0`` - a normal no-positions result, not an error.

    Returns:
        A class PositionAggregate summarizing directional exposure.
    """
    n_long: int = 0
    n_short: int = 0
    long_notional: float = 0.0
    short_notional: float = 0.0

    for position in positions:
        szi: float = parse_float(position.szi)
        notional: float = parse_float(position.positionValue)
        if szi < 0:
            n_short += 1
            short_notional += notional
        else:
            n_long += 1
            long_notional += notional

    gross: float = long_notional + short_notional
    net: float = long_notional - short_notional
    net_bias: float = net / gross if gross > 0.0 else 0.0

    return PositionAggregate(
        n_positions=len(positions),
        n_long=n_long,
        n_short=n_short,
        long_notional_usd=long_notional,
        short_notional_usd=short_notional,
        gross_notional_usd=gross,
        net_notional_usd=net,
        net_bias=net_bias,
    )

def derive_account_risk(state: HLClearinghouseState) -> AccountRisk:
    """
    Derive account-level liquidation risk, the headline whale-monitoring metric.

    Mechanism: parse ``crossMarginSummary.accountValue`` and ``crossMaintenanceMarginUsed`` as exact decimal.Decimal;
    the buffer is the difference. Fractions are the buffer/maintenance over account value (``None`` when account value
    is not positive, to avoid a divide-by-zero and signal an undefined ratio). Results are cast to float for the
    response model.

    Basis is ``crossMarginSummary`` because ``crossMaintenanceMarginUsed`` governs the cross pool specifically;
    for a pure-cross account this equals ``marginSummary``.

    Args:
        state: A parsed class `~hlmcp.schemas.hl_api.HLClearinghouseState`.

    Returns:
        A class `AccountRisk` with the account-level buffer as the headline.

    Raises:
        ValueError: If a required money string dails to parse, which is an unexpected API shape.
    """
    cross = state.crossMarginSummary
    account_value: Decimal = parse_decimal(cross.accountValue)
    maintenance: Decimal = parse_decimal(state.crossMaintenanceMarginUsed)
    buffer: Decimal = account_value - maintenance

    buffer_frac: float | None = None
    maintenance_frac: float | None = None
    if account_value > 0:
        buffer_frac = float(buffer / account_value)
        maintenance_frac = float(maintenance / account_value)

    return AccountRisk(
        account_value_usd=float(account_value),
        cross_maintenance_margin_used_usd=float(maintenance),
        liquidation_buffer_usd=float(buffer),
        liquidation_buffer_frac=buffer_frac,
        maintenance_margin_frac=maintenance_frac,
        total_notional_usd=parse_float(cross.totalNtlPos),
        total_margin_used_usd=parse_float(cross.totalMarginUsed),
        withdrawable_usd=parse_float(state.withdrawable),
    )
