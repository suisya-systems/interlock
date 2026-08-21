"""CI ingestion -- the identity, the refusals at the edge, and the verdict projection.

These tests are for the two failures ``docs/production-schema.md`` section 6.1
names and ``D-0033`` decides against, asserted from the API side rather than from
the DDL. ``tests/control_plane/test_production_schema.py`` already pins what the
``CHECK`` constraints and the ``ci_current_verdict`` view do to hand-written
``INSERT``s; what is unproven until here is that
:mod:`~claude_org_runtime.control_plane.ci_ingest` renders the *same* identity the
unique index enforces, refuses a malformed one before any of it reaches a
transaction, and folds the projection the way section 6.3 rule 5 says.

The cases that would each cost a real result if the module got them wrong:

* a **re-poll** of the identical fact is an idempotent no-op at the *first*
  statement of the append, so nothing downstream of it -- the consumption
  fan-out, the delivery outbox, the evidence row -- sees the repeat;
* an **indeterminate followed by the recovered verdict** for the same attempt is
  a new observation and moves the projection, which is the case that would
  otherwise strand a PR at ``indeterminate`` forever;
* a **head update** invalidates prior verdicts rather than letting them be
  overwritten, and the superseded rows stay in the table as evidence;
* a **late arrival** that orders lower is stored and moves nothing, which is the
  sentence section 6.3 exists to make true;
* a **rollup** stops projecting the moment a fine-grained scope exists;
* the **severity fold** puts ``indeterminate`` above ``passed`` and treats
  ``no_run`` as absent evidence rather than as a pass.

Every timestamp is :data:`T0` and arithmetic on it. The schema gives no timestamp
column a ``DEFAULT`` and no function here reads a clock, so a suite whose
expectations moved with the wall clock would be asserting something the
production code cannot even observe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import events
from claude_org_runtime.control_plane.ci_ingest import (
    CI_VERDICTS,
    VERDICT_SEVERITY,
    EmptyIdentityFieldRefused,
    MalformedAttemptRefused,
    MalformedHeadShaRefused,
    UnknownCheckScopeRefused,
    UnknownVerdictRefused,
    UnsupportedProviderRefused,
    observation_dedup_key,
    pr_verdict,
    record_ci_observation,
    scope_verdicts,
)
from claude_org_runtime.control_plane.migrator import create_production_control_plane

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

SHA_A = "a" * 40
SHA_B = "b" * 40

REPO = "repo-1"
PR_NUMBER = 7


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_production_control_plane(tmp_path / "production.sqlite3", now_ms=T0)
    try:
        add_repository(connection)
        add_pull_request(connection)
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal surroundings an observation needs
# --------------------------------------------------------------------------


def add_repository(cp: sqlite3.Connection, repo_id: str = REPO, at: int = T0) -> str:
    cp.execute(
        """
        INSERT INTO repository (repo_id, provider, provider_repo_id, owner, name,
                                created_at_ms, updated_at_ms)
        VALUES (?, 'github', NULL, 'acme', 'widget', ?, ?)
        """,
        (repo_id, at, at),
    )
    return repo_id


def add_head_event(cp: sqlite3.Connection, head_sha: str, at: int) -> int:
    """A bare spine row for a head observation, so a PR row has a ``head_event_seq``.

    Written with SQL rather than through
    :func:`~claude_org_runtime.control_plane.events.append_event` on purpose: the
    PR head projection is another agent's surface (section 7.2), and a CI test
    that went through it would fail for reasons that have nothing to do with CI.
    """

    cursor = cp.execute(
        """
        INSERT INTO event (event_id, event_type, subject_kind, subject_id, run_id, payload,
                           producer, producer_epoch, dedup_key, occurred_at_ms, ingested_at_ms)
        VALUES (?, 'pr_head_updated', 'pull_request', ?, NULL, '{}', 'gh-watcher', NULL,
                ?, ?, ?)
        """,
        (
            f"evt-head-{head_sha[:4]}-{at}",
            f"{REPO}#{PR_NUMBER}",
            f"pr_head/{REPO}/{PR_NUMBER}/{head_sha}",
            at,
            at,
        ),
    )
    return int(cursor.lastrowid)


def add_pull_request(cp: sqlite3.Connection, head_sha: str = SHA_A, at: int = T0) -> str:
    head_event_seq = add_head_event(cp, head_sha, at)
    cp.execute(
        """
        INSERT INTO pull_request (pr_id, repo_id, pr_number, provider_pr_id, head_sha,
                                  head_observed_at_ms, head_event_seq, state, merge_commit_sha,
                                  merged_at_ms, closed_at_ms, created_at_ms, updated_at_ms)
        VALUES ('pr-1', ?, ?, NULL, ?, ?, ?, 'open', NULL, NULL, NULL, ?, ?)
        """,
        (REPO, PR_NUMBER, head_sha, at, head_event_seq, at, at),
    )
    return "pr-1"


def move_head(cp: sqlite3.Connection, head_sha: str, at: int) -> None:
    """Advance the PR head, in the provider's order the monotonicity trigger requires."""

    head_event_seq = add_head_event(cp, head_sha, at)
    cp.execute(
        """
        UPDATE pull_request
           SET head_sha = ?, head_observed_at_ms = ?, head_event_seq = ?, updated_at_ms = ?
         WHERE pr_id = 'pr-1'
        """,
        (head_sha, at, head_event_seq, at),
    )


