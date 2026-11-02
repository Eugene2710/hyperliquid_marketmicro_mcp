# Headless routing runner — spike findings

Can Claude Code headless mode (`claude -p`, on a Claude subscription) replace
hand runs in Claude Desktop for the tool_selection eval (`evals/`)? This records
the evidence and the decisions it supports.

- **Environment:** macOS, measured 2026-10-29/31. Question 1 ran on Claude Code
  2.1.312; Claude Code auto-updated during the spike, so questions 2–3 (and
  every fixture) ran on 2.1.314. The runner should record
  `claude_code_version` from the `system/init` event on every row.
- **Scripts:** `data_exploration/headless_routing/` — `probe.py` runs one prompt
  and summarizes the transcript (it generates the hlmcp-only MCP config from
  the repo location); `batch.py` runs dataset cases sequentially with the
  runner's flags and prints observation rows; `scrub.py` removes
  machine-specific fields from recorded transcripts.
- **Fixtures:** `tests/fixtures/headless_routing/` (see "Fixtures" below).

**[measured]** = observed in a recorded run; **[derived]** = follows from a
measurement by an explicit rule; **[estimate]** = a reasoned guess not yet
validated (same convention as `docs/architecture.md`).

Status: questions 1–7 answered. **Verdict: GO** (see the end of question 7).

## 1. Billing — subscription, not API key: **GO**

- **[measured]** `ANTHROPIC_API_KEY` was unset, no other Anthropic or
  cloud-provider auth variables were set, and no settings file defined an
  `apiKeyHelper` or API key.
- **[measured]** `claude auth status --json` reported `authMethod: "claude.ai"`
  (subscription OAuth login), `apiProvider: "firstParty"`.
- **[measured]** What `-p` actually used: the stream-json `system/init` event
  reports `"apiKeySource": "none"` (OAuth subscription login), both with the
  parent shell's environment and under `env -i HOME USER PATH TERM`. This field,
  not `total_cost_usd`, is the billing signal.
- **[measured]** Each transcript carries a `rate_limit_event` with the plan's
  `five_hour` / `seven_day` utilization and an `overageStatus`. In this
  environment overage was disabled, so an exhausted allowance rate-limits the
  runner rather than billing it. Whoever runs the runner should check their own
  `overageStatus` once.
- **[measured]** `--bare` is unusable: `claude --help` says its auth is
  "strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and
  keychain are never read)".
- **[derived]** `result.total_cost_usd` (0.0179 for a one-word reply) is
  reported even on subscription auth, so it is a notional API-rate figure, not
  a charge. Use `rate_limit_event` utilization and wall time instead.
- **[measured]** A `claude -p` launched from inside a Claude Code session
  inherits that session's `CLAUDE_EFFORT` and `CLAUDE_CODE_*` variables. The
  probe launches with `env -i HOME USER PATH TERM` and passes `--model` /
  `--effort` explicitly; unpinned, a probe ran on the parent session's model.

## 2. Observability — tool name and full args visible: **GO**

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

- **[measured]** The harness did not alter any arguments: in all 9 recorded
  calls, `tool_use.input` equals the event's `wire_tool_inputs[<id>]` and
  `message.input_transformations` is `[]`.
- **[measured]** Tool names carry the MCP prefix `mcp__hlmcp__<tool>` (the
  server key in the MCP config is `hlmcp`); a parser must strip it.
- **[measured]** Event shape gotcha for the parser: `message` is an object on
  `assistant`/`user` events but a **string** on `system/permission_denied`.

## 3. No execution — denied but recorded; observed-tools rule

- **[measured]** Without pre-approval, every hlmcp call is denied and nothing
  runs, yet it is still recorded (question 2). That holds under the default
  permission handling, under `--permission-prompts none` and under
  `--permission-mode dontAsk`. All runs were sonnet-5/high.
- **[measured]** After a denial the model answers in text: **0 retries and 0
  switches to another tool in 17 runs** that had at least one denial. The
  runs: sel-001/003/004/005/012 ×1, sel-018 ×3 (+1 under each of the two
  flags), sel-013, sel-008, and sel-011 ×5.
- **[measured]** Multi-tool calls are **parallel in the first assistant
  message**. In sel-018, 5/5 runs called `funding_carry` and
  `order_book_imbalance` as two `tool_use` blocks sharing one `message.id`.
  `num_turns` was 3 only because each denial counts as a turn, so
  `num_turns` is not a call count.
