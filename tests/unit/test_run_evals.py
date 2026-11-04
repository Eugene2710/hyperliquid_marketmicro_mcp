"""Unit tests for the eval CLI's validation and scoring — offline, no model, no network.

Grading itself is tested in ``test_evals_grader.py``; these cover what
``run_evals`` adds on top: dataset validation of the new fields, and scoring
one or several results files. The backward-compatibility tests read the
**recorded** results files in ``evals/results/``, not hand-typed rows.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from evals.run_evals import (
    _DATASETS_DIR,
    _load_jsonl,
    _validate_tool_output,
    _validate_tool_selection,
    score_results,
)

RESULTS_DIR: Path = Path(__file__).resolve().parents[2] / "evals" / "results"

CASES: list[dict[str, Any]] = [
    {"id": "c-1", "prompt": "p", "expected_tool": "order_book_imbalance", "gate": True},
    {"id": "c-2", "prompt": "p", "expected_tool": None},
    {"id": "c-3", "prompt": "p", "expected_tool": "list_hip3_dexes"},
]


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write ``rows`` as a JSONL results file and return its path."""
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _dataset_cases() -> list[dict[str, Any]]:
    """Load the real tool_selection dataset."""
    return [case for _, case in _load_jsonl(_DATASETS_DIR / "tool_selection.jsonl")]


# --------------------------------------------------------------------------
# Scoring: a single file behaves exactly as before repeats existed
# --------------------------------------------------------------------------


def test_single_file_score_output_is_pinned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One results file prints the original per-case report, nothing more."""
    path = _write(
        tmp_path / "r1.jsonl",
        [
            {"id": "c-1", "observed_tool": "order_book_imbalance", "model": "m/medium"},
            {"id": "c-2", "observed_tool": "funding_carry", "model": "m/medium"},
        ],
    )
    assert score_results([path], CASES) == 1
    out, err = capsys.readouterr()
    assert out == (
        "tool_selection — routing accuracy\n\n"
        "[PASS] c-1: called order_book_imbalance\n"
        "[FAIL] c-2: expected NO tool, but called funding_carry\n"
        "\nrouting: 1/2 correct (50%)\n"
        "\n1 case(s) not observed, excluded from the score:\n"
        "    - c-3\n"
    )
    assert err == ""


@pytest.mark.parametrize(
    ("results_file", "headline", "exit_code"),
    [
        ("manual-2026-09-09.jsonl", "routing: 13/17 correct (76%)", 1),
        ("manual-2026-10-06.jsonl", "routing: 5/5 correct (100%)", 0),
    ],
)
def test_recorded_results_files_keep_their_numbers(
    results_file: str, headline: str, exit_code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two recorded runs still score as they did before the dataset grew.

    New case ids are unobserved in these files and so excluded from the
    denominator; the migrated sel-018 (now ``expected_tools``) still passes
    because both tools were recorded.
    """
    assert score_results([RESULTS_DIR / results_file], _dataset_cases()) == exit_code
    assert headline in capsys.readouterr().out


def test_missing_model_is_warned_on_stderr_not_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The 09-09 file's sel-004 row has a mistyped ``model`` key; stdout is unaffected."""
    score_results([RESULTS_DIR / "manual-2026-09-09.jsonl"], _dataset_cases())
    out, err = capsys.readouterr()
    assert "no 'model' on row(s) sel-004" in err
    assert "warning" not in out


# --------------------------------------------------------------------------
# Scoring: several files are repeats
# --------------------------------------------------------------------------


