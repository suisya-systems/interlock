"""The harness's opener: read-only proved, not claimed, and the migrator's refusals reused.

``ACCEPTANCE.md`` section 3 condition 5 asks for read-only **by capability, not
by convention**, and the distance between those two is the whole subject of this
file. A harness that merely *is* read-only today passes every test that writes
through it and then reads back nothing; the tests that matter are the ones that
take the claim apart -- remove one mechanism and show the other still refuses the
write, degrade the URI behind the opener's back and show it notices, hash the
file across an open and show that even the noticing wrote nothing.

The identity cases (a spike database, a database behind or ahead of this build,
an edited step) assert the **migrator's own exception types** deliberately: the
property under test is that the harness reuses that verification rather than
growing a second copy of it, and a test that accepted any ``ControlPlaneRefusal``
would keep passing on the day the copy appeared.
"""

from __future__ import annotations

import ast
import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import migrator as m
from claude_org_runtime.control_plane.migrator import (
    ControlPlaneRefusal,
    CorruptStateRefused,
    DatabaseAheadOfCodeRefused,
    MigrationChecksumRefused,
    MissingStateRefused,
    create_production_control_plane,
)
from claude_org_runtime.control_plane.schema import create_control_plane
from claude_org_runtime.measurement import reader
from claude_org_runtime.measurement.reader import (
    ReadOnlyCapabilityRefused,
    open_for_measurement,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

#: A write that is valid against every production database: the ledger table is
#: bootstrapped by the migrator itself, so it exists at every version. Its
#: DELETE/UPDATE triggers do not touch INSERT, so nothing but the read-only
#: capability under test can be what refuses this statement.
A_VALID_WRITE = (
    "INSERT INTO schema_migration (version, name, checksum, applied_at_ms) "
    "VALUES (9999, 'not_a_step', '" + "0" * 64 + "', 0)"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture
def production_db(tmp_path: Path) -> Path:
    """A real database migrated to head with the real ledger, then let go of."""

    path = tmp_path / "production.sqlite3"
    connection = create_production_control_plane(path, now_ms=T0)
    connection.close()
    return path


def write_step(directory: Path, filename: str, sql: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(sql, encoding="utf-8")
    return path


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """A two-step scratch ledger, so "behind" and "ahead" can both be built."""

    directory = tmp_path / "ledger"
    write_step(directory, "0001_alpha.sql", "CREATE TABLE alpha (id INTEGER PRIMARY KEY);\n")
    write_step(directory, "0002_beta.sql", "CREATE TABLE beta (id INTEGER PRIMARY KEY);\n")
    return directory


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sidecars(path: Path) -> list[Path]:
    """``-wal``/``-journal`` companions: evidence that a "read" in fact wrote."""

    return sorted(path.parent.glob(f"{path.name}-*"))


# --------------------------------------------------------------------------
# the capability itself
# --------------------------------------------------------------------------


def test_a_write_through_the_harness_connection_is_refused(production_db: Path):
    connection = open_for_measurement(production_db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(A_VALID_WRITE)
        # A read still works: the point is an instrument, not a closed door.
        assert connection.execute("PRAGMA user_version").fetchone()[0] > 0
    finally:
        connection.close()


def test_the_same_write_succeeds_on_an_ordinary_connection(production_db: Path, tmp_path: Path):
    # The control for every refusal in this file. Without it, a typo in
    # A_VALID_WRITE would make each of them pass for the wrong reason -- the
    # statement rejected as malformed rather than as a write -- and the suite
    # would certify a capability nobody had tested. Run against a copy, so the
    # database the other tests hash stays untouched.
    copy = tmp_path / "writable-copy.sqlite3"
    shutil.copy(production_db, copy)
    connection = sqlite3.connect(copy, isolation_level=None)
    try:
        connection.execute(A_VALID_WRITE)
    finally:
        connection.close()


def test_query_only_alone_refuses_the_write_with_the_file_opened_read_write(production_db: Path):
    # Mechanism 2 on its own. There are two mechanisms so that neither one's
    # failure is load-bearing, which is only a property if each is independently
    # sufficient -- this test and the next are that pair.
    connection = sqlite3.connect(production_db, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = ON")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(A_VALID_WRITE)
    finally:
        connection.close()


def test_mode_ro_alone_refuses_the_write_with_query_only_off(production_db: Path):
    # Mechanism 1 on its own, with the connection-level guard explicitly down so
    # that it cannot be what refuses.
    uri = production_db.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = OFF")
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(A_VALID_WRITE)
    finally:
        connection.close()


def test_the_harness_connection_reports_query_only_in_force(production_db: Path):
    connection = open_for_measurement(production_db)
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        connection.close()


def test_a_connection_that_is_not_mode_ro_is_refused_by_the_probe(
    production_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # The failure this refusal exists for: the URI stops carrying the capability
    # -- a bad join, a path that needed quoting, a future edit -- and every
    # figure the harness prints afterwards comes off a connection that could
    # have changed what it measured. query_only would still be ON, so nothing
    # else in the open would notice; only asking the file for a write lock does.
    before = digest(production_db)
    real_connect = sqlite3.connect

    def connect_without_the_capability(_uri, *args, **kwargs):
        kwargs.pop("uri", None)
        return real_connect(production_db, *args, **kwargs)

    monkeypatch.setattr(reader.sqlite3, "connect", connect_without_the_capability)
    with pytest.raises(ReadOnlyCapabilityRefused, match="mode=ro"):
        open_for_measurement(production_db)
    # The probe takes a lock and rolls it back without modifying a page, so even
    # the connection that turned out to be writable wrote nothing.
    assert digest(production_db) == before
    assert sidecars(production_db) == []


def test_a_busy_database_is_refused_rather_than_certified_read_only(
    production_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # The defect this test is the regression for: the probe accepted ANY
    # OperationalError as "the file refused the write", and a writable
    # connection whose write is blocked by another writer's RESERVED lock
    # raises exactly that type with "database is locked". A control plane with
    # a watcher or dispatcher mid-transaction is therefore not an edge case --
    # it is the ordinary state -- and under it the degraded, fully writable
    # connection below was handed back as the measurement handle and would
    # INSERT into schema_migration through it.
    #
    # Two things have to be true at once for the reproduction: the URI is
    # degraded exactly as in the test above, so the connection really is
    # read-write, and a second connection holds BEGIN IMMEDIATE so the probe's
    # write cannot land. The probe must then refuse -- an unproved capability
    # is refused on the same terms as an absent one.
    real_connect = sqlite3.connect

    def connect_without_the_capability(_uri, *args, **kwargs):
        kwargs.pop("uri", None)
        # timeout=0: with the default five-second busy handler this test would
        # spend that long blocking before SQLite gave the answer it is about.
        kwargs["timeout"] = 0
        return real_connect(production_db, *args, **kwargs)

    writer = sqlite3.connect(production_db, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(reader.sqlite3, "connect", connect_without_the_capability)
        with pytest.raises(ReadOnlyCapabilityRefused) as refusal:
            open_for_measurement(production_db)
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    message = str(refusal.value)
    # The two facts are different and the operator's next move differs with
    # them, so the refusal must not report contention as "the database was
    # writable" -- that sends someone to fix a URI that is not broken.
    assert "inconclusive" in message
    assert "database is locked" in message
    assert "was not opened mode=ro" not in message


def test_the_probe_still_certifies_an_idle_read_only_database(production_db: Path):
    # The other half of the same fix: refusing every unrecognised refusal must
    # not become refusing everything. With no writer in the way the real
    # mode=ro connection produces a real SQLITE_READONLY error and the open
    # succeeds, which is the case the harness exists for.
    connection = open_for_measurement(production_db)
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(A_VALID_WRITE)
    finally:
        connection.close()


def test_only_a_read_only_error_counts_as_proof_of_mode_ro(production_db: Path, tmp_path: Path):
    # Bind the classifier to errors SQLite actually raised, not to strings
    # pasted into the test: a pasted message would keep this test green on the
    # day SQLite reworded one, which is the day the classifier needs to fail.
    copy = tmp_path / "writable-copy.sqlite3"
    shutil.copy(production_db, copy)

    uri = copy.resolve().as_uri() + "?mode=ro"
    read_only = sqlite3.connect(uri, uri=True, isolation_level=None)
    writer = sqlite3.connect(copy, isolation_level=None)
    blocked = sqlite3.connect(copy, isolation_level=None, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError) as read_only_error:
            read_only.execute(A_VALID_WRITE)
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as busy_error:
            blocked.execute(A_VALID_WRITE)
    finally:
        writer.execute("ROLLBACK")
        for handle in (read_only, writer, blocked):
            handle.close()

    assert reader._the_error_says_the_database_is_read_only(read_only_error.value)
    assert not reader._the_error_says_the_database_is_read_only(busy_error.value)


def test_query_only_that_does_not_take_effect_is_refused(production_db: Path):
    # PRAGMA is a silent no-op for a name SQLite does not know, so "issued" and
    # "in force" are different states and only the read-back separates them.
    # Simulated here by handing the check a connection that answers 0.
    connection = sqlite3.connect(production_db, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = OFF")
        with pytest.raises(ReadOnlyCapabilityRefused, match="query_only"):
            reader._require_query_only(production_db, connection, when="in this test")
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the migrator's verification, reused rather than re-derived
# --------------------------------------------------------------------------


def test_a_spike_database_is_refused_with_the_migrators_own_type(tmp_path: Path):
    path = tmp_path / "spike.sqlite3"
    create_control_plane(path).close()
    with pytest.raises(CorruptStateRefused, match="spike database"):
        open_for_measurement(path)


def test_a_database_behind_this_build_is_refused_without_being_migrated(
    tmp_path: Path, ledger: Path
):
    one_step = tmp_path / "one_step"
    write_step(one_step, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text())
    path = tmp_path / "behind.sqlite3"
    create_production_control_plane(path, now_ms=T0, migrations_dir=one_step).close()

    before = digest(path)
    with pytest.raises(ControlPlaneRefusal) as refusal:
        open_for_measurement(path, migrations_dir=ledger)
    # Behind, specifically -- not the ahead refusal, which is a different
    # diagnosis with a different remedy.
    assert not isinstance(refusal.value, DatabaseAheadOfCodeRefused)
    assert "never migrates" in str(refusal.value)
    assert digest(path) == before


def test_a_database_ahead_of_this_build_is_refused(tmp_path: Path, ledger: Path):
    path = tmp_path / "ahead.sqlite3"
    create_production_control_plane(path, now_ms=T0, migrations_dir=ledger).close()

    one_step = tmp_path / "one_step"
    write_step(one_step, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text())
    with pytest.raises(DatabaseAheadOfCodeRefused):
        open_for_measurement(path, migrations_dir=one_step)


def test_an_applied_step_whose_bytes_changed_is_refused(tmp_path: Path, ledger: Path):
    path = tmp_path / "edited.sqlite3"
    create_production_control_plane(path, now_ms=T0, migrations_dir=ledger).close()
    # The dangerous edit: harmless-looking, leaves the version untouched, and is
    # invisible from the version number alone.
    (ledger / "0002_beta.sql").write_text(
        "CREATE TABLE beta (id INTEGER PRIMARY KEY, note TEXT);\n", encoding="utf-8"
    )
    with pytest.raises(MigrationChecksumRefused):
        open_for_measurement(path, migrations_dir=ledger)


def test_an_absent_database_is_refused_rather_than_measured_as_empty(tmp_path: Path):
    # R3: an absent database is not an empty one. Measured as empty it reports
    # zero incidents and a perfect miss rate.
    with pytest.raises(MissingStateRefused):
        open_for_measurement(tmp_path / "nothing-here.sqlite3")


def test_a_file_that_is_not_a_database_is_refused(tmp_path: Path):
    path = tmp_path / "not-a-database.sqlite3"
    path.write_bytes(b"this is not an SQLite file")
    with pytest.raises(CorruptStateRefused):
        open_for_measurement(path)


# --------------------------------------------------------------------------
# opening writes nothing at all
# --------------------------------------------------------------------------


def test_opening_leaves_the_file_byte_identical_and_makes_no_sidecar(production_db: Path):
    # v1's reporter promoted the database it read to WAL, which is a write to
    # the file and creates a -wal companion. Hashing the bytes catches the
    # promotion, the journal and any accidental page write in one assertion.
    before = digest(production_db)
    connection = open_for_measurement(production_db)
    try:
        connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()
        assert sidecars(production_db) == []
    finally:
        connection.close()
    assert digest(production_db) == before
    assert sidecars(production_db) == []


def test_a_refused_open_writes_nothing_either(tmp_path: Path, ledger: Path):
    # A database on its way to a refusal must not be written to on the way, not
    # even a rollback journal: the operator's next move after a checksum refusal
    # is forensic, and an instrument that touched the evidence has spoiled it.
    path = tmp_path / "ahead.sqlite3"
    create_production_control_plane(path, now_ms=T0, migrations_dir=ledger).close()
    one_step = tmp_path / "one_step"
    write_step(one_step, "0001_alpha.sql", (ledger / "0001_alpha.sql").read_text())

    before = digest(path)
    with pytest.raises(DatabaseAheadOfCodeRefused):
        open_for_measurement(path, migrations_dir=one_step)
    assert digest(path) == before
    assert sidecars(path) == []


# --------------------------------------------------------------------------
# "never migrates, never takes a lease" as a structural property
# --------------------------------------------------------------------------


def _imported_names(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_the_opener_imports_no_writer_and_no_lease():
    # Structural, because "the harness never migrates and holds no lease"
    # (measurement-harness.md section 1, D-0040) is a claim about capability: a
    # module that cannot name migrate_control_plane cannot call it, whereas a
    # module that merely does not call it today is one edit from doing so, and
    # that edit reads as innocuous in review. The same argument covers the lease
    # -- an instrument with a writer epoch could produce a fenced write.
    imported = _imported_names(reader)
    forbidden = {
        "migrate_control_plane",
        "create_production_control_plane",
        "open_production_control_plane",
    }
    assert imported & forbidden == set()
    assert not any("lease" in name for name in imported)
    assert not any("txn" in name for name in imported)


def test_the_opener_reuses_the_migrators_verifier_rather_than_its_own():
    # The other half of the same property: the identity rules live in one place.
    assert "verify_production_database" in _imported_names(reader)
    assert reader.verify_production_database is m.verify_production_database


def test_the_package_exports_no_way_to_write():
    import claude_org_runtime.measurement as package

    offenders = [
        name
        for name in package.__all__
        if any(word in name.lower() for word in ("migrate", "create", "write", "lease"))
    ]
    assert offenders == []
