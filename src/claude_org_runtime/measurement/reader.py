"""G6 -- the measurement harness's only door into the control plane: read-only by capability.

The failure this module is written against is on the record in v1's
``tools/org_metrics_report.py``. That tool's header says the ordinary connect
helper applies ``journal_mode=WAL`` and "would happily run forward migrations",
so the one program in the system that must never write -- the report -- promoted
the journal mode of the database it was reporting on and could migrate it as a
side effect of being pointed at it. Nothing in the tool was wrong; it called the
helper everything else called. Read-only was a property of how the tool was
*written*, and a property of how something is written is lost the first time
someone edits it.

``ACCEPTANCE.md`` section 3 condition 5 therefore requires the shadow path to be
read-only **enforced by capability, not by convention**, and
``docs/measurement-harness.md`` section 1 names the two enforcements: the SQLite
``mode=ro`` URI **and** ``PRAGMA query_only = ON``. ``D-0040`` records the same
pair as decided. This module is the only place the harness opens a database, and
it makes three properties structural rather than documented:

**Both mechanisms are verified in force before a row is read.** An unverified
claim of read-only is precisely the failure above -- ``mode=ro`` silently
degrading to read-write because a URI was built wrong, or a ``PRAGMA`` that was
issued and did not take, reads exactly like a harness that is behaving. So
``query_only`` is read back, and the file's own access mode is proved
behaviourally -- by offering the file a write it must refuse, with
``query_only`` momentarily off so that the connection-level guard cannot answer
in the file's place (see :func:`prove_read_only`, public so that a caller
holding a live connection evidences the capability off *that* connection rather
than off a second copy of this probe). Two independent mechanisms mean
neither one's failure is load-bearing, which is only true if each is *checked*.

**Identity, version and checksum verification is the migrator's, not a second
copy.** A spike database (``application_id`` ``ILK5``), a database behind this
build, one ahead of it, and one whose applied step bytes have changed are all
refused here with the migrator's own typed exceptions, because the harness calls
:func:`~claude_org_runtime.control_plane.migrator.verify_production_database`
rather than re-deriving the rules. A second implementation of "is this our
database" is a second thing to keep in step with ``docs/production-schema.md``
section 3, and the one that drifts is always the one nobody is looking at.

**The harness cannot simply call**
:func:`~claude_org_runtime.control_plane.migrator.open_production_control_plane`
**: that function returns a WRITABLE connection.** It never migrates -- that
separation (``D-0029``, ``production-schema.md`` section 3.2 rule 5) is exactly
what makes this module possible -- but it hands back an ordinary read-write
handle, and a read-write handle in the instrument is the v1 posture again: safe
only for as long as nobody writes through it. So the verification is shared and
the connection is not.

**Nothing here migrates and nothing here takes a lease.** Structurally, not by
promise: this module imports no writer -- not ``migrate_control_plane``, not
``create_production_control_plane``, not
:mod:`claude_org_runtime.control_plane.lease` -- and the connection it returns
cannot execute one. The harness holds no lease and no writer epoch
(``measurement-harness.md`` section 1), so there is no fenced write for a bug
here to produce. ``tests/measurement/test_reader.py`` asserts the absence of
those imports as a static property, in the same spirit as S8's no-edge
assertion, because an import added later would restore the capability without
changing a line of this docstring.

No clock is read here. The harness's periods are the caller's half-open
``[start, end)`` bounds (``time-base-policy.md`` section 2, rule 4) and this
module has no timestamp of its own to supply -- opening a database is not an
event, and nothing about it is recorded.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from claude_org_runtime.control_plane.migrator import (
    ControlPlaneRefusal,
    CorruptStateRefused,
    DatabaseAheadOfCodeRefused,
    MigrationChecksumRefused,
    MissingStateRefused,
    discover_migration_steps,
    head_version,
    verify_production_database,
)

__all__ = [
    "ControlPlaneRefusal",
    "CorruptStateRefused",
    "DatabaseAheadOfCodeRefused",
    "MigrationChecksumRefused",
    "MissingStateRefused",
    "ReadOnlyCapabilityRefused",
    "open_for_measurement",
    "prove_read_only",
]


class ReadOnlyCapabilityRefused(ControlPlaneRefusal):
    """A connection meant to be incapable of writing turned out to be capable of it.

    Its own refusal class rather than :class:`~...schema.CorruptStateRefused`
    because the fault is in **this process**, not in the file: the database may
    be perfectly healthy and the harness still has no business reading it
    through a handle that could write. The operator's next move is different
    too -- a corrupt database is restored, a harness that lost its read-only
    capability is stopped before it observes anything, because every figure it
    would go on to produce came off a connection that could have changed the
    thing it was measuring.
    """


def open_for_measurement(
    path: str | Path,
    *,
    migrations_dir: str | Path | None = None,
) -> sqlite3.Connection:
    """Open *path* for measurement: verified, read-only by capability, never migrated.

    The returned connection is the only handle the G6 harness gets. It is opened
    ``mode=ro`` with ``PRAGMA query_only = ON``, and **both** are proved in force
    before the first row is read -- ``ACCEPTANCE.md`` section 3 condition 5 asks
    for a capability, and an unverified capability is a convention with a longer
    docstring.

    The database is then held to exactly the standard
    :func:`~...migrator.open_production_control_plane` holds it to, by calling
    the same verifier: integrity, the production ``application_id``, a
    contiguous ledger, ``user_version`` agreeing with it, every applied step
    still hashing to its recorded checksum, and no dangling foreign key. A
    database at any version other than this build's head is refused rather than
    read -- the report's provenance header names the ``schema_migration`` head
    (``D-0040``), and a header naming a version whose column meanings this build
    does not have is worse than no header.

    *migrations_dir* exists so the discipline can be tested against a scratch
    ledger, exactly as in the migrator; production never points it elsewhere.

    :raises MissingStateRefused: if there is no file at *path*. An absent
        database is not an empty one, and a report over an empty one is a report
        of zero incidents.
    :raises CorruptStateRefused: for a file that is not SQLite, a failed
        ``integrity_check``, a spike or foreign ``application_id``, an absent or
        non-contiguous ledger, or a ``user_version`` disagreeing with it.
    :raises MigrationChecksumRefused: if an applied step's bytes have changed.
    :raises DatabaseAheadOfCodeRefused: if the database is ahead of this build.
    :raises ControlPlaneRefusal: if the database is *behind* this build. The
        harness never migrates, so it also never reads a stale database as
        though it were current.
    :raises ReadOnlyCapabilityRefused: if either read-only mechanism is not in
        force on the connection this function just opened, **or** if the probe
        that proves ``mode=ro`` could not reach an answer -- most often because
        another writer held the database and the probe's write came back
        ``database is locked``. An unproved capability is refused on the same
        terms as an absent one; the message distinguishes which happened.
    """

    target = Path(path)
    steps = discover_migration_steps(migrations_dir)
    if not target.exists():
        raise MissingStateRefused(
            f"{target} does not exist; the measurement harness refuses to open "
            "it (an absent database is not an empty one, and measuring an empty "
            "one reports zero of everything as though it were an observation)"
        )
    if not target.is_file():
        raise CorruptStateRefused(f"{target} is not a regular file")

    # mode=ro is the capability; the URI is built from the resolved path exactly
    # as the migrator's own read-only verification builds it, because a relative
    # path in a SQLite URI is resolved against the process's working directory
    # and would silently name a different file.
    uri = target.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as error:  # pragma: no cover - platform dependent
        raise CorruptStateRefused(f"{target} could not be opened: {error}") from error

    try:
        # Both halves are wrapped for the reason the migrator wraps its own
        # verification: "file is not a database", a truncated header or a bad
        # page read while answering a pragma all arrive as sqlite3.DatabaseError,
        # and every one of them is a refusal rather than an empty start (R3).
        # The capability proof is inside the wrapper because it reads a pragma
        # too, and a corrupt file must be refused as corrupt rather than escape
        # as a raw sqlite3 error from the harness's first line.
        try:
            _arm_and_verify_both_mechanisms(target, connection)
            applied = verify_production_database(
                target, connection, steps, require_ledger=True
            )
        except sqlite3.DatabaseError as error:
            raise CorruptStateRefused(
                f"{target} is not a readable database: {error}"
            ) from error

        current = int(applied[-1]["version"]) if applied else 0
        head = head_version(steps)
        if current != head:
            # Ahead is already refused inside the verifier, by name; reaching
            # here means behind. The harness has no migrate call to offer -- it
            # holds a connection that cannot write -- so the refusal points at
            # the operator's separate, deliberate step (D-0029).
            raise ControlPlaneRefusal(
                f"{target} is at version {current} and this build knows steps up "
                f"to {head}; the measurement harness never migrates and never "
                "reads a database at a version whose columns it does not know. "
                "Migrate it deliberately with control_plane.migrate_control_plane "
                "and point the harness at it again"
            )
    except BaseException:
        connection.close()
        raise
    return connection


# --------------------------------------------------------------------------
# proving the two mechanisms, rather than asserting them
# --------------------------------------------------------------------------


def _arm_and_verify_both_mechanisms(
    target: Path, connection: sqlite3.Connection
) -> None:
    """Put both mechanisms in force and read both back, or refuse.

    Private because it *arms* the connection it is given, which is only ever
    correct on a connection this module just opened; a caller holding someone
    else's live connection wants :func:`prove_read_only`, which only asks.

    The order matters: ``query_only`` is established and confirmed *first*, so
    that the file-mode probe below -- which has to lower ``query_only`` to ask
    its question -- is the only moment in this connection's life when the
    connection-level guard is down, and it is restored and re-read before the
    function returns.
    """

    connection.execute("PRAGMA query_only = ON")
    _require_query_only(target, connection, when="immediately after setting it")
    prove_read_only(connection, target)
    # Re-read after the probe: the probe is the one thing in this module that
    # turns the guard off, so it is also the one thing that could leave it off.
    _require_query_only(target, connection, when="after the file-mode probe")


def _require_query_only(
    target: Path, connection: sqlite3.Connection, *, when: str
) -> None:
    """Read ``PRAGMA query_only`` back and refuse anything but ``1``.

    Issuing a pragma is not the same as it taking effect: an unrecognised pragma
    name is a silent no-op in SQLite, so a typo -- ``query_ony``, ``read_only``
    -- produces a connection that reports nothing wrong and writes happily. The
    read-back is what converts that class of mistake from invisible into a
    refusal at open time.
    """

    value = connection.execute("PRAGMA query_only").fetchone()[0]
    if value != 1:
        raise ReadOnlyCapabilityRefused(
            f"PRAGMA query_only reads back as {value!r} {when} on the "
            f"measurement connection to {target}; the harness is read-only by "
            "capability (ACCEPTANCE.md section 3 condition 5) and will not "
            "observe through a handle whose guard is not in force"
        )


def prove_read_only(connection: sqlite3.Connection, target: str | Path) -> None:
    """Evidence, off *connection* itself, that the file behind it refuses writes.

    Public and taking the connection first because this is the only correct
    answer to "was **this** handle opened ``mode=ro``?", and more than one
    caller needs it. :func:`open_for_measurement` proves the capability for the
    connection it opens; ``ACCEPTANCE.md`` section 3 condition 5 asks for the
    evidence to come off the **live** connection the figures are measured
    through, which for a report already holding a connection is a different
    object. A second copy of the probe would agree with this one until one of
    the three subtleties below was fixed in one place only -- and the copy that
    drifts is the one certifying a writable handle as read-only. So there is one
    implementation and this is it.

    Returning normally is the evidence: the file refused a write **as
    read-only**. Every other outcome raises
    :class:`ReadOnlyCapabilityRefused`.

    There is no pragma that reports the access mode a database was opened with,
    and Python's ``sqlite3`` does not expose ``sqlite3_db_readonly()``, so the
    only honest read-back is behavioural: attempt the thing ``mode=ro`` is
    supposed to make impossible. ``query_only`` is lowered for the duration
    precisely because leaving it up would answer the wrong question -- the
    attempt would be refused by the connection-level guard and say nothing about
    the file, which is how a harness that has silently lost one of its two
    mechanisms goes on reporting that it has both.

    The probe is **``PRAGMA user_version`` set to the value it already holds,
    inside an explicit transaction that is always rolled back**, and all three
    of those clauses are load-bearing:

    * a *pragma* rather than a statement against a table, because the probe runs
      before verification -- the file may be a spike database, a foreign one, or
      not a database at all, and a missing table would come back as
      ``OperationalError`` too, which the probe would read as "read-only" and
      the harness would then trust a writable connection;
    * *the value it already holds*, so that the only path where the write can
      land is a path where it changes nothing;
    * *rolled back*, so that on a writable file -- the case this whole function
      exists to catch -- the page is restored and the file is left byte-identical
      with no journal surviving. ``tests/measurement/test_reader.py`` hashes the
      file across exactly that refusal.

    ``BEGIN IMMEDIATE`` alone is **not** a usable probe and was tried first: on
    this SQLite it succeeds against a ``mode=ro`` connection (the write lock is
    not taken until a page is dirtied), so it reports every read-only database as
    writable.

    A refused write is only proof when the refusal **names read-only**. The
    earlier version of this probe accepted any ``OperationalError`` as "the file
    refused it", and a writable connection whose write is blocked by another
    writer's RESERVED lock raises exactly that type with ``database is locked``
    -- the ordinary state of a control plane with a watcher or dispatcher
    mid-transaction. That reading turned a live control plane into a way of
    certifying a read-write handle as read-only, which is this module's own
    stated failure (a promise in place of a mechanism) reappearing inside the
    mechanism. An inconclusive probe is not a proof, so anything but a
    read-only error is a refusal now -- see
    :func:`_the_error_says_the_database_is_read_only`.

    **An inconclusive probe is a refusal, not a pass**, and that distinction is
    the whole defect this probe was rewritten to fix. "The capability could not
    be proved" and "the capability was absent" are two different facts: the
    refusal for contention says *inconclusive* and never says the database was
    writable, because an operator sent after the wrong one goes and fixes a URI
    that was never broken. Neither fact is a reason to go on measuring.

    The connection is left with ``query_only = ON`` however this returns -- the
    probe lowers the guard for exactly one statement and restores it in a
    ``finally``, so a caller that hands over an armed connection gets it back
    armed.

    :raises ReadOnlyCapabilityRefused: if the write was accepted (the file is
        not ``mode=ro``), or if it was refused by something other than SQLite's
        read-only error, which proves nothing either way.
    """

    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.execute("PRAGMA query_only = OFF")
    try:
        connection.execute("BEGIN")
        try:
            connection.execute(f"PRAGMA user_version = {int(user_version)}")
        except sqlite3.OperationalError as error:
            if _the_error_says_the_database_is_read_only(error):
                # The file refused the write *as read-only*: mode=ro is in
                # force, which is the answer this function came for.
                return
            # Anything else -- "database is locked" above all -- leaves the
            # question unanswered, and the refusal must say so rather than
            # report the database as writable: "the capability could not be
            # proved" and "the capability was absent" are different facts, and
            # an operator sent after the wrong one fixes the wrong thing.
            raise ReadOnlyCapabilityRefused(
                f"the read-only probe on the measurement connection to {target} "
                f"was inconclusive: the write was refused with {error!r}, which "
                "does not identify a read-only database, so it does not prove "
                "mode=ro is in force (a writable connection blocked by another "
                "writer's lock fails the same way). This is not a report that "
                "the database was writable -- it is a report that the harness "
                "could not tell. Retry when no writer holds the database, and "
                "if the error persists it is not contention (D-0040, "
                "ACCEPTANCE.md section 3 condition 5)"
            ) from error
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        raise ReadOnlyCapabilityRefused(
            f"the measurement connection to {target} accepted a write with "
            "query_only lowered, so it was not opened mode=ro -- the URI did "
            "not carry the capability it claims. The write was its own current "
            "user_version and was rolled back, so the file is unchanged; the "
            "harness stops here rather than reading, because a report is only "
            "evidence if the instrument could not have changed the thing it "
            "measured (D-0040)"
        )
    finally:
        connection.execute("PRAGMA query_only = ON")


#: SQLite's primary result code ``SQLITE_READONLY``. Extended codes carry it in
#: their low byte (``SQLITE_READONLY_DBMOVED`` = 1032 and friends), so the byte
#: is what identifies the family.
_SQLITE_READONLY = 8


def _the_error_says_the_database_is_read_only(error: sqlite3.OperationalError) -> bool:
    """Is *error* SQLite saying "read-only database", as opposed to anything else?

    Preferred mechanism is the result code, because it is what SQLite actually
    decided; the message is a rendering of it that has changed wording across
    releases. ``sqlite3.Error.sqlite3_errorcode`` exists only on Python 3.11+,
    and **this build runs 3.10** (checked: the attribute is absent), so the
    string comparison is not a defensive extra here -- it is the mechanism that
    does the work today, and the errorcode branch is what takes over silently
    when the interpreter is upgraded. Hence ``getattr`` rather than a version
    test: the code adopts the better mechanism the moment it exists.

    The string branch matches ``readonly`` only, never a generic "refused". The
    whole defect this replaces came from treating an unrecognised refusal as a
    proof, so an unrecognised message must fall through as False and be refused
    by the caller.
    """

    code = getattr(error, "sqlite3_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF == _SQLITE_READONLY
    # SQLite renders SQLITE_READONLY as "attempt to write a readonly database"
    # (and its extended codes as "... readonly database: <reason>"); no other
    # OperationalError on this path contains the token, and "database is locked"
    # notably does not.
    return "readonly" in str(error).lower() or "read-only" in str(error).lower()
