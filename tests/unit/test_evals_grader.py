"""Unit tests for the eval grader — pure, offline, no model and no network.

These fabricate both halves of every comparison (the response, and what the
model "did"), which is the whole point: the grading path can be proven correct
without an Anthropic API key, without live Hyperliquid, and without spending
anything. Every check in :data:`~evals.grader.KNOWN_CHECKS` is exercised in
*both* directions — a grader that has only ever passed is not known to work.
"""

from typing import Any

import pytest

from evals.grader import (
    DEFAULT_NEG_OF_TOLERANCE,
    KNOWN_CHECKS,
    AssertionResult,
    Observation,
    PathNotFound,
    SelectionCaseResult,
    evaluate_assertion,
    expected_tools_of,
    grade_output_case,
    grade_selection_case,
    model_warnings,
    resolve_path,
    summarize_repeats,
)

# A stand-in for an OrderBookImbalanceResponse.model_dump(), shaped like the
# real thing so the dotted paths under test match the ones in the datasets.
RESPONSE: dict[str, Any] = {
    "coin": "BTC",
    "mid_price": 67234.5,
    "n_bid_levels": 20,
    "n_ask_levels": 18,
    "bands": [{"band_bps": 10.0, "imbalance": 0.31}],
    "empty_list": [],
    "zero": 0,
    "flag": False,
    "nothing": None,
    "freshness": {"server_time_ms": 1_700_000_000_000, "staleness_ms": 491},
}


# --------------------------------------------------------------------------
# resolve_path
# --------------------------------------------------------------------------


def test_resolve_path_top_level_and_nested() -> None:
    """A single segment reads a top-level field; dots walk into nested maps."""
    assert resolve_path(RESPONSE, "coin") == "BTC"
    assert resolve_path(RESPONSE, "freshness.staleness_ms") == 491


def test_resolve_path_missing_key_names_the_path_and_alternatives() -> None:
    """A missing key raises PathNotFound and says what *was* available."""
    with pytest.raises(PathNotFound) as exc:
        resolve_path(RESPONSE, "freshness.nonexistent")
    assert "nonexistent" in str(exc.value)
    assert "server_time_ms" in str(exc.value)


def test_resolve_path_through_non_mapping_raises() -> None:
    """Walking into a scalar fails loudly rather than returning None."""
    with pytest.raises(PathNotFound):
        resolve_path(RESPONSE, "coin.nope")


# --------------------------------------------------------------------------
# evaluate_assertion — every check, both directions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("assertion", "expected"),
    [
        ({"path": "bands", "check": "non_empty"}, True),
        ({"path": "empty_list", "check": "non_empty"}, False),
        ({"path": "coin", "check": "eq", "value": "BTC"}, True),
        ({"path": "coin", "check": "eq", "value": "ETH"}, False),
        ({"path": "mid_price", "check": "gte", "value": 0}, True),
        ({"path": "mid_price", "check": "gte", "value": 1e9}, False),
        ({"path": "n_ask_levels", "check": "lte", "value": 20}, True),
        ({"path": "n_bid_levels", "check": "lte", "value": 5}, False),
        ({"path": "n_bid_levels", "check": "in_range", "value": [0, 20]}, True),
        ({"path": "n_bid_levels", "check": "in_range", "value": [0, 5]}, False),
        ({"path": "freshness.staleness_ms", "check": "gte", "value": 0}, True),
    ],
    ids=lambda p: str(p),
)
def test_every_check_in_both_directions(assertion: dict[str, Any], expected: bool) -> None:
    """Each supported check passes when it should and fails when it should."""
    assert evaluate_assertion(RESPONSE, assertion).passed is expected


def test_all_known_checks_are_covered_by_this_module() -> None:
    """Guard: if a new check is added to KNOWN_CHECKS, this file must grow too."""
    assert KNOWN_CHECKS == {"non_empty", "gte", "lte", "eq", "in_range", "neg_of"}