- **[measured]** The denial text differs by flag, which matters for retries:
  - `--permission-prompts none`: "The action was NOT performed; … **do not
    retry it** … continue with the parts of the task that do not require
    approval."
  - `--permission-mode dontAsk`: "You *may* attempt to accomplish this action
    using **other tools** …". This invites a switch to another tool, so it is
    not used.
  - default: "Claude requested permissions to use … but you haven't granted
    it yet."
- **[measured]** Denial has a blind spot: **sequential chains**. On Desktop,
  sel-011 chained `list_hip3_dexes` → `whale_position_monitor`
  (`evals/results/manual-2026-09-09.jsonl`). With all four tools pre-approved
  and actually executed, 1 of 2 headless runs did the same: two different
  `message.id`s, the second issued after reading the first result (fixture
  `chain_executed_sel011.jsonl`, 37.7 s). Denied runs went straight to
  `whale_position_monitor` 5/5. A discovery-first opening under denial would
  stop at `list_hip3_dexes` and score as a misroute.
- **[measured]** Executing is slower: 31.5 s and 37.7 s for sel-011 executed,
  against 6–8 s denied, and it calls live HL. Pre-approving only
  `list_hip3_dexes` works mechanically (sel-005 executed it in 11.7 s), but no
  chain occurred in 3 sel-011 runs, so whether it rescues chains is
  **unmeasured**.
- **[measured]** `--allowedTools` is variadic (`<tools...>`): placed before
  the prompt it swallowed the prompt as a tool name (`Error: Input must be
  provided either through stdin or as a prompt argument`). Pass it as a single
  `--allowedTools=a,b` argument.
- **[derived]** The denial is Claude Code's built-in permission check, not
  anything in this repo. The model's `tool_use` is only a request; the harness
  runs a tool only if it is pre-approved (`--allowedTools` or a settings allow
  rule). Otherwise it never forwards the call to the server and instead sends
  the model a `tool_result` with `is_error: true` carrying the denial text.
  Example: `funding_carry_denied.jsonl` lines 3 (request), 4
  (`system/permission_denied`) and 5 (the harness-written result). That the
  server never received the call rests on timing and the absence of HL data;
  there was no server-side log.

**Decision: deny all hlmcp tools** (`--permission-prompts none`, no
`--allowedTools`). **Observed tools** = the `tool_use` blocks of every assistant
message **up to and including the first message that contains a denied call**,
in call order, with `mcp__hlmcp__` stripped.

- Under deny-all this equals "the first assistant message that calls a tool",
  which captures parallel multi-tool calls (sel-018).
- Ending at the first denial excludes reactions to the denial by
  construction, even though none were seen (0/17). A retry would then be
  invisible to scoring, not counted as routing.
- Unlike a literal "first turn", the rule still holds if a cheap tool is
  later pre-approved: the executed call and the follow-up it triggers are
  both counted.
- **[estimate], low-to-medium confidence:** chain truncation is rare enough
  to accept. Discovery-first openings were 1 in 7 sel-011 runs, and only
  `list_hip3_dexes` → X chains are affected. Question 7 is the check. If
  chain cases disagree with Desktop, switch to
  `--allowedTools=mcp__hlmcp__list_hip3_dexes` (the rule above already
  supports it) rather than executing everything.

No fixture exists for a denied-then-retry transcript, because it never
occurred. A parser test for that case would have to be hand-built, which the
recorded-fixture rule disallows. Flagged for the runner work.

## 4. Isolation — achievable: **GO** (a–d)

Measured on 2026-11-02 (Claude Code 2.1.314, sonnet-5/high). Evidence is of
two kinds: what the `system/init` event says was loaded, and the **first
request's prompt size** (`usage.iterations[0]` input + cache-read +
cache-creation tokens), which shows what actually reached the model. The model
was also asked to list its skills, commands, tools and any CLAUDE.md/memory
text; this self-report agrees with the token counts in every variant.

- **a. hlmcp-only MCP — [measured].** With `--strict-mcp-config
  --mcp-config <file>`, every run's init reports `mcp_servers` = `[hlmcp]`
  (status `connected`) and nothing else.
- **b. Built-in tools disabled — [measured].** With `--tools ""`, init `tools`
  is exactly the 4 `mcp__hlmcp__*` tools, and the self-report lists the same 4.
  No web search or fetch is possible (`server_tool_use` counts are 0).
