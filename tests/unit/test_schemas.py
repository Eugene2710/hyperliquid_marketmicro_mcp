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
    HLFundingHistoryEntry,
    HLL2Book,
    HLMetaAndAssetCtxs,
    HLPerpDexs,
    HLPosition,
    HLPredictedFundings,
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


# --------------------------------------------------------------------------- #
# metaAndAssetCtxs / fundingHistory / predictedFundings                       #
# --------------------------------------------------------------------------- #


def test_meta_and_asset_ctxs_native_parses_index_aligned(
    load_json: Callable[[str], Any],
) -> None:
    """Native ``[meta, ctxs]`` parses with universe and ctxs the same length."""
    resp = HLMetaAndAssetCtxs.model_validate(load_json("meta_and_asset_ctxs.json"))

    assert len(resp.meta.universe) == len(resp.ctxs) == 234
    found = resp.ctx_for("BTC")
    assert found is not None
    asset, ctx = found
    assert asset.name == "BTC"
    assert ctx.funding == "0.0000125"  # recorded: the interest-rate floor
    assert resp.ctx_for("btc") is None  # lookup is case-sensitive


def test_meta_and_asset_ctxs_delisted_has_null_premium(
    load_json: Callable[[str], Any],
) -> None:
    """A delisted market (MATIC) is flagged and carries null premium/mid/impact."""
    resp = HLMetaAndAssetCtxs.model_validate(load_json("meta_and_asset_ctxs.json"))

    found = resp.ctx_for("MATIC")
    assert found is not None
    asset, ctx = found
    assert asset.isDelisted is True
    assert ctx.premium is None
    assert ctx.midPx is None
    assert ctx.impactPxs is None


def test_meta_and_asset_ctxs_hip3_prefixed_and_extra_fields(
    load_json: Callable[[str], Any],
) -> None:
    """HIP-3 (xyz) names come back dex-prefixed and HIP-3-only fields are kept."""
    resp = HLMetaAndAssetCtxs.model_validate(load_json("meta_and_asset_ctxs_xyz.json"))

    assert len(resp.meta.universe) == len(resp.ctxs)
    assert all(a.name.startswith("xyz:") for a in resp.meta.universe)
    found = resp.ctx_for("xyz:XYZ100")
    assert found is not None
    asset, _ = found
    assert asset.model_extra is not None
    assert "growthMode" in asset.model_extra


def test_funding_history_parses_oldest_first(load_json: Callable[[str], Any]) -> None:
    """168 hourly BTC settlements parse, ascending in time, ~1h apart."""
    rows = [HLFundingHistoryEntry.model_validate(r) for r in load_json("funding_history_btc.json")]

    assert len(rows) == 168
    assert all(r.coin == "BTC" for r in rows)
    times = [r.time for r in rows]
    assert times == sorted(times)
    gaps = {round((b - a) / 3_600_000) for a, b in zip(times, times[1:], strict=False)}
    assert gaps == {1}


def test_predicted_fundings_parses_with_nulls_and_missing_interval(
    load_json: Callable[[str], Any],
) -> None:
    """predictedFundings parses null venue entries and absent intervals."""
    resp = HLPredictedFundings.model_validate(load_json("predicted_fundings.json"))

    btc = resp.for_coin("BTC")
    assert btc is not None
    by_venue = dict(btc)
    assert set(by_venue) == {"BinPerp", "HlPerp", "BybitPerp"}
    assert by_venue["HlPerp"] is not None and by_venue["HlPerp"].fundingIntervalHours == 1
    assert by_venue["BinPerp"] is not None and by_venue["BinPerp"].fundingIntervalHours == 8
    entries = [f for _, venues in resp.root for _, f in venues]
    assert any(f is None for f in entries)  # venue does not list the coin
    assert any(f is not None and f.fundingIntervalHours is None for f in entries)
    assert all(":" not in coin for coin, _ in resp.root)  # native coins only
    assert resp.for_coin("NOT_A_COIN") is None
