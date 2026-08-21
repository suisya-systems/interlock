"""The production control-plane DDL -- section 11's verification table, made executable.

``docs/production-schema.md`` section 11 is a list of claims about constraints
that were exercised by hand against an in-memory database while the design was
being written. A hand-run check is evidence that a claim *was* true once; it is
not a thing that fails when someone widens a ``CHECK`` a year from now. This
module turns each row of that table into one test named after the claim, so the
document and the schema cannot drift apart silently: a change to the DDL that
contradicts the design has to break a test whose name says which sentence of the
design it broke.

Three tests here are not from section 11, and each is an obligation the design
hands to the implementation in so many words:

* **Commit order** (section 5.2, ``D-0030``). ``event.seq`` is only usable as a
  consumer cursor if a committed gap can never be back-filled. Section 11 says
  outright that it does not establish this, because it needs two connections and
  interleaved transactions. :func:`test_no_committed_event_seq_is_ever_observed_out_of_commit_order`
  is that test, and it is the thing that fails if this database is ever put
  behind something admitting concurrent writers.
* **All-or-nothing append** (section 5.4). The append is one transaction over
  the event, the per-consumer consumption rows, the delivery outbox rows and any
  typed side table. The property that matters on the failure path is that a
  fan-out which dies part way leaves *no* event row behind -- an event with no
  delivery record is precisely v1's push-vs-poll duplication returning.
* **The two adjudicated design gaps.** ``run.status``'s closed set and
  forward-only rule, and ``policy_detection_latency.budget_kind``, were settled
  during implementation because section 2 and ``time-base-policy.md`` section
  3.2 respectively left them underspecified. Each gets a test that pins the
  decision, so the next reader finds the adjudication asserted rather than
  inferred from the DDL.

One section 11 row is **stale** and is deliberately not reproduced as written --
see :func:`test_gate_stage_may_only_name_an_open_or_advance_transition_of_its_own_gate`.

Every timestamp below is an integer of milliseconds since the Unix epoch, and
every one of them comes from :data:`T0` and arithmetic on it rather than from a
clock. That is the schema's own convention (no timestamp column has a
``DEFAULT``) applied to its tests: a suite whose expectations move with the wall
clock cannot assert a tolerance boundary.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.migrator import (
    create_production_control_plane,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

SHA_A = "a" * 40
SHA_B = "b" * 40

#: The note of the revision seeded by ``0002_policy_seed.sql``. The seed is the
#: numeric table of ``time-base-policy.md`` section 3 as data, so the tests that
#: read policy read *those* numbers rather than restating them.
SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "production.sqlite3"


@pytest.fixture
def cp(db_path: Path):
    connection = create_production_control_plane(db_path, now_ms=T0)
    try:
        yield connection
    finally:
        connection.close()


def second_connection(db_path: Path, *, busy_timeout_ms: int = 0) -> sqlite3.Connection:
    """A second writer against the same file, configured like the first.

    ``isolation_level=None`` because these tests drive ``BEGIN``/``COMMIT``
    themselves -- the point of the commit-order test is *when* each transaction
    commits, which the driver's implicit transaction management would decide.
    The busy timeout defaults to zero so that a lock conflict surfaces as a
    failure to acquire rather than as a five-second pause.
    """

    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return connection


# --------------------------------------------------------------------------
# helpers -- the smallest legal row of each kind
# --------------------------------------------------------------------------


def add_run(cp, run_id: str = "run-1", status: str = "running", at: int = T0) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
        (run_id, status, at, at),
    )
    return run_id


def add_event(cp, event_id: str = "evt-1", at: int = T0, **kwargs) -> int:
    """Append one event and return the ``seq`` it was assigned."""

    row = {
        "event_type": "ci.check_suite.completed",
        "subject_kind": "run",
        "subject_id": "run-1",
        "run_id": None,
        "payload": "{}",
        "producer": "gh-watcher",
        "producer_epoch": None,
    }
    row.update(kwargs)
    row.setdefault("dedup_key", f"dk/{event_id}")
    cursor = cp.execute(
        """
        INSERT INTO event (event_id, event_type, subject_kind, subject_id, run_id, payload,
                           producer, producer_epoch, dedup_key, occurred_at_ms, ingested_at_ms)
        VALUES (:event_id, :event_type, :subject_kind, :subject_id, :run_id, :payload,
                :producer, :producer_epoch, :dedup_key, :occurred_at_ms, :ingested_at_ms)
        """,
        {"event_id": event_id, "occurred_at_ms": at, "ingested_at_ms": at, **row},
    )
    return int(cursor.lastrowid)


def add_consumer(cp, consumer_id: str = "cons-1", kind: str = "compute", at: int = T0,
                 registered_from_seq: int = 0) -> str:
    cp.execute(
        """
        INSERT INTO consumer (consumer_id, kind, lease_resource, registered_at_ms,
                              registered_from_seq)
        VALUES (?, ?, ?, ?, ?)
        """,
        (consumer_id, kind, f"consumer:{consumer_id}", at, registered_from_seq),
    )
    return consumer_id


def add_subscription(cp, consumer_id: str = "cons-1",
                     event_type: str = "ci.check_suite.completed",
                     recipient: str | None = None, at: int = T0) -> None:
    cp.execute(
        "INSERT INTO consumer_subscription (consumer_id, event_type, recipient, added_at_ms)"
        " VALUES (?, ?, ?, ?)",
        (consumer_id, event_type, recipient, at),
    )


def add_consumption(cp, consumer_id: str, event_seq: int, at: int = T0, **kwargs) -> None:
    row = {
        "status": "pending",
        "attempt_count": 0,
        "message_id": None,
        "last_error": None,
        "writer_epoch": None,
        "settled_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO event_consumption (consumer_id, event_seq, status, attempt_count, message_id,
                                       last_error, writer_epoch, created_at_ms, settled_at_ms)
        VALUES (:consumer_id, :event_seq, :status, :attempt_count, :message_id, :last_error,
                :writer_epoch, :created_at_ms, :settled_at_ms)
        """,
        {"consumer_id": consumer_id, "event_seq": event_seq, "created_at_ms": at, **row},
    )


def add_outbox(cp, message_id: str = "msg-1", dedup_key: str = "dk-1", at: int = T0, **kwargs):
    row = {
        "run_id": None,
        "recipient": "secretary",
        "payload": "{}",
        "status": "pending",
        "retry_count": 0,
        "writer_epoch": None,
        "delivered_at_ms": None,
        "acked_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO outbox (message_id, run_id, recipient, payload, dedup_key, status,
                            retry_count, writer_epoch, enqueued_at_ms, delivered_at_ms,
                            acked_at_ms)
        VALUES (:message_id, :run_id, :recipient, :payload, :dedup_key, :status, :retry_count,
                :writer_epoch, :enqueued_at_ms, :delivered_at_ms, :acked_at_ms)
        """,
        {"message_id": message_id, "dedup_key": dedup_key, "enqueued_at_ms": at, **row},
    )
    return message_id


def add_repository(cp, repo_id: str = "repo-1", at: int = T0, **kwargs) -> str:
    row = {"provider": "github", "provider_repo_id": None, "owner": "acme", "name": "widget"}
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO repository (repo_id, provider, provider_repo_id, owner, name,
                                created_at_ms, updated_at_ms)
        VALUES (:repo_id, :provider, :provider_repo_id, :owner, :name, :at, :at)
        """,
        {"repo_id": repo_id, "at": at, **row},
    )
    return repo_id