def test_multiple_files_report_stability_and_totals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Per case: stable pass / flaky / stable fail, then the totals."""
    r1 = _write(
        tmp_path / "r1.jsonl",
        [
            {"id": "c-1", "observed_tool": "order_book_imbalance", "model": "m/medium"},
            {"id": "c-2", "observed_tool": None, "model": "m/medium"},
            {"id": "c-3", "observed_tool": None, "model": "m/medium"},
        ],
    )
    r2 = _write(
        tmp_path / "r2.jsonl",
        [
            {"id": "c-1", "observed_tool": "order_book_imbalance", "model": "m/medium"},
            {"id": "c-2", "observed_tool": "funding_carry", "model": "m/medium"},
            {"id": "c-3", "observed_tool": None, "model": "m/medium"},
        ],
    )
    assert score_results([r1, r2], CASES) == 1
    out, err = capsys.readouterr()
    assert "[STABLE PASS] c-1: 2/2" in out
    assert "[FLAKY] c-2: 1/2 — expected NO tool, but called funding_carry" in out
    assert "[STABLE FAIL] c-3: 0/2" in out
    assert "stable pass: 1, flaky: 1, stable fail: 1 (of 3 observed cases)" in out
    assert err == ""


def test_multiple_files_on_different_models_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Medium and high runs scored together are flagged as not comparable."""
    r1 = _write(tmp_path / "r1.jsonl", [{"id": "c-1", "observed_tool": None, "model": "m/medium"}])
    r2 = _write(tmp_path / "r2.jsonl", [{"id": "c-1", "observed_tool": None, "model": "m/high"}])
    score_results([r1, r2], CASES)
    assert "mix models" in capsys.readouterr().err


def test_gate_only_scores_only_gated_cases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-gate cases are neither graded nor listed as unobserved."""
    path = _write(
        tmp_path / "r1.jsonl",
        [
            {"id": "c-1", "observed_tool": "order_book_imbalance", "model": "m"},
            {"id": "c-2", "observed_tool": "funding_carry", "model": "m"},
        ],
    )
    assert score_results([path], CASES, gate_only=True) == 0
    out = capsys.readouterr().out
    assert "routing: 1/1 correct" in out
    assert "c-2" not in out and "c-3" not in out


# --------------------------------------------------------------------------
# Validation of the new dataset fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "fragment"),
    [
        (
            {
                "expected_tool": "funding_carry",
                "expected_tools": ["funding_carry", "list_hip3_dexes"],
            },
            "both",
        ),
        ({}, "needs 'expected_tool'"),
        ({"expected_tools": ["funding_carry", "not_a_tool"]}, "not a registered tool"),
        (
            {
                "expected_tools": ["funding_carry", "order_book_imbalance"],
                "expected_args": {"coin": "BTC"},
            },
            "maps each expected tool",
        ),
        ({"expected_tool": None, "gate": "yes"}, "'gate' must be true or false"),
    ],
    ids=["both", "neither", "unregistered", "flat_args_on_multi", "gate_not_bool"],
)
def test_selection_validation_rejects(case: dict[str, Any], fragment: str) -> None:
    """Each malformed shape produces a named error."""
    errors = _validate_tool_selection([(1, {"id": "x", "prompt": "p", **case})])
    assert any(fragment in e for e in errors), errors


def test_selection_validation_accepts_the_new_fields() -> None:
    """A gated multi-tool case with per-tool args is valid."""
    case = {
        "id": "x",
        "prompt": "p",
        "expected_tools": ["funding_carry", "order_book_imbalance"],
        "expected_args": {"funding_carry": {"coin": "BTC"}},
        "gate": True,
    }
    assert _validate_tool_selection([(1, case)]) == []


@pytest.mark.parametrize(
    ("case", "fragment"),
    [
        ({"expect_error": ""}, "exception class name"),
        (
            {"expect_error": "ValueError", "assertions": [{"path": "coin", "check": "non_empty"}]},
            "takes no 'assertions'",
        ),
        ({"assertions": [{"path": "a", "check": "neg_of", "value": 1.0}]}, "must be a path"),
        (
            {"assertions": [{"path": "a", "check": "neg_of", "value": "b", "tolerance": -1}]},
            "non-negative",
        ),
    ],
    ids=["empty_error", "error_with_assertions", "neg_of_numeric_value", "negative_tolerance"],
)
def test_output_validation_rejects(case: dict[str, Any], fragment: str) -> None:
    """expect_error and neg_of shapes are checked before anything runs live."""
    full = {"id": "x", "tool": "funding_carry", "args": {"coin": "BTC"}, **case}
    errors = _validate_tool_output([(1, full)])
    assert any(fragment in e for e in errors), errors


def test_output_validation_accepts_expect_error_without_assertions() -> None:
    """An expect_error case needs no assertions."""
    case = {"id": "x", "tool": "funding_carry", "args": {"coin": "X"}, "expect_error": "ValueError"}
    assert _validate_tool_output([(1, case)]) == []
