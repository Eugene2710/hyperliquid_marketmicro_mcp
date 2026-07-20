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

Requires only **Python ≥ 3.12** — `venv` and `pip` ship with it, so nothing extra
to install. The Hyperliquid public API needs **no auth / no API key**.

> **Not yet published to PyPI.** Until it is, install **from source** (below). The
> PyPI command is how end users will install *once published* — pip fetches the
> built package directly, no clone needed.

### From source (works today) — pip, no extra tools

```bash
git clone git@github.com:Eugene2710/crypto_hl_marketmicro.git
cd crypto_hl_marketmicro
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install .                    # installs the package + its deps into .venv
which hlmcp-server               # -> /…/crypto_hl_marketmicro/.venv/bin/hlmcp-server
```

### From PyPI (once published) — pip

```bash
pip install hyperliquid-microstructure-mcp
```

Either path gives you the console command **`hlmcp-server`**, which runs the MCP
server over stdio. The distribution is `hyperliquid-microstructure-mcp`; the
**import** name is the short `hlmcp`.

```bash
hlmcp-server        # starts the stdio MCP server (an MCP client talks to it)
```

> **Have [`uv`](https://docs.astral.sh/uv/)?** It's a faster all-in-one
> alternative to the venv+pip dance, and it's what this repo uses for development.
> From the cloned repo: `uv sync` then `uv run hlmcp-server`. Once on PyPI:
> `uv add hyperliquid-microstructure-mcp`. Not required — pick pip *or* uv.

## Use it in Claude Desktop

Edit `claude_desktop_config.json` (**Settings → Developer → Edit Config**), add the
`hlmcp` entry below, then **fully quit and reopen** Claude Desktop (⌘Q — closing the
window isn't enough) so it reloads the config.

Point `command` at the **absolute path** of the `hlmcp-server` binary from your
install above (Claude Desktop launches with a minimal PATH, so a bare name may not
resolve — use the full path `which hlmcp-server` printed):

```json
{
  "mcpServers": {
    "hlmcp": {
      "command": "/absolute/path/to/crypto_hl_marketmicro/.venv/bin/hlmcp-server"
    }
  }
}
```

If you installed with `uv` instead, run it through `uv` (this also re-syncs the
project on launch, self-healing a stale venv):

```json
{
  "mcpServers": {
    "hlmcp": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--directory", "/absolute/path/to/crypto_hl_marketmicro", "hlmcp-server"]
    }
  }
}
```

Then ask, e.g., *"What's the order-book imbalance on BTC?"* or *"Show me whale
positioning on Hyperliquid."*

If the server won't start, check Claude Desktop's log
(`~/Library/Logs/Claude/mcp-server-hlmcp.log` on macOS). A `not found in the package
registry` error means the config is using a PyPI/`uvx` form before the package is
published — switch to an absolute-path form above.

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

## Using it well

You don't call these tools directly — you ask Claude (or any MCP client) in plain
language, and it picks the tool and reasons over the structured result. The value
compounds when you **chain the three tools into one decision** and let the freshness
and bucket-width metadata qualify every read.

Mental model:

- **`whale_position_monitor`** → positioning *bias* and *liquidation-cascade risk*.
- **`order_book_imbalance`** → near-term *buy/sell pressure* and *execution timing*.
- **`list_hip3_dexes`** → *widen the lens* to non-native markets, then feed the `dex`
  back into the other two.

Example prompts:

- *"What's the order-book imbalance on BTC at 10, 25, and 50 bps of mid — and is the
  bucket width tight enough to trust the 10 bps reading?"*
- *"Show the curated whales' net bias, and flag anyone whose account buffer is thin
  relative to gross notional."*
- *"List the HIP-3 deployments, then monitor the curated whales across all of them."*
  (sets `include_hip3: true`)

A worked workflow — *"Should I add to a BTC long right now?"*

1. **Context** — *"Are the curated whales net long or short, and is anyone near
   liquidation that could trigger a cascade?"* → `whale_position_monitor`.
2. **Pressure** — *"Now check BTC book imbalance at 10/25/50 bps — is there real bid
   support near touch?"* → `order_book_imbalance`.
3. **Synthesize** — *"Given whales are net long but two are near liquidation, and
   top-of-book is bid-heavy but the 10 bps bucket is coarse, how would you frame the
   risk of adding here?"*
4. **Re-check** — *"Re-run the imbalance and tell me the staleness."* Snapshots are
   ~500ms+ stale, so refresh right before deciding and read `staleness_ms`.

Tips to get the most out of it:

- **Make Claude read the metadata.** An imbalance number only means something if
  `bucket_width_bps` ≤ your tightest band and `staleness_ms` is low.
- **Match bands to intent** — tight (5–25 bps) for execution timing, wide (50–100 bps)
  for sentiment. Ask for several at once.
- **Anchor risk on the account buffer**, not per-position `liquidation_px` (often
  meaningless for cross-margin).
- **Bring your own wallets** — the curated set is a small starter list.
- **`include_hip3: true` only when you need completeness** — it fans out across all
  deployments and is slower.

### What *not* to use it for

- **Sub-second execution or HFT.** Data is ~500ms+ stale REST, research/slow-loop
  grade — see [Data & limits](#data--limits). Re-query and check `staleness_ms`
  rather than trusting one snapshot as a live tick.
- **A single-snapshot source of truth.** It's a research aid; corroborate before
  acting on it.
- **Order placement or anything that signs/trades.** Read-only by design — see
  [What this *isn't*](#what-this-isnt).

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
