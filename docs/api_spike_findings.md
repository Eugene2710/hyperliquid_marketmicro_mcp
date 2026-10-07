# Hyperliquid REST API — Spike Findings

The empirical record from the pre-build API spike. Code in this project should
respect these findings rather than re-deriving them. If a finding looks wrong,
flag it — do not silently code against a different assumption.

All measurements taken from Singapore against `https://api.hyperliquid.xyz/info`.
Local clock verified synced to NTP (+20ms ±31ms) so latency/staleness numbers are
real, not clock-skew artifacts.

## TL;DR

- REST API is healthy: ~84ms p50 request latency, no throttling at 7 req/sec
  sustained for 30s, clean concurrent fan-out to 20 requests.
- Data freshness: REST snapshots are ~500ms stale (median); end-to-end data age
  ~600ms. Research/slow-loop grade, not HFT.
- `clearinghouseState` schema fully validated against real wallets including a
  34-position cross-margin whale.
- HIP-3 ecosystem: 8 active deployments alongside native HL; comprehensive whale
  monitoring requires fan-out across all of them.

## Q1 — l2Book aggregation (`nSigFigs`, `mantissa`)

**The 20-level cap.** `l2Book` returns at most 20 levels per side, always.
Aggregation happens first, then truncation to 20. Documented on the info-endpoint
page.

**`nSigFigs`** rounds prices to N significant figures (valid: `null`, 2, 3, 4, 5).
Bucket width = 1 unit in the Nth significant figure of the price.

**`mantissa`** is a bucket-width MULTIPLIER, only valid at `nSigFigs=5`. This was
discovered empirically — it is NOT a leading-digit constraint as one might guess.
- `mantissa=2` → buckets 2× the base width
- `mantissa=5` → buckets 5× the base width
- `mantissa=1` → **HTTP 500** (treated as redundant with default `nSigFigs=5`).
  Do not send it.

The 1/2/5 sequence is the standard "preferred numbers" series used for tick
spacing. `mantissa` fills the granularity gap between `nSigFigs=5` (1×) and
`nSigFigs=4` (10×).

**Measured on BTC @ ~$65,515:**

| Setting | Bucket width | 20-level range |
|---|---|---|
| `null` / `nSigFigs=5` | $1 (0.15 bps) | $19 (2.9 bps) |
| `nSigFigs=5, mantissa=2` | $2 (0.31 bps) | $38 (5.8 bps) |
| `nSigFigs=5, mantissa=5` | $5 (0.76 bps) | $95 (14.5 bps) |
| `nSigFigs=4` | $10 (1.53 bps) | $190 (29.0 bps) |
| `nSigFigs=3` | $100 (15.27 bps) | $1900 (290 bps) |
| `nSigFigs=2` | $1000 (153.85 bps) | $19000 (2923 bps) |

20-level range ≈ 20 × bucket width.

**The 30→296 bps gap.** There is NO API setting between `nSigFigs=4` (~30 bps
range) and `nSigFigs=3` (~296 bps range) because `mantissa` is only valid at
`nSigFigs=5`. Band requirements in (30, 296] bps must use `nSigFigs=3` and accept
coarser buckets. Tools should report the actual bucket width achieved.

**Calibration caveat.** The bps-per-bucket relationship is PRICE-DEPENDENT
(`nSigFigs` works on significant figures). The ladder above is BTC-shaped; coins
at very different price magnitudes need recalibration. `choose_aggregation` uses
this ladder as a default; per-coin overrides or a runtime probe are the fix.

## Q2 — clearinghouseState schema (verified)

Validated against a real 34-position cross-margin whale ($12.3M account) and a
3-position wallet. All positions had identical shape; all `oneWay`/`cross`.

**Envelope:** `marginSummary`, `crossMarginSummary`, `crossMaintenanceMarginUsed`,
`withdrawable`, `assetPositions`, `time`.

