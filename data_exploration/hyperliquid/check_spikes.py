"""
7 Questions to address:

1. Does l2Book return what the docs claim? Hit it for BTC, dump the full JSON, eyeball the shape.
Confirm nSigFigs and nLevels actually affect output.

A: Yes, l2Book returns the value based on the nSigFigs and mantissa specified by the user with A HARD CAP OF 20.

- nSigFigs rounds prices to N significant figures before grouping them into buckets.
    Valid: null (full precision), 5, 4, 3, 2. The bucket width is one unit at the Nth significant figure of the price.
- mantissa scales that base bucket width by 2× or 5×. Only valid when nSigFigs=5. Valid: 2, 5.
    The value 1 is documented but returns HTTP 500 in practice (treated as redundant with default nSigFigs=5).

e.g
Setting         |          Bucket width         |          20-level range           |           What you can see
null or nSigFigs=5        $1 (0.16 bps)                   $19 (3 bps)                   Inside spread, top-of-book only
nSigFigs=5, mantissa=2    $2 (0.31 bps)                   $38 (6 bps)                   Top-of-book + a sliver beyond
nSigFigs=5, mantissa=5    $5 (0.78 bps)                   $95 (15 bps)                  Near-touch flow, ~1 spread width out


2. Does clearinghouseState work for any wallet? Pull a current top-leaderboard wallet from Hyperdash, query it,
confirm assetPositions has the structure you need.

- marginSummary: overall account snapshot, e.g
marginSummary = {
    "accountValue":      "10000.00",   # collateral + unrealized PnL; most important single number/what account is worth currently
    "totalNtlPos":       "80000.00",   # sum of |position notional| across all positions; total exposure regardless of direction
    "totalRawUsd":       "10000.00",   # raw USDC balance, ignoring PnL
    "totalMarginUsed":   "9600.00",    # collateral currently locked behind positions; slice of your collateral being locked up
}

- crossMarginSummary: the same shape of marginSummary, but only counts positions in cross-margin mode
Hyperliquid lets each position run in one of two nodes:

a. Cross-margin: positions share the same collateral pool. If your BTC long is winning and your ETH short is losing,
the BTC PnL props up the ETH position automatically. One unified pool of margin defending everything.

b. Isolated-margin: each position has its own walled-off chunk of collateral. The BTC long can't help the ETH short;
if the ETH short loses its allocated isolated margin, that one position liquidates while BTC keeps running.

- crossMaintenanceMarginUsed: the minimum margin cross pool must keep aside to avoid liquidation, given current positions.
2 different margin numebers exist:
a. Initial Margin: collateral required to open a position. With 10× leverage, initial = 10% of notional. This is what totalMarginUsed tracks.

b. Maintenance margin:  collateral required to keep a position open. Lower than initial (HL's maintenance ratio is typically half the initial ratio).
This is what crossMaintenanceMarginUsed tracks for the cross-margin subset.

- If accountValue drops below crossMaintenanceMarginUsed, cross-margin positions start getting liquidated.
The gap between accountValue and crossMaintenanceMarginUsed is your "buffer before liquidation" — a critical risk metric

- withdrawable: how much USDC the user can pull out of their HL account right now

- Roughly: accountValue - totalMarginUsed - any_held_orders = withdrawable


3. What's the latency profile from Singapore? Run 30 sequential calls, record p50/p90/p99.
HL has edge infrastructure in Singapore so this should be fast, but verify.

4. What's the rate-limit behavior? Documented limits vs. what actually happens.
Push it gently — say 5 req/sec for 30 seconds — and see if you hit 429s, soft throttling, or nothing.

5. Can you fan out concurrent clearinghouseState requests? Try 10 wallets in parallel.
This is the make-or-break test for Tool 2.

6. Is recent-trades available over REST or WS-only?
This determines whether Tool 3 (queue estimator) can be REST-only or needs a WS subscription.

7. Do the symbol-format edge cases work? Try a HIP-3 market (xyz:MSTR) and a spot index (@150).
The cleanest way to discover undocumented constraints.

Top 6 addresses by PnL, at point of extraction
0x393d0b87ed38fc779fd9611144ae649ba6082109
0x488d2a9b70cc18ef66057a48ab3d59da1c59fe08
0x4eb8d907136189a34c9b087950211b6a566f7819
0x05cafe987297448f21a3c7ae0ae815fddecac655
0xe44bd27c9f10fa2f89fdb3ab4b4f0e460da29ea8
0x0ddf9bae2af4b874b96d287a5ad42eb47138a902

"""
import asyncio
import json
import time
from statistics import median
from typing import Any

