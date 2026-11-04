"""Eval runner for hlmcp — validates datasets, grades tool output, scores routing.

Three modes, in increasing cost:

``--validate`` (default)
    Structurally validate the JSONL datasets. No imports of the package, no
    network, no model. Cheap enough to gate every commit.

``--run-output``
    Execute each ``tool_output`` case against **live Hyperliquid** and grade its
    assertions. Needs network. **Costs nothing** — the tools are called
    directly, so no LLM is involved anywhere in this path.

``--score <results.jsonl> [<results.jsonl> ...]``
    Score ``tool_selection`` runs: join hand-recorded (or headless-recorded)
    observations to the cases and report routing accuracy. One file reports
    per-case pass/fail; several files are treated as repeats of the same
    dataset and report per-case stable pass / flaky / stable fail.
    ``--gate-only`` restricts scoring to the cases marked ``"gate": true``.
    No network.

The grading logic itself lives in :mod:`evals.grader` — pure functions, unit
tested offline. This module owns only I/O: reading datasets, calling tools,
printing reports, and choosing an exit code.

**Note on the v0 skeleton's design:** it deliberately avoided importing
``hlmcp`` so it could run uninstalled, and hardcoded the tool names to match.
Grading requires the package, so that constraint is dropped — and the tool names
are now read from the live FastMCP app (:func:`registered_tools`) rather than a
hand-maintained constant, so the two can no longer drift.

Usage::

    uv run python evals/run_evals.py
    uv run python evals/run_evals.py --type tool_selection
    uv run python evals/run_evals.py --run-output
    uv run python evals/run_evals.py --score evals/results/manual-2026-08-14.jsonl
    uv run python evals/run_evals.py --score r1.jsonl r2.jsonl r3.jsonl --gate-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

# Run either as a script (`python evals/run_evals.py`, the invocation the READMEs
# document) or as a module (`python -m evals.run_evals`). Direct execution puts
# `evals/` on sys.path rather than the repo root, so `evals.grader` would not
# resolve; add the root ourselves in that case only.
if __package__ in (None, ""):  # pragma: no cover - import-path bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel  # noqa: E402 - must follow the sys.path bootstrap

from evals.grader import (  # noqa: E402 - must follow the sys.path bootstrap
    KNOWN_CHECKS,
    Observation,
    OutputCaseResult,
    SelectionCaseResult,
    expected_tools_of,
    grade_output_case,
    grade_selection_case,
    model_warnings,
    summarize_repeats,
)
from hlmcp.config import load_config  # noqa: E402
from hlmcp.server import mcp  # noqa: E402
from hlmcp.tools.funding_carry import (  # noqa: E402
    DEFAULT_LOOKBACK_HOURS,
    compute_funding_carry,
)
from hlmcp.tools.list_hip3_dexes import compute_list_hip3_dexes  # noqa: E402
from hlmcp.tools.order_book_imbalance import (  # noqa: E402
    DEFAULT_BANDS_BPS,
    compute_order_book_imbalance,
)
from hlmcp.tools.whale_position_monitor import (  # noqa: E402
    compute_whale_positions,
    load_curated_whales,
)
from hlmcp.venues.hyperliquid import HyperliquidPublic  # noqa: E402

_DATASETS_DIR: Path = Path(__file__).parent / "datasets"


@cache
def registered_tools() -> frozenset[str]:
    """Tool names the server actually registers, read from the live FastMCP app.

    Mechanism: takes nothing, asks the FastMCP app for its tool list, returns
    the names as a frozen set.

    Derived rather than hardcoded so a tool renamed in ``server.py`` immediately
    invalidates any dataset case still naming the old one, instead of the two
    drifting apart silently. Cached because it is asked for once per case.

    Returns:
        The registered tool names.
    """
    return frozenset(tool.name for tool in asyncio.run(mcp.list_tools()))


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of structurally validating one dataset.

    Attributes:
        dataset: The dataset name (``tool_selection`` | ``tool_output``).
        n_cases: How many cases were loaded.
        errors: One human-readable message per malformed case (empty = all valid).
    """

    dataset: str
    n_cases: int
    errors: list[str]