def test_non_empty_accepts_falsy_scalars() -> None:
    """`0` and `False` are present values, not empty ones.

    A naive truthiness test would fail a legitimately-zero staleness or a
    `False` flag — exactly the kind of silent wrong-failure that erodes trust
    in an eval.
    """
    assert evaluate_assertion(RESPONSE, {"path": "zero", "check": "non_empty"}).passed is True
    assert evaluate_assertion(RESPONSE, {"path": "flag", "check": "non_empty"}).passed is True
    assert evaluate_assertion(RESPONSE, {"path": "nothing", "check": "non_empty"}).passed is False


# --------------------------------------------------------------------------
# evaluate_assertion — malformed cases fail, they do not explode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "assertion",
    [
        {"path": "coin", "check": "startswith", "value": "B"},  # unknown check
        {"path": "mid_price", "check": "gte"},  # comparison with no value
        {"path": "missing.path", "check": "non_empty"},  # unresolvable path
        {"path": "n_bid_levels", "check": "in_range", "value": [1]},  # wrong bound count
        {"path": "n_bid_levels", "check": "in_range", "value": "0-20"},  # bounds not a list
        {"path": "coin", "check": "gte", "value": 0},  # non-numeric compared numerically
        {"path": "flag", "check": "gte", "value": 0},  # bool is not a number here
    ],
    ids=["unknown_check", "no_value", "bad_path", "one_bound", "str_bounds", "str_vs_num", "bool"],
)
def test_malformed_assertions_fail_without_raising(assertion: dict[str, Any]) -> None:
    """A bad assertion is a failing result, never an exception.

    One malformed case must not abort the whole run — the report should show
    what was wrong and the remaining cases should still be graded.
    """
    result = evaluate_assertion(RESPONSE, assertion)
    assert isinstance(result, AssertionResult)
    assert result.passed is False
    assert result.detail


# --------------------------------------------------------------------------
# grade_output_case
# --------------------------------------------------------------------------


def test_output_case_passes_only_when_every_assertion_passes() -> None:
    """One failing assertion fails the case."""
    case: dict[str, Any] = {
        "id": "out-001",
        "tool": "order_book_imbalance",
        "args": {"coin": "BTC"},
        "assertions": [
            {"path": "coin", "check": "eq", "value": "BTC"},
            {"path": "bands", "check": "non_empty"},
        ],
    }
    assert grade_output_case(case, RESPONSE).passed is True

    case["assertions"].append({"path": "mid_price", "check": "gte", "value": 1e9})
    result = grade_output_case(case, RESPONSE)
    assert result.passed is False
    assert [a.passed for a in result.assertions] == [True, True, False]


def test_output_case_records_a_raised_tool_error_distinctly() -> None:
    """A tool that raised is a different failure from one returning bad values."""
    case = {"id": "out-003", "tool": "whale_position_monitor", "args": {}, "assertions": [{}]}
    result = grade_output_case(case, None, error="HLAPIError: 500")
    assert result.passed is False
    assert result.error == "HLAPIError: 500"
    assert result.assertions == ()


def test_output_case_with_no_assertions_fails_rather_than_vacuously_passing() -> None:
    """An empty assertion list must not score as a pass.

    `all([])` is True, so the naive implementation would report a case that
    checks nothing as green — the worst possible outcome for a test harness.
    """
    case: dict[str, Any] = {"id": "out-x", "tool": "list_hip3_dexes", "args": {}, "assertions": []}
    assert grade_output_case(case, RESPONSE).passed is False


# --------------------------------------------------------------------------
# grade_selection_case
# --------------------------------------------------------------------------


def test_selection_correct_tool() -> None:
    """The happy path: model routed to the expected tool."""
    case = {"id": "sel-001", "expected_tool": "order_book_imbalance"}
    obs = Observation("sel-001", ["order_book_imbalance"])
    assert grade_selection_case(case, obs).tool_ok is True


