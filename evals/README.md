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

## Headless routing runner — spike findings

Can Claude Code headless mode (`claude -p`, on the subscription) replace hand
runs in Claude Desktop for the tool_selection eval? Measured on 2026-10-29/30,
Claude Code 2.1.312, macOS. Probe script and MCP config:
`data_exploration/headless_routing/`. Raw transcripts:
`tests/fixtures/headless_routing/`.

**[measured]** = observed in a pasted run; **[derived]** = follows from a
measurement by an explicit rule; **[estimate]** = a reasoned guess not yet
validated (same convention as `docs/architecture.md`).

Status: questions 1–2 answered; 3–7 pending.

### 1. Billing — subscription, not API key: **GO**

- **[measured]** No API key in the environment or settings:

  ```text
  $ if [ -n "${ANTHROPIC_API_KEY+x}" ]; then echo SET; else echo unset; fi
  unset
  $ env | cut -d= -f1 | grep -E '^(ANTHROPIC_|CLAUDE_CODE_|AWS_BEARER|CLAUDE_|VERTEX)'
  CLAUDE_CODE_CHILD_SESSION  CLAUDE_CODE_ENTRYPOINT  CLAUDE_CODE_EXECPATH
  CLAUDE_CODE_MESSAGING_SOCKET  CLAUDE_CODE_MESSAGING_TOKEN
  CLAUDE_CODE_SESSION_ATTENDED  CLAUDE_CODE_SESSION_ID  CLAUDE_CODE_SSE_PORT
  CLAUDE_EFFORT  CLAUDE_PID
  ```

  No `apiKeyHelper` / `ANTHROPIC_API_KEY` / `primaryApiKey` in
  `~/.claude/settings.json` or `~/.claude.json`; no managed or project
  settings files exist.
- **[measured]** `claude auth status --json` (identifiers omitted):
  `loggedIn: true, authMethod: "claude.ai", apiProvider: "firstParty",
  subscriptionType: "max"`.
- **[measured]** What `-p` actually used: the stream-json `system/init` event
  reports `"apiKeySource": "none"` (OAuth subscription login), both with the
  inherited environment and under `env -i HOME USER PATH TERM`.
- **[measured]** Overage cannot bill: every transcript carries a
  `rate_limit_event` with `"overageStatus": "rejected",
  "overageDisabledReason": "org_level_disabled", "isUsingOverage": false`.
  Exhausting the plan allowance rate-limits the runner; it does not charge.
- **[measured]** `--bare` is unusable: `claude --help` says its auth is
  "strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and
  keychain are never read)".
- **[derived]** `result.total_cost_usd` (0.0179 for a one-word reply) is
  reported even on subscription auth, so it is a notional API-rate figure, not
  a charge. Use `rate_limit_event` utilization and wall time instead.

### 2. Observability — tool name and full args visible: **GO**

- **[measured]** With `--output-format stream-json --verbose`, each attempted
  call appears in **three** places, all with the prefixed tool name:
  1. an `assistant` event whose `message.content[]` holds a `tool_use` block
     (`name`, `input`, `id`);
  2. a `system` event with `subtype: "permission_denied"` (`tool_name`,
     `tool_use_id`; when the call was denied);
  3. the final `result` event's `permission_denials[]` (`tool_name`,
     `tool_use_id`, `tool_input`).
- **[measured]** Args are verbatim, including nested values (sel-004):

  ```text
  {"name": "mcp__hlmcp__whale_position_monitor",
   "input": {"wallets": ["0xe09726ff25f5001f37b15049f54116cb83d7d0fe"],
             "include_hip3": true}}
  ```

- **[measured]** Tool names carry the MCP prefix `mcp__hlmcp__<tool>` (the
  server key in the MCP config is `hlmcp`); a parser must strip it.
- **[measured]** Event shape gotcha for the parser: `message` is an object on
  `assistant`/`user` events but a **string** on `system/permission_denied`.
- Fixtures (sonnet-5/high, hlmcp tools not pre-approved, one per tool):

  | File | Prompt (case) | `tool_use` |
  |---|---|---|
  | `funding_carry_denied.jsonl` | sel-012 | `funding_carry {"coin": "BTC"}` |
  | `order_book_imbalance_denied.jsonl` | sel-001 | `order_book_imbalance {"coin": "BTC"}` |
  | `whale_position_monitor_denied.jsonl` | sel-003 | `whale_position_monitor {}` |
  | `whale_position_monitor_args_denied.jsonl` | sel-004 | wallets + `include_hip3` (above) |
  | `list_hip3_dexes_denied.jsonl` | sel-005 | `list_hip3_dexes {}` |

  The fixtures are unredacted, as the brief requires. They contain local
  paths (`cwd`, `memory_paths`) and session ids, but no account identifiers
  (checked with a grep for the email, `orgId` and `orgName`).

### 3–7. Pending

Early observation for question 3, not yet a finding: in all 5 fixture runs the
denied call was followed by a text-only answer asking for permission. There
was no retry and no second tool (`num_turns: 2`).
