# evals/

Evaluation harness for `hlmcp`. **v0 ships the skeleton only** — the dataset
format, a handful of seed cases, and a runnable `run_evals.py` that loads and
validates them. Grading/execution against a live model or venue is post-v0
(a roadmap item — see `docs/architecture.md`).

It lives outside `src/` deliberately: it is tooling *about* the package, not part
of the shipped package.

## Two eval types

1. **Tool-selection** (`datasets/tool_selection.jsonl`) — given a natural-language
   user prompt, does the model pick the right tool (and the right arguments)?
   This measures whether the tool *docstrings* (the description the LLM sees) are
   good enough for correct routing. One case per line.

2. **Tool-output** (`datasets/tool_output.jsonl`) — given a concrete tool call
   (`tool` + `args`), does the returned response satisfy a set of assertions about
   its shape and values (e.g. `bands` non-empty, `staleness_ms >= 0`)? This
   measures whether the tool *computes* the right thing. One case per line.

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

- `id` (str, unique), `prompt` (str), `expected_tool` (str, one of the registered
  tool names) are **required**.
- `expected_args` (object) is **optional** — a soft expectation; the tool-selection
  grader may score arg match separately from tool match.

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
  are **required**.
- Each assertion has a dotted `path` into the response, a `check`
  (`non_empty` | `gte` | `lte` | `eq` | `in_range`), and (for comparison checks) a
  `value`. The set of checks is a v0 convention; the grader that interprets them is
  post-v0.

## Running

```bash
uv run python evals/run_evals.py            # validate every dataset, print a report
uv run python evals/run_evals.py --type tool_selection
```

The v0 runner **loads and structurally validates** the datasets and prints a
summary. It does not yet execute tools or call a model — those graders are marked
`TODO(post-v0)` in `run_evals.py`.