def test_selection_wrong_tool() -> None:
    """Routing to a different tool fails and names both tools."""
    case = {"id": "sel-001", "expected_tool": "order_book_imbalance"}
    result = grade_selection_case(case, Observation("sel-001", ["whale_position_monitor"]))
    assert result.tool_ok is False
    assert "expected order_book_imbalance" in " ".join(result.detail)


def test_selection_expected_a_tool_but_none_was_called() -> None:
    """Answering from memory instead of calling a tool is a routing failure."""
    case = {"id": "sel-001", "expected_tool": "order_book_imbalance"}
    result = grade_selection_case(case, Observation("sel-001", []))
    assert result.tool_ok is False
    assert "no tool was called" in " ".join(result.detail)


def test_negative_case_passes_when_no_tool_is_called() -> None:
    """A negative case (expected_tool: null) is satisfied by calling nothing."""
    case = {"id": "sel-neg", "expected_tool": None}
    assert grade_selection_case(case, Observation("sel-neg", [])).tool_ok is True


def test_negative_case_fails_when_a_tool_fires() -> None:
    """A tool firing on an unrelated prompt is caught.

    This is the failure a purely positive dataset cannot see: a tool that fires
    on everything scores 100% without a negative case to contradict it.
    """
    case = {"id": "sel-neg", "expected_tool": None}
    result = grade_selection_case(case, Observation("sel-neg", ["order_book_imbalance"]))
    assert result.tool_ok is False
    assert "expected NO tool" in " ".join(result.detail)


def test_args_are_scored_separately_from_routing() -> None:
    """Right tool with wrong args: tool_ok True, args_ok False."""
    case = {
        "id": "sel-002",
        "expected_tool": "order_book_imbalance",
        "expected_args": {"coin": "ETH", "bands_bps": [25]},
    }
    obs = Observation("sel-002", ["order_book_imbalance"], [{"coin": "ETH", "bands_bps": [10, 50]}])
    result = grade_selection_case(case, obs)
    assert (result.tool_ok, result.args_ok) == (True, False)


def test_extra_args_are_not_penalised() -> None:
    """Arg matching is a subset check.

    The model supplying a sensible extra (an explicit `include_hip3: false`) is
    not a routing error; penalising it would measure prompt-phrasing luck.
    """
    case = {
        "id": "sel-003",
        "expected_tool": "whale_position_monitor",
        "expected_args": {"wallets": ["0xabc"]},
    }
    obs = Observation(
        "sel-003",
        ["whale_position_monitor"],
        [{"wallets": ["0xabc"], "include_hip3": False}],
    )
    result = grade_selection_case(case, obs)
    assert (result.tool_ok, result.args_ok) == (True, True)


def test_args_not_compared_when_the_tool_was_wrong() -> None:
    """Args are left unscored against the wrong tool's signature."""
    case = {
        "id": "sel-002",
        "expected_tool": "order_book_imbalance",
        "expected_args": {"coin": "ETH"},
    }
    result = grade_selection_case(case, Observation("sel-002", ["list_hip3_dexes"], [{}]))
    assert result.args_ok is None
    assert "args not compared" in " ".join(result.detail)


def test_case_without_expected_args_reports_args_ok_as_none() -> None:
    """No expected_args means there is nothing to score, not a silent pass."""
    case = {"id": "sel-005", "expected_tool": "list_hip3_dexes"}
    assert grade_selection_case(case, Observation("sel-005", ["list_hip3_dexes"])).args_ok is None


# --------------------------------------------------------------------------
# Multi-tool turns
#
# Both scenarios below were observed on the first manual routing pass. They are
# described by BEHAVIOUR rather than by dataset case id, and use their own
# fabricated ids, so these tests keep saying something true if the dataset is
# renumbered or its prompts reworded.
# --------------------------------------------------------------------------


