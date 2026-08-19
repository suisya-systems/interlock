"""S5 -- the spike SQLite schema slice.

The tests are the durable half of this issue (D-0026): the schema they exercise
may be thrown away, and they are written so that whatever replaces it still has
to answer the same questions. Four of the five acceptance criteria of Issue
``#12`` are properties rather than behaviours -- the marking is in the file, no
``Q-0001`` or ``Q-0002`` answer is encoded, state is reconstructable by query
alone, corrupt state is refused -- so each is asserted against the artifact
itself rather than described in prose.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import claude_org_runtime
from claude_org_runtime.control_plane import schema as s5
from claude_org_runtime.control_plane.schema import (
    APPLICATION_ID,
    SCHEMA_REVISION,
    SPIKE_MARKING,
    SPIKE_SCHEMA_PATH,
    STATE_TABLES,
    ControlPlaneRefusal,
    CorruptStateRefused,
    MissingStateRefused,
    create_control_plane,
    load_schema_sql,
    open_control_plane,
    reconstruct,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "control-plane.sqlite3"


@pytest.fixture
def cp(db_path: Path):
    connection = create_control_plane(db_path)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal row of each kind
# --------------------------------------------------------------------------


def add_run(cp, run_id: str = "run-1", status: str = "running", at: int = T0) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
        (run_id, status, at, at),
    )
    return run_id


def add_session(cp, session_id: str = "sess-1", run_id: str = "run-1", at: int = T0, **kwargs):
    row = {
        "provider": "stub",
        "observation": "observed",
        "provider_state": "running",
        "observation_reason": None,
        "released_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO session (session_id, run_id, provider, observation, provider_state,
                             observation_reason, bound_at_ms, released_at_ms)
        VALUES (:session_id, :run_id, :provider, :observation, :provider_state,
                :observation_reason, :bound_at_ms, :released_at_ms)
        """,
        {"session_id": session_id, "run_id": run_id, "bound_at_ms": at, **row},
    )
    return session_id


def add_lease(cp, resource: str = "run-1", holder: str = "holder-a", epoch: int = 1, at: int = T0,
              ttl_ms: int = 30_000):
    cp.execute(
        "INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)"
        " VALUES (?, ?, ?, ?, ?)",
        (resource, holder, epoch, at, at + ttl_ms),
    )


def add_outbox(cp, message_id: str = "msg-1", dedup_key: str = "dk-1", at: int = T0, **kwargs):
    row = {
        "run_id": "run-1",
        "recipient": "secretary",
        "payload": "{}",
        "status": "pending",
        "retry_count": 0,
        "writer_epoch": 1,
        "delivered_at_ms": None,
        "acked_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO outbox (message_id, run_id, recipient, payload, dedup_key, status,
                            retry_count, writer_epoch, enqueued_at_ms, delivered_at_ms,
                            acked_at_ms)
        VALUES (:message_id, :run_id, :recipient, :payload, :dedup_key, :status,
                :retry_count, :writer_epoch, :enqueued_at_ms, :delivered_at_ms, :acked_at_ms)
        """,
        {"message_id": message_id, "dedup_key": dedup_key, "enqueued_at_ms": at, **row},
    )
    return message_id


def add_incident(cp, incident_id: str = "inc-1", dedup_key: str = "dk-1", at: int = T0, **kwargs):
    row = {
        "run_id": "run-1",
        "session_id": None,
        "fact_state": "NO_ACTIVITY_EVIDENCE",
        "detector_version": "d1",
        "retry_count": 0,
        "known_pattern": None,
        "elapsed_ms": None,
        "previous_assessment": None,
        "previous_action_id": None,
        "related_incident_id": None,
        "resolved_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO incident (incident_id, run_id, session_id, fact_state, detector_version,
                              dedup_key, retry_count, known_pattern, elapsed_ms,
                              previous_assessment, previous_action_id, related_incident_id,
                              created_at_ms, updated_at_ms, resolved_at_ms)
        VALUES (:incident_id, :run_id, :session_id, :fact_state, :detector_version, :dedup_key,
                :retry_count, :known_pattern, :elapsed_ms, :previous_assessment,
                :previous_action_id, :related_incident_id, :created_at_ms, :updated_at_ms,
                :resolved_at_ms)
        """,
        {
            "incident_id": incident_id,
            "dedup_key": dedup_key,
            "created_at_ms": at,
            "updated_at_ms": at,
            **row,
        },
    )
    return incident_id


def add_action(cp, action_id: str = "act-1", idempotency_key: str = "ik-1", at: int = T0, **kwargs):
    row = {
        "run_id": "run-1",
        "incident_id": None,
        "kind": "notify",
        "exactly_once_mechanism": "destination_idempotency_key",
        "status": "pending",
        "refusal_reason": None,
        "result": None,
        "writer_epoch": 1,
        "applied_at_ms": None,
    }
    row.update(kwargs)
    cp.execute(
        """
        INSERT INTO action (action_id, run_id, incident_id, kind, idempotency_key,
                            exactly_once_mechanism, status, refusal_reason, result,
                            writer_epoch, created_at_ms, applied_at_ms)
        VALUES (:action_id, :run_id, :incident_id, :kind, :idempotency_key,
                :exactly_once_mechanism, :status, :refusal_reason, :result, :writer_epoch,
                :created_at_ms, :applied_at_ms)
        """,
        {
            "action_id": action_id,
            "idempotency_key": idempotency_key,
            "created_at_ms": at,
            **row,
        },
    )
    return action_id


