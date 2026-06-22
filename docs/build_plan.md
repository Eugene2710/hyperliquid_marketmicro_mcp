# Build Plan

The build is sequenced into 7 steps (0–6). Each step is one fresh Claude Code
terminal session, to keep context tight and prevent drift. Steps respect a strict
dependency order (nothing depends on something built later) and front-load the
first demoable artifact as early as the dependencies allow.

## Why this decomposition

- **One architectural layer or one vertical slice per step.** Fine enough that a
  fresh-context session holds the whole thing without strain; coarse enough that
  orchestration overhead doesn't dominate.
- **Analytics before the venue adapter** even though the venue feels more
  fundamental: analytics are pure and depend on nothing but schemas, so they're
  the cleanest second step and build momentum. The venue is heavier (mocking,
  integration) and benefits from schemas being locked first.
- **First vertical slice at Step 4**, as early as its dependencies (schemas,
  analytics, venue) allow — the "works in Claude Desktop" moment de-risks the
  whole stack and is worth reaching fast.
- **Tests ship with each step, never deferred.** Unit from Step 1, integration
  from Step 3 (first network I/O), e2e from Step 4 (first installable MCP). There
  is no "testing phase" at the end because deferred testing is testing that
  doesn't happen.

## Per-step closeout ritual

Every step ends the same way, so the next terminal starts clean:

1. **Summary** — 3–6 sentences: what was built, what decisions were made, what (if
   anything) deviated from the plan and why.
2. **Tests** — run `ruff check .`, `mypy --strict src/`, `uv run pytest -m "not
   integration"` (and the integration tests where applicable). Paste the passing
   output into the summary.
3. **Git commit** — one focused commit (or a few) with a clear message. Conventional
   style: `feat(schemas): ...`, `test(venue): ...`, `chore: scaffold project`.
4. **Handoff prompt** — produce the next step's handoff prompt (template below),
   filled in with anything the next step needs to know that isn't already in the
   docs. Paste it into the new terminal to begin the next step.

## Handoff prompt template

```
Read CLAUDE.md, docs/architecture.md, docs/api_spike_findings.md, and
docs/build_plan.md.

Steps 0 through <N> are complete. Summary of what exists:
<2-4 sentence state of the codebase>

You are now doing Step <N+1>: <one-line goal>.

What to build:
<bullet list from the step's "Deliverables">

Constraints / gotchas specific to this step:
<anything from api_spike_findings.md that bites here, plus anything discovered
in prior steps that the next step must respect>

Exit criteria:
<the step's exit criteria>

Follow the per-step closeout ritual in docs/build_plan.md when done. Do not start
Step <N+2>.
```

---

## Step 0 — Scaffolding & toolchain

**Goal:** a clean, lintable, type-checkable empty package with the full directory
tree and toolchain configured.

**Deliverables:**
- `uv init`; `pyproject.toml` with project metadata and `[tool.ruff]`,
  `[tool.black]`, `[tool.mypy]` (strict), `[tool.pytest.ini_options]` (with an
  `integration` marker registered) configured.
- `uv add` runtime deps: `httpx`, `pydantic`, `fastmcp`. Dev deps: `ruff`,
  `black`, `mypy`, `pytest`, `pytest-asyncio`, `respx`, `python-dotenv`.
- Full directory tree per CLAUDE.md, with `__init__.py` in each package dir.
- `CLAUDE.md`, `.gitignore` (`.env`, `__pycache__`, `.venv`, build artifacts),
  `.env.example` (documents `HL_SAMPLE_WALLETS` and any config env vars).
- `git init`, first commit.

**Tests:** one smoke test — `tests/unit/test_smoke.py` asserting `import hlmcp`
succeeds.

**Why its own step:** pure setup, zero logic. Get the foundation right in
isolation so no later step fights the tooling.

**Exit:** `uv run python -c "import hlmcp"` works; `ruff check .` clean;
`mypy --strict src/` clean; smoke test passes; committed.

---

## Step 1 — Schemas + fixtures

**Goal:** the raw `HL*` API schemas, validated against recorded real responses.

**Deliverables:**
- `src/hlmcp/schemas/hl_api.py` — the clearinghouse types (`HLClearinghouseState`,
  `HLMarginSummary`, `HLAssetPosition`, `HLPosition`, `Leverage`, `CumFunding`)
  AND a new `HLL2Book` type for the l2Book response shape
  (`{coin, time, levels: [[bid_levels], [ask_levels]]}` where each level is
  `{px, sz, n}`). All with per-class docstrings and `Field` descriptions.
- `tests/fixtures/` — JSON captured from the spike: a 34-position clearinghouse
  response, l2Book responses at several `nSigFigs` settings, a `perpDexs` response.
- `tests/conftest.py` — fixture loaders.
- `tests/unit/test_schemas.py` — parse every fixture, assert structure.

**Tests:** unit only (no network).

**Why its own step:** schemas are the foundation every layer imports; validating
them against real recorded data is self-contained and needs no network.

**Exit:** all fixtures parse; lint/type clean; committed.

---

## Step 2 — Analytics (pure functions)

**Goal:** the computational core, fully unit-tested offline.

**Deliverables:**
- `src/hlmcp/analytics/aggregation.py` — `choose_aggregation`, the BTC ladder,
  `estimate_bucket_bps`, `L2BookParams` TypedDict.
- `src/hlmcp/analytics/imbalance.py` — `compute_imbalance`, `ImbalanceBand`.
- `src/hlmcp/analytics/utils.py` — `normalize_wallet`, decimal/parse helpers.
- `tests/unit/test_aggregation.py`, `test_imbalance.py`, `test_normalize_wallet.py`.