def observe(
    cp: sqlite3.Connection,
    *,
    observation_id: str,
    verdict: str,
    at: int,
    head_sha: str = SHA_A,
    check_scope: str = "check_suite",
    scope_id: str = "suite-1",
    attempt: int = 1,
    **kwargs,
):
    return record_ci_observation(
        cp,
        observation_id=observation_id,
        repo_id=REPO,
        pr_number=PR_NUMBER,
        head_sha=head_sha,
        check_scope=check_scope,
        scope_id=scope_id,
        attempt=attempt,
        verdict=verdict,
        observer="gh-watcher",
        observer_epoch=1,
        occurred_at_ms=at,
        ingested_at_ms=at,
        **kwargs,
    )


def register_delivery_consumer(cp: sqlite3.Connection, at: int = T0) -> str:
    """A subscribed delivery consumer, so the fan-out has something to fan out to.

    Without one, "the duplicate append ran nothing downstream" would be true
    vacuously: there would be no consumption row and no outbox row to be absent.
    """

    events.register_consumer(
        cp,
        consumer_id="secretary",
        kind="delivery",
        lease_resource="consumer:secretary",
        registered_at_ms=at,
        registered_from_seq=0,
    )
    events.subscribe(
        cp,
        consumer_id="secretary",
        event_type="ci_observed",
        recipient="secretary",
        added_at_ms=at,
    )
    return "secretary"


def counts(cp: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(cp.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("event", "ci_observation", "event_consumption", "outbox")
    }


def verdict_by_scope(cp: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["scope_id"]): str(row["verdict"])
        for row in scope_verdicts(cp, repo_id=REPO, pr_number=PR_NUMBER)
    }


# --------------------------------------------------------------------------
# the identity, rendered once
# --------------------------------------------------------------------------


def test_the_dedup_key_is_the_identity_tuple_rendered_in_the_documented_order():
    assert (
        observation_dedup_key(
            repo_id=REPO,
            pr_number=PR_NUMBER,
            head_sha=SHA_A,
            check_scope="check_suite",
            scope_id="suite-1",
            attempt=2,
            verdict="failed",
        )
        == f"ci/github/{REPO}/7/{SHA_A}/check_suite/suite-1/2/failed"
    )


def test_the_appended_event_carries_the_rendered_identity_as_its_dedup_key(cp):
    appended = observe(cp, observation_id="obs-1", verdict="passed", at=T0)

    dedup_key = cp.execute(
        "SELECT dedup_key FROM event WHERE seq = ?", (appended.seq,)
    ).fetchone()[0]
    assert dedup_key == observation_dedup_key(
        repo_id=REPO,
        pr_number=PR_NUMBER,
        head_sha=SHA_A,
        check_scope="check_suite",
        scope_id="suite-1",
        attempt=1,
        verdict="passed",
    )


def test_the_evidence_row_is_linked_to_the_seq_the_append_assigned(cp):
    appended = observe(cp, observation_id="obs-1", verdict="passed", at=T0)

    assert appended.duplicate is False
    assert (
        cp.execute(
            "SELECT event_seq FROM ci_observation WHERE observation_id = 'obs-1'"
        ).fetchone()[0]
        == appended.seq
    )


# --------------------------------------------------------------------------
# idempotency and the recovered verdict
# --------------------------------------------------------------------------


