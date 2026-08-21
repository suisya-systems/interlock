"""What ``watcher.py`` must keep true -- section 8's four distinctions, as tests.

``docs/production-schema.md`` section 8.1 names four things a single
``last_heartbeat_at`` column cannot say, each with a v1 incident behind it. Every
test below pins one of them, or pins the fence that keeps the answer honest:

* **"polled, nothing changed" versus "poll failed"** -- the alternation tests.
  ``last_success_at_ms`` and ``last_error_at_ms`` are *history* and the table's
  constraints on them are implications rather than biconditionals; tying either
  both ways would abort the first success-after-error and the first
  error-after-success, which is every recovery and every failure.
* **A replaced watcher's late heartbeat** -- the refusal tests. Zero rows has
  exactly two causes, they are disambiguated by a read rather than assumed, and
  the refusal is durable in every case, including the one the DDL trigger turns
  into an exception instead of a zero.
* **A missing watcher** -- :func:`test_a_registered_scope_that_never_heartbeats_is_uncovered`
  and its neighbours. This is ``relay_scan.py``'s twenty-day silence: the trace
  alone answers "fine" to every question, and only the roster can name the
  absence.
* **Partial coverage** -- the same query, with one scope of two covered.

Plus the property that makes the whole thing non-negotiable: a watcher holding
scope B's lease **cannot** heartbeat scope A. The lease resource is derived
inside the statement, so the misroute is unrepresentable rather than merely
discouraged, and the test asserts both halves -- the write is refused *and* A
stays uncovered, because a heartbeat that landed would silence the very
predicate the fence protects.

Both policy reads bind the effective revision (``D-0031``: a ``policy_*`` join
without a ``revision_id`` predicate is a defect), so each is tested with **two
revisions on record** -- a query that forgot the predicate still returns rows,
so only a second revision can tell the two apart.

Every timestamp comes from :data:`T0` and arithmetic on it. No test here reads a
clock; the module does not either, and a suite whose expectations moved with the
wall clock could not assert a tolerance boundary at all.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import watcher
from claude_org_runtime.control_plane.lease import acquire
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.control_plane.watcher import (
    HeartbeatRefused,
    ScopeNotRegistered,
    WatcherUsageError,
    error_streak_scopes,
    heartbeat,
    register_scope,
    retire_scope,
    scope_lease_resource,
    silent_scopes,
    uncovered_scopes,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

#: One scope's own expected interval. The seeded ``watcher_silence`` threshold
#: is the multiple 3, so silence begins strictly after 180_000 ms for this scope
#: and at some other figure for any scope registered with another interval --
#: which is the reason the policy row stores a multiple.
INTERVAL_MS = 60_000

SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)

LONG_TTL_MS = 3_600_000  # long enough that no test's arithmetic expires a lease


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_production_control_plane(tmp_path / "production.sqlite3", now_ms=T0)
    connection.execute(
        """
        INSERT INTO repository (repo_id, provider, owner, name, created_at_ms, updated_at_ms)
        VALUES ('repo-1', 'github', 'acme', 'widget', ?, ?)
        """,
        (T0, T0),
    )
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal setup of each kind
# --------------------------------------------------------------------------


def add_scope(cp, scope_id: str = "scope-1", *, interval_ms: int = INTERVAL_MS,
              at: int = T0) -> str:
    register_scope(
        cp,
        scope_id=scope_id,
        scope_kind="ci_repository",
        expected_interval_ms=interval_ms,
        registered_at_ms=at,
        repo_id="repo-1",
    )
    return scope_id


def hold(cp, scope_id: str, *, holder: str = "watcher-1", at: int = T0,
         ttl_ms: int = LONG_TTL_MS) -> int:
    """Take *scope_id*'s lease for *holder* and return the epoch it was given."""

    return acquire(
        cp, resource=scope_lease_resource(scope_id), holder=holder, now_ms=at, ttl_ms=ttl_ms
    ).epoch