def test_extra_tool_calls_do_not_fail_the_case() -> None:
    """Reaching the expected tool passes even when others were called too.

    Scenario: a prompt asking whether large traders are net long or short on a
    coin -- a *positions* question, whose partner case asks the near-identical
    *order book* question about the same coin. The model called the positions
    tool (correct) and the book tool as well, covering both readings instead of
    choosing between them.

    That still counts as reaching the right tool. Whether the extra call was
    worthwhile is a judgment the grader deliberately does not make: it records
    the calls and describes them, and a human reading the notes decides.
    """
    case = {"id": "case-parallel", "expected_tool": "whale_position_monitor"}
    obs = Observation("case-parallel", ["whale_position_monitor", "order_book_imbalance"])
    result = grade_selection_case(case, obs)
    assert result.tool_ok is True
    assert result.observed_tools == ["whale_position_monitor", "order_book_imbalance"]
    assert "also called order_book_imbalance" in " ".join(result.detail)


def test_expected_tool_found_anywhere_in_the_call_order() -> None:
    """The expected tool counts even when it was not the first one called.

    Scenario: a prompt asking what whales hold on HIP-3 markets. Answering it
    needs the deployment names first, so the model called the dex-listing tool
    and then the position tool -- correct sequencing, not a misroute.

    Grading on "the first tool called" would mark this a failure, which is why
    membership is the test rather than position.
    """
    case = {"id": "case-chained", "expected_tool": "whale_position_monitor"}
    obs = Observation("case-chained", ["list_hip3_dexes", "whale_position_monitor"])
    assert grade_selection_case(case, obs).tool_ok is True


def test_args_are_taken_from_the_expected_tools_own_call() -> None:
    """With several calls, args are compared against the right one.

    ``observed_args`` is positionally parallel to ``observed_tools``, so the
    comparison must index to the expected tool's own call. Taking the first
    call's args would compare the dex-listing call's (empty) arguments against
    what the position tool was expected to receive.
    """
    case = {
        "id": "case-chained",
        "expected_tool": "whale_position_monitor",
        "expected_args": {"include_hip3": True},
    }
    obs = Observation(
        "case-chained",
        ["list_hip3_dexes", "whale_position_monitor"],
        [{}, {"include_hip3": True}],
    )
    result = grade_selection_case(case, obs)
    assert (result.tool_ok, result.args_ok) == (True, True)


def test_negative_case_fails_when_any_tool_fires() -> None:
    """A negative case is not satisfied by calling two tools instead of one."""
    case = {"id": "case-negative", "expected_tool": None}
    obs = Observation("case-negative", ["order_book_imbalance", "list_hip3_dexes"])
    assert grade_selection_case(case, obs).tool_ok is False


# --------------------------------------------------------------------------
# Observation.from_row — both results-file shapes
# --------------------------------------------------------------------------


def test_from_row_accepts_the_single_tool_shape() -> None:
    """A results file written before multi-tool support keeps working.

    One ``observed_tool`` name and one ``observed_args`` object, normalized to
    single-element lists. Without this, adopting the list shape would mean
    rewriting every previously recorded run.
    """
    obs = Observation.from_row(
        {
            "id": "case-single",
            "observed_tool": "order_book_imbalance",
            "observed_args": {"coin": "BTC"},
            "model": "claude-sonnet-5/medium",
        }
    )
    assert obs.observed_tools == ["order_book_imbalance"]
    assert obs.args_for("order_book_imbalance") == {"coin": "BTC"}
    assert obs.model == "claude-sonnet-5/medium"


def test_from_row_accepts_the_multi_tool_shape() -> None:
    """The list shape: names in call order, args positionally parallel."""
    obs = Observation.from_row(
        {
            "id": "case-chained",
            "observed_tools": ["list_hip3_dexes", "whale_position_monitor"],
            "observed_args": [{}, {"include_hip3": True}],
        }
    )
    assert obs.observed_tools == ["list_hip3_dexes", "whale_position_monitor"]
    assert obs.args_for("whale_position_monitor") == {"include_hip3": True}
    assert obs.args_for("list_hip3_dexes") == {}