- **c. No project or user context — [measured]:**
  - Project context: the cwd is an empty temp dir, so there is no CLAUDE.md
    to discover. The auto-memory dir is per-cwd and empty: "No memory files
    were provided."
  - **A user-level `~/.claude/CLAUDE.md` leaks by default.** A temporary
    canary file containing a unique word was quoted back verbatim without
    `--setting-sources`. With `--setting-sources ""` the model answered NONE,
    the word appeared nowhere in the transcript, and the prompt was 164
    tokens smaller (12,475 vs 12,639). **`--setting-sources ""` is
    required.** It also stops user settings such as a default `effortLevel`
    from loading.
  - Skills and slash commands are registered but **not** injected.
    `--setting-sources ""` and `--disable-slash-commands` take init from 33
    skills / 69 commands to 0 / 0, yet the prompt is unchanged (12,513 vs
    12,509 tokens): with no Skill tool there is nothing to list. Both flags
    are kept as protection against a future configuration.
  - **What cannot be removed** without replacing the system prompt
    (`--system-prompt`): Claude Code's base coding-agent prompt (it mentions
    `/help`, `/code-review`, `/fast`), an environment block, the model
    identity, the date, the hlmcp **server instructions** (Desktop sends
    these too), and the **account email** (`# userEmail`). None of these
    describe the hlmcp tools, but the base prompt is not Desktop's.
    **[estimate]** That difference is the main fidelity risk, and
    question 7 measures it.
- **d. Fresh session per prompt — [measured].** One `claude -p` process per
  prompt with `--no-session-persistence`; every transcript has a distinct
  `session_id` (8/8 fixtures).
- **[measured] Side effect:** each run's new temp cwd leaves an empty
  `~/.claude/projects/<cwd-slug>/memory/` folder (29 accumulated during this
  spike; no files, and no `~/.claude.json` entries). **[derived]** The runner
  should reuse **one fixed empty cwd** for every prompt. Sessions stay fresh
  because each prompt is a new process, nothing can be written there (no
  tools that write), and only one folder is created. This is not yet run.
- **[measured] Account email in context:** a model reply can echo it (the
  self-report probes did). `scrub.py` therefore refuses any transcript
  containing an email address. Tested on such a transcript: it raised and
  wrote nothing.

**Isolation flags for the runner:** `env -i HOME USER PATH TERM`, a fixed empty
cwd, `--strict-mcp-config --mcp-config <hlmcp-only> --tools ""
--setting-sources "" --disable-slash-commands --no-session-persistence`.

## 5. Model and effort pinning — model **GO**; effort pinned but not observable

The goal is not to freeze the model forever. Models and Claude Code update.
The goal is that a before/after comparison changes **only** the docstring,
and that every result row records what produced it. Then an update shows up
as a different configuration that needs a new baseline, instead of being
pooled silently with old results.

- **[measured] Model is pinnable and reported.** `--model claude-sonnet-5` is
  echoed verbatim in three places in every transcript: `system/init.model`,
  every assistant `message.model`, and the key of `result.modelUsage`. All
  read `claude-sonnet-5`. Unpinned, a probe ran on the parent session's model,
  so the flag is mandatory.
- **[derived] Use the full model ID, not an alias.** An alias such as `sonnet`
  can resolve to a newer model later. **[estimate]** The full ID is treated as
  a fixed version; whether the model behind one ID can change was not
  verified.
- **[measured] Effort is not recorded in the transcript.** No event carries the
  effort level (`init.per_turn_effort_active: false` is the only effort-named
  field), so a transcript cannot prove which effort ran.
- **[derived] The `--effort` flag is the only source of effort.** The other
  two sources are removed: `env -i` drops `CLAUDE_EFFORT`, and
  `--setting-sources ""` stops the user settings' `effortLevel` (the user
  default in this environment was `xhigh`) from loading. Checking Claude
  Code's debug log for the applied effort was considered and skipped: it would
  not change what the runner can record.
- **Runner rule:** every row records the **requested** model and effort, the
  **reported** model (`message.model`), and `claude_code_version` (from
  `system/init`). Two runs are comparable only if all four match; otherwise
  re-run the baseline first. Claude Code auto-updated from 2.1.312 to 2.1.314
  during this spike, so version drift is real.
- For question 7 (agreement with Desktop), set `--model claude-sonnet-5
  --effort high` to match the Desktop rows in
  `evals/results/manual-2026-10-06.jsonl` (`claude-sonnet-5/high`).

## 6. Cost and time — about 7 s per prompt; usage signal coarse

Ten prompts were run sequentially with the runner's final flags (question 4
isolation set, `--permission-prompts none`, `--model claude-sonnet-5 --effort
high`), via `data_exploration/headless_routing/batch.py`. The cases were
sel-006/007/008/012/013/018/019 (also repeat 1 for question 7) and
sel-001/003/005. Measured 2026-11-02, Claude Code 2.1.314. All 10 had
`is_error: false`.

