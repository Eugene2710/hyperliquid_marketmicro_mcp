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