def add_pull_request(cp, pr_id: str = "pr-1", pr_number: int = 7, head_sha: str = SHA_A,
                     head_event_seq: int | None = None, at: int = T0, **kwargs) -> str:
    """A PR row, with an event appended for its head observation if none is given.

    ``head_event_seq`` is `NOT NULL`: a head is never recorded except as the
    projection of an observation on the spine, so the helper cannot build a
    legal row without one.
    """

    if head_event_seq is None:
        head_event_seq = add_event(cp, f"evt-head-{pr_id}-{head_sha[:4]}", at=at,
                                   event_type="pr.head.observed", subject_kind="pull_request",
                                   subject_id=pr_id)
    row = {
        "repo_id": "repo-1",
        "provider_pr_id": None,
        "state": "open",
        "merge_commit_sha": None,
        "merged_at_ms": None,
        "closed_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO pull_request (pr_id, repo_id, pr_number, provider_pr_id, head_sha,
                                  head_observed_at_ms, head_event_seq, state, merge_commit_sha,
                                  merged_at_ms, closed_at_ms, created_at_ms, updated_at_ms)
        VALUES (:pr_id, :repo_id, :pr_number, :provider_pr_id, :head_sha, :at, :head_event_seq,
                :state, :merge_commit_sha, :merged_at_ms, :closed_at_ms, :at, :at)
        """,
        {"pr_id": pr_id, "pr_number": pr_number, "head_sha": head_sha,
         "head_event_seq": head_event_seq, "at": at, **row},
    )
    return pr_id


def add_run_pr_link(cp, run_id: str = "run-1", pr_id: str = "pr-1", role: str = "primary",
                    resolution: str = "project_registry", at: int = T0, **kwargs) -> None:
    row = {"unlinked_at_ms": None, "unlink_reason": None}
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO run_pr_link (run_id, pr_id, role, resolution, linked_at_ms,
                                 unlinked_at_ms, unlink_reason)
        VALUES (:run_id, :pr_id, :role, :resolution, :linked_at_ms, :unlinked_at_ms,
                :unlink_reason)
        """,
        {"run_id": run_id, "pr_id": pr_id, "role": role, "resolution": resolution,
         "linked_at_ms": at, **row},
    )


def add_ci_observation(cp, observation_id: str = "obs-1", event_seq: int | None = None,
                       at: int = T0, **kwargs) -> str:
    if event_seq is None:
        event_seq = add_event(cp, f"evt-{observation_id}", at=at,
                              subject_kind="pull_request", subject_id="pr-1")
    row = {
        "provider": "github",
        "repo_id": "repo-1",
        "pr_number": 7,
        "head_sha": SHA_A,
        "check_scope": "check_suite",
        "scope_id": "suite-1",
        "attempt": 1,
        "verdict": "passed",
        "verdict_detail": None,
        "source_id": None,
        "observer": "gh-watcher",
        "observer_epoch": 1,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO ci_observation (observation_id, event_seq, provider, repo_id, pr_number,
                                    head_sha, check_scope, scope_id, attempt, verdict,
                                    verdict_detail, source_id, observer, observer_epoch,
                                    occurred_at_ms, ingested_at_ms)
        VALUES (:observation_id, :event_seq, :provider, :repo_id, :pr_number, :head_sha,
                :check_scope, :scope_id, :attempt, :verdict, :verdict_detail, :source_id,
                :observer, :observer_epoch, :occurred_at_ms, :ingested_at_ms)
        """,
        {"observation_id": observation_id, "event_seq": event_seq,
         "occurred_at_ms": at, "ingested_at_ms": at, **row},
    )
    return observation_id


def add_watcher_scope(cp, scope_id: str = "scope-1", at: int = T0, **kwargs) -> str:
    row = {
        "scope_kind": "ci_repository",
        "repo_id": "repo-1",
        "pr_id": None,
        "expected_interval_ms": 60_000,
        "enabled": 1,
        "retired_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO watcher_scope (scope_id, scope_kind, repo_id, pr_id, expected_interval_ms,
                                   enabled, registered_at_ms, retired_at_ms)
        VALUES (:scope_id, :scope_kind, :repo_id, :pr_id, :expected_interval_ms, :enabled,
                :registered_at_ms, :retired_at_ms)
        """,
        {"scope_id": scope_id, "registered_at_ms": at, **row},
    )
    return scope_id


def add_lease(cp, resource: str, holder: str = "watcher-a", epoch: int = 1, at: int = T0,
              ttl_ms: int = 300_000) -> None:
    cp.execute(
        "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        (resource, holder, epoch, at, at + ttl_ms),
    )


#: The fenced heartbeat of ``docs/production-schema.md`` section 8.3, verbatim
#: in shape: an upsert whose insert arm carries the same lease predicate as its
#: update arm, and which derives the lease resource from the scope rather than
#: accepting it as a parameter.
HEARTBEAT = """
INSERT INTO watcher_liveness (
        scope_id, holder, holder_epoch, last_attempt_at_ms, last_result,
        last_success_at_ms, last_change_at_ms, last_error_at_ms, last_error,
        consecutive_errors, attempt_count)
SELECT :scope_id, :holder, :epoch, :now_ms, :result,
       CASE WHEN :result <> 'error'           THEN :now_ms END,
       CASE WHEN :result =  'observed_change' THEN :now_ms END,
       CASE WHEN :result =  'error'           THEN :now_ms END,
       CASE WHEN :result =  'error'           THEN :error  END,
       CASE WHEN :result =  'error' THEN 1 ELSE 0 END, 1
 WHERE EXISTS (SELECT 1 FROM lease
                WHERE resource = 'watcher_scope:' || :scope_id
                  AND holder = :holder AND epoch = :epoch
                  AND expires_at_ms > :now_ms)
    ON CONFLICT(scope_id) DO UPDATE
   SET holder = :holder, holder_epoch = :epoch,
       last_attempt_at_ms = :now_ms, last_result = :result,
       last_success_at_ms = CASE WHEN :result <> 'error'
                                 THEN :now_ms ELSE last_success_at_ms END,
       last_change_at_ms  = CASE WHEN :result = 'observed_change'
                                 THEN :now_ms ELSE last_change_at_ms END,
       last_error_at_ms   = CASE WHEN :result = 'error'
                                 THEN :now_ms ELSE last_error_at_ms END,
       last_error         = CASE WHEN :result = 'error' THEN :error ELSE NULL END,
       consecutive_errors = CASE WHEN :result = 'error'
                                 THEN consecutive_errors + 1 ELSE 0 END,
       attempt_count      = attempt_count + 1
 WHERE watcher_liveness.holder_epoch <= :epoch
   AND EXISTS (SELECT 1 FROM lease
                WHERE resource = 'watcher_scope:' || :scope_id
                  AND holder = :holder AND epoch = :epoch
                  AND expires_at_ms > :now_ms)
"""


def heartbeat(cp, scope_id: str, *, holder: str = "watcher-a", epoch: int = 1,
              now_ms: int = T0, result: str = "observed_no_change",
              error: str | None = None) -> int:
    """Run the fenced heartbeat and return the number of rows it affected.

    Zero rows is the refusal: either the lease is not ours or a higher epoch
    holds the row. That the two are indistinguishable from the row count alone
    is by design (section 8.3) -- the watcher reads once to find out which.
    """

    cursor = cp.execute(HEARTBEAT, {"scope_id": scope_id, "holder": holder, "epoch": epoch,
                                    "now_ms": now_ms, "result": result, "error": error})
    return cursor.rowcount