def liveness(cp, scope_id: str) -> dict | None:
    cursor = cp.execute("SELECT * FROM watcher_liveness WHERE scope_id = ?", (scope_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([column[0] for column in cursor.description], row))


def refusals(cp) -> list[dict]:
    cursor = cp.execute(
        "SELECT * FROM action WHERE status = 'refused' ORDER BY rowid"
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def add_revision(cp, *, note: str, effective_at_ms: int, **thresholds: tuple) -> int:
    """A second policy revision, so a policy read that forgot to bind one fails.

    Each keyword is an incident class mapped to
    ``(threshold_kind, threshold_value)``; everything else on the row is carried
    from the seed, because what these tests vary is the tolerance and not the
    budget.
    """

    revision_id = cp.execute(
        "INSERT INTO policy_revision (note, decided_by, effective_at_ms) VALUES (?, ?, ?)",
        (note, "test", effective_at_ms),
    ).lastrowid
    for incident_class, (threshold_kind, threshold_value) in thresholds.items():
        cp.execute(
            """
            INSERT INTO policy_detection_latency
                (revision_id, incident_class, threshold_kind, threshold_value,
                 reconcile_period_ms, budget_ms, budget_kind)
            VALUES (?, ?, ?, ?, 120000, 600000, 'absolute_ms')
            """,
            (revision_id, incident_class, threshold_kind, threshold_value),
        )
    return int(revision_id)


# --------------------------------------------------------------------------
# the roster
# --------------------------------------------------------------------------


def test_the_scope_lease_resource_is_a_function_of_the_scope_id():
    assert scope_lease_resource("scope-1") == "watcher_scope:scope-1"


def test_heartbeat_takes_no_resource_argument_so_a_misroute_cannot_be_expressed():
    # Section 8.3 is explicit that a separate resource parameter is the defect,
    # not merely a smell: it lets scope B's holder mark scope A healthy. The
    # behavioural proof is below; this asserts the shape, so that a later
    # "convenience" override has to delete a test that says why it must not.
    assert "resource" not in inspect.signature(heartbeat).parameters


def test_a_pull_request_scope_must_name_its_pull_request(cp):
    with pytest.raises(WatcherUsageError):
        register_scope(cp, scope_id="s", scope_kind="ci_pull_request",
                       expected_interval_ms=INTERVAL_MS, registered_at_ms=T0,
                       repo_id="repo-1")


def test_a_repository_scope_may_not_name_a_pull_request(cp):
    with pytest.raises(WatcherUsageError):
        register_scope(cp, scope_id="s", scope_kind="ci_repository",
                       expected_interval_ms=INTERVAL_MS, registered_at_ms=T0,
                       repo_id="repo-1", pr_id="pr-1")


def test_retiring_a_scope_that_is_not_on_the_roster_is_refused(cp):
    with pytest.raises(ScopeNotRegistered):
        retire_scope(cp, scope_id="never-registered", retired_at_ms=T0)


def test_retiring_a_scope_keeps_its_last_trace(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_change", now_ms=T0)

    retire_scope(cp, scope_id="scope-1", retired_at_ms=T0 + 1)

    assert liveness(cp, "scope-1")["last_change_at_ms"] == T0
    assert uncovered_scopes(cp) == ()
    assert silent_scopes(cp, now_ms=T0 + 10 * INTERVAL_MS) == ()


# --------------------------------------------------------------------------
# the trace, and the fence inside it
# --------------------------------------------------------------------------


def test_the_first_heartbeat_of_a_scope_inserts_its_row_through_the_same_fence(cp):
    # The bootstrap arm. A bare UPDATE would change zero rows here, and zero
    # rows is how a stale writer is refused -- so this case has to be a write
    # and not a refusal, while still requiring the lease.
    add_scope(cp)
    epoch = hold(cp, "scope-1")

    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0 + 10)

    row = liveness(cp, "scope-1")
    assert row["attempt_count"] == 1
    assert row["last_result"] == "observed_no_change"
    assert row["last_attempt_at_ms"] == T0 + 10
    assert row["last_success_at_ms"] == T0 + 10
    # Nothing changed, so nothing was seen to change. The distinction the
    # single-column form loses.
    assert row["last_change_at_ms"] is None
    assert row["consecutive_errors"] == 0


def test_a_heartbeat_for_an_unregistered_scope_is_not_a_stale_writer(cp):
    with pytest.raises(ScopeNotRegistered):
        heartbeat(cp, scope_id="ghost", holder="watcher-1", epoch=1,
                  result="observed_no_change", now_ms=T0)
    assert refusals(cp) == []


def test_a_heartbeat_without_the_scope_lease_is_refused_and_recorded(cp):
    add_scope(cp)

    with pytest.raises(HeartbeatRefused) as refused:
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=1,
                  result="observed_change", now_ms=T0)

    assert refused.value.cause == "lease_not_held"
    assert liveness(cp, "scope-1") is None
    recorded = refusals(cp)
    assert len(recorded) == 1
    assert recorded[0]["action_id"] == refused.value.action_id
    assert recorded[0]["kind"] == "watcher_heartbeat@watcher_scope:scope-1"
    assert recorded[0]["writer_epoch"] == 1
    assert "watcher_scope:scope-1" in recorded[0]["refusal_reason"]


def test_a_watcher_holding_another_scopes_lease_cannot_heartbeat_this_one(cp):
    # Section 8.3's whole argument for deriving the resource, as a test. Scope A
    # has no watcher; the holder of scope B's lease tries to heartbeat A. If the
    # write landed, A would look healthy, watcher_silence would never fire for
    # it, and the uncovered query -- the only thing that can see a missing
    # watcher at all -- would go quiet too.
    add_scope(cp, "scope-a")
    add_scope(cp, "scope-b")
    epoch_b = hold(cp, "scope-b", holder="watcher-b")
    heartbeat(cp, scope_id="scope-b", holder="watcher-b", epoch=epoch_b,
              result="observed_no_change", now_ms=T0)

    with pytest.raises(HeartbeatRefused) as refused:
        heartbeat(cp, scope_id="scope-a", holder="watcher-b", epoch=epoch_b,
                  result="observed_change", now_ms=T0)

    assert refused.value.cause == "lease_not_held"
    assert liveness(cp, "scope-a") is None
    assert [row["scope_id"] for row in uncovered_scopes(cp)] == ["scope-a"]


def test_a_replaced_watcher_returning_with_its_old_epoch_is_refused(cp):
    add_scope(cp)
    old_epoch = hold(cp, "scope-1", holder="watcher-1", ttl_ms=1_000)
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=old_epoch,
              result="observed_change", now_ms=T0)
    # The lease lapses and is taken over, which raises the epoch. That is what
    # invalidates the old token -- not the clock.
    new_epoch = hold(cp, "scope-1", holder="watcher-2", at=T0 + 2_000)
    heartbeat(cp, scope_id="scope-1", holder="watcher-2", epoch=new_epoch,
              result="observed_no_change", now_ms=T0 + 2_000)

    with pytest.raises(HeartbeatRefused) as refused:
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=old_epoch,
                  result="observed_change", now_ms=T0 + 3_000)

    assert refused.value.cause == "lease_not_held"
    assert refused.value.observed.holder == "watcher-2"
    row = liveness(cp, "scope-1")
    assert (row["holder"], row["holder_epoch"]) == ("watcher-2", new_epoch)
    assert row["attempt_count"] == 2  # the refusal did not count as an attempt


