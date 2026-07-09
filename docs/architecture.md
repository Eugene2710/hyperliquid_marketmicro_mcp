# Architecture

## Purpose and positioning

`hlmcp` is an MCP server that exposes computed market-microstructure and
execution-analytics signals on Hyperliquid to LLM agents. The deliberate wedge:
the existing crypto MCP ecosystem is mostly *data-access* wrappers (mid prices,
candles, raw L2 books). The analytical layer — order-book imbalance, whale
positioning, liquidation risk, funding carry — is underserved. That layer is
where domain expertise compounds and where a tool earns the right to be
installed over the half-dozen generic crypto MCPs that already exist.

**Target users (in priority order):** solo quant developers building HL bots who
use Claude/Cursor in their dev loop; agentic-trading-bot builders; crypto-native
researchers publishing on HL; small prop shops with HL exposure; HL ecosystem
builders.

**Explicitly out of scope for v0:** order placement, signing, the exchange
endpoint, sub-100ms HFT use cases, cross-CEX-and-DEX universes. Read-only,
HL-first, analysis-and-slow-loop-decisioning grade.

## Layered design

Four layers, strict dependency direction (each depends only on layers above it
in this list):

1. **Schemas (`schemas/`)** — Pydantic data models. `hl_api.py` holds the raw
   `HL*` shapes that mirror the Hyperliquid API exactly (strings stay strings,
   nothing computed). `responses.py` holds the user-facing tool-response shapes
   (materialized floats, derived metrics, `FreshnessMeta`). Schemas have no
   dependencies on other layers.

2. **Venues (`venues/`)** — external API adapters. `hyperliquid.py` is the
   read-only REST adapter. It returns `HL*` types. It owns: HTTP lifecycle,
   concurrency capping (semaphore), rate limiting (token bucket), HIP-3 dex
   discovery + caching, address normalization, and dex validation. Depends on
   schemas only.

3. **Analytics (`analytics/`)** — PURE functions. No I/O, no async. Take schema
   types in, return schema types out. This purity is what makes the project
   testable: every analytics function can be exercised with recorded fixtures
   and has zero hidden dependencies. Depends on schemas only.

4. **Tools (`tools/`)** — MCP tool implementations, one per file. Thin
   orchestration: call the venue, pass results through analytics, wrap with
   `FreshnessMeta`, return a user-facing schema. The only code that combines
   I/O + computation + response assembly. Depends on venues, analytics, schemas.

`server.py` imports each tool and registers it with FastMCP. Reading `server.py`
should reveal the entire tool surface at a glance.

### Why this separation

The payoff is testability and change-isolation. `HL*` schemas change only when
HL's API changes. Tool-response schemas change every time we add or refactor a
tool. Analytics are deterministic and unit-testable offline. The venue is the
only place network behavior lives, so the only place that needs HTTP mocking and
integration tests. When something breaks, the layer tells you where to look: a
schema parse error is an API-shape change; an analytics test failure is a logic
bug; a venue test failure is an I/O or contract problem.

## Operating envelope

Each parameter below is tagged with its provenance so future work can tell a
load-bearing measurement from an engineering estimate. The tags:
**[measured]** = observed in the spike; **[derived]** = computed from a measurement
via an explicit rule; **[estimate]** = a reasoned starting guess not yet validated
against real usage (revisit these — also listed under "Open questions").

- **Concurrency cap: 20 in-flight requests** — **[measured]**. asyncio.Semaphore
  at the venue's `_post`. Q5 confirmed 20 concurrent `clearinghouseState` calls
  complete in ~150ms wall-clock with zero throttling. (Note: >20 was never tested;
  20 is sufficient for our use case, not a discovered ceiling.)

- **Sustained rate cap: 7 req/sec** — **[derived]**, high confidence. 70% of HL's
  documented ceiling (1200 weight/min ÷ 2 weight/request = 10 req/sec). Q4
  validated that 7 req/sec sustained for 30s produces zero errors and idle-level
  latency, so the derived value is also empirically confirmed safe.

- **Per-request timeout: 3–5s** — **[derived]**, medium-high confidence. Q3
  measured request-latency p99 at ~476–892ms across two small-sample runs; true
  p99 over many requests is uncertain and likely higher. Rule of thumb: timeout =
  3–5× observed p99 (≈2.7–4.5s) so legitimate slow-but-successful requests aren't
  killed and falsely counted as failures, while genuinely dead requests still fail
  fast enough to retry. The exact value within 3–5s is a judgment; the underlying
  p99 is from small samples.