def add_gate(cp, gate_id: str = "gate-1", origin_event_seq: int | None = None, at: int = T0,
             **kwargs) -> str:
    """A gate as it must be born: stage ``received``, no projection, not closed."""

    if origin_event_seq is None:
        origin_event_seq = add_event(cp, f"evt-open-{gate_id}", at=at, subject_kind="gate",
                                     subject_id=gate_id, event_type="gate.opened")
    row = {
        "gate_type": "worker_escalation",
        "run_id": None,
        "subject_kind": "run",
        "subject_id": "run-1",
        "rationale": "the worker needs a decision it may not make itself",
        "options": '["approve", "reject"]',
        "deadline_at_ms": None,
        "stage": "received",
        "stage_seq": None,
        "outcome": None,
        "superseded_by": None,
        "closed_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO gate (gate_id, gate_type, run_id, subject_kind, subject_id, origin_event_seq,
                          rationale, options, deadline_at_ms, stage, stage_seq,
                          stage_entered_at_ms, outcome, superseded_by, created_at_ms, closed_at_ms)
        VALUES (:gate_id, :gate_type, :run_id, :subject_kind, :subject_id, :origin_event_seq,
                :rationale, :options, :deadline_at_ms, :stage, :stage_seq, :at, :outcome,
                :superseded_by, :at, :closed_at_ms)
        """,
        {"gate_id": gate_id, "origin_event_seq": origin_event_seq, "at": at, **row},
    )
    return gate_id


def add_gate_transition(cp, gate_id: str = "gate-1", transition_kind: str = "open",
                        from_stage: str | None = None, to_stage: str = "received",
                        at: int = T0, **kwargs) -> int:
    row = {
        "actor_kind": "worker",
        "actor_id": "worker-1",
        "writer_epoch": None,
        "message_id": None,
        "body": None,
        "supersedes_seq": None,
    }
    row.update(kwargs)
    cursor = cp.execute(
        """
        INSERT INTO gate_transition (gate_id, transition_kind, from_stage, to_stage, actor_kind,
                                     actor_id, writer_epoch, message_id, body, supersedes_seq,
                                     occurred_at_ms, recorded_at_ms)
        VALUES (:gate_id, :transition_kind, :from_stage, :to_stage, :actor_kind, :actor_id,
                :writer_epoch, :message_id, :body, :supersedes_seq, :at, :at)
        """,
        {"gate_id": gate_id, "transition_kind": transition_kind, "from_stage": from_stage,
         "to_stage": to_stage, "at": at, **row},
    )
    return int(cursor.lastrowid)


def project_gate_stage(cp, gate_id: str, stage: str, stage_seq: int, at: int = T0) -> None:
    """Point the gate's projection at one of its own transitions."""

    cp.execute(
        "UPDATE gate SET stage = ?, stage_seq = ?, stage_entered_at_ms = ? WHERE gate_id = ?",
        (stage, stage_seq, at, gate_id),
    )


def add_policy_revision(cp, note: str, at: int, decided_by: str = "test") -> int:
    cursor = cp.execute(
        "INSERT INTO policy_revision (note, decided_by, effective_at_ms) VALUES (?, ?, ?)",
        (note, decided_by, at),
    )
    return int(cursor.lastrowid)


def add_detection_latency(cp, revision_id: int, incident_class: str, threshold_kind: str,
                          threshold_value: int, reconcile_period_ms: int, budget_ms: int,
                          budget_kind: str = "absolute_ms") -> None:
    cp.execute(
        """
        INSERT INTO policy_detection_latency (revision_id, incident_class, threshold_kind,
                                              threshold_value, reconcile_period_ms, budget_ms,
                                              budget_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (revision_id, incident_class, threshold_kind, threshold_value, reconcile_period_ms,
         budget_ms, budget_kind),
    )


def add_stage_tolerance(cp, revision_id: int, gate_type: str, stage: str,
                        tolerance_ms: int | None) -> None:
    cp.execute(
        "INSERT INTO policy_gate_stage_tolerance (revision_id, gate_type, stage, tolerance_ms)"
        " VALUES (?, ?, ?, ?)",
        (revision_id, gate_type, stage, tolerance_ms),
    )


def add_ai_invocation(cp, invocation_id: str = "inv-1", at: int = T0, **kwargs) -> str:
    row = {
        "incident_id": None,
        "run_id": None,
        "provider": "anthropic",
        "model": "some-model",
        "adapter_version": "a1",
        "usage_status": "reported",
        "output_tokens": 100,
        "input_tokens": 200,
        "cache_read_tokens": None,
        "max_output_tokens": 1024,
        "model_response_count": 1,
        "attempt_count": 1,
        "finished_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO ai_invocation (invocation_id, incident_id, run_id, provider, model,
                                   adapter_version, usage_status, output_tokens, input_tokens,
                                   cache_read_tokens, max_output_tokens, model_response_count,
                                   attempt_count, started_at_ms, finished_at_ms)
        VALUES (:invocation_id, :incident_id, :run_id, :provider, :model, :adapter_version,
                :usage_status, :output_tokens, :input_tokens, :cache_read_tokens,
                :max_output_tokens, :model_response_count, :attempt_count, :started_at_ms,
                :finished_at_ms)
        """,
        {"invocation_id": invocation_id, "started_at_ms": at, **row},
    )
    return invocation_id


def seed_revision_id(cp) -> int:
    """The revision ``0002_policy_seed.sql`` wrote, looked up by its note."""

    row = cp.execute(
        "SELECT revision_id FROM policy_revision WHERE note = ?", (SEED_NOTE,)
    ).fetchone()
    assert row is not None, "0002_policy_seed.sql must have applied"
    return int(row[0])


# The two liveness queries, section 8.4, and the relay-gap detector, section
# 9.6. They are reproduced here rather than imported because they are what the
# design promises the reconcile pass will be able to express -- the assertion is
# about the schema admitting the query, not about any one caller's copy of it.
SILENCE_QUERY = """
SELECT s.scope_id,
       :now_ms - l.last_attempt_at_ms AS silent_for_ms
  FROM watcher_scope s
  JOIN watcher_liveness l ON l.scope_id = s.scope_id
  JOIN policy_detection_latency p
    ON p.incident_class = 'watcher_silence'
   AND p.revision_id = (SELECT revision_id FROM policy_revision
                         WHERE effective_at_ms <= :now_ms
                         ORDER BY effective_at_ms DESC, revision_id DESC LIMIT 1)
 WHERE s.enabled = 1 AND s.retired_at_ms IS NULL
   AND p.threshold_kind = 'scope_interval_multiple'
   AND :now_ms - l.last_attempt_at_ms > s.expected_interval_ms * p.threshold_value
"""

COVERAGE_QUERY = """
SELECT s.scope_id
  FROM watcher_scope s
  LEFT JOIN watcher_liveness l ON l.scope_id = s.scope_id
 WHERE s.enabled = 1 AND s.retired_at_ms IS NULL
   AND l.scope_id IS NULL
"""

RELAY_GAP_QUERY = """
WITH effective AS (
    SELECT revision_id FROM policy_revision
     WHERE effective_at_ms <= :now_ms
     ORDER BY effective_at_ms DESC, revision_id DESC
     LIMIT 1)
SELECT g.gate_id, g.gate_type, g.stage, g.stage_entered_at_ms,
       :now_ms - g.stage_entered_at_ms AS age_ms
  FROM gate g
  JOIN policy_gate_stage_tolerance p
    ON p.gate_type = g.gate_type AND p.stage = g.stage
   AND p.revision_id = (SELECT revision_id FROM effective)
 WHERE g.closed_at_ms IS NULL
   AND p.tolerance_ms IS NOT NULL
   AND :now_ms - g.stage_entered_at_ms > p.tolerance_ms
"""


# --------------------------------------------------------------------------
# section 5 -- the event spine
# --------------------------------------------------------------------------


def test_a_repolled_fact_does_not_append_twice(cp):
    # Section 5.2: a producer that re-polls, restarts mid-append or re-fetches
    # the same page collides on dedup_key. This is what lets several producers
    # share one spine with no single-writer lease over the table.
    add_event(cp, "evt-1", dedup_key="github/check_suite/99/completed")
    with pytest.raises(sqlite3.IntegrityError, match="event.dedup_key"):
        add_event(cp, "evt-2", dedup_key="github/check_suite/99/completed")

    assert cp.execute("SELECT count(*) FROM event").fetchone() == (1,)