import httpx

URL = "https://api.hyperliquid.xyz/info"


async def post(client: httpx.AsyncClient, payload: dict[str, Any]) -> tuple[Any, float, int]:
    t0 = time.perf_counter()
    r = await client.post(URL, json=payload, timeout=10.0)
    ms = (time.perf_counter() - t0) * 1000
    try:
        return r.json(), ms, r.status_code
    except Exception:
        return r.text, ms, r.status_code

def choose_aggregation(max_band_bps: float) -> dict[str, int]:
    """Pick l2Book aggregation params so the 20-level range covers max_band_bps.

    Calibrated against BTC at ~$64k (Dec 2026 spike). The bps-per-level
    relationship varies with the coin's price magnitude, so this ladder is
    BTC-shaped; other symbols may need a runtime probe.

    Note: there is no API setting between nSigFigs=4 (~30 bps range) and
    nSigFigs=3 (~296 bps range). Band requirements in that gap will use
    nSigFigs=3 and get coarser-than-ideal bucket widths. The response
    metadata should report achieved bucket width so callers can judge fit.
    """
    if max_band_bps <= 6:
        return {"nSigFigs": 5, "mantissa": 2}     # ~6 bps range
    if max_band_bps <= 15:
        return {"nSigFigs": 5, "mantissa": 5}     # ~15 bps range
    if max_band_bps <= 30:
        return {"nSigFigs": 4}                    # ~30 bps range
    if max_band_bps <= 296:
        return {"nSigFigs": 3}                    # ~296 bps range (no finer option)
    if max_band_bps <= 2969:
        return {"nSigFigs": 2}                    # ~2969 bps range
    return {"nSigFigs": 2}                        # widest available; clamp


# For the default order_book_imbalance bands of [10, 25, 50, 100]:
#   max_band = 100 bps  →  choose_aggregation returns {"nSigFigs": 3}
# Which gives ~310 bps of range — plenty for the 100 bps band.