def test_a_liveness_row_at_a_higher_epoch_refuses_a_live_lease_holder(cp):
    # The second of the two zero-row causes, and the reason the refusal reads
    # the cause instead of assuming one: here the fence HOLDS, so "the lease is
    # not ours" would be a lie. The row is seeded directly because
    # lease.acquire's own monotonicity means no sequence of this module's calls
    # can produce a liveness epoch above the live lease's -- the branch is
    # defence in depth against a writer that does not come through here.
    add_scope(cp)
    epoch = hold(cp, "scope-1", holder="watcher-1")
    cp.execute(
        """
        INSERT INTO watcher_liveness (scope_id, holder, holder_epoch, last_attempt_at_ms,
                                      last_result, last_success_at_ms, attempt_count)
        VALUES ('scope-1', 'watcher-1', ?, ?, 'observed_change', ?, 1)
        """,
        (epoch + 5, T0, T0),
    )

    with pytest.raises(HeartbeatRefused) as refused:
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
                  result="observed_change", now_ms=T0 + 1)

    assert refused.value.cause == "epoch_superseded"
    assert liveness(cp, "scope-1")["holder_epoch"] == epoch + 5
    assert len(refusals(cp)) == 1


def test_a_different_holder_at_an_equal_epoch_is_refused_by_the_trigger_and_recorded(cp):
    # This one passes `holder_epoch <= :epoch` and is stopped by
    # watcher_liveness_epoch_is_monotonic, so it arrives as an integrity error
    # rather than as zero rows. It is the same stale writer and it gets the same
    # durable refusal -- ACCEPTANCE.md section 2 does not care which mechanism
    # rejected it. Both rows are seeded directly because acquire() raises the
    # epoch on every handover, so an equal-epoch handover cannot be produced
    # through the supported path.
    add_scope(cp)
    cp.execute(
        "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)"
        " VALUES ('watcher_scope:scope-1', 'watcher-new', 7, ?, ?)",
        (T0, T0 + LONG_TTL_MS),
    )
    cp.execute(
        """
        INSERT INTO watcher_liveness (scope_id, holder, holder_epoch, last_attempt_at_ms,
                                      last_result, last_success_at_ms, attempt_count)
        VALUES ('scope-1', 'watcher-old', 7, ?, 'observed_change', ?, 1)
        """,
        (T0, T0),
    )

    with pytest.raises(HeartbeatRefused) as refused:
        heartbeat(cp, scope_id="scope-1", holder="watcher-new", epoch=7,
                  result="observed_change", now_ms=T0 + 1)

    assert refused.value.cause == "epoch_not_raised_by_new_holder"
    assert liveness(cp, "scope-1")["holder"] == "watcher-old"
    assert len(refusals(cp)) == 1