def test_the_spine_is_append_only(cp):
    seq = add_event(cp)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        cp.execute("UPDATE event SET payload = '{\"a\":1}' WHERE seq = ?", (seq,))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted|replayed from"):
        cp.execute("DELETE FROM event WHERE seq = ?", (seq,))

    assert cp.execute("SELECT payload FROM event WHERE seq = ?", (seq,)).fetchone() == ("{}",)


def test_a_delivery_subscription_needs_a_recipient_and_a_compute_one_must_not_have_it(cp):
    # Section 5.3: the kind and the recipient are two halves of one fact, and a
    # delivery consumer with nowhere to deliver is a fan-out that silently drops.
    add_consumer(cp, "cons-delivery", kind="delivery")
    add_consumer(cp, "cons-compute", kind="compute")

    with pytest.raises(sqlite3.IntegrityError, match="delivery subscription"):
        add_subscription(cp, "cons-delivery", recipient=None)
    with pytest.raises(sqlite3.IntegrityError, match="compute subscription"):
        add_subscription(cp, "cons-compute", recipient="secretary")

    add_subscription(cp, "cons-delivery", recipient="secretary")
    add_subscription(cp, "cons-compute", recipient=None)
    assert cp.execute("SELECT count(*) FROM consumer_subscription").fetchone() == (2,)


def test_no_committed_event_seq_is_ever_observed_out_of_commit_order(cp, db_path):
    """Section 5.2 / ``D-0030``: the property a consumer cursor rests on.

    A cursor over ``seq`` is sound only if a committed gap can never be filled
    in later -- a consumer that advanced past ``N`` would otherwise never see a
    row arriving at ``N-1``. SQLite serialises write transactions, so ``seq`` is
    assigned in commit order. This test is the thing that fails if this database
    is ever put behind something that admits concurrent writers.
    """

    writer_a = second_connection(db_path)
    writer_b = second_connection(db_path)
    reader = second_connection(db_path)
    try:
        writer_a.execute("BEGIN IMMEDIATE")
        seq_a = add_event(writer_a, "evt-a", at=T0)

        # The interleave the design's soundness argument depends on being
        # impossible: while A holds the write transaction, B cannot start one,
        # so B cannot be assigned a seq that commits before A's.
        with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            writer_b.execute("BEGIN IMMEDIATE")

        # Nothing A wrote is visible anywhere else until it commits.
        assert reader.execute("SELECT count(*) FROM event").fetchone() == (0,)
        writer_a.execute("COMMIT")
        assert reader.execute("SELECT seq FROM event ORDER BY seq").fetchall() == [(seq_a,)]

        writer_b.execute("BEGIN IMMEDIATE")
        seq_b = add_event(writer_b, "evt-b", at=T0 + 1)
        # B's row is later in commit order and it is later in seq order; and it
        # is still invisible, so no reader can observe B before A.
        assert seq_b > seq_a
        assert reader.execute("SELECT seq FROM event ORDER BY seq").fetchall() == [(seq_a,)]
        writer_b.execute("COMMIT")

        observed = [row[0] for row in reader.execute("SELECT seq FROM event ORDER BY rowid")]
        assert observed == sorted(observed) == [seq_a, seq_b]
    finally:
        for connection in (writer_a, writer_b, reader):
            connection.close()


def test_an_append_whose_fanout_fails_leaves_no_event_behind(cp):
    """Section 5.4: the append is one transaction, or it is nothing.

    An event on the spine with no delivery record is exactly v1's
    push-vs-poll duplication -- the window in which a fact exists and nobody is
    obliged to deliver it. The fan-out below dies on its second consumption row
    (an unregistered consumer), and the property asserted is that the *event*
    goes with it.
    """

    add_consumer(cp, "cons-1", kind="compute")

    cp.execute("BEGIN")
    seq = add_event(cp, "evt-1", dedup_key="fact/1")
    add_consumption(cp, "cons-1", seq)
    with pytest.raises(sqlite3.IntegrityError):
        add_consumption(cp, "cons-never-registered", seq)
    cp.execute("ROLLBACK")

    assert cp.execute("SELECT count(*) FROM event").fetchone() == (0,)
    assert cp.execute("SELECT count(*) FROM event_consumption").fetchone() == (0,)

    # And the whole append, done in one transaction, lands as one unit: the
    # event, the per-consumer consumption row, the outbox row for the delivery
    # consumer, and the typed side table row keyed by the event's seq.
    add_repository(cp)
    add_consumer(cp, "cons-relay", kind="delivery")
    cp.execute("BEGIN")
    seq = add_event(cp, "evt-1", dedup_key="fact/1", subject_kind="pull_request",
                    subject_id="pr-1")
    add_consumption(cp, "cons-1", seq)
    add_outbox(cp, "msg-1", dedup_key="event/evt-1/cons-relay")
    add_consumption(cp, "cons-relay", seq, message_id="msg-1")
    add_ci_observation(cp, "obs-1", event_seq=seq)
    cp.execute("COMMIT")

    assert cp.execute(
        "SELECT count(*) FROM event_consumption WHERE event_seq = ?", (seq,)
    ).fetchone() == (2,)
    assert cp.execute(
        "SELECT message_id FROM event_consumption WHERE consumer_id = 'cons-relay'"
    ).fetchone() == ("msg-1",)
    assert cp.execute("SELECT event_seq FROM ci_observation").fetchone() == (seq,)


# --------------------------------------------------------------------------
# section 6 -- CI observation identity, ordering and the verdict projection
# --------------------------------------------------------------------------


def test_a_repeated_ci_observation_identity_is_refused(cp):
    add_repository(cp)
    add_ci_observation(cp, "obs-1")
    with pytest.raises(sqlite3.IntegrityError, match="ci_observation"):
        add_ci_observation(cp, "obs-2")

    # A re-run is a different attempt, and a different attempt is a different
    # fact rather than a duplicate of the same one.
    add_ci_observation(cp, "obs-3", attempt=2)
    assert cp.execute("SELECT count(*) FROM ci_observation").fetchone() == (2,)


def test_a_provider_outside_the_closed_set_is_refused_on_an_observation(cp):
    # D-0033: provider is CHECKed to 'github' alone, and a second provider
    # widens the CHECK in a migration step that brings its substitution test.
    # ci_observation is held to the same narrowing as repository.
    add_repository(cp)
    for index, spelling in enumerate(("GITHUB", "github.com", "gitlab", " github")):
        with pytest.raises(sqlite3.IntegrityError, match="provider"):
            add_ci_observation(cp, f"obs-bad-{index}", provider=spelling)
    add_ci_observation(cp, "obs-1", provider="github")


def test_a_provider_spelling_variant_cannot_reproject_a_red_pull_request_green(cp):
    # The cost of a merely non-empty provider, demonstrated. provider is part of
    # ci_observation_identity, so a spelling variant is admitted as a second row
    # for the SAME fact; the ci_current_verdict per-scope subquery does not
    # discriminate on provider, so the later-timestamped bogus row would win on
    # occurred_at_ms and the red PR would project green -- the section 6.1
    # verdict-honesty failure. The narrowed CHECK is what makes the second
    # insert impossible.
    add_repository(cp)
    add_pull_request(cp)
    add_ci_observation(cp, "obs-red", verdict="failed", at=T0)
    with pytest.raises(sqlite3.IntegrityError, match="provider"):
        add_ci_observation(cp, "obs-green", provider="GITHUB", verdict="passed", at=T0 + 1_000)

    assert cp.execute("SELECT verdict FROM ci_current_verdict").fetchall() == [("failed",)]