# async def q1_l2_aggregation(client: httpx.AsyncClient) -> None:
#     print("\n[Q1] l2Book — aggregation behavior")
#     variants = [
#         {"label": "full precision",  "params": {}},
#         {"label": "nSigFigs=5 m=1",  "params": {"nSigFigs": 5, "mantissa": 1}},
#         {"label": "nSigFigs=5 m=5",  "params": {"nSigFigs": 5, "mantissa": 5}},
#         {"label": "nSigFigs=4",      "params": {"nSigFigs": 4}},
#         {"label": "nSigFigs=2",      "params": {"nSigFigs": 2}},
#     ]
#     for v in variants:
#         payload = {"type": "l2Book", "coin": "BTC", **v["params"]}
#         data, ms, status = await post(client, payload)
#         bids, asks = data["levels"]
#         # The bucket size = distance between adjacent levels — shows aggregation
#         bucket_bps = ((float(bids[0]["px"]) - float(bids[1]["px"]))
#                       / float(bids[0]["px"])) * 10_000 if len(bids) > 1 else 0
#         print(f"  {v['label']:<20}  bid_levels={len(bids):<3} "
#               f"adjacent_bucket={bucket_bps:.2f}bps  latency={ms:.0f}ms")
async def q1_l2_aggregation(client: httpx.AsyncClient) -> None:
    """Explore the full grid of nSigFigs/mantissa settings on a real coin."""
    print("\n[Q1a] l2Book — aggregation grid (full exploration)")
    variants = [
        {"label": "full precision",  "params": {}},
        {"label": "nSigFigs=5",      "params": {"nSigFigs": 5}},
        {"label": "nSigFigs=5 m=1",  "params": {"nSigFigs": 5, "mantissa": 1}},
        {"label": "nSigFigs=5 m=2",  "params": {"nSigFigs": 5, "mantissa": 2}},
        {"label": "nSigFigs=5 m=5",  "params": {"nSigFigs": 5, "mantissa": 5}},
        {"label": "nSigFigs=4",      "params": {"nSigFigs": 4}},
        {"label": "nSigFigs=3",      "params": {"nSigFigs": 3}},
        {"label": "nSigFigs=2",      "params": {"nSigFigs": 2}},
    ]

    measurements: list[dict] = []  # for the calibration table at the end

    for v in variants:
        payload = {"type": "l2Book", "coin": "BTC", **v["params"]}
        data, ms, status = await post(client, payload)

        if not isinstance(data, dict) or "levels" not in data:
            print(f"  {v['label']:<20}  status={status}  "
                  f"unexpected_response={str(data)[:120]!r}  latency={ms:.0f}ms")
            continue

        bids, asks = data["levels"]
        if not bids or len(bids) < 2:
            print(f"  {v['label']:<20}  status={status}  "
                  f"empty_or_thin: bid_levels={len(bids)}  latency={ms:.0f}ms")
            continue

        top_bid_px = float(bids[0]["px"])
        next_bid_px = float(bids[1]["px"])
        bottom_bid_px = float(bids[-1]["px"])

        bucket_dollars = top_bid_px - next_bid_px
        bucket_bps = (bucket_dollars / top_bid_px) * 10_000
        # The 20-level range — how far from the top the deepest visible level sits
        range_dollars = top_bid_px - bottom_bid_px
        range_bps = (range_dollars / top_bid_px) * 10_000

        print(f"  {v['label']:<20}  status={status}  "
              f"top_bid=${top_bid_px:,.2f}  "
              f"bucket=${bucket_dollars:.2f} ({bucket_bps:.2f}bps)  "
              f"20-lvl_range=${range_dollars:.2f} ({range_bps:.1f}bps)  "
              f"latency={ms:.0f}ms")

        measurements.append({
            "label": v["label"],
            "params": v["params"],
            "top_bid": top_bid_px,
            "bucket_bps": bucket_bps,
            "range_bps": range_bps,
        })

    # Verify choose_aggregation: for each band requirement, does the picked
    # setting actually deliver enough range?
    print("\n[Q1b] choose_aggregation verification")
    for max_band_bps in [5, 10, 25, 50, 100, 250, 500, 1000]:
        picked = choose_aggregation(max_band_bps)
        payload = {"type": "l2Book", "coin": "BTC", **picked}
        data, ms, status = await post(client, payload)

        if not isinstance(data, dict) or "levels" not in data:
            print(f"  max_band={max_band_bps:>5}bps  picked={picked}  "
                  f"FAILED status={status}")
            continue

        bids = data["levels"][0]
        if len(bids) < 2:
            print(f"  max_band={max_band_bps:>5}bps  picked={picked}  thin book")
            continue

        top = float(bids[0]["px"])
        bottom = float(bids[-1]["px"])
        achieved_range_bps = ((top - bottom) / top) * 10_000
        verdict = "OK" if achieved_range_bps >= max_band_bps else "INSUFFICIENT"
        print(f"  max_band={max_band_bps:>5}bps  picked={str(picked):<35}  "
              f"achieved_range={achieved_range_bps:.0f}bps  {verdict}")

