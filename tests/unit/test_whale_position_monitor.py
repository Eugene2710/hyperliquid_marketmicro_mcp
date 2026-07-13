"""Unit tests for the ``whale_position_monitor`` tool logic.

Exercise the fan-out orchestration against a MOCKED venue (no network). The fake
venue serves configured states/exceptions per wallet (native) or per (wallet,
dex) (HIP-3) and records which wallets it was asked for, so tests can assert both
the assembled response and that malformed wallets never reach the network.
"""

from collections.abc import Callable
from typing import Any

import pytest

from hlmcp.schemas.hl_api import HLClearinghouseState
from hlmcp.schemas.responses import WhalePositionMonitorResponse
from hlmcp.tools.whale_position_monitor import compute_whale_positions
from hlmcp.venues.errors import HLAPIError

# Canonical throwaway wallets (already 0x + lowercase, so normalize is a no-op).
WALLET_A: str = "0x" + "ab" * 20
WALLET_B: str = "0x" + "cd" * 20
WALLET_C: str = "0x" + "ef" * 20

# States keyed to per-wallet fixtures below.
_PerDexStates = dict[str, HLClearinghouseState | Exception]


class _FakeVenue:
    """Stand-in for HyperliquidPublic serving configured fan-out results.

    Attributes:
        batch_calls: The wallet lists passed to each native batch call, in order.
        dex_calls: The wallets passed to each per-dex fan-out call, in order.
    """

    def __init__(
        self,
        *,
        native: _PerDexStates | None = None,
        per_user_dexes: dict[str, _PerDexStates | Exception] | None = None,
    ) -> None:
        """Store per-wallet (native) and per-(wallet, dex) (HIP-3) results."""
        self._native: _PerDexStates = native or {}
        self._per_user_dexes: dict[str, _PerDexStates | Exception] = per_user_dexes or {}
        self.batch_calls: list[list[str]] = []
        self.dex_calls: list[str] = []

    async def fetch_clearinghouse_states_batch(
        self, users: list[str], dex: str = "", per_request_timeout_s: float | None = None
    ) -> _PerDexStates:
        """Return the configured native state (or exception-value) for each user."""
        self.batch_calls.append(list(users))
        return {user: self._native[user] for user in users}

    async def fetch_all_dexes_for_user(
        self, user: str, per_request_timeout_s: float | None = None
    ) -> _PerDexStates:
        """Return the per-dex map for ``user``; raise a configured wallet-level error."""
        self.dex_calls.append(user)
        value = self._per_user_dexes[user]
        if isinstance(value, Exception):
            raise value
        return value


def _flat_state(time_ms: int = 1_700_000_000_000) -> HLClearinghouseState:
    """Build a valid clearinghouse state with no open positions."""
    return HLClearinghouseState.model_validate(
        {
            "marginSummary": {
                "accountValue": "1000.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "1000.0",
                "totalMarginUsed": "0.0",
            },
            "crossMarginSummary": {
                "accountValue": "1000.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "1000.0",
                "totalMarginUsed": "0.0",
            },
            "crossMaintenanceMarginUsed": "0.0",
            "withdrawable": "1000.0",
            "assetPositions": [],
            "time": time_ms,
        }
    )


# --------------------------------------------------------------------------- #
# native fan-out                                                              #
# --------------------------------------------------------------------------- #


async def test_native_two_wallets_sorted_by_gross(load_json: Callable[[str], Any]) -> None:
    """Two wallets with positions produce two reports, biggest gross notional first."""
    whale = HLClearinghouseState.model_validate(load_json("clearinghouse_whale.json"))
    small = HLClearinghouseState.model_validate(load_json("clearinghouse_small.json"))
    venue = _FakeVenue(native={WALLET_A: small, WALLET_B: whale})  # A smaller, B bigger

    resp = await compute_whale_positions(venue, [WALLET_A, WALLET_B])  # type: ignore[arg-type]

    assert isinstance(resp, WhalePositionMonitorResponse)
    assert resp.include_hip3 is False
    assert resp.n_wallets_queried == 2
    assert resp.failures == []
    assert venue.batch_calls == [[WALLET_A, WALLET_B]]
    # Whale (bigger gross) sorts ahead of the small wallet regardless of input order.
    assert [r.wallet for r in resp.reports] == [WALLET_B, WALLET_A]
    gross = [r.aggregate.gross_notional_usd for r in resp.reports]
    assert gross[0] > gross[1]
    # Every report is on the native dex and carries the account-level headline.
    assert all(r.dex == "" for r in resp.reports)
    assert resp.reports[0].account_risk.liquidation_buffer_usd == pytest.approx(
        12440406.8187709995
    )