def _load_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    """Load a JSONL file into ``(line_number, case)`` pairs, skipping blank lines.

    Mechanism: takes a path, parses each non-blank line as JSON, returns them
    paired with their 1-based line numbers.

    Args:
        path: Path to the ``.jsonl`` dataset.

    Returns:
        A list of ``(1-based line number, parsed object)`` tuples.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If a non-blank line is not a JSON object.
    """
    cases: list[tuple[int, dict[str, Any]]] = []
    text: str = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parsed: object = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError(f"{path.name}:{lineno}: expected a JSON object, got {type(parsed)}")
        cases.append((lineno, parsed))
    return cases


def _validate_tool_selection(cases: list[tuple[int, dict[str, Any]]]) -> list[str]:
    """Validate tool-selection cases; return one error string per malformed case.

    Mechanism: takes the loaded cases, checks each against the documented
    schema, returns the problems found.

    Required: ``id`` (str), ``prompt`` (str), and exactly one of
    ``expected_tool`` (a registered tool name **or ``null``** — ``null`` marks
    a *negative* case, where calling no tool is the correct behaviour) or
    ``expected_tools`` (2+ distinct registered names, all of which must be
    called). ``expected_args`` is optional and, if present, must be an object;
    for an ``expected_tools`` case it is keyed by tool name, each value an
    object. ``gate`` is optional and, if present, must be a bool.

    Args:
        cases: ``(line number, case)`` pairs from the dataset.

    Returns:
        A list of error messages (empty if every case is valid).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    known = registered_tools()
    for lineno, case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"line {lineno}: missing/empty string 'id'")
            continue
        if case_id in seen_ids:
            errors.append(f"line {lineno}: duplicate id {case_id!r}")
        seen_ids.add(case_id)
        if not isinstance(case.get("prompt"), str) or not case["prompt"]:
            errors.append(f"{case_id}: missing/empty string 'prompt'")
        try:
            expected_tools = expected_tools_of(case)
        except ValueError as exc:
            errors.append(f"{case_id}: {exc}")
            expected_tools = ()
        for tool in expected_tools:
            if tool not in known:
                errors.append(f"{case_id}: expected tool {tool!r} is not a registered tool")
        if "expected_args" in case:
            expected_args = case["expected_args"]
            if not isinstance(expected_args, dict):
                errors.append(f"{case_id}: 'expected_args' must be an object when present")
            elif len(expected_tools) > 1:
                for tool, per_tool in expected_args.items():
                    if tool not in expected_tools or not isinstance(per_tool, dict):
                        errors.append(
                            f"{case_id}: 'expected_args' of an expected_tools case maps each "
                            f"expected tool to an object; bad entry {tool!r}"
                        )
        if "gate" in case and not isinstance(case["gate"], bool):
            errors.append(f"{case_id}: 'gate' must be true or false, got {case['gate']!r}")
    return errors


def _validate_tool_output(cases: list[tuple[int, dict[str, Any]]]) -> list[str]:
    """Validate tool-output cases; return one error string per malformed case.

    Mechanism: takes the loaded cases, checks each against the documented
    schema, returns the problems found.

    Required: ``id`` (str), ``tool`` (a registered tool name), ``args``
    (object), and either ``assertions`` (non-empty list) or ``expect_error``
    (an exception class name; the case then has no assertions, since a raising
    call returns nothing to assert on). Each assertion must be an object with a
    string ``path`` and a ``check`` drawn from
    :data:`~evals.grader.KNOWN_CHECKS`; a ``neg_of`` assertion's ``value`` must
    be a path string, and its optional ``tolerance`` a non-negative number.

    Args:
        cases: ``(line number, case)`` pairs from the dataset.

    Returns:
        A list of error messages (empty if every case is valid).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    known = registered_tools()
    for lineno, case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"line {lineno}: missing/empty string 'id'")
            continue
        if case_id in seen_ids:
            errors.append(f"line {lineno}: duplicate id {case_id!r}")
        seen_ids.add(case_id)
        if case.get("tool") not in known:
            errors.append(f"{case_id}: 'tool' {case.get('tool')!r} is not a registered tool")
        if not isinstance(case.get("args"), dict):
            errors.append(f"{case_id}: 'args' must be an object")
        if "expect_error" in case:
            expect_error = case["expect_error"]
            if not isinstance(expect_error, str) or not expect_error:
                errors.append(f"{case_id}: 'expect_error' must be an exception class name")
            if "assertions" in case:
                errors.append(f"{case_id}: an expect_error case takes no 'assertions'")
            continue
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            errors.append(f"{case_id}: 'assertions' must be a non-empty list")
            continue
        for i, assertion in enumerate(assertions):
            if not isinstance(assertion, dict):
                errors.append(f"{case_id}: assertion {i} must be an object")
                continue
            if not isinstance(assertion.get("path"), str):
                errors.append(f"{case_id}: assertion {i} missing string 'path'")
            if assertion.get("check") not in KNOWN_CHECKS:
                errors.append(
                    f"{case_id}: assertion {i} has unknown check {assertion.get('check')!r}"
                )
            if assertion.get("check") == "neg_of":
                if not isinstance(assertion.get("value"), str):
                    errors.append(f"{case_id}: assertion {i} neg_of 'value' must be a path")
                tolerance = assertion.get("tolerance", 0)
                if (
                    isinstance(tolerance, bool)
                    or not isinstance(tolerance, (int, float))
                    or tolerance < 0
                ):
                    errors.append(
                        f"{case_id}: assertion {i} 'tolerance' must be a non-negative number"
                    )
    return errors


