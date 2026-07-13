"""Unit tests for the pure position analytics.

Exercise ``summarize_position``, ``aggregate_positions``, ``derive_account_risk``,
and ``funding_yield`` against recorded fixtures (a 34-position cross-margin whale,
a 3-position wallet) with hand-checked expected values, plus small synthetic
states for the edge cases the fixtures don't cover (zero notional, zero account
value). No network — these are pure functions.
"""

from collections.abc import Callable
from typing import Any

import pytest

from hlmcp.analytics.positions import (
    aggregate_positions,
    derive_account_risk,
    funding_yield,
    summarize_position,
)
from hlmcp.schemas.hl_api import HLClearinghouseState, HLPosition

# A complete position dict (all required HLPosition fields), used as a base that
# individual tests override to isolate one behavior. Merged via
# ``{**_BASE_POSITION, **overrides}`` so overrides win on shared keys.
_BASE_POSITION: dict[str, Any] = {
    "coin": "TEST",
    "szi": "10.0",
    "leverage": {"type": "cross", "value": 5},
    "entryPx": "100.0",
    "positionValue": "1000.0",
    "unrealizedPnl": "50.0",
    "returnOnEquity": "0.5",
    "liquidationPx": "80.0",
    "marginUsed": "200.0",
    "maxLeverage": 10,
    "cumFunding": {"allTime": "5.0", "sinceOpen": "3.0", "sinceChange": "1.0"},
}


def _position(**overrides: Any) -> HLPosition:
    """Build an HLPosition from the base dict with the given field overrides."""
    data: dict[str, Any] = {**_BASE_POSITION, **overrides}
    return HLPosition.model_validate(data)


def _by_coin(state: HLClearinghouseState, coin: str) -> HLPosition:
    """Return the position for ``coin`` from a parsed clearinghouse state."""
    for ap in state.assetPositions:
        if ap.position.coin == coin:
            return ap.position
    raise AssertionError(f"{coin} not in fixture")


# --------------------------------------------------------------------------- #
# summarize_position                                                          #
# --------------------------------------------------------------------------- #


def test_summarize_short_position_direction_and_size(load_json: Callable[[str], Any]) -> None:
    """A negative szi resolves to 'short' with a positive absolute size."""
    whale = HLClearinghouseState.model_validate(load_json("clearinghouse_whale.json"))
    atom = summarize_position(_by_coin(whale, "ATOM"))  # szi -62371.56

    assert atom.direction == "short"
    assert atom.signed_size == pytest.approx(-62371.56)
    assert atom.size == pytest.approx(62371.56)
    assert atom.leverage == 5
    assert atom.leverage_type == "cross"
    assert atom.max_leverage == 5


def test_summarize_long_position_null_liquidation_px(load_json: Callable[[str], Any]) -> None:
    """A positive szi resolves to 'long'; a null liquidationPx materializes to None."""
    whale = HLClearinghouseState.model_validate(load_json("clearinghouse_whale.json"))
    hype = summarize_position(_by_coin(whale, "HYPE"))  # szi +68434.82, liquidationPx null

    assert hype.direction == "long"
    assert hype.size == pytest.approx(68434.82)
    assert hype.liquidation_px is None
    assert hype.notional_usd == pytest.approx(4668075.9418400005)


def test_summarize_funding_yield_matches_helper() -> None:
    """funding_yield on the summary equals sinceOpen / positionValue."""
    pos = _position(
        positionValue="1000.0",
        cumFunding={"allTime": "9", "sinceOpen": "-30", "sinceChange": "1"},
    )
    summary = summarize_position(pos)

    assert summary.funding_since_open_usd == pytest.approx(-30.0)
    assert summary.funding_yield == pytest.approx(-30.0 / 1000.0)


# --------------------------------------------------------------------------- #
# funding_yield edge case                                                     #
# --------------------------------------------------------------------------- #


def test_funding_yield_zero_notional_is_none() -> None:
    """A zero position notional yields None (no meaningful denominator)."""
    assert funding_yield(_position(positionValue="0.0")) is None


# --------------------------------------------------------------------------- #
# aggregate_positions                                                         #
# --------------------------------------------------------------------------- #


