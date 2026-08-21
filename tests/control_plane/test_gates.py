"""G4 -- what the gate ledger must keep true, stated as the properties it exists for.

``docs/production-schema.md`` section 9 argues for a staged gate with an
immutable transition history, a relay that advances on the ack, and six terminal
outcomes. Each of those is an argument about a *failure* -- a question the human
saw twice, an answer the worker never got, a cancelled run's gate alarming
forever -- and an argument about a failure is only settled by a test that
reproduces it.

So the tests below are named after the properties rather than the functions, and
three of them are the ones the design would be worthless without:

* :func:`test_a_kill_at_any_relay_step_recovers_to_exactly_one_message_and_one_advance`
  is section 9.5's whole reason for existing, driven as a table over the four
  kill points the section enumerates.
* :func:`test_a_rewind_is_refused_and_re_asking_is_a_new_gate_linked_by_superseded_by`
  pins the no-backwards-edge rule, whose cost of being wrong is silent: a rewind
  resets ``stage_entered_at_ms`` and turns an old unanswered question into a
  young one exactly when somebody noticed it was old.
* :func:`test_a_long_presented_gate_is_not_a_relay_gap_and_the_opt_out_is_data`
  asserts that "a slow human is not a gap" lives in ``policy_gate_stage_tolerance``
  and not in the detector, by giving ``presented`` a tolerance in a new revision
  and watching the *same* query start reporting it.

Every timestamp is :data:`T0` plus arithmetic. No test reads a clock, because a
suite whose expectations move with the wall clock cannot assert a tolerance
boundary -- and the schema gives no timestamp column a ``DEFAULT`` for the same
reason.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import gates, policy
from claude_org_runtime.control_plane.gates import (
    ADMISSIBLE,
    CLOSE_OUTCOME_STAGES,
    GATE_OUTCOMES,
    GATE_STAGES,
    TERMINAL_RUN_STATUSES,
    AnswerBodyRequired,
    CorrectionTargetRefused,
    GateClosedRefused,
    InadmissibleTransitionRefused,
    RelayNotAckedRefused,
    UnknownGateRefused,
    advance_on_ack,
    close_gate,
    enqueue_relay,
    gates_needing_advance,
    gates_past_deadline,
    open_gate,
    record_correction,
    record_resend,
    relay_gaps,
    stalled_relays,
    sweep_subject_gone,
)
from claude_org_runtime.control_plane.migrator import create_production_control_plane

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

MINUTE = 60_000

#: ``0002_policy_seed.sql``'s revision, identified the way the detector does.
SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_production_control_plane(tmp_path / "production.sqlite3", now_ms=T0)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal row of each kind, and the world a gate needs
# --------------------------------------------------------------------------


def add_run(cp, run_id: str = "run-1", status: str = "running", at: int = T0) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
        (run_id, status, at, at),
    )
    return run_id


def add_origin_event(cp, event_id: str = "evt-escalation", run_id: str = "run-1",
                     at: int = T0) -> int:
    """The escalation event section 9.3 requires to exist before a gate opens.

    Inserted directly rather than through the spine's append: the precondition
    is that the *row* is there, and going through the fan-out would make these
    tests depend on which consumers happen to be registered.
    """

    cursor = cp.execute(
        """
        INSERT INTO event (event_id, event_type, subject_kind, subject_id, run_id,
                           producer, dedup_key, occurred_at_ms, ingested_at_ms)
        VALUES (?, 'worker_escalation_raised', 'run', ?, ?, 'worker', ?, ?, ?)
        """,
        (event_id, run_id, run_id, f"dk/{event_id}", at, at),
    )
    return int(cursor.lastrowid)


def a_gate(cp, gate_id: str = "gate-1", *, gate_type: str = "worker_escalation",
           run_id: str | None = "run-1", at: int = T0,
           deadline_at_ms: int | None = None) -> str:
    """A run, its escalation event, and a gate opened at ``received``."""

    if run_id is not None and cp.execute(
        "SELECT 1 FROM run WHERE run_id = ?", (run_id,)
    ).fetchone() is None:
        add_run(cp, run_id, at=at)
    seq = add_origin_event(cp, event_id=f"evt/{gate_id}", run_id=run_id or "run-1", at=at)
    open_gate(
        cp,
        gate_id=gate_id,
        gate_type=gate_type,
        subject_kind="run",
        subject_id=run_id or "run-1",
        rationale="the worker cannot decide whether to force-push",
        origin_event_seq=seq,
        created_at_ms=at,
        actor_kind="worker",
        actor_id="worker-7",
        options=["force-push", "abandon"],
        deadline_at_ms=deadline_at_ms,
        run_id=run_id,
    )
    return gate_id


def deliver(cp, message_id: str, at: int) -> None:
    """The outbox delivery worker's step, guarded so a re-run is a no-op."""

    cp.execute(
        "UPDATE outbox SET status = 'delivered', delivered_at_ms = ? "
        " WHERE message_id = ? AND status = 'pending'",
        (at, message_id),
    )


def ack(cp, message_id: str, at: int) -> None:
    cp.execute(
        "UPDATE outbox SET status = 'acked', acked_at_ms = ? "
        " WHERE message_id = ? AND status = 'delivered'",
        (at, message_id),
    )