- **Burst capacity: 15 tokens** — **[estimate]**, LOW confidence. The token
  bucket's max size, governing how many requests fire instantly before throttling
  to the 7/sec refill rate. Chosen so a single fan-out (e.g. `whale_position_monitor`
  across ~10–15 wallets) doesn't stall mid-batch, while staying safe against the
  per-minute budget (worst case: 15 instant + 7×60 sustained = 435 req = 870 weight,
  under the 1200 ceiling). But this is NOT derived from a measured workload — real
  tool-call burst shapes aren't known until the tools exist. Could reasonably be
  10 or 20. Ship as a `config.py` parameter and tune against observed usage. See
  "Open questions."

- **Data freshness: REST snapshots ~500ms stale (median), ~600ms end-to-end** —
  **[measured]**, high confidence. Q3 measured snapshot staleness at p50 491ms /
  p90 670ms / p99 737ms, AFTER verifying local clock sync to NTP (+20ms), so this
  is real staleness, not clock skew. The number is data; the *interpretation*
  (research/slow-loop grade, not HFT) is a judgment. Every tool response carries
  `FreshnessMeta.staleness_ms` so the LLM can reason about data age. A
  WebSocket-backed adapter for sub-second freshness is a roadmap item, NOT v0.

## Key architectural decisions

1. **MCP, not a library.** MCP servers install across Claude Desktop, Cursor,
   Claude Code, OpenAI Agents SDK, LangGraph — anything MCP-compliant. A library
   requires Python integration. The MCP is the distribution unlock. Internally we
   still build the analytics as importable pure functions, so library use is also
   possible; the MCP wraps them.

2. **Hyperliquid-first, not CEX-first.** HL exposes L4 individual-order data that
   no CEX provides; the public API needs no auth (zero-friction install); on-chain
   data is verifiable for eval fixtures; the HL-native dev community overlaps
   heavily with the agentic-AI-curious quant crowd. The `Venue` protocol
   (`venues/base.py`) is defined so other on-chain orderbook venues can be added
   if the landscape shifts — but HL is the only implementation at launch.

3. **Stateless tools, no background indexer (v0).** Q5 proved concurrent fan-out
   works, so `whale_position_monitor` can fetch on demand rather than maintaining
   a polled cache. A background indexer is a v0.3+ option if usage patterns demand
   it. Stateless servers are cheaper to operate and reason about.

4. **Client-side validation before every API call.** Address normalization and
   dex-name validation happen in the venue adapter BEFORE requests go out, because
   the API silently accepts malformed-but-parseable inputs (returning empty
   results that look valid) and returns un-diagnosable 500s for unknown dexes.
   See api_spike_findings.md Q2b and Q2c.

5. **Aggregation chosen from the requested band, not hardcoded.** `l2Book` caps
   at 20 levels/side; the right `nSigFigs`/`mantissa` depends on how deep a band
   the caller needs. `choose_aggregation` derives it. See Q1.

## Open questions / low-confidence decisions

These are decisions we made without full confidence. Flagged so future work
revisits them deliberately rather than treating them as settled.

- **Rate-limiter burst capacity (15 tokens) is an engineering estimate, not a
  measured value.** It governs how many requests fire instantly before throttling
  to the 7/sec refill. Picked to cover a typical fan-out without stalling while
  staying under the per-minute budget, but the real shape of LLM tool-call bursts
  is unknown until the tools exist. Ship it as a `config.py` parameter (default
  15) and tune against observed usage. Could reasonably be 10 or 20.
  **Confidence: low. Revisit once tools are live and burst patterns are visible.**

- **Retry attempt count and backoff are engineering estimates, not measured. 
  Step 4 added a tenacity retry at the venue's _post, retryable-only 
  (timeouts / transport errors / 5xx; never 4xx, which are deterministic). 
  _MAX_ATTEMPTS = 3 and the backoff (initial 0.25s, max 2.0s, exponential + jitter)
  are module constants chosen as a resilience margin — the spike saw zero 
  transient errors (Q4/Q5), so there is no measured failure distribution to tune 
  against. Kept as constants (not env-tunable) for v0 simplicity. 
  Confidence: low on the exact values; could reasonably be 2 or 5 attempts. 
  Revisit if real usage surfaces transient-error patterns.**

