"""User-facing tool-response schemas.

Distinct from the raw ``HL*`` shapes in :mod:`hlmcp.schemas.hl_api`: those mirror
the wire format (decimal *strings*, exact API field order, nothing computed);
the models here are what the tools RETURN to the calling LLM. This is the FIRST
layer where HL's decimal strings become materialized ``float``s (via the
:mod:`hlmcp.analytics.utils` parse helpers) and where derived metrics plus
freshness metadata are attached.

Every tool response carries a :class:`FreshnessMeta` so the calling LLM can
reason about data age. HL REST snapshots are ~500ms stale at the median before
network latency (``docs/api_spike_findings.md`` Q3) — research/slow-loop grade,
not HFT. Surfacing the age lets the model decide whether the snapshot is fresh
enough for what it is doing rather than assuming a real-time feed.

Pydantic v2 (``model_validate``/``model_dump``); no I/O here.
"""

from pydantic import BaseModel, Field

from hlmcp.analytics.imbalance import ImbalanceBand
from hlmcp.analytics.positions import AccountRisk, PositionAggregate, PositionSummary


class FreshnessMeta(BaseModel):
    """Data-age metadata attached to every tool response.

    Lets the calling LLM reason about how stale the underlying snapshot is. All
    three fields are integer milliseconds; ``staleness_ms`` is the derived
    headline number (how old the data was when we assembled the response).

    A negative ``staleness_ms`` is possible if the local clock trails HL's
    server clock; it is not clamped, so clock skew stays visible rather than
    being hidden as a spurious zero.

    Attributes:
        server_time_ms: HL server timestamp on the source snapshot (the ``time``
            field of the underlying response), milliseconds since epoch.
        fetched_at_ms: Local wall-clock time when this response was assembled,
            milliseconds since epoch.
        staleness_ms: ``fetched_at_ms - server_time_ms`` — how old the snapshot
            was when returned. Does NOT include HL's own ~500ms internal snapshot
            lag (that is upstream of ``server_time_ms``); see Q3.
    """

    server_time_ms: int = Field(
        description="HL server timestamp on the source snapshot, ms since epoch."
    )
    fetched_at_ms: int = Field(
        description="Local wall-clock when this response was assembled, ms since epoch."
    )
    staleness_ms: int = Field(
        description="fetched_at_ms - server_time_ms; snapshot age at return time, ms."
    )

    @classmethod
    def from_times(cls, *, server_time_ms: int, fetched_at_ms: int) -> "FreshnessMeta":
        """Build a :class:`FreshnessMeta`, deriving ``staleness_ms`` from the two times.

        Mechanism: takes the snapshot's server timestamp and the local assembly
        time, returns an instance with ``staleness_ms = fetched_at_ms -
        server_time_ms`` (may be negative under clock skew, deliberately unclamped).

        Args:
            server_time_ms: HL server timestamp on the source snapshot, ms epoch.
            fetched_at_ms: Local wall-clock at response assembly, ms epoch.

        Returns:
            A populated :class:`FreshnessMeta`.
        """
        return cls(
            server_time_ms=server_time_ms,
            fetched_at_ms=fetched_at_ms,
            staleness_ms=fetched_at_ms - server_time_ms,
        )


