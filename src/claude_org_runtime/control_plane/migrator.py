"""The production control-plane database: numbered, forward-only migration.

This module is the enforcement arm of ``D-0029`` and
``docs/production-schema.md`` section 3, and it is a *sibling* of
:mod:`.schema` rather than a successor to it. :mod:`.schema` opens the **spike**
database, which is throwaway by default and is refused rather than migrated
(``D-0026``). This module opens the **production** database, which is authored
in :mod:`.migrations` as numbered steps and *is* migrated -- forward, one step
per transaction, never backwards. The two never meet: there is no spike-to-
production converter, and ``PRAGMA application_id`` differs between them
(:data:`PRODUCTION_APPLICATION_ID`) so that no tool can mistake one file for the
other and read a spike database as though it held production state.

Four behaviours here are load-bearing rather than convenient:

**Opening does not migrate.** :func:`open_production_control_plane` verifies and
opens; :func:`migrate_control_plane` is the only thing that writes DDL. The
separation is a recorded trap, not a preference: v1's ``tools/org_metrics_report.py``
carries a header saying the ordinary connect helper "would happily run forward
migrations", which made a read-only report tool a writer of the database it was
reporting on. ``measurement-harness.md`` section 1 depends on this separation --
the harness opens ``mode=ro`` with ``PRAGMA query_only=ON`` and can therefore be
read-only *by capability*, which ``ACCEPTANCE.md`` section 3 condition 5
requires over read-only by convention.

**One step, one transaction, with its own ledger row.** A step and the
``schema_migration`` row recording it commit together or not at all, so a failed
step leaves the database at the previous version rather than half-migrated.
This is why steps are *not* applied with ``executescript()``: that method
commits any open transaction before it runs, which would silently apply each
step outside the transaction meant to contain it (see :func:`_apply_step`).

**Every applied step is checksum-verified on every open.** The checksum is the
sha256 of the step file's bytes, recorded when the step ran. A byte difference
is a refusal naming the version, the recorded digest and the computed one.
Editing a historical migration is how two databases silently diverge while both
report the same ``version`` -- the divergence is undetectable from the version
number alone, which is exactly what makes it dangerous.

**A database ahead of the code is refused, never downgraded.** There are no down
migrations (``docs/production-schema.md`` section 3.2 rule 1): a rollback is a
restore of the database file. A reverse step that has never been exercised is a
promise the recovery path cannot keep, so none is written and none is inferred.
That refusal is bound to the connection the caller ends up holding, not only to
the read-only one used to check: verification runs again over the returned
handle, and each step re-checks the head with the write lock held, because a
rolling deployment is by definition two builds opening the same database and
the older one must not be handed a database the newer one has moved.

Corrupt state is refused, never recovered as empty (R3). That posture, the
typed-refusal family (:class:`~.schema.ControlPlaneRefusal` and its two
subclasses, imported from :mod:`.schema` rather than re-declared) and the
verify-over-a-read-only-connection discipline are carried unchanged from the
spike opener, because the failures they were written against are unchanged.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .schema import APPLICATION_ID as SPIKE_APPLICATION_ID
from .schema import ControlPlaneRefusal, CorruptStateRefused, MissingStateRefused

__all__ = [
    "LEDGER_COMPANIONS",
    "MIGRATIONS_DIR",
    "MIGRATION_BUSY_TIMEOUT_MS",
    "PRODUCTION_APPLICATION_ID",
    "SCHEMA_MIGRATION_DDL",
    "STEP_FILENAME",
    "ControlPlaneRefusal",
    "CorruptStateRefused",
    "DatabaseAheadOfCodeRefused",
    "MigrationChecksumRefused",
    "MigrationStep",
    "MigrationStepsRefused",
    "MissingStateRefused",
    "applied_migrations",
    "create_production_control_plane",
    "discover_migration_steps",
    "head_version",
    "migrate_control_plane",
    "open_production_control_plane",
    "render_current_schema",
    "verify_production_database",
]

#: Where the production DDL lives. The steps are ``.sql`` files rather than
#: strings in Python for the same reason ``spike_schema.sql`` is: an operator
#: recovering a database reads, diffs and runs them with ``sqlite3`` without
#: importing anything.
MIGRATIONS_DIR = Path(__file__).with_name("migrations")

#: ``PRAGMA application_id`` for a production database. ASCII ``ILKP``, and its
#: only requirement is that it is **distinct from the spike's** ``ILK5``
#: (:data:`~.schema.APPLICATION_ID`). The distinctness is the whole mechanism:
#: it is what stops any tool -- this module, the measurement harness, a
#: hand-run ``sqlite3`` session -- from opening a spike database as a
#: production one, finding the tables it expected to be missing, and concluding
#: it is looking at a database that merely needs migrating. ``D-0026`` forbids
#: that migration and ``D-0013`` puts the cutover at the run boundary with no
#: state conversion, so the two files must be tellable apart before a single
#: row is read.
PRODUCTION_APPLICATION_ID = 0x494C4B50

#: ``NNNN_name.sql``. The number is the ordering key and the ledger key; the
#: name is documentation carried into ``schema_migration.name`` so that a row
#: read back from a recovered database says what it was. The pattern is
#: anchored and the character set is closed because anything it does not match
#: is a refusal rather than a skipped file -- a step silently ignored for being
#: named ``0007-fix.sql`` or ``0007_fix.sql.bak`` is a schema change that never
#: happened on one database and did happen on another.
STEP_FILENAME = re.compile(r"\A(\d{4})_([a-z0-9][a-z0-9_]*)\.sql\Z")

#: The only directory entries :func:`discover_migration_steps` is allowed to
#: pass over. This is an allowlist rather than a "skip what does not end in
#: ``.sql``" rule because the interesting failure is exactly the file that ends
#: in something *else*: ``0007_fix.sql.bak`` left by an editor, ``0007_fix.sql~``
#: left by another, ``0007_fix.sql.rej`` left by a failed patch. A suffix test
#: skips all three in silence, which is the divergence
#: :data:`STEP_FILENAME`'s comment claims is refused. The line is therefore
#: drawn at *provenance*, not at spelling: these entries exist because the
#: directory is a Python package and a repository, so their presence says
#: nothing about the schema; everything else in a migrations directory is a
#: candidate step and must be named like one.
LEDGER_COMPANIONS = frozenset(
    {"__init__.py", "__pycache__", "README.md", ".gitignore", ".gitkeep"}
)

#: How long a migrating connection waits for another writer before giving up.
#: Zero -- SQLite's default -- turns any concurrent reader that happens to hold
#: the file for a few milliseconds into a failed deploy, and the retry is a
#: human re-running the same command. Two processes genuinely racing to migrate
#: still collide (``BEGIN IMMEDIATE`` takes the write lock up front and the
#: loser's wait expires against a peer that holds it for the whole migration),
#: so waiting costs nothing that the refusal was protecting.
MIGRATION_BUSY_TIMEOUT_MS = 5_000

#: The ledger, bootstrapped by the migrator itself rather than by step ``0001``.
#: A step cannot record itself in a table that does not exist yet, and making
#: ``0001`` create the table would mean the very first step is the one step
#: whose application is not evidenced by the mechanism every other step is
#: audited by. ``docs/production-schema.md`` section 3.1 is the source of this
#: DDL, immutability triggers included.
SCHEMA_MIGRATION_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version        INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    checksum       TEXT    NOT NULL,   -- sha256 of the step file's bytes
    applied_at_ms  INTEGER NOT NULL,

    CHECK (typeof(version) = 'integer' AND version > 0),
    CHECK (length(name) > 0),
    CHECK (length(checksum) = 64),
    CHECK (typeof(applied_at_ms) = 'integer')
);

CREATE TRIGGER IF NOT EXISTS schema_migration_rows_are_never_deleted
BEFORE DELETE ON schema_migration
BEGIN
    SELECT RAISE(ABORT, 'a migration record is the evidence the step ran; it is never deleted');
END;

CREATE TRIGGER IF NOT EXISTS schema_migration_rows_are_immutable
BEFORE UPDATE ON schema_migration
BEGIN
    SELECT RAISE(ABORT, 'a migration record is written once');
END;
"""