def test_a_repoll_of_the_identical_fact_is_a_noop_at_the_first_statement_of_the_append(cp):
    register_delivery_consumer(cp)
    observe(cp, observation_id="obs-1", verdict="passed", at=T0)
    before = counts(cp)

    repoll = observe(cp, observation_id="obs-2", verdict="passed", at=T0 + 5_000)

    assert repoll.duplicate is True
    assert repoll.seq is None
    # Nothing downstream of statement 1 ran: no second consumption row, no
    # second outbox row, and no evidence row for the re-poll's own id.
    assert repoll.consumptions == ()
    assert repoll.messages == ()
    assert counts(cp) == before
    assert (
        cp.execute(
            "SELECT COUNT(*) FROM ci_observation WHERE observation_id = 'obs-2'"
        ).fetchone()[0]
        == 0
    )


def test_a_recovered_verdict_for_the_same_attempt_appends_and_moves_the_projection(cp):
    observe(cp, observation_id="obs-1", verdict="indeterminate", at=T0 + 1_000)
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "indeterminate"

    recovered = observe(cp, observation_id="obs-2", verdict="failed", at=T0 + 2_000)

    assert recovered.duplicate is False
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "failed"
    # The indeterminate observation is not deleted or overwritten; it is what we
    # actually saw at the time.
    assert (
        cp.execute(
            "SELECT verdict FROM ci_observation WHERE observation_id = 'obs-1'"
        ).fetchone()[0]
        == "indeterminate"
    )


def test_a_repeat_of_the_recovered_verdict_is_still_refused(cp):
    observe(cp, observation_id="obs-1", verdict="indeterminate", at=T0 + 1_000)
    observe(cp, observation_id="obs-2", verdict="failed", at=T0 + 2_000)

    again = observe(cp, observation_id="obs-3", verdict="failed", at=T0 + 3_000)

    assert again.duplicate is True
    assert (
        cp.execute(
            "SELECT COUNT(*) FROM ci_observation WHERE verdict = 'failed'"
        ).fetchone()[0]
        == 1
    )


# --------------------------------------------------------------------------
# ordering: the head, and the late arrival
# --------------------------------------------------------------------------


def test_a_head_update_invalidates_prior_verdicts_instead_of_letting_them_be_overwritten(cp):
    observe(cp, observation_id="obs-1", verdict="failed", at=T0 + 1_000)
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "failed"

    move_head(cp, SHA_B, T0 + 2_000)

    # The old verdict is gone from the projection without anything having been
    # written over it, and there is no evidence at all for the new head yet.
    assert scope_verdicts(cp, repo_id=REPO, pr_number=PR_NUMBER) == ()
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "no_run"
    assert (
        cp.execute(
            "SELECT head_sha, verdict FROM ci_observation WHERE observation_id = 'obs-1'"
        ).fetchone()
        == (SHA_A, "failed")
    )


def test_a_superseded_head_observation_is_never_eligible_again(cp):
    observe(cp, observation_id="obs-1", verdict="failed", at=T0 + 1_000)
    move_head(cp, SHA_B, T0 + 2_000)

    observe(cp, observation_id="obs-2", verdict="passed", at=T0 + 3_000, head_sha=SHA_B)

    assert verdict_by_scope(cp) == {"suite-1": "passed"}
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "passed"
    assert int(cp.execute("SELECT COUNT(*) FROM ci_observation").fetchone()[0]) == 2


def test_a_late_arrival_that_orders_lower_is_stored_and_does_not_move_the_projection(cp):
    observe(cp, observation_id="obs-late-loser", verdict="failed", at=T0 + 2_000)
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "failed"

    late = observe(cp, observation_id="obs-2", verdict="passed", at=T0 + 1_000)

    assert late.duplicate is False
    assert (
        cp.execute(
            "SELECT COUNT(*) FROM ci_observation WHERE observation_id = 'obs-2'"
        ).fetchone()[0]
        == 1
    )
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "failed"


def test_a_higher_attempt_wins_over_an_earlier_one_even_when_it_arrives_first(cp):
    observe(cp, observation_id="obs-2", verdict="passed", at=T0 + 1_000, attempt=2)
    observe(cp, observation_id="obs-1", verdict="failed", at=T0 + 2_000, attempt=1)

    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "passed"


# --------------------------------------------------------------------------
# the rollup's subordinate eligibility, and the severity fold
# --------------------------------------------------------------------------


