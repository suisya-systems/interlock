"""The one claim the two renderings make: they say the same thing.

Section 6 requires the provenance header "in both the Markdown and the JSON
renderings", and the failure that requirement is written against is not a missing
header -- it is a header that is *almost* the same in both, one field short,
because the two renderings were written by two hands. A test that asserted a
hand-written list of fields against each rendering would inherit the same defect:
it checks the fields whoever wrote it remembered.

So :func:`test_the_two_renderings_carry_the_same_facts` compares the artefacts
to each other. It parses the Markdown back into a flat mapping with a parser
written here -- it knows the table syntax, not the field list -- and walks the
JSON with a traversal written here, and asserts the two mappings are **equal**.
Neither side is a copy of the report's field list, so a field that reaches one
rendering and not the other fails it no matter which field it is, including one
added after this file was written.
:func:`test_a_fact_dropped_from_the_markdown_is_caught` is the mutation of that
test kept as a test: it renders a report with one row deleted and asserts the
comparison fails, so the comparison cannot pass by comparing nothing.

The rest is adversarial around the edges the renderings actually have:

* a **multi-line** value (the AC-9 narrative, a query's SQL) cannot live in a
  Markdown cell, so it is rendered as a fenced block -- and the equality test
  covers it, which is what stops the Markdown from quietly collapsing a narrative
  into one line while the JSON keeps it;
* a value carrying a ``|`` would shift every later column, so it is escaped, and
  a test puts a pipe in a fact and re-parses;
* the **aggregate** fingerprint mode has to be stamped as the weaker thing in
  both renderings, proved by rendering the same database both ways;
* **no verdict**: both renderings of a report are grepped, with word boundaries,
  for the vocabulary a verdict would be written in (``Q-0005`` is open).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import pytest

from claude_org_runtime.control_plane import ai_invocation, policy
from claude_org_runtime.control_plane.migrator import (
    create_production_control_plane,
)
from claude_org_runtime.measurement import ac9 as ac9_module
from claude_org_runtime.measurement import cohort as cohort_module
from claude_org_runtime.measurement import render as render_module
from claude_org_runtime.measurement import windows as windows_module
from claude_org_runtime.measurement.provenance import (
    AGGREGATE_STATEMENT,
    CONTENT_STATEMENT,
    FINGERPRINT_AGGREGATE,
    FINGERPRINT_CONTENT,
    FixtureSuiteRef,
    fingerprint_database,
)
from claude_org_runtime.measurement.reader import open_for_measurement
from claude_org_runtime.measurement.render import (
    EMPTY_BLOCK,
    JSON,
    MARKDOWN,
    DuplicateSectionRefused,
    MeasurementReport,
    ReportSection,
    SectionNameRefused,
    SectionsRequired,
    UnknownRendering,
    V1ShadowInput,
    V1ShadowInputRefused,
    build_measurement_report,
    cell,
    render,
    render_json,
    render_markdown,
)

T0 = 1_700_000_000_000
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS
GENERATED_AT = PERIOD_END + 60_000

#: The vocabulary a verdict would be written in. Word boundaries, because
#: "passed" must fail this and "surpassed" is not the claim being policed.
VERDICT_WORDS = re.compile(
    r"\b(pass|passes|passed|passing|fail|fails|failed|failing|go|no-go|nogo)\b",
    re.IGNORECASE,
)

#: Section 6's fields, as the document lists them, prefixed with the block the
#: report puts them in. Written from the document rather than read off the
#: implementation, so a field deleted from the header fails here.
SECTION_6_HEADER_FACTS = (
    "header.period_start_ms",
    "header.period_end_ms",
    "header.generated_at_ms",
    "header.tool_version",
    "header.db_path",
    "header.application_id",
    "header.user_version",
    "header.schema_migration_head.version",
    "header.schema_migration_head.name",
    "header.db_fingerprint",
    "header.fingerprint_mode",
    "header.policy_revision_id",
    "header.detector_versions",
    "header.adapter_versions",
    "header.query_definitions_sha256",
    "header.fixture_suite_ref.commit",
    "header.fixture_suite_ref.positive",
    "header.fixture_suite_ref.negative",
    "header.imputation_rule.bounded",
    "header.imputation_rule.sensitivity",
    "header.imputation_rule.unbounded_missing",
    "header.coverage.covered",
    "header.coverage.total",
    "header.censored",
    "header.censored_left",
    "header.unmatched",
    "header.banner",
)


# --------------------------------------------------------------------------
# the fixture database, built through the real writers
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    cp = sqlite3.connect(path, isolation_level=None)
    try:
        cp.execute("PRAGMA foreign_keys = ON")
        cp.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
            " VALUES ('run-1', 'completed', ?, ?)",
            (PERIOD_START + 1_000, PERIOD_START + 2_000),
        )
        cp.execute(
            """
            INSERT INTO incident (incident_id, run_id, session_id, fact_state,
                                  detector_version, dedup_key, created_at_ms,
                                  updated_at_ms)
            VALUES ('inc-1', 'run-1', NULL, 'stalled', 'detector/1',
                    'dedup/inc-1', ?, ?)
            """,
            (PERIOD_START + 1_500, PERIOD_START + 1_500),
        )
        ai_invocation.start_invocation(
            cp,
            invocation_id="inv-1",
            provider="anthropic",
            model="a-model",
            adapter_version="adapter/1",
            started_at_ms=PERIOD_START + 1_600,
            incident_id="inc-1",
            run_id="run-1",
            max_output_tokens=4096,
        )
        ai_invocation.complete_invocation(
            cp,
            invocation_id="inv-1",
            usage=ai_invocation.ProviderUsage.reported(
                adapter_version="adapter/1",
                output_tokens=512,
                input_tokens=2_048,
                cache_read_tokens=9_000,
            ),
            model_response_count=3,
            finished_at_ms=PERIOD_START + 1_900,
        )
    finally:
        cp.close()
    return path


def report_over(
    path: Path,
    *,
    fingerprint_mode: str = FINGERPRINT_CONTENT,
    grace_ms: int | None = None,
    v1_shadow: V1ShadowInput | None = None,
) -> MeasurementReport:
    connection = open_for_measurement(path)
    try:
        return build_measurement_report(
            connection,
            db_path=str(path),
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=GENERATED_AT,
            fixture_suite=FixtureSuiteRef.absent("no corpus in this test"),
            v1_shadow=(
                V1ShadowInput.absent("no shadow input in this test")
                if v1_shadow is None
                else v1_shadow
            ),
            grace_ms=grace_ms,
            fingerprint_mode=fingerprint_mode,
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# two parsers written here, neither one a copy of the report's field list
# --------------------------------------------------------------------------

_ROW = re.compile(r"^\| `(?P<key>[^`]+)` \| (?P<value>.*) \|$")
_BLOCK = re.compile(r"^### fact `(?P<key>[^`]+)`$")


def parse_markdown(text: str) -> Mapping[str, Any]:
    """Read a rendered Markdown report back into ``key -> value``.

    Knows the table and fence syntax and nothing about which facts exist, which
    is what makes it usable as one side of an equality assertion.
    """

    facts: dict[str, Any] = {}
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        row = _ROW.match(line)
        if row is not None and row.group("key") != "Fact":
            key = row.group("key")
            assert key not in facts, f"{key} appears twice in the Markdown"
            facts[key] = row.group("value")
            index += 1
            continue
        block = _BLOCK.match(line)
        if block is not None:
            fence = lines[index + 1]
            assert fence.startswith("```"), fence
            end = lines.index("```", index + 2)
            facts[block.group("key")] = "\n".join(lines[index + 2 : end])
            index = end + 1
            continue
        index += 1
    return facts


def walk_json(payload: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a parsed JSON report into ``dotted key -> rendered value``.

    Mappings recurse; every other leaf is rendered with the module's own cell
    formatter, except a multi-line string, which the Markdown carries verbatim in
    a fenced block and is therefore compared verbatim.
    """

    flat: dict[str, Any] = {}
    if isinstance(payload, dict):
        if not payload:
            return {prefix: cell(None)}
        for key, value in payload.items():
            flat.update(walk_json(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(payload, str) and "\n" in payload:
        return {prefix: payload}
    return {prefix: cell(payload)}


# --------------------------------------------------------------------------
# the headline claim
# --------------------------------------------------------------------------


def test_the_two_renderings_carry_the_same_facts(db: Path) -> None:
    report = report_over(db)

    from_markdown = parse_markdown(render_markdown(report))
    from_json = walk_json(json.loads(render_json(report)))

    assert from_markdown == from_json
    # and the comparison is over a real report, not an empty one
    assert len(from_json) > 40


def test_a_fact_dropped_from_the_markdown_is_caught(db: Path) -> None:
    """The mutation of the test above, kept as a test.

    Without this, a comparison that silently compared two empty mappings would
    pass and nobody would know the header had stopped being rendered.
    """

    report = report_over(db)
    markdown = render_markdown(report)
    mutilated = "\n".join(
        line
        for line in markdown.split("\n")
        if not line.startswith("| `header.db_fingerprint` |")
    )

    assert parse_markdown(mutilated) != walk_json(json.loads(render_json(report)))


@pytest.mark.parametrize("fact", SECTION_6_HEADER_FACTS)
def test_every_section_6_header_field_is_in_both_renderings(
    db: Path, fact: str
) -> None:
    report = report_over(db)

    assert fact in parse_markdown(render_markdown(report))
    assert fact in walk_json(json.loads(render_json(report)))


def test_the_narrative_survives_as_a_block_rather_than_a_collapsed_cell(
    db: Path,
) -> None:
    """A multi-line value is the case a Markdown table cannot hold.

    The AC-9 narrative is many lines; a renderer that put it in a cell would
    collapse it to one line and the Markdown reader would lose the four figures
    section 2.4 requires to be printed together.
    """

    report = report_over(db)
    key = "sections.ac9.narrative"

    rendered = parse_markdown(render_markdown(report))[key]
    assert "\n" in rendered
    assert rendered == report.section("ac9").narrative
    assert rendered == walk_json(json.loads(render_json(report)))[key]


def test_a_pipe_in_a_value_does_not_shift_the_columns() -> None:
    """A ``|`` inside a fact ends the cell unless it is escaped.

    The damage is not a broken-looking table: every column after it moves one to
    the left, so values get printed under other fields' names.
    """

    section = ReportSection(
        name="probe",
        title="a pipe in a value",
        facts={"text": "left | right", "after": "unshifted"},
    )
    markdown = render_markdown(
        MeasurementReport(header=_HeaderStub(), sections=(section,))
    )

    facts = parse_markdown(markdown)
    assert facts["sections.probe.facts.after"] == "unshifted"
    assert facts["sections.probe.facts.text"] == "left \\| right"


# --------------------------------------------------------------------------
# the fingerprint mode, stamped in both renderings
# --------------------------------------------------------------------------


def test_aggregate_mode_is_stamped_as_the_weaker_one_in_both_renderings(
    db: Path,
) -> None:
    strong = report_over(db, fingerprint_mode=FINGERPRINT_CONTENT)
    weak = report_over(db, fingerprint_mode=FINGERPRINT_AGGREGATE)

    for rendering in (render_markdown, render_json):
        strong_text = rendering(strong)
        weak_text = rendering(weak)
        assert FINGERPRINT_AGGREGATE in weak_text
        assert AGGREGATE_STATEMENT.replace("\n", " ") in weak_text.replace(
            "\n", " "
        )
        assert "does NOT establish identity of content" in weak_text
        assert "does NOT establish identity of content" not in strong_text
        assert CONTENT_STATEMENT.replace("\n", " ") in strong_text.replace(
            "\n", " "
        )

    weak_facts = walk_json(json.loads(render_json(weak)))
    assert weak_facts["header.fingerprint_mode"] == FINGERPRINT_AGGREGATE
    assert weak_facts["header.fingerprint_establishes_content_identity"] == "false"


def test_an_unknown_fingerprint_mode_is_refused_before_the_cohort_scan(
    db: Path,
) -> None:
    with pytest.raises(Exception) as raised:
        report_over(db, fingerprint_mode="approximate")
    assert "approximate" in str(raised.value)


# --------------------------------------------------------------------------
# no verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rendering", [render_markdown, render_json])
def test_no_verdict_word_appears_in_either_rendering(db: Path, rendering) -> None:
    text = rendering(report_over(db))

    found = VERDICT_WORDS.findall(text)
    assert not found, f"verdict vocabulary in the rendering: {sorted(set(found))}"
    assert "Q-0005" in text


@pytest.mark.parametrize("rendering", [render_markdown, render_json])
def test_both_renderings_encode_to_ascii_and_to_cp932(db: Path, rendering) -> None:
    text = rendering(report_over(db))

    assert text.isascii()
    text.encode("cp932")


# --------------------------------------------------------------------------
# the declared inputs
# --------------------------------------------------------------------------


def test_grace_from_the_revision_is_stamped_as_derived_and_a_declared_one_is_not(
    db: Path,
) -> None:
    """Grace is declared per report; where it is derived, the report says so.

    A report that printed a derived grace as though it had been declared would
    let a policy change move every observation window with nothing in the
    artefact recording that the number came from the policy at all.
    """

    connection = open_for_measurement(db)
    try:
        revision = policy.effective_revision_id(connection, now_ms=PERIOD_START)
        declared_by_policy = int(
            connection.execute(
                "SELECT DISTINCT reconcile_period_ms FROM policy_detection_latency"
                " WHERE revision_id = ?",
                (revision,),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    derived = report_over(db).section("observation_window").facts
    assert derived["grace_ms"] == declared_by_policy
    assert derived["grace_source"] == "revision_reconcile_period"

    stated = report_over(db, grace_ms=1_234).section("observation_window").facts
    assert stated["grace_ms"] == 1_234
    assert stated["grace_source"] == "declared"


def test_a_negative_declared_grace_is_refused_before_the_cohort_scan(
    db: Path,
) -> None:
    """A grace the window model refuses must not reach the report either.

    windows.episode_window rejects grace_ms < 0 (it shortens the window below
    the budget the detector is held to), so a report built with one attests, in
    its section 6 provenance, to a configuration that could never have produced
    a valid window -- and this branch classifies no episodes, so nothing
    downstream would raise and the report would render clean. The library entry
    point has to refuse it, with the window model's own type rather than a
    second copy of the rule.
    """

    with pytest.raises(windows_module.WindowRefusal):
        report_over(db, grace_ms=-1)


def test_the_grace_rule_the_report_enforces_is_the_window_model_s_own(
    db: Path, monkeypatch
) -> None:
    """The two must not be able to drift about what a valid grace is.

    Bound to the code: windows.require_grace_ms is replaced, and the report is
    asserted to refuse what the replacement refuses. A second copy of the rule
    inside render.py passes the test above and fails this one.
    """

    def only_multiples_of_seven(grace_ms: int) -> None:
        if grace_ms % 7:
            raise windows_module.WindowRefusal("not a multiple of seven")

    monkeypatch.setattr(
        windows_module, "require_grace_ms", only_multiples_of_seven
    )
    with pytest.raises(windows_module.WindowRefusal):
        report_over(db, grace_ms=1_234)
    assert (
        report_over(db, grace_ms=14).section("observation_window").facts[
            "grace_ms"
        ]
        == 14
    )


def test_a_negative_grace_is_refused_by_the_section_builder_too(db: Path) -> None:
    """The section builder is a public entry point of its own (``__all__``).

    Validating only in build_measurement_report would leave a caller who
    assembles a MeasurementReport from sections -- which this module exports the
    pieces for -- able to stamp the invalid grace anyway.
    """

    with pytest.raises(windows_module.WindowRefusal):
        render_module.section_from_window_declaration(
            grace_ms=-1,
            grace_source=windows_module.GRACE_DECLARED,
            episodes_classified=0,
        )


def test_the_zero_censoring_counts_say_why_they_are_zero(db: Path) -> None:
    """Zero censored episodes and zero episodes are different statements."""

    facts = walk_json(json.loads(render_json(report_over(db))))

    assert facts["header.censored"] == "0"
    assert facts["header.censored_left"] == "0"
    assert facts["sections.observation_window.facts.episodes_classified"] == "0"
    assert "for want of episodes" in (
        facts["sections.observation_window.facts.scope"]
    )


def test_an_absent_shadow_input_is_stated_rather_than_shown_as_an_empty_bucket(
    db: Path,
) -> None:
    facts = walk_json(json.loads(render_json(report_over(db))))

    assert facts["sections.inputs.facts.v1_shadow.source"] == EMPTY_BLOCK
    assert facts["sections.inputs.facts.v1_shadow.run_id_count"] == "0"
    assert "no shadow input" in facts["sections.inputs.facts.v1_shadow.absent_reason"]
    assert facts["header.coverage.excluded.v1_owned"] == "0"


def test_a_shadow_input_excludes_its_runs_and_names_its_source(db: Path) -> None:
    report = report_over(
        db, v1_shadow=V1ShadowInput.observed("v1-export.json", ["run-9"])
    )
    facts = walk_json(json.loads(render_json(report)))

    assert facts["sections.inputs.facts.v1_shadow.source"] == "v1-export.json"
    assert facts["sections.inputs.facts.v1_shadow.run_ids"] == "run-9"
    assert facts["header.coverage.excluded.v1_owned"] == "1"


def test_a_shadow_input_with_no_source_and_no_reason_is_refused() -> None:
    with pytest.raises(V1ShadowInputRefused):
        V1ShadowInput.observed("  ", ["run-1"])
    with pytest.raises(V1ShadowInputRefused):
        V1ShadowInput.absent("")


def test_the_query_catalogue_limitation_travels_with_the_report(db: Path) -> None:
    """Section 6 asks for every query as text; this report cannot give them all.

    The limitation is in the rendered artefact rather than only in a docstring,
    because the reader who would be misled is the one holding the report -- and
    so is the list of what is missing: a note saying "some statements are not
    carried" without naming them leaves the reader unable to tell whether the
    one they care about is among them.
    """

    facts = walk_json(json.loads(render_json(report_over(db))))

    assert (
        facts["sections.inputs.facts.query_catalogue_limitation"]
        == render_module.QUERY_CATALOGUE_LIMITATION
    )
    for where, why in render_module.UNATTESTED_STATEMENTS.items():
        assert facts[f"sections.inputs.facts.query_catalogue_exemptions.{where}"] == why


# --------------------------------------------------------------------------
# the report's own refusals
# --------------------------------------------------------------------------


class _HeaderStub:
    """A header substitute for the tests that are about the section machinery.

    It carries only what the renderer asks of a header, so a test about pipes in
    a cell does not need a migrated database.
    """

    def banner(self) -> tuple[str, ...]:
        return ("period is HOMOGENEOUS: a stub",)

    def as_mapping(self) -> Mapping[str, Any]:
        return {"stub": True}


def test_a_report_with_no_section_is_refused() -> None:
    with pytest.raises(SectionsRequired):
        MeasurementReport(header=_HeaderStub(), sections=())


def test_two_sections_under_one_name_are_refused() -> None:
    section = ReportSection(name="ac9", title="one", facts={})
    twin = ReportSection(name="ac9", title="two", facts={})
    with pytest.raises(DuplicateSectionRefused):
        MeasurementReport(header=_HeaderStub(), sections=(section, twin))


@pytest.mark.parametrize("name", ["", "  ", "a.b", "a b", "a|b", "a`b"])
def test_a_section_name_that_collides_with_the_key_syntax_is_refused(
    name: str,
) -> None:
    with pytest.raises(SectionNameRefused):
        ReportSection(name=name, title="t", facts={})


def test_a_section_without_a_title_is_refused() -> None:
    with pytest.raises(SectionNameRefused):
        ReportSection(name="ok", title="   ", facts={})


def test_asking_for_a_section_the_report_does_not_carry_is_refused(
    db: Path,
) -> None:
    with pytest.raises(SectionNameRefused):
        report_over(db).section("latency")


def test_an_unknown_rendering_is_refused(db: Path) -> None:
    report = report_over(db)
    assert render(report, MARKDOWN) == render_markdown(report)
    assert render(report, JSON) == render_json(report)
    with pytest.raises(UnknownRendering):
        render(report, "html")


# --------------------------------------------------------------------------
# the AC-9 section binds to the measurement, not to a second computation
# --------------------------------------------------------------------------


def test_the_ac9_section_reports_the_numbers_the_measurement_made(db: Path) -> None:
    """Every figure is read off the report; nothing here is recomputed.

    The test asserts against ``measure_ac9``'s own output rather than against
    hand-written numbers, so a section that silently recomputed a figure a
    different way would disagree with it.
    """

    connection = open_for_measurement(db)
    try:
        selected = cohort_module.select_cohort(
            connection,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=GENERATED_AT,
        )
        measured = ac9_module.measure_ac9(
            connection, selected, now_ms=GENERATED_AT
        )
    finally:
        connection.close()

    facts = render_module.section_from_ac9(measured, selected).facts
    assert facts["cohort"]["denominator"] == selected.denominator
    assert facts["series"]["model_response_total"] == measured.model_response_total
    assert facts["series"]["invocation_count"] == measured.invocation_count
    assert facts["coverage"]["covered_count"] == measured.covered_count
    assert set(facts["figures"]) == {
        figure.label.replace(" ", "_") for figure in measured.figures()
    }
    assert facts["baseline"]["source"] == measured.baseline.source


# --------------------------------------------------------------------------
# section 6 -- the report is measured over one state of the database
# --------------------------------------------------------------------------


def _fingerprint_now(path: Path, mode: str = FINGERPRINT_CONTENT) -> str:
    """The content digest of *path* right now, through a separate open."""

    connection = open_for_measurement(path)
    try:
        return fingerprint_database(
            connection, tables=render_module.FINGERPRINT_TABLES, mode=mode
        ).digest
    finally:
        connection.close()


def _racing_writer(path: Path, run_id: str):
    """A writer that commits *run_id* on the first AC-9 measurement.

    Patched over ``render.ac9_module.measure_ac9`` -- i.e. the point the report
    reaches after the cohort has been selected and before the provenance header
    is built -- so the commit lands exactly in the window section 6's
    fingerprint claim depends on being closed. ``timeout=0`` so a blocked write
    answers immediately instead of sitting on the busy handler.
    """

    real = ac9_module.measure_ac9
    outcome: dict[str, object] = {}

    def racing(connection, selected, **kwargs):
        writer = sqlite3.connect(path, isolation_level=None, timeout=0)
        try:
            writer.execute(
                "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
                " VALUES (?, 'completed', ?, ?)",
                (run_id, PERIOD_START + 3_000, PERIOD_START + 4_000),
            )
            outcome["committed"] = True
        except sqlite3.OperationalError as error:
            outcome["blocked"] = str(error)
        finally:
            writer.close()
        return real(connection, selected, **kwargs)

    return racing, outcome


def test_a_writer_committing_mid_report_cannot_move_the_database_under_it(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Section 6's claim, tested against a control plane that is being written to.

    ``db_fingerprint`` exists so that two reports over "the same" database are
    provably over the same content. A fingerprint taken at the end of a report
    whose rows moved during it certifies a state that never produced the
    figures: the cohort would name one run and the header would attest a
    database holding two. The report is built inside a read snapshot for exactly
    this reason, so the writer is held off until it closes.
    """

    before = _fingerprint_now(db)
    racing, outcome = _racing_writer(db, "run-mid-report")
    monkeypatch.setattr(render_module.ac9_module, "measure_ac9", racing)

    report = report_over(db)

    assert "committed" not in outcome, (
        "a writer committed inside the report: the report's reads are not over "
        "one state of the database"
    )
    assert "locked" in str(outcome.get("blocked", ""))
    facts = report.as_mapping()
    assert facts["header"]["db_fingerprint"] == before, (
        "the header fingerprints a state other than the one the figures were "
        "computed from"
    )
    assert facts["sections"]["ac9"]["facts"]["cohort"]["run_ids"] == ["run-1"]
    # And the writer that was held off is only held off for the report: the
    # cost is bounded by the report's duration, not by the process's.
    assert _fingerprint_now(db) == before
    monkeypatch.undo()


def test_the_report_is_built_inside_a_held_read_transaction(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot is the report's, not the caller's, so it cannot be forgotten.

    Observed from inside the report rather than by reading the source: a caller
    who wrapped the call themselves would satisfy a source test, and the point
    is that ``build_measurement_report`` holds the snapshot whoever calls it.
    """

    seen: list[bool] = []
    real = ac9_module.measure_ac9

    def observing(connection, selected, **kwargs):
        seen.append(connection.in_transaction)
        return real(connection, selected, **kwargs)

    monkeypatch.setattr(render_module.ac9_module, "measure_ac9", observing)
    report_over(db)
    assert seen == [True]