async def q3_latency_baseline(client: httpx.AsyncClient, n: int = 30) -> None:
    """Steady-state latency for a representative l2Book call.

    Repeats the same payload N times with a short pause between calls so we
    measure baseline latency, not throttling behavior. Q4 handles the under-load case.
    """
    print(f"\n[Q3] l2Book latency baseline — {n} identical calls, 250ms apart")
    payload = {"type": "l2Book", "coin": "BTC", "nSigFigs": 5}

    # Discard the first call as a warm-up — DNS, TLS handshake, connection pool init.
    await post(client, payload)

    request_latencies: list[float] = []
    snapshot_staleness: list[int] = []
    statuses: list[int] = []

    for _ in range(n):
        send_time_ms = int(time.time() * 1000)
        data, ms, status = await post(client, payload)
        statuses.append(status)
        request_latencies.append(ms)

        if isinstance(data, dict) and "time" in data:
            # Staleness = how old the snapshot was at the moment it left the server.
            # Approximated as: server_time vs the midpoint of our request window.
            midpoint = send_time_ms + (ms / 2)
            staleness = int(midpoint - data["time"])
            snapshot_staleness.append(staleness)

        await asyncio.sleep(0.25)

    # Helper for percentile from a sorted list
    def pct(sorted_vals: list[float], p: float) -> float:
        if not sorted_vals:
            return 0.0
        idx = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
        return sorted_vals[idx]

    s_lat = sorted(request_latencies)
    s_age = sorted(snapshot_staleness)

    print(f"  status_codes={sorted(set(statuses))}")
    print(f"  request latency (ms):")
    print(f"    min={min(s_lat):.0f}  p50={pct(s_lat, 0.5):.0f}  "
          f"p90={pct(s_lat, 0.9):.0f}  p99={pct(s_lat, 0.99):.0f}  "
          f"max={max(s_lat):.0f}")
    if snapshot_staleness:
        print(f"  snapshot staleness (ms — how old the data was when returned):")
        print(f"    min={min(s_age)}  p50={pct(s_age, 0.5):.0f}  "
              f"p90={pct(s_age, 0.9):.0f}  p99={pct(s_age, 0.99):.0f}  "
              f"max={max(s_age)}")

    # End-to-end "freshness budget": how stale is the data by the time it lands in our code
    if snapshot_staleness:
        end_to_end = [age + lat for age, lat in zip(snapshot_staleness, request_latencies)]
        e = sorted(end_to_end)
        print(f"  end-to-end data age (staleness + request latency, ms):")
        print(f"    p50={pct(e, 0.5):.0f}  p90={pct(e, 0.9):.0f}  p99={pct(e, 0.99):.0f}")


async def q3b_clock_skew_check(client: httpx.AsyncClient) -> None:
    """Measure clock offset between local machine and HL servers."""
    print("\n[Q3b] clock skew check — local vs HL server time")
    offsets: list[float] = []
    for _ in range(10):
        t_send = time.time() * 1000
        data, _, _ = await post(client, {"type": "l2Book", "coin": "BTC"})
        t_recv = time.time() * 1000
        if isinstance(data, dict) and "time" in data:
            # HL server time, vs the midpoint of our request window.
            # If clocks are synced, server_time should be very close to midpoint
            # (snapshot ages are typically tens of ms on a busy market).
            local_midpoint = (t_send + t_recv) / 2
            offset_ms = local_midpoint - data["time"]
            offsets.append(offset_ms)
        await asyncio.sleep(0.25)

    if offsets:
        offsets.sort()
        print(f"  local_midpoint - server_time, n={len(offsets)}:")
        print(f"    min={min(offsets):.0f}ms  median={offsets[len(offsets)//2]:.0f}ms  "
              f"max={max(offsets):.0f}ms")
        print(f"  interpretation:")
        print(f"    if median > ~100ms consistently → local clock is behind, OR")
        print(f"                                       HL's L2 snapshot is stale")
        print(f"    if median near 0 → clocks synced, L2 is fresh")
        print(f"    if median is negative → local clock ahead of HL's")

    if offsets:
        offsets.sort()
        print(f"  local_midpoint - server_time, n={len(offsets)}:")
        print(f"    min={min(offsets):.0f}ms  median={offsets[len(offsets)//2]:.0f}ms  "
              f"max={max(offsets):.0f}ms")
        print(f"  interpretation:")
        print(f"    if median > ~100ms consistently → local clock is behind, OR")
        print(f"                                       HL's L2 snapshot is stale")
        print(f"    if median near 0 → clocks synced, L2 is fresh")
        print(f"    if median is negative → local clock ahead of HL's")