class OrderBookImbalanceResponse(BaseModel):
    """Result of the ``order_book_imbalance`` tool for one symbol.

    Carries the per-band depth-weighted imbalance (:class:`ImbalanceBand`), the
    mid price every band is measured from, and — crucially — the *actual* bucket
    width the API delivered, so the caller can judge the resolution of the
    aggregation rather than trusting the requested setting. Because ``l2Book``
    caps at 20 levels/side and offers no setting between ``nSigFigs=4`` (~30 bps
    range) and ``nSigFigs=3`` (~296 bps range), a band in that gap gets coarser
    buckets than ideal; ``bucket_width_bps`` makes that visible (Q1).

    Attributes:
        coin: Symbol this book is for (echoes the request).
        mid_price: Mid price ``(best_bid + best_ask) / 2``, the band reference.
        nsigfigs: ``nSigFigs`` used for the aggregated request (``None`` = full
            precision), as chosen by ``choose_aggregation`` from the deepest band.
        mantissa: ``mantissa`` used (bucket-width multiplier, only ever 2 or 5;
            ``None`` when not applicable).
        bucket_width_usd: ACTUAL per-bucket width in USD, measured from the
            response (difference of the top two prices on a side), not estimated.
            ``None`` if a side had fewer than two levels to difference.
        bucket_width_bps: ``bucket_width_usd`` expressed in basis points of mid;
            the honest resolution of the returned book. ``None`` when
            ``bucket_width_usd`` is ``None``.
        n_bid_levels: Number of bid levels the response contained (<= 20).
        n_ask_levels: Number of ask levels the response contained (<= 20).
        bands: One :class:`ImbalanceBand` per requested band, in request order.
        freshness: Data-age metadata (see :class:`FreshnessMeta`).
    """

    coin: str = Field(description="Symbol this book is for.")
    mid_price: float = Field(description="Mid price (best_bid + best_ask) / 2.")
    nsigfigs: int | None = Field(
        default=None,
        description="nSigFigs used for the request; None = full precision.",
    )
    mantissa: int | None = Field(
        default=None,
        description="mantissa (bucket-width multiplier) used; None when not applicable.",
    )
    bucket_width_usd: float | None = Field(
        default=None,
        description=(
            "Actual per-bucket width in USD, measured from the response (top two "
            "prices differenced), not estimated. None if a side had < 2 levels."
        ),
    )
    bucket_width_bps: float | None = Field(
        default=None,
        description="bucket_width_usd in basis points of mid; the book's true resolution.",
    )
    n_bid_levels: int = Field(ge=0, description="Number of bid levels returned (<= 20).")
    n_ask_levels: int = Field(ge=0, description="Number of ask levels returned (<= 20).")
    bands: list[ImbalanceBand] = Field(
        description="Per-band depth-weighted imbalance, in request order."
    )
    freshness: FreshnessMeta = Field(description="Data-age metadata for this response.")


class WhaleWalletReport(BaseModel):
    """One wallet's positions and account-level risk on one dex.

    The unit of the ``whale_position_monitor`` result: a single (wallet, dex)
    snapshot that HAD at least one open position. ``account_risk`` is the headline
    (see :class:`~hlmcp.analytics.positions.AccountRisk`) — for cross-margin
    accounts the meaningful liquidation trigger is account-level, NOT the
    per-position ``liquidation_px`` carried on each :class:`PositionSummary`
    (api_spike_findings.md Q2).

    Attributes:
        wallet: The (normalized) wallet address this report is for.
        dex: The dex this snapshot is from — ``""`` for native HL perps, else a
            HIP-3 deployment name (e.g. ``"xyz"``).
        account_risk: Account-level liquidation risk — the headline metric.
        aggregate: Directional exposure aggregate across this wallet's positions.
        positions: Per-position summaries, in the API's ``assetPositions`` order.
        freshness: Data-age metadata for THIS wallet's snapshot (each wallet has
            its own server timestamp).
    """

    wallet: str = Field(description="Normalized wallet address this report is for.")
    dex: str = Field(description="Dex name; '' for native HL, else a HIP-3 deployment.")
    account_risk: AccountRisk = Field(description="Account-level liquidation risk (headline).")
    aggregate: PositionAggregate = Field(description="Directional exposure aggregate.")
    positions: list[PositionSummary] = Field(
        description="Per-position summaries, in assetPositions order."
    )
    freshness: FreshnessMeta = Field(description="Data-age metadata for this wallet's snapshot.")


class WhalePositionFailure(BaseModel):
    """A per-wallet (or per-dex) failure surfaced instead of aborting the whole call.

    The fan-out returns errors as values (api_spike_findings.md Q5 / venue
    ``fetch_*_batch``), so one bad wallet or a single dex's API error becomes an
    entry here rather than failing every other wallet. Covers malformed addresses
    (rejected client-side), API errors, and timeouts.

    Attributes:
        wallet: The wallet the failure is associated with (raw input form for a
            rejected address; normalized otherwise).
        dex: The dex the failure occurred on — ``""`` for native HL or when the
            failure is not dex-specific (e.g. a malformed address).
        error: Human-readable error description (exception type + message).
    """

    wallet: str = Field(description="Wallet the failure is associated with.")
    dex: str = Field(description="Dex the failure occurred on; '' if native HL / not dex-specific.")
    error: str = Field(description="Human-readable error (exception type + message).")


