"""Unit tests for the ``funding_carry`` tool logic.

Exercise the tool's orchestration against a MOCKED venue -- no network. The fake
venue serves recorded fixtures and records which endpoints were called, so we can
assert the ordering guarantees (the coin is validated against the ctx universe
BEFORE ``fundingHistory`` is called -- an unknown coin there is an HTTP 500 that
the retry policy would waste attempts on) and the HIP-3 branch (no
``predictedFundings``).
"""

from collections.abc import Callable
from typing import Any

import pytest

from hlmcp.analytics.funding import HOURS_PER_YEAR
from hlmcp.schemas.hl_api import HLFundingHistoryEntry, HLMetaAndAssetCtxs, HLPredictedFundings
from hlmcp.tools.funding_carry import MAX_LOOKBACK_HOURS, compute_funding_carry, dex_for_coin

_HOUR_MS: int = 3_600_000

# Recorded fundingHistory fixture: 168 BTC rows; newest settlement at this time.
_LAST_SETTLEMENT_MS: int = 1_791_208_800_034


class _FakeVenue:
    """A stand-in for HyperliquidPublic serving fixtures and logging calls.

    Attributes:
        calls: ``(method, args)`` for every fetch, in call order.
    """

    def __init__(
        self,
        load_fixture: Callable[[str], Any],
        history: list[HLFundingHistoryEntry] | None = None,
    ) -> None:
        """Load the recorded fixtures; ``history`` overrides the BTC history rows."""
        self._native: HLMetaAndAssetCtxs = HLMetaAndAssetCtxs.model_validate(
            load_fixture("meta_and_asset_ctxs.json")
        )
        self._xyz: HLMetaAndAssetCtxs = HLMetaAndAssetCtxs.model_validate(
            load_fixture("meta_and_asset_ctxs_xyz.json")
        )
        self._history: list[HLFundingHistoryEntry] = (
            history
            if history is not None
            else [
                HLFundingHistoryEntry.model_validate(r)
                for r in load_fixture("funding_history_btc.json")
            ]
        )
        self._predicted: HLPredictedFundings = HLPredictedFundings.model_validate(
            load_fixture("predicted_fundings.json")
        )
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch_meta_and_asset_ctxs(self, dex: str = "") -> HLMetaAndAssetCtxs:
        """Return the native or xyz ctx fixture; any other dex is unknown."""
        self.calls.append(("meta", (dex,)))
        if dex == "":
            return self._native
        if dex == "xyz":
            return self._xyz
        raise ValueError(f"unknown dex {dex!r}")

    async def fetch_funding_history(
        self, coin: str, start_ms: int, end_ms: int
    ) -> list[HLFundingHistoryEntry]:
        """Record the window and return the configured rows."""
        self.calls.append(("history", (coin, start_ms, end_ms)))
        return self._history

    async def fetch_predicted_fundings(self) -> HLPredictedFundings:
        """Record the call and return the predictedFundings fixture."""
        self.calls.append(("predicted", ()))
        return self._predicted

    def called(self, method: str) -> bool:
        """Return whether ``method`` was called at least once."""
        return any(m == method for m, _ in self.calls)


def test_dex_for_coin() -> None:
    """Native symbols map to ''; HIP-3 symbols map to their prefix."""
    assert dex_for_coin("BTC") == ""
    assert dex_for_coin("xyz:XYZ100") == "xyz"


async def test_native_btc_end_to_end(load_json: Callable[[str], Any]) -> None:
    """BTC: current/carry/realized/cross-venue populated and mutually consistent."""
    venue = _FakeVenue(load_json)
    now: int = _LAST_SETTLEMENT_MS + 10 * 60_000  # 10 minutes after newest settlement

    resp = await compute_funding_carry(venue, "BTC", now_ms=now)  # type: ignore[arg-type]

    assert resp.coin == "BTC" and resp.dex == ""
    assert resp.current.hourly_rate == pytest.approx(0.0000125)
    assert resp.current.annualized_simple == pytest.approx(0.0000125 * HOURS_PER_YEAR)
    assert resp.carry_annualized_long == pytest.approx(-resp.current.annualized_simple)
    assert resp.carry_annualized_short == pytest.approx(resp.current.annualized_simple)
    assert resp.realized.n_settlements == 168
    assert resp.cross_venue is not None
    by_venue = {v.venue: v for v in resp.cross_venue}
    assert by_venue["BinPerp"].hourly_rate == pytest.approx(0.00001923 / 8)
    assert by_venue["HlPerp"].hourly_rate == pytest.approx(0.0000125)
    assert resp.next_settlement_ms % _HOUR_MS == 0
    assert 0 < resp.next_settlement_ms - now <= _HOUR_MS
    # ctx fetched first, then history over [now - 168h, now].
    assert venue.calls[0] == ("meta", ("",))
    assert ("history", ("BTC", now - 168 * _HOUR_MS, now)) in venue.calls
    assert venue.called("predicted")