def stage_of(cp, gate_id: str) -> str:
    return str(cp.execute("SELECT stage FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()[0])


def gate_row(cp, gate_id: str) -> sqlite3.Row:
    cp.row_factory = sqlite3.Row
    try:
        return cp.execute("SELECT * FROM gate WHERE gate_id = ?", (gate_id,)).fetchone()
    finally:
        cp.row_factory = None


def transitions(cp, gate_id: str, **where) -> list[tuple]:
    clauses = "".join(f" AND {column} = :{column}" for column in where)
    return cp.execute(
        "SELECT seq, transition_kind, from_stage, to_stage, body, supersedes_seq, message_id"
        f"  FROM gate_transition WHERE gate_id = :gate_id{clauses} ORDER BY seq",
        {"gate_id": gate_id, **where},
    ).fetchall()


def outbox_rows(cp, dedup_key: str) -> list[tuple]:
    return cp.execute(
        "SELECT message_id, status, retry_count FROM outbox WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchall()


def add_revision(cp, *, note: str, effective_at_ms: int) -> int:
    cursor = cp.execute(
        "INSERT INTO policy_revision (note, decided_by, effective_at_ms) "
        "VALUES (?, 'a later D- entry', ?)",
        (note, effective_at_ms),
    )
    return int(cursor.lastrowid)


# --------------------------------------------------------------------------
# section 9.5 -- the crash window the whole section exists for
# --------------------------------------------------------------------------


def relay_pipeline(cp, gate_id: str, *, base: int):
    """The four steps of the section 9.5 table, each idempotent on its own.

    Recovery is *running the same four again*, which is the claim being tested:
    a Secretary that comes back after a kill does not need to know where it
    died, because no step can produce a second message or a second advance.
    """

    def enqueue() -> str:
        return enqueue_relay(
            cp,
            gate_id=gate_id,
            to_stage="presented",
            recipient="secretary",
            payload='{"question": "force-push?"}',
            message_id=f"msg/{gate_id}/presented",
            enqueued_at_ms=base,
        )

    def deliver_step() -> None:
        deliver(cp, f"msg/{gate_id}/presented", base + 1_000)

    def ack_step() -> None:
        ack(cp, f"msg/{gate_id}/presented", base + 2_000)

    def advance_step() -> None:
        for pending in gates_needing_advance(cp):
            advance_on_ack(
                cp,
                gate_id=str(pending["gate_id"]),
                to_stage=str(pending["to_stage"]),
                actor_kind="secretary",
                actor_id="secretary-1",
                occurred_at_ms=base + 3_000,
                recorded_at_ms=base + 3_000,
            )

    return (enqueue, deliver_step, ack_step, advance_step)


@pytest.mark.parametrize(
    "killed_after",
    [
        pytest.param(0, id="killed_before_the_enqueue"),
        pytest.param(1, id="killed_between_enqueue_and_delivery"),
        pytest.param(2, id="killed_between_delivery_and_ack"),
        pytest.param(3, id="killed_between_ack_and_advance"),
    ],
)
def test_a_kill_at_any_relay_step_recovers_to_exactly_one_message_and_one_advance(
    cp, killed_after: int
) -> None:
    gate_id = a_gate(cp)
    steps = relay_pipeline(cp, gate_id, base=T0 + MINUTE)
    for step in steps[:killed_after]:
        step()

    # The kill: nothing more runs. Recovery re-runs the whole sequence, which is
    # all a restarted Secretary can do -- it cannot know which step it died on.
    for step in relay_pipeline(cp, gate_id, base=T0 + 5 * MINUTE):
        step()

    assert len(outbox_rows(cp, f"gate/{gate_id}/presented")) == 1
    advances = transitions(cp, gate_id, transition_kind="advance", to_stage="presented")
    assert len(advances) == 1
    assert stage_of(cp, gate_id) == "presented"
    assert gates_needing_advance(cp) == ()


def test_the_stage_does_not_move_until_the_relay_is_acked(cp) -> None:
    gate_id = a_gate(cp)
    message_id = enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload="{}", message_id="msg-1", enqueued_at_ms=T0 + MINUTE,
    )
    with pytest.raises(RelayNotAckedRefused):
        advance_on_ack(
            cp, gate_id=gate_id, to_stage="presented", actor_kind="secretary",
            actor_id="secretary-1", occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
        )
    deliver(cp, message_id, T0 + 2 * MINUTE)
    with pytest.raises(RelayNotAckedRefused):
        advance_on_ack(
            cp, gate_id=gate_id, to_stage="presented", actor_kind="secretary",
            actor_id="secretary-1", occurred_at_ms=T0 + 2 * MINUTE,
            recorded_at_ms=T0 + 2 * MINUTE,
        )
    assert stage_of(cp, gate_id) == "received"

    ack(cp, message_id, T0 + 3 * MINUTE)
    assert advance_on_ack(
        cp, gate_id=gate_id, to_stage="presented", actor_kind="secretary",
        actor_id="secretary-1", occurred_at_ms=T0 + 3 * MINUTE,
        recorded_at_ms=T0 + 3 * MINUTE,
    ) is True
    assert stage_of(cp, gate_id) == "presented"


def test_a_re_enqueued_relay_takes_the_message_id_already_in_force(cp) -> None:
    gate_id = a_gate(cp)
    first = enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload="{}", message_id="msg-first", enqueued_at_ms=T0 + MINUTE,
    )
    second = enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload="{}", message_id="msg-second", enqueued_at_ms=T0 + 2 * MINUTE,
    )
    assert (first, second) == ("msg-first", "msg-first")
    assert len(outbox_rows(cp, f"gate/{gate_id}/presented")) == 1


# --------------------------------------------------------------------------
# section 9.3 -- the admissible edges, as data
# --------------------------------------------------------------------------


def test_the_admissible_advance_edges_are_exactly_the_three_of_section_9_3() -> None:
    advances = {(edge.from_stage, edge.to_stage) for edge in ADMISSIBLE if edge.kind == "advance"}
    assert advances == {
        ("received", "presented"),
        ("presented", "answered"),
        ("answered", "forwarded"),
    }
    # Every non-advance edge stands still or opens; nothing else moves a stage.
    for edge in ADMISSIBLE:
        if edge.kind not in ("advance", "open"):
            assert edge.from_stage == edge.to_stage