class WhalePositionMonitorResponse(BaseModel):
    """Result of the ``whale_position_monitor`` tool across a set of wallets.

    Reports are emitted only for (wallet, dex) snapshots that HAD at least one
    open position, sorted by gross notional descending (biggest exposure first).
    A queried wallet that is absent from both ``reports`` and ``failures`` simply
    had no open positions (a normal result, not an error). Partial failures live
    in ``failures`` so one bad wallet never sinks the whole call.

    Attributes:
        reports: Per-(wallet, dex) reports with positions, sorted by gross
            notional descending.
        failures: Per-wallet/-dex failures (malformed address, API error, timeout).
        n_wallets_queried: How many wallets were queried (after de-duplication of
            the input).
        include_hip3: Whether the query fanned out across HIP-3 dexes (else native
            HL only).
        freshness: Worst-case (oldest-snapshot) data-age across all reports; if
            there are no reports it reflects the fetch time (staleness 0).
    """

    reports: list[WhaleWalletReport] = Field(
        description="Per-(wallet, dex) reports with positions, sorted by gross notional desc."
    )
    failures: list[WhalePositionFailure] = Field(
        description="Per-wallet/-dex failures; partial failure does not sink the call."
    )
    n_wallets_queried: int = Field(ge=0, description="Number of wallets queried (de-duplicated).")
    include_hip3: bool = Field(description="Whether HIP-3 dexes were included in the fan-out.")
    freshness: FreshnessMeta = Field(
        description="Worst-case (oldest-snapshot) data age across reports."
    )


class DexInfo(BaseModel):
    """Metadata for one HIP-3 deployment from ``perpDexs`` discovery.

    Surfaces the load-bearing routing key (:attr:`name`, used as the ``dex`` field
    for ``clearinghouseState``/``whale_position_monitor``) alongside human-facing
    metadata and the deployment's asset universe.

    Attributes:
        name: Dex routing key (e.g. ``"xyz"``); pass as ``dex`` to other tools.
        full_name: Human-readable name (e.g. ``"Felix Exchange"``), or ``None``.
        deployer: Deployer wallet address, or ``None``.
        oracle_updater: Address authorized to push oracle updates, or ``None``.
        fee_recipient: Address receiving the deployment's fees, or ``None``.
        n_assets: Number of markets on this dex (from ``assetToStreamingOiCap``).
        assets: The dex's market symbols (e.g. ``["xyz:AAPL", ...]``), in API order.
    """

    name: str = Field(description="Dex routing key, e.g. 'xyz'; pass as the `dex` field.")
    full_name: str | None = Field(default=None, description="Human-readable dex name.")
    deployer: str | None = Field(default=None, description="Deployer wallet address.")
    oracle_updater: str | None = Field(default=None, description="Oracle-updater address.")
    fee_recipient: str | None = Field(default=None, description="Fee-recipient address.")
    n_assets: int = Field(ge=0, description="Number of markets on this dex.")
    assets: list[str] = Field(description="The dex's market symbols, in API order.")


class ListHip3DexesResponse(BaseModel):
    """Result of the ``list_hip3_dexes`` tool: the HIP-3 deployment catalog.

    Native HL (the null-first / empty-string dex) is intentionally excluded — this
    lists only the named HIP-3 deployments a caller can route to.

    Freshness note: the ``perpDexs`` response carries NO server timestamp, so
    :attr:`freshness` reflects local fetch time only (``staleness_ms == 0`` by
    construction). The list is cached ~5 min and changes only when a new deployer
    comes online (api_spike_findings.md Q2c), so freshness is not a meaningful
    signal here; it is present for response-shape uniformity.

    Attributes:
        dexes: Metadata for each named HIP-3 deployment, in API order.
        n_dexes: Number of HIP-3 deployments returned.
        freshness: Local fetch-time metadata (see the note above).
    """

    dexes: list[DexInfo] = Field(description="Named HIP-3 deployments, in API order.")
    n_dexes: int = Field(ge=0, description="Number of HIP-3 deployments returned.")
    freshness: FreshnessMeta = Field(
        description="Local fetch-time metadata (perpDexs has no server timestamp)."
    )
