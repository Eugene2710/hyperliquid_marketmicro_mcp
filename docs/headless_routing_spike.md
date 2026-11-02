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
  the repo location); `scrub.py` removes machine-specific fields from recorded
  transcripts.
- **Fixtures:** `tests/fixtures/headless_routing/` (see "Fixtures" below).

**[measured]** = observed in a recorded run; **[derived]** = follows from a
measurement by an explicit rule; **[estimate]** = a reasoned guess not yet
validated (same convention as `docs/architecture.md`).

Status: questions 1–3 answered; 4–7 pending.

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

## 4–7. Pending

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
newly recorded fixture before committing it.
