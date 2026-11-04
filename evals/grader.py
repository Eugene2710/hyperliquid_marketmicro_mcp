"""Pure grading logic for the eval harness — no I/O, no async, no model calls.

Everything here is a deterministic function over plain data, so the whole
grading path is unit-testable with fabricated inputs: no network, no live
Hyperliquid, no Anthropic API key. The I/O lives in :mod:`evals.run_evals`
(loading datasets, executing tools, printing reports); these modules only decide
pass/fail.

This module is the stable import surface. The code lives in one module per
concern, and every public name is re-exported here so callers import
``evals.grader`` and never track the split:

- :mod:`evals.observation` — :class:`Observation`, what a model did on one
  prompt (the results-row contract shared by hand-recorded runs and the
  headless runner), and the :data:`NO_TOOL` sentinel.
- :mod:`evals.output_checks` — :func:`grade_output_case`: did a tool's
  response satisfy its assertions? :data:`KNOWN_CHECKS` is the single source
  of truth for the check vocabulary declared in ``evals/README.md``.
- :mod:`evals.selection` — :func:`grade_selection_case`: did the model route
  to the right tool (or, for ``expected_tools``, all of the right tools)?
- :mod:`evals.repeats` — :func:`summarize_repeats`: stable pass / flaky /
  stable fail per case across repeated runs, and :func:`model_warnings` for
  runs that hide or mix their model.
"""

from __future__ import annotations

from evals.observation import NO_TOOL, Observation
from evals.output_checks import (
    DEFAULT_NEG_OF_TOLERANCE,
    KNOWN_CHECKS,
    AssertionResult,
    OutputCaseResult,
    PathNotFound,
    evaluate_assertion,
    grade_output_case,
    resolve_path,
)
from evals.repeats import RepeatSummary, Stability, model_warnings, summarize_repeats
from evals.selection import SelectionCaseResult, expected_tools_of, grade_selection_case

__all__ = [
    "DEFAULT_NEG_OF_TOLERANCE",
    "KNOWN_CHECKS",
    "NO_TOOL",
    "AssertionResult",
    "Observation",
    "OutputCaseResult",
    "PathNotFound",
    "RepeatSummary",
    "SelectionCaseResult",
    "Stability",
    "evaluate_assertion",
    "expected_tools_of",
    "grade_output_case",
    "grade_selection_case",
    "model_warnings",
    "resolve_path",
    "summarize_repeats",
]