def test_from_row_treats_null_observed_tool_as_no_call() -> None:
    """``"observed_tool": null`` is how "the model called nothing" is recorded.

    The correct answer for a negative case, so it must normalize to an empty
    list rather than a list containing ``None``.
    """
    obs = Observation.from_row({"id": "case-negative", "observed_tool": None, "observed_args": {}})
    assert obs.observed_tools == []
    assert obs.observed_args == []


def test_args_for_tolerates_a_short_args_list() -> None:
    """A partially-recorded observation still grades rather than raising.

    Hand-recorded runs are the normal input here, and a missing arg entry is a
    likely transcription gap. Treating it as ``{}`` degrades to an args mismatch
    the reader can see, instead of an IndexError that kills the whole run.
    """
    obs = Observation.from_row(
        {
            "id": "case-parallel",
            "observed_tools": ["whale_position_monitor", "order_book_imbalance"],
            "observed_args": [{"include_hip3": False}],
        }
    )
    assert obs.args_for("whale_position_monitor") == {"include_hip3": False}
    assert obs.args_for("order_book_imbalance") == {}
    assert obs.args_for("never_called") == {}


def test_from_row_rejects_a_row_with_no_id() -> None:
    """The id is the join key to the dataset; a row without one cannot be graded."""
    with pytest.raises(ValueError, match="id"):
        Observation.from_row({"observed_tool": "order_book_imbalance"})


# --------------------------------------------------------------------------
# neg_of — cross-field sign invariant
# --------------------------------------------------------------------------

# Shaped like a FundingCarryResponse.model_dump(): the carry pair is the case
# neg_of was built for.
CARRY: dict[str, Any] = {
    "carry_annualized_long": -0.1095,
    "carry_annualized_short": 0.1095,
    "carry_off": 0.2,
    "coin": "BTC",
}


def test_neg_of_passes_when_the_fields_are_negatives() -> None:
    """long == -short is exactly what the carry invariant asserts."""
    result = evaluate_assertion(
        CARRY,
        {"path": "carry_annualized_long", "check": "neg_of", "value": "carry_annualized_short"},
    )
    assert result.passed is True


def test_neg_of_fails_when_the_fields_are_not_negatives() -> None:
    """Equal signs (a flipped-sign bug on one side) fail."""
    result = evaluate_assertion(
        CARRY, {"path": "carry_annualized_short", "check": "neg_of", "value": "carry_off"}
    )
    assert result.passed is False
    assert "|a+b|" in result.detail


def test_neg_of_missing_second_path_fails_with_the_path_error() -> None:
    """A bad second path surfaces resolve_path's PathNotFound message, not a crash."""
    with pytest.raises(PathNotFound) as exc:
        resolve_path(CARRY, "carry_annualized_shrt")
    result = evaluate_assertion(
        CARRY,
        {"path": "carry_annualized_long", "check": "neg_of", "value": "carry_annualized_shrt"},
    )
    assert result.passed is False
    assert result.detail == str(exc.value)


def test_neg_of_value_must_be_a_path_string() -> None:
    """A numeric `value` is a dataset bug (neg_of compares two fields), so it fails."""
    result = evaluate_assertion(
        CARRY, {"path": "carry_annualized_long", "check": "neg_of", "value": 0.1095}
    )
    assert result.passed is False


def test_neg_of_rejects_non_numeric_fields() -> None:
    """A string field cannot be negated; the assertion fails rather than raising."""
    result = evaluate_assertion(
        CARRY, {"path": "coin", "check": "neg_of", "value": "carry_annualized_short"}
    )
    assert result.passed is False


@pytest.mark.parametrize(
    ("gap", "tolerance", "expected"),
    [
        (0.5, 0.5, True),  # exactly at the tolerance: inclusive
        (0.5, 0.25, False),  # beyond it
        (0.0, 0.0, True),  # exact negatives with zero tolerance
        (DEFAULT_NEG_OF_TOLERANCE * 10, None, False),  # default tolerance is tight
    ],
    ids=["at_edge", "beyond", "zero_exact", "default_tight"],
)
def test_neg_of_tolerance_edge(gap: float, tolerance: float | None, expected: bool) -> None:
    """|a + b| <= tolerance, inclusive; omitted tolerance uses the tight default.

    Values are binary-exact (0.25, 0.5, 1.0) so the edge case tests the
    comparison, not float rounding.
    """
    data: dict[str, Any] = {"a": -1.0, "b": 1.0 + gap}
    assertion: dict[str, Any] = {"path": "a", "check": "neg_of", "value": "b"}
    if tolerance is not None:
        assertion["tolerance"] = tolerance
    assert evaluate_assertion(data, assertion).passed is expected


