"""Three assertions that must be able to be violated, and a report that must not judge.

``canary.py`` fails silently in four ways, and a cheerful suite would notice
none of them, so each gets its own adversarial treatment here:

* **A finding that gets tidied away.** A dual write and a run claimed by both
  sides are the two things the report exists to catch, so both are constructed
  on purpose and asserted as *findings* -- and the ownership case additionally
  asserts the ledger still carries both claims, because deduping them would
  leave a correct finding count with no evidence under it.
* **A read-only assertion that reads a claim instead of the connection.** The
  decisive test hands the checker a connection opened read-**write** with
  ``PRAGMA query_only = ON`` set by hand: it satisfies every claim a report
  could print about itself and has no read-only capability at all. A checker
  that trusted the pragma passes it; one that asks the file does not.
* **A verdict arriving as prose.** The rendering of a report *with* findings in
  it is grepped for the verdict vocabulary with word boundaries, because the
  moment for a harness to slip a go/no-go in is the moment something is wrong.
* **A missing comparison rendering as a clean one.** The empty-v1 report is
  rendered and read for the words that distinguish "nothing was found" from
  "nothing was compared", in all three sections.

Fixtures are built through the production schema and a second, writable
connection; the harness's own connection cannot write, and nothing here relaxes
that. Expected keys, counts and strings are written out by hand -- nothing in
this file recomputes the module to compare against it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import repo_link
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.canary import (
    DUAL_WRITE,
    OWNERSHIP_COLLISION,
    QUERY_DEFINITIONS,
    RECORD_CLASS_PULL_REQUEST,
    RECORD_CLASS_RUN,
    RECORD_CLASSES,
    CanaryRefusal,
    OwnedRun,
    OwnershipInputRefused,
    ReadOnlyCapabilityRefused,
    UndeclaredRecordClass,
    V1InputRefused,
    V1OwnershipInput,
    V1WriterLedger,
    WrittenRecord,
    audit_writers,
    build_ownership_ledger,
    evidence_of_read_only,
    measure_canary_divergence,
    read_interlock_records,
    render_canary_divergence_report,
)
from claude_org_runtime.measurement.reader import open_for_measurement
from claude_org_runtime.measurement.shadow import (
    SUBJECT_PR_MERGE,
    CorrelationKey,
    ShadowEpisode,
    V1Reference,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS

V1_STORE = "v1:.state"
V1_SOURCE = "v1-shadow-adapter@1"

#: The verdict vocabulary section 5 forbids. Word boundaries, because the report
#: legitimately contains 'ongoing' and 'category' and this must not match those.
VERDICT_WORDS = re.compile(
    r"\b(pass|passes|passed|passing|fail|fails|failed|failing|go|no-go|nogo)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# helpers -- the world, built through a writable second connection
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def writable(path: Path) -> sqlite3.Connection:
    """An ordinary writable handle -- deliberately not the harness's."""

    return sqlite3.connect(path, isolation_level=None)


def add_run(
    cp: sqlite3.Connection, run_id: str, *, created: int, updated: int | None = None
) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
        " VALUES (?, 'running', ?, ?)",
        (run_id, created, created if updated is None else updated),
    )
    return run_id


def add_repository(cp: sqlite3.Connection, repo_id: str, owner: str, name: str) -> str:
    return repo_link.upsert_repository(
        cp, repo_id=repo_id, owner=owner, name=name, now_ms=T0
    )


def add_pull_request(
    cp: sqlite3.Connection, *, repo_id: str, pr_number: int, observed_at_ms: int
) -> None:
    repo_link.observe_pull_request(
        cp,
        repo_id=repo_id,
        pr_number=pr_number,
        head_sha="a" * 40,
        state="open",
        observed_at_ms=observed_at_ms,
        ingested_at_ms=observed_at_ms,
        event_id=f"evt-pr-{pr_number}",
        producer="pr_watcher",
    )


def merge_episode(episode_id: str, slug: str, number: str, onset_ms: int) -> ShadowEpisode:
    return ShadowEpisode(
        episode_id=episode_id,
        subject_class=SUBJECT_PR_MERGE,
        shape="pr_merged",
        onset_ms=onset_ms,
        key=CorrelationKey(
            subject_class=SUBJECT_PR_MERGE, parts=("github", slug, number)
        ),
    )


