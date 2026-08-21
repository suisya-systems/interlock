"""The production migrator -- every rule of production-schema.md section 3.2.

Section 3.2 is six numbered rules, and each of them is here as an executable
property rather than as prose: forward-only with no reverse step, one step per
transaction, checksum verification on every open, refusal of a database ahead
of the code, migration only by an explicit call, and corrupt state refused
rather than recovered as empty. They are written the way
``test_spike_schema.py`` is written -- against the artifact, not against a
description of it -- because the failures they guard are silent ones: a step
skipped for being misnamed, a historical step edited after it ran, an opener
that quietly migrates the database a read-only report was pointed at. None of
those announce themselves, so the test has to be the thing that notices.

Most cases run against a **scratch ledger** in ``tmp_path`` rather than against
the real ``migrations/`` directory. The discipline under test is the migrator's,
not ``0001_initial.sql``'s: a scratch ledger can be given a hole, a duplicate
number, or a step that fails halfway, and the real one deliberately cannot.
``migrations_dir`` exists as a parameter for exactly this and for nothing else.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import migrator as m
from claude_org_runtime.control_plane.migrator import (
    MIGRATIONS_DIR,
    PRODUCTION_APPLICATION_ID,
    ControlPlaneRefusal,
    CorruptStateRefused,
    DatabaseAheadOfCodeRefused,
    MigrationChecksumRefused,
    MigrationStepsRefused,
    MissingStateRefused,
    applied_migrations,
    create_production_control_plane,
    discover_migration_steps,
    head_version,
    migrate_control_plane,
    open_production_control_plane,
    render_current_schema,
)
from claude_org_runtime.control_plane.schema import APPLICATION_ID as SPIKE_APPLICATION_ID
from claude_org_runtime.control_plane.schema import (
    create_control_plane,
    open_control_plane,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
T1 = T0 + 60_000
T2 = T0 + 120_000


# --------------------------------------------------------------------------
# helpers -- a scratch ledger whose steps can be as broken as the case needs
# --------------------------------------------------------------------------


def write_step(directory: Path, filename: str, sql: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(sql, encoding="utf-8")
    return path


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """A two-step scratch ledger: ``alpha`` at 0001, ``beta`` at 0002."""

    directory = tmp_path / "ledger"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER PRIMARY KEY);\n")
    write_step(directory, "0002_beta.sql", "CREATE TABLE beta (id INTEGER PRIMARY KEY);\n")
    return directory


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "production.sqlite3"


def raw(path: Path) -> sqlite3.Connection:
    """A connection with none of the module's discipline, for inspection and sabotage."""

    return sqlite3.connect(path, isolation_level=None)


def tables_of(path: Path) -> set[str]:
    connection = raw(path)
    try:
        return {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()


def rows_of(path: Path, table: str) -> list[tuple]:
    """Every row of *table* read back over a fresh connection, to see what committed."""

    connection = raw(path)
    try:
        return connection.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        connection.close()


def version_of(path: Path) -> tuple[int, int]:
    """``(MAX(schema_migration.version), PRAGMA user_version)`` -- the authority and the cheap check."""

    connection = raw(path)
    try:
        ledger_head = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migration"
        ).fetchone()[0]
        pragma = connection.execute("PRAGMA user_version").fetchone()[0]
        return ledger_head, pragma
    finally:
        connection.close()


def sidecars(path: Path) -> list[Path]:
    """Journal and WAL files -- evidence that a "refused" open in fact wrote."""

    return sorted(path.parent.glob(f"{path.name}-*"))


# --------------------------------------------------------------------------
# rule 1 -- forward-only. There are no down migrations.
# --------------------------------------------------------------------------

_REVERSE_WORDS = ("down", "rollback", "revert", "unapply", "undo", "reverse", "backward")


def test_the_module_exposes_no_down_migration_api():
    # A reverse step that has never been exercised is a promise the recovery
    # path cannot keep (section 3.2 rule 1), so the guarantee is asserted as an
    # absence of surface rather than as a docstring: there is nothing a caller
    # could reach for even by accident. A rollback is a restore of the file.
    public = [name for name in dir(m) if not name.startswith("_")]
    offenders = [
        name for name in public if any(word in name.lower() for word in _REVERSE_WORDS)
    ]
    assert offenders == []
    assert set(m.__all__) <= set(public)


def test_no_step_file_carries_a_reverse_half():
    # The other place a down migration could hide: a paired 0003_x.down.sql, or
    # a step whose own name advertises an undo. Discovery would refuse the
    # former by filename anyway; this says it is not written in the first place.
    for path in MIGRATIONS_DIR.iterdir():
        assert not any(word in path.name.lower() for word in _REVERSE_WORDS), path


# --------------------------------------------------------------------------
# rule 2 -- one step, one transaction
# --------------------------------------------------------------------------