async def test_partial_failure_surfaced_not_fatal(load_json: Callable[[str], Any]) -> None:
    """One wallet's API error becomes a failure; the other still returns a report."""
    small = HLClearinghouseState.model_validate(load_json("clearinghouse_small.json"))
    venue = _FakeVenue(native={WALLET_A: small, WALLET_B: HLAPIError(500, "boom", {})})

    resp = await compute_whale_positions(venue, [WALLET_A, WALLET_B])  # type: ignore[arg-type]

    assert [r.wallet for r in resp.reports] == [WALLET_A]
    assert len(resp.failures) == 1
    assert resp.failures[0].wallet == WALLET_B
    assert resp.failures[0].dex == ""
    assert "HLAPIError" in resp.failures[0].error


async def test_malformed_wallet_becomes_failure_without_fetch(
    load_json: Callable[[str], Any],
) -> None:
    """A bad address is rejected client-side (a failure) and never sent to the venue."""
    small = HLClearinghouseState.model_validate(load_json("clearinghouse_small.json"))
    venue = _FakeVenue(native={WALLET_A: small})

    resp = await compute_whale_positions(venue, [WALLET_A, "not-a-wallet"])  # type: ignore[arg-type]

    assert [r.wallet for r in resp.reports] == [WALLET_A]
    assert resp.n_wallets_queried == 2  # both were queried; one was invalid
    assert [f.wallet for f in resp.failures] == ["not-a-wallet"]
    # Only the valid wallet reached the network — the malformed one short-circuited.
    assert venue.batch_calls == [[WALLET_A]]


async def test_flat_wallet_yields_no_report_no_failure() -> None:
    """A wallet with no open positions produces neither a report nor a failure."""
    venue = _FakeVenue(native={WALLET_A: _flat_state()})

    resp = await compute_whale_positions(venue, [WALLET_A])  # type: ignore[arg-type]

    assert resp.reports == []
    assert resp.failures == []
    assert resp.n_wallets_queried == 1


async def test_duplicate_wallets_deduped(load_json: Callable[[str], Any]) -> None:
    """A wallet passed twice is queried once and produces one report."""
    small = HLClearinghouseState.model_validate(load_json("clearinghouse_small.json"))
    venue = _FakeVenue(native={WALLET_A: small})

    resp = await compute_whale_positions(venue, [WALLET_A, WALLET_A])  # type: ignore[arg-type]

    assert resp.n_wallets_queried == 1
    assert len(resp.reports) == 1
    assert venue.batch_calls == [[WALLET_A]]


async def test_top_level_freshness_is_worst_case(load_json: Callable[[str], Any]) -> None:
    """Top-level freshness uses the OLDEST snapshot (max staleness) across reports."""
    whale = HLClearinghouseState.model_validate(load_json("clearinghouse_whale.json"))  # older
    small = HLClearinghouseState.model_validate(load_json("clearinghouse_small.json"))  # newer
    assert whale.time < small.time  # sanity: whale snapshot is the older one
    venue = _FakeVenue(native={WALLET_A: whale, WALLET_B: small})
    now_ms = small.time + 1000

    resp = await compute_whale_positions(venue, [WALLET_A, WALLET_B], now_ms=now_ms)  # type: ignore[arg-type]

    assert resp.freshness.server_time_ms == whale.time  # the oldest snapshot
    assert resp.freshness.fetched_at_ms == now_ms
    assert resp.freshness.staleness_ms == now_ms - whale.time


async def test_empty_wallets_raises() -> None:
    """An empty wallet list is a caller error."""
    venue = _FakeVenue()
    with pytest.raises(ValueError, match="at least one address"):
        await compute_whale_positions(venue, [])  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# HIP-3 fan-out                                                               #
# --------------------------------------------------------------------------- #


async def test_include_hip3_fans_across_dexes(load_json: Callable[[str], Any]) -> None:
    """include_hip3 fans across dexes; reports carry the dex; flat dexes are dropped."""
    whale = HLClearinghouseState.model_validate(load_json("clearinghouse_whale.json"))
    small = HLClearinghouseState.model_validate(load_json("clearinghouse_small.json"))
    venue = _FakeVenue(
        per_user_dexes={WALLET_A: {"": small, "xyz": whale, "flx": _flat_state()}}
    )

    resp = await compute_whale_positions(venue, [WALLET_A], include_hip3=True)  # type: ignore[arg-type]

    assert resp.include_hip3 is True
    assert venue.dex_calls == [WALLET_A]
    # flx is flat -> dropped; xyz (whale, bigger gross) sorts ahead of native small.
    assert [(r.wallet, r.dex) for r in resp.reports] == [(WALLET_A, "xyz"), (WALLET_A, "")]
    assert resp.failures == []


async def test_include_hip3_wallet_level_failure_surfaced() -> None:
    """A wallet-level raise during HIP-3 fan-out (e.g. perpDexs down) is a failure."""
    venue = _FakeVenue(per_user_dexes={WALLET_A: HLAPIError(500, "perpDexs down", {})})

    resp = await compute_whale_positions(venue, [WALLET_A], include_hip3=True)  # type: ignore[arg-type]

    assert resp.reports == []
    assert [f.wallet for f in resp.failures] == [WALLET_A]
    assert resp.failures[0].dex == ""
    assert "HLAPIError" in resp.failures[0].error