def test_a_verdict_outside_the_closed_set_is_refused(cp):
    add_repository(cp)
    with pytest.raises(sqlite3.IntegrityError, match="verdict"):
        add_ci_observation(cp, "obs-1", verdict="probably_fine")

    for index, verdict in enumerate(
        ("passed", "failed", "cancelled", "timed_out", "no_run", "indeterminate")
    ):
        add_ci_observation(cp, f"obs-v{index}", verdict=verdict)


def test_an_indeterminate_observation_is_superseded_by_the_recovered_verdict(cp):
    # Section 6.2: 'could not observe' is a verdict of its own, and the recovery
    # supersedes it without either row being rewritten. The repeat of the
    # recovered verdict is still the same fact and is still refused.
    add_repository(cp)
    add_pull_request(cp)
    add_ci_observation(cp, "obs-1", verdict="indeterminate", at=T0)
    add_ci_observation(cp, "obs-2", verdict="failed", at=T0 + 1_000)
    with pytest.raises(sqlite3.IntegrityError):
        add_ci_observation(cp, "obs-3", verdict="failed", at=T0 + 2_000)

    assert cp.execute("SELECT verdict FROM ci_current_verdict").fetchall() == [("failed",)]


def test_a_rollup_drops_out_of_the_projection_once_a_finegrained_scope_exists(cp):
    # Section 6.3 rule 3: the rollup is a fallback, not a peer. Two rows for one
    # head would otherwise let a reader pick whichever agreed with it.
    add_repository(cp)
    add_pull_request(cp)
    add_ci_observation(cp, "obs-rollup", check_scope="rollup", scope_id="head", verdict="passed")
    assert {row[0] for row in cp.execute("SELECT check_scope FROM ci_current_verdict")} == {
        "rollup"
    }

    add_ci_observation(cp, "obs-suite", check_scope="check_suite", scope_id="suite-1",
                       verdict="failed", at=T0 + 1_000)
    assert cp.execute(
        "SELECT check_scope, verdict FROM ci_current_verdict"
    ).fetchall() == [("check_suite", "failed")]


# --------------------------------------------------------------------------
# section 7 -- run to PR linkage
# --------------------------------------------------------------------------


def test_a_merged_pull_request_does_not_reopen(cp):
    add_repository(cp)
    add_pull_request(cp)
    cp.execute(
        "UPDATE pull_request SET state = 'merged', merged_at_ms = ?, closed_at_ms = ?,"
        " merge_commit_sha = ?, updated_at_ms = ? WHERE pr_id = 'pr-1'",
        (T0 + 10, T0 + 10, SHA_B, T0 + 10),
    )
    with pytest.raises(sqlite3.IntegrityError, match="does not reopen"):
        cp.execute("UPDATE pull_request SET state = 'open' WHERE pr_id = 'pr-1'")


def test_a_closed_unmerged_pull_request_reopens_and_a_merged_one_does_not(cp):
    # The two cases the single 'is it closed' flag cannot tell apart: a close is
    # revocable and a merge is a fact.
    add_repository(cp)
    add_pull_request(cp, "pr-open", pr_number=1)
    cp.execute(
        "UPDATE pull_request SET state = 'closed', closed_at_ms = ? WHERE pr_id = 'pr-open'",
        (T0 + 10,),
    )
    cp.execute(
        "UPDATE pull_request SET state = 'open', closed_at_ms = NULL WHERE pr_id = 'pr-open'"
    )
    assert cp.execute(
        "SELECT state, closed_at_ms FROM pull_request WHERE pr_id = 'pr-open'"
    ).fetchone() == ("open", None)

    add_pull_request(cp, "pr-merged", pr_number=2)
    cp.execute(
        "UPDATE pull_request SET state = 'merged', merged_at_ms = ?, closed_at_ms = ?,"
        " merge_commit_sha = ? WHERE pr_id = 'pr-merged'",
        (T0 + 10, T0 + 10, SHA_B),
    )
    with pytest.raises(sqlite3.IntegrityError, match="does not reopen"):
        cp.execute(
            "UPDATE pull_request SET state = 'open', closed_at_ms = NULL,"
            " merged_at_ms = NULL, merge_commit_sha = NULL WHERE pr_id = 'pr-merged'"
        )


def test_a_late_older_head_observation_cannot_revive_superseded_ci_evidence(cp):
    # Section 7.2: the head is a projection of the provider's own order, so a
    # slow poller returning with yesterday's head must be refused rather than
    # rewinding the head every CI verdict is keyed by.
    add_repository(cp)
    first = add_event(cp, "evt-head-1", at=T0)
    add_pull_request(cp, head_sha=SHA_A, head_event_seq=first, at=T0)
    later = add_event(cp, "evt-head-2", at=T0 + 1_000)

    cp.execute(
        "UPDATE pull_request SET head_sha = ?, head_observed_at_ms = ?, head_event_seq = ?"
        " WHERE pr_id = 'pr-1'",
        (SHA_B, T0 + 1_000, later),
    )
    with pytest.raises(sqlite3.IntegrityError, match="only moves forward"):
        cp.execute(
            "UPDATE pull_request SET head_sha = ?, head_observed_at_ms = ?, head_event_seq = ?"
            " WHERE pr_id = 'pr-1'",
            (SHA_A, T0, first),
        )

    assert cp.execute("SELECT head_sha FROM pull_request").fetchone() == (SHA_B,)


def test_a_second_live_primary_pr_per_run_is_refused_and_a_repoint_keeps_both_links(cp):
    # Section 7.3: one live primary, unbounded history. The unlink is what makes
    # a re-point expressible without deleting the evidence of the first link.
    add_run(cp)
    add_repository(cp)
    add_pull_request(cp, "pr-1", pr_number=1)
    add_pull_request(cp, "pr-2", pr_number=2)
    add_run_pr_link(cp, pr_id="pr-1")

    with pytest.raises(sqlite3.IntegrityError, match="run_pr_link.run_id"):
        add_run_pr_link(cp, pr_id="pr-2")

    cp.execute(
        "UPDATE run_pr_link SET unlinked_at_ms = ?, unlink_reason = ? WHERE pr_id = 'pr-1'",
        (T0 + 10, "re-pointed at the reopened PR"),
    )
    add_run_pr_link(cp, pr_id="pr-2", at=T0 + 11)

    assert cp.execute(
        "SELECT pr_id, unlinked_at_ms IS NULL FROM run_pr_link ORDER BY pr_id"
    ).fetchall() == [("pr-1", 0), ("pr-2", 1)]


def test_a_link_resolution_cannot_say_we_guessed_from_the_working_directory(cp):
    # Section 7.4: how the link was resolved is evidence. A guess recorded as a
    # resolution is a link nobody can later audit.
    add_run(cp)
    add_repository(cp)
    add_pull_request(cp)
    with pytest.raises(sqlite3.IntegrityError, match="resolution"):
        add_run_pr_link(cp, resolution="working_directory_guess")

    for index, resolution in enumerate(
        ("project_registry", "explicit_operator", "provider_event")
    ):
        add_pull_request(cp, f"pr-r{index}", pr_number=10 + index)
        add_run_pr_link(cp, pr_id=f"pr-r{index}", role="supporting", resolution=resolution)


# --------------------------------------------------------------------------
# section 8 -- watcher liveness
# --------------------------------------------------------------------------