# async def q1_q3_l2_and_latency(client: httpx.AsyncClient) -> None:
#     print("\n[Q1, Q3] l2Book — shape + latency")
#     data, ms, status = await post(client, {"type": "l2Book", "coin": "BTC", "nSigFigs": 5})
#     print(f"  status={status}  latency={ms:.0f}ms  keys={list(data) if isinstance(data, dict) else type(data)}")
#     if isinstance(data, dict) and "levels" in data:
#         bids, asks = data["levels"]
#         print(f"  bid_depth={len(bids)}  ask_depth={len(asks)}")
#         print(f"  top_bid={bids[0]}  top_ask={asks[0]}")
#         print(f"  server_time={data.get('time')}  age_ms={int(time.time()*1000) - data.get('time', 0)}")


async def q2_clearinghouse(client: httpx.AsyncClient, wallet: str) -> None:
    """Probe clearinghouseState — full response: margin summaries + positions."""
    print("\n[Q2] clearinghouseState — shape")
    data, ms, status = await post(client, {
        "type": "clearinghouseState",
        "user": wallet,
    })
    print(f"  status={status}  latency={ms:.0f}ms  wallet={wallet[:10]}...")

    if not isinstance(data, dict):
        print(f"  UNEXPECTED response type: {type(data).__name__}  raw={str(data)[:200]!r}")
        return

    # Top-level shape
    print(f"  keys={sorted(data.keys())}")

    # Account-level margin and risk
    print("\n  --- Account-level margin ---")
    print(f"  marginSummary:")
    print(json.dumps(data.get("marginSummary"), indent=4))
    print(f"  crossMarginSummary:")
    print(json.dumps(data.get("crossMarginSummary"), indent=4))
    print(f"  crossMaintenanceMarginUsed: {data.get('crossMaintenanceMarginUsed')}")
    print(f"  withdrawable: {data.get('withdrawable')}")

    # Derived account-level liquidation buffer
    try:
        account_value = float(data["marginSummary"]["accountValue"])
        maint_used = float(data["crossMaintenanceMarginUsed"])
        if maint_used > 0:
            buffer_usd = account_value - maint_used
            buffer_pct = (buffer_usd / account_value) * 100
            print(f"\n  Derived: cross-margin liquidation buffer = "
                  f"${buffer_usd:,.2f} ({buffer_pct:.1f}% of account value)")
        else:
            print(f"\n  Derived: no cross maintenance margin in use "
                  f"(either no cross positions or all positions over-collateralized)")
    except (KeyError, TypeError, ValueError) as e:
        print(f"\n  Could not compute liquidation buffer: {e}")

    # Server-side timestamp + staleness
    server_time = data.get("time")
    if server_time:
        import time
        age_ms = int(time.time() * 1000) - server_time
        print(f"  server_time={server_time}  response_age_ms={age_ms}")

    # Positions
    print("\n  --- Positions ---")
    positions = data.get("assetPositions", [])
    print(f"  open_positions={len(positions)}")

    if positions:
        # Per-position shape consistency across the full list
        all_outer_keys = {tuple(sorted(p.keys())) for p in positions}
        all_inner_keys = {tuple(sorted(p.get("position", {}).keys())) for p in positions}
        all_types = {p.get("type") for p in positions}
        all_lev_modes = {p.get("position", {}).get("leverage", {}).get("type") for p in positions}

        print(f"  distinct outer shapes: {len(all_outer_keys)}")
        print(f"  distinct inner shapes: {len(all_inner_keys)}")
        print(f"  position types seen: {all_types}")
        print(f"  leverage modes seen: {all_lev_modes}")

        if len(all_inner_keys) > 1:
            print(f"  WARNING — inner shape varies; investigate:")
            for shape in all_inner_keys:
                print(f"    {shape}")

        # Coin-uniqueness check (hedged mode would show duplicates)
        coins = [p["position"]["coin"] for p in positions]
        if len(coins) != len(set(coins)):
            duplicates = [c for c in set(coins) if coins.count(c) > 1]
            print(f"  duplicate coins found (hedged mode signal): {duplicates}")

        # Full dump of one position for shape inspection
        print(f"\n  Full first position:")
        print(json.dumps(positions[0], indent=2))

        # Compact summary of all positions
        print(f"\n  All positions (summary):")
        for p in positions:
            pos = p["position"]
            szi = float(pos["szi"])
            direction = "LONG" if szi > 0 else "SHORT"
            notional = float(pos["positionValue"])
            pnl = float(pos["unrealizedPnl"])
            lev = pos["leverage"]
            liq = pos.get("liquidationPx") or "—"
            print(f"    {pos['coin']:<8} {direction:<5} size={abs(szi):>14,.4f}  "
                  f"notional=${notional:>14,.2f}  pnl=${pnl:>+12,.2f}  "
                  f"lev={lev['type']}/{lev['value']}x  liq={liq}")
        if positions:
            print(f"  sample raw: {json.dumps(positions, indent=2)}")  # full