class MigrationStepsRefused(ControlPlaneRefusal):
    """The step files on disk are not a usable ledger, so nothing was applied.

    A gap in the numbering, a duplicate number, a filename the convention does
    not admit, or a step whose SQL could not be applied. Distinct from the two
    database-side refusals because the fault is in *this build*, not in the
    file the caller pointed at: no database is at fault and none was touched.
    """


class MigrationChecksumRefused(CorruptStateRefused):
    """An applied step's bytes no longer match the checksum recorded for it.

    A subclass of :class:`~.schema.CorruptStateRefused` because the answer is
    the spike's answer -- the database could not be verified, so it was not
    opened -- and because the operator's next move is the same: find out which
    of the two artefacts moved, never "run it anyway".
    """


class DatabaseAheadOfCodeRefused(CorruptStateRefused):
    """The database holds a version this build has no step for.

    Refused, never downgraded (``docs/production-schema.md`` section 3.2 rule
    4). The database has already been written by a newer build, so the tables
    this build would read may have columns it does not know and constraints it
    would violate; the only safe action is to stop and run the newer code.
    """


@dataclass(frozen=True)
class MigrationStep:
    """One ``NNNN_name.sql`` file, with the digest that pins its bytes.

    The checksum is over the **bytes** on disk, not over parsed or normalised
    SQL: normalising would forgive a whitespace edit, and the property being
    protected is that the file has not been touched at all since it ran.
    """

    version: int
    name: str
    path: Path
    checksum: str
    sql: str


