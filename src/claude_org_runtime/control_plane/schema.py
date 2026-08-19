"""S5 -- opening, creating and querying the **spike** control-plane database.

.. warning::

   **The schema this module applies is a spike schema, and no migration path is
   promised from it (D-0026).** The marking lives in ``spike_schema.sql``
   itself, where the acceptance criterion of Issue ``#12`` puts it -- not in a
   commit message and not in a plan document. This module is the enforcement
   arm of the same sentence: a database written at one
   :data:`SCHEMA_REVISION` is **refused** by the next one rather than migrated
   (:func:`open_control_plane`). Refusing is deliberate. A migration path would
   be the first half of a promotion nobody decided on, and ``Q-0001`` -- the
   real DDL, keys, indices, per-item single-writer table and migration policy
   -- stays open until it is decided on its own terms.

Two behaviours here are load-bearing rather than convenient:

**Corrupt state is refused, never recovered as empty (R3).** R3 records the v1
defect by name: "a broken state file recovers as empty" permits already-applied
effects to replay once dedup state is authoritative. So
:func:`open_control_plane` never creates, never repairs, and never returns a
connection to a database it could not verify. Every refusal is a typed
:class:`ControlPlaneRefusal` carrying what was wrong, and the file on disk is
left exactly as it was found -- verification runs over a **read-only**
connection, so the refusal path cannot even write a rollback journal. Creating
a database is a separate, explicit call (:func:`create_control_plane`), which
in turn refuses to touch a path that already exists.

**State is reconstructed by query alone (D-0001).** :func:`reconstruct` answers
"what was happening?" from the database and nothing else -- no cached handle, no
module-level registry, no state that a restarted process would have to be told
about. :data:`RECONSTRUCTION_QUERIES` holds the SQL as data so that the queries
themselves can be read, reviewed and run by hand against a database recovered
from a crash.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "APPLICATION_ID",
    "RECONSTRUCTION_QUERIES",
    "SCHEMA_REVISION",
    "SPIKE_MARKING",
    "SPIKE_SCHEMA_PATH",
    "STATE_TABLES",
    "ControlPlaneRefusal",
    "ControlPlaneState",
    "CorruptStateRefused",
    "MissingStateRefused",
    "create_control_plane",
    "expected_schema_fingerprint",
    "load_schema_sql",
    "open_control_plane",
    "reconstruct",
]

#: The DDL. It is a separate ``.sql`` file and not a string in this module on
#: purpose: the acceptance criterion asks that *the schema file* carry the spike
#: marking, and a schema file that can be read, diffed and run by ``sqlite3``
#: without importing Python is the one an operator will actually read.
SPIKE_SCHEMA_PATH = Path(__file__).with_name("spike_schema.sql")

#: The sentence :func:`load_schema_sql` refuses to load the DDL without. It is
#: checked at load time rather than asserted in a test alone, because the
#: failure it guards against is the marking being edited away by someone who
#: found it noisy -- which is exactly how a spike schema becomes *the* schema.
SPIKE_MARKING = "THIS IS A SPIKE SCHEMA. NO MIGRATION PATH IS PROMISED FROM IT."

#: ``PRAGMA application_id``. Stamped into every database this module creates so
#: that a file which is *valid SQLite but not ours* is refused as such rather
#: than reported as a schema with missing tables. ASCII ``ILK5``.
APPLICATION_ID = 0x494C4B35

#: ``PRAGMA user_version``. Bumped whenever ``spike_schema.sql`` changes shape.
#: A database at any other revision is refused: there is no migration (D-0026).
SCHEMA_REVISION = 1

#: The six tables of the slice, in the order Issue ``#12`` names them. Every one
#: of them must be present for a database to be usable; a database missing one
#: is corrupt, not empty.
STATE_TABLES = ("run", "session", "lease", "outbox", "incident", "action")

#: The recovery reads, as data. Each answers one question a process asks after a
#: mid-flight kill, and each is answerable from SQLite alone (D-0001).
RECONSTRUCTION_QUERIES: Mapping[str, str] = {
    # D-0001 names `run` as source-of-truth state, and a run may exist before any
    # session, outbox row or incident does -- so a reconstruction that reached
    # runs only through their children would lose exactly the run that was killed
    # at its riskiest moment. Every run is returned, unfiltered: which statuses
    # count as finished is part of the vocabulary Q-0001 leaves open, and a WHERE
    # clause here would pick one.
    "runs": """
        SELECT run_id, status, created_at_ms, updated_at_ms
          FROM run
         ORDER BY created_at_ms, run_id
    """,
    # Item 2: exactly one live session per run, re-identified after the crash
    # window. The uniqueness is the database's (see
    # ``session_one_active_binding_per_run``); this query is how a recovering
    # supervisor reads it back.
    "active_sessions": """
        SELECT session_id, run_id, provider, observation, provider_state,
               observation_reason, bound_at_ms
          FROM session
         WHERE released_at_ms IS NULL
         ORDER BY bound_at_ms, session_id
    """,
    # Item 5: which resources are held, and under which fencing token, at the
    # instant recovery runs. The caller supplies :now_ms -- the clock is not the
    # database's (ACCEPTANCE.md section 2 skews it on purpose).
    "held_leases": """
        SELECT resource, holder, epoch, acquired_at_ms, expires_at_ms
          FROM lease
         WHERE expires_at_ms > :now_ms
         ORDER BY resource
    """,
    # Item 5/6: everything enqueued and not yet acked, oldest first. "No outbox
    # row remains in a state with no owner after recovery" is checked against
    # this.
    "unfinished_outbox": """
        SELECT message_id, run_id, recipient, dedup_key, status, retry_count,
               writer_epoch, enqueued_at_ms, delivered_at_ms
          FROM outbox
         WHERE status <> 'acked'
         ORDER BY enqueued_at_ms, message_id
    """,
    # Item 4: "work resumes from unresolved incidents" (D-0001), and the row is
    # the whole packet the on-demand AI is restarted from (D-0007).
    "unresolved_incidents": """
        SELECT incident_id, run_id, session_id, fact_state, detector_version,
               dedup_key, retry_count, known_pattern, elapsed_ms, evidence_refs,
               recent_transitions, previous_assessment, previous_action_id,
               related_incident_id, created_at_ms, updated_at_ms
          FROM incident
         WHERE resolved_at_ms IS NULL
         ORDER BY created_at_ms, incident_id
    """,
    # Item 4: side effects that were recorded but not applied. Each names the
    # mechanism by which re-applying it is safe -- SQLite cannot tell an effect
    # that completed from one that never started, so the mechanism is the answer
    # and the query is not.
    "pending_actions": """
        SELECT action_id, run_id, incident_id, kind, idempotency_key,
               exactly_once_mechanism, writer_epoch, created_at_ms
          FROM action
         WHERE status = 'pending'
         ORDER BY created_at_ms, action_id
    """,
}


class ControlPlaneRefusal(Exception):
    """A database was refused. It was neither repaired nor recreated (R3)."""


class MissingStateRefused(ControlPlaneRefusal):
    """The database does not exist, and opening one never creates it.

    Separate from :class:`CorruptStateRefused` because the operator's next move
    differs: an absent database may legitimately be created
    (:func:`create_control_plane`), a corrupt one may not.
    """


class CorruptStateRefused(ControlPlaneRefusal):
    """The database exists but could not be verified, so it was not opened.

    Raised for a file that is not SQLite, a failed ``integrity_check`` or
    ``foreign_key_check``, a foreign ``application_id``, a ``user_version``
    this revision does not write, and a missing state table. All of them are
    the same answer -- refuse -- because the alternative R3 rules out is
    treating any of them as an empty database.
    """


@dataclass(frozen=True)
class ControlPlaneState:
    """What :func:`reconstruct` read back.

    Rows, not domain objects: this is the spike schema's shape, and D-0026 keeps
    it from becoming a domain model by inertia.
    """

    runs: Sequence[Mapping[str, Any]]
    active_sessions: Sequence[Mapping[str, Any]]
    held_leases: Sequence[Mapping[str, Any]]
    unfinished_outbox: Sequence[Mapping[str, Any]]
    unresolved_incidents: Sequence[Mapping[str, Any]]
    pending_actions: Sequence[Mapping[str, Any]]


def load_schema_sql() -> str:
    """Return the DDL, refusing it if the spike marking is not in the file.

    :raises ControlPlaneRefusal: if :data:`SPIKE_MARKING` is absent. The marking
        *is* the D-0026 mitigation; a schema that has lost it is one edit away
        from being applied as though something had promoted it.
    """

    sql = SPIKE_SCHEMA_PATH.read_text(encoding="utf-8")
    if SPIKE_MARKING not in sql:
        raise ControlPlaneRefusal(
            f"{SPIKE_SCHEMA_PATH.name} no longer carries the spike marking "
            f"({SPIKE_MARKING!r}); refusing to apply it (D-0026)"
        )
    return sql


def create_control_plane(path: str | Path) -> sqlite3.Connection:
    """Create the spike database at *path* and return an open connection.

    Creation is explicit and separate from opening, which is what keeps
    "recover as empty" from being reachable by accident (R3): no code path that
    merely wanted to *read* state can end up having made a new one.

    :raises ControlPlaneRefusal: if anything already exists at *path*. An
        existing database is never clobbered, and an existing non-database is
        never overwritten either.
    """

    target = Path(path)
    sql = load_schema_sql()

    # Claim the path with O_EXCL rather than by asking whether it exists: two
    # processes racing to create the same database would both pass an exists()
    # check, and the loser -- whose CREATE TABLE fails against the winner's
    # database -- would then unlink a database that was already in use. With
    # the claim atomic, only the process that actually created the file can
    # reach the cleanup below.
    try:
        os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError as error:
        raise ControlPlaneRefusal(
            f"{target} already exists; refusing to create over it "
            "(open_control_plane opens an existing database)"
        ) from error

    try:
        connection = sqlite3.connect(target)
    except BaseException:
        # The claim above created the file, so a connect that never returns one
        # would otherwise leave an empty file that refuses both creation (it
        # exists) and opening (it is not a database).
        target.unlink(missing_ok=True)
        raise

    try:
        connection.executescript(sql)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_REVISION}")
        connection.commit()
    except BaseException:
        # A half-created database is precisely the corrupt state R3 is about, so
        # a failed creation leaves nothing behind to be opened later.
        connection.close()
        target.unlink(missing_ok=True)
        raise
    _configure(connection)
    return connection


def open_control_plane(path: str | Path) -> sqlite3.Connection:
    """Open an existing spike database, or refuse.

    Never creates, never migrates, never repairs. Verification runs over a
    read-only connection first, so a database that fails it is not written to at
    all -- not even a rollback journal.

    :raises MissingStateRefused: if there is no file at *path*.
    :raises CorruptStateRefused: if the file is not a database this revision
        wrote and can verify.
    """

    target = Path(path)
    if not target.exists():
        raise MissingStateRefused(
            f"{target} does not exist; refusing to open (create_control_plane "
            "creates one explicitly -- an absent database is not an empty one)"
        )
    if not target.is_file():
        raise CorruptStateRefused(f"{target} is not a regular file")

    _verify_readonly(target)

    connection = sqlite3.connect(target)
    _configure(connection)
    return connection


def reconstruct(connection: sqlite3.Connection, now_ms: int) -> ControlPlaneState:
    """Rebuild the in-flight picture from the database alone (D-0001).

    *now_ms* is the caller's clock, not the database's: lease liveness is the
    one reconstruction answer that depends on time, and ACCEPTANCE.md section 2
    skews the clock across the expiry boundary on purpose.
    """

    def rows(name: str, **params: Any) -> Sequence[Mapping[str, Any]]:
        cursor = connection.execute(RECONSTRUCTION_QUERIES[name], params)
        try:
            columns = [column[0] for column in cursor.description]
            return tuple(dict(zip(columns, row)) for row in cursor.fetchall())
        finally:
            cursor.close()

    return ControlPlaneState(
        runs=rows("runs"),
        active_sessions=rows("active_sessions"),
        held_leases=rows("held_leases", now_ms=now_ms),
        unfinished_outbox=rows("unfinished_outbox"),
        unresolved_incidents=rows("unresolved_incidents"),
        pending_actions=rows("pending_actions"),
    )


# --------------------------------------------------------------------------
# verification and connection setup
# --------------------------------------------------------------------------


def _verify_readonly(target: Path) -> None:
    """Check *target* over a read-only connection, raising on the first fault."""

    uri = target.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:  # pragma: no cover - platform dependent
        raise CorruptStateRefused(f"{target} could not be opened: {error}") from error

    try:
        _verify(target, connection)
    except sqlite3.DatabaseError as error:
        # "file is not a database", a truncated header, a corrupted page read
        # while answering a pragma. All of them are refusals, never an empty
        # start (R3).
        raise CorruptStateRefused(f"{target} is not a readable database: {error}") from error
    finally:
        connection.close()


def _verify(target: Path, connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise CorruptStateRefused(f"{target} failed integrity_check: {integrity}")

    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if application_id != APPLICATION_ID:
        raise CorruptStateRefused(
            f"{target} carries application_id {application_id:#x}, not this "
            f"schema's {APPLICATION_ID:#x}; it is some other database"
        )

    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if user_version != SCHEMA_REVISION:
        raise CorruptStateRefused(
            f"{target} is at schema revision {user_version}, this build writes "
            f"{SCHEMA_REVISION}, and D-0026 promises no migration path from a "
            "spike schema; refusing rather than upgrading or starting empty"
        )

    present = {
        name
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [table for table in STATE_TABLES if table not in present]
    if missing:
        raise CorruptStateRefused(
            f"{target} is missing state table(s) {', '.join(missing)}; a database "
            "that lost a table is corrupt, not empty (R3)"
        )

    fingerprint = _schema_fingerprint(connection)
    if fingerprint != expected_schema_fingerprint():
        raise CorruptStateRefused(
            f"{target} does not carry this build's schema: a table, column, "
            "index, trigger or CHECK differs. integrity_check passes on a "
            "database that has lost a constraint, so the shape is compared "
            "outright -- and D-0026 promises no migration path, so the answer "
            "is refusal rather than repair"
        )

    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise CorruptStateRefused(
            f"{target} has {len(violations)} dangling foreign key reference(s); "
            "refusing rather than reading partial state"
        )


def expected_schema_fingerprint() -> str:
    """The fingerprint of a database freshly built from the current DDL.

    Derived by building the schema in memory rather than by keeping a constant
    beside the file, so the two cannot drift: a schema edit changes the expected
    fingerprint by construction, and every existing database is refused the
    moment the DDL changes shape -- which is what "no migration path" means in
    practice (D-0026).
    """

    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(load_schema_sql())
        return _schema_fingerprint(scratch)
    finally:
        scratch.close()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    """A digest over every schema object's own DDL text.

    ``PRAGMA integrity_check`` answers "are the pages readable?", not "is this
    the schema you wrote?" -- a database that has lost an index, a trigger or a
    CHECK passes it and then quietly permits what the lost constraint forbade.
    Names alone are not enough for the same reason, so the comparison is over
    the stored DDL of every object.
    """

    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    payload = "\n".join(f"{kind}\t{name}\t{sql or ''}" for kind, name, sql in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _configure(connection: sqlite3.Connection) -> None:
    # Foreign keys are off by default in SQLite and are per-connection, so a
    # connection that forgets this reads and writes a different schema from the
    # one the file declares.
    connection.execute("PRAGMA foreign_keys = ON")
    # D-0001 makes resume-after-kill a first-class requirement rather than an
    # error path, and a kill lands where it lands: a commit that is only in the
    # operating system's cache is a durable claim that is not durable.
    connection.execute("PRAGMA synchronous = FULL")
