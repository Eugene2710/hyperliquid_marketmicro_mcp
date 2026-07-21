"""``whale_position_monitor`` tool: positions + account-level risk across wallets.

Thin orchestration over the three layers: fan out ``clearinghouseState`` across a
set of wallets (and, when ``include_hip3`` is set, across every HIP-3 dex too),
run each result through the pure position analytics, and assemble per-wallet
reports whose HEADLINE is account-level liquidation risk. No computation lives in
the venue; no I/O lives in analytics; this file is the only place they meet.

Design choices (api_spike_findings.md):

- **Account-level risk is the headline, not per-position ``liquidationPx``.** For
  cross-margin accounts the per-position value is a whole-account snapshot that is
  often meaningless (Q2); the real trigger is ``accountValue -
  crossMaintenanceMarginUsed`` (see :func:`~hlmcp.analytics.positions.derive_account_risk`).
- **HIP-3 fan-out is opt-in (``include_hip3=False`` default).** A complete whale
  view needs all dexes, but most usage is native-HL-only and the fan-out is
  slower (architecture open question). Unknown-dex 500s are already prevented by
  client-side validation in the venue.
- **Partial failures are surfaced, not fatal.** The venue's batch/fan-out returns
  exceptions as values (Q5); one bad wallet or one dex's API error becomes a
  :class:`WhalePositionFailure`, never sinking the whole call. Malformed addresses
  are rejected client-side up front and reported the same way.
- **Empty ``assetPositions`` is normal.** A wallet with no positions on a snapshot
  produces no report (and no failure); absence means "no open positions".

Data-age caveat: HL REST snapshots are ~500ms stale before network latency (Q3).
Every report carries its own :class:`FreshnessMeta`; the top-level one is the
worst case. Research/slow-loop grade, NOT HFT.
"""

import asyncio
import json
import time
from collections.abc import Sequence
from importlib.resources import files

from hlmcp.analytics.positions import aggregate_positions, derive_account_risk, summarize_position
from hlmcp.analytics.utils import normalize_wallet
from hlmcp.schemas.hl_api import HLClearinghouseState, HLPosition
from hlmcp.schemas.responses import (
    FreshnessMeta,
    WhalePositionFailure,
    WhalePositionMonitorResponse,
    WhaleWalletReport,
)
from hlmcp.venues.hyperliquid import NATIVE_HL_DEX, HyperliquidPublic

# The packaged curated-whale file (see src/hlmcp/data/curated_whales.json). Kept
# INSIDE the package so it ships in the wheel and resolves after install.
_CURATED_WHALES_RESOURCE: str = "curated_whales.json"

# A per-dex fan-out result: each dex name maps to its state or the exception
# raised fetching it (exceptions-as-values, per the venue's batch contract).
_PerDexStates = dict[str, HLClearinghouseState | Exception]


def load_curated_whales() -> list[str]:
    """Load the default curated whale wallet set shipped with the package.

    The small, provenance-documented default input for
    :func:`compute_whale_positions` when the caller supplies no wallets. Read from
    the packaged ``hlmcp/data/curated_whales.json`` via :func:`importlib.resources`
    so it works both from source and from an installed wheel.

    Mechanism: read the packaged JSON resource, parse it, and return the
    ``address`` of every entry under ``wallets`` (raw form — the venue normalizes
    before sending).

    Returns:
        The curated wallet addresses, in file order.

    Raises:
        FileNotFoundError: If the packaged resource is missing (a packaging bug).
        KeyError / ValueError: If the JSON is malformed (missing ``wallets`` or an
            entry without ``address``).
    """
    raw: str = files("hlmcp.data").joinpath(_CURATED_WHALES_RESOURCE).read_text(encoding="utf-8")
    data = json.loads(raw)
    return [str(entry["address"]) for entry in data["wallets"]]