def test_a_step_whose_second_statement_fails_leaves_no_trace_of_its_first(ledger, db_path):
    # The precise failure this guards: a step applied statement-by-statement
    # OUTSIDE a transaction leaves the database carrying the half that ran, at
    # a version nobody applied. Two statements, the second unrunnable.
    write_step(
        ledger,
        "0003_half.sql",
        "CREATE TABLE gamma (id INTEGER PRIMARY KEY);\n"
        "INSERT INTO no_such_table (id) VALUES (1);\n",
    )

    with pytest.raises(MigrationStepsRefused, match="0003_half.sql"):
        create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)

    # create() unlinks a database it could not finish, so the surviving
    # evidence is that nothing was left behind at all.
    assert not db_path.exists()

    # Now the same failure against a database that already exists, where the
    # previous version is a real place to be left at rather than nothing.
    good = ledger.parent / "good"
    write_step(good, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text(encoding="utf-8"))
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=good)
    connection.close()

    # 0002 applies and commits; 0003 fails and is rolled back, so the database
    # is left at 2 -- the previous version, not the one that failed.
    with pytest.raises(MigrationStepsRefused, match="still at version 2"):
        migrate_control_plane(db_path, now_ms=T1, migrations_dir=ledger)

    assert version_of(db_path) == (2, 2)
    present = tables_of(db_path)
    assert "alpha" in present
    # 0002 ran in a transaction of its own and stays committed; only the failed
    # step's half is rolled back, which is what "one step, one transaction"
    # means as distinct from "one migration call, one transaction".
    assert "beta" in present
    assert "gamma" not in present
    assert [row["version"] for row in _ledger_rows(db_path)] == [1, 2]


def _ledger_rows(path: Path) -> tuple[dict[str, object], ...]:
    connection = raw(path)
    try:
        return applied_migrations(connection)
    finally:
        connection.close()


def test_each_step_commits_with_its_own_ledger_row(ledger, db_path):
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    connection.close()

    rows = _ledger_rows(db_path)
    assert [row["version"] for row in rows] == [1, 2]
    assert [row["name"] for row in rows] == ["alpha", "beta"]
    # applied_at_ms is the caller's clock verbatim: no DEFAULT, no strftime.
    assert {row["applied_at_ms"] for row in rows} == {T0}
    for row in rows:
        step = (ledger / f"{row['version']:04d}_{row['name']}.sql").read_bytes()
        assert row["checksum"] == hashlib.sha256(step).hexdigest()


# --------------------------------------------------------------------------
# rule 3 -- an applied step is checksum-verified on every open
# --------------------------------------------------------------------------


def test_an_applied_step_whose_bytes_changed_is_refused_naming_both_checksums(ledger, db_path):
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    connection.close()
    recorded = _ledger_rows(db_path)[0]["checksum"]

    step = ledger / "0001_alpha.sql"
    step.write_text(
        step.read_text(encoding="utf-8") + "-- a clarifying comment, added later\n",
        encoding="utf-8",
    )
    now_hashes_to = hashlib.sha256(step.read_bytes()).hexdigest()
    assert now_hashes_to != recorded

    with pytest.raises(MigrationChecksumRefused) as refusal:
        open_production_control_plane(db_path, migrations_dir=ledger)

    # Both digests, because the operator's question is which of the two
    # artifacts moved, and an error naming only one cannot answer it.
    message = str(refusal.value)
    assert recorded in message
    assert now_hashes_to in message
    assert "0001_alpha.sql" in message

    # And migrating is not the escape hatch: the divergence is reported before
    # anything is applied, so migration never papers over it.
    with pytest.raises(MigrationChecksumRefused):
        migrate_control_plane(db_path, now_ms=T1, migrations_dir=ledger)


def test_renaming_an_applied_step_is_the_same_refusal(ledger, db_path):
    # The rename breaks the only link between a ledger row and the bytes it
    # attests to, even when those bytes are untouched.
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    connection.close()
    (ledger / "0001_alpha.sql").rename(ledger / "0001_alpha_renamed.sql")

    with pytest.raises(MigrationChecksumRefused, match="renamed"):
        open_production_control_plane(db_path, migrations_dir=ledger)


def test_a_checksum_refusal_does_not_write_to_the_database(ledger, db_path):
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    connection.close()
    before = db_path.read_bytes()
    step = ledger / "0002_beta.sql"
    step.write_text(step.read_text(encoding="utf-8") + "-- edited\n", encoding="utf-8")

    with pytest.raises(MigrationChecksumRefused):
        open_production_control_plane(db_path, migrations_dir=ledger)

    # Verification runs over a read-only connection precisely so that a
    # database on its way to being refused is not written to -- not even a
    # rollback journal it would then have to recover from.
    assert db_path.read_bytes() == before
    assert sidecars(db_path) == []


# --------------------------------------------------------------------------
# rule 4 -- a database ahead of the code is refused, never downgraded
# --------------------------------------------------------------------------


def test_a_database_ahead_of_the_code_is_refused_and_left_at_its_own_version(ledger, db_path):
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    connection.close()

    older_build = ledger.parent / "older-build"
    write_step(older_build, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text(encoding="utf-8"))
    assert head_version(discover_migration_steps(older_build)) == 1

    with pytest.raises(DatabaseAheadOfCodeRefused, match="only up to 1"):
        open_production_control_plane(db_path, migrations_dir=older_build)
    with pytest.raises(DatabaseAheadOfCodeRefused):
        migrate_control_plane(db_path, now_ms=T1, migrations_dir=older_build)

    # Never downgraded: the newer build's table is still there, the ledger row
    # for it is still there, and the version has not moved.
    assert version_of(db_path) == (2, 2)
    assert "beta" in tables_of(db_path)
    assert [row["version"] for row in _ledger_rows(db_path)] == [1, 2]