#: Section 9.3's transition table, **actor column included**, transcribed from
#: ``docs/production-schema.md`` and not from :data:`ADMISSIBLE`.
#:
#: Transcribing the document here is the point. The constant under test is
#: itself a transcription, so the only way a transcription error shows up is a
#: second, independent copy of the source to compare it against -- and the check
#: above this one, which reads the *advance* edges out of the constant and
#: asserts a set built from the same three rows, is exactly the shape that
#: cannot catch one. It passed while the ``forwarded -> forwarded`` close
#: admitted all five actor kinds instead of ``system``, because it never looked
#: at ``kind == 'close'`` and never looked at ``actor_kinds`` at all.
#:
#: The document's wording, row by row:
#:
#: * ``-> received`` (``open``) is "worker (via system)": the worker raises it
#:   and the system may write it on the worker's behalf.
#: * the three ``advance`` rows name exactly one actor each.
#: * ``resend`` and ``correction`` are "any" at "any open stage", which is all
#:   four stages -- a forwarded gate is open until it closes.
#: * the close is **two** rows: "varies" out of ``received``/``presented``/
#:   ``answered``, and ``system`` alone out of ``forwarded``.
SECTION_9_3_ACTOR_COLUMN: dict[tuple[str | None, str, str], frozenset[str]] = {
    (None, "received", "open"): frozenset({"worker", "system"}),
    ("received", "presented", "advance"): frozenset({"secretary"}),
    ("presented", "answered", "advance"): frozenset({"human"}),
    ("answered", "forwarded", "advance"): frozenset({"secretary"}),
    **{
        (stage, stage, kind): frozenset(
            {"worker", "secretary", "human", "dispatcher_core", "system"}
        )
        for stage in GATE_STAGES
        for kind in ("resend", "correction")
    },
    **{
        (stage, stage, "close"): frozenset(
            {"worker", "secretary", "human", "dispatcher_core", "system"}
        )
        for stage in ("received", "presented", "answered")
    },
    ("forwarded", "forwarded", "close"): frozenset({"system"}),
}


def test_every_admissible_edge_carries_the_actor_column_of_section_9_3() -> None:
    """The whole table, actors and all -- not just the edges that move a stage.

    Two claims, and the second is why the assertion is a dict rather than a set
    of keys. First, ``ADMISSIBLE`` names one edge per ``(from, to, kind)``:
    :func:`~claude_org_runtime.control_plane.gates._require_actor` returns on the
    *first* match, so a duplicated key would make the second row unreachable
    data that no other test could distinguish from a typo. Second, each edge's
    actor set is the document's, which is the property that was silently wrong.
    """

    by_edge: dict[tuple[str | None, str, str], frozenset[str]] = {}
    for edge in ADMISSIBLE:
        key = (edge.from_stage, edge.to_stage, edge.kind)
        assert key not in by_edge, f"{key} appears twice; the second row is unreachable"
        by_edge[key] = edge.actor_kinds

    assert by_edge == SECTION_9_3_ACTOR_COLUMN


def test_only_the_system_closes_a_forwarded_gate(cp) -> None:
    """The narrowing above, driven through the writer rather than read off the table.

    A forwarded gate's close is the consequence of an ack, so the party to the
    gate may not assert it: letting a worker close its own gate as
    ``answered_and_forwarded`` would make the gate report the answer delivered on
    the say-so of the party that was supposed to receive it. The same close at
    ``received`` stays open to any actor, which is what makes this a property of
    the ``forwarded`` row and not a blanket rule.
    """

    forwarded = a_gate(cp, "gate-forwarded")
    bring_to(cp, forwarded, "forwarded", base=T0 + MINUTE)

    for actor_kind, actor_id in (("worker", "worker-7"), ("secretary", "secretary-1"),
                                 ("human", "ryo"), ("dispatcher_core", "core")):
        with pytest.raises(InadmissibleTransitionRefused):
            close_gate(
                cp, gate_id=forwarded, outcome="answered_and_forwarded",
                actor_kind=actor_kind, actor_id=actor_id,
                occurred_at_ms=T0 + 20 * MINUTE, recorded_at_ms=T0 + 20 * MINUTE,
            )
    assert gate_row(cp, forwarded)["outcome"] is None

    assert close_gate(
        cp, gate_id=forwarded, outcome="answered_and_forwarded", actor_kind="system",
        actor_id="reconcile", occurred_at_ms=T0 + 21 * MINUTE,
        recorded_at_ms=T0 + 21 * MINUTE,
    ) is True

    # ... and the same actor is still free to close an earlier stage, because
    # section 9.3 gives those rows "varies" and not 'system'.
    early = a_gate(cp, "gate-early", at=T0 + 30 * MINUTE)
    assert close_gate(
        cp, gate_id=early, outcome="withdrawn", actor_kind="worker", actor_id="worker-7",
        occurred_at_ms=T0 + 31 * MINUTE, recorded_at_ms=T0 + 31 * MINUTE,
    ) is True


def test_a_rewind_is_refused_and_re_asking_is_a_new_gate_linked_by_superseded_by(cp) -> None:
    gate_id = a_gate(cp)
    reach_answered(cp, gate_id, base=T0 + MINUTE, body="force-push it")

    for backwards in ("received", "presented"):
        with pytest.raises(InadmissibleTransitionRefused):
            advance_on_ack(
                cp, gate_id=gate_id, to_stage=backwards, actor_kind="secretary",
                actor_id="secretary-1", occurred_at_ms=T0 + 10 * MINUTE,
                recorded_at_ms=T0 + 10 * MINUTE,
            )
    entered = gate_row(cp, gate_id)["stage_entered_at_ms"]

    successor = a_gate(cp, "gate-2", at=T0 + 10 * MINUTE)
    assert close_gate(
        cp, gate_id=gate_id, outcome="superseded", actor_kind="secretary",
        actor_id="secretary-1", occurred_at_ms=T0 + 11 * MINUTE,
        recorded_at_ms=T0 + 11 * MINUTE, superseded_by=successor,
    ) is True

    row = gate_row(cp, gate_id)
    assert (row["stage"], row["outcome"], row["superseded_by"]) == (
        "answered", "superseded", successor,
    )
    # The aging basis the relay-gap detector reads survived the re-ask, which is
    # the reason the rewind is refused at all.
    assert row["stage_entered_at_ms"] == entered


def test_an_advance_that_skips_a_stage_is_refused(cp) -> None:
    gate_id = a_gate(cp)
    with pytest.raises(InadmissibleTransitionRefused):
        advance_on_ack(
            cp, gate_id=gate_id, to_stage="answered", actor_kind="human",
            actor_id="ryo", occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
            body="yes",
        )


def test_an_unknown_gate_is_refused_rather_than_created(cp) -> None:
    with pytest.raises(UnknownGateRefused):
        advance_on_ack(
            cp, gate_id="nope", to_stage="presented", actor_kind="secretary",
            actor_id="secretary-1", occurred_at_ms=T0, recorded_at_ms=T0,
        )


