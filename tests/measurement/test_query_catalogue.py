"""Section 6's query catalogue, kept complete by discovery rather than by prose.

The failure this file is written against is a provenance header that documents a
query nobody ran. ``docs/measurement-harness.md`` section 6 requires
``query_definitions`` to carry "every query the report ran, as text, plus a
sha256 over the set ... so a reader can run them by hand" (``D-0040``), and there
are exactly two ways to break that while everything still looks right:

* **a copy.** A statement written inline at its call site can only reach the
  header as a pasted second copy. The copy is correct on the day it is pasted and
  goes on being printed after the executed text changes, so the header attests to
  a query that never ran and the artefact shows no sign of it. The fix is the
  lift ``ac9.py`` and ``cohort.py`` have had -- the constant in the catalogue
  *is* the object handed to ``execute`` -- and
  :func:`test_every_statement_a_catalogued_module_executes_is_in_its_catalogue`
  is what keeps it that way: it re-derives, from each module's own source, every
  statement that module executes, and fails on one the catalogue does not carry.
* **a module.** A catalogue that is complete today stops being complete the day
  the report calls into a module that was written afterwards -- and a note in a
  docstring saying "keep this list current" is precisely what does not survive
  that. So :func:`test_the_report_catalogue_carries_every_statement_the_report_runs`
  does not read a list of modules at all: it builds a real report through a
  connection that records every statement executed, and asserts each recorded
  statement is either in the header's catalogue or named, with a reason, in
  ``render.UNATTESTED_STATEMENTS``. A new module reaching the report path fails
  it without this file having heard of the module.

The static half reuses ``test_known_holes._statements_executed`` rather than
parsing the package a second time: that resolver already understands the four
shapes a statement arrives in here (literal, module constant, catalogue entry,
dataclass field), and a second resolver would agree with it until one of them
learned a fifth.

The two halves are deliberately opposed. The static half can be satisfied by a
catalogue full of statements nothing runs; the trace half asserts the converse --
every name in the header's catalogue was observed executing -- so a stale entry
fails too. Neither half is a copy of the report's field list.

Nothing here writes: the report is built over a migrated production database
through the same read-only handle the harness uses, wrapped in a recorder that
forwards to it and adds nothing.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

from claude_org_runtime.measurement import render as render_module
from claude_org_runtime.measurement.provenance import (
    FINGERPRINT_CONTENT,
    FixtureSuiteRef,
    query_catalogue,
)
from claude_org_runtime.measurement.reader import open_for_measurement
from claude_org_runtime.measurement.render import (
    REPORT_QUERY_SOURCES,
    UNATTESTED_STATEMENTS,
    V1ShadowInput,
    build_measurement_report,
)

from .test_known_holes import _module_sources, _statements_executed
from .test_render import GENERATED_AT, PERIOD_END, PERIOD_START, db  # noqa: F401

#: A v1 run id this database does not hold. ``cohort.select_cohort`` refuses a
#: shadow input naming a run it holds (``D-0013``), and the shadow input is here
#: for a reason: without one, the chunked ownership-collision statement never
#: executes, and a trace that never ran it could not notice its absence from the
#: catalogue.
V1_SHADOW_RUN_ID = "v1-run-not-in-this-database"


# --------------------------------------------------------------------------
# the recorder
# --------------------------------------------------------------------------


class _RecordingConnection:
    """The read-only handle, plus a note of every statement executed through it.

    The caller is read off the stack rather than passed in, because the point is
    to name statements issued by code this file does not know about: a module
    added to the report path names itself in the failure message.

    Everything else is forwarded untouched. This is a recorder, not a stub -- the
    report it produces is the real report over the real database, so a statement
    that only runs on a non-empty cohort is recorded too.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self.recorded: list[tuple[str, str]] = []

    def execute(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        frame = sys._getframe(1)
        where = f"{Path(frame.f_code.co_filename).stem}.{frame.f_code.co_name}"
        self.recorded.append((where, statement))
        return self._connection.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _report_with_trace(path: Path) -> tuple[Any, list[tuple[str, str]]]:
    connection = open_for_measurement(path)
    recorder = _RecordingConnection(connection)
    try:
        report = build_measurement_report(
            recorder,
            db_path=str(path),
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=GENERATED_AT,
            fixture_suite=FixtureSuiteRef.absent("no corpus in this test"),
            v1_shadow=V1ShadowInput.observed("v1-export", (V1_SHADOW_RUN_ID,)),
            grace_ms=None,
            fingerprint_mode=FINGERPRINT_CONTENT,
        )
    finally:
        connection.close()
    return report, recorder.recorded


# --------------------------------------------------------------------------
# matching a statement against the catalogue
# --------------------------------------------------------------------------


def _squashed(statement: str) -> str:
    """*statement* with its layout collapsed.

    Indentation is the one difference between the catalogued text and the
    executed text that carries no meaning; every other difference does, and is
    left to fail.
    """

    return " ".join(statement.split())


def _catalogue_matches(catalogued: str, executed: str) -> bool:
    """Is *executed* the statement *catalogued* documents?

    ``{placeholders}`` is the one substitution the catalogue cannot avoid:
    SQLite has no parameter form for an ``IN`` list, so the placeholders are
    generated per chunk while the values stay bound. The template is expanded to
    the arity actually observed and then compared in full, rather than compared
    as a prefix -- a prefix match would accept a statement whose tail had
    changed, which is the drift the catalogue exists to catch.

    *executed* arrives expanded from the trace and unexpanded from the static
    resolver, which reads the template off the call site's ``.format`` and
    cannot know the arity; both are the same statement, so equality is tried
    before expansion.
    """

    if _squashed(catalogued) == _squashed(executed):
        return True
    if "{placeholders}" not in catalogued:
        return False
    generated = _squashed(executed).count("?") - catalogued.count("?")
    if generated < 0:
        return False
    expanded = catalogued.format(
        placeholders=", ".join("?" for _ in range(generated))
    )
    return _squashed(expanded) == _squashed(executed)


def _catalogue_name(catalogue: Mapping[str, str], executed: str) -> str | None:
    for name, text in catalogue.items():
        if _catalogue_matches(text, executed):
            return name
    return None


# --------------------------------------------------------------------------
# the static half -- a catalogued module carries every statement it executes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", sorted(REPORT_QUERY_SOURCES))
def test_every_statement_a_catalogued_module_executes_is_in_its_catalogue(
    module_name: str,
) -> None:
    """The lift, asserted from the source rather than from a docstring.

    ``_statements_executed`` resolves the argument of every ``execute`` call in
    the package back to its text. For a module that publishes a catalogue, each
    resolved text must be in that catalogue -- which is only possible when the
    call site executes the constant, since a text this test cannot resolve is
    reported as a failure rather than skipped.
    """

    catalogue = REPORT_QUERY_SOURCES[module_name]
    discovered = [
        (function, text)
        for short, function, _verb, text in _statements_executed()
        if short == module_name
    ]

    assert discovered, (
        f"{module_name}.py publishes a query catalogue but no execute call was "
        "found in it; either the module stopped running queries (drop its "
        "catalogue) or this test stopped finding them"
    )
    for function, text in discovered:
        assert text is not None, (
            f"{module_name}.{function} hands execute a statement that cannot be "
            "resolved to text, so the catalogue cannot carry the text that ran"
        )
        assert _catalogue_name(catalogue, text) is not None, (
            f"{module_name}.{function} executes a statement that "
            f"{module_name}.QUERY_DEFINITIONS does not carry:\n{text}\n"
            "Lift it to a module-level constant, execute the constant, and add "
            "it to the catalogue (measurement-harness.md section 6)"
        )


@pytest.mark.parametrize("module_name", sorted(REPORT_QUERY_SOURCES))
def test_a_catalogued_module_executes_the_constant_and_not_a_copy(
    module_name: str,
) -> None:
    """Equality of text is not the property; identity of object is.

    A statement inlined at its call site as a copy of the catalogued text passes
    the test above on the day it is written, and stops passing it only after the
    two have already disagreed -- which is one report too late, because the
    disagreeing report is the artefact. So the call site is required to *name*
    the constant (directly, through the catalogue, or through ``.format`` for an
    ``IN`` list's arity), and the catalogue is required to hold that same object:
    then there is one string and no copy to drift.
    """

    module = importlib.import_module(
        f"claude_org_runtime.measurement.{module_name}"
    )
    constants = [
        value
        for name, value in vars(module).items()
        if isinstance(value, str) and not name.startswith("__")
    ]
    for name, text in REPORT_QUERY_SOURCES[module_name].items():
        assert any(text is constant for constant in constants), (
            f"{module_name}.QUERY_DEFINITIONS[{name!r}] is a string of its own "
            "rather than the module-level constant the code executes; a "
            "catalogue holding its own copy is the drift this test exists for"
        )

    for line, argument in _execute_arguments(module_name):
        assert not isinstance(argument, (ast.Constant, ast.JoinedStr)), (
            f"{module_name}.py line {line} hands execute a literal; the "
            "catalogue can then only carry a copy of it. Execute the constant "
            "(measurement-harness.md section 6)"
        )
        assert isinstance(argument, (ast.Name, ast.Subscript, ast.Attribute)), (
            f"{module_name}.py line {line} hands execute a composed statement, "
            "which no catalogue entry can be the text of"
        )


def _execute_arguments(module_name: str) -> Iterator[tuple[int, ast.AST]]:
    """``(line, argument)`` for every ``execute`` call in *module_name*.

    ``.format(...)`` is unwrapped to the template it was called on: expanding an
    ``IN`` list's placeholders is arity, not text, and the template is what the
    catalogue carries.
    """

    for short, tree in _module_sources():
        if short != module_name:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"execute", "executemany", "executescript"}:
                continue
            if not node.args:
                continue
            argument = node.args[0]
            if (
                isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Attribute)
                and argument.func.attr == "format"
            ):
                argument = argument.func.value
            yield node.lineno, argument