def validate_dataset(dataset: str) -> ValidationResult:
    """Load and structurally validate one named dataset.

    Mechanism: takes a dataset name, loads its JSONL and runs the matching
    validator, returns the case count plus any errors.

    Args:
        dataset: ``tool_selection`` or ``tool_output``.

    Returns:
        A :class:`ValidationResult` with the case count and any errors.

    Raises:
        ValueError: If ``dataset`` is not a known dataset name.
        FileNotFoundError: If the dataset file is missing.
    """
    validators = {
        "tool_selection": _validate_tool_selection,
        "tool_output": _validate_tool_output,
    }
    if dataset not in validators:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {sorted(validators)}")
    cases = _load_jsonl(_DATASETS_DIR / f"{dataset}.jsonl")
    return ValidationResult(dataset=dataset, n_cases=len(cases), errors=validators[dataset](cases))


async def _call_tool(venue: HyperliquidPublic, tool: str, args: dict[str, Any]) -> BaseModel:
    """Dispatch one eval case to the tool it names.

    Mechanism: takes the shared venue, a tool name and its args, calls the
    matching ``compute_*`` orchestrator, returns its response model.

    Calls the same ``compute_*`` functions ``server.py`` wraps, so an eval
    exercises the real orchestration rather than a parallel reimplementation.
    Defaults mirror the server's thin wrappers (notably the curated whale set
    when ``wallets`` is omitted).

    Args:
        venue: The shared read-only adapter.
        tool: A registered tool name.
        args: The case's ``args`` object.

    Returns:
        The tool's response model.

    Raises:
        ValueError: If ``tool`` is not a known tool name.
    """
    if tool == "order_book_imbalance":
        bands: Any = args.get("bands_bps") or DEFAULT_BANDS_BPS
        return await compute_order_book_imbalance(venue, coin=str(args["coin"]), bands_bps=bands)
    if tool == "whale_position_monitor":
        wallets: Any = args.get("wallets") or load_curated_whales()
        return await compute_whale_positions(
            venue, wallets, include_hip3=bool(args.get("include_hip3", False))
        )
    if tool == "list_hip3_dexes":
        return await compute_list_hip3_dexes(venue)
    if tool == "funding_carry":
        hours: Any = args.get("lookback_hours") or DEFAULT_LOOKBACK_HOURS
        return await compute_funding_carry(venue, str(args["coin"]), int(hours))
    raise ValueError(f"no dispatch for tool {tool!r}")


