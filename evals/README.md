# evals/

Evaluation harness for `hlmcp`. Grading logic is pure (no I/O, unit-tested
offline in `tests/unit/test_evals_grader.py`) and imported through `grader.py`,
which re-exports one module per concern:

| Module | Concern |
|---|---|
| `observation.py` | `Observation` — what a model did on one prompt (the results-row contract) |
| `output_checks.py` | `tool_output` grading: assertions, `KNOWN_CHECKS`, `expect_error` |
| `selection.py` | `tool_selection` grading: single-tool, multi-tool, negative cases |
| `repeats.py` | per-case stability across repeated runs; model-mixing warnings |

`run_evals.py` owns all I/O — loading datasets, calling tools, printing reports
(tested in `tests/unit/test_run_evals.py`).

It lives outside `src/` deliberately: it is tooling *about* the package, not part
of the shipped package.

## Two eval types

1. **Tool-selection** (`datasets/tool_selection.jsonl`) — given a natural-language
   user prompt, does the model pick the right tool (and the right arguments)?
   This measures whether the tool *docstrings* (the description the LLM sees) are
   good enough for correct routing. One case per line. Run **by hand in Claude
   Desktop** and scored from a recorded results file (see below).

2. **Tool-output** (`datasets/tool_output.jsonl`) — given a concrete tool call
   (`tool` + `args`), does the returned response satisfy a set of assertions about
   its shape and values (e.g. `bands` non-empty, `staleness_ms` within a sane
   band)? This measures whether the tool *computes* the right thing. One case per
   line. Fully automated and **needs no model** — the tools are called directly.

## Dataset format (JSONL, one case per line)

### tool_selection case

```json
{
  "id": "sel-001",
  "prompt": "What's the order-book imbalance on BTC right now?",
  "expected_tool": "order_book_imbalance",
  "expected_args": {"coin": "BTC"},
  "notes": "Plain single-tool routing; args are a soft expectation."
}
```

- `id` (str, unique), `prompt` (str), and **exactly one** of `expected_tool` /
  `expected_tools` are **required** (both, or neither, is a validation error).
- **`expected_tool: null` marks a negative case** — the correct behaviour is to
  call *no* tool. Include some: a tool that fires on everything scores 100% on a
  purely positive dataset.
- **`expected_tools: [..]`** (2+ distinct names) marks a question that needs
  several tools: it passes only if **all** are called. Call order is not scored;
  extra tools are reported ("also called …"), not failed.
- `expected_args` (object) is **optional** — a soft expectation, scored
  separately from tool match, as a **subset** check (extra args the model
  supplies are not penalised). For an `expected_tools` case it is keyed by tool
  name, since tools rarely share a signature:

  ```json
  {"id": "sel-018", "prompt": "Is BTC funding positive right now, and is the order book bid-heavy?",
   "expected_tools": ["funding_carry", "order_book_imbalance"],
   "expected_args": {"funding_carry": {"coin": "BTC"}, "order_book_imbalance": {"coin": "BTC"}},
   "gate": true, "notes": "..."}
  ```
- `gate` (bool) is **optional**. `true` puts the case in the gate set — about a
  dozen cases (a canary per tool, every tool pair in at least one direction,
  2–3 negatives) run by hand on every docstring change and scored with
  `--gate-only`.
- `notes` should say which **axis** (own vocabulary, synonym/slang, indirect,
  argument-bearing, HIP-3) or **tool pair and direction** the case covers, and
  why its expected answer is right. Debatable cases say **DEBATABLE** and why.
- Never renumber or rewrite an existing `id`: results files join on it. Append.

### tool_output case

```json
{
  "id": "out-001",
  "tool": "order_book_imbalance",
  "args": {"coin": "BTC"},
  "assertions": [
    {"path": "bands", "check": "non_empty"},
    {"path": "freshness.staleness_ms", "check": "gte", "value": 0}
  ],
  "notes": "Live-data case: values change, so assert invariants, not exact numbers."
}
```