def discover_migration_steps(
    directory: str | Path | None = None,
) -> tuple[MigrationStep, ...]:
    """Return every step in *directory*, ordered by version, or refuse.

    *directory* defaults to :data:`MIGRATIONS_DIR`; it is a parameter so that
    the discipline itself can be tested against a scratch ledger, not so that
    production can be pointed elsewhere.

    Discovery is strict in five ways, and each strictness replaces a silent
    skip with a refusal:

    * any entry that is not one of :data:`LEDGER_COMPANIONS` and does not match
      :data:`STEP_FILENAME` is a refusal -- a step that is not applied because
      of how it was named is a schema divergence with no error message, and
      that is as true of ``0007_fix.sql.bak`` as of ``0007-fix.sql``;
    * a duplicate version is a refusal -- two files claiming ``0007`` means the
      ledger cannot say which one ran;
    * a gap is a refusal -- versions must run ``1..N`` without holes, because a
      missing ``0004`` is most often a file that was never committed, and
      applying ``0005`` on top of its absence produces a database no other
      build can reproduce;
    * a step whose bytes are not UTF-8 is a refusal rather than a
      ``UnicodeDecodeError`` escaping the typed-refusal contract;
    * **no steps at all is a refusal.** An empty directory is not "a schema at
      version zero": it is a build whose DDL did not ship -- the wheel packaged
      without its ``.sql`` package data is the ordinary way to get here -- and
      accepting it lets :func:`create_production_control_plane` produce a
      "production" database with no control-plane tables that the opener then
      reports as at head, which is corrupt state recovered as empty
      (``docs/production-schema.md`` section 3.2 rule 6, R3).

    :raises MigrationStepsRefused: on any of the above, or if *directory* does
        not exist. An absent ledger directory is a broken build, not an empty
        migration set.
    """

    root = Path(directory) if directory is not None else MIGRATIONS_DIR
    if not root.is_dir():
        raise MigrationStepsRefused(
            f"{root} is not a directory; the production DDL ledger is missing "
            "from this build, which is not the same thing as a database with "
            "no migrations applied"
        )

    by_version: dict[int, MigrationStep] = {}
    for path in sorted(root.iterdir()):
        if path.name in LEDGER_COMPANIONS:
            continue
        match = STEP_FILENAME.match(path.name)
        if match is None:
            raise MigrationStepsRefused(
                f"{path} is not a migration step name (expected NNNN_name.sql "
                "with a four-digit version and a lower-case name, or one of "
                f"the packaging companions {sorted(LEDGER_COMPANIONS)}); "
                "refusing rather than skipping it, because a skipped step is a "
                "schema change that happened on some databases and not others"
            )
        version = int(match.group(1))
        if version < 1:
            raise MigrationStepsRefused(
                f"{path} claims version {version}; versions start at 1 "
                "(schema_migration CHECKs version > 0)"
            )
        if version in by_version:
            raise MigrationStepsRefused(
                f"{path} and {by_version[version].path} both claim version "
                f"{version}; the ledger records one row per version and cannot "
                "say which of the two ran"
            )
        payload = path.read_bytes()
        try:
            sql = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise MigrationStepsRefused(
                f"{path} is not valid UTF-8 ({error}); a step file whose bytes "
                "cannot be decoded is a corrupted or truncated artifact in this "
                "build, and applying the part that happens to decode would put "
                "half a schema change on the database"
            ) from error
        by_version[version] = MigrationStep(
            version=version,
            name=match.group(2),
            path=path,
            checksum=hashlib.sha256(payload).hexdigest(),
            sql=sql,
        )

    steps = tuple(by_version[version] for version in sorted(by_version))
    if not steps:
        raise MigrationStepsRefused(
            f"{root} contains no migration steps; a build that ships no DDL is "
            "broken, not a schema at version zero. Refusing here is what stops "
            "a 'production' database being created with no control-plane "
            "tables and then reported as at head, which is corrupt state "
            "recovered as empty (docs/production-schema.md section 3.2 rule 6)"
        )
    for offset, step in enumerate(steps, start=1):
        if step.version != offset:
            raise MigrationStepsRefused(
                f"the migration ledger jumps from {offset - 1} to "
                f"{step.version} ({step.path.name}); a hole is usually a step "
                "that was never committed, and migrating across it produces a "
                "database no other build can reproduce"
            )
    return steps


def head_version(steps: Sequence[MigrationStep] | None = None) -> int:
    """The newest version this build knows.

    ``0`` only for an explicitly empty *steps*, which is a caller's own
    construction: discovery refuses an empty ledger rather than returning one,
    so this never reports zero for a real build.
    """

    known = discover_migration_steps() if steps is None else steps
    return known[-1].version if known else 0