# --------------------------------------------------------------------------
# rule 5 -- a database behind the code is migrated, and only by an explicit call
# --------------------------------------------------------------------------


def test_opening_a_database_behind_the_code_refuses_instead_of_migrating(ledger, db_path):
    first_only = ledger.parent / "first-only"
    write_step(first_only, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text(encoding="utf-8"))
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=first_only)
    connection.close()
    before = db_path.read_bytes()

    with pytest.raises(ControlPlaneRefusal, match="migrate_control_plane"):
        open_production_control_plane(db_path, migrations_dir=ledger)

    # This is the measurement harness's read-only guarantee (D-0040,
    # measurement-harness.md section 1) reduced to its one testable fact: the
    # opener is incapable of writing DDL, so a report tool pointed at a stale
    # production database cannot become the thing that migrated it. v1's
    # org_metrics_report.py documents that exact accident.
    assert db_path.read_bytes() == before
    assert version_of(db_path) == (1, 1)
    assert "beta" not in tables_of(db_path)
    assert sidecars(db_path) == []


def test_the_explicit_call_is_what_migrates_and_then_opening_succeeds(ledger, db_path):
    first_only = ledger.parent / "first-only"
    write_step(first_only, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text(encoding="utf-8"))
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=first_only).close()

    migrated = migrate_control_plane(db_path, now_ms=T1, migrations_dir=ledger)
    try:
        assert version_of(db_path) == (2, 2)
        assert "beta" in tables_of(db_path)
        rows = _ledger_rows(db_path)
        # The clock of each step is the clock of the call that applied it, so
        # the ledger says when each step ran rather than when the file was last
        # touched.
        assert [row["applied_at_ms"] for row in rows] == [T0, T1]
    finally:
        migrated.close()

    opened = open_production_control_plane(db_path, migrations_dir=ledger)
    try:
        assert opened.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        opened.close()


def test_migrating_an_at_head_database_twice_is_a_no_op(ledger, db_path):
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()
    before = _ledger_rows(db_path)

    migrate_control_plane(db_path, now_ms=T1, migrations_dir=ledger).close()

    after = _ledger_rows(db_path)
    assert after == before
    assert len(after) == 2
    # T1 appears nowhere: a no-op migration writes no row, so the ledger cannot
    # accumulate one entry per process start and misreport when a step ran.
    assert T1 not in {row["applied_at_ms"] for row in after}
    assert version_of(db_path) == (2, 2)


def test_migrating_never_creates_a_database(ledger, db_path):
    with pytest.raises(MissingStateRefused):
        migrate_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    assert not db_path.exists()


# --------------------------------------------------------------------------
# the ledger is evidence: immutable and undeletable
# --------------------------------------------------------------------------


def test_a_schema_migration_row_cannot_be_updated(ledger, db_path):
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="written once"):
            connection.execute(
                "UPDATE schema_migration SET checksum = ? WHERE version = 1", ("0" * 64,)
            )
        assert _row_count(connection) == 2
    finally:
        connection.close()


def test_a_schema_migration_row_cannot_be_deleted(ledger, db_path):
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    try:
        # Deleting the row is the other way to make an edited step verify: with
        # no row there is no recorded checksum to contradict the new bytes.
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            connection.execute("DELETE FROM schema_migration WHERE version = 1")
        assert _row_count(connection) == 2
    finally:
        connection.close()


def _row_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0]


# --------------------------------------------------------------------------
# discovery -- a badly formed ledger is a refusal, never a silent skip
# --------------------------------------------------------------------------


def test_a_numbering_gap_is_refused(tmp_path):
    directory = tmp_path / "gap"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER);\n")
    write_step(directory, "0003_gamma.sql", "CREATE TABLE gamma (id INTEGER);\n")

    with pytest.raises(MigrationStepsRefused, match="jumps from 1 to 3"):
        discover_migration_steps(directory)


def test_a_duplicate_version_prefix_is_refused(tmp_path):
    directory = tmp_path / "dupe"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER);\n")
    write_step(directory, "0002_beta.sql", "CREATE TABLE beta (id INTEGER);\n")
    write_step(directory, "0002_beta_again.sql", "CREATE TABLE beta2 (id INTEGER);\n")

    with pytest.raises(MigrationStepsRefused, match="both claim version 2"):
        discover_migration_steps(directory)