def test_a_stale_watchers_heartbeat_is_refused_by_the_fence(cp):
    add_repository(cp)
    scope = add_watcher_scope(cp)
    add_lease(cp, f"watcher_scope:{scope}", holder="watcher-a", epoch=7)

    assert heartbeat(cp, scope, epoch=7, now_ms=T0 + 1) == 1
    # The replaced watcher returning with its old token matches neither arm.
    assert heartbeat(cp, scope, epoch=3, now_ms=T0 + 2) == 0
    assert cp.execute(
        "SELECT holder_epoch, last_attempt_at_ms FROM watcher_liveness"
    ).fetchone() == (7, T0 + 1)


def test_a_watcher_bootstraps_and_then_keeps_both_success_and_error_history(cp):
    # Section 8.3: the insert arm exists so that the first heartbeat of a scope
    # is not indistinguishable from a stale-writer refusal, and the two history
    # columns are implications rather than biconditionals so that a recovery and
    # a failure are both writable.
    add_repository(cp)
    scope = add_watcher_scope(cp)
    add_lease(cp, f"watcher_scope:{scope}", epoch=1)

    assert heartbeat(cp, scope, now_ms=T0 + 1, result="observed_change") == 1
    assert heartbeat(cp, scope, now_ms=T0 + 2, result="error", error="403 from the provider") == 1
    assert heartbeat(cp, scope, now_ms=T0 + 3, result="observed_no_change") == 1

    row = cp.execute(
        "SELECT last_result, last_success_at_ms, last_change_at_ms, last_error_at_ms,"
        " last_error, consecutive_errors, attempt_count FROM watcher_liveness"
    ).fetchone()
    assert row == ("observed_no_change", T0 + 3, T0 + 1, T0 + 2, None, 0, 3)


def test_a_roster_entry_must_name_a_subject(cp):
    # The pr_id biconditional only binds the ci_pull_request kind, so a
    # 'ci_repository' row with repo_id and pr_id both NULL would be a roster
    # entry for nothing at all. No watcher has a subject to heartbeat for, so
    # the section 8.4 coverage query below would report it as uncovered forever
    # -- and a roster that permanently alarms is a roster nobody reads, which
    # defeats the one thing the roster exists to do.
    add_repository(cp)
    with pytest.raises(sqlite3.IntegrityError, match="repo_id"):
        add_watcher_scope(cp, "scope-nowhere", repo_id=None)

    # The pull-request kind still needs its repository named too, so the
    # biconditional and this rule hold together rather than at each other's
    # expense.
    add_pull_request(cp)
    with pytest.raises(sqlite3.IntegrityError, match="repo_id"):
        add_watcher_scope(cp, "scope-pr-nowhere", scope_kind="ci_pull_request",
                          repo_id=None, pr_id="pr-1")
    add_watcher_scope(cp, "scope-pr", scope_kind="ci_pull_request", pr_id="pr-1")

    # Nothing subjectless survived to sit in the coverage report.
    assert cp.execute(COVERAGE_QUERY).fetchall() == [("scope-pr",)]


def test_a_watcher_holding_another_scopes_lease_cannot_heartbeat_this_one(cp):
    # The lease resource is derived from the scope inside the statement, so a
    # misrouted heartbeat cannot mark an unwatched scope healthy and silence its
    # watcher_silence predicate.
    add_repository(cp)
    add_watcher_scope(cp, "scope-a")
    add_watcher_scope(cp, "scope-b")
    add_lease(cp, "watcher_scope:scope-b", holder="watcher-b", epoch=1)

    assert heartbeat(cp, "scope-b", holder="watcher-b", now_ms=T0 + 1) == 1
    assert heartbeat(cp, "scope-a", holder="watcher-b", now_ms=T0 + 1) == 0

    assert cp.execute("SELECT scope_id FROM watcher_liveness").fetchall() == [("scope-b",)]
    assert cp.execute(COVERAGE_QUERY).fetchall() == [("scope-a",)]


def test_the_silence_query_scales_the_policy_multiple_by_the_scopes_own_interval(cp):
    # Section 8.4: the threshold is stored as a multiple precisely so that one
    # scope's poll interval is not baked into a row every other scope reads.
    add_repository(cp)
    scope = add_watcher_scope(cp, expected_interval_ms=60_000)
    add_lease(cp, f"watcher_scope:{scope}", epoch=1, ttl_ms=10 ** 9)
    heartbeat(cp, scope, now_ms=T0)

    multiple = cp.execute(
        "SELECT threshold_value FROM policy_detection_latency"
        " WHERE revision_id = ? AND incident_class = 'watcher_silence'",
        (seed_revision_id(cp),),
    ).fetchone()
    assert multiple == (3,), "time-base-policy.md section 3.2: three missed polls"

    quiet_for_two = {"now_ms": T0 + 120_000}
    assert cp.execute(SILENCE_QUERY, quiet_for_two).fetchall() == []
    quiet_for_three_point_three = {"now_ms": T0 + 198_000}
    assert cp.execute(SILENCE_QUERY, quiet_for_three_point_three).fetchall() == [
        (scope, 198_000)
    ]


# --------------------------------------------------------------------------
# section 9 -- the Gate entity
# --------------------------------------------------------------------------


def test_a_gate_cannot_be_created_already_claiming_a_projection(cp):
    # Section 9.2: the projection is set by the opening transition, which cannot
    # exist before the gate does. A gate born at 'presented', or born naming a
    # stage_seq, or born closed, is a claim with no history under it.
    with pytest.raises(sqlite3.IntegrityError, match="opens at stage received"):
        add_gate(cp, "gate-presented", stage="presented")
    with pytest.raises(sqlite3.IntegrityError, match="opens at stage received"):
        add_gate(cp, "gate-seq", stage_seq=1)
    with pytest.raises(sqlite3.IntegrityError, match="opens at stage received"):
        add_gate(cp, "gate-closed", outcome="withdrawn", closed_at_ms=T0)

    assert cp.execute("SELECT count(*) FROM gate").fetchone() == (0,)


def test_a_gate_can_be_opened_end_to_end(cp):
    # Create with a null projection, append the opening transition, then point
    # the projection at it -- the only order the triggers admit.
    gate = add_gate(cp)
    opened = add_gate_transition(cp, gate, transition_kind="open", from_stage=None,
                                 to_stage="received")
    project_gate_stage(cp, gate, "received", opened)

    assert cp.execute("SELECT stage, stage_seq FROM gate").fetchone() == ("received", opened)

    # And the projection still cannot claim a stage no transition of this gate
    # reached.
    with pytest.raises(sqlite3.IntegrityError, match="projection"):
        project_gate_stage(cp, gate, "answered", opened, at=T0 + 1)


def test_gate_stage_may_only_name_an_open_or_advance_transition_of_its_own_gate(cp):
    """Section 9.2, with one section 11 row corrected rather than reproduced.

    The section 11 table claims the trigger "fires when pointed at the ``open``
    transition". It does not, and it must not: the section 9.2 DDL admits
    ``transition_kind IN ('open', 'advance')``, and another section 11 row says
    an end-to-end open -- whose only transition is the ``open`` one -- is
    accepted. Both cannot be true. The DDL wins, because a gate whose opening
    transition could not back its own projection could never reach a legal
    state at all. **Document fix owed: that one row of section 11 is stale and
    should read "fires when pointed at a resend, correction or close
    transition, or at another gate's transition".**
    """

    gate = add_gate(cp, "gate-1")
    other = add_gate(cp, "gate-2")
    opened = add_gate_transition(cp, gate, transition_kind="open", to_stage="received")
    project_gate_stage(cp, gate, "received", opened)

    advance = add_gate_transition(cp, gate, transition_kind="advance", from_stage="received",
                                  to_stage="presented", at=T0 + 1)
    project_gate_stage(cp, gate, "presented", advance, at=T0 + 1)
    assert cp.execute(
        "SELECT stage, stage_seq FROM gate WHERE gate_id = 'gate-1'"
    ).fetchone() == ("presented", advance)

    # What the trigger is actually for: a stage backed by a transition kind that
    # does not move the gate, and a stage backed by another gate's history.
    resend = add_gate_transition(cp, gate, transition_kind="resend", from_stage="presented",
                                 to_stage="presented", at=T0 + 2)
    with pytest.raises(sqlite3.IntegrityError, match="projection"):
        project_gate_stage(cp, gate, "presented", resend, at=T0 + 2)

    foreign = add_gate_transition(cp, other, transition_kind="open", to_stage="received",
                                  at=T0 + 3)
    with pytest.raises(sqlite3.IntegrityError, match="projection"):
        project_gate_stage(cp, gate, "received", foreign, at=T0 + 3)