**Tests:** unit. `compute_imbalance` against recorded l2Book fixtures with
hand-checked expected values; `choose_aggregation` across band sizes;
`normalize_wallet` across the Q2b edge cases (uppercase, no-prefix, malformed).

**Why its own step:** pure functions, no I/O, fully testable offline. Distinct
testing mode (deterministic in/out) from the I/O layers.

**Exit:** all unit tests green; lint/type clean; committed.

---

## Step 3 — Venue adapter + rate limiter + config

**Goal:** the read-only HL REST adapter, with concurrency + rate limiting, tested
both mocked and against live HL.

**Deliverables:**
- `src/hlmcp/config.py` — `HLConfig` dataclass + `load_config()` (env-var
  overrides, dotenv).
- `src/hlmcp/venues/errors.py` — `HLAPIError`.
- `src/hlmcp/venues/hyperliquid.py` — `HyperliquidPublic` (from the reviewed
  adapter: semaphore, `list_dexes` cache, `_validate_dex`,
  `fetch_clearinghouse_state`, `fetch_clearinghouse_states_batch`,
  `fetch_all_dexes_for_user`, plus an `l2Book` fetch method). Token-bucket rate
  limiter wrapping `_post`.
- `tests/venue/test_hyperliquid.py` — respx-mocked: normalization applied,
  unknown-dex raises before network, batch returns exceptions-as-values, 4xx →
  `HLAPIError` with plain-string body, semaphore caps concurrency.
- `tests/integration/test_live_hl.py` (`@pytest.mark.integration`) — real API:
  fetch one wallet's state, `list_dexes` returns ≥1 HIP-3 dex, one `l2Book`
  returns 20 levels/side.

**Tests:** venue (mocked) + integration (live).

**Why its own step:** the I/O boundary. Its tests need HTTP mocking (a distinct
discipline) and this is the first step with real integration tests. Cohesive.

**Gotchas (from spike):** unknown dex → 500; 4xx bodies are plain strings;
normalize addresses before sending; cache `perpDexs` ~5 min.

**Exit:** mocked tests pass; integration tests pass against live HL; lint/type
clean; committed.

---

## Step 4 — First vertical slice: `order_book_imbalance` end-to-end

**Goal:** one tool callable in Claude Desktop, returning real computed data. The
"it works" moment.

**Deliverables:**
- `src/hlmcp/schemas/responses.py` — `FreshnessMeta`, `OrderBookImbalanceResponse`.
- `src/hlmcp/tools/order_book_imbalance.py` — the tool: take coin + bands, pick
  aggregation via `choose_aggregation`, fetch l2Book, compute imbalance, wrap with
  `FreshnessMeta` (report actual bucket width + staleness), return the response.
- `src/hlmcp/server.py` — FastMCP app + registration of this tool.
- `tests/unit/test_order_book_imbalance.py` — tool logic with a mocked venue.
- `tests/integration/` — the tool against live HL.
- Manual e2e: install in Claude Desktop, ask the LLM to call it, verify.

**Tests:** unit + integration + e2e (manual install + invoke).

**Why its own step:** first vertical slice; validates the entire stack; the
first demoable artifact. High de-risking value.

**Exit:** tool callable in Claude Desktop returning real data; tests pass;
committed.

---

## Step 5 — `whale_position_monitor` + `list_hip3_dexes`

**Goal:** the two remaining v0 tools, reusing the proven pattern.

**Deliverables:**
- `src/hlmcp/analytics/positions.py` — `aggregate_positions` (direction split,
  net bias, gross notional), account-level liquidation-buffer derivation, optional
  funding-yield helper.
- `src/hlmcp/tools/whale_position_monitor.py` — fan out across wallets (and
  optionally HIP-3 dexes via `include_hip3`), aggregate, return positions +
  account-level risk (NOT per-position liquidationPx as the headline — see
  findings).
- `src/hlmcp/tools/list_hip3_dexes.py` — surface the `perpDexs` metadata.
- Curated-wallet handling: a `data/curated_whales.json` (provenance documented)
  loaded as the default wallet set.
- Register both in `server.py`. Unit + integration + e2e tests for each.

**Tests:** unit + integration + e2e.

**Why its own step:** reuses the Step-4 pattern but introduces genuinely new
analytics (position aggregation, account-level risk) and HIP-3 fan-out worth
isolating.

**Exit:** all three tools callable in Claude Desktop; tests pass; committed.

---

## Step 6 — Packaging, README, distribution

**Goal:** an installable, documented, tagged v0.1.0.

**Deliverables:**
- Console-script entry point (`hlmcp-server`) in `pyproject.toml`.
- `README.md` — positioning, install (`uv`/`pip`), Claude Desktop config snippet,
  tool catalog, freshness/limits disclosure, the "what this isn't" scope note.
- Build a wheel; install it in a clean venv; smoke-test the entry point.
- `evals/` skeleton — dataset format + a runnable `run_evals.py` stub for the
  tool-output and tool-selection evals (full eval content is post-v0).
- Tag `v0.1.0`.

**Tests:** clean-env install from wheel; entry-point smoke test.

**Why its own step:** distribution is fiddly and benefits from focus; mixing it
into feature work invites half-done packaging.

**Exit:** installable from wheel; README complete; tagged; committed.

---

## What is explicitly NOT in this plan (post-v0)

- WebSocket-backed venue adapter for sub-second freshness.
- Background indexer for whale monitoring.
- Funding-carry tool, queue-position estimator (needs WS), additional
  microstructure tools.
- The full eval dataset (Step 6 ships only the skeleton).
- L4 (Dwellir) integration for individual-order data.
- Per-coin aggregation-ladder calibration / runtime probe.

These are roadmap items. v0 ships 3 tools, fully tested and installable. Resist
pulling them forward; shipping the slice beats half-building the roadmap.