@pytest.mark.parametrize(
    "filename",
    [
        "0002-fix.sql",  # hyphen where the convention has an underscore
        "0002_Fix.sql",  # upper case, which sorts and reads differently
        "two_fix.sql",  # no version at all
        "002_fix.sql",  # three digits: sorts wrong the moment there are 10 steps
        "0002_.sql",  # a version with no name
    ],
)
def test_a_malformed_step_filename_is_refused_not_skipped(tmp_path, filename):
    directory = tmp_path / "malformed"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER);\n")
    write_step(directory, filename, "CREATE TABLE other (id INTEGER);\n")

    # Skipping it would be the dangerous outcome: a schema change that happened
    # on the databases whose operator ran the file by hand and not on the rest,
    # with no error anywhere to say so.
    with pytest.raises(MigrationStepsRefused, match="not a migration step name"):
        discover_migration_steps(directory)


def test_a_step_numbered_zero_is_refused(tmp_path):
    directory = tmp_path / "zero"
    write_step(directory, "0000_zero.sql", "CREATE TABLE zero (id INTEGER);\n")

    with pytest.raises(MigrationStepsRefused, match="versions start at 1"):
        discover_migration_steps(directory)


def test_an_absent_ledger_directory_is_a_broken_build_not_an_empty_ledger(tmp_path):
    with pytest.raises(MigrationStepsRefused, match="is not a directory"):
        discover_migration_steps(tmp_path / "nowhere")


def test_an_empty_ledger_directory_is_a_broken_build_not_a_schema_at_version_zero(
    tmp_path, db_path
):
    # The wheel that shipped without its .sql package data lands here: the
    # directory exists because it is a Python package, and every step in it is
    # gone. Treated as an empty migration set it would produce a "production"
    # database with no control-plane tables in it whose version -- 0 -- equals
    # this build's head, so the opener would then call it current. That is
    # corrupt state recovered as empty (section 3.2 rule 6, R3).
    directory = tmp_path / "empty"
    directory.mkdir()
    (directory / "__init__.py").write_text("", encoding="utf-8")

    with pytest.raises(MigrationStepsRefused, match="contains no migration steps"):
        discover_migration_steps(directory)

    with pytest.raises(MigrationStepsRefused, match="contains no migration steps"):
        create_production_control_plane(db_path, now_ms=T0, migrations_dir=directory)
    assert not db_path.exists()


@pytest.mark.parametrize(
    "filename",
    [
        "0002_beta.sql.bak",  # an editor's backup of a real step
        "0002_beta.sql~",  # another editor's
        "0002_beta.sql.rej",  # a patch that did not apply
        "notes.txt",  # anything else that is not a packaging companion
    ],
)
def test_a_file_that_looks_like_a_step_but_is_not_one_is_refused_not_skipped(
    tmp_path, filename
):
    # A suffix test would pass over all of these in silence, which is the very
    # divergence STEP_FILENAME's comment claims is refused: the operator who
    # ran 0002_beta.sql.bak by hand has a database this build cannot reproduce.
    directory = tmp_path / "leftovers"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER);\n")
    write_step(directory, filename, "CREATE TABLE beta (id INTEGER);\n")

    with pytest.raises(MigrationStepsRefused, match="not a migration step name"):
        discover_migration_steps(directory)


def test_the_packaging_companions_are_still_skipped(tmp_path):
    directory = tmp_path / "packaged"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER);\n")
    (directory / "__init__.py").write_text("", encoding="utf-8")
    (directory / "README.md").write_text("the ledger\n", encoding="utf-8")
    (directory / "__pycache__").mkdir()

    assert [step.name for step in discover_migration_steps(directory)] == ["alpha"]


def test_a_step_whose_bytes_are_not_utf8_is_a_typed_refusal(tmp_path):
    directory = tmp_path / "mojibake"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER);\n")
    (directory / "0002_beta.sql").write_bytes(b"CREATE TABLE beta (id INTEGER); -- \xff\n")

    # Not a UnicodeDecodeError: every fault in this build reaches the caller as
    # the module's refusal family, or the caller cannot handle them uniformly.
    with pytest.raises(MigrationStepsRefused, match="not valid UTF-8"):
        discover_migration_steps(directory)


def test_a_refused_ledger_creates_no_database(tmp_path, db_path):
    directory = tmp_path / "gap"
    write_step(directory, "0002_beta.sql", "CREATE TABLE beta (id INTEGER);\n")

    with pytest.raises(MigrationStepsRefused):
        create_production_control_plane(db_path, now_ms=T0, migrations_dir=directory)
    assert not db_path.exists()


