"""Eval harness for ``hlmcp`` — tooling *about* the package, not part of it.

Lives outside ``src/`` deliberately (see ``evals/README.md``). It is a package
only so :mod:`evals.grader` can be imported by the unit tests under ``tests/``;
nothing here is shipped in the wheel.
"""