def test_every_refusal_of_a_returning_writer_is_recorded_again(cp):
    # action_one_effect_per_key excludes refused rows on purpose: a writer that
    # keeps coming back is recorded every time, and none of those records is
    # what admits a second effect.
    add_scope(cp)
    for attempt in range(3):
        with pytest.raises(HeartbeatRefused):
            heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=1,
                      result="observed_change", now_ms=T0 + attempt)
    assert len(refusals(cp)) == 3


def test_a_result_and_its_error_message_must_agree(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    with pytest.raises(WatcherUsageError):
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
                  result="error", now_ms=T0)
    with pytest.raises(WatcherUsageError):
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
                  result="observed_change", now_ms=T0, error="but nothing failed")
    assert liveness(cp, "scope-1") is None


# --------------------------------------------------------------------------
# the alternation the implications exist for
# --------------------------------------------------------------------------


def test_success_then_error_then_success_keeps_both_histories(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")

    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_change", now_ms=T0)
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="error", now_ms=T0 + 1_000, error="HTTP 502 from the provider")
    after_failure = liveness(cp, "scope-1")
    # The first error-after-success. A biconditional on last_success_at_ms would
    # have aborted this write, and with it every failure the table exists to
    # record.
    assert after_failure["last_success_at_ms"] == T0
    assert after_failure["last_error_at_ms"] == T0 + 1_000
    assert after_failure["last_error"] == "HTTP 502 from the provider"
    assert after_failure["consecutive_errors"] == 1

    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0 + 2_000)
    after_recovery = liveness(cp, "scope-1")
    # The first success-after-error, which the mirror-image biconditional would
    # have aborted. The failure's timestamp survives its own recovery.
    assert after_recovery["last_error_at_ms"] == T0 + 1_000
    assert after_recovery["last_error"] is None
    assert after_recovery["last_success_at_ms"] == T0 + 2_000
    assert after_recovery["last_change_at_ms"] == T0  # still the only change seen
    assert after_recovery["consecutive_errors"] == 0
    assert after_recovery["attempt_count"] == 3