- `id` (str, unique), `tool` (str), `args` (object), and either `assertions`
  (non-empty list) or `expect_error` are **required**. A case with no assertions
  **fails** rather than vacuously passing — `all([])` is `True`, and a case that
  checks nothing must never report green.
- Each assertion has a dotted `path` into the response, a `check`
  (`non_empty` | `gte` | `lte` | `eq` | `in_range` | `neg_of`), and (for
  comparison checks) a `value`. `KNOWN_CHECKS` in `output_checks.py` is the
  single source of truth for the vocabulary.
- **`neg_of`** is cross-field: `value` is a **second path**, and the check passes
  when `|response[path] + response[value]| <= tolerance` (optional absolute
  `tolerance`, default `1e-12`). Use it for sign invariants on live data, where
  the values move but their relationship must not:

  ```json
  {"path": "carry_annualized_long", "check": "neg_of", "value": "carry_annualized_short"}
  ```
- **`expect_error: "<ExceptionClassName>"`** makes the case pass only if the call
  raises that exact class (by name; a subclass does not match). Such a case has
  no `assertions`. It pins error paths, e.g. an unknown coin raising
  `ValueError` from validation rather than `HLAPIError` from a retried 500:

  ```json
  {"id": "out-011", "tool": "funding_carry", "args": {"coin": "NOTACOIN"}, "expect_error": "ValueError"}
  ```
- Assert **invariants only**, never live exact values.
- Prefer a **band** over a one-sided bound for values that can legitimately sit
  either side of a boundary. `staleness_ms` is deliberately unclamped (see
  `schemas/responses.py`), so `gte 0` flips with clock drift; `in_range` still
  catches a genuine units or epoch bug without flaking.

## Running

```bash
uv run python evals/run_evals.py                       # validate every dataset (no network, no model)
uv run python evals/run_evals.py --type tool_selection # validate one dataset
uv run python evals/run_evals.py --run-output          # execute vs live HL and grade the assertions
uv run python evals/run_evals.py --score RESULTS.jsonl # score a recorded tool_selection run
uv run python evals/run_evals.py --score R1.jsonl R2.jsonl R3.jsonl  # score repeats
uv run python evals/run_evals.py --score RESULTS.jsonl --gate-only   # gate set only
```

**One results file** prints per-case PASS/FAIL and the routing total, as it
always has. **Several files** are scored as repeats of the same dataset: each
case is reported as **stable pass** (passed in every run that observed it),
**flaky** (mixed) or **stable fail** (never passed), followed by the totals. The
target is regression detection — a case moving from 3/3 to 0/3 is the signal —
not accuracy estimation.

Every results row should carry `model` (including effort, e.g.
`claude-sonnet-5/medium`). `--score` warns on stderr about rows with no `model`
and about files recorded on different models, since medium and high runs are
not comparable as repeats.

`--run-output` needs network but no API key: it calls the same `compute_*`
functions `server.py` wraps, so it exercises the real orchestration.

## Recording a tool-selection run

Point Claude Desktop at your **local** build (see the repo README's Claude
Desktop section), type each case's `prompt`, and note which tool fired. Record
one line per case in `results/manual-YYYY-MM-DD.jsonl`:

```json
{"id": "sel-001", "observed_tool": "order_book_imbalance",
 "observed_args": {"coin": "BTC"}, "model": "claude-sonnet-5", "notes": ""}
```

`"observed_tool": null` means no tool was called. Then `--score` that file.
Cases with no observation are listed and excluded from the denominator, so a
partial run reports honestly rather than scoring itself against missing data.

## Headless routing runner

Whether Claude Code headless mode (`claude -p`) can run the tool-selection
eval instead of hand runs in Claude Desktop is being evaluated. Findings so far:
[`docs/headless_routing_spike.md`](../docs/headless_routing_spike.md).
