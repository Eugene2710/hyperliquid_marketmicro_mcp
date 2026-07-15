# hlmcp — Hyperliquid Microstructure & Execution MCP

An open-source [MCP](https://modelcontextprotocol.io) server exposing **computed
market-microstructure and execution-analytics signals** on
[Hyperliquid](https://hyperliquid.xyz) — native HL perps *and* HIP-3
deployments — as tools that LLM agents and AI coding assistants (Claude Desktop,
Cursor, Claude Code, LangGraph, …) can call.

## Why this exists

Most crypto MCPs are **data-access wrappers**: mid prices, candles, raw L2 books.
`hlmcp` is deliberately *not* that. The value is in the **computed layer** — the
analysis you'd otherwise write yourself:

- **depth-weighted order-book imbalance** across basis-point bands, reporting the
  *actual achieved bucket width* so you know the resolution is real;
- **whale position monitoring** with **account-level** liquidation risk as the
  headline (not the often-meaningless per-position `liquidationPx` — see
  [Data & limits](#data--limits));
- **HIP-3 deployment discovery**, so cross-dex queries route correctly.

Read-only, HL-first, research- and slow-loop-decisioning grade.

## Install

Requires **Python ≥ 3.12**. The Hyperliquid public API needs **no auth / no API
key** — nothing to configure to get started.

```bash
uv add hyperliquid-microstructure-mcp     # with uv (recommended)
pip install hyperliquid-microstructure-mcp # or with pip
```

Either way you get the console command **`hlmcp-server`**, which runs the MCP
server over stdio. The distribution is `hyperliquid-microstructure-mcp`; the
**import** name is the short `hlmcp`.

```bash
hlmcp-server        # starts the stdio MCP server (an MCP client talks to it)
```

## Use it in Claude Desktop

Add the server to `claude_desktop_config.json` (**Settings → Developer → Edit
Config**), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "hlmcp": {
      "command": "hlmcp-server"
    }
  }
}
```

If `hlmcp-server` isn't on the PATH Claude Desktop sees, use an absolute path (the
`hlmcp-server` in your venv's `bin/`) or invoke via `uvx`:

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

Then ask, e.g., *"What's the order-book imbalance on BTC?"* or *"Show me whale
positioning on Hyperliquid."*

## Tools

Three read-only tools. Every response carries a `freshness` object
(`server_time_ms`, `fetched_at_ms`, `staleness_ms`) so the model can reason about
data age.

### `order_book_imbalance`

Depth-weighted order-book imbalance for a Hyperliquid symbol.

- **In:** `coin` (`"BTC"`, `"ETH"`, `"xyz:MSTR"` for a HIP-3 market, `"@150"` for a
  spot index); `bands_bps` — optional bp band half-widths from mid (default
  `[10, 25, 50, 100]`).
- **Out:** per-band bid/ask size + notional and the notional-weighted imbalance
  ratio (`+1` all bid-side / buying pressure, `-1` all ask-side, `0` balanced);
  `mid_price`; the **actual `bucket_width_bps` achieved**; level counts; freshness.
- Aggregation is sized to the coin's real price, so low-priced coins (XRP ~$1) get
  usable resolution. If `bucket_width_bps` exceeds your tightest band, that band's
  imbalance is unreliable — the tool surfaces this rather than hiding it.

### `whale_position_monitor`

Positions and **account-level liquidation risk** for large Hyperliquid traders.

- **In:** `wallets` — optional addresses in any common form; when omitted, a small
  **curated set** of known large traders is used (documented provenance, ships in
  the package — not an endorsement). `include_hip3` — if `true`, also query every
  HIP-3 deployment (slower, complete cross-dex view); default `false`.
- **Out:** per-wallet reports sorted by gross notional, each headlined by the
  **account-level buffer** (`accountValue − crossMaintenanceMarginUsed`, the
  meaningful cross-margin trigger), plus a directional-exposure aggregate
  (long/short split, net bias) and the individual positions. Per-wallet API errors
  land in `failures` instead of failing the whole call.
- Per-position `liquidation_px` is included but is often meaningless for
  cross-margin accounts — treat it as supplementary only.

### `list_hip3_dexes`

List the HIP-3 perp deployments on Hyperliquid.

- **In:** nothing.
- **Out:** one entry per named HIP-3 deployment — routing key `name` (usable as the
  `dex` argument elsewhere), human metadata (full name, deployer, oracle updater,
  fee recipient), and market universe (`n_assets`, `assets`). Native HL is excluded;
  this catalogs only the HIP-3 deployments you can route to.
- `perpDexs` carries no server timestamp, so `freshness` reflects local fetch time
  only.

## Data & limits

**Research / slow-loop grade, not HFT.** Read the freshness metadata and size your
loop accordingly:

- **REST snapshots are ~500ms stale (median)** before network latency — HL serves
  REST from a snapshot cache refreshing ~twice/sec. Measured (clock-synced):
  staleness p50 491ms / p90 670ms / p99 737ms; end-to-end data age p50 585ms /
  p99 988ms. Good for analysis and slow-loop decisioning (10s+ windows); **not**
  for sub-second loops or HFT. A WebSocket adapter for sub-second freshness is a
  roadmap item, not v0.
- **Rate limiting is built in:** the venue throttles to a sustained 7 req/sec, burst
  15, capped at 20 concurrent in-flight — well under HL's documented ceiling.
  Tunable via env vars (below); the defaults are safe.
- **Account-level risk, not per-position `liquidationPx`.** For cross-margin
  positions HL's per-position liquidation price is derived from a whole-account
  snapshot and is often meaningless (e.g. a $172 liquidation price on a $2 short),
  which is why the whale tool headlines the account-level buffer instead.

### Optional configuration

Nothing is required. All knobs are optional env vars (also loadable from `.env` —
see `.env.example`), falling back to spike-derived defaults:

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

- **Read-only.** No order placement, no signing, no exchange endpoint — that's a
  separate adapter with its own auth surface and threat model, out of scope here.
- **Not a real-time feed.** ~500ms REST staleness; no WebSocket in v0.
- **Not HFT infrastructure.** Research and slow-loop decisioning grade.
- **Not a raw-data wrapper.** Other MCPs already serve raw mids and candles; this
  ships *computed* signals.

## Project layout

```
src/hlmcp/
  config.py                    # env-driven HLConfig + load_config (rate/concurrency/timeout knobs)
  server.py                    # FastMCP app: 3 @mcp.tool wrappers + shared-venue lifespan; main() = hlmcp-server
  schemas/
    hl_api.py                  # raw HL API shapes (HL*-prefixed; strings stay strings, nothing computed)
    responses.py               # user-facing response models (FreshnessMeta, *Response)
  venues/
    hyperliquid.py             # read-only REST adapter: token bucket + semaphore + retry, HIP-3 discovery/cache
    errors.py                  # HLAPIError
  analytics/                   # PURE functions — no I/O, no async
    aggregation.py             # choose_aggregation: price-aware l2Book bucket sizing
    imbalance.py               # compute_imbalance across bp bands
    positions.py               # position aggregation + account-level liquidation buffer
    utils.py                   # normalize_wallet, decimal/parse helpers
  tools/                       # one MCP tool per file (thin: venue -> analytics -> response)
    order_book_imbalance.py    #   depth-weighted book imbalance
    whale_position_monitor.py  #   whale positions + account risk (loads the curated set)
    list_hip3_dexes.py         #   HIP-3 deployment catalog
  data/
    curated_whales.json        # default whale set (provenance documented); shipped inside the wheel
evals/                         # eval skeleton: datasets/*.jsonl + run_evals.py (validates format; graders post-v0)
tests/                         # unit/ (pure, mocked) · venue/ (respx-mocked HTTP) · integration/ (live HL) · fixtures/ (recorded responses)
docs/                          # architecture.md · api_spike_findings.md · build_plan.md
data_exploration/              # pre-build API spike scripts (reference only, not imported)
reference/                     # reviewed draft code adapted during the build (not packaged)
```

Strict layering, dependencies flow one way: `schemas/` → `venues/` → `analytics/`
→ `tools/` → `server.py`. See [`docs/architecture.md`](docs/architecture.md) for
the design and [`docs/api_spike_findings.md`](docs/api_spike_findings.md) for the
empirical API constraints.

## Development

```bash
uv sync                                # install with dev deps
uv run ruff check .                    # lint (must be clean)
uv run mypy --strict src/              # type-check (must be clean)
uv run pytest -m "not integration"     # unit + mocked venue tests
uv run pytest -m integration           # opt-in: hits the live HL API
uv run python evals/run_evals.py       # validate eval datasets
```

## License

Not yet specified. A license will be added before any public PyPI publish.
