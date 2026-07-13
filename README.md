# hlmcp — Hyperliquid Microstructure & Execution MCP

An open-source [MCP](https://modelcontextprotocol.io) server that exposes
**computed market-microstructure and execution-analytics signals** on
[Hyperliquid](https://hyperliquid.xyz) — native HL perps *and* HIP-3
deployments — as tools that LLM agents and AI coding assistants (Claude Desktop,
Cursor, Claude Code, LangGraph, …) can call.

## Why this exists

The crypto MCP ecosystem is mostly **data-access wrappers**: mid prices, candles,
raw L2 books. `hlmcp` is deliberately *not* that. The value here is in the
**computed layer** — the analysis you'd otherwise have to write yourself:

- **depth-weighted order-book imbalance** across basis-point bands, with the
  *actual achieved bucket width* reported so you know the resolution is real;
- **whale position monitoring** with **account-level** liquidation risk as the
  headline (not the often-meaningless per-position `liquidationPx` — see
  [Data & limits](#data--limits));
- **HIP-3 deployment discovery**, so cross-dex queries route correctly.

That analytical layer is where domain expertise compounds and where a tool earns
the right to be installed over the half-dozen generic crypto MCPs that already
exist.

## Install

Requires **Python ≥ 3.12**. The Hyperliquid public API needs **no auth / no API
key**, so there is nothing to configure to get started.

### With `uv` (recommended)

```bash
uv add hyperliquid-microstructure-mcp
# or, to run the server as a one-off tool without adding it to a project:
uvx --from hyperliquid-microstructure-mcp hlmcp-server
```

### With `pip`

```bash
pip install hyperliquid-microstructure-mcp
```

Both install the console entry point **`hlmcp-server`**, which runs the MCP
server over stdio. The **import** name is `hlmcp` (the distribution is named
`hyperliquid-microstructure-mcp`).

```bash
hlmcp-server        # starts the stdio MCP server (an MCP client speaks to it)
```

## Use it in Claude Desktop

Add the server to your `claude_desktop_config.json` (**Settings → Developer →
Edit Config**):

```json
{
  "mcpServers": {
    "hlmcp": {
      "command": "hlmcp-server"
    }
  }
}
```

If `hlmcp-server` isn't on the PATH Claude Desktop sees, use an absolute path
(e.g. the `hlmcp-server` inside your venv's `bin/`), or invoke via `uvx`:

```json
{
  "mcpServers": {
    "hlmcp": {
      "command": "uvx",
      "args": ["--from", "hyperliquid-microstructure-mcp", "hlmcp-server"]
    }
  }
}
```

Restart Claude Desktop, then ask it something like *"What's the order-book
imbalance on BTC?"* or *"Show me whale positioning on HYPE."*

## Tool catalog

Three read-only tools. Every response carries a `freshness` object
(`server_time_ms`, `fetched_at_ms`, `staleness_ms`) so the model can reason about
data age.

### `order_book_imbalance`

Depth-weighted order-book imbalance for a Hyperliquid symbol.

- **Input:** `coin` (e.g. `"BTC"`, `"ETH"`, `"xyz:MSTR"` for a HIP-3 market,
  `"@150"` for a spot index); `bands_bps` — optional list of basis-point band
  half-widths from mid (default `[10, 25, 50, 100]`).
- **Output:** per-band bid/ask size + notional and the notional-weighted
  imbalance ratio (`+1` = all bid-side / buying pressure, `-1` = all ask-side,
  `0` = balanced); the `mid_price`; the **actual `bucket_width_bps` achieved**;
  bid/ask level counts; and `freshness`.
- **Note:** aggregation is sized to the coin's real price, so low-priced coins
  (XRP ~$1) get usable resolution rather than one giant bucket. If
  `bucket_width_bps` is wider than your tightest band, that band's imbalance is
  unreliable — the tool surfaces this rather than hiding it.

### `whale_position_monitor`

Positions and **account-level liquidation risk** for large Hyperliquid traders.

- **Input:** `wallets` — optional list of addresses in any common form; when
  omitted, a small **curated set** of known large traders is used (documented
  provenance, ships in the package — not an endorsement, positions change
  constantly). `include_hip3` — if `true`, also query every HIP-3 deployment for a
  complete cross-dex view (slower); default `false` (native HL perps only).
- **Output:** per-wallet reports sorted by gross notional, each with the
  **account-level buffer** (`accountValue − crossMaintenanceMarginUsed`, the
  meaningful cross-margin trigger) as the headline, a directional-exposure
  aggregate (long/short split, net bias), and the individual positions.
  Per-wallet API errors land in `failures` rather than failing the whole call.
- **Note:** per-position `liquidation_px` is included but is often practically
  meaningless for cross-margin accounts (a snapshot of the whole account); treat
  it as supplementary diagnostic only.

### `list_hip3_dexes`

List the HIP-3 perp deployments on Hyperliquid.

- **Input:** none.
- **Output:** one entry per named HIP-3 deployment — routing key `name` (usable as
  the `dex` argument elsewhere), human metadata (full name, deployer, oracle
  updater, fee recipient), and its market universe (`n_assets`, `assets`). Native
  HL is intentionally excluded; this catalogs only the HIP-3 deployments you can
  route to. Use it to discover valid `dex` values before querying HIP-3 markets.
- **Note:** `perpDexs` carries no server timestamp, so `freshness` reflects local
  fetch time only.

## Data & limits

**This is research / slow-loop grade, not HFT.** Read the freshness metadata and
size your loop accordingly:

- **REST snapshots are ~500ms stale (median)** before network latency — HL serves
  REST from a snapshot cache refreshing ~twice/sec. Measured (clock-synced):
  staleness p50 491ms / p90 670ms / p99 737ms; end-to-end data age p50 585ms /
  p99 988ms. Suitable for analysis, research, and slow-loop decisioning (10s+
  windows). **Not** for sub-second loops or HFT. A WebSocket-backed adapter for
  sub-second freshness is a roadmap item, not v0.
- **Rate limiting is built in:** the venue adapter throttles to a sustained 7
  req/sec with a burst of 15, capped at 20 concurrent in-flight requests, well
  under HL's documented ceiling. These are tunable via env vars (see below) but
  the defaults are safe.
- **Account-level risk, not per-position `liquidationPx`.** For cross-margin
  positions HL's per-position liquidation price is derived from a whole-account
  snapshot and is often meaningless (e.g. a $172 liquidation price on a $2 short).
  `whale_position_monitor` surfaces the account-level buffer as the headline for
  this reason.

### Optional configuration

Nothing is required. All knobs are optional env vars (also loadable from a
`.env` file — see `.env.example`), falling back to spike-derived defaults:

| Env var | Default | Meaning |
|---|---|---|
| `HL_BASE_URL` | `https://api.hyperliquid.xyz/info` | HL info endpoint (read-only) |
| `HL_MAX_CONCURRENCY` | `20` | max in-flight requests |
| `HL_SUSTAINED_RATE_PER_SEC` | `7.0` | token-bucket refill rate (req/s) |
| `HL_BURST_CAPACITY` | `15.0` | token-bucket burst size |
| `HL_REQUEST_TIMEOUT_S` | `4.0` | per-request timeout (s) |
| `HL_DEX_CACHE_TTL_S` | `300.0` | HIP-3 discovery cache TTL (s) |
| `HL_SAMPLE_WALLETS` | *(unset)* | comma-separated addresses for integration tests |

## What this *isn't*

Scope is deliberately narrow for v0:

- **Read-only.** No order placement, no signing, no exchange endpoint. That is a
  separate adapter with its own auth surface and threat model, explicitly out of
  scope here.
- **Not a real-time feed.** ~500ms REST staleness; no WebSocket in v0.
- **Not HFT infrastructure.** Research and slow-loop decisioning grade.
- **Not a raw-data wrapper.** If you want raw mid prices and candles, other MCPs
  already do that. This ships *computed* signals.

## Development

```bash
uv sync                                   # install with dev deps
uv run ruff check .                       # lint (must be clean)
uv run mypy --strict src/                 # type-check (must be clean)
uv run pytest -m "not integration"        # unit + mocked venue tests
uv run pytest -m integration              # opt-in: hits the live HL API
```

Layering is strict: `schemas/` → `venues/` → `analytics/` (pure functions) →
`tools/` → `server.py`. See [`docs/architecture.md`](docs/architecture.md) for the
design, [`docs/api_spike_findings.md`](docs/api_spike_findings.md) for the
empirical API constraints, and [`evals/`](evals/) for the (skeleton) evaluation
harness.

## License

Not yet specified. A license will be added before any public PyPI publish.
