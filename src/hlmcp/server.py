"""FastMCP entry point and tool registration for the hlmcp server.

Reading this file should reveal the entire tool surface at a glance (CLAUDE.md):
each MCP tool is a thin wrapper here that hands off to its implementation under
:mod:`hlmcp.tools`. The wrappers own only the MCP-facing signature/docstring the
LLM sees; the real orchestration (venue -> analytics -> response) lives in the
tool modules and is unit-tested there against a mocked venue.

A single read-only :class:`~hlmcp.venues.hyperliquid.HyperliquidPublic` is opened
for the whole server lifetime via the FastMCP ``lifespan`` and shared across
tool calls (so its ``perpDexs`` cache and connection pool are reused). The venue
is read-only: no order placement, no signing, no exchange endpoint.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from hlmcp.config import load_config
from hlmcp.schemas.responses import OrderBookImbalanceResponse
from hlmcp.tools.order_book_imbalance import DEFAULT_BANDS_BPS, compute_order_book_imbalance
from hlmcp.venues.hyperliquid import HyperliquidPublic

_SERVER_INSTRUCTIONS: str = (
    "Read-only market-microstructure and execution analytics on Hyperliquid "
    "(native perps and HIP-3 deployments). Tools return COMPUTED signals, not raw "
    "data. Every response carries freshness metadata: REST snapshots are ~500ms "
    "stale before network latency, so this is research/slow-loop grade, not HFT."
)


class _VenueHolder:
    """Mutable slot holding the shared venue for the server's lifetime.

    Populated by :func:`_lifespan` on startup and cleared on shutdown. A tiny
    holder (rather than a module ``global``) keeps the assignment explicit and
    type-checked. ``None`` means the lifespan has not started.

    Attributes:
        venue: The shared read-only adapter, or ``None`` before startup.
    """

    venue: HyperliquidPublic | None = None


_holder: _VenueHolder = _VenueHolder()


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    """Open one shared read-only venue for the server's lifetime.

    Mechanism: build an :class:`HyperliquidPublic` from the environment-derived
    config, publish it on the module holder for tool wrappers to read, and close
    it (via the adapter's async context manager) on shutdown.

    Args:
        server: The FastMCP instance (unused; required by the lifespan protocol).

    Yields:
        ``None`` — tools read the venue from the module holder, not the yield.
    """
    async with HyperliquidPublic(load_config()) as venue:
        _holder.venue = venue
        try:
            yield
        finally:
            _holder.venue = None


mcp: FastMCP = FastMCP(name="hlmcp", instructions=_SERVER_INSTRUCTIONS, lifespan=_lifespan)


def _require_venue() -> HyperliquidPublic:
    """Return the shared venue, or raise if the lifespan has not started.

    Returns:
        The live :class:`HyperliquidPublic` shared across tool calls.

    Raises:
        RuntimeError: If called before startup / after shutdown (no venue set).
    """
    if _holder.venue is None:
        raise RuntimeError("venue is not initialised; the server lifespan has not started")
    return _holder.venue


@mcp.tool
async def order_book_imbalance(
    coin: str,
    bands_bps: list[float] | None = None,
) -> OrderBookImbalanceResponse:
    """Depth-weighted order-book imbalance for a Hyperliquid symbol.

    Fetches the aggregated L2 book for ``coin`` and, for each requested
    basis-point band around the mid price, reports the bid/ask size + notional
    and the notional-weighted imbalance ratio (``+1`` = all bid-side / buying
    pressure, ``-1`` = all ask-side, ``0`` = balanced). Aggregation is sized to
    the coin's actual price, so low-priced coins (e.g. XRP ~$1) get usable
    resolution, not one giant bucket. The response also reports the ACTUAL bucket
    width achieved and freshness metadata.

    Data age: HL REST snapshots are ~500ms stale before network latency — suitable
    for analysis and slow-loop decisioning (10s+ windows), NOT HFT. Check
    ``freshness.staleness_ms`` and ``bucket_width_bps`` (if the bucket is wider
    than your tightest band, that band's imbalance is unreliable).

    Args:
        coin: Symbol, e.g. ``"BTC"``, ``"ETH"``, ``"xyz:MSTR"`` (a HIP-3 market),
            or ``"@150"`` (a spot index).
        bands_bps: Basis-point band half-widths from mid to evaluate, each > 0.
            Defaults to ``[10, 25, 50, 100]`` when omitted.

    Returns:
        An :class:`OrderBookImbalanceResponse`: per-band imbalance, mid price,
        achieved bucket width, level counts, and freshness.
    """
    bands: Sequence[float] = bands_bps if bands_bps else DEFAULT_BANDS_BPS
    return await compute_order_book_imbalance(_require_venue(), coin, bands)


def main() -> None:
    """Console entry point: run the FastMCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