def test_consecutive_errors_counts_up_and_resets_only_on_a_success(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    for attempt in range(4):
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
                  result="error", now_ms=T0 + attempt, error="bad credential")
    assert liveness(cp, "scope-1")["consecutive_errors"] == 4

    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0 + 10)
    assert liveness(cp, "scope-1")["consecutive_errors"] == 0


# --------------------------------------------------------------------------
# the three incident conditions, kept distinct
# --------------------------------------------------------------------------


def test_a_registered_scope_that_never_heartbeats_is_uncovered(cp):
    add_scope(cp, "scope-a")
    assert [row["scope_id"] for row in uncovered_scopes(cp)] == ["scope-a"]
    # ...and silence cannot see it. A scope with no row has no last_attempt to
    # be late against, which is exactly why the roster exists.
    assert silent_scopes(cp, now_ms=T0 + 10 * INTERVAL_MS) == ()


def test_partial_coverage_names_only_the_scope_nobody_is_watching(cp):
    add_scope(cp, "scope-a")
    add_scope(cp, "scope-b")
    epoch = hold(cp, "scope-b", holder="watcher-b")
    heartbeat(cp, scope_id="scope-b", holder="watcher-b", epoch=epoch,
              result="observed_no_change", now_ms=T0)

    assert [row["scope_id"] for row in uncovered_scopes(cp)] == ["scope-a"]


def test_silence_begins_strictly_after_the_scopes_own_interval_multiple(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0)

    on_the_bound = T0 + 3 * INTERVAL_MS
    assert silent_scopes(cp, now_ms=on_the_bound) == ()
    just_past = silent_scopes(cp, now_ms=on_the_bound + 1)
    assert [row["scope_id"] for row in just_past] == ["scope-1"]
    assert just_past[0]["silent_for_ms"] == 3 * INTERVAL_MS + 1


def test_silence_is_measured_against_each_scopes_own_interval(cp):
    # The reason the threshold is stored as a multiple: one millisecond figure
    # would mis-age whichever scope was not the one it was derived from.
    add_scope(cp, "brisk", interval_ms=10_000)
    add_scope(cp, "leisurely", interval_ms=600_000)
    for scope_id in ("brisk", "leisurely"):
        epoch = hold(cp, scope_id, holder=f"watcher-{scope_id}")
        heartbeat(cp, scope_id=scope_id, holder=f"watcher-{scope_id}", epoch=epoch,
                  result="observed_no_change", now_ms=T0)

    named = [row["scope_id"] for row in silent_scopes(cp, now_ms=T0 + 60_000)]
    assert named == ["brisk"]


def test_an_erroring_watcher_that_is_punctual_is_a_streak_and_not_silent(cp):
    # Two conditions, two remedies: a dead process versus a broken credential.
    # Collapsing them produces one alarm that names neither.
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    for attempt in range(5):
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
                  result="error", now_ms=T0 + attempt, error="HTTP 401")

    now = T0 + 5
    assert [row["scope_id"] for row in error_streak_scopes(cp, now_ms=now)] == ["scope-1"]
    assert silent_scopes(cp, now_ms=now) == ()
    assert uncovered_scopes(cp) == ()


def test_a_silent_watcher_that_last_succeeded_is_not_an_error_streak(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_change", now_ms=T0)

    now = T0 + 10 * INTERVAL_MS
    assert [row["scope_id"] for row in silent_scopes(cp, now_ms=now)] == ["scope-1"]
    assert error_streak_scopes(cp, now_ms=now) == ()


def test_the_streak_opens_on_the_threshold_th_failure_and_not_the_one_after(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    for attempt in range(4):
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
                  result="error", now_ms=T0 + attempt, error="HTTP 401")
    assert error_streak_scopes(cp, now_ms=T0 + 4) == ()

    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="error", now_ms=T0 + 4, error="HTTP 401")
    assert [row["scope_id"] for row in error_streak_scopes(cp, now_ms=T0 + 5)] == ["scope-1"]


