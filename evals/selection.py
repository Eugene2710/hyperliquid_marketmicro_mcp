"""Grading for ``tool_selection`` cases — pure, no I/O.

Decides whether the model routed to the expected tool(s), from an
:class:`~evals.observation.Observation`, whatever produced it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from evals.observation import NO_TOOL, Observation


@dataclass(frozen=True)
class SelectionCaseResult:
    """Aggregate outcome for one ``tool_selection`` case.

    ``tool_ok`` and ``args_ok`` are reported separately and deliberately: per
    ``evals/README.md`` the expected args are a *soft* expectation, so calling
    the right tool with imperfect args is a materially different (and much less
    serious) result than calling the wrong tool. Folding them into one boolean
    would hide that.

    ``observed_tools`` is kept in full rather than reduced to a single name, but
    calling several tools is deliberately NOT scored. Extra calls may be
    necessary sequencing (discover dex names, then query them) or unnecessary
    hedging, and nothing here can distinguish the two -- so the list is reported
    as a fact and the judgment stays with the human reading the case notes.

    Attributes:
        case_id: The case's ``id``.
        expected_tool: Tool a single-tool case expects; ``None`` for a negative
            case AND for a multi-tool case (read ``expected_tools`` for those).
        observed_tools: Tools the model actually called, in call order.
        tool_ok: Whether the expected tool was called at all (for a negative
            case, whether nothing was called; for a multi-tool case, whether
            every expected tool was called). The headline metric.
        args_ok: Whether every expected arg matched, or ``None`` when the case
            declares no ``expected_args`` (nothing to check).
        detail: Human-readable notes, including per-argument mismatches.
        expected_tools: Every tool the case expects, in dataset order: one name
            for a single-tool case, several for a multi-tool case, empty for a
            negative case.
    """

    case_id: str
    expected_tool: str | None
    observed_tools: list[str]
    tool_ok: bool
    args_ok: bool | None
    detail: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()


def expected_tools_of(case: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the tools a ``tool_selection`` case expects.

    Mechanism: takes a case, reads exactly one of ``expected_tool`` (one name,
    or ``null`` for a negative case) and ``expected_tools`` (a list of two or
    more names, all of which must be called), returns them as a tuple.

    Args:
        case: The dataset case.

    Returns:
        ``()`` for a negative case, ``(name,)`` for a single-tool case, or the
        listed names for a multi-tool case.

    Raises:
        ValueError: If the case has both keys or neither, if ``expected_tool``
            is neither a non-empty string nor ``null``, or if
            ``expected_tools`` is not a list of at least two distinct non-empty
            strings (one tool belongs in ``expected_tool``; none is
            ``expected_tool: null``).
    """
    has_single, has_multi = "expected_tool" in case, "expected_tools" in case
    if has_single and has_multi:
        raise ValueError("has both 'expected_tool' and 'expected_tools'; use one")
    if not has_single and not has_multi:
        raise ValueError("needs 'expected_tool' (null for a negative case) or 'expected_tools'")
    if has_single:
        single = case["expected_tool"]
        if single is NO_TOOL:
            return ()
        if not isinstance(single, str) or not single:
            raise ValueError(f"'expected_tool' must be a tool name or null, got {single!r}")
        return (single,)
    multi = case["expected_tools"]
    if (
        not isinstance(multi, list)
        or len(multi) < 2
        or not all(isinstance(t, str) and t for t in multi)
        or len(set(multi)) != len(multi)
    ):
        raise ValueError(f"'expected_tools' must list 2+ distinct tool names, got {multi!r}")
    return tuple(multi)