# --------------------------------------------------------------------------
# section 9.3 -- resends, corrections, and the verbatim answer
# --------------------------------------------------------------------------


def reach_presented(cp, gate_id: str, *, base: int) -> str:
    message_id = enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload="{}", message_id=f"msg/{gate_id}/presented", enqueued_at_ms=base,
    )
    deliver(cp, message_id, base + 1_000)
    ack(cp, message_id, base + 2_000)
    advance_on_ack(
        cp, gate_id=gate_id, to_stage="presented", actor_kind="secretary",
        actor_id="secretary-1", occurred_at_ms=base + 3_000, recorded_at_ms=base + 3_000,
    )
    return message_id


def reach_answered(cp, gate_id: str, *, base: int, body: str) -> int:
    reach_presented(cp, gate_id, base=base)
    advance_on_ack(
        cp, gate_id=gate_id, to_stage="answered", actor_kind="human", actor_id="ryo",
        occurred_at_ms=base + 4_000, recorded_at_ms=base + 5_000, body=body,
    )
    return int(cp.execute(
        "SELECT stage_seq FROM gate WHERE gate_id = ?", (gate_id,)
    ).fetchone()[0])


def test_a_resend_does_not_move_the_stage(cp) -> None:
    gate_id = a_gate(cp)
    reach_presented(cp, gate_id, base=T0 + MINUTE)
    before = gate_row(cp, gate_id)

    seq = record_resend(
        cp, gate_id=gate_id, actor_kind="secretary", actor_id="secretary-1",
        occurred_at_ms=T0 + 5 * MINUTE, recorded_at_ms=T0 + 5 * MINUTE,
        message_id=f"msg/{gate_id}/presented",
    )
    after = gate_row(cp, gate_id)

    assert (after["stage"], after["stage_seq"]) == (before["stage"], before["stage_seq"])
    assert after["stage_entered_at_ms"] == before["stage_entered_at_ms"]
    resend = transitions(cp, gate_id, transition_kind="resend")
    assert [row[0] for row in resend] == [seq]
    assert resend[0][2] == resend[0][3] == "presented"


def test_a_correction_carries_supersedes_seq_and_both_texts_survive(cp) -> None:
    gate_id = a_gate(cp)
    answered_seq = reach_answered(cp, gate_id, base=T0 + MINUTE, body="force-push it")

    record_correction(
        cp, gate_id=gate_id, supersedes_seq=answered_seq, body="do NOT force-push it",
        actor_kind="human", actor_id="ryo", occurred_at_ms=T0 + 9 * MINUTE,
        recorded_at_ms=T0 + 9 * MINUTE,
    )

    bodies = [row[4] for row in transitions(cp, gate_id) if row[4] is not None]
    assert bodies == ["force-push it", "do NOT force-push it"]
    correction = transitions(cp, gate_id, transition_kind="correction")[0]
    assert correction[5] == answered_seq
    assert stage_of(cp, gate_id) == "answered"


def test_a_correction_may_not_name_another_gates_transition(cp) -> None:
    first = a_gate(cp, "gate-1")
    other = a_gate(cp, "gate-2")
    other_seq = int(cp.execute(
        "SELECT stage_seq FROM gate WHERE gate_id = ?", (other,)
    ).fetchone()[0])
    with pytest.raises(CorrectionTargetRefused):
        record_correction(
            cp, gate_id=first, supersedes_seq=other_seq, body="not mine to correct",
            actor_kind="human", actor_id="ryo", occurred_at_ms=T0 + MINUTE,
            recorded_at_ms=T0 + MINUTE,
        )


def test_the_verbatim_answer_is_neither_paraphrased_nor_overwritten(cp) -> None:
    gate_id = a_gate(cp)
    verbatim = "  force-push, but ONLY after the CI run at 3f2a1b0 is green.\n(-- ryo)  "
    answered_seq = reach_answered(cp, gate_id, base=T0 + MINUTE, body=verbatim)

    stored = cp.execute(
        "SELECT body FROM gate_transition WHERE seq = ?", (answered_seq,)
    ).fetchone()[0]
    assert stored == verbatim

    with pytest.raises(sqlite3.IntegrityError):
        cp.execute(
            "UPDATE gate_transition SET body = 'force push ok' WHERE seq = ?",
            (answered_seq,),
        )
    assert cp.execute(
        "SELECT body FROM gate_transition WHERE seq = ?", (answered_seq,)
    ).fetchone()[0] == verbatim


def test_an_advance_to_answered_without_a_body_is_refused(cp) -> None:
    gate_id = a_gate(cp)
    reach_presented(cp, gate_id, base=T0 + MINUTE)
    with pytest.raises(AnswerBodyRequired):
        advance_on_ack(
            cp, gate_id=gate_id, to_stage="answered", actor_kind="human", actor_id="ryo",
            occurred_at_ms=T0 + 6 * MINUTE, recorded_at_ms=T0 + 6 * MINUTE,
        )
    assert stage_of(cp, gate_id) == "presented"


# --------------------------------------------------------------------------
# section 9.4 -- the terminal taxonomy
# --------------------------------------------------------------------------


def bring_to(cp, gate_id: str, stage: str, *, base: int) -> None:
    if stage == "received":
        return
    if stage == "presented":
        reach_presented(cp, gate_id, base=base)
        return
    reach_answered(cp, gate_id, base=base, body="an answer")
    if stage == "answered":
        return
    message_id = enqueue_relay(
        cp, gate_id=gate_id, to_stage="forwarded", recipient="worker-7",
        payload="{}", message_id=f"msg/{gate_id}/forwarded", enqueued_at_ms=base + 6_000,
    )
    deliver(cp, message_id, base + 7_000)
    ack(cp, message_id, base + 8_000)
    advance_on_ack(
        cp, gate_id=gate_id, to_stage="forwarded", actor_kind="secretary",
        actor_id="secretary-1", occurred_at_ms=base + 9_000, recorded_at_ms=base + 9_000,
    )