def applied_migrations(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    """Read the ledger back, oldest first.

    Exposed because the ledger is evidence: ``D-0040`` has the measurement
    report carry the ``schema_migration`` head in its provenance header, and it
    reads it through a read-only connection that must never migrate.
    """

    cursor = connection.execute(
        "SELECT version, name, checksum, applied_at_ms FROM schema_migration ORDER BY version"
    )
    try:
        columns = [column[0] for column in cursor.description]
        return tuple(dict(zip(columns, row)) for row in cursor.fetchall())
    finally:
        cursor.close()


def create_production_control_plane(
    path: str | Path,
    *,
    now_ms: int,
    migrations_dir: str | Path | None = None,
) -> sqlite3.Connection:
    """Create a production database at *path*, migrated to head.

    Creation is explicit and separate from opening, exactly as in the spike
    (:func:`~.schema.create_control_plane`): no code path that merely wanted to
    *read* state can end up having made a new database, which is how "a broken
    state file recovers as empty" (R3) becomes reachable by accident.

    *now_ms* is the caller's clock -- the ``applied_at_ms`` of every step
    written here comes from it. Neither ``time.time()`` nor SQLite's own clock
    is consulted anywhere in this module; ``ACCEPTANCE.md`` section 2 injects
    clock skew on purpose and a column that defaults to the database's clock
    makes that untestable.

    :raises ControlPlaneRefusal: if anything already exists at *path*, or if
        *path*'s parent directory is not there -- a database asked for inside a
        directory nobody created is a misconfigured deployment, and it reaches
        the caller as this module's refusal rather than as a raw ``OSError``.
    """

    _require_epoch_ms(now_ms)
    target = Path(path)
    steps = discover_migration_steps(migrations_dir)

    # Claim the path with O_EXCL rather than by asking whether it exists: two
    # processes racing to create the same database would both pass an exists()
    # check, and the loser -- whose migration fails against the winner's
    # database -- would then unlink a database that was already in use. With
    # the claim atomic, only the process that actually created the file can
    # reach the cleanup below.
    try:
        os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError as error:
        raise ControlPlaneRefusal(
            f"{target} already exists; refusing to create over it "
            "(open_production_control_plane opens an existing database, "
            "migrate_control_plane brings it forward)"
        ) from error
    except OSError as error:
        # A missing parent directory is the common one, and it arrives as a
        # bare FileNotFoundError that reads like a database that is absent --
        # the opposite diagnosis from the true one, which is that the path was
        # never creatable. Permission and read-only-filesystem failures are the
        # same shape of misconfiguration and get the same typed refusal.
        raise ControlPlaneRefusal(
            f"{target} could not be created ({error}); the directory it lives "
            "in must exist and be writable before a control plane is created "
            "in it"
        ) from error

    try:
        connection = sqlite3.connect(target, isolation_level=None)
    except BaseException:
        # The claim above created the file, so a connect that never returns one
        # would otherwise leave an empty file that refuses both creation (it
        # exists) and opening (it is not a database).
        target.unlink(missing_ok=True)
        raise

    try:
        connection.execute(f"PRAGMA application_id = {PRODUCTION_APPLICATION_ID}")
        _configure(connection)
        _bootstrap_ledger(connection)
        _apply_pending(connection, steps, now_ms=now_ms)
    except BaseException:
        # A half-created database is precisely the corrupt state R3 is about:
        # left on disk it would be refused by creation (it exists) and by
        # opening (it is at some version nobody applied deliberately), and the
        # operator would have to reason about which. Nothing is left behind.
        connection.close()
        target.unlink(missing_ok=True)
        raise
    return connection


def open_production_control_plane(
    path: str | Path,
    *,
    migrations_dir: str | Path | None = None,
) -> sqlite3.Connection:
    """Open an existing production database, or refuse. **Never migrates.**

    That this function does not migrate is the load-bearing part of its
    contract, not an omission. ``measurement-harness.md`` section 1 requires the
    instrument to be read-only *by capability* (``ACCEPTANCE.md`` section 3
    condition 5), and it can only be so because opening is incapable of writing
    DDL. The trap it is written against is on the record: v1's
    ``tools/org_metrics_report.py`` documents that the ordinary connect helper
    "would happily run forward migrations", so a report tool -- the one program
    in the system that must not write -- silently became a migrator of
    production. A caller that wants the database brought forward calls
    :func:`migrate_control_plane` and says so.

    Verification runs over a **read-only** connection first, so a database that
    fails it is not written to at all -- not even a rollback journal -- and then
    a second time over the writable connection this returns, because the
    read-only one is closed before that connection is opened and a newer build
    can migrate the file in the gap (a rolling deployment is exactly that gap,
    repeated). The guarantee the caller gets is therefore *the database was at
    this build's head when this handle first read it*; SQLite gives a returned
    handle no lock it could hold across the return, so a writer that moves the
    database afterwards is outside this mechanism. A caller that needs to know
    the database is *still* at head calls :func:`verify_production_database` on
    this connection at that moment.

    :raises MissingStateRefused: if there is no file at *path*.
    :raises CorruptStateRefused: for a file that is not SQLite, a failed
        ``integrity_check`` or ``foreign_key_check``, a foreign or spike
        ``application_id``, a ``user_version`` disagreeing with the ledger, or
        an absent ledger.
    :raises MigrationChecksumRefused: if an applied step's bytes have changed.
    :raises DatabaseAheadOfCodeRefused: if the database is ahead of this build.
    :raises ControlPlaneRefusal: if the database is *behind* this build. Opening
        does not migrate, so it also does not pretend a stale database is
        current; the refusal names :func:`migrate_control_plane`.
    """

    target = Path(path)
    steps = discover_migration_steps(migrations_dir)
    if not target.exists():
        raise MissingStateRefused(
            f"{target} does not exist; refusing to open "
            "(create_production_control_plane creates one explicitly -- an "
            "absent database is not an empty one)"
        )
    if not target.is_file():
        raise CorruptStateRefused(f"{target} is not a regular file")

    _refuse_unless_at_head(target, _verify_readonly(target, steps, require_ledger=True), steps)

    connection = sqlite3.connect(target, isolation_level=None)
    try:
        _configure(connection)
        # Verified again, on the handle that is actually handed back. The pass
        # above ran on a read-only connection that is closed by the time this
        # one is opened, and a rolling deployment is precisely a period in
        # which a newer build may migrate the file in that gap -- after which
        # this older build would return a writable handle to a database ahead
        # of its code, which is the one thing DatabaseAheadOfCodeRefused exists
        # to prevent (D-0029, docs/production-schema.md section 3.2 rule 1). A
        # refusal that can be false in the deployment shape it was written for
        # is not a mechanism, so the check is bound to the connection the
        # caller gets rather than to one nobody keeps.
        #
        # What this does *not* give, stated plainly because the module's
        # register is that a promise must be a mechanism: verification is a
        # read at an instant, and SQLite offers a returned handle no lock it
        # could hold open across the return. A writer can still move the
        # database after this line and before the caller's first statement.
        # The claim is therefore "this database was at this build's head when
        # this handle first read it", not "it stays there for the life of the
        # handle". A caller that needs the stronger fact calls
        # verify_production_database on this connection at the moment it needs
        # it -- that is why it takes a connection.
        _refuse_unless_at_head(
            target,
            verify_production_database(target, connection, steps, require_ledger=True),
            steps,
        )
    except BaseException:
        connection.close()
        raise
    return connection


def migrate_control_plane(
    path_or_connection: str | Path | sqlite3.Connection,
    *,
    now_ms: int,
    migrations_dir: str | Path | None = None,
) -> sqlite3.Connection:
    """Apply every unapplied step, one step per transaction, and return the connection.

    This is the *only* function in the module that writes DDL, and it is called
    deliberately or not at all (``docs/production-schema.md`` section 3.2 rule
    5). Given a path it opens the database -- which must already exist,
    creation being :func:`create_production_control_plane`'s job -- and given a
    connection it uses that one, which is how a caller that already holds the
    database migrates it without a second handle.

    Before anything is applied the existing ledger is verified in full: every
    already-applied step is re-hashed against its recorded checksum, a database
    ahead of this build is refused, and ``PRAGMA user_version`` must agree with
    ``MAX(version)``. Migration is therefore never the operation that papers
    over a divergence it should have reported. Given a path that verification
    happens twice -- read-only, then again on the writable connection, because
    another build can migrate the database between the two -- and each step
    re-checks the head with the write lock already held, which is where the
    check stops being a narrowed window and becomes a guarantee: a step is
    applied only onto the exact version this build verified.

    *now_ms* is the caller's clock and is written verbatim into
    ``applied_at_ms``. The database's own clock is never consulted: there is no
    ``DEFAULT`` on the column and no ``strftime`` anywhere in this module.

    :raises ControlPlaneRefusal: if *path_or_connection* is a connection with a
        transaction already open. Nothing is applied and nothing is committed:
        ending that transaction is the caller's decision, not a side effect of
        migrating.
    :raises MissingStateRefused: if *path_or_connection* is a path with no file.
    :raises MigrationChecksumRefused: if an applied step's bytes have changed.
    :raises DatabaseAheadOfCodeRefused: if the database is ahead of this build.
    :raises MigrationStepsRefused: if a step's SQL fails, or if another
        migrator moved the database between this call's verification and the
        step's transaction; that step's transaction is rolled back and the
        database stays at the version the other writer left it at.
    """

    _require_epoch_ms(now_ms)
    steps = discover_migration_steps(migrations_dir)

    if isinstance(path_or_connection, sqlite3.Connection):
        connection = path_or_connection
        # A transaction already open on the caller's connection is refused
        # before anything else, because the very next line would end it: with
        # the driver's implicit transaction management on, assigning
        # isolation_level = None commits whatever is open. That commit is a
        # write the caller never asked for and cannot see, inside a function
        # whose whole contract is "one step per transaction, never backwards"
        # (docs/production-schema.md section 3.2 rule 5, D-0029) -- and it
        # survives even when migration then refuses, so a refusal would have
        # persisted somebody's half-finished work, which is the opposite of
        # what a refusal means here (R3). The caller's transaction is the
        # caller's to end, so say which two ways there are to end it.
        if connection.in_transaction:
            raise ControlPlaneRefusal(
                "the connection handed to migrate_control_plane has a "
                "transaction open; commit or roll it back first, because "
                "putting the connection in autocommit mode for the migration "
                "would commit that work implicitly -- and it would stay "
                "committed even if the migration is then refused"
            )
        # The caller's connection may have been handed to us with Python's
        # implicit transaction management still on; the explicit BEGIN/COMMIT
        # of _apply_step is only honest with isolation_level = None.
        connection.isolation_level = None
        _configure(connection)
        _claim_blank_database(connection)
        verify_production_database(
            Path("<caller connection>"), connection, steps, require_ledger=False
        )
        _bootstrap_ledger(connection)
        _apply_pending(connection, steps, now_ms=now_ms)
        return connection

    target = Path(path_or_connection)
    if not target.exists():
        raise MissingStateRefused(
            f"{target} does not exist; migrating never creates "
            "(create_production_control_plane does, and stamps the "
            "application_id that says whose database it is)"
        )
    if not target.is_file():
        raise CorruptStateRefused(f"{target} is not a regular file")

    # Verified read-only first, exactly as the spike opener does: a database
    # that is going to be refused must not be written to on the way to the
    # refusal, not even a rollback journal. The ledger may legitimately be
    # absent here -- a database created and then killed before its first step
    # is behind, not corrupt -- so its bootstrap is deferred until after the
    # rest of the file has passed.
    _verify_readonly(target, steps, require_ledger=False)

    connection = sqlite3.connect(target, isolation_level=None)
    try:
        _configure(connection)
        # Same reason as in open_production_control_plane: the read-only pass
        # above is closed before this handle exists, and a newer build in a
        # rolling deploy can migrate the file in between. The no-op migration
        # is where that hides -- with the database already past this build's
        # head there is nothing to apply, so without this second pass the
        # older build would quietly be handed a writable connection to a
        # database ahead of its code instead of DatabaseAheadOfCodeRefused.
        # This narrows the window rather than closing it; what closes it for
        # the writes themselves is the head check _apply_step makes with the
        # write lock already held.
        verify_production_database(target, connection, steps, require_ledger=False)
        _bootstrap_ledger(connection)
        _apply_pending(connection, steps, now_ms=now_ms)
    except BaseException:
        connection.close()
        raise
    return connection


def render_current_schema(connection: sqlite3.Connection | None = None) -> str:
    """Emit the whole current schema as sorted DDL, for ``docs/schema-current.sql``.

    A schema whose present shape can only be learned by reading N migration
    steps in order is a schema nobody reviews, so the build emits the
    accumulated result as one file (``docs/production-schema.md`` section 3.1).
    The emitted header says what the file is, because the dangerous mistake is
    not reading it -- it is *applying* it, or editing it and expecting a
    database to follow.

    With *connection* omitted the DDL is rendered from a freshly migrated
    in-memory database, which is the definition: the schema is whatever the
    steps produce from nothing. ``applied_at_ms`` for that throwaway database is
    ``0`` -- it is never written to disk and no ledger row from it survives the
    call.
    """

    scratch: sqlite3.Connection | None = None
    if connection is None:
        scratch = sqlite3.connect(":memory:", isolation_level=None)
        connection = migrate_control_plane(scratch, now_ms=0)
    try:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        head = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        if scratch is not None:
            scratch.close()

    # Tables before the indices and triggers that reference them, so the file
    # reads top-down the way the steps do; within a kind, by name, so that a
    # diff between two generated files shows only what actually changed.
    order = {"table": 0, "view": 1, "index": 2, "trigger": 3}
    rows.sort(key=lambda row: (order.get(row[0], 9), row[1]))

    header = (
        "-- ==========================================================================\n"
        "--  GENERATED FILE -- DO NOT EDIT, AND DO NOT APPLY.\n"
        "--\n"
        "--  Emitted by control_plane.migrator.render_current_schema() from an empty\n"
        "--  database migrated to head. It is a READING AID: the production schema is\n"
        "--  the numbered, forward-only steps in\n"
        "--  src/claude_org_runtime/control_plane/migrations/, and they are the only\n"
        "--  thing that is ever applied to a database (D-0029,\n"
        "--  docs/production-schema.md section 3.1).\n"
        "--\n"
        "--  Editing this file changes no database. Running it produces a database\n"
        "--  with no schema_migration ledger, which every opener here refuses.\n"
        "--  A schema change is a new step file; this file is regenerated from it.\n"
        f"--\n--  schema_migration head: {head}\n"
        "-- ==========================================================================\n"
    )
    body = "\n".join(f"{sql.strip()};" for _, _, sql in rows)
    return f"{header}\n{body}\n"


# --------------------------------------------------------------------------
# applying steps
# --------------------------------------------------------------------------


def _bootstrap_ledger(connection: sqlite3.Connection) -> None:
    """Create ``schema_migration`` if it is not there yet.

    The migrator owns this table rather than step ``0001`` because a step
    cannot record itself in a table that does not exist. ``IF NOT EXISTS``
    everywhere makes the bootstrap idempotent across every open, which it has
    to be: it runs before each migration, including the ones with nothing to do.
    """

    connection.executescript(SCHEMA_MIGRATION_DDL)


def _claim_blank_database(connection: sqlite3.Connection) -> None:
    """Stamp the production ``application_id`` onto a database with nothing in it.

    Only :func:`create_production_control_plane` claims a *file*; this claims a
    connection the caller opened -- an in-memory database in
    :func:`render_current_schema`, or a test's scratch handle -- and it is
    deliberately unable to relabel anything: it fires only when the
    ``application_id`` is still ``0`` *and* the database holds no schema
    objects at all. A spike database, a foreign database, or a production
    database mid-history all fail one of those two conditions and fall through
    to :func:`_verify`, which refuses them by name.
    """

    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if application_id != 0:
        return
    objects = connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    if objects:
        return
    connection.execute(f"PRAGMA application_id = {PRODUCTION_APPLICATION_ID}")


def _apply_pending(
    connection: sqlite3.Connection,
    steps: Sequence[MigrationStep],
    *,
    now_ms: int,
) -> None:
    """Apply every step past the database's current version, in order."""

    connection.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")
    current = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migration"
    ).fetchone()[0]
    for step in steps:
        if step.version <= current:
            continue
        _apply_step(connection, step, now_ms=now_ms)