async def q2b_clearinghouse_edge_cases(client: httpx.AsyncClient) -> None:
    """Probe clearinghouseState's response to malformed and edge-case inputs."""
    print("\n[Q2b] clearinghouseState — edge cases")

    edge_cases = [
        # Valid format, almost certainly no HL activity ever
        ("empty_wallet_zeros",  "0x0000000000000000000000000000000000000001"),
        # Valid format, random hex — almost certainly no HL activity
        ("empty_wallet_random", "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"),
        # Malformed: too short (10 hex chars after 0x instead of 40)
        ("too_short",           "0xabc1234567"),
        # Malformed: too long (50 hex chars)
        ("too_long",            "0x" + "a" * 50),
        # Malformed: missing 0x prefix
        ("no_prefix",           "a" * 40),
        # Malformed: non-hex characters in the address
        ("non_hex_chars",       "0x" + "z" * 40),
        # Wrong type entirely
        ("not_an_address",      "not-a-wallet"),
        # Empty string
        ("empty_string",        ""),
        # Uppercase address (EIP-55 mixed case is standard, but pure uppercase might trip it)
        ("uppercase_hex",       "0xD6E56265DEADBEEFDEADBEEFDEADBEEFDEADBEEF"),
    ]

    for label, wallet in edge_cases:
        try:
            data, ms, status = await post(client, {
                "type": "clearinghouseState",
                "user": wallet,
            })
        except Exception as e:
            print(f"  {label:<22}  EXCEPTION  {type(e).__name__}: {e}  ")
            continue

        # Classify the response
        if isinstance(data, dict):
            has_envelope = "marginSummary" in data and "assetPositions" in data
            if has_envelope:
                n_positions = len(data.get("assetPositions", []))
                account_value = data.get("marginSummary", {}).get("accountValue", "—")
                print(f"  {label:<22}  status={status}  valid_envelope  "
                      f"positions={n_positions}  accountValue={account_value}  "
                      f"latency={ms:.0f}ms")
            else:
                # 200 but error-shaped body
                print(f"  {label:<22}  status={status}  unexpected_dict  "
                      f"keys={sorted(data.keys())}  raw={str(data)[:200]!r}  "
                      f"latency={ms:.0f}ms")
        else:
            print(f"  {label:<22}  status={status}  "
                  f"type={type(data).__name__}  raw={str(data)[:200]!r}  "
                  f"latency={ms:.0f}ms")


