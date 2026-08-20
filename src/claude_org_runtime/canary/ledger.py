"""The routing ledger -- creating, opening and refusing its database.

.. warning::

   **Item 10 rehearsal artifact (Issue #23, D-0022), throwaway by default
   (D-0026).** The marking lives in ``routing_ledger.sql`` itself and this
   module refuses to load DDL that has lost it, for the same reason S5 does:
   the failure mode is a rehearsal store quietly becoming a real one.

The ledger is a **separate SQLite file**, deliberately not a table in the S5
control-plane database. Three reasons, each load-bearing:

* The S5 schema is fingerprint-frozen and refused at any other shape
  (D-0026); the ledger must not be the edit that unfreezes it.
* The rollback property item 10 rehearses is "the run stores do not change
  when routing does". A ledger inside one of the run stores would make that
  sentence unstatable: every routing decision would be a write to a store the
  audit must show unwritten.
* The routing point sits *above* both systems (it decides which one a new
  run belongs to, before the first system-specific write), so its record
  belongs to neither system's store.

The discipline is S5's, inherited deliberately: corrupt state is refused,
never recovered as empty (R3); creation is explicit and separate from
opening; verification runs read-only so a refused file is left exactly as it
was found; and a database at another revision is refused, never migrated.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path

from claude_org_runtime.canary.marking import REHEARSAL_MARKING

__all__ = [
    "INTERLOCK",
    "LEDGER_APPLICATION_ID",
    "LEDGER_REVISION",
    "LEDGER_SCHEMA_PATH",
    "LEDGER_TABLES",
    "OWNING_SYSTEMS",
    "SYNTHETIC_V1",
    "CorruptLedgerRefused",
    "MissingLedgerRefused",
    "RoutingLedgerRefusal",
    "create_routing_ledger",
    "load_ledger_sql",
    "open_routing_ledger",
]

#: The closed owning-system vocabulary. Two values because the canary shape
#: (D-0013) has exactly two systems -- and the stand-in is named
#: ``synthetic_v1``, not ``v1``, so no record this rehearsal writes can be
#: read later as evidence obtained against the live counterparty.
INTERLOCK = "interlock"
SYNTHETIC_V1 = "synthetic_v1"
OWNING_SYSTEMS = (INTERLOCK, SYNTHETIC_V1)

#: The DDL, as a file an operator can read and diff without importing Python
#: (the same reasoning as ``spike_schema.sql``).
LEDGER_SCHEMA_PATH = Path(__file__).with_name("routing_ledger.sql")

#: ``PRAGMA application_id`` for ledger files: ASCII ``ILKC`` (canary), so a
#: ledger handed to the S5 opener -- or vice versa -- is refused as "some
#: other database" rather than reported as one with missing tables.
LEDGER_APPLICATION_ID = 0x494C4B43

#: ``PRAGMA user_version``. A ledger at any other revision is refused; there
#: is no migration (D-0026).
LEDGER_REVISION = 1

LEDGER_TABLES = ("routing_decision", "run_owner")


class RoutingLedgerRefusal(Exception):
    """A ledger database was refused. It was neither repaired nor recreated."""


class MissingLedgerRefused(RoutingLedgerRefusal):
    """No file at the path; opening never creates (an absent ledger is not an
    empty one)."""


class CorruptLedgerRefused(RoutingLedgerRefusal):
    """The file exists but could not be verified, so it was not opened."""


def _collapsed(text: str) -> str:
    """*text* with SQL comment prefixes and line breaks folded away, so the
    multi-line marking in the DDL header can be matched as one sentence."""

    return re.sub(r"\s+", " ", text.replace("\n--", "\n"))


def load_ledger_sql() -> str:
    """Return the DDL, refusing it if the rehearsal marking has been edited
    away (the marking *is* the D-0022 labelling; see :mod:`.marking`)."""

    sql = LEDGER_SCHEMA_PATH.read_text(encoding="utf-8")
    if REHEARSAL_MARKING not in _collapsed(sql):
        raise RoutingLedgerRefusal(
            f"{LEDGER_SCHEMA_PATH.name} no longer carries the rehearsal "
            "marking; refusing to apply it (D-0022, D-0026)"
        )
    return sql


def create_routing_ledger(path: str | Path) -> sqlite3.Connection:
    """Create the ledger at *path* and return an open connection.

    :raises RoutingLedgerRefusal: if anything already exists at *path*.
    """

    target = Path(path)
    sql = load_ledger_sql()

    # O_EXCL claims the path atomically, so only the process that actually
    # created the file can reach the cleanup below (the same race S5 closes).
    try:
        os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError as error:
        raise RoutingLedgerRefusal(
            f"{target} already exists; refusing to create over it"
        ) from error

    try:
        connection = sqlite3.connect(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

    try:
        connection.executescript(sql)
        connection.execute(f"PRAGMA application_id = {LEDGER_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {LEDGER_REVISION}")
        connection.commit()
    except BaseException:
        # A half-created ledger is exactly the corrupt state R3 refuses, so a
        # failed creation leaves nothing behind to be opened later.
        connection.close()
        target.unlink(missing_ok=True)
        raise
    _configure(connection)
    return connection


def open_routing_ledger(path: str | Path) -> sqlite3.Connection:
    """Open an existing ledger, or refuse. Never creates, migrates or repairs.

    :raises MissingLedgerRefused: if there is no file at *path*.
    :raises CorruptLedgerRefused: if the file is not a ledger this revision
        wrote and can verify.
    """

    target = Path(path)
    if not target.exists():
        raise MissingLedgerRefused(
            f"{target} does not exist; refusing to open "
            "(create_routing_ledger creates one explicitly)"
        )
    if not target.is_file():
        raise CorruptLedgerRefused(f"{target} is not a regular file")

    _verify_readonly(target)

    connection = sqlite3.connect(target)
    _configure(connection)
    return connection


def _verify_readonly(target: Path) -> None:
    uri = target.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:  # pragma: no cover - platform dependent
        raise CorruptLedgerRefused(f"{target} could not be opened: {error}") from error

    try:
        _verify(target, connection)
    except sqlite3.DatabaseError as error:
        raise CorruptLedgerRefused(f"{target} is not a readable database: {error}") from error
    finally:
        connection.close()


def _verify(target: Path, connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise CorruptLedgerRefused(f"{target} failed integrity_check: {integrity}")

    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if application_id != LEDGER_APPLICATION_ID:
        raise CorruptLedgerRefused(
            f"{target} carries application_id {application_id:#x}, not the "
            f"routing ledger's {LEDGER_APPLICATION_ID:#x}; it is some other database"
        )

    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if user_version != LEDGER_REVISION:
        raise CorruptLedgerRefused(
            f"{target} is at ledger revision {user_version}, this build writes "
            f"{LEDGER_REVISION}, and no migration path exists (D-0026)"
        )

    present = {
        name
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [table for table in LEDGER_TABLES if table not in present]
    if missing:
        raise CorruptLedgerRefused(
            f"{target} is missing ledger table(s) {', '.join(missing)}; a "
            "database that lost a table is corrupt, not empty (R3)"
        )

    # The immutability triggers ARE the ledger's guarantees, and a database
    # that has lost one passes integrity_check -- so the shape is compared
    # outright, the same way S5 compares its own.
    if _schema_fingerprint(connection) != expected_ledger_fingerprint():
        raise CorruptLedgerRefused(
            f"{target} does not carry this build's ledger schema: a table, "
            "trigger or CHECK differs, and losing a trigger here is losing "
            "the mid-flight immutability itself; refusing rather than reading"
        )

    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise CorruptLedgerRefused(
            f"{target} has {len(violations)} dangling foreign key reference(s); "
            "refusing rather than reading partial state"
        )


def expected_ledger_fingerprint() -> str:
    """The fingerprint of a ledger freshly built from the current DDL,
    derived rather than kept as a constant so the two cannot drift."""

    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(load_ledger_sql())
        return _schema_fingerprint(scratch)
    finally:
        scratch.close()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    payload = "\n".join(f"{kind}\t{name}\t{sql or ''}" for kind, name, sql in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