**Position inner shape (`assetPositions[].position`):** `coin`, `szi` (signed
size, negative=short), `leverage{type, value}`, `entryPx`, `positionValue`,
`unrealizedPnl`, `returnOnEquity`, `liquidationPx`, `marginUsed`, `maxLeverage`,
`cumFunding{allTime, sinceOpen, sinceChange}`.

**Field notes:**
- All numerics serialized as STRINGS for decimal precision. Parse explicitly.
- `liquidationPx` is `null` (not string) when over-collateralized to impossibility.
- `maxLeverage` is the symbol's ceiling, NOT this position's current leverage
  (that's `leverage.value`).
- `cumFunding.allTime` can differ from `sinceOpen` if a position was closed and
  reopened.
- Position wrapper `type` is `oneWay` | `hedged`. In `oneWay`, one net position
  per symbol. In `hedged`, long and short on the same symbol can coexist (symbol
  may appear twice in `assetPositions`).

**Risk-monitoring note (important).** For cross-margin positions, per-position
`liquidationPx` is derived from a snapshot of the whole account and is often
practically meaningless (e.g. a $172 liquidation price on a $2 short, because the
account has a huge buffer). The REAL liquidation threshold is account-level:
`accountValue − crossMaintenanceMarginUsed`. Tools should surface the
account-level buffer as the headline risk metric; per-position `liquidationPx` is
supplementary diagnostic only.

**Not yet observed:** isolated-margin position shape (may carry extra fields),
hedged-mode response shape, HIP-3-market position shape. Schema may need
extension when these appear — note this in code comments.

## Q2b — clearinghouseState edge cases (verified)

**Accepted as "valid format, no positions" (200 + empty envelope):** canonical
addresses with no activity; 40-char hex WITHOUT `0x` prefix (silently normalized
server-side); pure-uppercase addresses; addresses with dust balance (nonzero
`accountValue`, 0 positions).

**Rejected with HTTP 422:** wrong-length hex, non-hex characters, non-address
strings, empty string. Error body is a PLAIN STRING, not JSON:
`"Failed to deserialize the JSON body into the target type"` (a Rust `serde`
error — HL's backend is Rust; validation happens at the request-parse layer).

**Implications:**
- Normalize addresses client-side (lowercase, ensure `0x`) BEFORE sending, or a
  real wallet queried in non-canonical form silently returns empty = wrong data.
- Treat any non-2xx as an error carrying the body as a plain string; do NOT
  attempt JSON parsing on 4xx bodies.
- Empty `assetPositions` is a normal result, not an error.

## Q2c — dex parameter / HIP-3 routing (verified)

- Default (omitted) and explicit empty string `""` are IDENTICAL — both target
  native HL perps.
- Known HIP-3 dex names route correctly (`dex: "xyz"` returns positions on the
  xyz deployment). Balances are per-dex; a wallet can have separate margin pools
  on native HL and each HIP-3 dex.
- **Unknown dex name → HTTP 500 with empty body.** Indistinguishable from a real
  server error. REQUIRES client-side validation against the dex list.
- `dex: null` → HTTP 422 (type rejection). `dex: " "` (whitespace) → HTTP 500.

**Discovery via `perpDexs`:** `{"type": "perpDexs"}` returns a list whose first
element is `null` (native HL, represented as empty-string key) and remaining
elements are dicts with `name`, `fullName`, `deployer`, `oracleUpdater`,
`feeRecipient`, `assetToStreamingOiCap`. Observed: 8 active HIP-3 deployments.
Cache ~5 min (changes rarely).

**Universe sizes:** native HL 230 markets; xyz 91 markets. Total ecosystem likely
600+ markets across all dexes — meaningful enough that full whale coverage
requires HIP-3 fan-out.

## Q3 — latency baseline (verified, clock-synced)

Request latency (network round-trip): min 79ms, p50 84ms, p90 183ms, p99 476ms.

Snapshot staleness (real, clock verified): p50 491ms, p90 670ms, p99 737ms.

End-to-end data age (staleness + request latency): p50 585ms, p90 818ms,
p99 988ms.

**Conclusion:** REST `l2Book` is NOT a real-time feed; snapshots are ~0.5s stale
before network latency. Suitable for analysis, research, slow-loop decisioning
(10s+ windows), feature snapshots. NOT for HFT or sub-second loops. The
consistent ~500ms staleness suggests HL serves REST from a snapshot cache
refreshing ~twice/sec; the WebSocket `l2Book` subscription bypasses this. A
WS-backed adapter is the roadmap fix for live use.

## Q4 — sustained-load (verified)

7 req/sec (70% of documented ceiling) for 30s: 203 requests, ZERO errors, latency
identical to idle baseline (p50 84ms, p90 179ms, p99 872ms vs Q3's 84/204/892).
HL serves sustained traffic from the same path as bursts — no cold-path penalty.
Consumed ~35% of the 1-min weight budget; ample headroom.

**Production policy:** token bucket at 7 req/sec sustained, burst capacity 15.

## Q5 — concurrent fan-out (verified)

20 simultaneous `clearinghouseState` calls: 149ms wall-clock, 20/20 success, 0
errors. Wall-clock ≈ single-request p50 — genuine server-side parallelism, no
throttling. (A concurrency=10 run showed a 336ms outlier; small-sample variance,
`gather` wall-time = max(individual). Not a real degradation.)

**Conclusions:** `whale_position_monitor` can be stateless on-demand fan-out, no
background indexer. Semaphore cap at 20 in `_post`. Cross-dex query (20 wallets ×
9 dexes = 180 calls) completes ~1.4s via batches capped at 20 in-flight.
**Untested:** concurrency >20 (no ceiling found; 20 is enough for our use case).

## Q6 — recent trades availability (verified)

`candleSnapshot` works over REST (returns OHLCV). The per-trade feed is
WebSocket-only (`trades` subscription on `wss://api.hyperliquid.xyz/ws`). Any tool
needing tick-level fills (e.g. a queue-position estimator) requires a WS consumer;
out of v0 scope.

## Q7 — symbol formats (verified)

`l2Book` resolves cleanly for: `BTC`, `ETH` (native perps); `xyz:MSTR` (HIP-3
market); `@150` (spot index). Spot uses `PURR/USDC` for PURR and `@{index}`
otherwise. Remapping gotcha: BTC/USDC in the HL app is UBTC/USDC on mainnet
HyperCore — use the L1 name to detect remappings.

## Funding — metaAndAssetCtxs / fundingHistory / predictedFundings (live-probed 2026-10-05)

Probed with read-only curl while planning the `funding_carry` tool; fixtures
recorded the same day (`tests/fixtures/meta_and_asset_ctxs*.json`,
`funding_history_btc.json`, `predicted_fundings.json`).

**`metaAndAssetCtxs`** (`{"type": "metaAndAssetCtxs", "dex": ""}`):
- Returns `[meta, ctxs]`. `meta.universe[i]` and `ctxs[i]` describe the same
  market — the pairing is **purely positional** (native: 234 / 234).
- Ctx fields (all strings): `funding`, `premium`, `oraclePx`, `markPx`, `midPx`,
  `openInterest` (coin units), `dayNtlVlm`, `impactPxs`, `prevDayPx`, `dayBaseVlm`.
- `funding` is the **predicted rate for the current hour, per hour** (HL settles
  hourly). BTC showed `0.0000125` = the interest-rate floor (0.01% / 8h).
- `premium`, `midPx`, `impactPxs` are **`null` for delisted markets**
  (`isDelisted: true` on the universe entry; e.g. `MATIC`). Observed 56/234
  native, 21/131 on `xyz`.
- HIP-3 works with `"dex": "xyz"`; universe names come back **dex-prefixed**
  (`xyz:XYZ100`), and HIP-3 universe entries carry extra fields (`growthMode`,
  `deployerFeeScale`, `lastFeeScaleChangeTime`) → `extra="allow"`.
- **No server timestamp anywhere in the response.**

**`fundingHistory`** (`{"type": "fundingHistory", "coin", "startTime", "endTime"?}`):
- Rows `{coin, fundingRate, premium, time}`, one per hourly settlement,
  **oldest first** from `startTime`; `endTime` is honored.
- **At most 500 rows per response.** A 60-day request returned exactly the oldest
  500 hours — longer windows must be paged (next `startTime` = last row's
  `time + 1`). Settlement `time`s carry a few ms of jitter past the hour.
- HIP-3 coins work with the prefixed name (`xyz:XYZ100`).
- **Unknown coin → HTTP 500 with body `null`.** 5xx is retryable in our tenacity
  policy, so a typo would burn 3 attempts. Validate the coin against the
  `metaAndAssetCtxs` universe BEFORE calling (the `funding_carry` tool does).

**`predictedFundings`** (`{"type": "predictedFundings"}`):
- `[[coin, [[venue, {fundingRate, nextFundingTime, fundingIntervalHours} | null], ...]], ...]`,
  venues `BinPerp`, `HlPerp`, `BybitPerp` — the only three venues the endpoint
  returns. **Native coins only** (no `:` names).
- Rates are **per each venue's own interval** (BTC: Binance/Bybit 8h, HL 1h;
  some coins 4h) → divide by `fundingIntervalHours` before comparing.
- A venue entry is `null` when that venue does not list the coin (70 entries in
  the fixture); `fundingIntervalHours` is **sometimes absent** (21 entries) → the
  normalized rate is left `None`, not guessed.
- **HL's own `nextFundingTime` equals the most recent settlement — it lags one
  interval, so it is in the past.** 4/4 snapshots (fixture 2026-10-05, 3 live
  2026-10-06): at 02:35 UTC, with the last BTC settlement at 02:00 per
  `fundingHistory`, `HlPerp.nextFundingTime` was 02:00 for every coin (true next:
  03:00). Ground truth is HL's own hourly `fundingHistory` plus the clock, not the
  other venues. The next HL settlement is therefore computed as the next whole
  hour. Cause unknown. (`BinPerp`/`BybitPerp` showed future times consistent with
  an 8h 00/08/16 UTC grid, but were not checked against Binance/Bybit's own APIs;
  the tool does not use their `nextFundingTime`.)

**Sign conventions (verified live 2026-10-06):**
- Funding rate: **positive = longs pay shorts** (HL docs; consistent with the
  `userFunding` ledger deltas below).
- **`clearinghouseState.cumFunding` is positive when the position PAID funding**
  (it is a cost: the negation of the `userFunding` ledger's `usdc` cash flow).
  Checked 8 long positions (2 curated wallets) with a positive current rate: each
  latest `userFunding` delta had `usdc < 0` (cash paid), while
  `cumFunding.sinceChange` was **positive** on all 8. Shorts confirm the other
  direction: an ETH short received `usdc = +26.230339` in its one settlement since
  the last size change and showed `sinceChange = -26.230339` exactly. The
  `CumFunding` docstring in `schemas/hl_api.py` originally said the opposite and
  has been corrected. `funding_carry` defines its own convention (+ = that side
  receives) and does not depend on `cumFunding`.

## Doc references

- Info endpoint (l2Book, candleSnapshot, symbol notation):
  `hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint`
- clearinghouseState (perpetuals sub-page):
  `.../api/info-endpoint/perpetuals`
- Rate limits:
  `.../api/rate-limits-and-user-limits`
- WebSocket:
  `.../api/websocket`
- Official Python SDK (tiebreaker when docs are ambiguous):
  `github.com/hyperliquid-dex/hyperliquid-python-sdk`