# --------------------------------------------------------------------------
# the trace half -- the report's catalogue against the report's own execution
# --------------------------------------------------------------------------


def test_the_report_catalogue_carries_every_statement_the_report_runs(
    db: Path,
) -> None:
    """Section 6's requirement, checked against what the report actually did.

    This is the test that keeps the catalogue complete as modules are added: it
    knows nothing about which modules the report calls, only that whatever it
    called must be attested -- in the catalogue by its text, or by name and
    reason in ``UNATTESTED_STATEMENTS``.
    """

    report, recorded = _report_with_trace(db)
    catalogue = report.header.queries.definitions

    assert len(recorded) > 10, (
        "the trace recorded almost nothing, so this assertion is vacuous; the "
        "recorder is no longer seeing the report's statements"
    )
    for where, statement in recorded:
        if _catalogue_name(catalogue, statement) is not None:
            continue
        assert where in UNATTESTED_STATEMENTS, (
            f"{where} executes a statement the report's query_definitions does "
            f"not carry and UNATTESTED_STATEMENTS does not name:\n{statement}\n"
            "Section 6 requires every query the report ran, as text: lift it to "
            "a constant and add its module to render.REPORT_QUERY_SOURCES, or "
            "-- if its text cannot exist before it runs -- name it in "
            "render.UNATTESTED_STATEMENTS with the reason"
        )


