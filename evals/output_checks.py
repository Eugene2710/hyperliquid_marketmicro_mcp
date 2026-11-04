"""Grading for ``tool_output`` cases — pure, no I/O.

Evaluates a case's assertions against a tool's response, given as plain data
(a ``model_dump()``-ed response), so the caller owns all I/O.
:data:`KNOWN_CHECKS` is the single source of truth for the check vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

# The assertion vocabulary a tool_output case may use. Kept here (not in
# run_evals) so the validator and the evaluator can never disagree about which
# checks exist -- a case using an unknown check is a dataset bug, and both the
# structural validation pass and the grading pass read this same set.
KNOWN_CHECKS: Final[frozenset[str]] = frozenset(
    {"non_empty", "gte", "lte", "eq", "in_range", "neg_of"}
)

# Absolute tolerance ``neg_of`` uses when an assertion does not set ``tolerance``.
# The invariant it was built for (carry_annualized_long == -carry_annualized_short)
# holds bit-exactly: both sides are ``(+/-rate) * 8760``, IEEE-754 negation is
# exact, and a product's rounding does not depend on its sign. The slack only
# absorbs a future implementation computing the two sides by different float paths.
DEFAULT_NEG_OF_TOLERANCE: Final[float] = 1e-12


class PathNotFound(LookupError):
    """Raised when a dotted assertion path does not resolve in a response.

    Distinct from a failed assertion: a missing path means the *case* refers to
    a field the response does not have (a dataset bug, or a renamed response
    field), not that the value was wrong. Callers surface it as a failure with a
    different message so the two are never confused.
    """


@dataclass(frozen=True)
class AssertionResult:
    """Outcome of evaluating one assertion against one response.

    Attributes:
        path: The dotted path this assertion addressed (e.g. ``freshness.staleness_ms``).
        check: The check name, one of :data:`KNOWN_CHECKS`.
        passed: Whether the assertion held.
        detail: Human-readable explanation, always populated (on success too, so
            a passing report can show what was actually compared).
    """

    path: str
    check: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class OutputCaseResult:
    """Aggregate outcome for one ``tool_output`` case.

    Attributes:
        case_id: The case's ``id``.
        tool: The tool the case exercised.
        passed: True only if every assertion passed.
        assertions: Per-assertion results, in dataset order.
        error: Populated when the tool call itself raised, in which case
            ``assertions`` is empty and ``passed`` is False. A tool that blew up
            is a distinct failure from a tool that returned bad values. The
            exception: an ``expect_error`` case that raised the expected class
            passes, and ``error`` records what was raised.
    """

    case_id: str
    tool: str
    passed: bool
    assertions: tuple[AssertionResult, ...] = ()
    error: str | None = None


def resolve_path(data: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted ``path`` against a nested mapping.

    Mechanism: takes a response mapping and a dotted path, walks the segments
    left to right, returns the value at the end.

    Args:
        data: The response as plain data (typically ``response.model_dump()``).
        path: Dotted path, e.g. ``"freshness.staleness_ms"``. A single segment
            with no dots addresses a top-level field.

    Returns:
        The value at ``path``.

    Raises:
        PathNotFound: If any segment is missing, or if a non-final segment
            resolves to something that is not a mapping (so the path cannot be
            walked further).
    """
    current: Any = data
    walked: list[str] = []
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            raise PathNotFound(
                f"{'.'.join(walked) or '<root>'} is {type(current).__name__}, not a mapping; "
                f"cannot resolve {segment!r} in path {path!r}"
            )
        if segment not in current:
            available = ", ".join(sorted(current)[:8]) or "<empty>"
            raise PathNotFound(
                f"{path!r}: no key {segment!r} at "
                f"{'.'.join(walked) or '<root>'} (available: {available})"
            )
        walked.append(segment)
        current = current[segment]
    return current