async def q2c_clearinghouse_dex_routing(client: httpx.AsyncClient, wallet: str) -> None:
    """Probe clearinghouseState's `dex` field — routing and HIP-3 discovery."""
    print("\n[Q2c] clearinghouseState — dex parameter behavior")

    # --- Q2c-1: dex parameter variants on a known active wallet ---
    print("\n[Q2c-1] dex parameter routing")

    dex_variants: list[tuple[str, dict[str, Any]]] = [
        ("default (omitted)",       {}),
        ("explicit empty string",   {"dex": ""}),
        ("known HIP-3: xyz",        {"dex": "xyz"}),
        ("nonexistent dex",         {"dex": "definitely_not_a_real_dex"}),
        ("empty-string with space", {"dex": " "}),
        ("null-equivalent (None)",  {"dex": None}),
    ]
    for label, extra in dex_variants:
        payload = {"type": "clearinghouseState", "user": wallet, **extra}
        try:
            data, ms, status = await post(client, payload)
        except Exception as e:
            print(f"  {label:<30}  EXCEPTION {type(e).__name__}: {e}")
            continue

        if isinstance(data, dict) and "marginSummary" in data:
            n_positions = len(data.get("assetPositions", []))
            account_value = data.get("marginSummary", {}).get("accountValue", "—")
            print(f"  {label:<30}  status={status}  valid_envelope  "
                  f"positions={n_positions}  accountValue={account_value}  "
                  f"latency={ms:.0f}ms")
        else:
            print(f"  {label:<30}  status={status}  "
                  f"type={type(data).__name__}  raw={str(data)[:160]!r}  "
                  f"latency={ms:.0f}ms")

    # --- Q2c-2: discovering HIP-3 dexes via `meta` and `perpDexs` ---
    print("\n[Q2c-2] discovery of HIP-3 dexes via info endpoints")

    discovery_payloads: list[tuple[str, dict[str, Any]]] = [
        # `meta` returns universe (perp markets) for the default dex
        ("meta (default dex)",      {"type": "meta"}),
        # `meta` with dex parameter — does it accept dex routing?
        ("meta (dex=xyz)",          {"type": "meta", "dex": "xyz"}),
        # `perpDexs` is the dedicated discovery endpoint mentioned in HL docs
        ("perpDexs",                {"type": "perpDexs"}),
        # `metaAndAssetCtxs` — fuller metadata, may include dex info
        ("metaAndAssetCtxs",        {"type": "metaAndAssetCtxs"}),
    ]
    for label, payload in discovery_payloads:
        try:
            data, ms, status = await post(client, payload)
        except Exception as e:
            print(f"  {label:<28}  EXCEPTION {type(e).__name__}: {e}")
            continue

        if status != 200:
            print(f"  {label:<28}  status={status}  raw={str(data)[:160]!r}  "
                  f"latency={ms:.0f}ms")
            continue

        # Heuristics for shape
        if isinstance(data, list):
            print(f"  {label:<28}  status={status}  list_len={len(data)}  "
                  f"first_item_type={type(data[0]).__name__ if data else 'empty'}  "
                  f"latency={ms:.0f}ms")
            if data and isinstance(data[0], dict):
                print(f"    first item keys: {sorted(data[0].keys())}")
            elif data:
                print(f"    sample: {json.dumps(data[:3], indent=2)[:400]}")
        elif isinstance(data, dict):
            print(f"  {label:<28}  status={status}  keys={sorted(data.keys())}  "
                  f"latency={ms:.0f}ms")
            # If universe is present, count markets
            if "universe" in data and isinstance(data["universe"], list):
                print(f"    universe contains {len(data['universe'])} markets")
                if data["universe"]:
                    print(f"    sample market: {json.dumps(data['universe'][0])[:300]}")
        else:
            print(f"  {label:<28}  status={status}  "
                  f"type={type(data).__name__}  latency={ms:.0f}ms")

    # --- Q2c-3: if we found HIP-3 dexes, query the wallet's positions on them ---
    print("\n[Q2c-3] (placeholder) fan-out across discovered dexes")
    print("  This block is informational — re-run after inspecting Q2c-2 results")
    print("  to add explicit dex names found in the discovery output.")