# --------------------------------------------------------------------------
# both policy reads bind the effective revision (D-0031)
# --------------------------------------------------------------------------


def test_silence_binds_the_effective_policy_revision(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0)
    now = T0 + 5 * INTERVAL_MS
    assert [row["scope_id"] for row in silent_scopes(cp, now_ms=now)] == ["scope-1"]

    # A later revision relaxes the multiple from 3 to 10. An unbound join would
    # still match the seed row and keep alarming -- which is D-0031's corollary
    # exactly: the defect returns rows, so only a second revision exposes it.
    add_revision(cp, note="relaxed watcher silence", effective_at_ms=T0,
                 watcher_silence=("scope_interval_multiple", 10))

    assert silent_scopes(cp, now_ms=now) == ()
    assert [row["scope_id"] for row in silent_scopes(cp, now_ms=T0 + 11 * INTERVAL_MS)] == [
        "scope-1"
    ]


def test_silence_reads_the_revision_that_was_effective_at_the_callers_instant(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0)
    add_revision(cp, note="relaxed watcher silence", effective_at_ms=T0 + 10 * INTERVAL_MS,
                 watcher_silence=("scope_interval_multiple", 10))

    # Before the new revision takes effect the old multiple still governs.
    assert [row["scope_id"] for row in silent_scopes(cp, now_ms=T0 + 4 * INTERVAL_MS)] == [
        "scope-1"
    ]
    # After it, the same scope is inside the relaxed tolerance again.
    assert silent_scopes(cp, now_ms=T0 + 10 * INTERVAL_MS) == ()


def test_the_error_streak_binds_the_effective_policy_revision(cp):
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    for attempt in range(2):
        heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
                  result="error", now_ms=T0 + attempt, error="HTTP 401")
    assert error_streak_scopes(cp, now_ms=T0 + 2) == ()

    add_revision(cp, note="tightened watcher error streak", effective_at_ms=T0,
                 watcher_error_streak=("consecutive_count", 2))

    assert [row["scope_id"] for row in error_streak_scopes(cp, now_ms=T0 + 2)] == ["scope-1"]


def test_two_revisions_at_one_instant_resolve_by_the_later_revision_id(cp):
    # Both tiebreak columns matter: without the revision_id half, a correction
    # filed in the same millisecond would resolve by SQLite's row order.
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0)
    add_revision(cp, note="first at this instant", effective_at_ms=T0 + 1,
                 watcher_silence=("scope_interval_multiple", 1))
    add_revision(cp, note="correction at the same instant", effective_at_ms=T0 + 1,
                 watcher_silence=("scope_interval_multiple", 100))

    assert silent_scopes(cp, now_ms=T0 + 5 * INTERVAL_MS) == ()


def test_a_retired_revision_is_not_joined_alongside_the_live_one(cp):
    # The shape of the defect D-0031 names: an unbound join returns one row per
    # revision, so the same scope would be reported twice.
    add_scope(cp)
    epoch = hold(cp, "scope-1")
    heartbeat(cp, scope_id="scope-1", holder="watcher-1", epoch=epoch,
              result="observed_no_change", now_ms=T0)
    add_revision(cp, note="a second revision with the same tolerance", effective_at_ms=T0,
                 watcher_silence=("scope_interval_multiple", 3))

    assert len(silent_scopes(cp, now_ms=T0 + 5 * INTERVAL_MS)) == 1


def test_the_watcher_module_never_reads_a_clock(cp):
    source = Path(watcher.__file__).read_text(encoding="utf-8")
    assert "time.time" not in source
    assert "import time" not in source