def test_a_rollup_drops_out_of_the_projection_once_a_finegrained_scope_exists(cp):
    observe(
        cp,
        observation_id="obs-rollup",
        verdict="failed",
        at=T0 + 1_000,
        check_scope="rollup",
        scope_id="head",
    )
    assert verdict_by_scope(cp) == {"head": "failed"}

    observe(cp, observation_id="obs-suite", verdict="passed", at=T0 + 2_000)

    assert verdict_by_scope(cp) == {"suite-1": "passed"}
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "passed"


def test_indeterminate_outranks_passed_because_an_unobservable_check_is_not_a_green_one(cp):
    observe(cp, observation_id="obs-1", verdict="passed", at=T0 + 1_000, scope_id="suite-1")
    observe(
        cp,
        observation_id="obs-2",
        verdict="indeterminate",
        at=T0 + 1_000,
        scope_id="suite-2",
    )

    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "indeterminate"


def test_failed_outranks_every_other_verdict_in_the_fold(cp):
    for index, verdict in enumerate(("passed", "cancelled", "timed_out", "failed")):
        observe(
            cp,
            observation_id=f"obs-{index}",
            verdict=verdict,
            at=T0 + 1_000,
            scope_id=f"suite-{index}",
        )

    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "failed"


def test_no_run_is_absent_evidence_rather_than_a_pass(cp):
    observe(cp, observation_id="obs-1", verdict="no_run", at=T0 + 1_000)

    assert verdict_by_scope(cp) == {"suite-1": "no_run"}
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "no_run"


def test_a_no_run_scope_never_outvotes_a_real_verdict(cp):
    observe(cp, observation_id="obs-1", verdict="no_run", at=T0 + 1_000, scope_id="suite-1")
    observe(cp, observation_id="obs-2", verdict="passed", at=T0 + 1_000, scope_id="suite-2")

    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "passed"


def test_a_pull_request_with_no_observation_at_all_is_absent_evidence(cp):
    assert pr_verdict(cp, repo_id=REPO, pr_number=PR_NUMBER) == "no_run"


def test_the_severity_order_ranks_every_member_of_the_closed_verdict_set(cp):
    assert set(VERDICT_SEVERITY) == CI_VERDICTS
    assert (
        VERDICT_SEVERITY["failed"]
        > VERDICT_SEVERITY["timed_out"]
        > VERDICT_SEVERITY["cancelled"]
        > VERDICT_SEVERITY["indeterminate"]
        > VERDICT_SEVERITY["passed"]
    )


# --------------------------------------------------------------------------
# refusals at the edge
# --------------------------------------------------------------------------


def test_a_case_variant_provider_is_refused_at_the_edge(cp):
    with pytest.raises(UnsupportedProviderRefused):
        observe(cp, observation_id="obs-1", verdict="passed", at=T0, provider="GITHUB")

    assert counts(cp)["ci_observation"] == 0


def test_an_abbreviated_head_sha_is_refused_because_two_heads_can_share_a_prefix(cp):
    with pytest.raises(MalformedHeadShaRefused):
        observe(cp, observation_id="obs-1", verdict="passed", at=T0, head_sha=SHA_A[:7])


def test_an_upper_case_head_sha_is_refused(cp):
    with pytest.raises(MalformedHeadShaRefused):
        observe(cp, observation_id="obs-1", verdict="passed", at=T0, head_sha=SHA_A.upper())


def test_an_attempt_below_one_is_refused(cp):
    with pytest.raises(MalformedAttemptRefused):
        observe(cp, observation_id="obs-1", verdict="passed", at=T0, attempt=0)


def test_a_verdict_outside_the_closed_set_is_refused(cp):
    with pytest.raises(UnknownVerdictRefused):
        observe(cp, observation_id="obs-1", verdict="green", at=T0)


def test_a_check_scope_outside_the_closed_set_is_refused(cp):
    with pytest.raises(UnknownCheckScopeRefused):
        observe(cp, observation_id="obs-1", verdict="passed", at=T0, check_scope="job")


def test_an_empty_scope_id_is_refused_so_the_rendered_key_stays_unambiguous(cp):
    with pytest.raises(EmptyIdentityFieldRefused):
        observe(cp, observation_id="obs-1", verdict="passed", at=T0, scope_id="")


def test_a_refused_observation_appends_no_event(cp):
    register_delivery_consumer(cp)
    before = counts(cp)

    with pytest.raises(MalformedHeadShaRefused):
        observe(cp, observation_id="obs-1", verdict="passed", at=T0, head_sha="abc")

    assert counts(cp) == before