def test_a_gate_stage_projection_never_walks_backwards(cp):
    gate = add_gate(cp)
    opened = add_gate_transition(cp, gate, transition_kind="open", to_stage="received")
    project_gate_stage(cp, gate, "received", opened)
    advance = add_gate_transition(cp, gate, transition_kind="advance", from_stage="received",
                                  to_stage="presented", at=T0 + 1)
    project_gate_stage(cp, gate, "presented", advance, at=T0 + 1)

    with pytest.raises(sqlite3.IntegrityError, match="never walks backwards"):
        project_gate_stage(cp, gate, "received", opened, at=T0 + 2)
    with pytest.raises(sqlite3.IntegrityError, match="never walks backwards"):
        cp.execute("UPDATE gate SET stage_seq = NULL WHERE gate_id = ?", (gate,))


def test_gate_transitions_are_immutable_and_undeletable(cp):
    gate = add_gate(cp)
    seq = add_gate_transition(cp, gate, transition_kind="open", to_stage="received")

    with pytest.raises(sqlite3.IntegrityError, match="correction transition"):
        cp.execute("UPDATE gate_transition SET to_stage = 'answered' WHERE seq = ?", (seq,))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted|relay-gap evidence"):
        cp.execute("DELETE FROM gate_transition WHERE seq = ?", (seq,))


def test_an_outcome_outside_the_terminal_taxonomy_is_refused(cp):
    # Section 9.4: 'closed' is not an outcome. Every close names which of the
    # six ways it ended, because the taxonomy is what the measurement harness
    # counts against.
    gate = add_gate(cp)
    with pytest.raises(sqlite3.IntegrityError, match="outcome"):
        cp.execute(
            "UPDATE gate SET outcome = 'done', closed_at_ms = ? WHERE gate_id = ?",
            (T0 + 1, gate),
        )

    cp.execute(
        "UPDATE gate SET outcome = 'expired', closed_at_ms = ? WHERE gate_id = ?",
        (T0 + 1, gate),
    )
    assert cp.execute("SELECT outcome FROM gate").fetchone() == ("expired",)


def test_a_closed_gate_keeps_its_outcome(cp):
    gate = add_gate(cp)
    cp.execute(
        "UPDATE gate SET outcome = 'withdrawn', closed_at_ms = ? WHERE gate_id = ?",
        (T0 + 1, gate),
    )

    with pytest.raises(sqlite3.IntegrityError, match="keeps its outcome"):
        cp.execute("UPDATE gate SET outcome = 'expired' WHERE gate_id = ?", (gate,))
    with pytest.raises(sqlite3.IntegrityError, match="keeps its outcome"):
        cp.execute("UPDATE gate SET closed_at_ms = NULL WHERE gate_id = ?", (gate,))


def test_a_second_relay_for_the_same_gate_stage_is_refused(cp):
    # Section 9.5: the enqueue is idempotent because the relay row is the
    # identity of 'this stage has been sent', not a log of sends.
    gate = add_gate(cp)
    add_outbox(cp, "msg-1", dedup_key="gate/gate-1/presented")
    add_outbox(cp, "msg-2", dedup_key="gate/gate-1/presented/again")
    cp.execute(
        "INSERT INTO gate_relay (gate_id, to_stage, message_id, enqueued_at_ms)"
        " VALUES (?, 'presented', 'msg-1', ?)",
        (gate, T0),
    )

    with pytest.raises(sqlite3.IntegrityError, match="gate_relay"):
        cp.execute(
            "INSERT INTO gate_relay (gate_id, to_stage, message_id, enqueued_at_ms)"
            " VALUES (?, 'presented', 'msg-2', ?)",
            (gate, T0 + 1),
        )


def test_the_relay_gap_detector_emits_one_row_per_gate_with_two_revisions_on_record(cp):
    # Section 9.6: policy rows are versioned and never updated in place, so a
    # join without a revision predicate would alarm once per tolerance ever
    # recorded -- some of them retired months ago.
    gate = add_gate(cp)
    opened = add_gate_transition(cp, gate, transition_kind="open", to_stage="received")
    project_gate_stage(cp, gate, "received", opened)

    later = add_policy_revision(cp, "a later tolerance for the same stage", at=T0)
    add_stage_tolerance(cp, later, "worker_escalation", "received", 240_000)

    now = T0 + 300_000
    rows = cp.execute(RELAY_GAP_QUERY, {"now_ms": now}).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == gate

    # And a gate inside the effective revision's tolerance emits nothing.
    assert cp.execute(RELAY_GAP_QUERY, {"now_ms": T0 + 200_000}).fetchall() == []


# --------------------------------------------------------------------------
# section 10 -- policy data
# --------------------------------------------------------------------------


def test_the_detection_budget_check_includes_the_reconcile_period(cp):
    # time-base-policy.md section 3.1: T + P <= L is the whole derivation, and
    # making it a CHECK is what stops a tolerance being raised to the budget and
    # leaving the pass no room to notice the crossing.
    revision = add_policy_revision(cp, "budget arithmetic", at=T0)

    add_detection_latency(cp, revision, "relay_gap", "absolute_ms", 180_000, 120_000, 300_000)
    with pytest.raises(sqlite3.IntegrityError):
        add_detection_latency(cp, revision, "t_equals_l", "absolute_ms", 300_000, 120_000,
                              300_000)
    with pytest.raises(sqlite3.IntegrityError):
        add_detection_latency(cp, revision, "over_budget", "absolute_ms", 240_000, 120_000,
                              300_000)


def test_a_relative_threshold_stores_losslessly_and_an_over_budget_absolute_one_does_not(cp):
    # Section 10: three of the classes are not absolute durations, and
    # precomputing them into milliseconds would bake one subject's interval or
    # TTL into a row every other subject also reads.
    revision = add_policy_revision(cp, "the four kinds", at=T0)

    add_detection_latency(cp, revision, "watcher_silence", "scope_interval_multiple", 3,
                          120_000, 600_000)
    add_detection_latency(cp, revision, "watcher_error_streak", "consecutive_count", 5,
                          120_000, 600_000)
    add_detection_latency(cp, revision, "lease_orphan", "lease_ttl_multiple", 1, 120_000, 2,
                          budget_kind="lease_ttl_multiple")
    add_detection_latency(cp, revision, "ci_outcome_undrained", "absolute_ms", 180_000,
                          120_000, 300_000)
    assert cp.execute(
        "SELECT count(*) FROM policy_detection_latency WHERE revision_id = ?", (revision,)
    ).fetchone() == (4,)

    with pytest.raises(sqlite3.IntegrityError):
        add_detection_latency(cp, revision, "over_budget", "absolute_ms", 600_000, 120_000,
                              300_000)
    with pytest.raises(sqlite3.IntegrityError, match="threshold_kind"):
        add_detection_latency(cp, revision, "unknown_kind", "vibes", 1, 120_000, 300_000)