async def _run_output_cases(cases: list[dict[str, Any]]) -> list[OutputCaseResult]:
    """Execute every tool-output case against live HL and grade it.

    Mechanism: takes the validated cases, opens one shared venue, calls each
    case's tool and grades its assertions, returns the per-case results.

    Cases run sequentially against one shared adapter so the rate limiter and
    ``perpDexs`` cache are shared, exactly as in the running server. A tool that
    raises is captured as that case's failure rather than aborting the run — one
    broken tool should not hide the verdict on the others.

    Args:
        cases: The tool-output dataset cases.

    Returns:
        One :class:`~evals.grader.OutputCaseResult` per case, in dataset order.
    """
    results: list[OutputCaseResult] = []
    async with HyperliquidPublic(load_config()) as venue:
        for case in cases:
            tool = str(case.get("tool", ""))
            args = case.get("args") if isinstance(case.get("args"), dict) else {}
            try:
                response = await _call_tool(venue, tool, dict(args or {}))
            except Exception as exc:  # noqa: BLE001 - captured as the case's result
                name = type(exc).__name__
                results.append(
                    grade_output_case(case, None, error=f"{name}: {exc}", error_type=name)
                )
                continue
            results.append(grade_output_case(case, response.model_dump()))
    return results


def _load_observations(path: Path) -> dict[str, Observation]:
    """Load hand-recorded (or API-recorded) routing observations, keyed by case id.

    Mechanism: takes a results-file path, parses each line into an
    :class:`~evals.grader.Observation`, returns them keyed by case id.

    Args:
        path: A JSONL results file, one observation per line.

    Returns:
        Observations keyed by ``id``.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If a line lacks a usable ``id``.
    """
    observations: dict[str, Observation] = {}
    for lineno, row in _load_jsonl(path):
        try:
            observation = Observation.from_row(row)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{lineno}: {exc}") from exc
        observations[observation.case_id] = observation
    return observations


def report_output(results: list[OutputCaseResult]) -> int:
    """Print a tool-output report and return the process exit code.

    Mechanism: takes the graded cases, prints per-assertion detail for each,
    returns 0 if all passed else 1.

    Args:
        results: Graded tool-output cases.

    Returns:
        ``0`` if every case passed, else ``1``.
    """
    print("tool_output — executed against live Hyperliquid (no model involved)\n")
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        print(f"[{mark}] {result.case_id} ({result.tool})")
        if result.error:
            # A passing case with an error is an expect_error case that raised
            # the expected class: shown as a fact, not as a problem.
            print(f"    {'ok' if result.passed else '!'} {result.error}")
        for assertion in result.assertions:
            sub = "ok " if assertion.passed else "FAIL"
            print(f"    {sub} {assertion.path} {assertion.check}: {assertion.detail}")
    passed = sum(1 for r in results if r.passed)
    print(f"\n{passed}/{len(results)} cases passed.")
    return 0 if passed == len(results) else 1