def test_a_step_ending_in_an_incomplete_statement_is_refused(tmp_path, db_path):
    directory = tmp_path / "truncated"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (\n    id INTEGER\n")

    with pytest.raises(MigrationStepsRefused, match="incomplete statement"):
        create_production_control_plane(db_path, now_ms=T0, migrations_dir=directory)
    assert not db_path.exists()


# --------------------------------------------------------------------------
# rule 6 -- corrupt state is refused, never recovered as empty
# --------------------------------------------------------------------------


def test_user_version_disagreeing_with_the_ledger_head_is_refused(ledger, db_path):
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()
    connection = raw(db_path)
    try:
        # The pragma is the cheap check and the table is the authority
        # (section 3.1); a disagreement means one was written by something that
        # did not write the other, so neither can be trusted afterwards.
        connection.execute("PRAGMA user_version = 1")
    finally:
        connection.close()

    with pytest.raises(CorruptStateRefused, match="user_version"):
        open_production_control_plane(db_path, migrations_dir=ledger)
    assert version_of(db_path) == (2, 1)


def test_a_ledger_with_a_hole_is_refused(ledger, db_path):
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()
    connection = raw(db_path)
    try:
        # The delete trigger is the front door; this reaches around it the way
        # a hand-run sqlite3 session would, to prove the opener does not depend
        # on the trigger having been in force when the damage was done.
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "DELETE FROM sqlite_master WHERE name = 'schema_migration_rows_are_never_deleted'"
        )
        connection.execute("PRAGMA writable_schema = OFF")
    finally:
        connection.close()
    connection = raw(db_path)
    try:
        connection.execute("DELETE FROM schema_migration WHERE version = 1")
    finally:
        connection.close()

    with pytest.raises(CorruptStateRefused, match="not contiguous"):
        open_production_control_plane(db_path, migrations_dir=ledger)


def test_a_production_database_without_its_ledger_is_refused_not_rebuilt(ledger, db_path):
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()
    connection = raw(db_path)
    try:
        connection.execute("DROP TABLE schema_migration")
    finally:
        connection.close()

    with pytest.raises(CorruptStateRefused, match="no schema_migration table"):
        open_production_control_plane(db_path, migrations_dir=ledger)
    # Not rebuilt behind the caller's back, and the rows it did hold survive.
    assert "schema_migration" not in tables_of(db_path)
    assert "alpha" in tables_of(db_path)


def test_an_absent_database_is_refused_and_not_created(ledger, db_path):
    with pytest.raises(MissingStateRefused):
        open_production_control_plane(db_path, migrations_dir=ledger)
    assert not db_path.exists()


def test_a_file_that_is_not_a_database_is_refused_and_left_alone(ledger, db_path):
    db_path.write_bytes(b"not a database, just a note someone left in the state directory")
    before = db_path.read_bytes()

    with pytest.raises(CorruptStateRefused):
        open_production_control_plane(db_path, migrations_dir=ledger)

    assert db_path.read_bytes() == before
    assert sidecars(db_path) == []


def test_creating_under_a_missing_parent_directory_is_a_typed_refusal(ledger, tmp_path):
    # os.open's bare FileNotFoundError reads as "the database is absent", which
    # is the opposite diagnosis from the true one: the path was never creatable
    # because nobody made the directory it lives in.
    target = tmp_path / "nonexistent" / "production.sqlite3"

    with pytest.raises(ControlPlaneRefusal, match="could not be created"):
        create_production_control_plane(target, now_ms=T0, migrations_dir=ledger)
    assert not target.exists()


def test_a_write_lock_held_by_another_writer_is_a_typed_refusal(
    ledger, db_path, monkeypatch
):
    # The collision BEGIN IMMEDIATE is chosen to force, forced for real: a
    # second connection holds the write lock for the whole attempt. It must
    # arrive as this module's refusal rather than as a raw OperationalError,
    # and it must arrive only after the busy_timeout has actually been waited
    # out -- a deploy that fails instantly on a racing reader is the failure
    # the timeout exists to prevent.
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()
    write_step(ledger, "0003_gamma.sql", "CREATE TABLE gamma (id INTEGER);\n")
    monkeypatch.setattr(m, "MIGRATION_BUSY_TIMEOUT_MS", 250)

    blocker = raw(db_path)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("CREATE TABLE squatter (id INTEGER)")
    try:
        started = time.monotonic()
        with pytest.raises(MigrationStepsRefused, match="could not take the write lock"):
            migrate_control_plane(db_path, now_ms=T1, migrations_dir=ledger)
        waited_ms = (time.monotonic() - started) * 1000
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert waited_ms >= 200  # it waited, rather than failing the deploy at once
    assert version_of(db_path) == (2, 2)
    assert "gamma" not in tables_of(db_path)


def test_creating_over_an_existing_path_is_refused(ledger, db_path):
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()
    before = db_path.read_bytes()

    with pytest.raises(ControlPlaneRefusal, match="already exists"):
        create_production_control_plane(db_path, now_ms=T1, migrations_dir=ledger)
    assert db_path.read_bytes() == before


@pytest.mark.parametrize("bad_clock", [None, 1.5, "1700000000000", True])
def test_the_clock_must_be_an_integer_of_epoch_milliseconds(ledger, db_path, bad_clock):
    # True is in the list on purpose: it is an int in Python, so a bool would
    # store 1 -- a timestamp in 1970 that the typeof CHECK cannot catch,
    # because SQLite sees a perfectly good integer.
    with pytest.raises(TypeError):
        create_production_control_plane(db_path, now_ms=bad_clock, migrations_dir=ledger)
    assert not db_path.exists()


# --------------------------------------------------------------------------
# the two databases are never mistaken for one another
# --------------------------------------------------------------------------


def test_the_production_application_id_differs_from_the_spike(ledger, db_path):
    assert PRODUCTION_APPLICATION_ID != SPIKE_APPLICATION_ID
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    try:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == PRODUCTION_APPLICATION_ID
    finally:
        connection.close()