def _apply_step(
    connection: sqlite3.Connection,
    step: MigrationStep,
    *,
    now_ms: int,
) -> None:
    """Apply one step and record it, in one transaction.

    ``executescript()`` is deliberately not used for the step's SQL. It issues a
    ``COMMIT`` before running whatever it was given, so a step applied through
    it would land *outside* the transaction that is supposed to contain it --
    and the failure would be invisible until the day a step failed halfway and
    left the database carrying half of it. The statements are therefore split
    with :func:`sqlite3.complete_statement` (which understands that the
    semicolons inside a ``CREATE TRIGGER ... BEGIN ... END`` body are not
    statement terminators) and executed one at a time inside an explicit
    ``BEGIN``.

    ``BEGIN IMMEDIATE`` rather than a deferred begin: the write lock is taken
    up front, so two processes racing to migrate the same database collide at
    the first statement instead of at the ``COMMIT``, after one of them has
    already done its work. That ``BEGIN`` is inside the guarded region because
    it is the statement most likely to fail: it is where the collision the
    paragraph above engineers actually lands, and a collision that escapes as a
    raw ``sqlite3.OperationalError`` is exactly the untyped refusal this
    module's contract says cannot happen.

    :raises MigrationStepsRefused: if the write lock cannot be taken, or if any
        statement fails. The transaction is rolled back first, so the database
        is left at the previous version with no part of this step applied.
    """

    began = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        began = True
        # With the write lock now held, re-read the head. Everything checked
        # before this line was checked without the lock, so between that
        # verification and this transaction another migrator -- the newer half
        # of a rolling deploy -- may have moved the database. Applying this
        # step anyway would run its DDL against a shape this build never
        # verified, and the failure would surface as a raw "table already
        # exists" or a primary-key collision on schema_migration rather than as
        # a refusal that says what happened. Inside the transaction the check
        # is not a narrowed window but an actual guarantee: no other writer can
        # move the database between this SELECT and the COMMIT.
        head = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migration"
        ).fetchone()[0]
        if head != step.version - 1:
            raise MigrationStepsRefused(
                f"migration step {step.path.name} expected the database at "
                f"version {step.version - 1} but found it at {head}: another "
                "migrator moved it after this one verified it (a rolling "
                "deploy migrating the same database). Nothing was applied; "
                "re-run migrate_control_plane, which will verify the database "
                "as it now stands and refuse it if it is ahead of this build "
                "(docs/production-schema.md section 3.2 rule 1)"
            )
        for statement in _statements(step):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migration (version, name, checksum, applied_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (step.version, step.name, step.checksum, now_ms),
        )
        # user_version is part of the database header and therefore part of this
        # transaction, so the cheap check and the authoritative table cannot end
        # up disagreeing because of a crash between two commits.
        connection.execute(f"PRAGMA user_version = {step.version}")
        connection.execute("COMMIT")
    except BaseException as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if not began and isinstance(error, sqlite3.OperationalError):
            raise MigrationStepsRefused(
                f"migration step {step.path.name} could not take the write "
                f"lock within {MIGRATION_BUSY_TIMEOUT_MS} ms -- another writer "
                "holds the database, most likely a second migration of it. "
                f"Nothing was applied; the database is still at version "
                f"{step.version - 1}: {error}"
            ) from error
        if isinstance(error, sqlite3.Error):
            raise MigrationStepsRefused(
                f"migration step {step.path.name} failed and was rolled back; "
                f"the database is still at version {step.version - 1}: {error}"
            ) from error
        raise