def _dedupe_preserving_order(wallets: Sequence[str]) -> list[str]:
    """Return the input wallets de-duplicated, preserving first-seen order.

    De-dup is on the raw string (a malformed address cannot be normalized), so
    two spellings of the same canonical address are not collapsed — acceptable
    for a small curated/opt-in list and avoids raising on a bad entry here.

    Args:
        wallets: The raw input wallet strings.

    Returns:
        The unique wallet strings in first-seen order.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for wallet in wallets:
        if wallet not in seen:
            seen.add(wallet)
            unique.append(wallet)
    return unique


def _build_report(
    wallet: str, dex: str, state: HLClearinghouseState, fetched_at_ms: int
) -> WhaleWalletReport | None:
    """Assemble one :class:`WhaleWalletReport`, or ``None`` if the wallet is flat.

    Mechanism: if ``state`` has no open positions, return ``None`` (a normal
    no-positions result — emitted as neither a report nor a failure). Otherwise
    summarize each position, aggregate directional exposure, derive account-level
    risk, and stamp the snapshot's own freshness (``fetched_at_ms - state.time``).

    Args:
        wallet: The (normalized) wallet address.
        dex: The dex this snapshot is from ("" native HL, else a HIP-3 name).
        state: The parsed ``clearinghouseState`` for this (wallet, dex).
        fetched_at_ms: Local assembly time for the freshness stamp, ms epoch.

    Returns:
        A populated :class:`WhaleWalletReport`, or ``None`` if there are no
        positions.
    """
    positions: list[HLPosition] = [ap.position for ap in state.assetPositions]
    if not positions:
        return None

    return WhaleWalletReport(
        wallet=wallet,
        dex=dex,
        account_risk=derive_account_risk(state),
        aggregate=aggregate_positions(positions),
        positions=[summarize_position(position) for position in positions],
        freshness=FreshnessMeta.from_times(server_time_ms=state.time, fetched_at_ms=fetched_at_ms),
    )


def _record_result(
    wallet: str,
    dex: str,
    result: HLClearinghouseState | BaseException,
    fetched_at_ms: int,
    reports: list[WhaleWalletReport],
    failures: list[WhalePositionFailure],
) -> None:
    """Route one fan-out result to ``reports`` or ``failures`` in place.

    Mechanism: an exception-as-value becomes a :class:`WhalePositionFailure`; a
    state with >=1 position becomes a :class:`WhaleWalletReport` (flat states are
    dropped). Mutates ``reports``/``failures`` rather than returning, so callers
    can accumulate across a fan-out.

    Args:
        wallet: The wallet the result belongs to.
        dex: The dex the result is for ("" native HL, else HIP-3 name).
        result: Either a parsed state or the exception captured fetching it.
        fetched_at_ms: Local assembly time for the freshness stamp, ms epoch.
        reports: Accumulator for successful, non-flat reports (mutated).
        failures: Accumulator for failures (mutated).
    """
    if isinstance(result, BaseException):
        failures.append(
            WhalePositionFailure(wallet=wallet, dex=dex, error=f"{type(result).__name__}: {result}")
        )
        return
    report: WhaleWalletReport | None = _build_report(wallet, dex, result, fetched_at_ms)
    if report is not None:
        reports.append(report)


async def compute_whale_positions(
    venue: HyperliquidPublic,
    wallets: Sequence[str],
    *,
    include_hip3: bool = False,
    now_ms: int | None = None,
) -> WhalePositionMonitorResponse:
    """Monitor positions and account-level risk across a set of whale wallets.

    Orchestrates venue -> analytics -> response: validate wallets client-side,
    fan out ``clearinghouseState`` (native HL only, or across every HIP-3 dex when
    ``include_hip3``), and assemble per-wallet reports headlined by account-level
    liquidation risk. Partial failures are surfaced, never fatal (Q5).

    Mechanism: de-dupe the input; reject malformed addresses up front (each a
    :class:`WhalePositionFailure`); for the valid set, either
    ``fetch_clearinghouse_states_batch`` (native) or one
    ``fetch_all_dexes_for_user`` per wallet, gathered concurrently (HIP-3);
    route each result via :func:`_record_result`; sort reports by gross notional
    descending; stamp a worst-case (oldest-snapshot) top-level
    :class:`FreshnessMeta`.

    Args:
        venue: The read-only Hyperliquid adapter.
        wallets: Wallet addresses to monitor (any common form; normalized before
            sending). Must be non-empty.
        include_hip3: If ``True``, fan out across native HL AND every HIP-3 dex
            (slower, complete). If ``False`` (default), native HL only.
        now_ms: Wall-clock (ms since epoch) to measure staleness against; defaults
            to the current time. Injectable so tests are deterministic.

    Returns:
        A :class:`WhalePositionMonitorResponse`: reports (sorted by gross notional
        desc), failures, the queried-wallet count, ``include_hip3``, and top-level
        freshness.

    Raises:
        ValueError: If ``wallets`` is empty.
        HLAPIError: Only if a shared prerequisite fails wholesale (e.g. the
            ``perpDexs`` discovery needed to validate/enumerate dexes); per-wallet
            API errors are captured as failures, not raised.
    """
    if not wallets:
        raise ValueError("wallets must contain at least one address")

    unique_wallets: list[str] = _dedupe_preserving_order(wallets)
    fetched_at_ms: int = now_ms if now_ms is not None else int(time.time() * 1000)

    reports: list[WhaleWalletReport] = []
    failures: list[WhalePositionFailure] = []

    # Validate client-side so one malformed address is a failure, not a raise that
    # would sink the whole batch (the venue's batch normalizer raises eagerly).
    valid_wallets: list[str] = []
    for wallet in unique_wallets:
        try:
            valid_wallets.append(normalize_wallet(wallet))
        except ValueError as exc:
            failures.append(
                WhalePositionFailure(
                    wallet=wallet, dex=NATIVE_HL_DEX, error=f"{type(exc).__name__}: {exc}"
                )
            )

    if include_hip3:
        await _fanout_hip3(venue, valid_wallets, fetched_at_ms, reports, failures)
    else:
        await _fanout_native(venue, valid_wallets, fetched_at_ms, reports, failures)

    # Biggest exposure first — the most decision-relevant ordering for a monitor.
    reports.sort(key=lambda report: report.aggregate.gross_notional_usd, reverse=True)

    return WhalePositionMonitorResponse(
        reports=reports,
        failures=failures,
        n_wallets_queried=len(unique_wallets),
        include_hip3=include_hip3,
        freshness=_top_level_freshness(reports, fetched_at_ms),
    )


async def _fanout_native(
    venue: HyperliquidPublic,
    valid_wallets: list[str],
    fetched_at_ms: int,
    reports: list[WhaleWalletReport],
    failures: list[WhalePositionFailure],
) -> None:
    """Fan out ``clearinghouseState`` across wallets on native HL only.

    Mechanism: one ``fetch_clearinghouse_states_batch`` (concurrent, exceptions as
    values) for all valid wallets on the native dex; route each result. No-op if
    there are no valid wallets.

    Args:
        venue: The read-only adapter.
        valid_wallets: Pre-validated (normalized) wallet addresses.
        fetched_at_ms: Local assembly time for freshness stamps, ms epoch.
        reports: Accumulator for reports (mutated).
        failures: Accumulator for failures (mutated).
    """
    if not valid_wallets:
        return
    batch: _PerDexStates = await venue.fetch_clearinghouse_states_batch(valid_wallets)
    for wallet, result in batch.items():
        _record_result(wallet, NATIVE_HL_DEX, result, fetched_at_ms, reports, failures)


async def _fanout_hip3(
    venue: HyperliquidPublic,
    valid_wallets: list[str],
    fetched_at_ms: int,
    reports: list[WhaleWalletReport],
    failures: list[WhalePositionFailure],
) -> None:
    """Fan out ``clearinghouseState`` across every dex for each wallet.

    Mechanism: run one ``fetch_all_dexes_for_user`` per wallet, gathered
    concurrently; each yields a per-dex ``{dex: state | Exception}`` map that is
    routed dex-by-dex. A wallet-level failure (e.g. the ``perpDexs`` discovery
    raising) is caught here and recorded as a single native-dex failure for that
    wallet rather than propagating.

    Args:
        venue: The read-only adapter.
        valid_wallets: Pre-validated (normalized) wallet addresses.
        fetched_at_ms: Local assembly time for freshness stamps, ms epoch.
        reports: Accumulator for reports (mutated).
        failures: Accumulator for failures (mutated).
    """
    if not valid_wallets:
        return

    async def one(wallet: str) -> tuple[str, _PerDexStates | Exception]:
        """Fetch all dexes for one wallet, capturing a wallet-level raise as a value."""
        try:
            return wallet, await venue.fetch_all_dexes_for_user(wallet)
        except Exception as exc:  # perpDexs failure / unexpected wallet-level error
            return wallet, exc

    for wallet, per_dex in await asyncio.gather(*(one(w) for w in valid_wallets)):
        if isinstance(per_dex, Exception):
            failures.append(
                WhalePositionFailure(
                    wallet=wallet, dex=NATIVE_HL_DEX, error=f"{type(per_dex).__name__}: {per_dex}"
                )
            )
            continue
        for dex, result in per_dex.items():
            _record_result(wallet, dex, result, fetched_at_ms, reports, failures)


def _top_level_freshness(reports: Sequence[WhaleWalletReport], fetched_at_ms: int) -> FreshnessMeta:
    """Derive the worst-case (oldest-snapshot) freshness across all reports.

    Mechanism: use the smallest ``server_time_ms`` among reports (the oldest
    snapshot, hence the largest staleness) against ``fetched_at_ms``; with no
    reports, fall back to ``fetched_at_ms`` for both (staleness 0).

    Args:
        reports: The assembled per-wallet reports.
        fetched_at_ms: Local assembly time, ms epoch.

    Returns:
        A :class:`FreshnessMeta` representing the worst-case snapshot age.
    """
    if not reports:
        return FreshnessMeta.from_times(server_time_ms=fetched_at_ms, fetched_at_ms=fetched_at_ms)
    oldest_server_ms: int = min(report.freshness.server_time_ms for report in reports)
    return FreshnessMeta.from_times(server_time_ms=oldest_server_ms, fetched_at_ms=fetched_at_ms)
