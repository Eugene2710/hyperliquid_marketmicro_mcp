"""Runnable skeleton eval runner for hlmcp (v0).

What it does TODAY: loads the JSONL datasets under ``evals/datasets/``,
structurally validates every case against the format documented in
``evals/README.md``, and prints a per-dataset report. It exits non-zero if any
case is malformed, so the format itself is testable and can gate CI.

What it does NOT do yet (``TODO(post-v0)``): actually invoke the tools against a
venue, call a model for tool-selection, or grade assertions. Those graders are
stubbed with explicit ``NotImplementedError``-free no-ops so the skeleton runs
clean. See ``docs/build_plan.md`` — v0 ships the skeleton; the graded eval is a
roadmap item.

Usage::

    uv run python evals/run_evals.py
    uv run python evals/run_evals.py --type tool_selection
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# The three tool names the server registers; an eval that references any other
# name is a dataset bug (kept in sync with hlmcp.server manually — the skeleton
# deliberately does not import the package so it can run without it installed).
REGISTERED_TOOLS: frozenset[str] = frozenset(
    {"order_book_imbalance", "whale_position_monitor", "list_hip3_dexes"}
)

# Assertion checks the tool-output format allows (interpreted by the post-v0
# grader; here we only validate that a case uses a known one).
KNOWN_CHECKS: frozenset[str] = frozenset({"non_empty", "gte", "lte", "eq", "in_range"})

_DATASETS_DIR: Path = Path(__file__).parent / "datasets"


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


def _load_jsonl(path: Path) -> list[tuple[int, dict[str, object]]]:
    """Load a JSONL file into ``(line_number, case)`` pairs, skipping blank lines.

    Args:
        path: Path to the ``.jsonl`` dataset.

    Returns:
        A list of ``(1-based line number, parsed object)`` tuples.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If a non-blank line is not a JSON object.
    """
    cases: list[tuple[int, dict[str, object]]] = []
    text: str = path.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parsed: object = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError(f"{path.name}:{lineno}: expected a JSON object, got {type(parsed)}")
        cases.append((lineno, parsed))
    return cases


def _validate_tool_selection(cases: list[tuple[int, dict[str, object]]]) -> list[str]:
    """Validate tool-selection cases; return one error string per malformed case.

    Required fields: ``id`` (str), ``prompt`` (str), ``expected_tool`` (a
    registered tool name). ``expected_args`` is optional and, if present, must be
    an object.

    Args:
        cases: ``(line number, case)`` pairs from the dataset.

    Returns:
        A list of error messages (empty if every case is valid).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
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
        expected_tool = case.get("expected_tool")
        if expected_tool not in REGISTERED_TOOLS:
            errors.append(f"{case_id}: 'expected_tool' {expected_tool!r} is not a registered tool")
        if "expected_args" in case and not isinstance(case["expected_args"], dict):
            errors.append(f"{case_id}: 'expected_args' must be an object when present")
    return errors


def _validate_tool_output(cases: list[tuple[int, dict[str, object]]]) -> list[str]:
    """Validate tool-output cases; return one error string per malformed case.

    Required fields: ``id`` (str), ``tool`` (a registered tool name), ``args``
    (object), ``assertions`` (non-empty list). Each assertion must be an object
    with a string ``path`` and a ``check`` drawn from :data:`KNOWN_CHECKS`.

    Args:
        cases: ``(line number, case)`` pairs from the dataset.

    Returns:
        A list of error messages (empty if every case is valid).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    for lineno, case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"line {lineno}: missing/empty string 'id'")
            continue
        if case_id in seen_ids:
            errors.append(f"line {lineno}: duplicate id {case_id!r}")
        seen_ids.add(case_id)
        if case.get("tool") not in REGISTERED_TOOLS:
            errors.append(f"{case_id}: 'tool' {case.get('tool')!r} is not a registered tool")
        if not isinstance(case.get("args"), dict):
            errors.append(f"{case_id}: 'args' must be an object")
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
    return errors


def validate_dataset(dataset: str) -> ValidationResult:
    """Load and structurally validate one named dataset.

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


def run(datasets: list[str]) -> int:
    """Validate the requested datasets, print a report, and return an exit code.

    TODO(post-v0): after validation, execute the tool-output cases against a
    (mocked or live) venue and grade their assertions, and run the tool-selection
    cases through a model to score routing. For v0 this only validates format.

    Args:
        datasets: The dataset names to validate.

    Returns:
        ``0`` if every case in every dataset is structurally valid, else ``1``.
    """
    print("hlmcp eval skeleton — structural validation only (grading is post-v0)\n")
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
    print("All cases structurally valid. (No tools were executed — see TODO(post-v0).)")
    return 0


def main() -> int:
    """CLI entry point: parse ``--type`` and run validation.

    Returns:
        The process exit code from :func:`run`.
    """
    parser = argparse.ArgumentParser(description="Validate hlmcp eval datasets (v0 skeleton).")
    parser.add_argument(
        "--type",
        choices=["tool_selection", "tool_output"],
        help="Validate only this dataset (default: both).",
    )
    args = parser.parse_args()
    datasets: list[str] = [args.type] if args.type else ["tool_selection", "tool_output"]
    return run(datasets)


if __name__ == "__main__":
    sys.exit(main())
