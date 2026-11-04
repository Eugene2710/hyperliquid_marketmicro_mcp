"""Folding repeated ``tool_selection`` runs into per-case stability — pure, no I/O.

The routing eval is a regression detector: a case moving from 3/3 to 0/3 is the
signal. One run per case cannot tell a flaky case from a broken one, so the same
dataset is run several times and each case is classified across the runs here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from evals.observation import Observation
from evals.selection import SelectionCaseResult

# Per-case verdict across repeated runs of the same dataset.
Stability = Literal["stable pass", "flaky", "stable fail"]


@dataclass(frozen=True)
class RepeatSummary:
    """One case's routing verdicts folded across several runs.

    Attributes:
        case_id: The case's ``id``.
        n_runs: How many runs observed this case. A run that skipped it does
            not count, so a partial run cannot pose as a failure.
        n_passed: How many of those runs routed correctly (``tool_ok``).
        stability: ``"stable pass"`` (every run passed), ``"stable fail"``
            (none did) or ``"flaky"`` (mixed).
        failures: The ``detail`` of each failing run, in run order.
    """

    case_id: str
    n_runs: int
    n_passed: int
    stability: Stability
    failures: tuple[str, ...] = ()


def summarize_repeats(runs: Sequence[Sequence[SelectionCaseResult]]) -> list[RepeatSummary]:
    """Fold several graded runs into one stability verdict per case.

    Mechanism: takes one list of graded cases per run, groups the results by
    case id in first-seen order, counts passes, returns a
    :class:`RepeatSummary` per case.

    Args:
        runs: Graded results, one sequence per results file.

    Returns:
        One summary per case observed in at least one run.
    """
    grouped: dict[str, list[SelectionCaseResult]] = {}
    for run in runs:
        for result in run:
            grouped.setdefault(result.case_id, []).append(result)
    summaries: list[RepeatSummary] = []
    for case_id, results in grouped.items():
        n_passed: int = sum(1 for r in results if r.tool_ok)
        stability: Stability
        if n_passed == len(results):
            stability = "stable pass"
        elif n_passed == 0:
            stability = "stable fail"
        else:
            stability = "flaky"
        failures: tuple[str, ...] = tuple("; ".join(r.detail) for r in results if not r.tool_ok)
        summaries.append(RepeatSummary(case_id, len(results), n_passed, stability, failures))
    return summaries


def model_warnings(runs: Mapping[str, Sequence[Observation]]) -> list[str]:
    """Warn about results rows that hide or mix the model that produced them.

    Mechanism: takes each results file's observations keyed by file name,
    flags rows with no recorded ``model`` and, when the recorded models differ
    across the set, says which file used which; returns the warnings.

    Effort changes tool-calling behaviour, so scoring a ``/medium`` run together
    with a ``/high`` run as if they were repeats would report a model difference
    as flakiness. These are warnings, not errors, so old results files stay
    scoreable.

    Args:
        runs: Observations per results file, keyed by a display name.

    Returns:
        Human-readable warnings; empty when every row names one shared model.
    """
    warnings: list[str] = []
    per_file: dict[str, set[str]] = {}
    for name, observations in runs.items():
        unnamed: list[str] = [o.case_id for o in observations if o.model == "unknown"]
        if unnamed:
            warnings.append(f"{name}: no 'model' on row(s) {', '.join(unnamed)}")
        per_file[name] = {o.model for o in observations if o.model != "unknown"}
    all_models: set[str] = set().union(*per_file.values())
    if len(all_models) > 1:
        breakdown: str = "; ".join(f"{n}: {', '.join(sorted(m))}" for n, m in per_file.items())
        warnings.append(f"results mix models ({breakdown}) — not comparable as repeats")
    return warnings
