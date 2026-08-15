# evals/

Evaluation harness for `hlmcp`. Grading logic lives in `grader.py` (pure
functions, no I/O, unit-tested offline in `tests/unit/test_evals_grader.py`);
`run_evals.py` owns all I/O — loading datasets, calling tools, printing reports.

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

- `id` (str, unique), `prompt` (str), `expected_tool` are **required**.
- **`expected_tool: null` marks a negative case** — the correct behaviour is to
  call *no* tool. Include some: a tool that fires on everything scores 100% on a
  purely positive dataset.
- `expected_args` (object) is **optional** — a soft expectation, scored
  separately from tool match, as a **subset** check (extra args the model
  supplies are not penalised).

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

- `id` (str, unique), `tool` (str), `args` (object), `assertions` (non-empty list)
  are **required**. A case with no assertions **fails** rather than vacuously
  passing — `all([])` is `True`, and a case that checks nothing must never report
  green.
- Each assertion has a dotted `path` into the response, a `check`
  (`non_empty` | `gte` | `lte` | `eq` | `in_range`), and (for comparison checks) a
  `value`. `KNOWN_CHECKS` in `grader.py` is the single source of truth for the
  vocabulary.
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
```

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
