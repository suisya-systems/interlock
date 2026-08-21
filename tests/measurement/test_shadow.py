"""Four keys composed from real rows, five buckets that partition, and no miss without a verdict.

``shadow.py`` can be wrong in three ways that a cheerful test suite would never
notice, so each one gets adversarial treatment here:

* **A key that does not compose from what the schema stores.** Every key test
  builds the rows through the production schema and the real writers
  (``repo_link``, ``ci_ingest``, ``gates.open_gate``) and then asserts the key's
  components, so a key that quietly needed a column the schema has not got fails
  at insert time rather than passing against a hand-built dictionary.
* **A lowercasing that is a second source of truth.** A test that stored a
  lowercase slug would pass whether the fold happened in SQL, in Python, or not
  at all. So the repository is stored as ``Aa-Org/Renga`` -- case preserved, as
  ``0001_initial.sql`` requires -- and the v1 adapter hands over the key spelled
  the way v1 spells it. If the fold is missing the episodes do not pair, and the
  test sees a fabricated ``interlock_only`` plus a fabricated candidate miss.
* **A ``v1_only`` episode that becomes a number without anyone deciding.**
  Section 3.3's rule has two halves and both are tested from the outside: the
  miss count *refuses* while a candidate is open, and the open candidate is
  still in the bucket, still counted, and still printed.

The reconciliation tests are pure -- no connection, no clock -- because the
reconciliation is. Where a test needs a censored episode it goes through
``windows.classify_episodes`` and :func:`shadow.censored_episode_ids` rather than
naming an id by hand, so the integration between the two modules is exercised by
the same assertion that exercises the bucket.

Nothing here re-implements the module to compare against; expected keys, buckets
and counts are written out by hand.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import ci_ingest, policy, repo_link
from claude_org_runtime.control_plane.gates import open_gate
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.reader import open_for_measurement
from claude_org_runtime.measurement.shadow import (
    ADJUDICATIONS,
    AWAITING_HUMAN,
    BOTH,
    CENSORED,
    FROM_FIXTURE_LABEL,
    INTERLOCK_ONLY,
    MISS,
    ONSET_BUCKET_MS,
    POSITIONAL_KEY_CAVEAT,
    RECONCILIATION_BUCKETS,
    SHADOW_ABSENT,
    SHADOW_PRESENT,
    SUBJECT_CI_OUTCOME,
    SUBJECT_PR_MERGE,
    SUBJECT_SESSION_LIVENESS,
    SUBJECT_WORKER_ESCALATION,
    UNMATCHED_KEY,
    UNDETERMINED,
    V1_FALSE_POSITIVE,
    V1_ONLY,
    AdjudicationPending,
    CorrelationKey,
    DuplicateCorrelationKey,
    DuplicateEpisodeIdRefused,
    EpisodeKeyRefused,
    ShadowEpisode,
    ShadowReferenceAbsent,
    ShadowRefusal,
    UnknownAdjudication,
    UnknownSubjectClass,
    V1Reference,
    censored_episode_ids,
    read_ci_outcome_episodes,
    read_interlock_episodes,
    read_pr_merge_episodes,
    read_session_liveness_episodes,
    read_worker_escalation_episodes,
    reconcile,
    render_shadow_reconciliation,
)
from claude_org_runtime.measurement.windows import Episode, classify_episodes

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
MINUTE = 60_000
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS

#: ``0002_policy_seed.sql``'s revision, found by note rather than assumed to be 1.
SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)

#: An absolute-``L`` class, used where a test needs a window and not a policy
#: argument (``time-base-policy.md`` section 3.2).
ABSOLUTE_CLASS = "session_no_evidence"

SHA_A = "a" * 40
SHA_B = "b" * 40

#: The fact_state a session-liveness episode carries in these fixtures.
#: ``incident.fact_state`` is unconstrained text, so the reader is *told* which
#: states are the class -- which is exactly what the reader refuses to guess.
LIVENESS_STATE = "session_no_evidence"


# --------------------------------------------------------------------------
# helpers -- the world, built through the real writers
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def writable(path: Path) -> sqlite3.Connection:
    """An ordinary writable handle -- deliberately not the harness's.

    The harness's connection cannot write (``reader.py``); fixtures are built
    through a second connection rather than by relaxing that property.
    """

    return sqlite3.connect(path, isolation_level=None)


def add_run(cp: sqlite3.Connection, run_id: str, *, at: int = T0) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
        " VALUES (?, 'running', ?, ?)",
        (run_id, at, at),
    )
    return run_id


def add_session(
    cp: sqlite3.Connection, session_id: str, run_id: str, *, at: int = T0
) -> str:
    cp.execute(
        """
        INSERT INTO session (session_id, run_id, provider, binding_phase,
                             observation, observation_reason, bound_at_ms)
        VALUES (?, ?, 'agent_view', 'spawned', 'unobserved', 'not read back yet', ?)
        """,
        (session_id, run_id, at),
    )
    return session_id


def add_incident(
    cp: sqlite3.Connection,
    incident_id: str,
    *,
    run_id: str | None,
    session_id: str | None,
    created_at_ms: int,
    elapsed_ms: int | None,
    fact_state: str = LIVENESS_STATE,
) -> str:
    cp.execute(
        """
        INSERT INTO incident (incident_id, run_id, session_id, fact_state,
                              detector_version, dedup_key, elapsed_ms,
                              created_at_ms, updated_at_ms)
        VALUES (?, ?, ?, ?, 'd-1', ?, ?, ?, ?)
        """,
        (
            incident_id,
            run_id,
            session_id,
            fact_state,
            f"dk/{incident_id}",
            elapsed_ms,
            created_at_ms,
            created_at_ms,
        ),
    )
    return incident_id


def add_origin_event(cp: sqlite3.Connection, event_id: str, run_id: str, at: int) -> int:
    cursor = cp.execute(
        """
        INSERT INTO event (event_id, event_type, subject_kind, subject_id, run_id,
                           producer, dedup_key, occurred_at_ms, ingested_at_ms)
        VALUES (?, 'worker_escalation_raised', 'run', ?, ?, 'worker', ?, ?, ?)
        """,
        (event_id, run_id, run_id, f"dk/{event_id}", at, at),
    )
    return int(cursor.lastrowid)


def add_escalation(
    cp: sqlite3.Connection, gate_id: str, *, run_id: str | None, at: int
) -> str:
    """One ``worker_escalation`` gate at ``received``, opened the real way."""

    seq = add_origin_event(cp, f"evt/{gate_id}", run_id or "run-orphan-origin", at)
    open_gate(
        cp,
        gate_id=gate_id,
        gate_type="worker_escalation",
        subject_kind="run",
        subject_id=run_id or "no-run",
        rationale="the worker asked",
        origin_event_seq=seq,
        created_at_ms=at,
        actor_kind="worker",
        actor_id="worker-1",
        run_id=run_id,
    )
    return gate_id


def add_repository(cp: sqlite3.Connection, repo_id: str, owner: str, name: str) -> str:
    return repo_link.upsert_repository(
        cp, repo_id=repo_id, owner=owner, name=name, now_ms=T0
    )


def add_pull_request(
    cp: sqlite3.Connection,
    *,
    repo_id: str,
    pr_number: int,
    head_sha: str,
    state: str = "open",
    observed_at_ms: int = T0,
    merged_at_ms: int | None = None,
    merge_commit_sha: str | None = None,
    closed_at_ms: int | None = None,
    event_id: str = "evt-pr",
) -> None:
    repo_link.observe_pull_request(
        cp,
        repo_id=repo_id,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
        observed_at_ms=observed_at_ms,
        ingested_at_ms=observed_at_ms,
        event_id=event_id,
        producer="pr_watcher",
        merged_at_ms=merged_at_ms,
        merge_commit_sha=merge_commit_sha,
        closed_at_ms=closed_at_ms,
    )


def add_ci_observation(
    cp: sqlite3.Connection,
    *,
    observation_id: str,
    repo_id: str,
    pr_number: int,
    head_sha: str,
    check_scope: str,
    scope_id: str,
    verdict: str,
    occurred_at_ms: int,
) -> None:
    ci_ingest.record_ci_observation(
        cp,
        observation_id=observation_id,
        repo_id=repo_id,
        pr_number=pr_number,
        head_sha=head_sha,
        check_scope=check_scope,
        scope_id=scope_id,
        attempt=1,
        verdict=verdict,
        observer="pr_watcher",
        observer_epoch=1,
        occurred_at_ms=occurred_at_ms,
        ingested_at_ms=occurred_at_ms,
    )


def seed_revision_id(path: Path) -> int:
    connection = writable(path)
    try:
        row = connection.execute(
            "SELECT revision_id FROM policy_revision WHERE note = ?", (SEED_NOTE,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, "0002_policy_seed.sql must have applied"
    return int(row[0])


def an_episode(
    episode_id: str,
    *,
    subject_class: str = SUBJECT_PR_MERGE,
    parts: tuple[str, ...] | None = ("github", "o/r", "1"),
    shape: str = "merged",
    onset_ms: int = T0,
    key_gap: str | None = None,
) -> ShadowEpisode:
    key = (
        None
        if parts is None
        else CorrelationKey(subject_class=subject_class, parts=parts)
    )
    return ShadowEpisode(
        episode_id=episode_id,
        subject_class=subject_class,
        shape=shape,
        onset_ms=onset_ms,
        key=key,
        key_gap=key_gap,
        evidence={"note": "fixture"},
    )


def reconciled(
    interlock,
    v1,
    *,
    censored_ids=(),
    fixture_labels=None,
    source: str = "v1-adapter",
):
    reference = (
        v1
        if isinstance(v1, V1Reference)
        else V1Reference.observed(source=source, episodes=v1)
    )
    return reconcile(
        period_start_ms=PERIOD_START,
        period_end_ms=PERIOD_END,
        interlock_episodes=interlock,
        v1_reference=reference,
        censored_ids=censored_ids,
        fixture_labels={} if fixture_labels is None else fixture_labels,
    )


# --------------------------------------------------------------------------
# the four correlation keys, composed from what the schema actually stores
# --------------------------------------------------------------------------


def test_the_ci_outcome_key_composes_from_ci_observation_joined_to_repository(db: Path):
    """Provider, folded slug, PR number and head -- and the outcome is the projection.

    The two scopes disagree on purpose. A reader that took the newest
    observation would report ``passed``; section 6.3 rule 5 says the head's
    verdict is the most severe of its eligible scopes, and this reader gets it
    by calling ``ci_ingest.pr_verdict`` rather than folding a second time.
    """

    cp = writable(db)
    try:
        repo_id = add_repository(cp, "repo-1", "Aa-Org", "Renga")
        add_pull_request(cp, repo_id=repo_id, pr_number=302, head_sha=SHA_A)
        add_ci_observation(
            cp,
            observation_id="obs-1",
            repo_id=repo_id,
            pr_number=302,
            head_sha=SHA_A,
            check_scope="check_suite",
            scope_id="suite-1",
            verdict="failed",
            occurred_at_ms=T0 + MINUTE,
        )
        add_ci_observation(
            cp,
            observation_id="obs-2",
            repo_id=repo_id,
            pr_number=302,
            head_sha=SHA_A,
            check_scope="workflow_run",
            scope_id="wf-1",
            verdict="passed",
            occurred_at_ms=T0 + 2 * MINUTE,
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_ci_outcome_episodes(
            connection, onset_from_ms=PERIOD_START, onset_to_ms=PERIOD_END
        )
    finally:
        connection.close()

    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.subject_class == SUBJECT_CI_OUTCOME
    assert episode.key is not None
    assert episode.key.parts == ("github", "aa-org/renga", "302", SHA_A)
    assert episode.shape == "failed", "a green scope must not soften a red one"
    assert episode.onset_ms == T0 + MINUTE, (
        "the onset is the provider's earliest eligible observation of this head, "
        "not our ingest of it"
    )
    assert not episode.key.positional


def test_a_case_differing_repo_slug_still_matches_the_v1_spelling(db: Path):
    """The fold is real, and it is the database's own fold.

    ``repository`` preserves ``Aa-Org/Renga`` in its columns (only
    ``repository_by_slug`` folds), so a key built without folding would read
    ``Aa-Org/Renga`` and never meet v1's ``aa-org/renga``. The pairing is the
    assertion: unfolded, this test sees one ``interlock_only`` and one candidate
    miss instead of one ``both``.
    """

    cp = writable(db)
    try:
        repo_id = add_repository(cp, "repo-1", "Aa-Org", "Renga")
        add_pull_request(
            cp,
            repo_id=repo_id,
            pr_number=302,
            head_sha=SHA_A,
            state="merged",
            merged_at_ms=T0 + MINUTE,
            merge_commit_sha=SHA_B,
            closed_at_ms=T0 + MINUTE,
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        ours = read_pr_merge_episodes(
            connection, onset_from_ms=PERIOD_START, onset_to_ms=PERIOD_END
        )
    finally:
        connection.close()

    v1_episode = ShadowEpisode(
        episode_id="v1-merge-302",
        subject_class=SUBJECT_PR_MERGE,
        shape="merged",
        onset_ms=T0 + MINUTE,
        key=CorrelationKey(
            subject_class=SUBJECT_PR_MERGE,
            # v1 stored a pr_url and normalised it to a lowercase slug.
            parts=("github", "aa-org/renga", "302"),
        ),
    )

    report = reconciled(ours, [v1_episode])
    assert report.counts()[BOTH] == 1
    assert report.counts()[INTERLOCK_ONLY] == 0
    assert report.counts()[V1_ONLY] == 0


def test_the_pr_merge_key_omits_the_head_and_onsets_at_the_provider_merge(db: Path):
    """Three components, not four, and the merge instant is the provider's."""

    cp = writable(db)
    try:
        repo_id = add_repository(cp, "repo-1", "owner", "repo")
        add_pull_request(cp, repo_id=repo_id, pr_number=7, head_sha=SHA_A)
        add_pull_request(
            cp,
            repo_id=repo_id,
            pr_number=7,
            head_sha=SHA_B,
            state="merged",
            observed_at_ms=T0 + 3 * MINUTE,
            merged_at_ms=T0 + 3 * MINUTE,
            merge_commit_sha=SHA_B,
            closed_at_ms=T0 + 3 * MINUTE,
            event_id="evt-pr-merged",
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_pr_merge_episodes(
            connection, onset_from_ms=PERIOD_START, onset_to_ms=PERIOD_END
        )
    finally:
        connection.close()

    assert len(episodes) == 1
    assert episodes[0].key is not None
    assert episodes[0].key.parts == ("github", "owner/repo", "7")
    assert episodes[0].onset_ms == T0 + 3 * MINUTE


def test_escalations_are_numbered_over_the_runs_whole_history_not_the_window(db: Path):
    """The positional ordinal must not change when the report period changes.

    Three escalations; the window admits only the last two. Numbering within the
    window would call them 1 and 2 -- and a daily report would then pair the
    run's third escalation with v1's first.
    """

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        add_escalation(cp, "gate-1", run_id="run-1", at=T0)
        add_escalation(cp, "gate-2", run_id="run-1", at=T0 + MINUTE)
        add_escalation(cp, "gate-3", run_id="run-1", at=T0 + 2 * MINUTE)
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_worker_escalation_episodes(
            connection,
            onset_from_ms=T0 + MINUTE,
            onset_to_ms=T0 + 3 * MINUTE,
        )
    finally:
        connection.close()

    assert [episode.episode_id for episode in episodes] == [
        "escalation:gate-2",
        "escalation:gate-3",
    ]
    assert [episode.key.parts for episode in episodes] == [
        ("run-1", "2"),
        ("run-1", "3"),
    ]
    assert all(episode.key.positional for episode in episodes)
    assert all(episode.positional_key for episode in episodes)


def test_an_escalation_without_a_run_lands_in_unmatched_key_rather_than_being_dropped(
    db: Path,
):
    """A key component the row does not have is a bucket, not a disappearance."""

    cp = writable(db)
    try:
        add_run(cp, "run-orphan-origin")
        add_escalation(cp, "gate-orphan", run_id=None, at=T0)
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_worker_escalation_episodes(
            connection, onset_from_ms=PERIOD_START, onset_to_ms=PERIOD_END
        )
    finally:
        connection.close()

    assert len(episodes) == 1
    assert episodes[0].key is None
    assert "run_id" in episodes[0].key_gap

    report = reconciled(episodes, [an_episode("v1-1")])
    assert report.counts()[UNMATCHED_KEY] == 1
    assert episodes[0].episode_id in report.filed_episode_ids()


def test_the_session_liveness_key_buckets_the_onset_and_not_the_detection(db: Path):
    """Onset is ``created_at_ms - elapsed_ms``, bucketed to 60 s.

    The two incidents were *raised* five minutes apart and the condition began
    within the same minute in both. Keying on ``created_at_ms`` would put them
    in different buckets, which is the reconciliation disagreeing about identity
    precisely because the two detectors have different latencies -- the quantity
    AC-10 is trying to measure.
    """

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        onset = T0 + 10 * MINUTE
        add_incident(
            cp,
            "inc-fast",
            run_id="run-1",
            session_id=None,
            created_at_ms=onset + MINUTE,
            elapsed_ms=MINUTE,
        )
        add_incident(
            cp,
            "inc-slow",
            run_id="run-1",
            session_id=None,
            created_at_ms=onset + 6 * MINUTE + 30_000,
            elapsed_ms=6 * MINUTE + 30_000,
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_session_liveness_episodes(
            connection,
            onset_from_ms=PERIOD_START,
            onset_to_ms=PERIOD_END,
            fact_states=(LIVENESS_STATE,),
        )
    finally:
        connection.close()

    assert len(episodes) == 2
    assert {episode.onset_ms for episode in episodes} == {T0 + 10 * MINUTE}
    assert {episode.key.parts for episode in episodes} == {
        ("run-1", str((T0 + 10 * MINUTE) // ONSET_BUCKET_MS))
    }


def test_session_liveness_recovers_the_run_through_the_session_binding(db: Path):
    """Section 3.3 says ``incident`` joined to ``session``, and this is why."""

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        add_session(cp, "sess-1", "run-1")
        add_incident(
            cp,
            "inc-1",
            run_id=None,
            session_id="sess-1",
            created_at_ms=T0 + 2 * MINUTE,
            elapsed_ms=MINUTE,
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_session_liveness_episodes(
            connection,
            onset_from_ms=PERIOD_START,
            onset_to_ms=PERIOD_END,
            fact_states=(LIVENESS_STATE,),
        )
    finally:
        connection.close()

    assert episodes[0].key.parts[0] == "run-1"


def test_an_incident_with_no_elapsed_ms_cannot_state_an_onset_and_is_unmatched(db: Path):
    """The nullable column is a key gap, not a licence to use the detection time."""

    cp = writable(db)
    try:
        add_run(cp, "run-1")
        add_incident(
            cp,
            "inc-1",
            run_id="run-1",
            session_id=None,
            created_at_ms=T0 + 2 * MINUTE,
            elapsed_ms=None,
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_session_liveness_episodes(
            connection,
            onset_from_ms=PERIOD_START,
            onset_to_ms=PERIOD_END,
            fact_states=(LIVENESS_STATE,),
        )
    finally:
        connection.close()

    assert len(episodes) == 1
    assert episodes[0].key is None
    assert "elapsed_ms" in episodes[0].key_gap

    report = reconciled(episodes, [an_episode("v1-1")])
    assert report.counts()[UNMATCHED_KEY] == 1


def test_the_liveness_reader_refuses_to_guess_which_fact_states_are_the_class(db: Path):
    """No silent default: the schema does not carry the closed set, so the caller does."""

    connection = open_for_measurement(db)
    try:
        with pytest.raises(ShadowRefusal) as raised:
            read_session_liveness_episodes(
                connection,
                onset_from_ms=PERIOD_START,
                onset_to_ms=PERIOD_END,
                fact_states=(),
            )
    finally:
        connection.close()
    assert "fact_state" in str(raised.value)


def test_read_interlock_episodes_covers_every_subject_class(db: Path):
    """One call, four classes -- a class read by nobody would be all candidate miss."""

    cp = writable(db)
    try:
        repo_id = add_repository(cp, "repo-1", "owner", "repo")
        add_pull_request(
            cp,
            repo_id=repo_id,
            pr_number=7,
            head_sha=SHA_A,
            state="merged",
            merged_at_ms=T0 + MINUTE,
            merge_commit_sha=SHA_B,
            closed_at_ms=T0 + MINUTE,
        )
        add_ci_observation(
            cp,
            observation_id="obs-1",
            repo_id=repo_id,
            pr_number=7,
            head_sha=SHA_A,
            check_scope="check_suite",
            scope_id="suite-1",
            verdict="passed",
            occurred_at_ms=T0 + MINUTE,
        )
        add_run(cp, "run-1")
        add_escalation(cp, "gate-1", run_id="run-1", at=T0 + MINUTE)
        add_incident(
            cp,
            "inc-1",
            run_id="run-1",
            session_id=None,
            created_at_ms=T0 + 3 * MINUTE,
            elapsed_ms=MINUTE,
        )
    finally:
        cp.close()

    connection = open_for_measurement(db)
    try:
        episodes = read_interlock_episodes(
            connection,
            onset_from_ms=PERIOD_START,
            onset_to_ms=PERIOD_END,
            liveness_fact_states=(LIVENESS_STATE,),
        )
    finally:
        connection.close()

    assert {episode.subject_class for episode in episodes} == {
        SUBJECT_CI_OUTCOME,
        SUBJECT_PR_MERGE,
        SUBJECT_WORKER_ESCALATION,
        SUBJECT_SESSION_LIVENESS,
    }


def test_a_selection_window_that_is_empty_or_inverted_is_refused(db: Path):
    connection = open_for_measurement(db)
    try:
        with pytest.raises(ShadowRefusal):
            read_pr_merge_episodes(
                connection, onset_from_ms=PERIOD_END, onset_to_ms=PERIOD_START
            )
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the no-shadow-reference state
# --------------------------------------------------------------------------


def test_an_empty_v1_input_is_the_no_shadow_reference_state_not_all_interlock_only():
    """The whole point of the adapter, and the flattering answer it refuses.

    Nine Interlock episodes and an adapter that returned nothing: reconciled
    naively that is nine improvements and no miss anywhere -- a perfect period
    produced by the *absence* of data. The report says instead that it had no
    second observer, and every comparison accessor refuses.
    """

    ours = [an_episode(f"ours-{n}", parts=("github", "o/r", str(n))) for n in range(9)]

    report = reconciled(ours, V1Reference.observed(source="v1-adapter", episodes=()))

    assert report.available is False
    assert report.shadow_reference == SHADOW_ABSENT
    assert report.interlock_episode_count == 9
    assert "no episodes" in report.shadow_absent_reason
    for accessor in (
        report.counts,
        report.filed_episode_ids,
        report.awaiting_adjudication,
        report.adjudication_counts,
        report.confirmed_miss_count,
    ):
        with pytest.raises(ShadowReferenceAbsent):
            accessor()

    rendered = render_shadow_reconciliation(report)
    assert "ABSENT" in rendered
    assert INTERLOCK_ONLY not in rendered, (
        "a report with no reference must not print a bucket a reader could take "
        "for a comparison"
    )


def test_an_absent_reference_must_say_why_and_the_reason_reaches_the_report():
    report = reconciled(
        [an_episode("ours-1")],
        V1Reference.absent(reason="outside the shadow period"),
    )
    assert report.shadow_reference == SHADOW_ABSENT
    assert "outside the shadow period" in render_shadow_reconciliation(report)


def test_attesting_empty_is_the_explicit_way_to_say_v1_saw_nothing():
    """The real state exists and is reachable -- but only on purpose."""

    report = reconciled(
        [an_episode("ours-1")], V1Reference.attests_empty(source="v1-adapter")
    )
    assert report.shadow_reference == SHADOW_PRESENT
    assert report.counts()[INTERLOCK_ONLY] == 1
    assert report.counts()[V1_ONLY] == 0


# --------------------------------------------------------------------------
# v1_only: neither counted nor discarded without a verdict
# --------------------------------------------------------------------------


def test_a_v1_only_episode_is_never_counted_as_a_miss_without_a_classification():
    """Both halves of section 3.3's rule, from the outside.

    Not counted: the only method that returns a miss number refuses, and names
    the episode. Not discarded: the same episode is in the bucket, in the
    counts, in the awaiting list, and in the rendered report.
    """

    candidate = an_episode("v1-1", shape="relay_gap", parts=("github", "o/r", "9"))
    report = reconciled([], [candidate])

    assert report.counts()[V1_ONLY] == 1
    assert [item.episode.episode_id for item in report.v1_only] == ["v1-1"]
    assert [item.episode.episode_id for item in report.awaiting_adjudication()] == ["v1-1"]
    assert report.adjudication_counts()[AWAITING_HUMAN] == 1

    with pytest.raises(AdjudicationPending) as raised:
        report.confirmed_miss_count()
    assert "v1-1" in str(raised.value)

    rendered = render_shadow_reconciliation(report)
    assert "awaiting human adjudication" in rendered
    assert "v1-1" in rendered
    assert "confirmed misses" not in rendered


def test_a_fixture_label_settles_a_candidate_and_a_false_positive_is_not_a_miss():
    """A label makes the count available; ``v1_false_positive`` keeps it at zero.

    The second half matters more than the first: v1 raising something we did not
    is a miss *or* v1's own false positive, and a harness that assumed the
    former would report AC-10 failing every time v1 alarmed on nothing.
    """

    missed = an_episode("v1-miss", shape="relay_gap", parts=("github", "o/r", "1"))
    bogus = an_episode("v1-bogus", shape="ghost", parts=("github", "o/r", "2"))
    report = reconciled(
        [],
        [missed, bogus],
        fixture_labels={"relay_gap": MISS, "ghost": V1_FALSE_POSITIVE},
    )

    assert report.awaiting_adjudication() == ()
    assert report.confirmed_miss_count() == 1
    assert report.adjudication_counts() == {
        MISS: 1,
        V1_FALSE_POSITIVE: 1,
        UNDETERMINED: 0,
        AWAITING_HUMAN: 0,
    }
    assert [item.adjudication_source for item in report.v1_only] == [
        FROM_FIXTURE_LABEL,
        FROM_FIXTURE_LABEL,
    ]
    assert "confirmed misses: 1" in render_shadow_reconciliation(report)


def test_undetermined_is_a_settled_answer_and_is_not_a_miss():
    """``D-0006``'s "cannot determine is a legitimate outcome", applied to the report."""

    report = reconciled(
        [],
        [an_episode("v1-1", shape="murky")],
        fixture_labels={"murky": UNDETERMINED},
    )
    assert report.awaiting_adjudication() == ()
    assert report.confirmed_miss_count() == 0
    assert report.adjudication_counts()[UNDETERMINED] == 1


def test_a_fixture_label_outside_the_vocabulary_is_refused():
    with pytest.raises(UnknownAdjudication):
        reconciled([], [an_episode("v1-1")], fixture_labels={"merged": "probably"})


# --------------------------------------------------------------------------
# the partition, and censoring's precedence
# --------------------------------------------------------------------------


def test_the_five_buckets_partition_the_input_with_no_double_counting():
    """Every episode from both sides is filed exactly once.

    One episode of each kind, plus both halves of a matched pair, plus a
    censored one from each side. The assertion is a multiset equality: an
    episode dropped, duplicated, or filed twice fails it, and no bucket-by-bucket
    count could.
    """

    paired_ours = an_episode("ours-paired", parts=("github", "o/r", "1"))
    paired_v1 = an_episode("v1-paired", parts=("github", "o/r", "1"))
    ours_only = an_episode("ours-only", parts=("github", "o/r", "2"))
    v1_only_episode = an_episode("v1-only", parts=("github", "o/r", "3"))
    keyless_ours = an_episode("ours-keyless", parts=None, key_gap="no pr_number")
    keyless_v1 = an_episode("v1-keyless", parts=None, key_gap="no pr_number")
    censored_ours = an_episode("ours-censored", parts=("github", "o/r", "4"))
    censored_v1 = an_episode("v1-censored", parts=("github", "o/r", "5"))

    ours = [paired_ours, ours_only, keyless_ours, censored_ours]
    theirs = [paired_v1, v1_only_episode, keyless_v1, censored_v1]

    report = reconciled(
        ours,
        theirs,
        censored_ids={"ours-censored", "v1-censored"},
    )

    assert report.counts() == {
        BOTH: 1,
        INTERLOCK_ONLY: 1,
        V1_ONLY: 1,
        UNMATCHED_KEY: 2,
        CENSORED: 2,
    }
    filed = report.filed_episode_ids()
    assert len(filed) == len(set(filed)), "no episode may be filed twice"
    assert sorted(filed) == sorted(
        episode.episode_id for episode in ours + theirs
    ), "no episode may go missing"


def test_a_pair_is_censored_when_either_half_is_and_no_miss_is_fabricated():
    """Matching happens before censoring, and this is the case that proves it.

    The Interlock half is censored; its v1 counterpart is not. Censoring first
    would drop our half, leave v1's unmatched, and report a *miss* -- fabricated
    out of a report boundary, which is exactly what section 3.5 exists to stop.
    """

    ours = an_episode("ours-1", parts=("github", "o/r", "1"))
    theirs = an_episode("v1-1", parts=("github", "o/r", "1"))

    report = reconciled([ours], [theirs], censored_ids={"ours-1"})

    assert report.counts() == {
        BOTH: 0,
        INTERLOCK_ONLY: 0,
        V1_ONLY: 0,
        UNMATCHED_KEY: 0,
        CENSORED: 2,
    }
    assert sorted(episode.episode_id for episode in report.censored) == [
        "ours-1",
        "v1-1",
    ]


def test_censored_ids_come_from_the_windows_module_and_not_from_here(db: Path):
    """The one adaptor, exercised end to end.

    The window's own boundary decides: an episode whose ``[onset, onset+L+grace)``
    ends one millisecond past the period is censored, and the reconciliation
    files the v1 counterpart as censored too rather than as a miss. Nothing in
    this test names a censored id by hand.
    """

    revision_id = seed_revision_id(db)
    connection = open_for_measurement(db)
    try:
        budget_ms = int(
            policy.detection_latency(
                connection, revision_id=revision_id, incident_class=ABSOLUTE_CLASS
            )["budget_ms"]
        )
        grace_ms = 0
        # One millisecond too late to be judged inside this period.
        late_onset = PERIOD_END - budget_ms - grace_ms + 1
        window_report = classify_episodes(
            connection,
            revision_id=revision_id,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            grace_ms=grace_ms,
            episodes=[
                Episode(
                    episode_id="ours-late",
                    incident_class=ABSOLUTE_CLASS,
                    onset_ms=late_onset,
                )
            ],
        )
    finally:
        connection.close()

    assert censored_episode_ids(window_report) == {"ours-late"}

    report = reconciled(
        [an_episode("ours-late", parts=("github", "o/r", "1"), onset_ms=late_onset)],
        [an_episode("v1-late", parts=("github", "o/r", "1"), onset_ms=late_onset)],
        censored_ids=censored_episode_ids(window_report),
    )
    assert report.counts()[CENSORED] == 2
    assert report.counts()[V1_ONLY] == 0


def test_a_keyless_censored_episode_is_censored_and_does_not_inflate_the_key_bucket():
    """Section 7 reads ``unmatched_key`` as a verdict on the KEY, not on the period."""

    report = reconciled(
        [an_episode("ours-1", parts=None, key_gap="no run_id")],
        [an_episode("v1-1")],
        censored_ids={"ours-1"},
    )
    assert report.counts()[UNMATCHED_KEY] == 0
    assert report.counts()[CENSORED] == 1


# --------------------------------------------------------------------------
# the positional key, said out loud
# --------------------------------------------------------------------------


def test_the_positional_caveat_rides_on_the_key_and_on_the_report():
    """Sections 3.3 and 7: the weakest join must not be invisible at read time."""

    positional = CorrelationKey(
        subject_class=SUBJECT_WORKER_ESCALATION, parts=("run-1", "2")
    )
    assert positional.positional is True
    assert (
        CorrelationKey(subject_class=SUBJECT_PR_MERGE, parts=("github", "o/r", "1")).positional
        is False
    )

    episode = ShadowEpisode(
        episode_id="ours-1",
        subject_class=SUBJECT_WORKER_ESCALATION,
        shape="received",
        onset_ms=T0,
        key_gap="gate.run_id is NULL",
    )
    report = reconciled([episode], [an_episode("v1-1")])
    assert report.positional_caveat == POSITIONAL_KEY_CAVEAT
    assert "positional" in render_shadow_reconciliation(report)


def test_two_escalations_at_one_ordinal_are_refused_with_the_caveat_attached():
    """The positional key colliding is the key failing; it is named, not absorbed."""

    first = ShadowEpisode(
        episode_id="ours-1",
        subject_class=SUBJECT_WORKER_ESCALATION,
        shape="received",
        onset_ms=T0,
        key=CorrelationKey(subject_class=SUBJECT_WORKER_ESCALATION, parts=("run-1", "2")),
    )
    second = ShadowEpisode(
        episode_id="ours-2",
        subject_class=SUBJECT_WORKER_ESCALATION,
        shape="received",
        onset_ms=T0 + 1,
        key=CorrelationKey(subject_class=SUBJECT_WORKER_ESCALATION, parts=("run-1", "2")),
    )
    with pytest.raises(DuplicateCorrelationKey) as raised:
        reconciled([first, second], [an_episode("v1-1")])
    assert "same order" in str(raised.value)


def test_a_matched_pair_that_disagrees_on_shape_is_a_finding_and_not_a_miss():
    ours = an_episode("ours-1", shape="failed", parts=("github", "o/r", "1"))
    theirs = an_episode("v1-1", shape="passed", parts=("github", "o/r", "1"))
    report = reconciled([ours], [theirs])
    assert report.counts()[BOTH] == 1
    assert report.counts()[V1_ONLY] == 0
    assert report.both[0].shape_agrees is False
    assert report.both[0].onset_delta_ms == 0


# --------------------------------------------------------------------------
# refusals that keep an episode from vanishing
# --------------------------------------------------------------------------


def test_an_episode_with_neither_key_nor_gap_or_with_both_is_refused():
    with pytest.raises(EpisodeKeyRefused):
        ShadowEpisode(
            episode_id="e", subject_class=SUBJECT_PR_MERGE, shape="merged", onset_ms=T0
        )
    with pytest.raises(EpisodeKeyRefused):
        ShadowEpisode(
            episode_id="e",
            subject_class=SUBJECT_PR_MERGE,
            shape="merged",
            onset_ms=T0,
            key=CorrelationKey(subject_class=SUBJECT_PR_MERGE, parts=("github", "o/r", "1")),
            key_gap="also this",
        )


def test_an_empty_key_component_is_a_missing_component_and_is_refused():
    with pytest.raises(EpisodeKeyRefused):
        CorrelationKey(subject_class=SUBJECT_PR_MERGE, parts=("github", "", "1"))


def test_a_key_of_another_subject_class_is_refused():
    with pytest.raises(EpisodeKeyRefused):
        ShadowEpisode(
            episode_id="e",
            subject_class=SUBJECT_PR_MERGE,
            shape="merged",
            onset_ms=T0,
            key=CorrelationKey(
                subject_class=SUBJECT_CI_OUTCOME,
                parts=("github", "o/r", "1", SHA_A),
            ),
        )


def test_a_subject_class_outside_the_table_is_refused():
    with pytest.raises(UnknownSubjectClass):
        CorrelationKey(subject_class="something_new", parts=("a",))


def test_one_episode_id_on_both_sides_is_refused():
    with pytest.raises(DuplicateEpisodeIdRefused):
        reconciled([an_episode("shared")], [an_episode("shared")])


def test_an_empty_or_inverted_report_period_is_refused():
    with pytest.raises(ShadowRefusal):
        reconcile(
            period_start_ms=PERIOD_END,
            period_end_ms=PERIOD_START,
            interlock_episodes=[],
            v1_reference=V1Reference.attests_empty(source="v1"),
            censored_ids=(),
            fixture_labels={},
        )


# --------------------------------------------------------------------------
# the report's own shape
# --------------------------------------------------------------------------


def test_the_buckets_are_section_3_3_s_five_and_counts_emits_all_of_them_at_zero():
    assert RECONCILIATION_BUCKETS == (
        BOTH,
        INTERLOCK_ONLY,
        V1_ONLY,
        UNMATCHED_KEY,
        CENSORED,
    )
    report = reconciled([], V1Reference.attests_empty(source="v1"))
    assert dict(report.counts()) == {name: 0 for name in RECONCILIATION_BUCKETS}
    assert report.filed_episode_ids() == ()


def test_the_rendered_report_is_ascii_and_survives_a_cp932_console():
    """CLI output must not crash a Windows terminal on an em-dash."""

    report = reconciled(
        [an_episode("ours-1", parts=("github", "o/r", "1"))],
        [an_episode("v1-1", shape="relay_gap", parts=("github", "o/r", "2"))],
    )
    rendered = render_shadow_reconciliation(report)
    rendered.encode("ascii")
    rendered.encode("cp932")
    assert POSITIONAL_KEY_CAVEAT.isascii()
    assert list(report.counts()) == list(RECONCILIATION_BUCKETS)
    assert list(ADJUDICATIONS) == [MISS, V1_FALSE_POSITIVE, UNDETERMINED]