async def q4_rate_limit(client: httpx.AsyncClient, n: int = 30) -> None:
    print(f"\n[Q4] rate-limit probe — {n} sequential l2Book calls")
    lats: list[float] = []
    statuses: list[int] = []
    for _ in range(n):
        _, ms, st = await post(client, {"type": "l2Book", "coin": "BTC"})
        lats.append(ms)
        statuses.append(st)
    bad = [s for s in statuses if s != 200]
    print(f"  errors={len(bad)}/{n}  status_codes_seen={sorted(set(statuses))}")
    s = sorted(lats)
    print(f"  p50={median(s):.0f}ms  p90={s[int(n*0.9)]:.0f}ms  p99={s[int(n*0.99)]:.0f}ms")


async def q5_concurrent(client: httpx.AsyncClient, wallets: list[str]) -> None:
    print(f"\n[Q5] concurrent clearinghouseState — {len(wallets)} wallets")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        post(client, {"type": "clearinghouseState", "user": w}) for w in wallets
    ])
    wall = (time.perf_counter() - t0) * 1000
    statuses = [s for _, _, s in results]
    print(f"  wall_ms={wall:.0f}  per_req_amortized={wall/len(wallets):.0f}ms")
    print(f"  status_codes={sorted(set(statuses))}")


async def q6_trades_rest(client: httpx.AsyncClient) -> None:
    print("\n[Q6] recent trades over REST?")
    # Try candleSnapshot (documented) — confirms REST OHLCV works.
    data, ms, status = await post(client, {
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": "1m",
            "startTime": int(time.time() * 1000) - 600_000,
            "endTime": int(time.time() * 1000),
        },
    })
    print(f"  candleSnapshot: status={status} latency={ms:.0f}ms count={len(data) if isinstance(data, list) else 'n/a'}")
    # Note: per-trade feed is WS-only via 'trades' subscription. If you need
    # tick-level fills for queue estimator, plan a small WS consumer.


async def q7_symbol_formats(client: httpx.AsyncClient) -> None:
    print("\n[Q7] symbol-format edge cases")
    for coin in ["BTC", "ETH", "xyz:MSTR", "@150"]:
        data, ms, status = await post(client, {"type": "l2Book", "coin": coin})
        ok = isinstance(data, dict) and "levels" in data
        print(f"  coin={coin!r:>14}  status={status}  has_levels={ok}  latency={ms:.0f}ms")


async def main() -> None:
    # Fill these from Hyperdash's current leaderboard before running.
    sample_wallet = "0xf62edeee17968d4c55d1c74936d2110333342f30" # 0xd6e56265890b76413d1d527eb9b75e334c0c5b42
    wallet_pool = ["0x393d0b87ed38fc779fd9611144ae649ba6082109",
                   "0x488d2a9b70cc18ef66057a48ab3d59da1c59fe08",
                   "0x4eb8d907136189a34c9b087950211b6a566f7819",
                   "0x05cafe987297448f21a3c7ae0ae815fddecac655",
                   "0xe44bd27c9f10fa2f89fdb3ab4b4f0e460da29ea8",
                   "0x0ddf9bae2af4b874b96d287a5ad42eb47138a902",
                   ]   # 6 different leaderboard wallets

    async with httpx.AsyncClient() as client:
        await q1_l2_aggregation(client)
        await q2_clearinghouse(client, sample_wallet)
        await q2b_clearinghouse_edge_cases(client)
        await q2c_clearinghouse_dex_routing(client, sample_wallet)
        await q3_latency_baseline(client, n=20)
        await q3b_clock_skew_check(client)
        await q5_concurrent(client, wallet_pool)
        await q6_trades_rest(client)
        await q7_symbol_formats(client)
        await q4_rate_limit(client, n=30)  # last — most likely to trip throttling


if __name__ == "__main__":
    asyncio.run(main())