def test_a_spike_database_is_refused_by_the_production_opener(ledger, tmp_path):
    spike = tmp_path / "spike.sqlite3"
    create_control_plane(spike).close()

    # There is no migration from the spike schema and none will be written
    # (D-0026, D-0013: the cutover is at the run boundary with no state
    # conversion), so the refusal has to be by identity -- before a single row
    # is read and before the missing tables can be mistaken for "needs
    # migrating".
    with pytest.raises(CorruptStateRefused, match="spike database"):
        open_production_control_plane(spike, migrations_dir=ledger)
    with pytest.raises(CorruptStateRefused, match="spike database"):
        migrate_control_plane(spike, now_ms=T0, migrations_dir=ledger)


def test_a_production_database_is_refused_by_the_spike_opener(ledger, db_path):
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()

    with pytest.raises(CorruptStateRefused, match="application_id"):
        open_control_plane(db_path)


def test_a_foreign_database_is_refused(ledger, tmp_path):
    other = tmp_path / "someone-elses.sqlite3"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(CorruptStateRefused, match="application_id"):
        open_production_control_plane(other, migrations_dir=ledger)


def test_migrating_a_foreign_database_does_not_relabel_it(ledger, tmp_path):
    # _claim_blank_database stamps only a database that is both unstamped and
    # empty; anything with objects in it falls through to the refusal by name,
    # so migration can never be the operation that adopts someone else's file.
    other = tmp_path / "someone-elses.sqlite3"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(CorruptStateRefused, match="application_id"):
        migrate_control_plane(other, now_ms=T0, migrations_dir=ledger)

    inspect = raw(other)
    try:
        assert inspect.execute("PRAGMA application_id").fetchone()[0] == 0
        assert "schema_migration" not in tables_of(other)
    finally:
        inspect.close()


# --------------------------------------------------------------------------
# the real ledger, and the generated reading aid
# --------------------------------------------------------------------------


def test_a_step_that_leaves_a_dangling_reference_is_refused_and_rolled_back(ledger, db_path):
    """Foreign keys are checked per step, not per statement, and that is the trade.

    The rebuild in ``0003_outbox_cancelled_status.sql`` needs
    ``PRAGMA foreign_keys = OFF`` -- SQLite cannot alter a CHECK, and the
    documented table rebuild drops a table three others reference -- and that
    pragma does nothing inside a transaction, so it is issued around the whole
    migration. What replaces the per-statement enforcement is a whole-database
    ``PRAGMA foreign_key_check`` inside each step's own transaction, and this is
    the test that it actually refuses: without it, turning the pragma off would
    be a hole in every step rather than a licence for one.
    """

    write_step(ledger, "0003_child.sql",
               "CREATE TABLE child (id INTEGER PRIMARY KEY,"
               " parent INTEGER REFERENCES alpha(id));\n"
               "INSERT INTO child (id, parent) VALUES (1, 404);\n")

    with pytest.raises(MigrationStepsRefused, match="foreign key violation"):
        create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)

    assert not db_path.exists()


def test_the_migrating_connection_ends_with_foreign_keys_enforced(ledger, db_path):
    # The pragma is turned off for the duration of the migration and must not
    # leak into the handle the caller goes on to write through: a connection
    # that silently does not enforce foreign keys is the failure the whole
    # _configure block exists to prevent.
    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        connection.close()

    reopened = open_production_control_plane(db_path, migrations_dir=ledger)
    try:
        assert reopened.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        reopened.close()