def _statements(step: MigrationStep) -> Iterator[str]:
    """Split a step file into individually executable statements.

    Accumulates lines until SQLite itself calls the buffer complete, which is
    the only splitter that gets trigger bodies right -- a naive split on ``;``
    cuts every ``CREATE TRIGGER`` in half, and this schema is largely triggers.
    """

    buffer = ""
    for line in step.sql.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if _has_sql(buffer):
        raise MigrationStepsRefused(
            f"{step.path.name} ends in an incomplete statement (a missing "
            "semicolon, or an unterminated string or trigger body); refusing "
            "to apply a step whose tail SQLite cannot parse"
        )


def _has_sql(text: str) -> bool:
    """Whether *text* is anything other than blank lines and ``--`` comments."""

    return any(
        line.strip() and not line.strip().startswith("--") for line in text.splitlines()
    )


# --------------------------------------------------------------------------
# verification and connection setup
# --------------------------------------------------------------------------


def _verify_readonly(
    target: Path,
    steps: Sequence[MigrationStep],
    *,
    require_ledger: bool,
) -> tuple[dict[str, object], ...]:
    """Verify *target* over a read-only connection, raising on the first fault."""

    uri = target.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:  # pragma: no cover - platform dependent
        raise CorruptStateRefused(f"{target} could not be opened: {error}") from error

    try:
        return verify_production_database(target, connection, steps, require_ledger=require_ledger)
    except sqlite3.DatabaseError as error:
        # "file is not a database", a truncated header, a corrupted page read
        # while answering a pragma. All of them are refusals, never an empty
        # start (R3).
        raise CorruptStateRefused(f"{target} is not a readable database: {error}") from error
    finally:
        connection.close()