def report_over(
    db_path: Path,
    *,
    v1_reference: V1Reference,
    v1_writer_ledger: V1WriterLedger,
    v1_ownership: V1OwnershipInput,
    interlock_episodes=(),
):
    connection = open_for_measurement(db_path)
    try:
        return measure_canary_divergence(
            connection,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            interlock_episodes=interlock_episodes,
            v1_reference=v1_reference,
            censored_ids=frozenset(),
            fixture_labels={},
            v1_writer_ledger=v1_writer_ledger,
            v1_ownership=v1_ownership,
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# condition 2 -- the writer audit
# --------------------------------------------------------------------------


def test_a_record_written_by_both_stores_is_a_dual_write_finding(db: Path) -> None:
    with writable(db) as cp:
        add_run(cp, "run-a", created=PERIOD_START + 10)
        add_run(cp, "run-b", created=PERIOD_START + 20)

    ledger = V1WriterLedger.observed(
        source=V1_SOURCE,
        records=(
            WrittenRecord(
                record_class="run",
                record_key="run-b",
                first_written_at_ms=PERIOD_START + 15,
                last_written_at_ms=PERIOD_START + 40,
                store=V1_STORE,
            ),
        ),
    )
    connection = open_for_measurement(db)
    try:
        audit = audit_writers(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ledger=ledger,
        )
    finally:
        connection.close()

    assert audit.finding_count == 1
    finding = audit.findings[0]
    assert (finding.record_class, finding.record_key) == ("run", "run-b")
    # Both records survive whole: the instants are what a person reads next.
    assert finding.interlock.first_written_at_ms == PERIOD_START + 20
    assert finding.v1.last_written_at_ms == PERIOD_START + 40
    assert finding.v1.store == V1_STORE
    assert audit.interlock_record_count == 2
    assert audit.v1_record_count == 1


def test_the_pull_request_key_is_folded_in_sql_so_a_cased_slug_still_collides(
    db: Path,
) -> None:
    """v1 spells the slug lowercase; the row preserves case, as the schema requires.

    An independently spelled fold -- or none -- leaves the two keys unequal and
    reports a clean audit over a repository both systems wrote.
    """

    with writable(db) as cp:
        add_repository(cp, "repo-1", "Aa-Org", "Renga")
        add_pull_request(
            cp, repo_id="repo-1", pr_number=302, observed_at_ms=PERIOD_START + 30
        )

    ledger = V1WriterLedger.observed(
        source=V1_SOURCE,
        records=(
            WrittenRecord(
                record_class="pull_request",
                record_key="github/aa-org/renga#302",
                first_written_at_ms=PERIOD_START + 31,
                last_written_at_ms=PERIOD_START + 31,
                store=V1_STORE,
            ),
        ),
    )
    connection = open_for_measurement(db)
    try:
        audit = audit_writers(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ledger=ledger,
        )
    finally:
        connection.close()

    assert [f.record_key for f in audit.findings] == ["github/aa-org/renga#302"]


def test_a_record_written_only_by_one_store_is_not_a_finding(db: Path) -> None:
    with writable(db) as cp:
        add_run(cp, "run-a", created=PERIOD_START + 10)

    connection = open_for_measurement(db)
    try:
        audit = audit_writers(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ledger=V1WriterLedger.observed(
                source=V1_SOURCE,
                records=(
                    WrittenRecord(
                        record_class="run",
                        record_key="run-elsewhere",
                        first_written_at_ms=PERIOD_START,
                        last_written_at_ms=PERIOD_START,
                        store=V1_STORE,
                    ),
                ),
            ),
        )
    finally:
        connection.close()

    assert audit.finding_count == 0
    assert audit.available is True


def test_the_window_test_is_write_span_overlap_not_last_write_inside(db: Path) -> None:
    """A record created before the window and updated after it is still compared.

    The schema keeps a first and a last write and nothing between, so such a
    record may well have been written inside the window. Over-inclusion costs a
    candidate finding a person dismisses; the tighter test drops the finding.
    """

    with writable(db) as cp:
        add_run(cp, "spanning", created=PERIOD_START - 1_000, updated=PERIOD_END + 1_000)
        add_run(cp, "after", created=PERIOD_END, updated=PERIOD_END)
        add_run(cp, "before", created=PERIOD_START - 20, updated=PERIOD_START - 1)

    connection = open_for_measurement(db)
    try:
        records = read_interlock_records(
            connection, window_from_ms=PERIOD_START, window_to_ms=PERIOD_END
        )
    finally:
        connection.close()

    assert [record.record_key for record in records] == ["spanning"]


def test_a_v1_record_in_an_unqueried_class_is_refused_not_skipped(db: Path) -> None:
    connection = open_for_measurement(db)
    try:
        with pytest.raises(UndeclaredRecordClass) as refusal:
            audit_writers(
                connection,
                window_from_ms=PERIOD_START,
                window_to_ms=PERIOD_END,
                v1_ledger=V1WriterLedger.observed(
                    source=V1_SOURCE,
                    records=(
                        WrittenRecord(
                            record_class="pending_decision",
                            record_key="pd-1",
                            first_written_at_ms=PERIOD_START,
                            last_written_at_ms=PERIOD_START,
                            store=V1_STORE,
                        ),
                    ),
                ),
            )
    finally:
        connection.close()
    assert "pending_decision" in str(refusal.value)


def test_an_empty_v1_read_is_absent_and_an_attestation_is_a_comparison(
    db: Path,
) -> None:
    with writable(db) as cp:
        add_run(cp, "run-a", created=PERIOD_START + 1)

    connection = open_for_measurement(db)
    try:
        degraded = audit_writers(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ledger=V1WriterLedger.observed(source=V1_SOURCE, records=()),
        )
        attested = audit_writers(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ledger=V1WriterLedger.attests_empty(source=V1_SOURCE),
        )
    finally:
        connection.close()

    assert degraded.available is False
    assert "attests_empty" in (degraded.absent_reason or "")
    # The Interlock side is still counted, so the reader can see the audit had
    # something to compare against and no second list to compare it with.
    assert degraded.interlock_record_count == 1
    assert attested.available is True
    assert attested.finding_count == 0


def test_an_input_without_provenance_is_refused() -> None:
    with pytest.raises(V1InputRefused):
        V1WriterLedger.observed(source="", records=())
    with pytest.raises(V1InputRefused):
        V1WriterLedger.attests_empty(source="")
    with pytest.raises(V1InputRefused):
        V1WriterLedger.absent(reason="")
    with pytest.raises(V1InputRefused):
        V1OwnershipInput.observed(source="", runs=())


# --------------------------------------------------------------------------
# conditions 3, 4, 6 -- the ownership ledger
# --------------------------------------------------------------------------


def v1_owned(run_id: str, at: int) -> OwnedRun:
    return OwnedRun(
        run_id=run_id, owning_system="v1", decided_at_ms=at, store=V1_STORE
    )


def test_a_run_claimed_by_both_is_a_finding_and_is_not_deduped(db: Path) -> None:
    with writable(db) as cp:
        add_run(cp, "shared", created=PERIOD_START + 100)
        add_run(cp, "ours-only", created=PERIOD_START + 200)

    connection = open_for_measurement(db)
    try:
        ledger = build_ownership_ledger(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ownership=V1OwnershipInput.observed(
                source=V1_SOURCE,
                runs=(
                    v1_owned("shared", PERIOD_START + 90),
                    v1_owned("theirs-only", PERIOD_START + 300),
                ),
            ),
        )
    finally:
        connection.close()

    assert ledger.collision_run_ids() == ("shared",)
    finding = ledger.findings[0]
    assert [claim.owning_system for claim in finding.claims] == ["interlock", "v1"]
    assert [claim.decided_at_ms for claim in finding.claims] == [
        PERIOD_START + 100,
        PERIOD_START + 90,
    ]
    # Not deduped: two Interlock runs plus two v1 claims are four ledger
    # entries, and 'shared' appears twice.
    assert len(ledger.entries) == 4
    assert [entry.run_id for entry in ledger.entries].count("shared") == 2


def test_the_collision_check_is_not_bounded_by_the_listing_window(db: Path) -> None:
    """The mid-flight case: the run started before the canary window.

    Bounding the collision check by the window would blind it to exactly the
    run that changed owner -- a run started on one side before the canary and
    appearing on the other inside it.
    """

    with writable(db) as cp:
        add_run(cp, "in-flight", created=PERIOD_START - 5_000)

    connection = open_for_measurement(db)
    try:
        ledger = build_ownership_ledger(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ownership=V1OwnershipInput.observed(
                source=V1_SOURCE, runs=(v1_owned("in-flight", PERIOD_START - 6_000),)
            ),
        )
    finally:
        connection.close()

    assert ledger.collision_run_ids() == ("in-flight",)
    # The Interlock claim is read from the row, not from the listing -- the
    # listing does not contain it, since the run started before the window.
    assert ledger.findings[0].claims[0].decided_at_ms == PERIOD_START - 5_000
    assert [entry.run_id for entry in ledger.entries] == ["in-flight"]


def test_one_side_claiming_a_run_twice_is_refused_not_filed_as_divergence() -> None:
    with pytest.raises(OwnershipInputRefused) as refusal:
        V1OwnershipInput.observed(
            source=V1_SOURCE,
            runs=(v1_owned("dup", PERIOD_START), v1_owned("dup", PERIOD_START + 1)),
        )
    assert "dup" in str(refusal.value)


def test_an_absent_v1_ownership_input_reports_no_collision_and_says_why(
    db: Path,
) -> None:
    with writable(db) as cp:
        add_run(cp, "run-a", created=PERIOD_START + 1)

    connection = open_for_measurement(db)
    try:
        ledger = build_ownership_ledger(
            connection,
            window_from_ms=PERIOD_START,
            window_to_ms=PERIOD_END,
            v1_ownership=V1OwnershipInput.observed(source=V1_SOURCE, runs=()),
        )
    finally:
        connection.close()

    assert ledger.available is False
    assert ledger.finding_count == 0
    assert len(ledger.entries) == 1


# --------------------------------------------------------------------------
# condition 5 -- the read-only assertion, off the live connection
# --------------------------------------------------------------------------


def test_the_read_only_evidence_comes_off_the_live_connection(db: Path) -> None:
    connection = open_for_measurement(db)
    try:
        evidence = evidence_of_read_only(connection)
    finally:
        connection.close()

    assert evidence.query_only == 1
    assert evidence.query_only_after_probe == 1
    # The path is not an argument to the checker: it can only have come from the
    # connection itself.
    assert Path(evidence.database_path).resolve() == db.resolve()
    assert evidence.uri.endswith("?mode=ro")
    assert db.name in evidence.uri


def test_a_writable_connection_is_refused(db: Path) -> None:
    connection = writable(db)
    try:
        with pytest.raises(ReadOnlyCapabilityRefused) as refusal:
            evidence_of_read_only(connection)
    finally:
        connection.close()
    assert "query_only" in str(refusal.value)


def test_a_claim_of_read_only_does_not_substitute_for_the_capability(
    db: Path,
) -> None:
    """Read-write, with ``query_only`` raised by hand: convention, not capability.

    This connection satisfies every claim a report could print about itself.
    Only asking the file separates it from one opened ``mode=ro``, which is the
    distinction condition 5 is drawing.
    """

    connection = writable(db)
    connection.execute("PRAGMA query_only = ON")
    try:
        with pytest.raises(ReadOnlyCapabilityRefused) as refusal:
            evidence_of_read_only(connection)
    finally:
        connection.close()
    assert "mode=ro" in str(refusal.value)


def test_a_connection_with_no_file_cannot_evidence_the_capability() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA query_only = ON")
    try:
        with pytest.raises(ReadOnlyCapabilityRefused) as refusal:
            evidence_of_read_only(connection)
    finally:
        connection.close()
    assert "no file" in str(refusal.value)


def test_the_report_refuses_before_it_measures_anything(db: Path) -> None:
    """A writable connection stops the report, however much data is behind it."""

    with writable(db) as cp:
        add_run(cp, "shared", created=PERIOD_START + 1)

    connection = writable(db)
    try:
        with pytest.raises(ReadOnlyCapabilityRefused):
            measure_canary_divergence(
                connection,
                period_start_ms=PERIOD_START,
                period_end_ms=PERIOD_END,
                interlock_episodes=(),
                v1_reference=V1Reference.attests_empty(source=V1_SOURCE),
                censored_ids=frozenset(),
                fixture_labels={},
                v1_writer_ledger=V1WriterLedger.attests_empty(source=V1_SOURCE),
                v1_ownership=V1OwnershipInput.observed(
                    source=V1_SOURCE, runs=(v1_owned("shared", PERIOD_START),)
                ),
            )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the report: no verdict, and no missing comparison passed off as a clean one
# --------------------------------------------------------------------------


def report_with_both_findings(db: Path):
    with writable(db) as cp:
        add_run(cp, "shared", created=PERIOD_START + 100)
    return report_over(
        db,
        interlock_episodes=(merge_episode("ours-1", "aa-org/renga", "7", PERIOD_START),),
        v1_reference=V1Reference.observed(
            source=V1_SOURCE,
            episodes=(merge_episode("theirs-1", "aa-org/renga", "9", PERIOD_START),),
        ),
        v1_writer_ledger=V1WriterLedger.observed(
            source=V1_SOURCE,
            records=(
                WrittenRecord(
                    record_class="run",
                    record_key="shared",
                    first_written_at_ms=PERIOD_START + 90,
                    last_written_at_ms=PERIOD_START + 90,
                    store=V1_STORE,
                ),
            ),
        ),
        v1_ownership=V1OwnershipInput.observed(
            source=V1_SOURCE, runs=(v1_owned("shared", PERIOD_START + 90),)
        ),
    )


def test_the_report_states_both_findings_and_still_emits_no_verdict(db: Path) -> None:
    report = report_with_both_findings(db)

    assert report.finding_counts() == {DUAL_WRITE: 1, OWNERSHIP_COLLISION: 1}
    rendered = render_canary_divergence_report(report)

    # The moment a harness would slip a verdict in is the moment something is
    # wrong, so the grep runs over the rendering that has findings in it.
    assert VERDICT_WORDS.search(rendered) is None, rendered
    assert "Q-0005" in rendered
    assert "no verdict on the canary" in rendered
    assert "VIOLATED" in rendered
    assert "shared" in rendered
    assert rendered.isascii()
    rendered.encode("cp932")


def test_a_report_with_no_findings_also_emits_no_verdict(db: Path) -> None:
    report = report_over(
        db,
        v1_reference=V1Reference.attests_empty(source=V1_SOURCE),
        v1_writer_ledger=V1WriterLedger.attests_empty(source=V1_SOURCE),
        v1_ownership=V1OwnershipInput.attests_empty(source=V1_SOURCE),
    )
    rendered = render_canary_divergence_report(report)

    assert report.finding_counts() == {DUAL_WRITE: 0, OWNERSHIP_COLLISION: 0}
    assert VERDICT_WORDS.search(rendered) is None, rendered
    assert "VIOLATED" not in rendered


def test_an_empty_v1_input_renders_and_says_there_is_no_shadow_reference(
    db: Path,
) -> None:
    """Every section says a comparison did not happen, rather than showing zero."""

    with writable(db) as cp:
        add_run(cp, "run-a", created=PERIOD_START + 1)

    report = report_over(
        db,
        interlock_episodes=(merge_episode("ours-1", "aa-org/renga", "7", PERIOD_START),),
        v1_reference=V1Reference.observed(source=V1_SOURCE, episodes=()),
        v1_writer_ledger=V1WriterLedger.observed(source=V1_SOURCE, records=()),
        v1_ownership=V1OwnershipInput.observed(source=V1_SOURCE, runs=()),
    )
    rendered = render_canary_divergence_report(report)

    assert report.reconciliation.available is False
    assert "shadow reference: ABSENT" in rendered
    assert "v1 store: ABSENT" in rendered
    assert "v1 claims: ABSENT" in rendered
    assert "not evidence that none was" in rendered
    assert "only visible as a run both systems claim" in rendered
    # The instrument was still evidenced: an absent comparison is not an absent
    # read-only assertion.
    assert "PRAGMA query_only: 1" in rendered
    assert "?mode=ro" in rendered
    assert VERDICT_WORDS.search(rendered) is None, rendered


def test_an_empty_period_is_refused(db: Path) -> None:
    connection = open_for_measurement(db)
    try:
        with pytest.raises(CanaryRefusal):
            measure_canary_divergence(
                connection,
                period_start_ms=PERIOD_END,
                period_end_ms=PERIOD_END,
                interlock_episodes=(),
                v1_reference=V1Reference.attests_empty(source=V1_SOURCE),
                censored_ids=frozenset(),
                fixture_labels={},
                v1_writer_ledger=V1WriterLedger.attests_empty(source=V1_SOURCE),
                v1_ownership=V1OwnershipInput.attests_empty(source=V1_SOURCE),
            )
    finally:
        connection.close()


def test_the_query_definitions_are_the_queries_that_run() -> None:
    """Provenance that is the executed text, not a description of it."""

    for record_class in RECORD_CLASSES:
        assert (
            QUERY_DEFINITIONS[f"record_class:{record_class.name}"] is record_class.sql
        )
    assert {"run", "pull_request"} == {
        RECORD_CLASS_RUN.name,
        RECORD_CLASS_PULL_REQUEST.name,
    }
