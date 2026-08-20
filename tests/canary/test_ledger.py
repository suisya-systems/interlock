"""The routing ledger's database-enforced guarantees.

These tests are the durable half (D-0026): the ledger implementation may be
thrown away, but whatever records run ownership at the real canary still has
to refuse a mid-flight owner change, an edited routing history and a store it
cannot verify -- and refuse them in the store, not in the discipline of the
writer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.canary import ledger as ledger_module
from claude_org_runtime.canary.ledger import (
    LEDGER_APPLICATION_ID,
    LEDGER_REVISION,
    LEDGER_SCHEMA_PATH,
    LEDGER_TABLES,
    CorruptLedgerRefused,
    MissingLedgerRefused,
    RoutingLedgerRefusal,
    create_routing_ledger,
    load_ledger_sql,
    open_routing_ledger,
)
from claude_org_runtime.canary.marking import REHEARSAL_MARKING

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "routing-ledger.sqlite3"


@pytest.fixture
def ledger(ledger_path: Path):
    connection = create_routing_ledger(ledger_path)
    try:
        yield connection
    finally:
        connection.close()


def add_decision(ledger, owning_system: str = "synthetic_v1", at: int = T0, reason: str = "baseline"):
    with ledger:
        ledger.execute(
            "INSERT INTO routing_decision (owning_system, decided_at_ms, reason) VALUES (?, ?, ?)",
            (owning_system, at, reason),
        )


def add_run(ledger, run_id: str = "run-1", owning_system: str = "synthetic_v1", seq: int = 1, at: int = T0):
    with ledger:
        ledger.execute(
            "INSERT INTO run_owner (run_id, owning_system, decision_seq, routed_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (run_id, owning_system, seq, at),
        )


# --------------------------------------------------------------------------
# the marking
# --------------------------------------------------------------------------


def test_the_ddl_carries_the_rehearsal_marking_as_one_sentence():
    collapsed = ledger_module._collapsed(LEDGER_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert REHEARSAL_MARKING in collapsed


def test_the_ddl_is_refused_if_the_marking_is_removed(tmp_path, monkeypatch):
    stripped = tmp_path / "stripped.sql"
    text = LEDGER_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "NOT A" in text
    stripped.write_text(text.replace("NOT A", "A"), encoding="utf-8")
    monkeypatch.setattr(ledger_module, "LEDGER_SCHEMA_PATH", stripped)
    with pytest.raises(RoutingLedgerRefusal, match="rehearsal"):
        load_ledger_sql()


# --------------------------------------------------------------------------
# no mid-flight owner change, enforced by the store
# --------------------------------------------------------------------------


def test_a_run_never_changes_owning_system(ledger):
    add_decision(ledger)
    add_run(ledger, owning_system="synthetic_v1")
    with pytest.raises(sqlite3.IntegrityError, match="mid-flight"):
        ledger.execute("UPDATE run_owner SET owning_system = 'interlock' WHERE run_id = 'run-1'")


def test_a_run_owner_row_admits_no_update_at_all(ledger):
    # There is nothing legitimately updatable on the row, so even a
    # same-value update is refused: the trigger guards the row, not a column.
    add_decision(ledger)
    add_run(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="mid-flight"):
        ledger.execute("UPDATE run_owner SET routed_at_ms = routed_at_ms WHERE run_id = 'run-1'")


def test_a_run_owner_row_is_never_deleted(ledger):
    add_decision(ledger)
    add_run(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        ledger.execute("DELETE FROM run_owner")


def test_or_replace_is_not_a_way_around_the_owner_trigger(ledger):
    # INSERT OR REPLACE resolves the conflict by deleting the standing row,
    # and with recursive_triggers off (SQLite's default) that implicit
    # delete fires no trigger at all -- a mid-flight owner change in one
    # statement. The connections this module hands out turn the pragma on,
    # and this test holds them to it.
    add_decision(ledger)
    add_run(ledger, owning_system="synthetic_v1")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        ledger.execute(
            "INSERT OR REPLACE INTO run_owner (run_id, owning_system, decision_seq, routed_at_ms) "
            "VALUES ('run-1', 'interlock', 1, ?)",
            (T0 + 1,),
        )
    assert ledger.execute(
        "SELECT owning_system FROM run_owner WHERE run_id = 'run-1'"
    ).fetchone() == ("synthetic_v1",)


def test_or_replace_is_not_a_way_around_the_decision_history(ledger):
    add_decision(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        ledger.execute(
            "INSERT OR REPLACE INTO routing_decision "
            "(decision_seq, owning_system, decided_at_ms, reason) "
            "VALUES (1, 'interlock', ?, 'rewritten')",
            (T0 + 1,),
        )


def test_one_ledger_row_per_run(ledger):
    add_decision(ledger)
    add_run(ledger, run_id="run-1")
    with pytest.raises(sqlite3.IntegrityError):
        add_run(ledger, run_id="run-1", owning_system="interlock")


# --------------------------------------------------------------------------
# the routing history is append-only, in order
# --------------------------------------------------------------------------


def test_a_routing_decision_is_never_edited(ledger):
    add_decision(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="never edited"):
        ledger.execute("UPDATE routing_decision SET owning_system = 'interlock'")


def test_a_routing_decision_is_never_deleted(ledger):
    add_decision(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        ledger.execute("DELETE FROM routing_decision")


def test_a_decision_cannot_be_back_filled_behind_the_newest(ledger):
    # The newest row IS the routing, so an insert at a smaller sequence would
    # change which decision is current without appending anything.
    add_decision(ledger)
    add_decision(ledger, owning_system="interlock", reason="canary")
    # An occupied sequence number is a plain uniqueness refusal ...
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(
            "INSERT INTO routing_decision (decision_seq, owning_system, decided_at_ms, reason) "
            "VALUES (1, 'synthetic_v1', ?, 'rewrite')",
            (T0,),
        )
    # ... and a vacant one BEHIND the newest is refused by the ordering
    # trigger: it would change which decision is current without appending.
    with pytest.raises(sqlite3.IntegrityError, match="appended in order"):
        ledger.execute(
            "INSERT INTO routing_decision (decision_seq, owning_system, decided_at_ms, reason) "
            "VALUES (0, 'synthetic_v1', ?, 'prehistory')",
            (T0,),
        )


# --------------------------------------------------------------------------
# the vocabulary is closed, the types are asserted
# --------------------------------------------------------------------------


@pytest.mark.parametrize("table_insert", [
    "INSERT INTO routing_decision (owning_system, decided_at_ms, reason) VALUES ('v1', ?, 'r')",
    "INSERT INTO run_owner (run_id, owning_system, decision_seq, routed_at_ms) "
    "VALUES ('run-x', 'v1', 1, ?)",
])
def test_the_owning_system_vocabulary_is_closed(ledger, table_insert):
    # 'v1' in particular is refused: the stand-in is named synthetic_v1 so a
    # rehearsal ledger can never read as evidence against the live system.
    add_decision(ledger)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(table_insert, (T0,))


def test_a_timestamp_that_is_not_an_integer_is_refused(ledger):
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(
            "INSERT INTO routing_decision (owning_system, decided_at_ms, reason) "
            "VALUES ('interlock', 'yesterday', 'r')"
        )


def test_an_empty_reason_is_refused(ledger):
    # A decision with no reason is unauditable; a rollback especially so.
    with pytest.raises(sqlite3.IntegrityError):
        ledger.execute(
            "INSERT INTO routing_decision (owning_system, decided_at_ms, reason) "
            "VALUES ('interlock', ?, '')",
            (T0,),
        )


# --------------------------------------------------------------------------
# refusal discipline (R3): never created by an open, never read when broken
# --------------------------------------------------------------------------


def test_an_absent_ledger_is_not_an_empty_one(ledger_path):
    with pytest.raises(MissingLedgerRefused):
        open_routing_ledger(ledger_path)


def test_creation_refuses_an_existing_path(ledger_path, ledger):
    with pytest.raises(RoutingLedgerRefusal, match="already exists"):
        create_routing_ledger(ledger_path)


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    impostor = tmp_path / "impostor.sqlite3"
    impostor.write_text("not a database", encoding="utf-8")
    with pytest.raises(CorruptLedgerRefused):
        open_routing_ledger(impostor)


def test_some_other_database_is_refused_by_application_id(tmp_path):
    other = tmp_path / "other.sqlite3"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE routing_decision (x)")
    connection.execute("CREATE TABLE run_owner (x)")
    connection.execute(f"PRAGMA user_version = {LEDGER_REVISION}")
    connection.commit()
    connection.close()
    with pytest.raises(CorruptLedgerRefused, match="application_id"):
        open_routing_ledger(other)


def test_another_revision_is_refused_never_migrated(ledger_path, ledger):
    ledger.execute(f"PRAGMA user_version = {LEDGER_REVISION + 1}")
    ledger.commit()
    ledger.close()
    with pytest.raises(CorruptLedgerRefused, match="revision"):
        open_routing_ledger(ledger_path)


def test_a_ledger_that_lost_a_trigger_is_refused(ledger_path, ledger):
    # integrity_check passes on a database that has lost a trigger -- and the
    # trigger here IS the mid-flight immutability, so the shape is compared.
    ledger.execute("DROP TRIGGER run_owner_never_changes_mid_flight")
    ledger.commit()
    ledger.close()
    with pytest.raises(CorruptLedgerRefused, match="trigger"):
        open_routing_ledger(ledger_path)


def test_a_ledger_that_lost_a_table_is_missing_state_not_empty(ledger_path, ledger):
    ledger.execute("DROP TABLE run_owner")
    ledger.commit()
    ledger.close()
    with pytest.raises(CorruptLedgerRefused, match="missing ledger table"):
        open_routing_ledger(ledger_path)


def test_a_dangling_ledger_reference_is_refused(ledger_path, ledger):
    # foreign_keys is per-connection, so a foreign writer can leave a
    # run_owner row pointing at a decision that does not exist; opening
    # refuses the store rather than reading partial state.
    ledger.execute("PRAGMA foreign_keys = OFF")
    add_run(ledger, seq=99)
    ledger.close()
    with pytest.raises(CorruptLedgerRefused, match="dangling"):
        open_routing_ledger(ledger_path)


def test_a_directory_is_not_a_ledger(tmp_path):
    with pytest.raises(CorruptLedgerRefused, match="regular file"):
        open_routing_ledger(tmp_path)


def test_a_verifiable_ledger_reopens_with_its_rows(ledger_path, ledger):
    add_decision(ledger)
    add_run(ledger)
    ledger.close()
    reopened = open_routing_ledger(ledger_path)
    try:
        assert reopened.execute("SELECT COUNT(*) FROM run_owner").fetchone() == (1,)
        assert LEDGER_TABLES == ("routing_decision", "run_owner")
    finally:
        reopened.close()


def test_the_ledger_application_id_is_not_the_control_plane_s():
    from claude_org_runtime.control_plane.schema import APPLICATION_ID

    assert LEDGER_APPLICATION_ID != APPLICATION_ID