def test_budget_kind_exempts_a_relative_budget_from_the_absolute_arithmetic(cp):
    """The second adjudicated gap, pinned.

    ``time-base-policy.md`` section 3.2 gives ``lease_orphan`` a budget of
    ``2 x lease TTL``, which an absolute ``budget_ms`` column cannot hold: read
    as milliseconds, ``2`` is smaller than any threshold and the ``T + P <= L``
    CHECK would refuse the row the policy table exists to carry. So the budget
    carries its own kind, the CHECK applies only when *both* sides are absolute,
    and a relative budget is asserted per subject by the reconcile pass's
    ``policy_budget_violation`` instead -- where the subject's own TTL is known.
    """

    revision = add_policy_revision(cp, "relative budgets", at=T0)

    # 1 + 120000 > 2 by absolute arithmetic; the row is still legal, because 2
    # is a multiple of the lease TTL and not a duration.
    add_detection_latency(cp, revision, "lease_orphan", "lease_ttl_multiple", 1, 120_000, 2,
                          budget_kind="lease_ttl_multiple")

    # The default keeps every row that says nothing absolute, so the CHECK is
    # not opted out of by omission.
    cp.execute(
        "INSERT INTO policy_detection_latency (revision_id, incident_class, threshold_kind,"
        " threshold_value, reconcile_period_ms, budget_ms) VALUES (?, 'defaulted',"
        " 'absolute_ms', 180000, 120000, 300000)",
        (revision,),
    )
    assert cp.execute(
        "SELECT budget_kind FROM policy_detection_latency"
        " WHERE revision_id = ? AND incident_class = 'defaulted'",
        (revision,),
    ).fetchone() == ("absolute_ms",)
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute(
            "INSERT INTO policy_detection_latency (revision_id, incident_class, threshold_kind,"
            " threshold_value, reconcile_period_ms, budget_ms) VALUES (?, 'defaulted_over',"
            " 'absolute_ms', 240000, 120000, 300000)",
            (revision,),
        )

    with pytest.raises(sqlite3.IntegrityError, match="budget_kind"):
        add_detection_latency(cp, revision, "unknown_budget", "absolute_ms", 1, 120_000, 2,
                              budget_kind="ttl")


def test_the_seeded_policy_is_the_time_base_documents_own_numbers(cp):
    # 0002_policy_seed.sql is time-base-policy.md section 3.2 as data. If a
    # number moves in the document without the seed moving with it, the
    # detector runs on a tolerance nobody decided.
    seeded = {
        row[0]: row[1:]
        for row in cp.execute(
            "SELECT incident_class, threshold_kind, threshold_value, reconcile_period_ms,"
            " budget_ms, budget_kind FROM policy_detection_latency WHERE revision_id = ?",
            (seed_revision_id(cp),),
        )
    }

    assert seeded["relay_gap"] == ("absolute_ms", 180_000, 120_000, 300_000, "absolute_ms")
    assert seeded["ci_outcome_undrained"] == (
        "absolute_ms", 180_000, 120_000, 300_000, "absolute_ms")
    assert seeded["consumer_backlog"] == (
        "absolute_ms", 300_000, 120_000, 600_000, "absolute_ms")
    assert seeded["watcher_silence"] == (
        "scope_interval_multiple", 3, 120_000, 600_000, "absolute_ms")
    assert seeded["watcher_error_streak"] == (
        "consecutive_count", 5, 120_000, 600_000, "absolute_ms")
    assert seeded["lease_orphan"] == ("lease_ttl_multiple", 1, 120_000, 2, "lease_ttl_multiple")
    # The reconcile period is one decision, not one per class.
    assert {values[2] for values in seeded.values()} == {120_000}


# --------------------------------------------------------------------------
# measurement-harness.md section 2.3 -- the AI invocation record
# --------------------------------------------------------------------------


def test_an_invocations_output_token_ceiling_scales_with_its_response_count(cp):
    # A multi-response invocation legitimately exceeds a single response's cap,
    # so the ceiling is per response and the CHECK multiplies. Asserting against
    # the flat cap would refuse every honest agentic loop.
    add_ai_invocation(cp, "inv-many", output_tokens=3_000, max_output_tokens=1_024,
                      model_response_count=4)
    with pytest.raises(sqlite3.IntegrityError):
        add_ai_invocation(cp, "inv-one", output_tokens=3_000, max_output_tokens=1_024,
                          model_response_count=1)


def test_an_invocation_that_reports_usage_must_carry_the_tokens_it_reported(cp):
    # measurement-harness.md: 'unavailable' and 'zero' must stay distinguishable,
    # or every provider outage reads as a free invocation in the report.
    with pytest.raises(sqlite3.IntegrityError):
        add_ai_invocation(cp, "inv-1", usage_status="reported", output_tokens=None)
    add_ai_invocation(cp, "inv-2", usage_status="unavailable", output_tokens=None)


# --------------------------------------------------------------------------
# section 3.1 -- the migration ledger
# --------------------------------------------------------------------------


def test_a_migration_record_is_written_once_and_never_deleted(cp):
    applied = cp.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
    assert applied, "the fixture database is migrated to head"

    with pytest.raises(sqlite3.IntegrityError, match="written once"):
        cp.execute("UPDATE schema_migration SET checksum = ? WHERE version = 1", ("0" * 64,))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        cp.execute("DELETE FROM schema_migration WHERE version = 1")


# --------------------------------------------------------------------------
# section 2 -- the two conventions, and the adjudicated run status
# --------------------------------------------------------------------------


def test_run_status_is_a_closed_set_that_only_walks_forward(cp):
    """The first adjudicated gap, pinned.

    Section 2 says the production ``run`` table "carries a CHECK on a closed
    status set and a forward-only trigger" and never enumerates the set. The set
    adopted here is ``created -> running <-> suspended -> {completed, failed,
    cancelled}``. Two properties are deliberate: ``suspended`` is **not**
    terminal, because a suspend is resumable (``time-base-policy.md`` section
    3.4 has a paused run suspend its session predicates by *status*, which only
    works if the status can come back); and a terminal run is never reopened,
    because every completion is a fact some report has already counted.
    """

    with pytest.raises(sqlite3.IntegrityError):
        add_run(cp, "run-bogus", status="paused")

    run = add_run(cp, "run-1", status="created")
    for legal in ("running", "suspended", "running", "completed"):
        cp.execute("UPDATE run SET status = ? WHERE run_id = ?", (legal, run))
    assert cp.execute("SELECT status FROM run").fetchone() == ("completed",)

    for reopen in ("created", "running", "suspended", "failed", "cancelled"):
        with pytest.raises(sqlite3.IntegrityError, match="never reopened"):
            cp.execute("UPDATE run SET status = ? WHERE run_id = ?", (reopen, run))

    rewound = add_run(cp, "run-2", status="running")
    with pytest.raises(sqlite3.IntegrityError, match="terminal"):
        cp.execute("UPDATE run SET status = 'created' WHERE run_id = ?", (rewound,))

    # A suspend is not a terminal state: it resumes, and it may also end.
    cp.execute("UPDATE run SET status = 'suspended' WHERE run_id = ?", (rewound,))
    cp.execute("UPDATE run SET status = 'cancelled' WHERE run_id = ?", (rewound,))


def test_no_timestamp_column_in_the_production_schema_carries_a_default(cp):
    # Section 2: the clock is the caller's. ACCEPTANCE.md section 2 injects
    # clock skew across expiry boundaries, and a column defaulted to SQLite's
    # own clock makes that case untestable while handing a recovering process a
    # timestamp it never chose.
    tables = [
        name
        for (name,) in cp.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        for row in cp.execute(f"PRAGMA table_info({table})"):
            column, default = row[1], row[4]
            if column.endswith("_at_ms") or column.endswith("_at"):
                assert default is None, f"{table}.{column} defaults to {default!r}"