- **[measured] Time per prompt:**

  | Measure | Value |
  |---|---|
  | Wall time per prompt | min 4.8 s, median 7.0 s, max 8.0 s |
  | Model time (`result.duration_ms`) | median 5.4 s |
  | Startup overhead (Claude Code + hlmcp server launch) | median 1.4 s |
  | 10 prompts, total | 69 s |

- **[derived] Run sizes at ~7 s per prompt:** gate set (≈12 cases) × 3
  repeats ≈ 4 min; full set (≈50 cases) × 3 repeats ≈ 18 min. Runs are
  sequential; parallel processes were not tested.
- **[measured] Usage signal:** each transcript's `rate_limit_event` reported
  only the `seven_day` window, at utilization `0.01` before and after the
  batch. `five_hour` was absent in all 10, whereas earlier runs reported
  both. The figure did not move over 10 prompts, so per-prompt cost cannot
  be read from it; the reported precision appears to be whole percentage
  points.
- **[estimate]** A 3-repeat full run uses a small fraction of the weekly
  allowance. Confirm with `/usage` before and after the first full run.
- **Runner rules:**
  - [derived] Record `rate_limit` (the windows present) on every row.
  - [derived] Abort if `rate_limit_info.status` is not `allowed`. Overage is
    rejected, so a limited run produces errors, not charges, and errors must
    not be scored as routing.
  - [measured] The parser must not assume a window key exists (`five_hour`
    or `seven_day`).
- **[measured] Fixed cwd works** (the untested recommendation from
  question 4). All 10 runs shared one empty directory, which created exactly
  one `~/.claude/projects/` folder (14 → 15). The directory stayed empty, and
  the 10 transcripts have 10 distinct `session_id`s, so each prompt is still a
  fresh session.
- **[measured] Observation rows load via `Observation.from_row`.** `batch.py`
  printed one row per case in the proposed format (below); every row loads,
  and the observed-tools rule gives the expected result on all 8 recorded
  fixtures (parallel calls kept, executed chain kept, no-tool → `[]`).

**Proposed observation row** (one per case per repeat; `observed_tools` /
`observed_args` and `model` are what `Observation.from_row` reads; the rest is
metadata for comparability and failure handling):

```json
{"id": "sel-018",
 "observed_tools": ["funding_carry", "order_book_imbalance"],
 "observed_args": [{"coin": "BTC"}, {"coin": "BTC"}],
 "model": "claude-sonnet-5/high",
 "requested_model": "claude-sonnet-5", "effort": "high",
 "reported_model": "claude-sonnet-5", "claude_code_version": "2.1.314",
 "runner": "claude-code-headless",
 "duration_ms": 5307, "wall_s": 7.0, "is_error": false,
 "rate_limit": {"seven_day": 0.01}}
```

`model` keeps the `<model>/<effort>` form already used by the Desktop results
files, so headless and Desktop rows compare on one field.

## 7. Agreement with Desktop — 7/7 cases agree: **GO**

sel-006/007/008/012/013/018/019 were run 3× each with the runner's flags at
`claude-sonnet-5`/high (repeat 1 is the question 6 batch). Both sides were
scored with the project's grader (`evals/run_evals.py --score`). "Agree" means
the headless **majority result** (PASS/FAIL) equals the Desktop result; which
tool fired is reported but not required to match.

| Case | Headless r1 / r2 / r3 | Majority | Desktop | Desktop source | Agree |
|---|---|---|---|---|---|
| sel-006 | PASS / PASS / PASS | PASS | PASS | manual-2026-10-06, high | ✓ |
| sel-012 | PASS / PASS / PASS | PASS | PASS | manual-2026-10-06, high | ✓ |
| sel-013 | PASS / PASS / PASS | PASS | PASS | manual-2026-10-06, high | ✓ |
| sel-018 | PASS / PASS / PASS | PASS | PASS | manual-2026-10-06, high | ✓ |
| sel-019 | PASS / PASS / PASS | PASS | PASS | manual-2026-10-06, high | ✓ |
| sel-007 | PASS / PASS / PASS | PASS | PASS | manual-2026-09-09, medium | ✓ |
| sel-008 | FAIL / FAIL / FAIL | FAIL | FAIL | manual-2026-09-09, medium | ✓ (different tool) |

- **[measured] 7/7 agree** on the majority result (criterion: ≥ 6/7). On the
  5 cases whose Desktop baseline is at high effort, **5/5**.
- **[measured] Stable:** all 21 runs repeated their case's result exactly;
  there were no flaky cases. Args matched as well. One run of sel-019 added
  `lookback_hours: 24`, which the grader's subset check accepts.
