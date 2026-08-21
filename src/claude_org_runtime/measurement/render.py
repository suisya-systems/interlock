"""G6 -- one computed report, two renderings, and the header both of them carry.

The failure this module is written against is a report that says two different
things to two readers. ``docs/measurement-harness.md`` section 6 requires the
provenance header "in both the Markdown and the JSON renderings", and the reason
the document says *both* rather than *a* rendering is that the two are produced
by different code paths in every implementation that has ever had them: the JSON
is dumped from a structure, the Markdown is written by hand, and the field added
last -- ``censored``, ``unbounded_missing``, the fingerprint mode -- reaches one
of them. The reader with the other one then makes a decision the report's own
data contradicts, and nothing in either artefact shows that this happened.

So the two renderings are not two renderers here. There is exactly one shape,
:meth:`MeasurementReport.as_mapping`, and both renderings are projections of it:
:func:`render_json` dumps it, :func:`render_markdown` flattens it with dotted
keys into one table (plus a fenced block per multi-line value, so a narrative or
a query's SQL text survives instead of being collapsed into a cell). A fact
cannot exist in one rendering and not the other, because neither rendering
chooses which facts it carries.

The banner is printed twice on purpose: once as plain lines at the top, where a
human reads it, and once as an ordinary ``header.banner`` row, where a diff of
two reports finds it. A banner that only exists as decoration is a banner that a
machine comparison of two reports cannot see.

**No verdict.** ``measurement-harness.md`` section 7 records ``Q-0005`` as open:
no exit criterion, sample-size minimum or acceptance threshold has been decided.
A renderer that printed one would decide it by inertia, which is why
:data:`NO_VERDICT_NOTE` is a field of the report rather than a docstring promise
and why ``tests/measurement/test_render.py`` greps both renderings for the
vocabulary a verdict would be written in.

**ASCII only.** Every string this module authors reaches ``--help`` and stdout on
a cp932 console, where one em-dash is a ``UnicodeEncodeError`` rather than a
degraded character -- and ``pytest``'s ``redirect_stdout`` captures UTF-8, so no
test that does not encode explicitly can catch it. Hyphens, never em-dashes.

**Read-only, and the clock is the caller's.** :func:`build_measurement_report`
takes the handle :func:`~claude_org_runtime.measurement.reader.open_for_measurement`
returns and issues ``SELECT`` statements through the modules it calls; ``now_ms``
is a parameter (``time-base-policy.md`` section 2 rule 2). Nothing here reads a
clock, and nothing here decides anything: it assembles measurements other modules
made and prints them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from claude_org_runtime.control_plane import policy

from . import ac9 as ac9_module
from . import cohort as cohort_module
from . import windows as windows_module
from .provenance import (
    FINGERPRINT_CONTENT,
    FINGERPRINT_MODES,
    FingerprintModeRefused,
    FixtureSuiteRef,
    ReportHeader,
    build_header,
    coverage_from_ac9,
    imputation_from_ac9,
)
from .reader import ControlPlaneRefusal

__all__ = [
    "BLOCK_LANGUAGE",
    "EMPTY_BLOCK",
    "FINGERPRINT_TABLES",
    "JSON",
    "MARKDOWN",
    "MeasurementReport",
    "NO_VERDICT_NOTE",
    "QUERY_CATALOGUE_LIMITATION",
    "RENDERINGS",
    "REPORT_KIND",
    "REPORT_QUERY_SOURCES",
    "RenderRefusal",
    "ReportSection",
    "SectionNameRefused",
    "DuplicateSectionRefused",
    "SectionsRequired",
    "UNATTESTED_STATEMENTS",
    "UnknownRendering",
    "V1ShadowInput",
    "V1ShadowInputRefused",
    "WINDOW_EPISODES_NOT_CLASSIFIED",
    "build_measurement_report",
    "cell",
    "flatten",
    "render",
    "render_json",
    "render_markdown",
    "report_query_definitions",
    "section_from_ac9",
    "section_from_window_declaration",
]


REPORT_KIND = "interlock-measurement-report"

MARKDOWN = "markdown"
JSON = "json"
RENDERINGS = (MARKDOWN, JSON)

#: Printed as a field of the report, in both renderings. Phrased without the
#: vocabulary a verdict would use, because the test that keeps verdicts out of
#: this module greps for that vocabulary and a note about verdicts written in it
#: would be indistinguishable from one.
NO_VERDICT_NOTE = (
    "Q-0005 is OPEN: no exit criterion, no sample-size minimum and no "
    "acceptance threshold has been decided, so this report states measurements "
    "and decides nothing about them (measurement-harness.md section 7)"
)

#: The tables this report's own figures are read off, which is the scope section
#: 6's content fingerprint has to cover: a fingerprint over fewer tables than the
#: report read would certify as identical two reads that differed in a table the
#: report used.
FINGERPRINT_TABLES = (
    "run",
    "ai_invocation",
    "incident",
    "policy_revision",
    "policy_detection_latency",
)

#: The measurement modules whose statements this report executes, each mapping
#: its own names to the very constants it hands ``execute``. Folded into the
#: header's ``query_definitions`` so section 6's catalogue carries the report's
#: measurement queries as text rather than a note saying it does not.
#:
#: This is a mapping of module name to catalogue, not a flat merge, because the
#: completeness test walks it: for each named module it re-derives, from the
#: module's own source, every statement that module executes and asserts each one
#: is in that module's catalogue. A module added to the report without being
#: added here fails the same test from the other side -- the report is built with
#: its statements traced, and a traced statement that is neither catalogued nor
#: listed in :data:`UNATTESTED_STATEMENTS` is a hole in the catalogue.
REPORT_QUERY_SOURCES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "cohort": cohort_module.QUERY_DEFINITIONS,
        "ac9": ac9_module.QUERY_DEFINITIONS,
    }
)

#: Every statement this report executes and this catalogue does **not** carry,
#: named by the function that issues it, with the reason. Section 6 asks for
#: every query as text; where a statement cannot be carried, the honest artefact
#: is a note that names exactly which one and why -- not silence, and not a
#: pasted copy that drifts from the text that ran.
#:
#: Two different reasons live here and the difference matters to a reader:
#: ``provenance``'s fingerprint statements have **no fixed text at all** (each is
#: composed from the columns the table itself reports, at call time, so the text
#: depends on the database being measured), and what the digest covers is
#: attested instead by ``header.db_fingerprint``'s own statement and by
#: ``header.fingerprint_mode``. The rest are statements that are still inline in
#: modules outside this catalogue's reach; each is the same one-line lift
#: ``ac9.py`` and ``cohort.py`` have had, and until it is made, naming their text
#: here would be the pasted copy this note exists to avoid.
UNATTESTED_STATEMENTS: Mapping[str, str] = MappingProxyType(
    {
        "provenance._columns_of": (
            "the table introspection behind the content fingerprint; composed "
            "per table at call time, so it has no fixed text to carry"
        ),
        "provenance._feed_rows": (
            "the fingerprint's per-table projection, composed from that table's "
            "own columns at call time; what the digest covers is attested by "
            "header.db_fingerprint's statement and header.fingerprint_mode"
        ),
        "provenance.build_header": (
            "the schema-identity pragmas (application_id, user_version), which "
            "the header carries as fields rather than as query text; this "
            "module's two measurement queries ARE in the catalogue"
        ),
        "migrator.applied_migrations": (
            "the schema_migration ledger read behind "
            "header.schema_migration_head, inline in control_plane/migrator.py"
        ),
        "policy.effective_revision_id": (
            "the revision in force at the period start, inline in "
            "control_plane/policy.py; the revision it resolved is carried as "
            "header.policy_revision_id"
        ),
        "policy.revision_over_period": (
            "the revision-change scan behind the header's banner, inline in "
            "control_plane/policy.py"
        ),
        "windows.default_grace_ms": (
            "the reconcile-period default read from the resolved revision, "
            "inline in windows.py; the value it returned is carried as "
            "sections.observation_window.facts.grace_ms with its source"
        ),
    }
)

#: Printed as a field of the report. Generated from :data:`UNATTESTED_STATEMENTS`
#: rather than written beside it, because a hand-written note and the list it
#: describes drift in one direction only: the note goes on claiming a
#: completeness the list stopped having.
QUERY_CATALOGUE_LIMITATION = (
    "query_definitions carries every statement the measurement modules this "
    "report runs execute -- "
    + ", ".join(f"{module}.py" for module in REPORT_QUERY_SOURCES)
    + " -- plus the header's own, as the text that ran rather than a copy of "
    f"it. {len(UNATTESTED_STATEMENTS)} "
    "further statements the report issues are not in it: each is named, with "
    "the reason it cannot be carried, under inputs.query_catalogue_exemptions"
)


def report_query_definitions() -> Mapping[str, str]:
    """Every catalogued statement this report runs, as one ``name -> text`` set.

    The merge is a function rather than a module-level constant so that a name
    used by two modules for two different texts is refused where a reader can
    see which report asked for it: ``build_header`` merges this with the
    header's own queries and refuses the same collision, and a constant built at
    import time would raise during import instead.
    """

    merged: dict[str, str] = {}
    for module, definitions in REPORT_QUERY_SOURCES.items():
        for name, text in definitions.items():
            existing = merged.get(name)
            if existing is not None and existing != text:
                raise RenderRefusal(
                    f"query name {name!r} is used by {module} for a text another "
                    "measurement module already claims; the header's digest "
                    "would be over one of them and the report would have run "
                    "the other"
                )
            merged[name] = text
    return MappingProxyType(merged)


#: Why the header's censoring counts are zero on a report of this shape. A zero
#: that means "no episode was classified" and a zero that means "no episode was
#: censored" are different statements, and section 3.5 makes the second one load
#: bearing, so the first is said out loud rather than left to be misread.
WINDOW_EPISODES_NOT_CLASSIFIED = (
    "no episode was classified in this report: this branch implements detectors "
    "and reporting, and the driver that produces episodes is not part of it, so "
    "the censored and censored_left counts are zero for want of episodes rather "
    "than because nothing was censored"
)

#: The fence language of a multi-line value's block. ``text`` rather than the
#: value's real syntax: a block tagged ``sql`` that is not SQL is a lie a reader
#: acts on, and the report holds both kinds.
BLOCK_LANGUAGE = "text"

#: What an empty mapping renders as. An empty block is a fact -- "the report has
#: no unmatched episodes" -- and dropping its row would make it indistinguishable
#: from a field nobody computed.
EMPTY_BLOCK = "(none)"

_TABLE_HEAD = ("| Fact | Value |", "| --- | --- |")


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


class RenderRefusal(ControlPlaneRefusal):
    """Base for every refusal this module raises."""


class SectionNameRefused(RenderRefusal):
    """A section name that cannot be a stable key in both renderings."""


class DuplicateSectionRefused(RenderRefusal):
    """Two sections under one name: one of them would be invisible in the JSON."""


class SectionsRequired(RenderRefusal):
    """A header with no section is provenance for a measurement nobody made."""


class UnknownRendering(RenderRefusal):
    """A rendering this module does not produce."""


class V1ShadowInputRefused(RenderRefusal):
    """A shadow input that states neither its source nor its absence."""


# --------------------------------------------------------------------------
# the v1 shadow input
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class V1ShadowInput:
    """The v1 run ids a report was given, or a stated absence of them.

    ``D-0013`` leaves no v1-owned run in this database to find, so the
    ``v1_owned`` exclusion bucket can only come from outside
    (``cohort.select_cohort``'s *v1_shadow_run_ids*). Passing nothing yields an
    empty bucket, and an empty bucket rendered without saying where it came from
    reads as "v1 owned no run in this period" -- an assertion the report is in no
    position to make. So the input is either observed from a named source or
    absent for a stated reason, and the report prints which.
    """

    source: str | None
    run_ids: tuple[str, ...]
    absent_reason: str | None

    @classmethod
    def observed(cls, source: str, run_ids: Iterable[str]) -> "V1ShadowInput":
        if not source.strip():
            raise V1ShadowInputRefused(
                "name where the v1 shadow run ids came from; an unnamed source "
                "cannot be checked by a reader recomputing this report"
            )
        return cls(
            source=source, run_ids=tuple(str(run_id) for run_id in run_ids),
            absent_reason=None,
        )

    @classmethod
    def absent(cls, reason: str) -> "V1ShadowInput":
        if not reason.strip():
            raise V1ShadowInputRefused(
                "state why this report has no v1 shadow input; an unexplained "
                "absence is indistinguishable from a report that forgot to pass "
                "one, and the two produce the same empty v1_owned bucket"
            )
        return cls(source=None, run_ids=(), absent_reason=reason)

    def as_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "source": self.source,
                "absent_reason": self.absent_reason,
                "run_id_count": len(self.run_ids),
                "run_ids": list(self.run_ids),
            }
        )


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportSection:
    """One measurement's facts, plus the module's own rendering of them.

    ``facts`` is the machine-comparable half and ``narrative`` the human half,
    and both travel in **both** renderings: a JSON consumer that could not see
    the narrative would be reading a different report from the operator, which
    is the failure the module docstring names, one level down.
    """

    name: str
    title: str
    facts: Mapping[str, Any]
    narrative: str | None = None

    def __post_init__(self) -> None:
        # The name becomes a dotted key in the Markdown and an object key in the
        # JSON. A name carrying a dot would produce a Markdown key that parses
        # back as two levels of nesting the JSON does not have, so two readers
        # comparing the renderings would disagree about the shape.
        if not self.name or not self.name.strip():
            raise SectionNameRefused("a section needs a name to be keyed by")
        if any(character in self.name for character in ". |`"):
            raise SectionNameRefused(
                f"section name {self.name!r} carries a dot, a space, a pipe or a "
                "backtick; those are the Markdown rendering's own syntax, and a "
                "key that collides with it renders as a different key than the "
                "JSON carries"
            )
        if not self.title.strip():
            raise SectionNameRefused(
                f"section {self.name!r} needs a title; a section a reader cannot "
                "name is a table of numbers about nothing"
            )

    def as_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "title": self.title,
                "narrative": self.narrative,
                "facts": dict(self.facts),
            }
        )


def section_from_ac9(
    report: "ac9_module.Ac9Report", cohort: "cohort_module.RunCohort"
) -> ReportSection:
    """Section 2's measurement as facts, with ``render_ac9_report`` as narrative.

    The cohort travels with the AC-9 numbers because ``D-0038`` makes the
    excluded-reason breakdown required output -- "a reduction rate printed
    without them is not a valid report" -- and a JSON consumer reading only this
    section must be as unable to print the rate without them as an operator
    reading the text is.

    Every number here is read off *report* and *cohort*; nothing is recomputed.
    A second computation of a figure the report already carries is a second
    figure, and the day they disagree there is no way to tell which one the
    narrative beside them came from.
    """

    figures = {
        figure.label.replace(" ", "_"): {
            "kind": figure.kind,
            "value": figure.value,
            "basis": figure.basis,
        }
        for figure in report.figures()
    }
    return ReportSection(
        name="ac9",
        title="AC-9 - AI prompts and output tokens",
        narrative=ac9_module.render_ac9_report(report),
        facts={
            "cohort": {
                "denominator": cohort.denominator,
                "run_ids": list(cohort.run_ids),
                "excluded": dict(cohort.excluded_counts()),
            },
            "series": {
                "model_response_total": report.model_response_total,
                "invocation_count": report.invocation_count,
                "attempt_total": report.attempt_total,
                "observed_output_tokens": report.observed_output_tokens,
                "input_tokens_total": report.input_tokens_total,
                "cache_read_tokens_total": report.cache_read_tokens_total,
                "unattributed_invocations": report.unattributed_invocations,
            },
            "coverage": {
                "covered_count": report.covered_count,
                "missing_count": report.missing_count,
                "ratio": report.coverage_ratio,
                "is_complete": report.coverage_is_complete,
            },
            "figures": figures,
            "prompt_half": {
                "model_responses_per_100_runs": report.model_responses_per_100_runs,
                "prompt_reduction": report.prompt_reduction,
            },
            "imputation": {
                "bounded_output_tokens": report.bounded_output_tokens,
                "sensitivity_output_tokens": report.sensitivity_output_tokens,
                "covered_p95_output_tokens": report.covered_p95_output_tokens,
                "unbounded_missing": list(report.unbounded_missing),
                "unconfirmed_response_count": list(
                    report.unconfirmed_response_count
                ),
                "supports_acceptance_claim": report.supports_acceptance_claim,
            },
            "ac1_violations": list(report.ac1_violations),
            "baseline": {
                "source": report.baseline.source,
                "completed_runs": report.baseline.completed_runs,
                "model_responses": report.baseline.model_responses,
                "output_tokens": report.baseline.output_tokens,
                "tool_calls": report.baseline.tool_calls,
                "cache_read_tokens": report.baseline.cache_read_tokens,
            },
        },
    )


def section_from_window_declaration(
    *, grace_ms: int, grace_source: str, episodes_classified: int
) -> ReportSection:
    """The observation-window grace this report was computed under.

    Grace is declared **per report** (section 3.5), so it belongs in the report
    even when the report classified no episode: a reader comparing two reports
    has to be able to see that the window moved, and a grace that is only visible
    when there are episodes is invisible on exactly the report whose emptiness it
    might explain.
    """

    return ReportSection(
        name="observation_window",
        title="Observation window - the grace this report was computed under",
        narrative=None,
        facts={
            "grace_ms": grace_ms,
            "grace_source": grace_source,
            "episodes_classified": episodes_classified,
            "scope": WINDOW_EPISODES_NOT_CLASSIFIED,
        },
    )


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementReport:
    """A provenance header and the sections measured under it.

    The header is not optional and is not a section: section 6 makes it the
    thing that turns the numbers into evidence, and a section list a caller
    could assemble without one would produce a report that is an opinion.
    """

    header: ReportHeader
    sections: tuple[ReportSection, ...]

    def __post_init__(self) -> None:
        if not self.sections:
            raise SectionsRequired(
                "a report with no section is a provenance header for a "
                "measurement nobody made; give it the sections it is provenance "
                "for"
            )
        seen: set[str] = set()
        for section in self.sections:
            if section.name in seen:
                raise DuplicateSectionRefused(
                    f"two sections are named {section.name!r}; the JSON "
                    "rendering keys sections by name, so the second one would "
                    "replace the first and the Markdown would still show both"
                )
            seen.add(section.name)

    def as_mapping(self) -> Mapping[str, Any]:
        """The one shape both renderings are projections of.

        Ordered so that a reader who stops after the first screen has stopped
        after the verdict note and the header's homogeneity banner, not before
        them.
        """

        return MappingProxyType(
            {
                "report_kind": REPORT_KIND,
                "verdict": NO_VERDICT_NOTE,
                "header": self.header.as_mapping(),
                "sections": {
                    section.name: section.as_mapping()
                    for section in self.sections
                },
            }
        )

    def section(self, name: str) -> ReportSection:
        for section in self.sections:
            if section.name == name:
                return section
        raise SectionNameRefused(
            f"this report carries no section named {name!r}; it carries "
            f"{', '.join(section.name for section in self.sections) or '(none)'}"
        )


def build_measurement_report(
    connection,
    *,
    db_path: str,
    period_start_ms: int,
    period_end_ms: int,
    now_ms: int,
    fixture_suite: FixtureSuiteRef,
    v1_shadow: V1ShadowInput,
    grace_ms: int | None = None,
    fingerprint_mode: str = FINGERPRINT_CONTENT,
    baseline: "ac9_module.MeasuredBaseline" = ac9_module.V1_MEASURED_BASELINE,
) -> MeasurementReport:
    """Assemble the report a caller can render, deciding nothing.

    *connection* must be
    :func:`~claude_org_runtime.measurement.reader.open_for_measurement`'s handle:
    every module called below issues ``SELECT`` statements through it and this
    function opens nothing of its own, so there is no path here that could
    acquire a writable one.

    *now_ms* is the caller's clock, read once at the process boundary and passed
    down (``migrator._require_epoch_ms``'s discipline, ``time-base-policy.md``
    section 2 rule 2). Nothing below this signature reads a clock.

    *fixture_suite* and *v1_shadow* are required keywords with no default, for
    the reason ``build_header`` gives about its own: a defaulted corpus reference
    or a defaulted shadow input would go missing on exactly the report that
    needed it, and both are declared per report rather than derived from the
    database.

    *grace_ms* declares the observation-window grace. ``None`` resolves it from
    the policy revision in force (``windows.default_grace_ms``, one reconcile
    period) and stamps the source as such, which is a derivation the report
    records rather than a constant it hides.

    The censoring counts on the header are zero here and
    :data:`WINDOW_EPISODES_NOT_CLASSIFIED` says why: this branch implements
    detectors and reporting, and the driver that produces episodes is not part of
    it, so there is nothing to censor yet.
    """

    if fingerprint_mode not in FINGERPRINT_MODES:
        # Refused here as well as inside fingerprint_database, so a caller that
        # mistypes the mode learns it before the cohort scan rather than after.
        raise FingerprintModeRefused(
            f"fingerprint mode {fingerprint_mode!r} is not one of "
            f"{', '.join(FINGERPRINT_MODES)}"
        )

    # Every policy read binds a caller-resolved revision (D-0031's corollary),
    # and the revision this report binds is the one in force at its period's
    # start -- the instant its earliest judgement would have been made under.
    revision_id = policy.effective_revision_id(connection, now_ms=period_start_ms)

    if grace_ms is None:
        resolved_grace_ms = windows_module.default_grace_ms(
            connection, revision_id=revision_id
        )
        grace_source = windows_module.GRACE_REVISION_RECONCILE_PERIOD
    else:
        resolved_grace_ms = grace_ms
        grace_source = windows_module.GRACE_DECLARED

    selected = cohort_module.select_cohort(
        connection,
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        now_ms=now_ms,
        v1_shadow_run_ids=v1_shadow.run_ids,
    )
    measured = ac9_module.measure_ac9(
        connection, selected, now_ms=now_ms, baseline=baseline
    )

    header = build_header(
        connection,
        db_path=db_path,
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        generated_at_ms=now_ms,
        policy_revision_id=revision_id,
        fingerprint_tables=FINGERPRINT_TABLES,
        query_definitions=report_query_definitions(),
        fixture_suite=fixture_suite,
        imputation=imputation_from_ac9(measured),
        coverage=coverage_from_ac9(measured, selected),
        censored=0,
        censored_left=0,
        unmatched={},
        fingerprint_mode=fingerprint_mode,
    )

    inputs = ReportSection(
        name="inputs",
        title="Inputs declared for this report",
        narrative=None,
        facts={
            "v1_shadow": v1_shadow.as_mapping(),
            "query_catalogue_limitation": QUERY_CATALOGUE_LIMITATION,
            "query_catalogue_exemptions": dict(UNATTESTED_STATEMENTS),
        },
    )
    return MeasurementReport(
        header=header,
        sections=(
            inputs,
            section_from_window_declaration(
                grace_ms=resolved_grace_ms,
                grace_source=grace_source,
                episodes_classified=0,
            ),
            section_from_ac9(measured, selected),
        ),
    )


# --------------------------------------------------------------------------
# the two renderings
# --------------------------------------------------------------------------


def render(report: MeasurementReport, rendering: str) -> str:
    """Render *report* in the named rendering.

    The dispatch is here so that a caller choosing a rendering from a flag
    cannot reach one of them and miss the other's existence.
    """

    if rendering == MARKDOWN:
        return render_markdown(report)
    if rendering == JSON:
        return render_json(report)
    raise UnknownRendering(
        f"{rendering!r} is not one of {', '.join(RENDERINGS)}"
    )


def render_json(report: MeasurementReport) -> str:
    """The JSON rendering: :meth:`MeasurementReport.as_mapping`, verbatim.

    ``sort_keys`` is off, because the mapping's order is the reading order -- the
    verdict note and the header's banner come first by construction and sorting
    would bury them. ``ensure_ascii`` is on: this reaches a cp932 console, where
    a non-encodable character raises rather than degrades.
    """

    return json.dumps(_plain(report.as_mapping()), indent=2, ensure_ascii=True)


def render_markdown(report: MeasurementReport) -> str:
    """The Markdown rendering: the same mapping, flattened.

    Every leaf of the mapping reaches the output, single-line values as table
    rows and multi-line ones as fenced blocks keyed by the same dotted name. The
    split exists because a cell cannot hold a newline: collapsing a narrative or
    a query's SQL into one line would leave the Markdown reader with a fact the
    JSON reader can act on and they cannot.
    """

    lines: list[str] = [f"# {REPORT_KIND}", ""]
    lines.extend(report.header.banner())
    lines.append("")
    lines.extend(_TABLE_HEAD)

    blocks: list[tuple[str, str]] = []
    for key, value in flatten(report.as_mapping()):
        if isinstance(value, str) and "\n" in value:
            blocks.append((key, value))
            continue
        lines.append(f"| `{key}` | {cell(value)} |")

    for key, value in blocks:
        lines.extend(("", f"### fact `{key}`", "```" + BLOCK_LANGUAGE))
        lines.extend(value.split("\n"))
        lines.append("```")
    return "\n".join(lines) + "\n"


def flatten(mapping: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """``mapping`` as dotted key / leaf pairs, in the mapping's own order.

    Mappings recurse and everything else is a leaf, including lists: a list of
    scalars is one fact ("these ids"), and exploding it into indexed keys would
    make two reports with the same ids in a different order look different in the
    Markdown while comparing equal in the JSON.
    """

    pairs: list[tuple[str, Any]] = []
    for key, value in mapping.items():
        pairs.extend(_flatten(str(key), value))
    return tuple(pairs)


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        if not value:
            # An empty block is a fact and keeps its row. Dropping it would make
            # "nothing was unmatched" indistinguishable from "nobody computed
            # unmatched", which is the difference the header exists to state.
            return [(prefix, None)]
        flattened: list[tuple[str, Any]] = []
        for key, item in value.items():
            flattened.extend(_flatten(f"{prefix}.{key}", item))
        return flattened
    return [(prefix, value)]


def cell(value: Any) -> str:
    """One Markdown table cell: ASCII-shaped, pipe-safe, single-line.

    A ``|`` inside a value ends the cell and shifts every later column one to the
    left, which is a rendering that silently mislabels values rather than one
    that looks broken -- so it is escaped rather than trusted not to appear.
    ``None`` is :data:`EMPTY_BLOCK` and not an empty cell, because an empty cell
    reads as a field nobody filled in.
    """

    if value is None:
        rendered = EMPTY_BLOCK
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (list, tuple)):
        rendered = (
            ", ".join(cell(item) for item in value) if value else EMPTY_BLOCK
        )
    else:
        rendered = str(value)
    return rendered.replace("|", "\\|").replace("\n", " ").strip()


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value