@pytest.mark.parametrize("outcome", GATE_OUTCOMES)
def test_every_terminal_outcome_is_reachable_and_a_closed_gate_keeps_it(cp, outcome: str) -> None:
    stage = sorted(CLOSE_OUTCOME_STAGES[outcome])[0]
    gate_id = a_gate(cp, f"gate-{outcome}")
    bring_to(cp, gate_id, stage, base=T0 + MINUTE)
    successor = a_gate(cp, "gate-successor", at=T0 + 20 * MINUTE) if outcome == "superseded" else None

    assert close_gate(
        cp, gate_id=gate_id, outcome=outcome, actor_kind="system", actor_id="reconcile",
        occurred_at_ms=T0 + 30 * MINUTE, recorded_at_ms=T0 + 30 * MINUTE,
        superseded_by=successor,
    ) is True

    row = gate_row(cp, gate_id)
    assert (row["outcome"], row["closed_at_ms"], row["stage"]) == (
        outcome, T0 + 30 * MINUTE, stage,
    )
    # A second close with the same outcome is the reconcile pass running again.
    assert close_gate(
        cp, gate_id=gate_id, outcome=outcome, actor_kind="system", actor_id="reconcile",
        occurred_at_ms=T0 + 40 * MINUTE, recorded_at_ms=T0 + 40 * MINUTE,
        superseded_by=successor,
    ) is False
    # A different outcome is refused, and so is an UPDATE that goes around us.
    other = "expired" if outcome != "expired" else "withdrawn"
    with pytest.raises(GateClosedRefused):
        close_gate(
            cp, gate_id=gate_id, outcome=other, actor_kind="worker",
            actor_id="worker-7", occurred_at_ms=T0 + 41 * MINUTE,
            recorded_at_ms=T0 + 41 * MINUTE,
        )
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute("UPDATE gate SET outcome = ? WHERE gate_id = ?", (other, gate_id))
    assert gate_row(cp, gate_id)["outcome"] == outcome


def test_an_outcome_is_refused_from_a_stage_it_is_not_reachable_from(cp) -> None:
    gate_id = a_gate(cp)
    # 'unanswerable' is the human declining, and there is no human at 'received'.
    with pytest.raises(InadmissibleTransitionRefused):
        close_gate(
            cp, gate_id=gate_id, outcome="unanswerable", actor_kind="human", actor_id="ryo",
            occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
        )


def test_closing_a_gate_puts_the_closure_on_the_spine(cp) -> None:
    gate_id = a_gate(cp)
    close_gate(
        cp, gate_id=gate_id, outcome="withdrawn", actor_kind="worker", actor_id="worker-7",
        occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
    )
    row = cp.execute(
        "SELECT event_type, subject_kind, subject_id FROM event WHERE dedup_key = ?",
        (f"gate_closed/{gate_id}",),
    ).fetchone()
    assert row == ("gate_closed", "gate", gate_id)


def test_an_expiry_is_announced_as_its_own_event_type(cp) -> None:
    gate_id = a_gate(cp, deadline_at_ms=T0 + 10 * MINUTE)
    reach_presented(cp, gate_id, base=T0 + MINUTE)
    close_gate(
        cp, gate_id=gate_id, outcome="expired", actor_kind="system", actor_id="reconcile",
        occurred_at_ms=T0 + 11 * MINUTE, recorded_at_ms=T0 + 11 * MINUTE,
    )
    assert cp.execute(
        "SELECT event_type FROM event WHERE dedup_key = ?", (f"gate_closed/{gate_id}",)
    ).fetchone()[0] == "gate_expired"


def test_subject_gone_is_produced_by_the_sweep_against_a_terminal_run(cp) -> None:
    add_run(cp, "run-live", status="running")
    add_run(cp, "run-dead", status="running")
    live = a_gate(cp, "gate-live", run_id="run-live")
    dead = a_gate(cp, "gate-dead", run_id="run-dead")
    reach_presented(cp, dead, base=T0 + MINUTE)

    assert sweep_subject_gone(cp, now_ms=T0 + 5 * MINUTE) == ()

    cp.execute(
        "UPDATE run SET status = 'cancelled', updated_at_ms = ? WHERE run_id = 'run-dead'",
        (T0 + 6 * MINUTE,),
    )
    assert sweep_subject_gone(cp, now_ms=T0 + 7 * MINUTE) == (dead,)

    assert gate_row(cp, dead)["outcome"] == "subject_gone"
    assert gate_row(cp, live)["outcome"] is None
    # The pass is restartable: a second sweep closes nothing twice.
    assert sweep_subject_gone(cp, now_ms=T0 + 8 * MINUTE) == ()
    # The closed gate stops being aged; the live one is still the detector's.
    assert [gap["gate_id"] for gap in relay_gaps(cp, now_ms=T0 + 600 * MINUTE)] == [live]


@pytest.mark.parametrize("status", TERMINAL_RUN_STATUSES)
def test_the_sweep_finds_the_subject_run_through_subject_id_at_every_terminal_status(
    cp, status: str
) -> None:
    """``run_id`` is nullable, so the ``subject_kind='run'`` join is not a duplicate.

    A gate that names its run only as its *subject* is the same situation as one
    that names it in ``run_id`` -- there is nobody left to forward to either way
    -- and a sweep that read ``gate.run_id`` alone would leave the first kind
    open forever, which is precisely the permanent-open-row failure
    ``subject_gone`` exists to end. ``cancelled`` is the case the neighbouring
    test drives; the parametrisation is here because all three of
    :data:`TERMINAL_RUN_STATUSES` are absorbing under
    ``run_status_is_forward_only`` and the sweep must not privilege one of them.
    """

    add_run(cp, "run-1", status="running")
    gate_id = a_gate(cp, "gate-subject-only", run_id=None)
    # The gate points at the run only through the subject, which is the branch
    # under test: gate.run_id is NULL, so the run_id half of the join matches
    # nothing and only the subject_kind='run' half can find the run.
    row = gate_row(cp, gate_id)
    assert (row["run_id"], row["subject_kind"], row["subject_id"]) == (None, "run", "run-1")

    assert sweep_subject_gone(cp, now_ms=T0 + 5 * MINUTE) == ()

    cp.execute(
        "UPDATE run SET status = ?, updated_at_ms = ? WHERE run_id = 'run-1'",
        (status, T0 + 6 * MINUTE),
    )
    assert sweep_subject_gone(cp, now_ms=T0 + 7 * MINUTE) == (gate_id,)
    assert gate_row(cp, gate_id)["outcome"] == "subject_gone"