# --------------------------------------------------------------------------
# expect_error — output cases that must raise
# --------------------------------------------------------------------------

ERROR_CASE: dict[str, Any] = {
    "id": "case-unknown-coin",
    "tool": "funding_carry",
    "args": {"coin": "NOTACOIN"},
    "expect_error": "ValueError",
}


def test_expect_error_passes_on_the_expected_class() -> None:
    """The call raised the named class: the case passes and records what was raised."""
    result = grade_output_case(
        ERROR_CASE, None, error="ValueError: 'NOTACOIN' is not listed", error_type="ValueError"
    )
    assert result.passed is True
    assert result.error == "ValueError: 'NOTACOIN' is not listed"


def test_expect_error_fails_when_the_call_succeeds() -> None:
    """An error path that silently starts returning data is a regression."""
    result = grade_output_case(ERROR_CASE, {"coin": "NOTACOIN"})
    assert result.passed is False
    assert "succeeded" in str(result.error)


def test_expect_error_fails_on_a_different_class() -> None:
    """E.g. an unvalidated coin reaching fundingHistory raises HLAPIError, not ValueError."""
    result = grade_output_case(
        ERROR_CASE, None, error="HLAPIError: HTTP 500", error_type="HLAPIError"
    )
    assert result.passed is False
    assert "expected ValueError, got HLAPIError" in str(result.error)


# --------------------------------------------------------------------------
# expected_tools — multi-tool selection cases
# --------------------------------------------------------------------------

BOTH_TOOLS: dict[str, Any] = {
    "id": "case-both",
    "expected_tools": ["funding_carry", "order_book_imbalance"],
    "expected_args": {"funding_carry": {"coin": "BTC"}, "order_book_imbalance": {"coin": "BTC"}},
}


def test_expected_tools_all_called_passes_in_any_order() -> None:
    """Every expected tool reached; call order is not scored."""
    obs = Observation(
        "case-both", ["order_book_imbalance", "funding_carry"], [{"coin": "BTC"}, {"coin": "BTC"}]
    )
    result = grade_selection_case(BOTH_TOOLS, obs)
    assert (result.tool_ok, result.args_ok) == (True, True)
    assert result.expected_tool is None
    assert result.expected_tools == ("funding_carry", "order_book_imbalance")


def test_expected_tools_one_missing_fails_and_names_it() -> None:
    """Answering half of a two-part question is a fail, and the detail says which half."""
    result = grade_selection_case(BOTH_TOOLS, Observation("case-both", ["funding_carry"]))
    assert result.tool_ok is False
    assert "missing order_book_imbalance" in result.detail[0]
    assert result.args_ok is None


def test_expected_tools_extra_tool_passes_with_also_called() -> None:
    """Extras are stated, not judged -- the same rule as single-tool cases."""
    obs = Observation(
        "case-both",
        ["funding_carry", "order_book_imbalance", "whale_position_monitor"],
        [{"coin": "BTC"}, {"coin": "BTC"}, {}],
    )
    result = grade_selection_case(BOTH_TOOLS, obs)
    assert result.tool_ok is True
    assert "also called whale_position_monitor" in result.detail[0]


def test_expected_tools_args_are_compared_per_tool() -> None:
    """Each tool's args are checked against its own call and named on mismatch."""
    obs = Observation(
        "case-both", ["funding_carry", "order_book_imbalance"], [{"coin": "BTC"}, {"coin": "ETH"}]
    )
    result = grade_selection_case(BOTH_TOOLS, obs)
    assert (result.tool_ok, result.args_ok) == (True, False)
    assert "order_book_imbalance.coin: got 'ETH', want 'BTC'" in result.detail


