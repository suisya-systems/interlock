"""The header's one hard claim: two reports with one digest saw one content.

Everything else in ``provenance.py`` is transcription -- section 6's table into
a dataclass -- and transcription is checked by asserting the fields are there.
One field is not transcription, and it is the one this file is built around.

``db_fingerprint`` asserts that two reads were over the same content. The cheap
implementation section 6 rejected (row counts plus ``MAX(seq)``/``MAX(rowid)``)
passes every test that inserts rows and asserts the digest moved, because
inserting moves a count. It fails only on the case that actually happens in this
schema: an **in-place UPDATE** -- an ``outbox`` status, a ``gate`` outcome, a
``usage_status`` backfilled by a late adapter. So
:func:`test_content_fingerprint_moves_on_an_in_place_update_that_moves_no_count`
builds exactly that edit, *asserts in the test body* that the count and the
maximum did not move, and then asserts the content digest did -- and its twin
asserts the aggregate digest did **not**, which is what makes the aggregate mode
demonstrably the weaker thing the header says it is rather than a synonym.

The other adversarial edges here:

* the digest is scoped to the tables it names, proved by writing into a table
  outside the list and asserting the digest is byte-identical -- a fingerprint
  that quietly covered everything would pass the update tests and mean something
  else;
* the banner cannot be rendered around, proved by asserting it in *both*
  renderings for both causes section 6 names (a second detector version, and a
  policy revision that changed mid-period);
* the section-6 field list is parametrised over a list written from the document
  and checked against both renderings, so a field dropped from one rendering
  fails even though the other still carries it.

Nothing here recomputes a digest to compare against: the tests compare digests
this module produced under two states of the world, which is the property the
field claims, and no reimplementation of the hash can make that assertion pass
by agreeing with itself.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import ai_invocation, policy
from claude_org_runtime.control_plane.migrator import (
    applied_migrations,
    create_production_control_plane,
)
from claude_org_runtime.measurement import ac9, cohort, fixtures
from claude_org_runtime.measurement.provenance import (
    AGGREGATE_STATEMENT,
    BOUNDED_IMPUTATION_RULE,
    CONTENT_STATEMENT,
    FINGERPRINT_AGGREGATE,
    FINGERPRINT_CONTENT,
    HEADER_QUERIES,
    SENSITIVITY_IMPUTATION_RULE,
    TOOL_VERSION,
    CoverageSummary,
    FingerprintModeRefused,
    FixtureSuiteRef,
    ImputationRule,
    NotAProductionDatabase,
    PeriodRefused,
    ProvenanceRefusal,
    QueryDefinitionsRefused,
    RevisionNotInPeriod,
    TableNotReadable,
    build_header,
    coverage_from_ac9,
    fingerprint_database,
    fixture_suite_ref,
    imputation_from_ac9,
    iso8601_ms,
    query_catalogue,
    render_header_json,
    render_header_markdown,
)
from claude_org_runtime.measurement.reader import open_for_measurement

T0 = 1_700_000_000_000
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS
GENERATED_AT = PERIOD_END + 60_000

#: The tables a report of this shape reads. Named here so every test scopes its
#: fingerprint the same way and a test that widens the scope has to say so.
READ_TABLES = ("incident", "ai_invocation", "run")

#: The query set a caller supplies. Deliberately not one of HEADER_QUERIES'
#: names, so the merge is exercised on every header built here.
CALLER_QUERIES = {"caller_incidents": "SELECT count(*) FROM incident"}

#: Section 6's table, field for field, as the document lists it -- written out
#: here rather than read off ``as_mapping()``, so that a field deleted from the
#: implementation fails these tests instead of quietly shrinking the list they
#: check.
SECTION_6_FIELDS = (
    "period_start_ms",
    "period_start_iso",
    "period_end_ms",
    "period_end_iso",
    "generated_at_ms",
    "tool_version",
    "db_path",
    "application_id",
    "database_is_production",
    "user_version",
    "schema_migration_head.version",
    "schema_migration_head.name",
    "db_fingerprint",
    "fingerprint_mode",
    "policy_revision_id",
    "detector_versions",
    "adapter_versions",
    "query_definitions",
    "query_definitions_sha256",
    "fixture_suite_ref.commit",
    "fixture_suite_ref.positive",
    "fixture_suite_ref.negative",
    "imputation_rule.bounded",
    "imputation_rule.sensitivity",
    "imputation_rule.unbounded_missing",
    "coverage.covered",
    "coverage.total",
    "coverage.excluded",
    "censored",
    "censored_left",
    "unmatched",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def writable(path: Path) -> sqlite3.Connection:
    """An ordinary writable handle -- deliberately not the harness's."""

    return sqlite3.connect(path, isolation_level=None)