def executable_ddl() -> str:
    """The DDL with its comments stripped -- what SQLite actually sees.

    Several assertions below are about what the schema *encodes*, and the
    comments deliberately discuss the very things the schema must not encode
    (writer assignment, collapse semantics). Scanning the raw text would fail on
    the explanation of why the thing is absent.
    """

    return "\n".join(re.sub(r"--.*$", "", line) for line in load_schema_sql().splitlines())


# --------------------------------------------------------------------------
# criterion 1 -- the marking is in the schema file itself (D-0026)
# --------------------------------------------------------------------------


def test_the_schema_file_itself_carries_the_spike_marking():
    text = SPIKE_SCHEMA_PATH.read_text(encoding="utf-8")

    assert SPIKE_MARKING in text
    assert "no migration path" in text.lower()
    assert "D-0026" in text
    # It has to be visible without scrolling: the mitigation is that a reader
    # who opens the file sees it, not that it exists somewhere inside.
    assert SPIKE_MARKING in "\n".join(text.splitlines()[:12])


def test_the_marking_says_q_0001_is_not_answered_here():
    text = SPIKE_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "Q-0001" in text
    assert "throwaway" in text.lower()


def test_the_ddl_is_refused_if_the_marking_is_removed(tmp_path, monkeypatch):
    stripped = tmp_path / "spike_schema.sql"
    stripped.write_text(load_schema_sql().replace(SPIKE_MARKING, ""), encoding="utf-8")
    monkeypatch.setattr(s5, "SPIKE_SCHEMA_PATH", stripped)

    with pytest.raises(ControlPlaneRefusal, match="spike marking"):
        load_schema_sql()
    with pytest.raises(ControlPlaneRefusal, match="spike marking"):
        create_control_plane(tmp_path / "unmarked.sqlite3")
    assert not (tmp_path / "unmarked.sqlite3").exists()


# --------------------------------------------------------------------------
# the slice -- six tables and nothing else
# --------------------------------------------------------------------------


