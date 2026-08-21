"""AC-7 -- the canary divergence report: what the canary produced, and no verdict on it.

The failure this module is written against is the one ``ACCEPTANCE.md`` section 3
names in its own words and then refuses to commit: the canary's *shape* is
decided (``D-0013``) and its **duration, sample size and numeric exit / rollback
criteria are not** (``Q-0005``). Section 3 goes further and closes the obvious
shortcut -- AC-9's reduction targets "are not the same thing as canary go/no-go
thresholds, and this document does not convert one into the other". A harness
that printed a verdict would perform exactly that conversion, and it would
perform it invisibly: the number it compared against would be whichever
threshold the person writing the harness had in mind on the day, wearing the
authority of a tool. Everything downstream -- whether the cutover proceeds,
whether a rollback is called -- would then rest on a threshold nobody decided.
So this module emits the measurements the decision will be made from, and the
decision stays with the people ``Q-0005`` is waiting on. That is why there is no
``passed`` field on any dataclass here and no verdict line in the rendering;
``tests/measurement/test_canary.py`` greps the rendered report for the verdict
vocabulary so the absence is a property of the code rather than of this
paragraph.

What the report *is*, per ``docs/measurement-harness.md`` section 5: section
3.3's episode reconciliation rendered per period, plus the three assertions
``ACCEPTANCE.md`` section 3's Verification bullets ask for. Each is a different
kind of statement and they are kept apart deliberately:

**The writer audit** (condition 2, bullet 1) is about *records*: no record was
written by both stores over the canary window. It cannot be answered from this
database alone -- half the evidence is in v1's store -- so the v1 side arrives as
an input, exactly as the shadow adapter's episodes do
(:class:`~claude_org_runtime.measurement.shadow.V1Reference`), and for the same
reason: a harness that reached into ``.state/`` and an ``events`` table would
stop running the day those paths moved, which is during the cutover it exists to
observe. A v1 record naming a class this audit does not query is **refused**
rather than skipped (:class:`UndeclaredRecordClass`): a class nobody compares
produces no overlap, and "no overlap" read off an unasked question is the
flattering answer arriving through the absence of data.

**The ownership ledger** (conditions 3, 4, 6) is about *runs*, and it rests on
the settled reading of ``D-0013``: ownership is decided once at run start, and a
row in this database **is** the assertion that the run is Interlock-owned --
there is no ownership column and none is coming. A run that changed owner
mid-flight is therefore not a state anything records; it is detectable only as a
run **claimed by both sides**, and that collision is the assertion failing.
:func:`~claude_org_runtime.measurement.cohort.select_cohort` meets the same
collision and *refuses* (``OwnershipAssertionRefused``), which is right for it:
its output is a denominator, and a denominator computed from a contradicted
input is a number with no reason to be doubted. This report's output is the
finding itself, so refusing here would destroy the one artefact AC-7 asks for.
Both claims are kept, both are printed, and neither is deduped -- deduping is
what turns a violated assertion into a tidy row.

**The read-only assertion** (condition 5) is about *this process*, and it is the
one bullet that names its own evidence: read-only "enforced by capability, not by
convention". A field on a report saying ``read_only: true`` is a convention with
a longer name -- it is true because someone typed it, and it stays true after the
capability is gone. So :func:`evidence_of_read_only` reads ``PRAGMA query_only``
back **off the live connection** the rest of the report is being measured
through, names the ``mode=ro`` URI that connection's own ``database_list`` says
it is attached to, and then proves the file mode behaviourally through
``reader``'s probe. A connection that merely *claims* read-only -- opened
read-write with ``query_only`` raised by hand -- clears the first check and is
caught by the second, which is the distinction the bullet is drawing.

Ordering is load-bearing: the read-only evidence is gathered **before the first
measurement query**. Evidence collected afterwards would be evidence about a
connection that had already been used, and the question is whether the
instrument could have changed what it measured.

Out of scope, and stated rather than implied: the two remaining verification
bullets -- the rehearsed, timed rollback and the bridge inventory -- are
*operational* records of something a person does, not measurements this harness
can read out of a database, and nothing here pretends to produce them. Nor does
this module raise an incident, apply a remedy, or decide AC-10.

Nothing here writes and nothing here reads a clock. The connection comes from
:func:`~claude_org_runtime.measurement.reader.open_for_measurement`; every bound
is the caller's half-open ``[start, end)`` (``time-base-policy.md`` section 2,
rule 4).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Collection, Iterable, Mapping, Sequence

# reader.prove_read_only is reader.py's one implementation of the probe, called
# rather than copied: ACCEPTANCE.md section 3 condition 5 wants the read-only
# assertion evidenced off the LIVE connection the report is measured through,
# and open_for_measurement proves it only for the connection it opens itself --
# a different object from the one handed to this module.
from claude_org_runtime.measurement.reader import (
    ControlPlaneRefusal,
    ReadOnlyCapabilityRefused,
    prove_read_only,
)
from claude_org_runtime.measurement.shadow import (
    ShadowEpisode,
    ShadowReconciliation,
    V1Reference,
    reconcile,
    render_shadow_reconciliation,
)

__all__ = [
    "CanaryDivergenceReport",
    "CanaryRefusal",
    "DUAL_WRITE",
    "DualWriteFinding",
    "INTERLOCK_STORE",
    "MODE_RO",
    "OWNERSHIP_COLLISION",
    "OwnedRun",
    "OwnershipCollisionFinding",
    "OwnershipLedger",
    "QUERY_DEFINITIONS",
    "READ_ONLY_URI_QUERY",
    "RECORD_CLASSES",
    "RECORD_CLASS_PULL_REQUEST",
    "RECORD_CLASS_RUN",
    "ReadOnlyCapabilityRefused",
    "ReadOnlyEvidence",
    "RecordClass",
    "UndeclaredRecordClass",
    "V1OwnershipInput",
    "V1WriterLedger",
    "WriterAudit",
    "WrittenRecord",
    "audit_writers",
    "build_ownership_ledger",
    "evidence_of_read_only",
    "measure_canary_divergence",
    "read_interlock_records",
    "render_canary_divergence_report",
]


#: The store name this database's own records carry. A literal, because there is
#: exactly one Interlock store and the v1 side names itself in its own input --
#: a record's store is how a finding says *which two* wrote it.
INTERLOCK_STORE = "interlock"

#: The URI query string that carries the read-only capability (``D-0040``,
#: ``measurement-harness.md`` section 1). Named here because the report prints
#: the URI it was measured through, and a report that printed a URI without this
#: fragment would be reporting the absence of the capability.
MODE_RO = "mode=ro"

#: The finding kinds. Closed and always both emitted, at zero as well: a reader
#: diffing two reports must see ``dual_write: 0``, because a missing key reads as
#: "nothing to report" when it means "this report was produced by code that did
#: not look".
DUAL_WRITE = "dual_write"
OWNERSHIP_COLLISION = "ownership_collision"

FINDING_KINDS: tuple[str, ...] = (DUAL_WRITE, OWNERSHIP_COLLISION)

#: Read off the live connection to name the file it is attached to. ``main`` is
#: the schema this harness measures; an attached second database would be a
#: different question and this report does not have one.
READ_ONLY_URI_QUERY = "PRAGMA database_list"


class CanaryRefusal(ControlPlaneRefusal):
    """Base for this module's refusals, under the control plane's hierarchy."""


class UndeclaredRecordClass(CanaryRefusal):
    """The v1 side handed over a record in a class this audit does not query.

    Skipping it would be the worst available outcome: the audit would run, find
    no overlap in that class -- because it never asked this database about it --
    and report an assertion holding on the strength of a comparison that did not
    happen. Condition 2 is a claim about *all* authoritative records, so a class
    outside :data:`RECORD_CLASSES` is either a class this build must learn to
    query or a record v1 should not be handing over, and both of those are for a
    person to settle before the number means anything.
    """


class OwnershipInputRefused(CanaryRefusal):
    """The v1 ownership input contradicts itself, or is not shaped like one.

    Distinct from an ownership *finding*: a finding is two systems disagreeing
    about the world, which is what this report exists to record, whereas this is
    one system's own list disagreeing with itself -- the same run claimed twice
    on the v1 side. The report cannot file that as a divergence between systems,
    because it is not one.
    """


class V1InputRefused(CanaryRefusal):
    """A v1-side input was constructed without the provenance it must carry."""


# --------------------------------------------------------------------------
# condition 5 -- the read-only assertion, evidenced from the runtime
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadOnlyEvidence:
    """What was read *off the live connection* to evidence condition 5.

    Every field is a reading, not a claim. ``query_only`` is the value
    ``PRAGMA query_only`` returned before this report ran a single measurement
    query; ``uri`` is built from the path the connection's own
    :data:`READ_ONLY_URI_QUERY` reports, so it names the file that was actually
    attached rather than the file the caller believes it opened; and
    ``file_mode_probe`` records how the file itself answered a write.

    There is deliberately no ``read_only: bool``. A boolean would be the same
    shape whether it came from a measurement or from a literal, and condition 5
    is precisely a rule about which of those two a claim of read-only is allowed
    to be.
    """

    query_only: int
    database_path: str
    uri: str
    file_mode_probe: str
    query_only_after_probe: int


#: How the file answered the probe. One value, because any other outcome is a
#: refusal rather than a recorded reading -- see :func:`evidence_of_read_only`.
FILE_REFUSED_THE_WRITE = "the file refused a write as SQLITE_READONLY"


def evidence_of_read_only(connection: sqlite3.Connection) -> ReadOnlyEvidence:
    """Evidence condition 5 from *connection* itself, or refuse.

    Three readings, in this order and for this reason:

    1. ``PRAGMA query_only`` **as found**. Not set-then-read: setting it first
       would make every connection pass, including the writable one this check
       exists to catch. What is wanted is the state the rest of the report will
       be measured in.
    2. The attached file, from :data:`READ_ONLY_URI_QUERY`, and the ``mode=ro``
       URI naming it. A URI the caller passed in would be a claim about the past;
       this one is what the connection says it is holding now.
    3. The file-mode probe (``reader``'s), which lowers ``query_only`` for one
       statement so the *file* answers rather than the connection guard. This is
       what separates a capability from a convention: a read-write connection
       with ``query_only`` raised by hand clears reading 1 and is caught here.

    ``query_only`` is read once more afterwards, because the probe is the one
    thing in the harness that lowers it and therefore the one thing that could
    leave it lowered.

    :raises ReadOnlyCapabilityRefused: if ``query_only`` is not in force, if the
        connection is attached to no file (an in-memory or temporary database
        cannot evidence ``mode=ro``, and a report that skipped the check for
        those would be skipping it for the one connection that has no file mode
        at all), or if the file accepts a write -- and equally if the probe
        could not reach an answer. All of these come back as ``reader``'s own
        refusal type, because they are one fact: this report is being measured
        through a handle that could have changed what it measured.
    """

    found = connection.execute("PRAGMA query_only").fetchone()[0]
    if found != 1:
        raise ReadOnlyCapabilityRefused(
            f"PRAGMA query_only reads back as {found!r} on the connection this "
            "canary divergence report was handed; ACCEPTANCE.md section 3 "
            "condition 5 requires the shadow path to be read-only enforced by "
            "capability, and this report evidences that by reading the live "
            "connection rather than by asserting it. Open the database through "
            "measurement.reader.open_for_measurement"
        )

    path = _attached_database_path(connection)
    uri = f"{path.resolve().as_uri()}?{MODE_RO}"

    # Raises ReadOnlyCapabilityRefused on a writable file and on an
    # inconclusive probe alike; a returned call is the file having refused the
    # write as read-only, which is the only reading this function records.
    prove_read_only(connection, path)

    after = connection.execute("PRAGMA query_only").fetchone()[0]
    if after != 1:
        raise ReadOnlyCapabilityRefused(
            "PRAGMA query_only reads back as "
            f"{after!r} after the file-mode probe on {path}; the probe lowers "
            "the connection guard for one statement and must restore it, so a "
            "connection left unguarded here is the harness having disarmed "
            "itself while checking that it was armed"
        )

    return ReadOnlyEvidence(
        query_only=int(found),
        database_path=str(path),
        uri=uri,
        file_mode_probe=FILE_REFUSED_THE_WRITE,
        query_only_after_probe=int(after),
    )


def _attached_database_path(connection: sqlite3.Connection) -> Path:
    """The file ``main`` is attached to, read off *connection*.

    An empty file name is SQLite's answer for an in-memory or temporary
    database. That is refused rather than tolerated: such a connection has no
    file to be read-only *by capability*, so the evidence condition 5 asks for
    does not exist for it, and producing a report that quietly omitted the check
    would leave the omission indistinguishable from the check having succeeded.
    """

    for _seq, name, file_name in connection.execute(READ_ONLY_URI_QUERY):
        if name != "main":
            continue
        if not file_name:
            raise ReadOnlyCapabilityRefused(
                "the connection this report was handed is attached to no file "
                "(an in-memory or temporary database); mode=ro is a property of "
                "opening a file, so there is nothing here that could evidence "
                "ACCEPTANCE.md section 3 condition 5"
            )
        return Path(file_name)
    raise ReadOnlyCapabilityRefused(  # pragma: no cover - SQLite always has main
        "the connection this report was handed has no 'main' database"
    )


# --------------------------------------------------------------------------
# condition 2 -- the writer audit
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordClass:
    """One class of authoritative record, and the query that finds it here.

    ``sql`` is executed, not described: the text in this object *is* the text
    that runs, so the ``query_definitions`` provenance
    (``measurement-harness.md`` section 6) cannot drift away from the query it
    documents. It binds ``:window_from_ms`` and ``:window_to_ms`` and returns
    ``record_key``, ``first_written_at_ms`` and ``last_written_at_ms``.

    ``key_shape`` is the spelling both systems must agree on. It is written down
    because the audit's whole power is that the two sides compute the same
    string from different schemas -- a key one side spells differently overlaps
    with nothing and reports a clean audit for the wrong reason.
    """

    name: str
    key_shape: str
    sql: str


#: A run. ``run_id`` is the key both sides carry -- it is the routing decision's
#: subject (``D-0013``), so a run v1 also wrote is nameable by exactly this
#: string on both sides.
RECORD_CLASS_RUN = RecordClass(
    name="run",
    key_shape="run_id",
    # The window test is an OVERLAP of the record's write span, not "was last
    # written inside the window". The schema records a first and a last write
    # and nothing between (there is no per-write audit trail), so a record
    # created before the window and updated after it may well have been written
    # inside it, and the audit has no way to say otherwise. Overlap
    # over-includes; over-inclusion can only add a candidate finding a person
    # then dismisses, whereas the tighter test can drop the one record the whole
    # audit exists to find.
    sql="""
        SELECT run_id            AS record_key,
               created_at_ms     AS first_written_at_ms,
               updated_at_ms     AS last_written_at_ms
        FROM run
        WHERE created_at_ms < :window_to_ms
          AND updated_at_ms >= :window_from_ms
        ORDER BY run_id
    """,
)

#: A pull request. Keyed the way section 3.3 keys its PR episodes -- provider,
#: lowercased owner/repo, number -- because that is the spelling v1 can reach
#: from its stored ``pr_url``, and because the fold happens in SQL for the
#: reason shadow.py gives: SQLite's ``lower()`` folds ASCII only while Python's
#: is Unicode-aware, so an independently spelled fold agrees on every ASCII slug
#: and then names a repository the database's own index never named.
RECORD_CLASS_PULL_REQUEST = RecordClass(
    name="pull_request",
    key_shape="provider/owner/name#pr_number, owner and name lowercased",
    sql="""
        SELECT repository.provider || '/'
               || lower(repository.owner) || '/'
               || lower(repository.name) || '#'
               || pull_request.pr_number       AS record_key,
               pull_request.created_at_ms      AS first_written_at_ms,
               pull_request.updated_at_ms      AS last_written_at_ms
        FROM pull_request
        JOIN repository ON repository.repo_id = pull_request.repo_id
        WHERE pull_request.created_at_ms < :window_to_ms
          AND pull_request.updated_at_ms >= :window_from_ms
        ORDER BY record_key
    """,
)

#: The classes audited by default: the two whose keys **both** systems can
#: spell. The list is short on purpose. A class Interlock can key and v1 cannot
#: contributes an empty v1 side, which is not evidence of no dual write -- it is
#: the absence of a comparison, and putting it in the report would dress one up
#: as the other. Adding a class means the v1 adapter learned to spell its key,
#: which is a change to both sides at once.
RECORD_CLASSES: tuple[RecordClass, ...] = (
    RECORD_CLASS_RUN,
    RECORD_CLASS_PULL_REQUEST,
)

#: Every query this module executes, as text, for section 6's provenance header.
QUERY_DEFINITIONS: Mapping[str, str] = MappingProxyType(
    {
        "read_only_uri": READ_ONLY_URI_QUERY,
        **{f"record_class:{cls.name}": cls.sql for cls in RECORD_CLASSES},
        "ownership_ledger": """
        SELECT run_id, created_at_ms
        FROM run
        WHERE created_at_ms >= :window_from_ms
          AND created_at_ms < :window_to_ms
        ORDER BY created_at_ms, run_id
    """,
        "ownership_collision": "SELECT run_id FROM run WHERE run_id IN (...)",
    }
)


@dataclass(frozen=True)
class WrittenRecord:
    """One authoritative record, and the store that wrote it.

    Both sides use this type. ``first_written_at_ms``/``last_written_at_ms``
    bracket the writing rather than pinning it, because neither store keeps a
    per-write trail; the audit turns on *identity*, and the instants are what a
    person reads when deciding what a finding means.
    """

    record_class: str
    record_key: str
    first_written_at_ms: int
    last_written_at_ms: int
    store: str

    @property
    def identity(self) -> tuple[str, str]:
        return (self.record_class, self.record_key)


@dataclass(frozen=True)
class DualWriteFinding:
    """One record both stores wrote. Condition 2 violated, named.

    Carries both records whole -- not a merged row -- because the question a
    person asks next is *when* each store wrote it, and a finding that had
    already reconciled the two instants would have answered that question by
    discarding it.
    """

    record_class: str
    record_key: str
    interlock: WrittenRecord
    v1: WrittenRecord


@dataclass(frozen=True)
class V1WriterLedger:
    """v1's list of what it wrote, as a separable adapter hands it over.

    The constructors force apart the two states an empty list conflates, on
    exactly the argument
    :class:`~claude_org_runtime.measurement.shadow.V1Reference` makes for
    episodes: an adapter that returned nothing and an adapter that did not run
    look identical from their output, and read as "v1 wrote nothing" they
    produce a writer audit that finds no dual write for the one reason that
    proves nothing.
    """

    source: str | None
    records: tuple[WrittenRecord, ...]
    absent_reason: str | None

    @property
    def available(self) -> bool:
        return self.source is not None

    @classmethod
    def absent(cls, *, reason: str) -> "V1WriterLedger":
        if not reason:
            raise V1InputRefused(
                "an absent writer ledger must say why it is absent; the report "
                "prints the reason where the audit would have been"
            )
        return cls(source=None, records=(), absent_reason=reason)

    @classmethod
    def observed(
        cls, *, source: str, records: Iterable[WrittenRecord]
    ) -> "V1WriterLedger":
        """Records read by *source*; an empty read degrades to :meth:`absent`."""

        if not source:
            raise V1InputRefused(
                "an observed writer ledger must name its source (D-0040: a "
                "report records where its numbers came from)"
            )
        materialised = tuple(records)
        if not materialised:
            return cls.absent(
                reason=(
                    f"the v1 writer audit adapter {source!r} returned no "
                    "records; an empty read is not evidence that v1 wrote "
                    "nothing (use V1WriterLedger.attests_empty to claim that "
                    "on purpose)"
                )
            )
        return cls(source=source, records=materialised, absent_reason=None)

    @classmethod
    def attests_empty(cls, *, source: str) -> "V1WriterLedger":
        """*source* audited v1's store over this window and found no record."""

        if not source:
            raise V1InputRefused(
                "an attestation that v1 wrote nothing must name who attests it"
            )
        return cls(source=source, records=(), absent_reason=None)


@dataclass(frozen=True)
class WriterAudit:
    """Verification bullet 1: no record was written by both stores.

    ``findings`` is the answer; the counts around it are what make an empty
    ``findings`` mean something. Zero findings over zero compared records is not
    the assertion holding, it is the assertion unasked, and the rendering says
    which of the two happened.
    """

    window_from_ms: int
    window_to_ms: int
    available: bool
    v1_source: str | None
    absent_reason: str | None
    record_classes: tuple[str, ...]
    interlock_record_count: int
    v1_record_count: int
    findings: tuple[DualWriteFinding, ...]

    @property
    def finding_count(self) -> int:
        return len(self.findings)


def read_interlock_records(
    connection: sqlite3.Connection,
    *,
    window_from_ms: int,
    window_to_ms: int,
    record_classes: Sequence[RecordClass] = RECORD_CLASSES,
) -> tuple[WrittenRecord, ...]:
    """This database's authoritative records whose write span meets the window."""

    _require_window(window_from_ms, window_to_ms)
    bindings = {"window_from_ms": window_from_ms, "window_to_ms": window_to_ms}
    records: list[WrittenRecord] = []
    for record_class in record_classes:
        for row in connection.execute(record_class.sql, bindings):
            records.append(
                WrittenRecord(
                    record_class=record_class.name,
                    record_key=str(row[0]),
                    first_written_at_ms=int(row[1]),
                    last_written_at_ms=int(row[2]),
                    store=INTERLOCK_STORE,
                )
            )
    return tuple(records)


def audit_writers(
    connection: sqlite3.Connection,
    *,
    window_from_ms: int,
    window_to_ms: int,
    v1_ledger: V1WriterLedger,
    record_classes: Sequence[RecordClass] = RECORD_CLASSES,
) -> WriterAudit:
    """Compare both stores' records over the canary window (condition 2).

    A record present on both sides under one ``(record_class, record_key)`` is a
    :class:`DualWriteFinding`. Nothing is deduped and nothing is resolved: which
    store "really" owns the record is the question the finding exists to put in
    front of a person, and an audit that answered it would be deciding the
    thing it was built to detect.

    :raises UndeclaredRecordClass: if a v1 record names a class outside
        *record_classes*.
    """

    _require_window(window_from_ms, window_to_ms)
    declared = tuple(cls.name for cls in record_classes)

    if not v1_ledger.available:
        return WriterAudit(
            window_from_ms=window_from_ms,
            window_to_ms=window_to_ms,
            available=False,
            v1_source=None,
            absent_reason=v1_ledger.absent_reason,
            record_classes=declared,
            interlock_record_count=len(
                read_interlock_records(
                    connection,
                    window_from_ms=window_from_ms,
                    window_to_ms=window_to_ms,
                    record_classes=record_classes,
                )
            ),
            v1_record_count=0,
            findings=(),
        )

    for record in v1_ledger.records:
        if record.record_class not in declared:
            raise UndeclaredRecordClass(
                f"the v1 writer ledger {v1_ledger.source!r} names record class "
                f"{record.record_class!r} (key {record.record_key!r}), which "
                f"this audit does not query; it queries {', '.join(declared)}. "
                "Auditing a class on one side only cannot show that no record "
                "was written by both -- it can only fail to find one "
                "(ACCEPTANCE.md section 3 condition 2)"
            )

    ours = read_interlock_records(
        connection,
        window_from_ms=window_from_ms,
        window_to_ms=window_to_ms,
        record_classes=record_classes,
    )
    by_identity = {record.identity: record for record in ours}

    findings: list[DualWriteFinding] = []
    for record in v1_ledger.records:
        mine = by_identity.get(record.identity)
        if mine is not None:
            findings.append(
                DualWriteFinding(
                    record_class=record.record_class,
                    record_key=record.record_key,
                    interlock=mine,
                    v1=record,
                )
            )

    return WriterAudit(
        window_from_ms=window_from_ms,
        window_to_ms=window_to_ms,
        available=True,
        v1_source=v1_ledger.source,
        absent_reason=None,
        record_classes=declared,
        interlock_record_count=len(ours),
        v1_record_count=len(v1_ledger.records),
        findings=tuple(
            sorted(findings, key=lambda f: (f.record_class, f.record_key))
        ),
    )


# --------------------------------------------------------------------------
# conditions 3, 4, 6 -- the run -> owning system ledger
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnedRun:
    """One system's claim that it owns a run, and when the claim was made.

    ``decided_at_ms`` is the run's start on the claiming side: ``D-0013`` decides
    routing once, at run start, so the start instant *is* the decision instant.
    There is no separate routing table to read, on either side.
    """

    run_id: str
    owning_system: str
    decided_at_ms: int
    store: str


@dataclass(frozen=True)
class OwnershipCollisionFinding:
    """One run claimed by both systems. Conditions 3, 4 and 6 violated, named.

    ``claims`` holds every claim as it arrived -- both of them, in full. The one
    thing this must not do is reduce them to a run id: with the two claims side
    by side a person can see which system started it and which picked it up
    mid-flight, which is the difference between a routing bug and a converter
    that was not supposed to exist (condition 6).
    """

    run_id: str
    claims: tuple[OwnedRun, ...]


@dataclass(frozen=True)
class V1OwnershipInput:
    """The runs v1 says it owns, from the same separable adapter.

    Same three constructors and the same argument as
    :class:`V1WriterLedger`: read as "v1 owned nothing", an empty list makes
    every collision impossible and the ledger reads clean because nobody looked.
    """

    source: str | None
    runs: tuple[OwnedRun, ...]
    absent_reason: str | None

    @property
    def available(self) -> bool:
        return self.source is not None

    @classmethod
    def absent(cls, *, reason: str) -> "V1OwnershipInput":
        if not reason:
            raise V1InputRefused(
                "an absent ownership input must say why it is absent"
            )
        return cls(source=None, runs=(), absent_reason=reason)

    @classmethod
    def observed(cls, *, source: str, runs: Iterable[OwnedRun]) -> "V1OwnershipInput":
        if not source:
            raise V1InputRefused("an observed ownership input must name its source")
        materialised = tuple(runs)
        if not materialised:
            return cls.absent(
                reason=(
                    f"the v1 ownership adapter {source!r} returned no runs; an "
                    "empty read is not evidence that v1 owned none (use "
                    "V1OwnershipInput.attests_empty to claim that on purpose)"
                )
            )
        seen: dict[str, OwnedRun] = {}
        for run in materialised:
            previous = seen.get(run.run_id)
            if previous is not None:
                raise OwnershipInputRefused(
                    f"the v1 ownership input {source!r} claims run "
                    f"{run.run_id!r} twice (at {previous.decided_at_ms} from "
                    f"{previous.store} and at {run.decided_at_ms} from "
                    f"{run.store}); one system claiming a run twice is that "
                    "system's list contradicting itself, not a divergence "
                    "between systems, and this report cannot file it as one"
                )
            seen[run.run_id] = run
        return cls(source=source, runs=materialised, absent_reason=None)

    @classmethod
    def attests_empty(cls, *, source: str) -> "V1OwnershipInput":
        if not source:
            raise V1InputRefused(
                "an attestation that v1 owned no run must name who attests it"
            )
        return cls(source=source, runs=(), absent_reason=None)


@dataclass(frozen=True)
class OwnershipLedger:
    """Verification bullet 2: run -> owning system at run start.

    ``entries`` is the ledger itself, both systems' claims in one list, and it is
    **not** deduped: a run appearing twice is the finding, and the ledger a
    person reads must show it twice or the finding has no evidence behind it.
    """

    window_from_ms: int
    window_to_ms: int
    available: bool
    v1_source: str | None
    absent_reason: str | None
    entries: tuple[OwnedRun, ...]
    findings: tuple[OwnershipCollisionFinding, ...]

    @property
    def finding_count(self) -> int:
        return len(self.findings)

    def collision_run_ids(self) -> tuple[str, ...]:
        """The runs both systems claim.

        Named separately because it is the input
        :func:`~claude_org_runtime.measurement.cohort.select_cohort` refuses on:
        an AC-9 report over this period cannot be produced while this tuple is
        non-empty, and this is where the operator reads *which* runs made that
        so.
        """

        return tuple(finding.run_id for finding in self.findings)


def build_ownership_ledger(
    connection: sqlite3.Connection,
    *,
    window_from_ms: int,
    window_to_ms: int,
    v1_ownership: V1OwnershipInput,
) -> OwnershipLedger:
    """The run -> owning-system ledger over the canary window (conditions 3, 4, 6).

    The Interlock side is every run whose ``created_at_ms`` falls in the window:
    a run row here *is* the Interlock-ownership assertion (``D-0013``; there is
    no ownership column), and routing is decided at run start, so the run's
    creation is its ledger entry.

    **The collision check is not bounded by the window, and the listing is.**
    Those are two different questions. The listing answers "what was routed
    during the canary", which is a window question. A collision answers "did a
    run change owner mid-flight", and a run that changed owner did so precisely
    by starting on one side *before* the window and appearing on the other
    inside it -- bounding the check would blind it to the case it exists for. So
    every run id the v1 input names is checked against the whole ``run`` table.
    """

    _require_window(window_from_ms, window_to_ms)

    entries: list[OwnedRun] = [
        OwnedRun(
            run_id=str(row[0]),
            owning_system=INTERLOCK_STORE,
            decided_at_ms=int(row[1]),
            store=INTERLOCK_STORE,
        )
        for row in connection.execute(
            QUERY_DEFINITIONS["ownership_ledger"],
            {"window_from_ms": window_from_ms, "window_to_ms": window_to_ms},
        )
    ]

    if not v1_ownership.available:
        return OwnershipLedger(
            window_from_ms=window_from_ms,
            window_to_ms=window_to_ms,
            available=False,
            v1_source=None,
            absent_reason=v1_ownership.absent_reason,
            entries=tuple(entries),
            findings=(),
        )

    claimed_here = _runs_this_database_holds(
        connection, [run.run_id for run in v1_ownership.runs]
    )
    ours_by_id = {entry.run_id: entry for entry in entries}

    findings: list[OwnershipCollisionFinding] = []
    for run in v1_ownership.runs:
        entries.append(run)
        if run.run_id not in claimed_here:
            continue
        # The Interlock claim may be outside the listing window -- that is the
        # mid-flight case -- so it is read from the row rather than taken from
        # the listing, which would drop exactly those findings.
        mine = ours_by_id.get(run.run_id) or _ownership_row(connection, run.run_id)
        findings.append(
            OwnershipCollisionFinding(run_id=run.run_id, claims=(mine, run))
        )

    return OwnershipLedger(
        window_from_ms=window_from_ms,
        window_to_ms=window_to_ms,
        available=True,
        v1_source=v1_ownership.source,
        absent_reason=None,
        entries=tuple(sorted(entries, key=lambda e: (e.decided_at_ms, e.run_id, e.store))),
        findings=tuple(sorted(findings, key=lambda f: f.run_id)),
    )


def _runs_this_database_holds(
    connection: sqlite3.Connection, run_ids: Sequence[str]
) -> frozenset[str]:
    """Which of *run_ids* this database holds a run row for.

    The same fact ``cohort._assert_no_run_is_claimed_by_both`` computes, and
    chunked for the same reason it chunks: SQLite's default parameter ceiling is
    999 and a v1 input is a list of whatever length the adapter hands over, so a
    query that worked in testing would fail on the first real period. What
    differs is only the verb -- ``cohort`` refuses, because its output is a
    denominator that would otherwise be quietly short; this module reports,
    because the finding *is* its output.
    """

    held: set[str] = set()
    for start in range(0, len(run_ids), 500):
        chunk = list(run_ids[start : start + 500])
        placeholders = ", ".join("?" for _ in chunk)
        held.update(
            row[0]
            for row in connection.execute(
                f"SELECT run_id FROM run WHERE run_id IN ({placeholders})", chunk
            )
        )
    return frozenset(held)


def _ownership_row(connection: sqlite3.Connection, run_id: str) -> OwnedRun:
    row = connection.execute(
        "SELECT run_id, created_at_ms FROM run WHERE run_id = ?", (run_id,)
    ).fetchone()
    return OwnedRun(
        run_id=str(row[0]),
        owning_system=INTERLOCK_STORE,
        decided_at_ms=int(row[1]),
        store=INTERLOCK_STORE,
    )


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CanaryDivergenceReport:
    """Section 5's report for one period: reconciliation plus three assertions.

    There is no verdict field, and the omission is the point -- see the module
    docstring. ``finding_counts`` is as close as the report comes to a summary,
    and a count of findings is a measurement: it says what was observed, not
    whether it is acceptable, and ``Q-0005`` is where acceptable gets decided.
    """

    period_start_ms: int
    period_end_ms: int
    read_only: ReadOnlyEvidence
    reconciliation: ShadowReconciliation
    writer_audit: WriterAudit
    ownership: OwnershipLedger

    def finding_counts(self) -> Mapping[str, int]:
        """Both kinds, always, at zero as well as above it."""

        return MappingProxyType(
            {
                DUAL_WRITE: self.writer_audit.finding_count,
                OWNERSHIP_COLLISION: self.ownership.finding_count,
            }
        )


def measure_canary_divergence(
    connection: sqlite3.Connection,
    *,
    period_start_ms: int,
    period_end_ms: int,
    interlock_episodes: Iterable[ShadowEpisode],
    v1_reference: V1Reference,
    censored_ids: Collection[str],
    fixture_labels: Mapping[str, str],
    v1_writer_ledger: V1WriterLedger,
    v1_ownership: V1OwnershipInput,
    record_classes: Sequence[RecordClass] = RECORD_CLASSES,
) -> CanaryDivergenceReport:
    """Assemble section 5's report over one period.

    The reconciliation is computed here, by calling
    :func:`~claude_org_runtime.measurement.shadow.reconcile`, rather than taken
    as a finished argument. That is not convenience: the report is section 3.3's
    reconciliation *rendered per period*, and a finished reconciliation handed in
    could have been computed over a different period, which the reader of the
    rendered page has no way to see. Computing it from the same two bounds makes
    the alignment structural. Nothing about the reconciliation is
    re-implemented -- the buckets, the censoring precedence and the miss-count
    refusal are all shadow's.

    The read-only evidence is gathered **first**, before any measurement query
    touches *connection*.
    """

    _require_window(period_start_ms, period_end_ms)
    read_only = evidence_of_read_only(connection)

    reconciliation = reconcile(
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        interlock_episodes=interlock_episodes,
        v1_reference=v1_reference,
        censored_ids=censored_ids,
        fixture_labels=fixture_labels,
    )
    writer_audit = audit_writers(
        connection,
        window_from_ms=period_start_ms,
        window_to_ms=period_end_ms,
        v1_ledger=v1_writer_ledger,
        record_classes=record_classes,
    )
    ownership = build_ownership_ledger(
        connection,
        window_from_ms=period_start_ms,
        window_to_ms=period_end_ms,
        v1_ownership=v1_ownership,
    )
    return CanaryDivergenceReport(
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        read_only=read_only,
        reconciliation=reconciliation,
        writer_audit=writer_audit,
        ownership=ownership,
    )


#: Printed once, at the end of every rendering. It is the reason the report
#: stops where it stops, and it is text rather than a rule in a docstring
#: because the person reading the rendering is the person who would otherwise
#: supply the missing verdict from memory.
NO_VERDICT_NOTE = (
    "Q-0005 (canary duration, sample size, numeric exit criteria) is open. "
    "AC-9's reduction targets are not canary exit thresholds and ACCEPTANCE.md "
    "section 3 does not convert one into the other, so this report states the "
    "measurements a canary decision will be made from and states no verdict on "
    "the canary."
)


def render_canary_divergence_report(report: CanaryDivergenceReport) -> str:
    """The report as text. ASCII only -- this reaches a cp932 console.

    Every section prints even when it is empty, and an unavailable v1 side
    prints its reason where its numbers would have been. The alternative --
    omitting a section with nothing in it -- makes "no dual write was found" and
    "no dual-write audit ran" render identically, and those are the two readings
    condition 2 turns on.
    """

    lines = [
        "Canary divergence report "
        f"[{report.period_start_ms}, {report.period_end_ms})",
        "",
        render_shadow_reconciliation(report.reconciliation),
        "",
    ]
    lines.extend(_render_writer_audit(report.writer_audit))
    lines.append("")
    lines.extend(_render_ownership(report.ownership))
    lines.append("")
    lines.extend(_render_read_only(report.read_only))
    lines.append("")
    lines.append(f"NOTE: {NO_VERDICT_NOTE}")
    return "\n".join(lines)


def _render_writer_audit(audit: WriterAudit) -> list[str]:
    lines = [
        "Writer audit (ACCEPTANCE.md section 3 condition 2) "
        f"[{audit.window_from_ms}, {audit.window_to_ms})",
        f"  record classes audited: {', '.join(audit.record_classes)}",
    ]
    if not audit.available:
        lines.append("  v1 store: ABSENT")
        lines.append(f"  reason: {audit.absent_reason}")
        lines.append(
            f"  Interlock records read: {audit.interlock_record_count}. "
            "No comparison is reported: with one store's records missing, "
            "finding no record written by both is not evidence that none was."
        )
        return lines
    lines.append(f"  v1 store: {audit.v1_source}")
    lines.append(f"  records compared: interlock={audit.interlock_record_count}, "
                 f"v1={audit.v1_record_count}")
    lines.append(f"  {DUAL_WRITE} findings: {audit.finding_count}")
    for finding in audit.findings:
        lines.append(
            f"    - {finding.record_class} {finding.record_key}: "
            f"{finding.interlock.store} wrote "
            f"[{finding.interlock.first_written_at_ms}, "
            f"{finding.interlock.last_written_at_ms}], "
            f"{finding.v1.store} wrote "
            f"[{finding.v1.first_written_at_ms}, {finding.v1.last_written_at_ms}]"
        )
    if audit.findings:
        lines.append(
            "  Condition 2 (no dual write) is VIOLATED for the records above."
        )
    return lines


def _render_ownership(ledger: OwnershipLedger) -> list[str]:
    lines = [
        "Ownership ledger at run start (conditions 3, 4, 6) "
        f"[{ledger.window_from_ms}, {ledger.window_to_ms})",
    ]
    if not ledger.available:
        lines.append("  v1 claims: ABSENT")
        lines.append(f"  reason: {ledger.absent_reason}")
        lines.append(
            f"  Interlock-owned runs listed: {len(ledger.entries)}. "
            "No collision is reported: a run changing owner mid-flight is only "
            "visible as a run both systems claim, and only one side's claims "
            "are here."
        )
        return lines
    lines.append(f"  v1 claims from: {ledger.v1_source}")
    lines.append(f"  ledger entries: {len(ledger.entries)}")
    for entry in ledger.entries:
        lines.append(
            f"    - {entry.run_id} -> {entry.owning_system} "
            f"at {entry.decided_at_ms} ({entry.store})"
        )
    lines.append(f"  {OWNERSHIP_COLLISION} findings: {ledger.finding_count}")
    for finding in ledger.findings:
        claims = "; ".join(
            f"{claim.owning_system} at {claim.decided_at_ms} ({claim.store})"
            for claim in finding.claims
        )
        lines.append(f"    - {finding.run_id} claimed by {claims}")
    if ledger.findings:
        lines.append(
            "  Conditions 3, 4 and 6 (no run changes owner mid-flight) are "
            "VIOLATED for the runs above. Both claims are listed as they "
            "arrived; the report does not pick a side."
        )
    return lines


def _render_read_only(evidence: ReadOnlyEvidence) -> list[str]:
    return [
        "Shadow path read-only assertion (condition 5), read off the live "
        "connection",
        f"  PRAGMA query_only: {evidence.query_only}",
        f"  uri: {evidence.uri}",
        f"  file mode probe: {evidence.file_mode_probe}",
        f"  PRAGMA query_only after probe: {evidence.query_only_after_probe}",
    ]


def _require_window(from_ms: int, to_ms: int) -> None:
    if to_ms <= from_ms:
        raise CanaryRefusal(
            f"the canary window [{from_ms}, {to_ms}) is empty or inverted; a "
            "half-open window must end strictly after it starts "
            "(time-base-policy.md section 2, rule 4)"
        )