def test_aggregate_whale_split_and_bias(load_json: Callable[[str], Any]) -> None:
    """The 34-position whale is 1 long (HYPE) / 33 short; net bias is finite in [-1,1]."""
    whale = HLClearinghouseState.model_validate(load_json("clearinghouse_whale.json"))
    agg = aggregate_positions([ap.position for ap in whale.assetPositions])

    assert agg.n_positions == 34
    assert agg.n_long == 1
    assert agg.n_short == 33
    assert agg.gross_notional_usd == pytest.approx(agg.long_notional_usd + agg.short_notional_usd)
    assert agg.net_notional_usd == pytest.approx(agg.long_notional_usd - agg.short_notional_usd)
    assert -1.0 <= agg.net_bias <= 1.0
    # HYPE alone ($4.67M) outweighs the summed shorts here, so net bias is long (>0).
    assert agg.net_bias > 0.0


def test_aggregate_all_short_bias_is_minus_one() -> None:
    """Two shorts and no longs give net_bias exactly -1.0."""
    positions = [
        _position(coin="A", szi="-1.0", positionValue="100.0"),
        _position(coin="B", szi="-2.0", positionValue="300.0"),
    ]
    agg = aggregate_positions(positions)

    assert agg.n_long == 0
    assert agg.n_short == 2
    assert agg.long_notional_usd == 0.0
    assert agg.short_notional_usd == pytest.approx(400.0)
    assert agg.net_bias == pytest.approx(-1.0)


def test_aggregate_empty_is_flat() -> None:
    """No positions is a normal flat result: all zeros, net_bias 0.0 (no divide-by-zero)."""
    agg = aggregate_positions([])

    assert agg.n_positions == 0
    assert agg.gross_notional_usd == 0.0
    assert agg.net_bias == 0.0


# --------------------------------------------------------------------------- #
# derive_account_risk                                                         #
# --------------------------------------------------------------------------- #


def test_account_risk_buffer_headline(load_json: Callable[[str], Any]) -> None:
    """Buffer = accountValue - crossMaintenanceMarginUsed, the headline metric."""
    whale = HLClearinghouseState.model_validate(load_json("clearinghouse_whale.json"))
    risk = derive_account_risk(whale)

    # 12985854.2078639995 - 545447.389093 = 12440406.8187709995
    assert risk.account_value_usd == pytest.approx(12985854.2078639995)
    assert risk.cross_maintenance_margin_used_usd == pytest.approx(545447.389093)
    assert risk.liquidation_buffer_usd == pytest.approx(12440406.8187709995)
    assert risk.liquidation_buffer_frac == pytest.approx(12440406.8187709995 / 12985854.2078639995)
    assert risk.maintenance_margin_frac == pytest.approx(545447.389093 / 12985854.2078639995)
    assert risk.withdrawable_usd == pytest.approx(11805821.4801180009)


def test_account_risk_small_wallet(load_json: Callable[[str], Any]) -> None:
    """The 3-position wallet's buffer and fractions parse and derive correctly."""
    small = HLClearinghouseState.model_validate(load_json("clearinghouse_small.json"))
    risk = derive_account_risk(small)

    # 319194.558725 - 47968.681271 = 271225.877454
    assert risk.liquidation_buffer_usd == pytest.approx(271225.877454)
    assert risk.total_notional_usd == pytest.approx(3698519.1897200001)
    assert risk.withdrawable_usd == pytest.approx(0.0)


def test_account_risk_zero_account_value_fracs_are_none() -> None:
    """A zero account value makes the fractions None (undefined), not a divide error."""
    zero = HLClearinghouseState.model_validate(
        {
            "marginSummary": {
                "accountValue": "0.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "0.0",
                "totalMarginUsed": "0.0",
            },
            "crossMarginSummary": {
                "accountValue": "0.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "0.0",
                "totalMarginUsed": "0.0",
            },
            "crossMaintenanceMarginUsed": "0.0",
            "withdrawable": "0.0",
            "assetPositions": [],
            "time": 1_700_000_000_000,
        }
    )
    risk = derive_account_risk(zero)

    assert risk.account_value_usd == 0.0
    assert risk.liquidation_buffer_usd == 0.0
    assert risk.liquidation_buffer_frac is None
    assert risk.maintenance_margin_frac is None