def test_the_outbox_rebuild_carries_every_row_and_every_reference_forward(db_path):
    """0003 is a table rebuild, and a rebuild that loses a row loses evidence.

    Migrated in two halves deliberately: the database is created at 0002 from a
    copy of the shipped steps, rows and a child reference are written into it,
    and only then is the real ledger applied. A rebuild verified only against an
    empty database proves nothing about the ``INSERT INTO ... SELECT`` at its
    centre.
    """

    shipped = MIGRATIONS_DIR
    at_0002 = db_path.parent / "at-0002"
    for name in ("0001_initial.sql", "0002_policy_seed.sql"):
        write_step(at_0002, name, (shipped / name).read_text(encoding="utf-8"))

    connection = create_production_control_plane(db_path, now_ms=T0, migrations_dir=at_0002)
    try:
        connection.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
            " VALUES ('run-1', 'running', ?, ?)", (T0, T0))
        connection.execute(
            "INSERT INTO outbox (message_id, run_id, recipient, payload, dedup_key,"
            "                    status, retry_count, enqueued_at_ms, delivered_at_ms)"
            " VALUES ('msg-1', 'run-1', 'secretary', '{}', 'dk-1', 'delivered', 4, ?, ?)",
            (T0, T0 + 1))
        # A child of outbox, in the shape the three shipped referrers have --
        # event_consumption, gate_transition and gate_relay all carry
        # REFERENCES outbox(message_id). It is written by hand here rather than
        # through one of them because each of those needs a gate or an event
        # around it, and what is under test is the reference, not their rows.
        assert {
            table
            for (table,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
            if any(row[2] == "outbox"
                   for row in connection.execute(f"PRAGMA foreign_key_list({table})"))
        } == {"event_consumption", "gate_transition", "gate_relay"}
        connection.execute(
            "CREATE TABLE child (message_id TEXT REFERENCES outbox(message_id))")
        connection.execute("INSERT INTO child VALUES ('msg-1')")
    finally:
        connection.close()

    migrated = migrate_control_plane(db_path, now_ms=T1)
    try:
        assert version_of(db_path) == (head_version(), head_version())
        # Every column of the row survives the rebuild, including the delivery
        # evidence and the attempt count.
        assert migrated.execute(
            "SELECT run_id, recipient, dedup_key, status, retry_count, delivered_at_ms"
            "  FROM outbox WHERE message_id = 'msg-1'"
        ).fetchone() == ("run-1", "secretary", "dk-1", "delivered", 4, T0 + 1)
        # And the reference into it: the rebuild drops and recreates the parent
        # table, so a child left dangling would be the silent half of the risk.
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert migrated.execute("SELECT message_id FROM child").fetchall() == [("msg-1",)]
        assert migrated.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        migrated.close()


def test_the_real_ledger_is_discoverable_and_contiguous():
    steps = discover_migration_steps()
    assert steps, "the production DDL ledger must ship with the package"
    assert [step.version for step in steps] == list(range(1, len(steps) + 1))
    assert head_version(steps) == steps[-1].version


def test_the_real_ledger_migrates_an_empty_database_to_head(db_path):
    connection = create_production_control_plane(db_path, now_ms=T0)
    try:
        head = head_version()
        assert version_of(db_path) == (head, head)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    reopened = open_production_control_plane(db_path)
    reopened.close()


def test_render_current_schema_matches_a_freshly_migrated_database(db_path):
    connection = create_production_control_plane(db_path, now_ms=T0)
    try:
        from_disk = render_current_schema(connection)
    finally:
        connection.close()

    # The definition of the current schema is "whatever the steps produce from
    # nothing", so the generated reading aid must be derivable without a
    # database to point at -- otherwise docs/schema-current.sql could drift
    # from the steps and nothing would notice.
    assert render_current_schema() == from_disk


def test_the_generated_schema_says_it_must_not_be_applied():
    rendered = render_current_schema()
    assert "DO NOT EDIT, AND DO NOT APPLY" in rendered
    assert f"schema_migration head: {head_version()}" in rendered
    # It is a reading aid, not a step: the header has to say so, because the
    # dangerous mistake is not reading the file, it is applying it.
    assert "GENERATED FILE" in rendered
    assert rendered.endswith("\n")


def test_the_generated_schema_is_ascii_so_it_survives_a_cp932_console():
    render_current_schema().encode("ascii")


def test_rendering_leaves_no_database_behind(tmp_path, monkeypatch):
    # render_current_schema() migrates an in-memory database when given no
    # connection; a file appearing anywhere would mean the "reading aid" had
    # become a writer.
    monkeypatch.chdir(tmp_path)
    render_current_schema()
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# A caller's open transaction is not ours to commit
# --------------------------------------------------------------------------


def test_migrating_a_connection_with_an_open_transaction_is_refused(ledger, db_path):
    # sqlite3's default isolation_level opens a transaction before DML and ends
    # it when isolation_level is set to None. Migrating used to make that
    # assignment blind, so handing over a connection mid-transaction committed
    # the caller's work as a side effect of migrating.
    create_production_control_plane(db_path, now_ms=1, migrations_dir=ledger).close()
    connection = sqlite3.connect(db_path)  # driver default, not autocommit
    try:
        connection.execute("INSERT INTO alpha (id) VALUES (7)")
        assert connection.in_transaction
        with pytest.raises(ControlPlaneRefusal) as caught:
            migrate_control_plane(connection, now_ms=2, migrations_dir=ledger)
        # The message has to say what to do about it, since the caller is the
        # only one that can decide whether that work should land.
        assert "commit or roll it back" in str(caught.value)
        # Still open, so a rollback still undoes it: nothing was decided for the
        # caller.
        assert connection.in_transaction
        connection.rollback()
    finally:
        connection.close()
    assert rows_of(db_path, "alpha") == []


def test_a_refused_migration_does_not_commit_the_callers_open_transaction(ledger, tmp_path):
    # The sharp end: the database is refused (a foreign application_id), and a
    # refusal that has already persisted somebody's half-finished work is the
    # opposite of what a refusal means (R3).
    target = tmp_path / "foreign.sqlite3"
    setup = raw(target)
    try:
        setup.execute("CREATE TABLE scratch (v TEXT)")
        setup.execute(f"PRAGMA application_id = {SPIKE_APPLICATION_ID}")
    finally:
        setup.close()

    connection = sqlite3.connect(target)
    try:
        connection.execute("INSERT INTO scratch VALUES ('half-finished')")
        with pytest.raises(ControlPlaneRefusal):
            migrate_control_plane(connection, now_ms=2, migrations_dir=ledger)
        connection.rollback()
    finally:
        connection.close()
    assert rows_of(target, "scratch") == []


def test_an_autocommit_connection_is_still_migrated(ledger, db_path):
    # The refusal is about an open transaction, not about isolation_level: the
    # ordinary caller that opens its own autocommit connection must still work.
    create_production_control_plane(db_path, now_ms=1, migrations_dir=ledger).close()
    write_step(ledger, "0003_gamma.sql", "CREATE TABLE gamma (id INTEGER PRIMARY KEY);\n")
    connection = raw(db_path)
    try:
        migrate_control_plane(connection, now_ms=2, migrations_dir=ledger)
    finally:
        connection.close()
    assert "gamma" in tables_of(db_path)


# --------------------------------------------------------------------------
# the verify-close-reopen window: a rolling deploy migrating in the gap
# --------------------------------------------------------------------------


def _older_build_ledger(ledger: Path) -> Path:
    """A build that knows step 0001 only -- the older half of a rolling deploy."""

    older = ledger.parent / "older-build"
    write_step(older, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text(encoding="utf-8"))
    return older


def _migrate_in_the_gap(monkeypatch, ledger: Path, db_path: Path) -> None:
    """Let a newer build migrate *db_path* after verification and before connect.

    The window is driven deterministically rather than by timing: the real
    _verify_readonly runs, and the newer build's migration is spliced in
    immediately after it returns -- exactly where the closed read-only
    connection leaves the file unobserved.
    """

    real = m._verify_readonly

    def verify_then_let_the_newer_build_migrate(*args, **kwargs):
        applied = real(*args, **kwargs)
        monkeypatch.setattr(m, "_verify_readonly", real)  # once, not on re-verification
        migrate_control_plane(db_path, now_ms=T1, migrations_dir=ledger).close()
        return applied

    monkeypatch.setattr(m, "_verify_readonly", verify_then_let_the_newer_build_migrate)


def test_opening_refuses_a_database_a_newer_build_migrated_in_the_verify_reopen_gap(
    ledger, db_path, monkeypatch
):
    # DatabaseAheadOfCodeRefused exists so an older build cannot operate on a
    # database a newer one has moved forward, and a rolling deploy is the
    # deployment shape it was written for. Verification on a read-only
    # connection that is closed before the writable one is opened leaves a
    # window in which exactly that can happen, so the returned handle is
    # verified again on itself.
    older_build = _older_build_ledger(ledger)
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=older_build).close()
    assert version_of(db_path) == (1, 1)

    _migrate_in_the_gap(monkeypatch, ledger, db_path)

    with pytest.raises(DatabaseAheadOfCodeRefused, match="only up to 1"):
        open_production_control_plane(db_path, migrations_dir=older_build)
    assert version_of(db_path) == (2, 2)