@pytest.mark.parametrize(
    "case",
    [
        {"id": "x", "expected_tool": "funding_carry", "expected_tools": ["a", "b"]},  # both
        {"id": "x"},  # neither
        {"id": "x", "expected_tools": ["funding_carry"]},  # one tool belongs in expected_tool
        {"id": "x", "expected_tools": ["funding_carry", "funding_carry"]},  # duplicate
        {"id": "x", "expected_tools": []},  # none is expected_tool: null
        {"id": "x", "expected_tool": 3},  # not a name
    ],
    ids=["both", "neither", "single_in_list", "duplicate", "empty", "non_string"],
)
def test_expected_tools_of_rejects_malformed_cases(case: dict[str, Any]) -> None:
    """Validation (which calls this) rejects both, neither, and malformed lists."""
    with pytest.raises(ValueError):
        expected_tools_of(case)


def test_expected_tools_of_reads_each_shape() -> None:
    """Negative, single and multi cases normalize to a tuple."""
    assert expected_tools_of({"expected_tool": None}) == ()
    assert expected_tools_of({"expected_tool": "list_hip3_dexes"}) == ("list_hip3_dexes",)
    assert expected_tools_of(BOTH_TOOLS) == ("funding_carry", "order_book_imbalance")


def test_malformed_selection_case_grades_as_a_failure() -> None:
    """Grading a malformed case fails visibly instead of raising mid-run."""
    result = grade_selection_case({"id": "x"}, Observation("x", []))
    assert result.tool_ok is False
    assert result.detail[0].startswith("malformed case:")


# --------------------------------------------------------------------------
# Repeats — stability across runs, model warnings
# --------------------------------------------------------------------------


def _result(case_id: str, ok: bool) -> SelectionCaseResult:
    """Build a minimal graded result for the repeat tests."""
    return SelectionCaseResult(case_id, "funding_carry", [], ok, None, ("ok" if ok else "miss",))


def test_summarize_repeats_classifies_each_case() -> None:
    """All pass -> stable pass; mixed -> flaky; none -> stable fail; first-seen order."""
    runs = [
        [_result("a", True), _result("b", True), _result("c", False)],
        [_result("a", True), _result("b", False), _result("c", False)],
        [_result("a", True), _result("b", True), _result("c", False)],
    ]
    summaries = summarize_repeats(runs)
    assert [(s.case_id, s.n_passed, s.n_runs, s.stability) for s in summaries] == [
        ("a", 3, 3, "stable pass"),
        ("b", 2, 3, "flaky"),
        ("c", 0, 3, "stable fail"),
    ]
    assert summaries[2].failures == ("miss", "miss", "miss")


def test_summarize_repeats_counts_only_runs_that_observed_the_case() -> None:
    """A run that skipped a case is not a failure of that case."""
    summaries = summarize_repeats([[_result("a", True)], []])
    assert (summaries[0].n_runs, summaries[0].stability) == (1, "stable pass")


def test_model_warnings_flags_mixed_models_and_missing_model() -> None:
    """Medium vs high in one scoring set is visible; so is a row with no model."""
    warnings = model_warnings(
        {
            "r1.jsonl": [Observation("a", model="claude-sonnet-5/medium"), Observation("b")],
            "r2.jsonl": [Observation("a", model="claude-sonnet-5/high")],
        }
    )
    assert any("r1.jsonl: no 'model' on row(s) b" in w for w in warnings)
    assert any("mix models" in w for w in warnings)


def test_model_warnings_silent_when_one_model() -> None:
    """Consistent runs produce no noise."""
    runs = {
        "r1.jsonl": [Observation("a", model="claude-sonnet-5/medium")],
        "r2.jsonl": [Observation("a", model="claude-sonnet-5/medium")],
    }
    assert model_warnings(runs) == []
