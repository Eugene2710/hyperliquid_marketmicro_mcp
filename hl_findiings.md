## REST API latency profile (Q3, Q3c)

### Measurement setup
- Endpoint: `POST https://api.hyperliquid.xyz/info` from Singapore
- Payload: `{"type": "l2Book", "coin": "BTC", "nSigFigs": 5}`
- 20 sequential calls, 250ms apart, after warm-up discard
- Local clock verified against NTP at +20ms ±31ms (effectively synced)

### Request latency (network round-trip)
- min: 77ms
- p50: 84ms
- p90: 204ms
- p99: 892ms (note: small sample, p99 ≈ max)

### Snapshot staleness (real, after clock-sync verification)
- p50: 499ms
- p90: 697ms
- p99: 722ms

### End-to-end data age (staleness + request latency)
- p50: 600ms
- p90: 803ms
- p99: 1092ms

### Implications
- REST `l2Book` is *not* a real-time feed. Snapshots are 0.5-0.7 seconds
  stale even before network latency. Total data age at the consumer is
  ~0.6-1.1 seconds.
- Suitable for: analysis, research workflows, slow-loop agent decisioning
  (10s+ decision windows), feature snapshots into ML pipelines, dashboards.
- Not suitable for: HFT, sub-second decision loops, queue-position
  estimation when actually trading.
- The WebSocket `l2Book` subscription provides push-based real-time updates;
  a v0.2 WS-backed adapter is on the roadmap for the live-trading use case.

### Implications for tool design
- Every tool response must carry `meta.staleness_ms` so the LLM can reason
  about data age.
- Tool docstrings should mention freshness profile so LLMs reading them
  know when not to trust the data for time-sensitive decisions.
- Venue adapter request timeouts should be 3-5 seconds (allow for p99 tails),
  not 1 second.