- **The aggregation ladder is BTC-calibrated; low-priced coins are now handled by 
  a price-aware probe. nSigFigs rounds by significant figures, so a setting's 
  bps-per-bucket scales as ~1/(leading digit of price) - the raw BTC ladder badly 
  over-coarsens cheap coins (XRP @ ~$1.12 got ~89 bps buckets for a 100 bps band,
  collapsing the band into a single estimate_bucket_bps - still covers the band; 
  the order_book_imbalance tool feeds it a mid read from a one-shot full-precision
  probe fetch reused when it already reaches the deepest band, else one extra fetch).
  price=None retains the BTC ladder for back-compat and for coins with no available 
  mid. Residual low-confidence points: (a) the mid's within-decade leading digit 
  still drifts as price moves, so a pick can be one setting off near the granularity 
  gaps - surfaced, not silently wrong; (b) it costs up to one extra fetch per call; 
  (c) verified empirically that HL  keeps a uniform bucket grid across a whole book 
  even crossing  a power-of-ten boundary (XRP $1.12→$0.93 at a constant $0.01 step),
  so the feared "unequal buckets at a decade boundary" does not occur. 
  Confidence: medium-high for BTC-magnitude and low-priced coins now;
  a per-coin runtime probe / allMids-sourced price remains the fuller fix. 
  Revisit for extreme magnitudes and HIP-3 symbols.**

- **Whether `mantissa` should be exposed to users at all.** It's an internal
  detail (a 2×/5× bucket-width multiplier) that, if set wrong, produces empty
  books. We currently treat it as internal-only, derived by `choose_aggregation`.
  **Confidence: medium. Likely correct to keep internal.**

- **Whether the 30→296 bps aggregation gap matters in practice.** The API has no
  setting between `nSigFigs=4` and `nSigFigs=3` 
  (mantissa is only valid at nSigFigs=5) so bands landing in that window get 
  coarser buckets than ideal. The gap lands where the coin's price puts it and 
  reports the measured achieved bucket width (measure_bucket_width_usd,via 
  min-of-consecutive-gaps — robust to omitted empty buckets), not just an 
  estimate, so callers see the true resolution and can judge. 
  Confidence: medium-high that surfacing the measured width is the honest 
  treatment; unknown how often users hit the gap.**

- **Whether ~500ms REST staleness is acceptable for the target users.** We
  believe yes for analysis and slow-loop decisioning, no for HFT, and we document
  it honestly. But we haven't validated this against real user needs.
  **Confidence: medium. The WS adapter is the hedge if it turns out to matter.**

- **HIP-3 fan-out default.** A complete whale view requires querying all ~9 dexes,
  but that's slower and most usage is native-HL-only. We plan `include_hip3=False`
  as the default with opt-in fan-out. **Confidence: medium on the default
  direction.**

- **Whether to ship a curated whale list.** `whale_position_monitor` is more
  useful with a default list of known large traders, but maintaining it is work
  and the addresses are someone else's curation. Leaning toward shipping a small
  list in a separate, community-PR-able JSON file with provenance documented.
  **Confidence: low on the exact approach.**

- **Package/distribution name.** Import name `hlmcp`; PyPI name likely
  `hyperliquid-microstructure-mcp`. **Confidence: medium; revisit before publish.**

## Testing strategy

Three test types, each with a distinct purpose. They are written WITH the code in
each build step, never deferred to a separate testing phase.

- **Unit tests (`tests/unit/`)** — pure functions and schema parsing, no network.
  Mocked dependencies where needed. Cover: `compute_imbalance` against recorded
  fixtures, `choose_aggregation` ladder behavior, `normalize_wallet` edge cases,
  position aggregation, schema parsing of real recorded responses. Fast, run on
  every commit, run in CI.

- **Integration tests (`tests/integration/`, `@pytest.mark.integration`)** — hit
  the LIVE Hyperliquid API. These are real integration tests, not mocked. Cover:
  fetching a real wallet's `clearinghouseState`, `list_dexes`, a real `l2Book`,
  end-to-end tool calls against live data. Opt-in (excluded from the default
  `pytest -m "not integration"` run) because they depend on an external service,
  can be rate-limited, and may be flaky. Run them deliberately before releases
  and when changing the venue layer.

- **E2E tests (from Step 4 onward)** — the MCP server installed in Claude Desktop
  (or the MCP inspector), invoked as an LLM would invoke it, verifying the whole
  path including the MCP protocol layer. Partly manual for v0 (install, ask the
  LLM to call the tool, verify the response). Automatable later via the MCP
  inspector's programmatic interface.

The `tests/venue/` directory holds respx-mocked HTTP tests of the venue adapter
— between unit and integration: they exercise the adapter's real logic
(normalization, validation, fan-out, error handling) but mock the HTTP layer so
they're fast and deterministic.