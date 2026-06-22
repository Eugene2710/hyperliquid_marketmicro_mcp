"""Parse every recorded fixture against the ``HL*`` schemas and assert structure.

These are contract tests: each recorded real response must validate against the
schema, and the parsed result must round-trip the load-bearing fields. A failure
here means either the schema is wrong or the HL API shape drifted — exactly the
signal the raw-schema layer exists to provide.
"""

from collections.abc import Callable
from typing import Any

import pytest

from hlmcp.schemas.hl_api import (
    HLClearinghouseState,
    HLL2Book,
    HLPerpDexs,
    HLPosition,
)

# --------------------------------------------------------------------------- #
# clearinghouseState                                                          #
# --------------------------------------------------------------------------- #


def test_clearinghouse_whale_parses(load_json: Callable[[str], Any]) -> None:
    """The 34-position cross-margin whale parses and exposes all positions."""
    raw = load_json("clearinghouse_whale.json")
    state = HLClearinghouseState.model_validate(raw)

    assert len(state.assetPositions) == 34
    # Numerics stay strings — nothing is coerced in this layer.
    assert isinstance(state.marginSummary.accountValue, str)
    assert isinstance(state.assetPositions[0].position.szi, str)
    assert isinstance(state.time, int)
    # All positions in this fixture are oneWay / cross.
    assert {ap.type for ap in state.assetPositions} == {"oneWay"}
    assert {ap.position.leverage.type for ap in state.assetPositions} == {"cross"}


def test_clearinghouse_small_parses(load_json: Callable[[str], Any]) -> None:
    """A small (few-position) wallet parses with the identical shape."""
    raw = load_json("clearinghouse_small.json")
    state = HLClearinghouseState.model_validate(raw)

    assert 0 < len(state.assetPositions) <= 5
    pos = state.assetPositions[0].position
    assert pos.coin
    assert isinstance(pos.maxLeverage, int)
    assert isinstance(pos.cumFunding.allTime, str)


def test_liquidation_px_optional(load_json: Callable[[str], Any]) -> None:
    """``liquidationPx`` is ``None`` for at least one over-collateralized position.

    The whale fixture has an over-collateralized leg (HYPE) whose liquidationPx
    is JSON ``null``; confirm the Optional models it rather than failing to parse.
    """
    raw = load_json("clearinghouse_whale.json")
    state = HLClearinghouseState.model_validate(raw)

    liq_values = [ap.position.liquidationPx for ap in state.assetPositions]
    assert any(v is None for v in liq_values), "expected ≥1 null liquidationPx"
    assert all(v is None or isinstance(v, str) for v in liq_values)


def test_maxleverage_distinct_from_position_leverage(load_json: Callable[[str], Any]) -> None:
    """``maxLeverage`` (symbol ceiling) is modeled separately from leverage.value."""
    raw = load_json("clearinghouse_whale.json")
    state = HLClearinghouseState.model_validate(raw)

    pos: HLPosition = state.assetPositions[0].position
    assert pos.maxLeverage >= pos.leverage.value  # ceiling ≥ position's leverage


# --------------------------------------------------------------------------- #
# l2Book                                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fixture_name",
    [
        "l2book_btc_nsf5.json",
        "l2book_btc_nsf5_m5.json",
        "l2book_btc_nsf4.json",
        "l2book_btc_nsf3.json",
    ],
)
def test_l2book_parses_at_each_aggregation(
    load_json: Callable[[str], Any], fixture_name: str
) -> None:
    """Each captured aggregation setting parses into the two-sided book shape."""
    raw = load_json(fixture_name)
    book = HLL2Book.model_validate(raw)

    assert book.coin == "BTC"
    assert isinstance(book.time, int)
    # levels is [bids, asks]; the API caps each side at 20 levels.
    assert len(book.levels) == 2
    bids, asks = book.levels
    assert 0 < len(bids) <= 20
    assert 0 < len(asks) <= 20
    # Level fields: px/sz strings, n an int.
    assert isinstance(bids[0].px, str)
    assert isinstance(bids[0].sz, str)
    assert isinstance(bids[0].n, int)
    # Bids descend, asks ascend (basic ordering sanity).
    assert float(bids[0].px) > float(bids[-1].px)
    assert float(asks[0].px) < float(asks[-1].px)


def test_l2book_spread_field_present(load_json: Callable[[str], Any]) -> None:
    """The (undocumented-in-spike) ``spread`` field is captured when present."""
    book = HLL2Book.model_validate(load_json("l2book_btc_nsf5.json"))
    assert book.spread is not None
    assert isinstance(book.spread, str)


# --------------------------------------------------------------------------- #
# perpDexs                                                                     #
# --------------------------------------------------------------------------- #


def test_perpdexs_null_first_shape(load_json: Callable[[str], Any]) -> None:
    """The perpDexs list is null-first; ``.dexes`` excludes the native-HL slot."""
    raw = load_json("perpdexs.json")
    resp = HLPerpDexs.model_validate(raw)

    assert resp.root[0] is None  # native HL is the leading null
    assert len(resp.dexes) == len(resp.root) - 1
    assert all(d.name for d in resp.dexes)  # every named dex has a routing key


def test_perpdexs_retains_new_fields(load_json: Callable[[str], Any]) -> None:
    """Fields HL added since the spike are captured, not silently dropped.

    The known HIP-3 dex ``xyz`` should expose the newer metadata fields
    (e.g. ``subDeployers``, ``deployerFeeScale``) that the original spike did
    not document. extra="allow" plus explicit declarations keep them.
    """
    raw = load_json("perpdexs.json")
    resp = HLPerpDexs.model_validate(raw)

    xyz = next((d for d in resp.dexes if d.name == "xyz"), None)
    assert xyz is not None
    assert xyz.deployer is not None
    assert xyz.subDeployers is not None
    assert xyz.deployerFeeScale is not None
    assert xyz.assetToStreamingOiCap is not None