def seed_revision_id(path: Path) -> int:
    connection = open_for_measurement(path)
    try:
        return policy.effective_revision_id(connection, now_ms=PERIOD_START)
    finally:
        connection.close()


def add_run(cp: sqlite3.Connection, run_id: str) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
        " VALUES (?, 'completed', ?, ?)",
        (run_id, PERIOD_START + 1, PERIOD_START + 2),
    )
    return run_id


def add_incident(
    cp: sqlite3.Connection,
    incident_id: str,
    *,
    run_id: str | None = None,
    detector_version: str = "detector/1",
    created_at_ms: int = PERIOD_START + 10,
    fact_state: str = "stalled",
) -> str:
    cp.execute(
        """
        INSERT INTO incident (incident_id, run_id, session_id, fact_state,
                              detector_version, dedup_key, created_at_ms,
                              updated_at_ms)
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            incident_id,
            run_id,
            fact_state,
            detector_version,
            f"dedup/{incident_id}",
            created_at_ms,
            created_at_ms,
        ),
    )
    return incident_id


def add_invocation(
    cp: sqlite3.Connection,
    invocation_id: str,
    *,
    adapter_version: str,
    incident_id: str | None = None,
    run_id: str | None = None,
    started_at_ms: int = PERIOD_START + 20,
) -> str:
    ai_invocation.start_invocation(
        cp,
        invocation_id=invocation_id,
        provider="anthropic",
        model="a-model",
        adapter_version=adapter_version,
        started_at_ms=started_at_ms,
        incident_id=incident_id,
        run_id=run_id,
        max_output_tokens=4096,
    )
    return invocation_id


def add_revision(cp: sqlite3.Connection, *, effective_at_ms: int) -> int:
    """A second policy revision taking effect inside the period."""

    cp.execute(
        "INSERT INTO policy_revision (note, decided_by, effective_at_ms)"
        " VALUES ('a later time base', 'D-0031', ?)",
        (effective_at_ms,),
    )
    return int(cp.execute("SELECT last_insert_rowid()").fetchone()[0])


def header_over(
    path: Path,
    *,
    revision_id: int,
    fingerprint_mode: str = FINGERPRINT_CONTENT,
    query_definitions=None,
    fixture_suite: FixtureSuiteRef | None = None,
    censored: int = 3,
    censored_left: int = 1,
    unmatched=None,
    **kwargs,
):
    connection = open_for_measurement(path)
    try:
        return build_header(
            connection,
            db_path=str(path),
            period_start_ms=kwargs.pop("period_start_ms", PERIOD_START),
            period_end_ms=kwargs.pop("period_end_ms", PERIOD_END),
            generated_at_ms=kwargs.pop("generated_at_ms", GENERATED_AT),
            policy_revision_id=revision_id,
            fingerprint_tables=kwargs.pop("fingerprint_tables", READ_TABLES),
            query_definitions=(
                CALLER_QUERIES if query_definitions is None else query_definitions
            ),
            fixture_suite=(
                FixtureSuiteRef(
                    commit="c0ffee",
                    positive=4,
                    negative=2,
                    content_digest="deadbeef",
                )
                if fixture_suite is None
                else fixture_suite
            ),
            imputation=ImputationRule(
                bounded=BOUNDED_IMPUTATION_RULE,
                sensitivity=SENSITIVITY_IMPUTATION_RULE,
                unbounded_missing=0,
            ),
            coverage=CoverageSummary(
                covered=3, total=4, excluded={"v1_owned": 0, "in_flight": 1}
            ),
            censored=censored,
            censored_left=censored_left,
            unmatched={"unmatched_key": 2} if unmatched is None else unmatched,
            fingerprint_mode=fingerprint_mode,
            **kwargs,
        )
    finally:
        connection.close()


def fingerprint_of(path: Path, *, mode: str = FINGERPRINT_CONTENT, tables=READ_TABLES):
    connection = open_for_measurement(path)
    try:
        return fingerprint_database(connection, tables=tables, mode=mode)
    finally:
        connection.close()


def counts_and_maxima(path: Path) -> dict[str, tuple[int, int | None]]:
    """What the rejected aggregate fingerprint is made of, read independently.

    Used to *prove* the premise of the update tests -- that the cheap form has
    nothing to notice -- rather than asserting it in prose.
    """

    connection = open_for_measurement(path)
    try:
        return {
            table: tuple(
                connection.execute(
                    f"SELECT COUNT(*), MAX(rowid) FROM {table}"
                ).fetchone()
            )
            for table in READ_TABLES
        }
    finally:
        connection.close()


def populate(path: Path) -> None:
    """One run, one incident, one invocation: enough for every table read."""

    cp = writable(path)
    try:
        run_id = add_run(cp, "run-1")
        incident_id = add_incident(cp, "inc-1", run_id=run_id)
        add_invocation(
            cp,
            "invocation-1",
            adapter_version="adapter/1",
            incident_id=incident_id,
            run_id=run_id,
        )
    finally:
        cp.close()


def backfill_usage(path: Path) -> None:
    """The late adapter finally answers: an in-place fill-in, through the real writer.

    Section 6 names this edit by name -- "a ``usage_status`` backfilled by a
    late adapter" -- and it is written here through
    :func:`ai_invocation.complete_invocation` rather than by hand, so the test
    is over the update the system actually performs.
    """

    cp = writable(path)
    try:
        ai_invocation.complete_invocation(
            cp,
            invocation_id="invocation-1",
            usage=ai_invocation.ProviderUsage.reported(
                adapter_version="adapter/1", output_tokens=1_200
            ),
            model_response_count=1,
            finished_at_ms=PERIOD_START + 25,
        )
        assert (
            cp.execute(
                "SELECT usage_status FROM ai_invocation"
                " WHERE invocation_id = 'invocation-1'"
            ).fetchone()[0]
            == "reported"
        ), "the premise of the update tests: the row really did change"
    finally:
        cp.close()


# --------------------------------------------------------------------------
# the fingerprint: the field the header's claim rests on
# --------------------------------------------------------------------------


def test_content_fingerprint_moves_on_an_in_place_update_that_moves_no_count(
    db: Path,
) -> None:
    """The whole reason section 6 rejected counts plus maxima, as a test.

    The edit is the one that actually happens: a late adapter backfills a
    ``usage_status``. It changes what every AC-9 figure in the report says, and
    it changes no row count and no ``MAX(rowid)`` -- asserted here, not assumed.
    """

    populate(db)
    before = fingerprint_of(db)
    aggregates_before = counts_and_maxima(db)

    backfill_usage(db)

    assert counts_and_maxima(db) == aggregates_before, (
        "the premise of this test: the edit moved no count and no maximum"
    )
    after = fingerprint_of(db)
    assert after.digest != before.digest
    assert after.mode == FINGERPRINT_CONTENT
    assert after.establishes_content_identity
    assert after.statement == CONTENT_STATEMENT


def test_aggregate_fingerprint_is_blind_to_the_same_edit(db: Path) -> None:
    """The rejected form, reproduced faithfully enough to be seen failing.

    If this ever starts passing (that is, the aggregate digest starts moving),
    the two modes have stopped being different and the header's weaker-mode
    statement has become a lie in the other direction.
    """

    populate(db)
    before = fingerprint_of(db, mode=FINGERPRINT_AGGREGATE)

    backfill_usage(db)

    after = fingerprint_of(db, mode=FINGERPRINT_AGGREGATE)
    assert after.digest == before.digest
    assert not after.establishes_content_identity
    assert after.statement == AGGREGATE_STATEMENT


def test_an_aggregate_report_says_its_fingerprint_proves_nothing_about_content(
    db: Path,
) -> None:
    """Both renderings carry the disclaimer, and the content one does not."""

    populate(db)
    revision_id = seed_revision_id(db)

    weak = header_over(db, revision_id=revision_id, fingerprint_mode=FINGERPRINT_AGGREGATE)
    weak_markdown = render_header_markdown(weak)
    weak_json = json.loads(render_header_json(weak))
    assert weak_json["fingerprint_mode"] == FINGERPRINT_AGGREGATE
    assert weak_json["fingerprint_establishes_content_identity"] is False
    assert weak_json["fingerprint_statement"] == AGGREGATE_STATEMENT
    assert "does NOT establish identity of content" in weak_markdown

    strong = header_over(db, revision_id=revision_id)
    strong_json = json.loads(render_header_json(strong))
    assert strong_json["fingerprint_establishes_content_identity"] is True
    assert strong_json["fingerprint_statement"] == CONTENT_STATEMENT
    assert "does NOT establish identity of content" not in render_header_markdown(strong)


def test_two_reports_over_an_unchanged_database_fingerprint_identically(
    db: Path,
) -> None:
    """The other half of the claim: no digest churn without a content change.

    A digest that moved between two reads of an untouched database would make
    "these two reports are over the same content" unprovable in practice, which
    is the same failure from the other side.
    """

    populate(db)
    revision_id = seed_revision_id(db)
    first = header_over(db, revision_id=revision_id)
    second = header_over(db, revision_id=revision_id, generated_at_ms=GENERATED_AT + 5_000)
    assert first.fingerprint.digest == second.fingerprint.digest
    assert first.generated_at_ms != second.generated_at_ms


def test_the_fingerprint_covers_the_tables_it_names_and_no_others(db: Path) -> None:
    """Scope is a property of the digest, not an incidental of the whole file.

    A row written into a table outside the list must not move it -- otherwise
    the header's ``fingerprint_tables`` would be decoration and the digest would
    be over "the database", which is a different and unstated claim.
    """

    populate(db)
    narrow_before = fingerprint_of(db, tables=("run",))
    wide_before = fingerprint_of(db, tables=READ_TABLES)

    cp = writable(db)
    try:
        add_incident(cp, "inc-2", run_id="run-1")
    finally:
        cp.close()

    assert fingerprint_of(db, tables=("run",)).digest == narrow_before.digest
    assert fingerprint_of(db, tables=READ_TABLES).digest != wide_before.digest


def test_the_fingerprint_separates_null_from_empty_and_keeps_value_boundaries(
    db: Path,
) -> None:
    """The type tag and the length prefix, each exercised on a real row.

    ``NULL`` and ``''`` are different facts in this schema (an unrecorded
    pattern versus a recorded empty one) and hash differently only because of
    the type tag. The second pair is chosen so that the tags alone do **not**
    separate them: two adjacent text columns holding ``('as', 'b')`` and
    ``('a', 'sb')`` produce the identical byte stream once each value is
    written as its tag followed by its bytes, so only the explicit length
    prefix keeps them apart -- and without it two materially different rows
    would share one digest, which is the aggregate mode's false-identity
    failure arriving by another route.
    """

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        # fact_state and detector_version are adjacent columns, which is what
        # makes the boundary between them the thing under test.
        add_incident(cp, "inc-1", run_id="run-1", fact_state="as")
        cp.execute(
            "UPDATE incident SET detector_version = 'b' WHERE incident_id = 'inc-1'"
        )
    finally:
        cp.close()
    null_pattern = fingerprint_of(db, tables=("incident",))

    cp = writable(db)
    try:
        cp.execute("UPDATE incident SET known_pattern = '' WHERE incident_id = 'inc-1'")
    finally:
        cp.close()
    empty_pattern = fingerprint_of(db, tables=("incident",))
    assert empty_pattern.digest != null_pattern.digest

    cp = writable(db)
    try:
        cp.execute(
            "UPDATE incident SET fact_state = 'a', detector_version = 'sb'"
            " WHERE incident_id = 'inc-1'"
        )
    finally:
        cp.close()
    moved_boundary = fingerprint_of(db, tables=("incident",))
    assert moved_boundary.digest != empty_pattern.digest


def test_a_table_that_is_not_there_is_refused_not_skipped(db: Path) -> None:
    connection = open_for_measurement(db)
    try:
        with pytest.raises(TableNotReadable) as refusal:
            fingerprint_database(connection, tables=("incidents",))
        assert "incidents" in str(refusal.value)
        with pytest.raises(TableNotReadable):
            fingerprint_database(connection, tables=())
        with pytest.raises(TableNotReadable):
            fingerprint_database(connection, tables=("run", "run"))
        with pytest.raises(FingerprintModeRefused):
            fingerprint_database(connection, tables=("run",), mode="cheap")
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the version sets and the banner
# --------------------------------------------------------------------------


def test_detector_versions_is_a_set_over_the_period(db: Path) -> None:
    """Two incidents on one version give one member; a version outside the
    period is not in the set at all (the bounds are half-open)."""

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        add_incident(cp, "inc-1", run_id="run-1", detector_version="detector/1")
        add_incident(
            cp,
            "inc-2",
            run_id="run-1",
            detector_version="detector/1",
            created_at_ms=PERIOD_START + 500,
        )
        add_incident(
            cp,
            "inc-outside",
            run_id="run-1",
            detector_version="detector/9",
            created_at_ms=PERIOD_END,
        )
    finally:
        cp.close()

    header = header_over(db, revision_id=seed_revision_id(db))
    assert header.detector_versions == ("detector/1",)
    assert not header.non_homogeneous


def test_a_second_detector_version_raises_the_banner_in_both_renderings(
    db: Path,
) -> None:
    """Section 6's first non-homogeneity cause, and the banner is unmissable.

    Asserted in both renderings because a banner that only reaches one of them
    is absent for whichever reader has the other.
    """

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        add_incident(cp, "inc-1", run_id="run-1", detector_version="detector/1")
        add_incident(
            cp,
            "inc-2",
            run_id="run-1",
            detector_version="detector/2",
            created_at_ms=PERIOD_START + 500,
        )
    finally:
        cp.close()

    header = header_over(db, revision_id=seed_revision_id(db))
    assert header.detector_versions == ("detector/1", "detector/2")
    assert header.non_homogeneous

    markdown = render_header_markdown(header)
    document = json.loads(render_header_json(header))
    assert markdown.startswith("!!")
    assert "NON-HOMOGENEOUS PERIOD" in markdown
    assert "detector/1" in markdown and "detector/2" in markdown
    assert "Q-0009" in markdown, "the set is exposed, not resolved"
    assert document["non_homogeneous"] is True
    assert any("NON-HOMOGENEOUS PERIOD" in line for line in document["banner"])
    assert len(document["non_homogeneity_reasons"]) == 1


def test_a_policy_revision_change_inside_the_period_raises_the_banner(
    db: Path,
) -> None:
    """Section 6's second cause: the budgets moved under the latency figures."""

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        add_incident(cp, "inc-1", run_id="run-1")
        add_revision(cp, effective_at_ms=PERIOD_START + DAY_MS // 2)
    finally:
        cp.close()

    header = header_over(db, revision_id=seed_revision_id(db))
    assert len(header.policy_revision_ids) == 2
    assert header.non_homogeneous
    markdown = render_header_markdown(header)
    assert "policy_revision_id changed inside the period" in markdown
    assert json.loads(render_header_json(header))["non_homogeneous"] is True


def test_a_homogeneous_period_says_so_rather_than_printing_nothing(db: Path) -> None:
    populate(db)
    header = header_over(db, revision_id=seed_revision_id(db))
    markdown = render_header_markdown(header)
    assert not header.non_homogeneous
    assert markdown.startswith("period is HOMOGENEOUS")
    assert json.loads(render_header_json(header))["non_homogeneity_reasons"] == []


def test_adapter_versions_is_a_set_over_the_period(db: Path) -> None:
    """The AC-9 token seam, same reasoning, same shape -- and a second adapter
    version is exposed even though it does not itself raise the banner."""

    cp = writable(db)
    try:
        run_id = add_run(cp, "run-1")
        incident_id = add_incident(cp, "inc-1", run_id=run_id)
        add_invocation(
            cp, "inv-1", adapter_version="adapter/2", incident_id=incident_id, run_id=run_id
        )
        add_invocation(
            cp,
            "inv-2",
            adapter_version="adapter/1",
            incident_id=incident_id,
            run_id=run_id,
            started_at_ms=PERIOD_START + 30,
        )
        add_invocation(
            cp,
            "inv-outside",
            adapter_version="adapter/99",
            incident_id=incident_id,
            run_id=run_id,
            started_at_ms=PERIOD_END,
        )
    finally:
        cp.close()

    header = header_over(db, revision_id=seed_revision_id(db))
    assert header.adapter_versions == ("adapter/1", "adapter/2")


# --------------------------------------------------------------------------
# the queries are data
# --------------------------------------------------------------------------


def test_the_query_digest_moves_when_a_query_text_moves(db: Path) -> None:
    """A ``>=`` that became a ``>`` changes the report and nothing else in the
    header; the digest over the query text is the only field that can see it."""

    populate(db)
    revision_id = seed_revision_id(db)
    original = header_over(
        db,
        revision_id=revision_id,
        query_definitions={"episodes": "SELECT 1 WHERE created_at_ms >= :from"},
    )
    edited = header_over(
        db,
        revision_id=revision_id,
        query_definitions={"episodes": "SELECT 1 WHERE created_at_ms > :from"},
    )
    assert original.queries.digest != edited.queries.digest
    assert original.fingerprint.digest == edited.fingerprint.digest, (
        "only the query text changed; the database did not"
    )


def test_the_query_digest_is_over_the_set_not_the_writing_order(db: Path) -> None:
    first = query_catalogue({"a": "SELECT 1", "b": "SELECT 2"})
    second = query_catalogue({"b": "SELECT 2", "a": "SELECT 1"})
    assert first.digest == second.digest
    assert query_catalogue({"a": "SELECT 1", "b": "SELECT 3"}).digest != first.digest


def test_the_header_carries_its_own_queries_as_text(db: Path) -> None:
    """The two queries this module runs are in the set a reader can run by
    hand, alongside the caller's."""

    populate(db)
    header = header_over(db, revision_id=seed_revision_id(db))
    for name, text in HEADER_QUERIES.items():
        assert header.queries.definitions[name] == text
    assert "caller_incidents" in header.queries.definitions
    assert "FROM incident" in header.queries.definitions["provenance_detector_versions"]


def test_a_name_carrying_two_texts_is_refused(db: Path) -> None:
    populate(db)
    with pytest.raises(QueryDefinitionsRefused):
        header_over(
            db,
            revision_id=seed_revision_id(db),
            query_definitions={"provenance_detector_versions": "SELECT 1"},
        )
    with pytest.raises(QueryDefinitionsRefused):
        query_catalogue({})
    with pytest.raises(QueryDefinitionsRefused):
        query_catalogue({"empty": "   "})


# --------------------------------------------------------------------------
# every section-6 field, in both renderings
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", SECTION_6_FIELDS)
def test_every_section_6_field_is_in_both_renderings(db: Path, field: str) -> None:
    populate(db)
    header = header_over(db, revision_id=seed_revision_id(db))
    markdown = render_header_markdown(header)
    document = json.loads(render_header_json(header))

    assert f"`{field}" in markdown, f"{field} is missing from the Markdown rendering"

    node = document
    for part in field.split("."):
        assert isinstance(node, dict) and part in node, (
            f"{field} is missing from the JSON rendering"
        )
        node = node[part]


def test_the_period_bounds_are_printed_as_both_epoch_ms_and_iso(db: Path) -> None:
    populate(db)
    header = header_over(db, revision_id=seed_revision_id(db))
    document = json.loads(render_header_json(header))
    assert document["period_start_ms"] == PERIOD_START
    assert document["period_end_ms"] == PERIOD_END
    assert document["period_start_iso"] == iso8601_ms(PERIOD_START)
    assert document["period_end_iso"].endswith("Z")
    assert document["period_bounds"] == "half-open [start, end)"
    assert iso8601_ms(0) == "1970-01-01T00:00:00.000Z"
    assert iso8601_ms(1) == "1970-01-01T00:00:00.001Z"
    with pytest.raises(PeriodRefused):
        iso8601_ms(-1)


def test_the_header_names_the_database_and_that_it_is_a_production_one(
    db: Path,
) -> None:
    """``application_id``, ``user_version`` and the migration head come off the
    database, and the head is the newest applied step by version *and* name."""

    populate(db)
    header = header_over(db, revision_id=seed_revision_id(db))

    connection = open_for_measurement(db)
    try:
        applied = applied_migrations(connection)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()

    newest = applied[-1]
    assert header.schema_migration_head.version == newest["version"]
    assert header.schema_migration_head.name == newest["name"]
    assert header.user_version == user_version
    assert header.database_is_production
    assert header.db_path == str(db)
    assert header.tool_version == TOOL_VERSION


def test_a_non_production_database_cannot_be_given_a_production_header(
    tmp_path: Path,
) -> None:
    """The header states the database was a production one, so it may not be
    built over one that is not -- the field a later reader checks would be the
    field that lied."""

    path = tmp_path / "not-production.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA application_id = 305419896")
        connection.execute("CREATE TABLE run (run_id TEXT PRIMARY KEY)")
    finally:
        connection.close()

    reading = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(NotAProductionDatabase):
            build_header(
                reading,
                db_path=str(path),
                period_start_ms=PERIOD_START,
                period_end_ms=PERIOD_END,
                generated_at_ms=GENERATED_AT,
                policy_revision_id=1,
                fingerprint_tables=("run",),
                query_definitions=CALLER_QUERIES,
                fixture_suite=FixtureSuiteRef.absent("no recall in this report"),
                imputation=ImputationRule(
                    bounded=BOUNDED_IMPUTATION_RULE,
                    sensitivity=SENSITIVITY_IMPUTATION_RULE,
                    unbounded_missing=0,
                ),
                coverage=CoverageSummary(covered=0, total=0, excluded={}),
                censored=0,
                censored_left=0,
                unmatched={},
            )
    finally:
        reading.close()


def test_a_revision_not_in_force_in_the_period_is_refused(db: Path) -> None:
    populate(db)
    with pytest.raises(RevisionNotInPeriod):
        header_over(db, revision_id=9_999)


def test_a_report_cannot_be_generated_before_its_period_closed(db: Path) -> None:
    populate(db)
    with pytest.raises(PeriodRefused):
        header_over(
            db, revision_id=seed_revision_id(db), generated_at_ms=PERIOD_END - 1
        )
    with pytest.raises(PeriodRefused):
        header_over(
            db,
            revision_id=seed_revision_id(db),
            period_start_ms=PERIOD_END,
            period_end_ms=PERIOD_START,
        )


def test_the_censored_counts_are_carried_and_must_be_counts(db: Path) -> None:
    """Section 3.5's numbers are header fields, and a negative one is refused
    rather than printed -- a negative censored count is a bug upstream and the
    header is the last place it can be caught before it is published."""

    populate(db)
    header = header_over(db, revision_id=seed_revision_id(db), censored=7, censored_left=2)
    document = json.loads(render_header_json(header))
    assert document["censored"] == 7
    assert document["censored_left"] == 2
    with pytest.raises(ProvenanceRefusal):
        header_over(db, revision_id=seed_revision_id(db), censored=-1)


def test_the_unmatched_counts_are_carried_verbatim(db: Path) -> None:
    populate(db)
    header = header_over(
        db,
        revision_id=seed_revision_id(db),
        unmatched={"unmatched_key": 4, "unmatched_key_escalation": 1},
    )
    markdown = render_header_markdown(header)
    assert "`unmatched.unmatched_key`" in markdown
    assert json.loads(render_header_json(header))["unmatched"]["unmatched_key"] == 4


# --------------------------------------------------------------------------
# the adapters onto the sections that produce the figures
# --------------------------------------------------------------------------


def test_coverage_and_imputation_come_off_the_real_ac9_report(db: Path) -> None:
    """The two AC-9 blocks are copied from the report, never recounted.

    Built through ``select_cohort`` and ``measure_ac9`` so that a change in what
    AC-9 calls covered reaches this header without anyone editing it -- and so
    that ``unbounded_missing`` on the header is the same number section 2.4
    calls disqualifying.
    """

    cp = writable(db)
    try:
        run_id = add_run(cp, "run-1")
        incident_id = add_incident(cp, "inc-1", run_id=run_id)
        add_invocation(
            cp, "inv-1", adapter_version="adapter/1", incident_id=incident_id, run_id=run_id
        )
        # A second invocation with no ceiling: un-imputable, so section 2.4's
        # unbounded_missing is non-zero and the header must say the report
        # cannot support an acceptance claim.
        ai_invocation.start_invocation(
            cp,
            invocation_id="inv-2",
            provider="anthropic",
            model="a-model",
            adapter_version="adapter/1",
            started_at_ms=PERIOD_START + 40,
            incident_id=incident_id,
            run_id=run_id,
        )
        cp.execute(
            "UPDATE ai_invocation SET finished_at_ms = ? WHERE invocation_id = 'inv-2'",
            (PERIOD_START + 50,),
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        selected = cohort.select_cohort(
            connection,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=GENERATED_AT,
        )
        report = ac9.measure_ac9(connection, selected, now_ms=GENERATED_AT)
    finally:
        connection.close()

    coverage = coverage_from_ac9(report, selected)
    imputation = imputation_from_ac9(report)
    assert coverage.covered == report.covered_count
    assert coverage.total == report.invocation_count
    assert dict(coverage.excluded) == dict(selected.excluded_counts())
    assert imputation.unbounded_missing == len(report.unbounded_missing)
    assert imputation.unbounded_missing > 0
    assert not imputation.supports_acceptance_claim

    header = header_over(
        db, revision_id=seed_revision_id(db), censored=0, censored_left=0
    )
    document = json.loads(render_header_json(header))
    assert set(document["coverage"]) == {"covered", "total", "ratio", "excluded"}


def test_coverage_ratio_is_none_and_not_zero_over_an_empty_cohort() -> None:
    assert CoverageSummary(covered=0, total=0, excluded={}).ratio is None
    assert CoverageSummary(covered=1, total=4, excluded={}).ratio == 0.25


def test_the_fixture_suite_ref_splits_positive_from_negative(db: Path) -> None:
    """Built from the shipped corpus, so the counts are the corpus's own."""

    root = Path(__file__).resolve().parents[1] / "fixtures" / "labelled"
    corpus = fixtures.load_corpus(root)
    reference = fixture_suite_ref(corpus, commit="0123abc")
    composition = corpus.composition()
    assert reference.positive == composition["positive"]
    assert reference.negative == composition["negative"]
    assert reference.total == composition["total"]
    assert reference.content_digest == corpus.content_digest
    assert reference.absent_reason is None
    with pytest.raises(ProvenanceRefusal):
        fixture_suite_ref(corpus, commit="  ")


def test_a_report_with_no_corpus_states_the_absence(db: Path) -> None:
    """A missing ``fixture_suite_ref`` reads as a report that forgot to record
    one; a stated absence is a different claim and is the honest one."""

    populate(db)
    header = header_over(
        db,
        revision_id=seed_revision_id(db),
        fixture_suite=FixtureSuiteRef.absent("no recall measured in this period"),
    )
    document = json.loads(render_header_json(header))
    assert document["fixture_suite_ref"]["absent_reason"] == (
        "no recall measured in this period"
    )
    assert document["fixture_suite_ref"]["total"] is None
    assert "no recall measured in this period" in render_header_markdown(header)
    with pytest.raises(ProvenanceRefusal):
        FixtureSuiteRef.absent("   ")


# --------------------------------------------------------------------------
# the renderings themselves
# --------------------------------------------------------------------------


def test_both_renderings_are_ascii_and_encode_on_a_cp932_console(db: Path) -> None:
    """The header is printed to a console that may be cp932; a character that
    cannot encode there crashes the report rather than degrading it."""

    populate(db)
    header = header_over(db, revision_id=seed_revision_id(db))
    for rendering in (render_header_markdown(header), render_header_json(header)):
        assert rendering.isascii()
        rendering.encode("cp932")


def test_a_pipe_in_a_query_cannot_shift_the_markdown_columns(db: Path) -> None:
    populate(db)
    header = header_over(
        db,
        revision_id=seed_revision_id(db),
        query_definitions={"piped": "SELECT 'a' | 'b'"},
    )
    row = [
        line
        for line in render_header_markdown(header).splitlines()
        if "query_definitions.piped" in line
    ]
    assert len(row) == 1
    assert "\\|" in row[0], "the pipe inside the query text is escaped"
    # Three unescaped pipes: the leading one, the column separator, the
    # trailing one. A fourth would mean the query text opened a new column.
    assert row[0].count("|") - row[0].count("\\|") == 3