def report_selection(results: list[SelectionCaseResult], unobserved: list[str]) -> int:
    """Print a routing report and return the process exit code.

    Mechanism: takes the graded cases plus any cases with no observation,
    prints per-case verdicts and the aggregate, returns 0 if all routed
    correctly else 1.

    Routing accuracy and argument accuracy are reported separately: per
    ``evals/README.md`` the expected args are a soft expectation, so a wrong arg
    on the right tool is a much weaker signal than a wrong tool. Unobserved
    cases are listed but excluded from the denominator, so a partial manual run
    reports honestly instead of silently scoring itself against missing data.

    Args:
        results: Graded selection cases.
        unobserved: Case ids present in the dataset with no observation.

    Returns:
        ``0`` if every observed case routed correctly, else ``1``.
    """
    print("tool_selection — routing accuracy\n")
    for result in results:
        mark = "PASS" if result.tool_ok else "FAIL"
        print(f"[{mark}] {result.case_id}: {'; '.join(result.detail)}")

    graded = len(results)
    routed = sum(1 for r in results if r.tool_ok)
    # Multi-tool turns are surfaced as a fact, not a demerit. Extra calls may be
    # necessary sequencing (discover dex names, then query them) or unnecessary
    # hedging, and nothing here can tell which. This points at the cases; the
    # notes carry the judgment. It exists so a clean routing percentage can never
    # quietly hide that some cases behaved differently.
    multi = [r for r in results if len(r.observed_tools) > 1]
    with_args = [r for r in results if r.args_ok is not None]
    args_ok = sum(1 for r in with_args if r.args_ok)

    print(f"\nrouting: {routed}/{graded} correct" + (f" ({routed / graded:.0%})" if graded else ""))
    if multi:
        ids = ", ".join(r.case_id for r in multi)
        print(f"multi:   {len(multi)}/{graded} used more than one tool ({ids}) — see notes")
    if with_args:
        print(f"args:    {args_ok}/{len(with_args)} matched (soft expectation)")
    if unobserved:
        print(f"\n{len(unobserved)} case(s) not observed, excluded from the score:")
        for case_id in unobserved:
            print(f"    - {case_id}")
    if graded and routed == graded:
        print("\nAll observed cases routed correctly.")
    return 0 if graded and routed == graded else 1


def report_repeats(runs: list[list[SelectionCaseResult]], unobserved: list[str]) -> int:
    """Print a per-case stability report over repeated runs; return the exit code.

    Mechanism: takes one list of graded cases per results file plus the cases
    no file observed, folds them with :func:`~evals.grader.summarize_repeats`,
    prints each case's verdict and the totals, returns 0 only if every
    observed case passed in every run.

    Args:
        runs: Graded selection cases, one list per results file.
        unobserved: Case ids no results file observed.

    Returns:
        ``0`` if every observed case is a stable pass, else ``1``.
    """
    print(f"tool_selection — routing stability over {len(runs)} runs\n")
    summaries = summarize_repeats(runs)
    labels: dict[str, str] = {
        "stable pass": "STABLE PASS",
        "flaky": "FLAKY",
        "stable fail": "STABLE FAIL",
    }
    for summary in summaries:
        line = (
            f"[{labels[summary.stability]}] {summary.case_id}: {summary.n_passed}/{summary.n_runs}"
        )
        # Distinct failure details only: three identical misroutes say it once.
        failures = list(dict.fromkeys(summary.failures))
        print(line + (f" — {' | '.join(failures)}" if failures else ""))

    counts: dict[str, int] = {s: 0 for s in labels}
    for summary in summaries:
        counts[summary.stability] += 1
    print(
        f"\nstable pass: {counts['stable pass']}, flaky: {counts['flaky']}, "
        f"stable fail: {counts['stable fail']} (of {len(summaries)} observed cases)"
    )
    if unobserved:
        print(f"\n{len(unobserved)} case(s) not observed in any run, excluded from the score:")
        for case_id in unobserved:
            print(f"    - {case_id}")
    all_stable = bool(summaries) and counts["stable pass"] == len(summaries)
    if all_stable:
        print("\nAll observed cases routed correctly in every run.")
    return 0 if all_stable else 1


