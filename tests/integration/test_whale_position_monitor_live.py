"""Integration tests for the ``whale_position_monitor`` tool against LIVE Hyperliquid.

Marked ``@pytest.mark.integration`` (via ``pytestmark``) so they are excluded
from the default ``pytest -m "not integration"`` run. They hit the real, no-auth
public API end-to-end across the packaged curated whale set, so they can be slow
or rate-limited; run them deliberately before releases.

These assert response STRUCTURE and internal consistency (buffer math, direction
signs, sort order, freshness), not specific position values — live positions
change constantly.
"""

import pytest

from hlmcp.schemas.responses import WhalePositionMonitorResponse, WhaleWalletReport
from hlmcp.tools.whale_position_monitor import compute_whale_positions, load_curated_whales
from hlmcp.venues.hyperliquid import HyperliquidPublic

pytestmark = pytest.mark.integration


def _assert_report_consistent(report: WhaleWalletReport) -> None:
    """Assert one report's internal invariants hold (buffer math, direction signs)."""
    risk = report.account_risk
    # Buffer is exactly accountValue - crossMaintenanceMarginUsed.
    assert risk.liquidation_buffer_usd == pytest.approx(
        risk.account_value_usd - risk.cross_maintenance_margin_used_usd, rel=1e-9
    )
    # Direction is consistent with the sign of the signed size, for every position.
    for pos in report.positions:
        assert (pos.signed_size < 0) == (pos.direction == "short")
        assert pos.size == pytest.approx(abs(pos.signed_size))


async def test_whale_monitor_native_curated_set_well_formed() -> None:
    """The curated whale set returns well-formed, internally consistent native reports.

    Universal properties: every curated wallet is valid (no failures on native HL),
    reports are sorted by gross notional descending, each report's account-level
    buffer math and per-position direction signs are consistent, and freshness is
    internally consistent and recent.
    """
    curated: list[str] = load_curated_whales()
    async with HyperliquidPublic() as venue:
        resp: WhalePositionMonitorResponse = await compute_whale_positions(venue, curated)

    assert resp.include_hip3 is False
    assert resp.n_wallets_queried == len(curated)
    # Curated addresses are valid and native HL should not error → no failures.
    assert resp.failures == []

    # Reports are sorted by gross notional, descending.
    grosses = [r.aggregate.gross_notional_usd for r in resp.reports]
    assert grosses == sorted(grosses, reverse=True)

    for report in resp.reports:
        assert report.dex == ""
        assert report.positions  # a report is only emitted when it has positions
        assert report.aggregate.n_positions == len(report.positions)
        assert report.aggregate.gross_notional_usd > 0
        _assert_report_consistent(report)
        # Freshness internally consistent and recent (< 1 min old).
        assert report.freshness.staleness_ms == (
            report.freshness.fetched_at_ms - report.freshness.server_time_ms
        )
        assert report.freshness.staleness_ms < 60_000


async def test_whale_monitor_include_hip3_fans_across_dexes() -> None:
    """An opt-in HIP-3 fan-out over one curated wallet stays well-formed across dexes.

    Uses a single wallet to bound load (one wallet × all dexes). Any reports must
    carry a valid dex label and remain internally consistent; a wallet with no
    positions anywhere is a valid (empty) result.
    """
    wallet: str = load_curated_whales()[0]
    async with HyperliquidPublic() as venue:
        dex_names: set[str] = await venue._known_dex_names()
        resp: WhalePositionMonitorResponse = await compute_whale_positions(
            venue, [wallet], include_hip3=True
        )

    assert resp.include_hip3 is True
    assert resp.n_wallets_queried == 1
    for report in resp.reports:
        assert report.dex in dex_names  # "" (native) or a real HIP-3 name
        assert report.positions
        _assert_report_consistent(report)