def test_migrating_refuses_a_database_a_newer_build_migrated_in_the_verify_reopen_gap(
    ledger, db_path, monkeypatch
):
    # migrate_control_plane's path branch has the same window, and a no-op
    # migration is where it hides: with the database already past this build's
    # head there is nothing to apply, so without re-verification the older
    # build silently gets a writable handle to a database ahead of its code.
    older_build = _older_build_ledger(ledger)
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=older_build).close()

    _migrate_in_the_gap(monkeypatch, ledger, db_path)

    with pytest.raises(DatabaseAheadOfCodeRefused, match="only up to 1"):
        migrate_control_plane(db_path, now_ms=T2, migrations_dir=older_build)
    assert version_of(db_path) == (2, 2)


def test_a_step_is_not_applied_over_a_database_another_migrator_moved(
    ledger, db_path, monkeypatch
):
    # Re-verification is a read at a point in time; the write path needs the
    # check inside the transaction that does the writing. With the write lock
    # held, a ledger head that is not exactly step.version - 1 means another
    # migrator moved the database between the verification and this step, so
    # the step is refused instead of applied on top of a shape this build never
    # saw.
    create_production_control_plane(db_path, now_ms=T0, migrations_dir=ledger).close()
    write_step(ledger, "0003_gamma.sql", "CREATE TABLE gamma (id INTEGER PRIMARY KEY);\n")

    real_apply_step = m._apply_step
    moved = False

    def move_the_database_first(connection, step, *, now_ms):
        nonlocal moved
        if not moved:
            moved = True
            other = raw(db_path)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("CREATE TABLE gamma (id INTEGER PRIMARY KEY)")
                other.execute(
                    "INSERT INTO schema_migration (version, name, checksum, applied_at_ms) "
                    "VALUES (3, 'gamma', ?, ?)",
                    (m.discover_migration_steps(ledger)[-1].checksum, T1),
                )
                other.execute("PRAGMA user_version = 3")
                other.execute("COMMIT")
            finally:
                other.close()
        return real_apply_step(connection, step, now_ms=now_ms)

    monkeypatch.setattr(m, "_apply_step", move_the_database_first)

    with pytest.raises(MigrationStepsRefused, match="moved"):
        migrate_control_plane(db_path, now_ms=T2, migrations_dir=ledger)
    assert version_of(db_path) == (3, 3)
    assert [row["version"] for row in _ledger_rows(db_path)] == [1, 2, 3]