def score_results(
    paths: list[Path], cases: list[dict[str, Any]], *, gate_only: bool = False
) -> int:
    """Score one or more results files against the selection cases; return the exit code.

    Mechanism: takes results-file paths and the dataset cases, filters to the
    gate set if asked, grades each file's observations, warns on stderr about
    missing or mixed ``model`` values, then prints the single-run report for
    one file or the stability report for several.

    A single file goes through :func:`report_selection` unchanged, so its stdout
    is exactly what it was before repeats existed. The model warnings go to
    stderr for the same reason.

    Args:
        paths: Results files; several are scored as repeats of one dataset.
        cases: The ``tool_selection`` dataset cases, in dataset order.
        gate_only: Score only the cases marked ``"gate": true``.

    Returns:
        The report's exit code.

    Raises:
        FileNotFoundError: If a results file does not exist.
        ValueError: If a results row lacks a usable ``id``.
    """
    if gate_only:
        cases = [case for case in cases if case.get("gate") is True]
    per_file: dict[str, dict[str, Observation]] = {
        str(path): _load_observations(path) for path in paths
    }
    for warning in model_warnings({name: list(obs.values()) for name, obs in per_file.items()}):
        print(f"warning: {warning}", file=sys.stderr)

    runs: list[list[SelectionCaseResult]] = []
    observed_ids: set[str] = set()
    for observations in per_file.values():
        graded: list[SelectionCaseResult] = []
        for case in cases:
            observation = observations.get(str(case.get("id", "")))
            if observation is not None:
                graded.append(grade_selection_case(case, observation))
                observed_ids.add(observation.case_id)
        runs.append(graded)
    unobserved = [str(c.get("id", "")) for c in cases if str(c.get("id", "")) not in observed_ids]

    if len(runs) == 1:
        return report_selection(runs[0], unobserved)
    return report_repeats(runs, unobserved)


def run_validation(datasets: list[str]) -> int:
    """Validate the requested datasets, print a report, and return an exit code.

    Mechanism: takes dataset names, structurally validates each, prints the
    per-dataset result, returns 0 when all are well-formed.

    Args:
        datasets: The dataset names to validate.

    Returns:
        ``0`` if every case in every dataset is structurally valid, else ``1``.
    """
    print("hlmcp evals — structural validation\n")
    total_errors = 0
    for dataset in datasets:
        result = validate_dataset(dataset)
        status = "OK" if not result.errors else f"{len(result.errors)} ERROR(S)"
        print(f"[{status}] {dataset}: {result.n_cases} case(s)")
        for err in result.errors:
            print(f"    - {err}")
        total_errors += len(result.errors)
    print()
    if total_errors:
        print(f"FAILED: {total_errors} malformed case(s).")
        return 1
    print("All cases structurally valid.")
    return 0


def main() -> int:
    """CLI entry point.

    Mechanism: takes the command line, dispatches to validation, live tool
    grading, or routing scoring, returns that mode's exit code.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description="Validate, run, and score hlmcp evals.")
    parser.add_argument(
        "--type",
        choices=["tool_selection", "tool_output"],
        help="Validate only this dataset (default: both).",
    )
    parser.add_argument(
        "--run-output",
        action="store_true",
        help="Execute tool_output cases against live HL and grade them (no model, no cost).",
    )
    parser.add_argument(
        "--score",
        metavar="RESULTS.jsonl",
        nargs="+",
        help="Score tool_selection run(s) from recorded results file(s); several files are "
        "scored as repeats (stable pass / flaky / stable fail per case).",
    )
    parser.add_argument(
        "--gate-only",
        action="store_true",
        help='With --score: score only the cases marked "gate": true.',
    )
    args = parser.parse_args()
    if args.gate_only and not args.score:
        parser.error("--gate-only requires --score")

    if args.run_output:
        validation = validate_dataset("tool_output")
        if validation.errors:
            print("Refusing to run: tool_output dataset is malformed.")
            for err in validation.errors:
                print(f"    - {err}")
            return 1
        cases = [case for _, case in _load_jsonl(_DATASETS_DIR / "tool_output.jsonl")]
        return report_output(asyncio.run(_run_output_cases(cases)))

    if args.score:
        cases = [case for _, case in _load_jsonl(_DATASETS_DIR / "tool_selection.jsonl")]
        return score_results([Path(p) for p in args.score], cases, gate_only=args.gate_only)

    datasets: list[str] = [args.type] if args.type else ["tool_selection", "tool_output"]
    return run_validation(datasets)


if __name__ == "__main__":
    sys.exit(main())
