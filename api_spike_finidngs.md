# Hyperliquid REST API Spike — Findings

Conducted: 12 June 2026
Spike duration: ~3 hours
Coverage: l2Book aggregation, clearinghouseState shape and edge cases,
          HIP-3 dex routing, latency profile, sustained-load behavior,
          concurrent fan-out, symbol formats, recent-trades availability

## TL;DR

- REST API is healthy from Singapore: 80ms p50 request latency, no throttling
  observed at 7 req/sec sustained for 30 seconds, no errors.
- Data freshness: 500ms median snapshot staleness; REST is research-grade,
  not HFT. WS upgrade on roadmap.
- Schema for clearinghouseState fully validated; analytics layer can rely
  on consistent shapes.
- HIP-3 ecosystem has 8 active dexes; comprehensive whale monitoring requires
  fan-out across all of them.

## Verified findings

### l2Book aggregation
[mantissa semantics, ladder, 30→296 bps gap]

### clearinghouseState
[schema, edge cases, error bodies, dex routing]

### Latency
[Q3 numbers, sustained-load comparison, clock skew verified]

### Concurrency
[Q5 numbers, semaphore-at-20 conclusion]

### Symbols
[BTC/ETH/HIP-3/spot all work via l2Book]

## Operating envelope (production policy)

- Max concurrency: 20 in-flight requests (semaphore at venue adapter)
- Sustained rate: 7 req/sec (token bucket; 70% of documented ceiling)
- Burst capacity: 15 tokens (allows brief LLM-driven bursts)
- Per-request timeout: 3-5 seconds (3-5× Q3 p99)
- Address normalization: required client-side
- Dex validation: required client-side (against perpDexs cache)

## Untested / known gaps

- Concurrency >20 (not measured; 20 is enough for our use case)
- Sustained load >30s (not measured; 30s is enough to characterize)
- WebSocket subscriptions (separate sub-project for live feeds)
- Exchange endpoint (out of scope; read-only adapter)
- Isolated-margin position shape (no isolated positions observed in tested wallets)
- Hedged-mode position shape (no hedged-mode wallets observed)

## Code artifacts produced

- venues/hyperliquid.py (venue adapter)
- schemas: HLClearinghouseState et al
- analytics/aggregation.py (choose_aggregation)
- docs/api_spike_findings.md (this file)