# --------------------------------------------------------------------------
# section 9.6 -- the two detectors
# --------------------------------------------------------------------------


def test_a_gate_left_at_received_is_a_relay_gap_past_its_stage_tolerance(cp) -> None:
    gate_id = a_gate(cp)
    tolerance = 3 * MINUTE  # policy_gate_stage_tolerance, worker_escalation/received

    assert relay_gaps(cp, now_ms=T0 + tolerance) == ()
    gaps = relay_gaps(cp, now_ms=T0 + tolerance + 1)
    assert [gap["gate_id"] for gap in gaps] == [gate_id]
    assert gaps[0]["age_ms"] == tolerance + 1


def test_a_long_presented_gate_is_not_a_relay_gap_and_the_opt_out_is_data(cp) -> None:
    gate_id = a_gate(cp, deadline_at_ms=T0 + 30 * MINUTE)
    reach_presented(cp, gate_id, base=T0 + MINUTE)

    # Hours at 'presented': a slow human is not a gap, and the detector says so
    # because policy_gate_stage_tolerance stores NULL for the stage.
    assert relay_gaps(cp, now_ms=T0 + 600 * MINUTE) == ()

    # What governs this leg instead is the gate's own deadline.
    assert gates_past_deadline(cp, now_ms=T0 + 30 * MINUTE - 1) == ()
    overdue = gates_past_deadline(cp, now_ms=T0 + 31 * MINUTE)
    assert [row["gate_id"] for row in overdue] == [gate_id]
    close_gate(
        cp, gate_id=gate_id, outcome="expired", actor_kind="system", actor_id="reconcile",
        occurred_at_ms=T0 + 31 * MINUTE, recorded_at_ms=T0 + 31 * MINUTE,
    )
    assert gate_row(cp, gate_id)["outcome"] == "expired"

    # And the opt-out really is data, not a branch: a later revision that gives
    # 'presented' a tolerance makes the same query report the same shape of gate
    # with no code change at all.
    second = a_gate(cp, "gate-2", at=T0 + 40 * MINUTE)
    reach_presented(cp, second, base=T0 + 41 * MINUTE)
    revision = add_revision(cp, note="presented now ages", effective_at_ms=T0 + 50 * MINUTE)
    cp.execute(
        "INSERT INTO policy_gate_stage_tolerance (revision_id, gate_type, stage, tolerance_ms)"
        " VALUES (?, 'worker_escalation', 'presented', ?)",
        (revision, 5 * MINUTE),
    )
    gaps = relay_gaps(cp, now_ms=T0 + 60 * MINUTE)
    assert [gap["gate_id"] for gap in gaps] == [second]


@pytest.mark.parametrize("gate_type", ("plan_approval", "risk_approval"))
def test_an_unpoliced_gate_type_is_silently_never_aged(cp, gate_type: str) -> None:
    """A stated known hole, pinned so it is visible in the suite and not only in prose.

    ``0002_policy_seed.sql`` seeds no tolerance rows for ``plan_approval`` or
    ``risk_approval`` on purpose -- ``time-base-policy.md`` decides no numbers
    for them, and inventing some in a migration is the policy-in-code that
    ``D-0031`` forbids. The section 9.6 query joins
    ``policy_gate_stage_tolerance`` inline, so such a gate simply does not match
    at any age, and nothing anywhere says it is unpoliced.

    The contrast is the point, and it is asserted against the code rather than
    described: :func:`policy.gate_stage_tolerance` **refuses** the same
    ``(gate_type, stage)`` with :class:`policy.PolicyRowMissing`, because a
    caller asking for a number it does not have must not be handed silence. The
    detector has no equivalent -- there is no ``gate_type_unpoliced`` incident
    class the way the watcher side has ``watcher_scope_uncovered`` -- and the
    design's own query has this shape, so closing the hole means deciding a new
    incident class, not editing ``relay_gaps``.

    When that decision lands, this test is the one that fails, and its failure
    is the reminder that a hole was being carried deliberately.
    """

    policed = a_gate(cp, "gate-policed")
    unpoliced = a_gate(cp, "gate-unpoliced", gate_type=gate_type, run_id="run-2")

    aged = [gap["gate_id"] for gap in relay_gaps(cp, now_ms=T0 + 600 * MINUTE)]
    assert aged == [policed], "the unpoliced gate is not aged -- this is the hole"
    assert gate_row(cp, unpoliced)["closed_at_ms"] is None

    revision = policy.effective_revision_id(cp, now_ms=T0 + 600 * MINUTE)
    with pytest.raises(policy.PolicyRowMissing):
        policy.gate_stage_tolerance(
            cp, revision_id=revision, gate_type=gate_type, stage="received"
        )


def test_the_detector_binds_one_revision_and_emits_one_row_per_gate(cp) -> None:
    gate_id = a_gate(cp)
    revision = add_revision(cp, note="tighter received", effective_at_ms=T0 + MINUTE)
    cp.execute(
        "INSERT INTO policy_gate_stage_tolerance (revision_id, gate_type, stage, tolerance_ms)"
        " VALUES (?, 'worker_escalation', 'received', ?)",
        (revision, MINUTE),
    )
    gaps = relay_gaps(cp, now_ms=T0 + 10 * MINUTE)
    assert [gap["gate_id"] for gap in gaps] == [gate_id]
    # The newer revision is the one in force, so its tolerance is the one aged
    # against -- not the seed's, and not both.
    assert gaps[0]["age_ms"] == 10 * MINUTE