- **[measured] Multi-tool behaviour matches:** sel-018 called both
  `funding_carry` and `order_book_imbalance` in every headless run, as on
  Desktop. sel-013 never also called `order_book_imbalance`, again as on
  Desktop.
- **[measured] sel-008 agrees on the result but not the tool.** Headless fired
  `funding_carry` 3/3, while Desktop fired `order_book_imbalance`. The Desktop
  baseline was medium effort and was recorded before `funding_carry` existed,
  so it is not comparable. `funding_carry` firing is the `mark_px` temptation
  the case's notes predict. That is a docstring/product decision for the
  routing check, not a runner defect.
- **[estimate] Limits:**
  - Only 7 cases, mostly ones Desktop already passes, and each Desktop case
    was observed once.
  - The evidence is that the runner **reproduces Desktop's scores on these
    cases**, not that Claude Code's coding-agent system prompt routes like
    Desktop's in general (question 4c).
  - The gate-set comparison in the routing-check work tests this more
    broadly. Treat any headless/Desktop disagreement found there as a
    fidelity finding first.

### Verdict: GO

| Criterion | Required | Result |
|---|---|---|
| 1. Billing | subscription | ✓ `apiKeySource: "none"`; overage rejected |
| 2. Observability | name + args visible | ✓ verbatim, in three places |
| 4a–d. Isolation | all achievable | ✓ with the flags below; user `CLAUDE.md` excluded by `--setting-sources ""` |
| 7. Agreement | ≥ 6/7 cases | ✓ 7/7 (5/5 on the high-effort baseline) |

### Runner command line

One process per prompt, all from one fixed empty directory:

```bash
env -i HOME="$HOME" USER="$USER" PATH="$PATH" TERM="$TERM" \
claude -p \
  --output-format stream-json --verbose \
  --strict-mcp-config --mcp-config <generated hlmcp-only config> \
  --tools "" \
  --setting-sources "" \
  --disable-slash-commands \
  --permission-prompts none \
  --no-session-persistence \
  --model claude-sonnet-5 --effort <medium|high> \
  "<prompt>"
```

- The MCP config is generated per checkout (`probe.build_mcp_config`): the
  venv's `hlmcp-server` with `PYTHONPATH=<repo>/src`.
- Observed tools follow the question 3 rule. Rows use the question 6 format.
  Abort when `rate_limit_info.status` is not `allowed`.
- If any `--allowedTools` is ever added, pass it as one `--allowedTools=a,b`
  argument; it is variadic and swallows the prompt otherwise.

## Fixtures

Recorded with claude-sonnet-5 at high effort, from an empty temp cwd, with
hlmcp as the only MCP server and no built-in tools.

| File | Prompt (case) | Shape |
|---|---|---|
| `funding_carry_denied.jsonl` | sel-012 | `funding_carry {"coin": "BTC"}`, denied |
| `order_book_imbalance_denied.jsonl` | sel-001 | `order_book_imbalance {"coin": "BTC"}`, denied |
| `whale_position_monitor_denied.jsonl` | sel-003 | `whale_position_monitor {}`, denied |
| `whale_position_monitor_args_denied.jsonl` | sel-004 | wallets + `include_hip3`, denied |
| `list_hip3_dexes_denied.jsonl` | sel-005 | `list_hip3_dexes {}`, denied |
| `multi_tool_parallel_prompts_none.jsonl` | sel-018 | two parallel denied calls, one `message.id`, `--permission-prompts none` |
| `no_tool_sel007.jsonl` | sel-007 | no tool call, `permission_denials: []`, `num_turns: 1` |
| `chain_executed_sel011.jsonl` | sel-011 | tools executed; sequential `list_hip3_dexes` → `whale_position_monitor` (two `message.id`s) |

**Scrubbed, not hand-edited.** The `system/init` event records the recording
machine's paths and its installed Claude Code skills, plugins and agents.
`scrub.py` replaces `cwd`, `original_cwd`, `memory_paths` and
`messaging_socket_path` with `<scrubbed>`, empties `skills`, `slash_commands`,
`plugins` and `agents`, and rewrites any leftover home or temp path. Everything
a parser reads is unchanged: event types, `message.id`, `tool_use`
names/inputs/ids, `permission_denials`, and `init.tools` / `mcp_servers` /
`model` / `apiKeySource` / `claude_code_version`. This was checked against the
pre-scrub copies (8/8 identical on those fields). Session and request ids are
kept; they are random and carry no personal data. Re-run `scrub.py` on any
newly recorded fixture before committing it. It refuses a file containing an
email address, which has to be removed by hand.