def test_every_catalogued_query_was_one_the_report_ran(db: Path) -> None:
    """The converse, so completeness cannot be bought with stale entries.

    ``query_definitions`` is "every query the report ran" in both directions. A
    name in the catalogue that nothing executed is a statement a reader would
    run by hand believing it produced one of these numbers.
    """

    report, recorded = _report_with_trace(db)
    catalogue = report.header.queries.definitions

    observed = {
        name
        for _where, statement in recorded
        for name in [_catalogue_name(catalogue, statement)]
        if name is not None
    }
    assert observed == set(catalogue), (
        "these catalogue entries name no statement this report executed: "
        f"{sorted(set(catalogue) - observed)}"
    )


def test_no_declared_exemption_is_stale(db: Path) -> None:
    """An exemption outlives the statement it excuses, and reads as a hole.

    The note the report prints is generated from ``UNATTESTED_STATEMENTS``, so a
    dead entry tells the reader the report ran something it did not.
    """

    _report, recorded = _report_with_trace(db)
    issuers = {where for where, _statement in recorded}

    assert set(UNATTESTED_STATEMENTS) <= issuers, (
        "render.UNATTESTED_STATEMENTS excuses statements this report never "
        f"issued: {sorted(set(UNATTESTED_STATEMENTS) - issuers)}"
    )


def test_the_digest_still_moves_when_a_query_text_moves(db: Path) -> None:
    """``test_provenance`` proves this over a caller's set; here over the real one.

    Not a second copy of that test: what is checked here is that the *enlarged*
    catalogue -- the measurement modules' statements folded in -- is inside the
    digest, so an edit to one of the lifted constants moves the sha256 the header
    publishes. A catalogue carried beside the digest instead of inside it would
    pass every other test in this file.
    """

    report, _recorded = _report_with_trace(db)
    definitions = dict(report.header.queries.definitions)

    assert query_catalogue(definitions).digest == report.header.queries.digest

    lifted = {
        name: text
        for name, text in definitions.items()
        if name in dict(REPORT_QUERY_SOURCES["ac9"])
        or name in dict(REPORT_QUERY_SOURCES["cohort"])
    }
    assert lifted, "the lifted measurement queries are not in the header's set"

    for name, text in lifted.items():
        edited = dict(definitions)
        edited[name] = text.replace("run_id", "run_id_2", 1)
        assert query_catalogue(edited).digest != report.header.queries.digest, (
            f"editing the text of {name} did not move the query digest"
        )