def test_a_stalled_relay_is_detected_by_its_own_predicate(cp) -> None:
    gate_id = a_gate(cp)
    enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload="{}", message_id="msg-1", enqueued_at_ms=T0,
    )
    now = T0 + 2 * MINUTE + 10_000  # past the 2 min delivery tolerance, inside the 3 min stage one

    assert relay_gaps(cp, now_ms=now) == ()
    stalled = stalled_relays(cp, now_ms=now, tolerance_ms=2 * MINUTE)
    assert [(row["gate_id"], row["to_stage"]) for row in stalled] == [(gate_id, "presented")]

    # Acking it clears the delivery stall without anything else changing: the
    # two conditions are separable only because the advance is ack-gated.
    deliver(cp, "msg-1", now)
    ack(cp, "msg-1", now + 1_000)
    assert stalled_relays(cp, now_ms=now + 2_000, tolerance_ms=2 * MINUTE) == ()
    assert [row["gate_id"] for row in gates_needing_advance(cp)] == [gate_id]


def test_a_closed_gate_is_neither_aged_nor_relayed_to(cp) -> None:
    gate_id = a_gate(cp)
    close_gate(
        cp, gate_id=gate_id, outcome="withdrawn", actor_kind="worker", actor_id="worker-7",
        occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
    )
    assert relay_gaps(cp, now_ms=T0 + 600 * MINUTE) == ()
    assert gates_past_deadline(cp, now_ms=T0 + 600 * MINUTE) == ()
    with pytest.raises(GateClosedRefused):
        enqueue_relay(
            cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
            payload="{}", message_id="msg-1", enqueued_at_ms=T0 + 2 * MINUTE,
        )


# --------------------------------------------------------------------------
# section 9.2 -- the projection
# --------------------------------------------------------------------------


def test_a_gate_opens_at_received_pointing_at_its_own_open_transition(cp) -> None:
    gate_id = a_gate(cp)
    row = gate_row(cp, gate_id)
    opening = transitions(cp, gate_id)
    assert len(opening) == 1
    assert (opening[0][1], opening[0][2], opening[0][3]) == ("open", None, "received")
    assert (row["stage"], row["stage_seq"]) == ("received", opening[0][0])
    assert row["stage_entered_at_ms"] == row["created_at_ms"]
    assert row["options"] == '["force-push", "abandon"]'


def test_the_projection_may_not_be_pointed_at_another_gates_transition(cp) -> None:
    first = a_gate(cp, "gate-1")
    other = a_gate(cp, "gate-2")
    other_seq = int(gate_row(cp, other)["stage_seq"])
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute(
            "UPDATE gate SET stage = 'received', stage_seq = ? WHERE gate_id = ?",
            (other_seq, first),
        )


def test_only_a_named_stage_is_relayed(cp) -> None:
    gate_id = a_gate(cp)
    for stage in GATE_STAGES:
        if stage in ("presented", "forwarded"):
            continue
        with pytest.raises(ValueError):
            enqueue_relay(
                cp, gate_id=gate_id, to_stage=stage, recipient="secretary",
                payload="{}", message_id=f"msg-{stage}", enqueued_at_ms=T0,
            )


def test_closing_a_gate_retires_its_undelivered_relay(cp) -> None:
    """A close retires the message nobody is waiting for -- in the same commit.

    The inverse of the defect this replaced. Closure moves every not-yet-acked
    relay of the gate to ``cancelled`` (``0003_outbox_cancelled_status.sql``),
    so a delivery worker reading the outbox is no longer told to present a
    withdrawn question, and :func:`stalled_relays` stops naming the relay
    instead of aging it without bound -- the "alarms forever" failure the
    section 9.4 ``subject_gone`` outcome exists to end, which had reappeared one
    table over.

    Cancellation is terminal but not an erasure, so the delivery evidence is
    asserted to survive it: the row still says it was delivered, and still says
    how many attempts it took.
    """

    gate_id = a_gate(cp)
    enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload='{"question": "force-push?"}', message_id="msg-undelivered",
        enqueued_at_ms=T0,
    )
    # Sent, and not answered: 'delivered' means sent, not acked, so this is the
    # case a cancellation must still cover -- the question was put in front of
    # a human and became moot while they were reading it.
    deliver(cp, "msg-undelivered", T0 + 1_000)
    cp.execute(
        "UPDATE outbox SET retry_count = 2 WHERE message_id = 'msg-undelivered'"
    )

    assert stalled_relays(cp, now_ms=T0 + 10 * MINUTE, tolerance_ms=2 * MINUTE)

    close_gate(
        cp, gate_id=gate_id, outcome="withdrawn", actor_kind="worker",
        actor_id="worker-7", occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
    )

    assert outbox_rows(cp, f"gate/{gate_id}/presented") == [
        ("msg-undelivered", "cancelled", 2)
    ]

    # However far the clock is wound on, the retired relay is not named again.
    for now in (T0 + 10 * MINUTE, T0 + 600 * MINUTE, T0 + 60_000 * MINUTE):
        assert stalled_relays(cp, now_ms=now, tolerance_ms=2 * MINUTE) == ()

    # Terminal, not erased: what the row recorded about the delivery is intact.
    delivered_at_ms, retry_count = cp.execute(
        "SELECT delivered_at_ms, retry_count FROM outbox WHERE message_id = ?",
        ("msg-undelivered",),
    ).fetchone()
    assert (delivered_at_ms, retry_count) == (T0 + 1_000, 2)


def test_closing_a_gate_leaves_an_acked_relay_alone(cp) -> None:
    """A gate that closed because it was answered keeps its answered relay.

    The ack is what section 9.5 justifies the stage advance by, so rewriting the
    row that carries it would delete the evidence for a decision that really was
    taken. ``cancelled`` is for a message nobody is waiting for; an acked one
    was already waited for and arrived.
    """

    gate_id = a_gate(cp)
    enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload="{}", message_id="msg-answered", enqueued_at_ms=T0,
    )
    deliver(cp, "msg-answered", T0 + 1_000)
    ack(cp, "msg-answered", T0 + 2_000)
    advance_on_ack(
        cp, gate_id=gate_id, to_stage="presented", actor_kind="secretary",
        actor_id="secretary-1", occurred_at_ms=T0 + 2_000, recorded_at_ms=T0 + 2_000,
    )

    close_gate(
        cp, gate_id=gate_id, outcome="withdrawn", actor_kind="worker",
        actor_id="worker-7", occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
    )

    assert outbox_rows(cp, f"gate/{gate_id}/presented") == [
        ("msg-answered", "acked", 0)
    ]
    assert cp.execute(
        "SELECT acked_at_ms FROM outbox WHERE message_id = 'msg-answered'"
    ).fetchone() == (T0 + 2_000,)