def grade_selection_case(case: Mapping[str, Any], observation: Observation) -> SelectionCaseResult:
    """Grade one ``tool_selection`` case against what the model actually did.

    Mechanism: takes a case and an observation, checks whether the expected tool
    was among those called (and whether it was the only one, and whether its args
    match), returns the three verdicts separately.

    A turn may call several tools, so ``tool_ok`` asks only whether the expected
    one was reached, not whether it was the only one called. The extra calls are
    recorded in ``observed_tools`` and described in ``detail``, but not scored --
    see :class:`SelectionCaseResult` for why.

    Argument comparison is a **subset** check against the expected tool's own
    call: every key in ``expected_args`` must be present and equal, but extra
    args the model supplied are not penalised. Defaults and reasonable extras
    (e.g. an explicit ``include_hip3: false``) are not routing errors, and
    failing them would make the metric measure prompt-phrasing luck instead of
    tool choice.

    A multi-tool case (``expected_tools``) is graded by
    :func:`_grade_multi_tool_case`: every listed tool must be called.

    Args:
        case: The dataset case (``id``, ``prompt``, ``expected_tool`` or
            ``expected_tools``, optional ``expected_args``). ``expected_tool``
            of ``None`` marks a negative case: the correct behaviour is to call
            no tool at all.
        observation: What the model did.

    Returns:
        A :class:`SelectionCaseResult`. A malformed case (see
        :func:`expected_tools_of`) is returned as a failure, not raised, so one
        bad case never hides the verdict on the rest.
    """
    case_id = str(case.get("id", "<no id>"))
    observed = observation.observed_tools
    try:
        expected_tools = expected_tools_of(case)
    except ValueError as exc:
        return SelectionCaseResult(
            case_id, None, list(observed), False, None, (f"malformed case: {exc}",)
        )
    if len(expected_tools) > 1:
        return _grade_multi_tool_case(case_id, case, expected_tools, observation)
    expected_tool = expected_tools[0] if expected_tools else None

    detail: list[str] = []

    if expected_tool is None:
        # Negative case: the correct behaviour is to call nothing at all.
        tool_ok = not observed
        detail.append(
            "called no tool (correct)"
            if tool_ok
            else f"expected NO tool, but called {', '.join(observed)}"
        )
    else:
        tool_ok = expected_tool in observed
        if not tool_ok:
            detail.append(
                f"expected {expected_tool}, but no tool was called"
                if not observed
                else f"expected {expected_tool}, got {', '.join(observed)}"
            )
        elif observed == [expected_tool]:
            detail.append(f"called {expected_tool}")
        else:
            # Stated, not judged -- see the class docstring.
            others = [t for t in observed if t != expected_tool]
            detail.append(
                f"called {expected_tool} (correct); also called {', '.join(others)}"
                f" — {len(observed)} calls"
            )

    expected_args = case.get("expected_args")
    args_ok: bool | None = None
    if isinstance(expected_args, Mapping) and expected_args:
        # Only meaningful when the expected tool was actually reached -- comparing
        # args against a different tool's signature would report noise as a
        # second failure.
        if not tool_ok or expected_tool is None:
            detail.append("args not compared (wrong tool)")
        else:
            mismatches = _arg_mismatches(expected_args, observation.args_for(expected_tool))
            args_ok = not mismatches
            detail.extend(mismatches or ["args match"])

    return SelectionCaseResult(
        case_id=case_id,
        expected_tool=expected_tool,
        observed_tools=list(observed),
        tool_ok=tool_ok,
        args_ok=args_ok,
        detail=tuple(detail),
        expected_tools=expected_tools,
    )


def _arg_mismatches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """List how one call's arguments fall short of the expected subset.

    Mechanism: takes the expected args and one call's actual args, checks every
    expected key is present and equal, returns one message per mismatch.

    Args:
        expected: The case's expected arguments for this tool.
        actual: The arguments the model passed to that tool.

    Returns:
        Mismatch messages; empty when every expected key matched.
    """
    mismatches: list[str] = []
    for key, want in expected.items():
        if key not in actual:
            mismatches.append(f"{key}: missing (expected {want!r})")
        elif actual[key] != want:
            mismatches.append(f"{key}: got {actual[key]!r}, want {want!r}")
    return mismatches


def _grade_multi_tool_case(
    case_id: str,
    case: Mapping[str, Any],
    expected_tools: tuple[str, ...],
    observation: Observation,
) -> SelectionCaseResult:
    """Grade a case that expects several tools (``expected_tools``).

    Mechanism: takes the case and its expected tool list, passes ``tool_ok``
    only when every expected tool was called (order not scored), names any
    missing ones, states any extra ones, then compares each tool's own
    ``expected_args`` entry against that tool's call.

    For a multi-tool case ``expected_args`` is keyed by tool name
    (``{"funding_carry": {"coin": "BTC"}, ...}``), because two tools rarely
    share a signature; a tool with no entry has nothing to compare.

    Args:
        case_id: The case's ``id``.
        case: The dataset case.
        expected_tools: The tools the case expects (two or more).
        observation: What the model did.

    Returns:
        A :class:`SelectionCaseResult` whose ``expected_tool`` is ``None`` and
        whose ``expected_tools`` carries the full list.
    """
    observed = observation.observed_tools
    missing = [t for t in expected_tools if t not in observed]
    tool_ok = not missing
    detail: list[str] = []
    if missing:
        called = f"called {', '.join(observed)}" if observed else "no tool was called"
        detail.append(
            f"expected {', '.join(expected_tools)}; missing {', '.join(missing)} ({called})"
        )
    else:
        others = [t for t in observed if t not in expected_tools]
        # Extras are stated, not judged -- same rule as the single-tool path.
        detail.append(
            f"called {', '.join(expected_tools)}"
            + (
                f" (correct); also called {', '.join(others)} — {len(observed)} calls"
                if others
                else ""
            )
        )

    expected_args = case.get("expected_args")
    args_ok: bool | None = None
    if isinstance(expected_args, Mapping) and expected_args:
        if not tool_ok:
            detail.append("args not compared (expected tools not all called)")
        else:
            mismatches: list[str] = []
            for tool in expected_tools:
                per_tool = expected_args.get(tool)
                if isinstance(per_tool, Mapping):
                    actual = observation.args_for(tool)
                    mismatches.extend(f"{tool}.{m}" for m in _arg_mismatches(per_tool, actual))
            args_ok = not mismatches
            detail.extend(mismatches or ["args match"])

    return SelectionCaseResult(
        case_id=case_id,
        expected_tool=None,
        observed_tools=list(observed),
        tool_ok=tool_ok,
        args_ok=args_ok,
        detail=tuple(detail),
        expected_tools=expected_tools,
    )
