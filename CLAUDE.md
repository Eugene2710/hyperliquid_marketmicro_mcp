# hlmcp — Hyperliquid Microstructure & Execution MCP

> Read this file first, every session. Then read `docs/architecture.md` for the
> design and `docs/build_plan.md` for where we are in the build. If you are
> starting a build step, the handoff prompt you were given names the step.

## What this is

An open-source MCP server exposing **market microstructure and execution
analytics** on Hyperliquid — native HL perps *and* HIP-3 deployments — as tools
that LLM agents and AI coding assistants (Claude Desktop, Cursor, Claude Code,
LangGraph, etc.) can call. Not a data-access wrapper (those exist); the value is
in *computed* signals: depth-weighted order-book imbalance, whale position
monitoring, account-level liquidation risk, funding carry.

Read-only. No order placement, no signing, no exchange endpoint — that is a
separate adapter with its own auth surface and threat model, explicitly out of
scope for v0.

## Toolchain — non-negotiable

- **Package manager: `uv`.** NEVER use `pip` directly. Use `uv add <pkg>`,
  `uv run <cmd>`, `uv sync`. If you catch yourself typing `pip`, stop.
- **Lint: `ruff check .`** — must pass clean before any commit.
- **Format: `ruff format .`** then **`black .`** (configured to agree).
- **Type-check: `mypy --strict src/`** — must pass clean before any commit.
- **Test: `uv run pytest`** — unit tests must pass before any commit;
  integration tests (marked `@pytest.mark.integration`) are opt-in and hit the
  live HL API.

A commit is not "done" until `ruff check .`, `mypy --strict src/`, and
`uv run pytest -m "not integration"` are all green.

- **Broken venv recovery:** if `import hlmcp` fails (e.g. pytest can't collect
  with `ModuleNotFoundError: No module named 'hlmcp'`) while `mypy --strict src/`
  still passes, the `.venv` is in a half-state — the editable install is
  registered but its `.pth` isn't on `sys.path`. Fix: `rm -rf .venv && uv sync`.
  If a *fresh* venv still can't import, it's a real `pyproject.toml`/layout bug,
  not corruption — stop rebuilding and look there.

## Code conventions

- **Type hints are mandatory** on every function signature, every class
  attribute, and every non-trivial local variable. This is a hard requirement,
  not a preference.
- **Every function and class has a docstring** explaining what it does, its
  args, what it returns, and what it raises. Important module-level variables
  get an explanatory comment or docstring too.
- Pydantic v2 syntax (`model_validate`, not `.parse_obj`; `model_dump`, not
  `.dict`).
- Async-first for anything touching I/O; **pure functions** (no I/O, no async)
  for everything in `analytics/`.
- One MCP tool per file under `src/hlmcp/tools/`; register them in `server.py`.
- Empirical API constraints are documented in `docs/api_spike_findings.md`.
  Respect those findings; do not re-derive them. If you think a finding is
  wrong, flag it rather than silently coding against a different assumption.

## User preferences (the human you are working with)

- Evidence-based responses with explicit sourcing. Show the reasoning.
- Minimal, targeted changes over wholesale rewrites. Don't refactor things you
  weren't asked to touch.
- Type hints in all Python code — already stated above, restated because it
  matters.
- Concise. No filler preamble.
- When unsure about a design decision, SAY SO explicitly rather than projecting
  false confidence. Decisions the human and I were unsure about are flagged in
  `docs/architecture.md` under "Open questions / low-confidence decisions."

## Where things live

```
src/hlmcp/
  config.py              # constants, defaults, env-var loading
  server.py              # FastMCP entry point + tool registration
  schemas/
    hl_api.py            # raw HL API response shapes (HL* prefixed types)
    responses.py         # user-facing tool response schemas (FreshnessMeta, ...)
  venues/
    base.py              # Venue protocol (added when a 2nd venue appears)
    hyperliquid.py       # HyperliquidPublic adapter (read-only REST)
    errors.py            # HLAPIError
  analytics/             # PURE functions only — no I/O, no async
    aggregation.py       # choose_aggregation, l2Book bucket sizing
    imbalance.py         # compute_imbalance
    positions.py         # whale position aggregation, liquidation buffer
    utils.py             # normalize_wallet, decimal helpers
  tools/                 # one MCP tool per file
    order_book_imbalance.py
    whale_position_monitor.py
    list_hip3_dexes.py
tests/
  conftest.py            # shared fixtures
  fixtures/              # recorded real API responses (from the spike)
  unit/                  # pure, mocked — no network
  venue/                 # respx-mocked HTTP tests of the venue layer
  integration/           # @pytest.mark.integration — hits live HL
docs/                    # architecture, spike findings, build plan
data_exploration/        # the spike scripts; reference only, not imported
evals/                   # eval harness (outside src/ deliberately)
```

## Build workflow (per step)

We build one step per fresh terminal to manage context. Each step ends with:
a written summary, passing tests, a git commit, and a handoff prompt for the
next step. The full sequence and per-step handoff prompts are in
`docs/build_plan.md`. Do not jump ahead of the current step.