def test_a_second_close_sweep_over_a_closed_gate_stays_a_no_op(cp) -> None:
    """Re-running the sweep must not trip the trigger on the cancelled row.

    ``cancelled`` is terminal, so a second attempt to cancel the same row would
    be a step out of a terminal status and an ``IntegrityError``. close_gate's
    own idempotence (returning ``False`` for a re-close with the same outcome)
    is what keeps that unreachable, and this pins it -- a reconcile sweep runs
    again every period, over gates it closed last time.
    """

    gate_id = a_gate(cp)
    enqueue_relay(
        cp, gate_id=gate_id, to_stage="presented", recipient="secretary",
        payload="{}", message_id="msg-twice", enqueued_at_ms=T0,
    )
    kwargs = dict(
        gate_id=gate_id, outcome="withdrawn", actor_kind="worker",
        actor_id="worker-7", occurred_at_ms=T0 + MINUTE, recorded_at_ms=T0 + MINUTE,
    )
    assert close_gate(cp, **kwargs) is True
    assert close_gate(cp, **kwargs) is False
    assert outbox_rows(cp, f"gate/{gate_id}/presented") == [
        ("msg-twice", "cancelled", 0)
    ]


def test_a_losing_concurrent_close_is_refused_instead_of_told_its_outcome_landed(
    cp, monkeypatch
) -> None:
    """The loser of a race for the close must not be handed the winner's outcome.

    ``close_gate`` reads the gate *outside* the append's transaction, so two
    callers with different outcomes can both pass that read; the winner commits
    and the loser then collides on ``gate_closed/<gate_id>`` and gets a
    duplicate back. Reporting that as the ordinary idempotent ``False`` would
    tell the loser its ``expired`` close was already done while section 9.4's
    taxonomy actually records ``withdrawn`` -- a projection claiming something
    the history does not say, which is the one thing the ledger is for.

    The race is driven deterministically rather than with threads: the winner
    commits from inside the append seam, which is exactly the window between
    the loser's pre-check and its append.
    """

    gate_id = a_gate(cp)
    reach_presented(cp, gate_id, base=T0 + MINUTE)

    real_append = gates.append_event
    winner_committed: list[bool] = []

    def append_after_the_winner_commits(connection, **kwargs):
        if not winner_committed:
            winner_committed.append(True)
            assert close_gate(
                cp, gate_id=gate_id, outcome="withdrawn", actor_kind="worker",
                actor_id="worker-7", occurred_at_ms=T0 + 5 * MINUTE,
                recorded_at_ms=T0 + 5 * MINUTE,
            ) is True
        return real_append(connection, **kwargs)

    monkeypatch.setattr(gates, "append_event", append_after_the_winner_commits)

    with pytest.raises(GateClosedRefused):
        close_gate(
            cp, gate_id=gate_id, outcome="expired", actor_kind="system",
            actor_id="reconcile", occurred_at_ms=T0 + 6 * MINUTE,
            recorded_at_ms=T0 + 6 * MINUTE,
        )
    assert winner_committed == [True]
    row = gate_row(cp, gate_id)
    assert (row["outcome"], row["closed_at_ms"]) == ("withdrawn", T0 + 5 * MINUTE)


def test_a_concurrent_close_with_the_same_outcome_stays_the_idempotent_no_op(
    cp, monkeypatch
) -> None:
    """Losing the race to an *identical* close is still "already done", not a refusal.

    The counterpart to the test above: the duplicate path must distinguish
    "already done, identically" from "already done, differently", and collapsing
    both into a refusal would break the reconcile sweep, whose second pass over
    a gate it closed last time is the ordinary case and not an incident.
    """

    gate_id = a_gate(cp)
    reach_presented(cp, gate_id, base=T0 + MINUTE)

    real_append = gates.append_event
    winner_committed: list[bool] = []

    def append_after_the_winner_commits(connection, **kwargs):
        if not winner_committed:
            winner_committed.append(True)
            assert close_gate(
                cp, gate_id=gate_id, outcome="expired", actor_kind="system",
                actor_id="reconcile", occurred_at_ms=T0 + 5 * MINUTE,
                recorded_at_ms=T0 + 5 * MINUTE,
            ) is True
        return real_append(connection, **kwargs)

    monkeypatch.setattr(gates, "append_event", append_after_the_winner_commits)

    assert close_gate(
        cp, gate_id=gate_id, outcome="expired", actor_kind="system",
        actor_id="reconcile", occurred_at_ms=T0 + 6 * MINUTE,
        recorded_at_ms=T0 + 6 * MINUTE,
    ) is False
    row = gate_row(cp, gate_id)
    assert (row["outcome"], row["closed_at_ms"]) == ("expired", T0 + 5 * MINUTE)


def test_a_closure_identity_on_the_spine_without_a_closure_is_refused(cp) -> None:
    """The dedup key alone is never taken as evidence that the gate closed.

    ``close_gate`` writes the closure as the append's ``side_effect``, so the
    two commit together and this state cannot arise from this module. It is
    asserted because the duplicate path's re-read is what makes that true: an
    outside writer that took the identity must not be able to make a later
    close report success for a closure that is not in the table.
    """

    gate_id = a_gate(cp)
    cp.execute(
        """
        INSERT INTO event (event_id, event_type, subject_kind, subject_id, run_id,
                           producer, dedup_key, occurred_at_ms, ingested_at_ms)
        VALUES (?, 'gate_closed', 'gate', ?, 'run-1', 'dispatcher_core', ?, ?, ?)
        """,
        (f"gate_closed/{gate_id}", gate_id, f"gate_closed/{gate_id}",
         T0 + MINUTE, T0 + MINUTE),
    )
    cp.commit()

    with pytest.raises(GateClosedRefused):
        close_gate(
            cp, gate_id=gate_id, outcome="withdrawn", actor_kind="worker",
            actor_id="worker-7", occurred_at_ms=T0 + 2 * MINUTE,
            recorded_at_ms=T0 + 2 * MINUTE,
        )
    assert gate_row(cp, gate_id)["closed_at_ms"] is None