def verify_production_database(
    target: Path,
    connection: sqlite3.Connection,
    steps: Sequence[MigrationStep],
    *,
    require_ledger: bool,
) -> tuple[dict[str, object], ...]:
    """Check the file's identity and its ledger against *steps*.

    Public, and public for one reason: the G6 measurement harness
    (:mod:`claude_org_runtime.measurement.reader`) must hold a database to
    exactly this standard while opening it on a connection of its own -- one
    that is read-only by capability, which :func:`open_production_control_plane`
    is not, since it returns a writable handle. A second implementation of
    "is this our database, at our version, with our steps' bytes" would be a
    second thing to keep in step with ``docs/production-schema.md`` section 3,
    and the copy that drifts is always the one nobody is looking at. Callers
    supply the connection; nothing about it is written to.

    Returns the ledger rows so that the caller does not read them twice. With
    *require_ledger* false an absent ``schema_migration`` table is accepted as
    "nothing applied yet", which is the state a database is in between being
    created and being migrated; every other caller treats its absence as
    corruption, because a production database that lost its ledger cannot say
    what it is.
    """

    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if integrity != [("ok",)]:
        raise CorruptStateRefused(f"{target} failed integrity_check: {integrity}")

    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if application_id == SPIKE_APPLICATION_ID:
        raise CorruptStateRefused(
            f"{target} is a spike database (application_id "
            f"{SPIKE_APPLICATION_ID:#x}), not a production one. There is no "
            "migration from the spike schema and none will be written "
            "(D-0026, D-0013: the cutover is at the run boundary with no "
            "state conversion)"
        )
    if application_id != PRODUCTION_APPLICATION_ID:
        raise CorruptStateRefused(
            f"{target} carries application_id {application_id:#x}, not the "
            f"production {PRODUCTION_APPLICATION_ID:#x}; it is some other "
            "database"
        )

    tables = {
        name
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "schema_migration" not in tables and require_ledger:
        raise CorruptStateRefused(
            f"{target} has no schema_migration table; a production database "
            "without its ledger cannot say which steps it has had applied, and "
            "guessing from the tables that happen to be present is how two "
            "databases at different shapes both report the same version"
        )

    applied = applied_migrations(connection) if "schema_migration" in tables else ()
    known = {step.version: step for step in steps}
    highest = head_version(steps)

    for row in applied:
        version = int(row["version"])  # typeof CHECKed integer at insert time
        step = known.get(version)
        if step is None:
            raise DatabaseAheadOfCodeRefused(
                f"{target} has migration {version} ({row['name']}) applied and "
                f"this build knows steps only up to {highest}; refusing rather "
                "than downgrading -- there are no down migrations, and a "
                "rollback is a restore of the database file "
                "(docs/production-schema.md section 3.2 rule 1)"
            )
        if row["checksum"] != step.checksum:
            raise MigrationChecksumRefused(
                f"{target} recorded migration {version} ({row['name']}) with "
                f"checksum {row['checksum']}, but {step.path.name} now hashes "
                f"to {step.checksum}. An applied step is never edited: two "
                "databases whose histories differ would both keep reporting "
                f"version {version}, and the divergence would be invisible "
                "from the version alone"
            )
        if row["name"] != step.name:
            raise MigrationChecksumRefused(
                f"{target} recorded migration {version} as {row['name']!r} and "
                f"this build's step {version} is named {step.name!r}; the file "
                "was renamed after it was applied, which breaks the only link "
                "between a ledger row and the bytes it attests to"
            )

    current = int(applied[-1]["version"]) if applied else 0
    if len(applied) != current:
        # Contiguity on disk is checked at discovery; this is the same property
        # on the database side. A ledger with 0001 and 0003 but not 0002 is a
        # database whose shape no sequence of this build's steps can produce.
        recorded = ", ".join(str(row["version"]) for row in applied)
        raise CorruptStateRefused(
            f"{target}'s ledger is not contiguous from 1 (recorded: {recorded}); "
            "a database missing an intermediate step is not at any version this "
            "build can reason about"
        )

    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if user_version != current:
        raise CorruptStateRefused(
            f"{target} has PRAGMA user_version = {user_version} but its "
            f"schema_migration head is {current}. The table is the authority "
            "and the pragma is the cheap check (docs/production-schema.md "
            "section 3.1); a disagreement means one of them was written by "
            "something that did not write the other, so neither can be trusted"
        )

    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise CorruptStateRefused(
            f"{target} has {len(violations)} dangling foreign key reference(s); "
            "refusing rather than reading partial state"
        )
    return applied


def _refuse_unless_at_head(
    target: Path,
    applied: Sequence[dict[str, object]],
    steps: Sequence[MigrationStep],
) -> None:
    """Refuse a database that is *behind* this build's steps.

    One function rather than two copies because
    :func:`open_production_control_plane` now makes this judgement twice -- once
    read-only, once on the handle it returns -- and a check whose two copies can
    drift is a check that eventually says two different things about the same
    database.
    """

    current = int(applied[-1]["version"]) if applied else 0
    if current != head_version(steps):
        raise ControlPlaneRefusal(
            f"{target} is at version {current} and this build knows steps up "
            f"to {head_version(steps)}; opening never migrates as a side "
            "effect (D-0029), so call migrate_control_plane explicitly"
        )


def _require_epoch_ms(now_ms: int) -> None:
    """Reject a clock value that is not an integer count of milliseconds.

    ``bool`` is excluded explicitly because it is an ``int`` in Python and
    ``applied_at_ms = True`` would store ``1`` -- a timestamp of 1970 that the
    ``typeof`` CHECK cannot catch, since SQLite sees a perfectly good integer.
    """

    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise TypeError(
            f"now_ms must be an int of epoch milliseconds, got {type(now_ms).__name__}; "
            "the clock is the caller's and is never read from the database"
        )


def _configure(connection: sqlite3.Connection) -> None:
    # Foreign keys are off by default in SQLite and are per-connection, so a
    # connection that forgets this reads and writes a different schema from the
    # one the file declares.
    connection.execute("PRAGMA foreign_keys = ON")
    # D-0001 makes resume-after-kill a first-class requirement rather than an
    # error path, and a kill lands where it lands: a commit that is only in the
    # operating system's cache is a durable claim that is not durable.
    connection.execute("PRAGMA synchronous = FULL")