def test_the_slice_is_exactly_the_six_named_tables(cp):
    tables = {
        name
        for (name,) in cp.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == set(STATE_TABLES)


def test_every_table_is_reachable_from_a_reconstruction_query():
    # D-0001: nothing is stored that no recovery query can read back.
    sql = " ".join(s5.RECONSTRUCTION_QUERIES.values())
    for table in STATE_TABLES:
        assert re.search(rf"\bFROM {table}\b", sql), table


# --------------------------------------------------------------------------
# criterion 3 -- dedup key and retry count on incidents (D-0007)
# --------------------------------------------------------------------------


def test_incident_dedup_key_and_retry_count_are_present_and_non_nullable(cp):
    columns = {row[1]: row for row in cp.execute("PRAGMA table_info(incident)")}

    assert "dedup_key" in columns and "retry_count" in columns
    assert columns["dedup_key"][3] == 1, "dedup_key must be NOT NULL (D-0007)"
    assert columns["retry_count"][3] == 1, "retry_count must be NOT NULL (D-0007)"


def test_an_incident_without_a_dedup_key_is_refused(cp):
    add_run(cp)
    with pytest.raises(sqlite3.IntegrityError):
        add_incident(cp, dedup_key=None)
    with pytest.raises(sqlite3.IntegrityError):
        add_incident(cp, incident_id="inc-2", dedup_key="")


def test_an_incident_retry_count_is_never_null_and_never_decreases(cp):
    add_run(cp)
    add_incident(cp)
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute("UPDATE incident SET retry_count = NULL WHERE incident_id = 'inc-1'")
    cp.execute("UPDATE incident SET retry_count = 3 WHERE incident_id = 'inc-1'")
    with pytest.raises(sqlite3.IntegrityError, match="must not decrease"):
        cp.execute("UPDATE incident SET retry_count = 2 WHERE incident_id = 'inc-1'")


# --------------------------------------------------------------------------
# criterion 4 -- no Q-0001 and no Q-0002 answer is encoded
# --------------------------------------------------------------------------


def test_no_table_assigns_a_writer_to_a_state_item(cp):
    # Q-0001 leaves the per-item single-writer table open. A column naming which
    # component owns which state item would answer it in DDL, and every
    # downstream test would then inherit the answer without anyone deciding it.
    forbidden = ("role", "component", "secretary", "dispatcher", "curator", "supervisor", "layer")
    for table in STATE_TABLES:
        for row in cp.execute(f"PRAGMA table_info({table})"):
            column = row[1].lower()
            assert not any(word in column for word in forbidden), f"{table}.{column}"

    ddl = executable_ddl().lower()
    for word in forbidden:
        assert word not in ddl, f"{word!r} appears in the executable DDL"


def test_the_incident_dedup_key_is_indexed_but_not_unique(cp):
    # Q-0002 is open: a UNIQUE dedup_key would force the increment-in-place
    # rule and make the linked-incident rule inexpressible.
    indexes = {
        name: cp.execute(f"PRAGMA index_info({name})").fetchall()
        for (name,) in cp.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    unique = {
        name
        for (name, unique_flag) in [
            (row[1], row[2]) for row in cp.execute("PRAGMA index_list(incident)")
        ]
        if unique_flag
    }
    assert "incident_by_dedup_key" in indexes
    assert "incident_by_dedup_key" not in unique


@pytest.mark.parametrize("collapse_rule", ["increment_in_place", "linked_incident"])
def test_both_q_0002_collapse_rules_are_expressible(cp, collapse_rule):
    # The test parameterises the rule rather than picking one, which is what
    # ACCEPTANCE.md section 2 requires of every downstream test until Q-0002 is
    # settled. Both branches must work against the same schema.
    add_run(cp)
    first = add_incident(cp, "inc-1", dedup_key="same-key")

    if collapse_rule == "increment_in_place":
        cp.execute(
            "UPDATE incident SET retry_count = retry_count + 1, updated_at_ms = ?"
            " WHERE dedup_key = 'same-key'",
            (T0 + 1,),
        )
        rows = cp.execute("SELECT retry_count FROM incident WHERE dedup_key = 'same-key'").fetchall()
        assert rows == [(1,)]
    else:
        add_incident(cp, "inc-2", dedup_key="same-key", related_incident_id=first, at=T0 + 1)
        rows = cp.execute(
            "SELECT incident_id, related_incident_id FROM incident WHERE dedup_key = 'same-key'"
            " ORDER BY incident_id"
        ).fetchall()
        assert rows == [("inc-1", None), ("inc-2", "inc-1")]


def test_no_renotification_window_is_baked_into_the_schema():
    # Q-0002's window and Q-0003's reconcile interval are open in absolute time.
    # A default, a CHECK against a duration or a column named for a window would
    # be one of them answered by DDL.
    ddl = executable_ddl().lower()
    for word in ("window", "renotif", "interval", "notify_after", "cooldown"):
        assert word not in ddl, word
    # No timestamp column carries a DEFAULT either: the clock is the caller's.
    assert not re.search(r"_at_ms[^,]*default", ddl)


def test_a_fact_state_vocabulary_is_not_frozen_in_the_ddl(cp):
    # D-0005's set is closed but lives in DECISIONS.md; duplicating it in a
    # schema that promises no migration would make extending it a schema change.
    add_run(cp)
    add_incident(cp, fact_state="SOME_LATER_FACT_STATE")
    assert cp.execute("SELECT count(*) FROM incident").fetchone()[0] == 1


# --------------------------------------------------------------------------
# gate item 2 -- one session per run, across the crash window
# --------------------------------------------------------------------------


def test_a_second_active_session_for_one_run_is_refused(cp):
    add_run(cp)
    add_session(cp, "sess-1")
    with pytest.raises(sqlite3.IntegrityError):
        add_session(cp, "sess-2")

    assert cp.execute("SELECT count(*) FROM session WHERE released_at_ms IS NULL").fetchone() == (1,)


def test_a_released_binding_frees_the_run_for_the_next_session(cp):
    add_run(cp)
    add_session(cp, "sess-1")
    cp.execute("UPDATE session SET released_at_ms = ? WHERE session_id = 'sess-1'", (T0 + 5,))
    add_session(cp, "sess-2", at=T0 + 6)

    active = cp.execute("SELECT session_id FROM session WHERE released_at_ms IS NULL").fetchall()
    assert active == [("sess-2",)]


def test_a_session_cannot_be_bound_to_a_run_that_does_not_exist(cp):
    with pytest.raises(sqlite3.IntegrityError):
        add_session(cp, "sess-1", run_id="no-such-run")


def test_a_readout_is_never_stored_empty(cp):
    # R4: "could not observe" and "observed nothing" must stay distinguishable.
    add_run(cp)
    with pytest.raises(sqlite3.IntegrityError):
        add_session(cp, "sess-1", observation="observed", provider_state=None)
    with pytest.raises(sqlite3.IntegrityError):
        add_session(cp, "sess-2", observation="unobserved", provider_state="running",
                    observation_reason=None)

    add_session(cp, "sess-3", observation="unobserved", provider_state=None,
                observation_reason="child has not reported yet")


def test_a_timestamp_that_is_not_an_integer_is_refused(cp):
    # No STRICT tables before SQLite 3.37, so the type checks are CHECKs; a
    # string timestamp would sort wrong in every recovery query.
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?,?,?,?)",
            ("run-x", "running", "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )


# --------------------------------------------------------------------------
# gate item 5 -- lease, fencing token, outbox, ack, dedup
# --------------------------------------------------------------------------


def test_a_lease_epoch_never_goes_backwards(cp):
    add_lease(cp, holder="holder-a", epoch=7)
    cp.execute("UPDATE lease SET epoch = 8 WHERE resource = 'run-1'")
    with pytest.raises(sqlite3.IntegrityError, match="never decreases"):
        cp.execute("UPDATE lease SET epoch = 3 WHERE resource = 'run-1'")

    # A renewal by the same holder keeps its epoch: re-acquiring is not what
    # invalidates a token, and forcing a bump would make every heartbeat
    # invalidate writes that are still in flight.
    cp.execute("UPDATE lease SET expires_at_ms = ? WHERE resource = 'run-1'", (T0 + 90_000,))
    assert cp.execute("SELECT epoch FROM lease").fetchone() == (8,)


def test_a_change_of_holder_must_raise_the_epoch(cp):
    # The handover written without naming the epoch is the dangerous one: it
    # hands the replacement the previous holder's token, and a paused former
    # holder returning with that token is then indistinguishable from the
    # current one at any destination that validates tokens rather than rows.
    add_lease(cp, holder="holder-a", epoch=4)

    with pytest.raises(sqlite3.IntegrityError, match="new holder must raise it"):
        cp.execute(
            "UPDATE lease SET holder = 'holder-b', acquired_at_ms = ?, expires_at_ms = ?"
            " WHERE resource = 'run-1'",
            (T0 + 60_000, T0 + 90_000),
        )
    with pytest.raises(sqlite3.IntegrityError, match="new holder must raise it"):
        cp.execute("UPDATE lease SET holder = 'holder-b', epoch = 4 WHERE resource = 'run-1'")

    cp.execute(
        "UPDATE lease SET holder = 'holder-b', epoch = 5, acquired_at_ms = ?, expires_at_ms = ?"
        " WHERE resource = 'run-1'",
        (T0 + 60_000, T0 + 90_000),
    )
    assert cp.execute("SELECT holder, epoch FROM lease").fetchone() == ("holder-b", 5)


def test_a_lease_resource_cannot_be_renamed_out_of_the_way(cp):
    # Blocking DELETE is not enough: renaming the primary key vacates the
    # resource, and the next INSERT takes it at epoch 1 -- the same token reuse,
    # reached by a different statement.
    add_lease(cp, resource="run-1", epoch=9)

    with pytest.raises(sqlite3.IntegrityError, match="never renamed"):
        cp.execute("UPDATE lease SET resource = 'run-1-old' WHERE resource = 'run-1'")
    with pytest.raises(sqlite3.IntegrityError):
        add_lease(cp, resource="run-1", holder="holder-c", epoch=1)

    assert cp.execute("SELECT resource, epoch FROM lease").fetchall() == [("run-1", 9)]


def test_an_outbox_row_keeps_the_identity_its_ack_was_recorded_against(cp):
    add_run(cp)
    add_outbox(cp)
    with pytest.raises(sqlite3.IntegrityError, match="message identity"):
        cp.execute("UPDATE outbox SET message_id = 'msg-2' WHERE message_id = 'msg-1'")


def test_a_lease_row_is_expired_not_deleted(cp):
    add_lease(cp)
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        cp.execute("DELETE FROM lease WHERE resource = 'run-1'")


def test_a_protected_write_validates_the_fencing_token_in_the_write(cp):
    # ACCEPTANCE.md section 2: expiry discovery alone is insufficient, the token
    # is validated atomically as part of the write. The schema's job is to make
    # that one statement possible; this is the statement.
    add_run(cp)
    add_lease(cp, resource="run-1", holder="holder-a", epoch=2)
    add_outbox(cp, writer_epoch=2)

    protected_write = """
        UPDATE outbox SET retry_count = retry_count + 1
         WHERE message_id = :message_id
           AND EXISTS (SELECT 1 FROM lease
                        WHERE resource = :resource AND holder = :holder
                          AND epoch = :epoch AND expires_at_ms > :now_ms)
    """
    stale = {"message_id": "msg-1", "resource": "run-1", "holder": "holder-a", "epoch": 1,
             "now_ms": T0}
    assert cp.execute(protected_write, stale).rowcount == 0

    current = dict(stale, epoch=2)
    assert cp.execute(protected_write, current).rowcount == 1

    expired = dict(current, now_ms=T0 + 10 ** 9)
    assert cp.execute(protected_write, expired).rowcount == 0
    assert cp.execute("SELECT retry_count FROM outbox").fetchone() == (1,)


def test_an_outbox_retry_count_is_monotonic_and_survives_a_restart(cp, db_path):
    add_run(cp)
    add_outbox(cp)
    cp.execute("UPDATE outbox SET retry_count = 4 WHERE message_id = 'msg-1'")
    with pytest.raises(sqlite3.IntegrityError, match="must not decrease"):
        cp.execute("UPDATE outbox SET retry_count = 0 WHERE message_id = 'msg-1'")
    cp.commit()
    cp.close()

    reopened = open_control_plane(db_path)
    try:
        assert reopened.execute("SELECT retry_count FROM outbox").fetchone() == (4,)
    finally:
        reopened.close()


def test_an_acked_message_is_acked_once(cp):
    add_run(cp)
    add_outbox(cp)
    cp.execute(
        "UPDATE outbox SET status = 'delivered', delivered_at_ms = ? WHERE message_id = 'msg-1'",
        (T0 + 1,),
    )
    cp.execute(
        "UPDATE outbox SET status = 'acked', acked_at_ms = ? WHERE message_id = 'msg-1'",
        (T0 + 2,),
    )

    # A duplicate ack changes nothing; a *different* ack instant is refused
    # rather than silently overwriting the first one.
    cp.execute(
        "UPDATE outbox SET status = 'acked', acked_at_ms = ? WHERE message_id = 'msg-1'",
        (T0 + 2,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="acked once"):
        cp.execute("UPDATE outbox SET acked_at_ms = ? WHERE message_id = 'msg-1'", (T0 + 9,))

    assert cp.execute("SELECT status, acked_at_ms FROM outbox").fetchall() == [("acked", T0 + 2)]


def test_an_outbox_row_cannot_claim_a_state_its_timestamps_deny(cp):
    add_run(cp)
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(cp, status="delivered", delivered_at_ms=None)
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(cp, message_id="msg-2", status="acked", delivered_at_ms=T0 + 1, acked_at_ms=None)
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(cp, message_id="msg-3", status="pending", delivered_at_ms=T0 + 1)


def test_one_effect_per_idempotency_key_and_refusals_stay_recordable(cp):
    add_run(cp)
    add_action(cp, "act-1", idempotency_key="ik-1")
    with pytest.raises(sqlite3.IntegrityError):
        add_action(cp, "act-2", idempotency_key="ik-1")

    # A refused attempt is durable, and a stale writer that keeps returning can
    # be recorded every time without any of those rows admitting an effect.
    add_action(cp, "act-3", idempotency_key="ik-1", status="refused",
               refusal_reason="stale fencing token")
    add_action(cp, "act-4", idempotency_key="ik-1", status="refused",
               refusal_reason="stale fencing token")

    effects = cp.execute(
        "SELECT count(*) FROM action WHERE idempotency_key = 'ik-1' AND status <> 'refused'"
    ).fetchone()
    assert effects == (1,)


def test_an_action_must_name_its_exactly_once_mechanism(cp):
    add_run(cp)
    with pytest.raises(sqlite3.IntegrityError):
        add_action(cp, exactly_once_mechanism=None)
    with pytest.raises(sqlite3.IntegrityError):
        add_action(cp, exactly_once_mechanism="hope")

    for index, mechanism in enumerate(
        ("destination_idempotency_key", "transactional_with_record", "human_gate")
    ):
        add_action(cp, f"act-{index}", idempotency_key=f"ik-{index}",
                   exactly_once_mechanism=mechanism)


def test_an_applied_action_is_applied_once(cp):
    add_run(cp)
    add_action(cp)
    cp.execute(
        "UPDATE action SET status = 'applied', applied_at_ms = ? WHERE action_id = 'act-1'",
        (T0 + 1,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="applied once"):
        cp.execute(
            "UPDATE action SET applied_at_ms = ? WHERE action_id = 'act-1'", (T0 + 2,)
        )


def test_a_refused_action_carries_its_reason(cp):
    add_run(cp)
    with pytest.raises(sqlite3.IntegrityError):
        add_action(cp, status="refused", refusal_reason=None)


# --------------------------------------------------------------------------
# criterion 2 -- state is reconstructable by query from SQLite alone (D-0001)
# --------------------------------------------------------------------------


def _write_in_flight_state(cp) -> None:
    add_run(cp, "run-1")
    add_run(cp, "run-2")
    add_session(cp, "sess-1", "run-1")
    add_session(cp, "sess-old", "run-2", at=T0 - 10, released_at_ms=T0 - 5)
    add_lease(cp, resource="run-1", holder="holder-a", epoch=3, ttl_ms=30_000)
    add_lease(cp, resource="run-2", holder="holder-b", epoch=1, at=T0 - 90_000, ttl_ms=1_000)
    add_outbox(cp, "msg-1", dedup_key="dk-1")
    add_outbox(cp, "msg-2", dedup_key="dk-2", status="acked", delivered_at_ms=T0 + 1,
               acked_at_ms=T0 + 2)
    add_incident(cp, "inc-1", dedup_key="dk-1")
    add_incident(cp, "inc-2", dedup_key="dk-2", resolved_at_ms=T0 + 3)
    add_action(cp, "act-1", idempotency_key="ik-1")
    add_action(cp, "act-2", idempotency_key="ik-2", status="applied", applied_at_ms=T0 + 4)
    cp.commit()


def test_reconstruction_reads_only_what_is_still_in_flight(cp):
    _write_in_flight_state(cp)
    state = reconstruct(cp, now_ms=T0 + 1_000)

    assert [row["run_id"] for row in state.runs] == ["run-1", "run-2"]
    assert [row["session_id"] for row in state.active_sessions] == ["sess-1"]
    assert [row["resource"] for row in state.held_leases] == ["run-1"]
    assert [row["message_id"] for row in state.unfinished_outbox] == ["msg-1"]
    assert [row["incident_id"] for row in state.unresolved_incidents] == ["inc-1"]
    assert [row["action_id"] for row in state.pending_actions] == ["act-1"]


def test_lease_liveness_is_read_against_the_callers_clock(cp):
    _write_in_flight_state(cp)

    # The clock is skewed across the expiry boundary, as ACCEPTANCE.md section 2
    # requires; the answer changes with the caller's clock and with nothing else.
    assert [row["resource"] for row in reconstruct(cp, now_ms=T0 - 89_500).held_leases] == [
        "run-1",
        "run-2",
    ]
    assert reconstruct(cp, now_ms=T0 + 10 ** 9).held_leases == ()


def test_the_incident_packet_is_reconstructed_whole(cp):
    # D-0007: the on-demand AI is startable statelessly from the row alone, so
    # every field of the packet has to come back out of the query.
    add_run(cp)
    add_incident(cp, known_pattern="approval-prompt", elapsed_ms=1234,
                 previous_assessment="watch", detector_version="d7", retry_count=2)
    packet = reconstruct(cp, now_ms=T0).unresolved_incidents[0]

    assert packet["dedup_key"] == "dk-1"
    assert packet["retry_count"] == 2
    assert packet["detector_version"] == "d7"
    assert packet["known_pattern"] == "approval-prompt"
    assert packet["elapsed_ms"] == 1234
    assert packet["previous_assessment"] == "watch"
    assert packet["evidence_refs"] == "[]"


def test_state_survives_the_process_that_wrote_it(cp, db_path, tmp_path):
    """The reconstruction a *fresh interpreter* gets is the same one (D-0001).

    Run in a subprocess rather than on a second connection: a second connection
    in this process would still share module state, and the claim under test is
    that nothing a recovering process needs lives only in a process.
    """

    _write_in_flight_state(cp)
    in_process = reconstruct(cp, now_ms=T0 + 1_000)
    cp.close()

    src = Path(claude_org_runtime.__file__).resolve().parent.parent
    program = (
        "import json, sys, dataclasses\n"
        "from claude_org_runtime.control_plane import open_control_plane, reconstruct\n"
        "connection = open_control_plane(sys.argv[1])\n"
        "state = reconstruct(connection, now_ms=int(sys.argv[2]))\n"
        "print(json.dumps(dataclasses.asdict(state)))\n"
    )
    # Inherit the environment and add src to it, rather than handing the child a
    # hand-built one: on Windows an interpreter started without SystemRoot and
    # without the PATH its DLLs are found on does not reach main() at all, so a
    # replaced environment fails the test for a reason that has nothing to do
    # with what it is testing.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(src), env["PYTHONPATH"]]) if env.get(
        "PYTHONPATH"
    ) else str(src)

    result = subprocess.run(
        [sys.executable, "-c", program, str(db_path), str(T0 + 1_000)],
        capture_output=True, text=True, env=env,
    )
    # Report the child's own words. check=True raises a CalledProcessError that
    # carries the exit status and nothing else, which is a failure nobody can
    # diagnose from a CI log.
    assert result.returncode == 0, (
        f"the recovering process exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    recovered = json.loads(result.stdout)
    assert recovered["active_sessions"] == [dict(row) for row in in_process.active_sessions]
    assert recovered["unresolved_incidents"] == [
        dict(row) for row in in_process.unresolved_incidents
    ]
    assert recovered["pending_actions"] == [dict(row) for row in in_process.pending_actions]
    assert recovered["unfinished_outbox"] == [dict(row) for row in in_process.unfinished_outbox]
    assert recovered["held_leases"] == [dict(row) for row in in_process.held_leases]
    assert recovered["runs"] == [dict(row) for row in in_process.runs]


# --------------------------------------------------------------------------
# criterion 5 -- corrupt state is refused, never recovered as empty (R3)
# --------------------------------------------------------------------------


def test_an_absent_database_is_refused_and_not_created(db_path):
    with pytest.raises(MissingStateRefused):
        open_control_plane(db_path)
    assert not db_path.exists()


def test_a_file_that_is_not_a_database_is_refused_and_left_alone(db_path):
    db_path.write_bytes(b"this is not a database, it is a note someone left")
    before = db_path.read_bytes()

    with pytest.raises(CorruptStateRefused):
        open_control_plane(db_path)

    assert db_path.read_bytes() == before
    assert not list(db_path.parent.glob("*-journal"))
    assert not list(db_path.parent.glob("*-wal"))


def test_a_truncated_database_is_refused(cp, db_path):
    add_run(cp)
    cp.commit()
    cp.close()
    with db_path.open("r+b") as handle:
        handle.truncate(db_path.stat().st_size // 3)

    with pytest.raises(CorruptStateRefused):
        open_control_plane(db_path)


def test_a_database_missing_a_state_table_is_refused_not_rebuilt(cp, db_path):
    add_run(cp)
    cp.commit()
    cp.execute("DROP TABLE action")
    cp.commit()
    cp.close()

    with pytest.raises(CorruptStateRefused, match="missing state table"):
        open_control_plane(db_path)

    # Refused means untouched: the table is not recreated behind the caller's
    # back, and the surviving rows are not discarded either.
    raw = sqlite3.connect(db_path)
    try:
        tables = {name for (name,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "action" not in tables
        assert raw.execute("SELECT count(*) FROM run").fetchone() == (1,)
    finally:
        raw.close()


def test_a_database_from_another_application_is_refused(tmp_path):
    other = tmp_path / "someone-elses.sqlite3"
    raw = sqlite3.connect(other)
    raw.execute("CREATE TABLE notes (body TEXT)")
    raw.commit()
    raw.close()

    with pytest.raises(CorruptStateRefused, match="application_id"):
        open_control_plane(other)


def test_a_database_at_another_revision_is_refused_rather_than_migrated(cp, db_path):
    cp.commit()
    cp.close()
    raw = sqlite3.connect(db_path)
    raw.execute(f"PRAGMA user_version = {SCHEMA_REVISION + 1}")
    raw.commit()
    raw.close()

    with pytest.raises(CorruptStateRefused, match="no migration path"):
        open_control_plane(db_path)


def test_a_dangling_reference_is_refused(cp, db_path):
    cp.commit()
    cp.close()
    # Foreign keys are per-connection, so a writer that never enabled them can
    # leave a session pointing at no run. Recovery must not read that as state.
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO session (session_id, run_id, provider, observation, provider_state,"
        " bound_at_ms) VALUES ('sess-1', 'ghost-run', 'stub', 'observed', 'running', ?)",
        (T0,),
    )
    raw.commit()
    raw.close()

    with pytest.raises(CorruptStateRefused, match="foreign key"):
        open_control_plane(db_path)


def test_creating_over_an_existing_path_is_refused(cp, db_path):
    add_run(cp)
    cp.commit()

    with pytest.raises(ControlPlaneRefusal, match="already exists"):
        create_control_plane(db_path)

    assert cp.execute("SELECT count(*) FROM run").fetchone() == (1,)


def test_a_created_database_is_stamped_so_it_can_be_recognised(cp, db_path):
    assert cp.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    assert cp.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_REVISION


def test_an_opened_connection_enforces_foreign_keys(cp, db_path):
    cp.commit()
    cp.close()
    reopened = open_control_plane(db_path)
    try:
        assert reopened.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        reopened.close()


# --------------------------------------------------------------------------
# durable evidence cannot be edited away (round-1 self-review)
# --------------------------------------------------------------------------


def test_an_action_keeps_the_idempotency_key_it_was_recorded_with(cp):
    # A key unique among *current* values is a snapshot, not evidence: rewriting
    # an applied action's key vacates it, and the next writer takes the original
    # key as though the first effect had never happened.
    add_run(cp)
    add_action(cp, "act-1", idempotency_key="ik-1", status="applied", applied_at_ms=T0 + 1)

    with pytest.raises(sqlite3.IntegrityError, match="keeps the idempotency key"):
        cp.execute("UPDATE action SET idempotency_key = 'ik-2' WHERE action_id = 'act-1'")
    with pytest.raises(sqlite3.IntegrityError):
        add_action(cp, "act-2", idempotency_key="ik-1")


def test_a_refused_action_stays_refused(cp):
    add_run(cp)
    add_action(cp, status="refused", refusal_reason="stale fencing token")

    with pytest.raises(sqlite3.IntegrityError, match="stays refused"):
        cp.execute(
            "UPDATE action SET status = 'pending', refusal_reason = NULL WHERE action_id = 'act-1'"
        )
    assert cp.execute("SELECT status, refusal_reason FROM action").fetchall() == [
        ("refused", "stale fencing token")
    ]


def test_the_outbox_lifecycle_does_not_walk_backwards(cp):
    add_run(cp)
    add_outbox(cp)
    cp.execute(
        "UPDATE outbox SET status = 'delivered', delivered_at_ms = ? WHERE message_id = 'msg-1'",
        (T0 + 1,),
    )

    # Whichever guard fires first -- the forward-only status trigger or the
    # set-once delivery instant -- the row does not walk back. A status
    # regression that leaves delivered_at_ms alone is refused by the CHECK
    # instead, so every route out of 'delivered' is closed.
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute(
            "UPDATE outbox SET status = 'pending', delivered_at_ms = NULL"
            " WHERE message_id = 'msg-1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute("UPDATE outbox SET status = 'pending' WHERE message_id = 'msg-1'")
    with pytest.raises(sqlite3.IntegrityError, match="delivered once"):
        cp.execute("UPDATE outbox SET delivered_at_ms = ? WHERE message_id = 'msg-1'", (T0 + 9,))
    with pytest.raises(sqlite3.IntegrityError, match="keeps the dedup key"):
        cp.execute("UPDATE outbox SET dedup_key = 'dk-other' WHERE message_id = 'msg-1'")

    assert cp.execute("SELECT status, delivered_at_ms, dedup_key FROM outbox").fetchall() == [
        ("delivered", T0 + 1, "dk-1")
    ]


def test_an_empty_reason_is_as_empty_as_a_missing_one(cp):
    # R4 is about the *distinction* surviving, and '' erases it exactly as NULL
    # would -- with the added harm that a CHECK written against NULL says it did
    # not.
    add_run(cp)
    with pytest.raises(sqlite3.IntegrityError):
        add_session(cp, "sess-1", observation="unobserved", provider_state=None,
                    observation_reason="")
    with pytest.raises(sqlite3.IntegrityError):
        add_session(cp, "sess-2", observation="observed", provider_state="")
    with pytest.raises(sqlite3.IntegrityError):
        add_action(cp, status="refused", refusal_reason="")


# --------------------------------------------------------------------------
# the shape of the schema is verified, not just the names (round-1 self-review)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage",
    [
        "DROP INDEX session_one_active_binding_per_run",
        "DROP INDEX action_one_effect_per_key",
        "DROP TRIGGER lease_epoch_is_monotonic",
        "DROP TRIGGER outbox_ack_is_set_once",
    ],
)
def test_a_database_that_lost_a_constraint_is_refused(cp, db_path, damage):
    # integrity_check answers "are the pages readable", not "is this the schema
    # you wrote". A database missing an index or a trigger passes it and then
    # permits exactly what the lost constraint forbade -- silently, and only at
    # the moment it matters.
    cp.commit()
    cp.execute(damage)
    cp.commit()
    cp.close()

    with pytest.raises(CorruptStateRefused, match="schema"):
        open_control_plane(db_path)


def test_the_expected_fingerprint_is_derived_from_the_ddl_not_pinned_beside_it(cp):
    assert s5._schema_fingerprint(cp) == s5.expected_schema_fingerprint()


def test_a_creation_that_loses_a_race_does_not_delete_the_winners_database(cp, db_path, monkeypatch):
    # Two processes creating the same absent path both pass an exists() check;
    # the loser's CREATE TABLE then fails against the winner's database, and a
    # cleanup that trusts "I was creating it" deletes a live database. The claim
    # is atomic instead, so the loser never reaches the cleanup.
    add_run(cp)
    cp.commit()
    monkeypatch.setattr(Path, "exists", lambda self: False)

    with pytest.raises(ControlPlaneRefusal, match="already exists"):
        create_control_plane(db_path)

    monkeypatch.undo()
    assert db_path.exists()
    survivor = open_control_plane(db_path)
    try:
        assert survivor.execute("SELECT count(*) FROM run").fetchone() == (1,)
    finally:
        survivor.close()


def test_a_run_with_nothing_hanging_off_it_still_reconstructs(cp):
    # The riskiest moment for a run is before anything references it: a
    # reconstruction that reached runs only through their sessions, outbox rows
    # or incidents would lose exactly the run that was killed there.
    add_run(cp, "run-lonely", status="starting")
    state = reconstruct(cp, now_ms=T0)

    assert [(row["run_id"], row["status"]) for row in state.runs] == [("run-lonely", "starting")]
    assert state.active_sessions == ()


def test_exactly_once_evidence_cannot_be_deleted_out_of_the_way(cp):
    # Freezing a value protects it only while the row exists. Deleting an applied
    # action vacates its idempotency key, and the same effect can then be applied
    # a second time -- the one thing item 4 asks this table to make impossible.
    add_run(cp)
    add_action(cp, "act-1", idempotency_key="ik-1", status="applied", applied_at_ms=T0 + 1)
    add_outbox(cp, "msg-1", status="acked", delivered_at_ms=T0 + 1, acked_at_ms=T0 + 2)

    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        cp.execute("DELETE FROM action WHERE action_id = 'act-1'")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        cp.execute("DELETE FROM outbox WHERE message_id = 'msg-1'")

    # And a refusal is evidence too, so it is not deletable either.
    add_action(cp, "act-2", idempotency_key="ik-2", status="refused", refusal_reason="stale token")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        cp.execute("DELETE FROM action WHERE action_id = 'act-2'")

    with pytest.raises(sqlite3.IntegrityError):
        add_action(cp, "act-3", idempotency_key="ik-1")


def test_a_creation_that_cannot_connect_leaves_no_file_behind(db_path, monkeypatch):
    # The O_EXCL claim creates the file before SQLite is involved, so a connect
    # that never returns a connection would otherwise leave an empty file that
    # refuses creation (it exists) and refuses opening (it is not a database).
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(s5.sqlite3, "connect", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        create_control_plane(db_path)

    monkeypatch.undo()
    assert not db_path.exists()
    create_control_plane(db_path).close()