def _as_number(value: Any) -> float:
    """Coerce a JSON value to a float for ordered comparison.

    Mechanism: takes a value, rejects anything not a real number, returns it as
    a float.

    ``bool`` is excluded even though it subclasses ``int`` -- ``True >= 0.5``
    is legal Python but always a dataset bug, and silently comparing it would
    turn a malformed case into a passing one.

    Args:
        value: The value to coerce.

    Returns:
        The value as a ``float``.

    Raises:
        TypeError: If ``value`` is not an ``int`` or ``float`` (or is a ``bool``).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected a number, got {type(value).__name__}: {value!r}")
    return float(value)


def evaluate_assertion(data: Mapping[str, Any], assertion: Mapping[str, Any]) -> AssertionResult:
    """Evaluate one assertion against a response.

    Mechanism: takes a response mapping and one assertion, resolves the
    assertion's dotted path, applies its check, returns pass/fail with a
    human-readable explanation.

    Every failure mode is returned as a failing :class:`AssertionResult` rather
    than raised, so one malformed assertion never aborts a whole eval run --
    the report shows what went wrong for that assertion and the rest still run.

    Args:
        data: The response as plain data.
        assertion: A mapping with ``path`` (str), ``check`` (one of
            :data:`KNOWN_CHECKS`), and — for the comparison checks — ``value``.
            ``in_range`` expects ``value`` to be a two-element ``[lo, hi]``.
            ``neg_of`` expects ``value`` to be a second dotted path, and takes an
            optional absolute ``tolerance`` (default
            :data:`DEFAULT_NEG_OF_TOLERANCE`); it passes when
            ``|data[path] + data[value]| <= tolerance``.

    Returns:
        An :class:`AssertionResult`. Never raises.
    """
    path = str(assertion.get("path", "<missing>"))
    check = str(assertion.get("check", "<missing>"))

    if check not in KNOWN_CHECKS:
        return AssertionResult(path, check, False, f"unknown check {check!r}")

    try:
        actual = resolve_path(data, path)
    except PathNotFound as exc:
        return AssertionResult(path, check, False, str(exc))

    if check == "non_empty":
        # Sized values must actually contain something; unsized values only have
        # to exist. `0` and `False` are legitimately non-empty scalars, so a
        # plain truthiness test would wrongly fail them.
        if isinstance(actual, (str, bytes, Sequence, Mapping)):
            ok = len(actual) > 0
            return AssertionResult(
                path, check, ok, f"len={len(actual)}" + ("" if ok else " (empty)")
            )
        ok = actual is not None
        return AssertionResult(path, check, ok, f"value={actual!r}")

    if "value" not in assertion:
        return AssertionResult(path, check, False, f"check {check!r} requires a 'value'")
    expected = assertion["value"]

    if check == "neg_of":
        # Cross-field: `value` names a second path, and the two must be negatives
        # of each other. Lets a case assert a sign invariant on live data (the
        # values move; their relationship must not).
        if not isinstance(expected, str):
            return AssertionResult(
                path, check, False, f"neg_of needs 'value' to be a path string, got {expected!r}"
            )
        try:
            other = resolve_path(data, expected)
        except PathNotFound as exc:
            return AssertionResult(path, check, False, str(exc))
        try:
            a, b = _as_number(actual), _as_number(other)
            tolerance = _as_number(assertion.get("tolerance", DEFAULT_NEG_OF_TOLERANCE))
        except TypeError as exc:
            return AssertionResult(path, check, False, str(exc))
        gap = abs(a + b)
        return AssertionResult(
            path, check, gap <= tolerance, f"{a} vs -({expected}={b}): |a+b|={gap} <= {tolerance}"
        )

    if check == "eq":
        ok = bool(actual == expected)
        return AssertionResult(path, check, ok, f"{actual!r} == {expected!r}")

    if check == "in_range":
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            return AssertionResult(path, check, False, f"in_range needs [lo, hi], got {expected!r}")
        bounds = list(expected)
        if len(bounds) != 2:
            return AssertionResult(
                path, check, False, f"in_range needs exactly 2 bounds, got {len(bounds)}"
            )
        try:
            value, low, high = _as_number(actual), _as_number(bounds[0]), _as_number(bounds[1])
        except TypeError as exc:
            return AssertionResult(path, check, False, str(exc))
        ok = low <= value <= high
        return AssertionResult(path, check, ok, f"{value} in [{low}, {high}]")

    # gte / lte
    try:
        value, limit = _as_number(actual), _as_number(expected)
    except TypeError as exc:
        return AssertionResult(path, check, False, str(exc))
    ok = value >= limit if check == "gte" else value <= limit
    symbol = ">=" if check == "gte" else "<="
    return AssertionResult(path, check, ok, f"{value} {symbol} {limit}")


def grade_output_case(
    case: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    error: str | None = None,
    error_type: str | None = None,
) -> OutputCaseResult:
    """Grade one ``tool_output`` case against the response its tool produced.

    Mechanism: takes a case and either the tool's response or the error it
    raised; an ``expect_error`` case passes only if the raised class name
    matches, any other case evaluates every assertion; returns the aggregate
    outcome.

    ``expect_error`` matches the exception's exact class name, not its
    ancestry: the grader sees only the name, and an error path that starts
    raising a different class (even a subclass) is a contract change worth
    surfacing.

    Args:
        case: The dataset case (``id``, ``tool``, ``args``, and either
            ``assertions`` or ``expect_error``).
        response: The tool's response as plain data, or ``None`` if it failed.
        error: The error text if the tool call raised (by convention
            ``"<ClassName>: <message>"``); when set on a case without
            ``expect_error``, no assertions are evaluated and the case fails.
        error_type: The raised exception's class name (e.g. ``"ValueError"``),
            compared against the case's ``expect_error``.

    Returns:
        An :class:`OutputCaseResult`. A case with zero assertions fails — an
        empty assertion list would otherwise vacuously pass and silently
        measure nothing. On an ``expect_error`` case ``error`` carries the
        raised text on a pass too, so the report shows what was raised.
    """
    case_id = str(case.get("id", "<no id>"))
    tool = str(case.get("tool", "<no tool>"))

    expect_error = case.get("expect_error")
    if expect_error is not None:
        if error is None and error_type is None:
            return OutputCaseResult(
                case_id, tool, passed=False, error=f"expected {expect_error}; the call succeeded"
            )
        raised = str(error) if error is not None else str(error_type)
        if error_type == expect_error:
            return OutputCaseResult(case_id, tool, passed=True, error=raised)
        return OutputCaseResult(
            case_id, tool, passed=False, error=f"expected {expect_error}, got {raised}"
        )

    if error is not None:
        return OutputCaseResult(case_id, tool, passed=False, error=error)
    if response is None:
        return OutputCaseResult(case_id, tool, passed=False, error="no response and no error")

    raw = case.get("assertions")
    assertions = [a for a in raw if isinstance(a, Mapping)] if isinstance(raw, list) else []
    if not assertions:
        return OutputCaseResult(case_id, tool, passed=False, error="case has no assertions")

    results = tuple(evaluate_assertion(response, a) for a in assertions)
    return OutputCaseResult(case_id, tool, all(r.passed for r in results), results)