async def test_freshness_anchored_on_newest_settlement(load_json: Callable[[str], Any]) -> None:
    """With an injected now, staleness = now - newest settlement time, exactly."""
    now: int = _LAST_SETTLEMENT_MS + 10 * 60_000

    resp = await compute_funding_carry(_FakeVenue(load_json), "BTC", now_ms=now)  # type: ignore[arg-type]

    assert resp.freshness.server_time_ms == _LAST_SETTLEMENT_MS
    assert resp.freshness.fetched_at_ms == now
    assert resp.freshness.staleness_ms == 10 * 60_000


async def test_freshness_falls_back_to_fetch_time_without_settlements(
    load_json: Callable[[str], Any],
) -> None:
    """No settlements in the window: realized is empty and staleness is 0."""
    now: int = 1_800_000_000_000

    resp = await compute_funding_carry(
        _FakeVenue(load_json, history=[]),
        "BTC",
        now_ms=now,  # type: ignore[arg-type]
    )

    assert resp.realized.n_settlements == 0
    assert resp.realized.mean_hourly_rate is None
    assert resp.freshness.server_time_ms == now
    assert resp.freshness.staleness_ms == 0


async def test_hip3_skips_predicted_fundings(load_json: Callable[[str], Any]) -> None:
    """A HIP-3 coin routes to its dex and never calls predictedFundings."""
    venue = _FakeVenue(load_json)

    resp = await compute_funding_carry(venue, "xyz:XYZ100", now_ms=1_800_000_000_000)  # type: ignore[arg-type]

    assert resp.dex == "xyz"
    assert resp.cross_venue is None
    assert venue.calls[0] == ("meta", ("xyz",))
    assert not venue.called("predicted")


@pytest.mark.parametrize("coin", ["btc", "NOT_A_COIN", "xyz:NOPE"])
async def test_unknown_coin_raises_before_history(
    coin: str, load_json: Callable[[str], Any]
) -> None:
    """Unknown symbols (incl. wrong case) raise ValueError; history never fetched."""
    venue = _FakeVenue(load_json)

    with pytest.raises(ValueError, match="not a listed perp"):
        await compute_funding_carry(venue, coin)  # type: ignore[arg-type]

    assert not venue.called("history")
    assert not venue.called("predicted")


async def test_delisted_coin_raises_before_history(load_json: Callable[[str], Any]) -> None:
    """A delisted market (MATIC) raises ValueError; history never fetched."""
    venue = _FakeVenue(load_json)

    with pytest.raises(ValueError, match="delisted"):
        await compute_funding_carry(venue, "MATIC")  # type: ignore[arg-type]

    assert not venue.called("history")


async def test_unknown_dex_raises(load_json: Callable[[str], Any]) -> None:
    """An unknown dex prefix propagates the venue's ValueError; history never fetched."""
    venue = _FakeVenue(load_json)

    with pytest.raises(ValueError, match="dex"):
        await compute_funding_carry(venue, "nodex:BTC")  # type: ignore[arg-type]

    assert not venue.called("history")


@pytest.mark.parametrize("hours", [0, -1, MAX_LOOKBACK_HOURS + 1])
async def test_lookback_out_of_range_raises_before_network(
    hours: int, load_json: Callable[[str], Any]
) -> None:
    """lookback_hours outside 1..720 raises before ANY venue call."""
    venue = _FakeVenue(load_json)

    with pytest.raises(ValueError, match="lookback_hours"):
        await compute_funding_carry(venue, "BTC", hours)  # type: ignore[arg-type]

    assert venue.calls == []


@pytest.mark.parametrize("hours", [1, MAX_LOOKBACK_HOURS])
async def test_lookback_bounds_accepted(hours: int, load_json: Callable[[str], Any]) -> None:
    """Both ends of the range are accepted and set the history window."""
    venue = _FakeVenue(load_json)
    now: int = 1_800_000_000_000

    resp = await compute_funding_carry(venue, "BTC", hours, now_ms=now)  # type: ignore[arg-type]

    assert resp.lookback_hours == hours
    assert ("history", ("BTC", now - hours * _HOUR_MS, now)) in venue.calls
