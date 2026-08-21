"""G6 -- the header every report carries about itself, so it can be recomputed.

The failure this module is written against is the one
``docs/measurement-harness.md`` section 6 opens with: **a report that cannot be
recomputed later is an opinion.** A rate printed on its own is unfalsifiable six
weeks later, when the question is not "what did it say" but "was it right" --
and answering that needs the database it was read from, the migration head that
shaped that database, the policy numbers every latency was judged against, the
detector build that produced the incidents, the queries that were actually run,
and the corpus the recall was measured over. None of those are recoverable from
the number. Each one of them changes the number.

So the header is not decoration on the report; it is the part of the report that
makes the rest of it a measurement. Section 6's table is reproduced here field
for field, and both renderings are generated from :meth:`ReportHeader.as_mapping`
-- one mapping, two formatters -- so a field cannot be present in the JSON and
missing from the Markdown. Two renderings that are allowed to drift are two
different claims about the same run, and the one the reader happens to have is
the one that is wrong.

**The fingerprint is the field that does the work, and its cheap form does not
do it.** Section 6 records the alternative that was considered and rejected: row
counts plus ``MAX(seq)`` / ``MAX(rowid)``. The reason it fails is a property of
this schema, not a matter of taste. Most of the state a report reads is *updated
in place* -- a verdict projection, an ``outbox`` status, a ``gate`` outcome, a
``usage_status`` backfilled by a late adapter after the provider finally
answered. Every one of those edits changes what the report says. **Not one of
them changes a row count or a maximum.** An aggregate fingerprint would
therefore stamp two materially different reads with the same digest and certify
them as the same content -- which is the exact claim the provenance header
exists to make, so the cheap form is not a weaker version of the field, it is a
false version of it. :data:`FINGERPRINT_CONTENT` is a sha256 over the *ordered
rows* of every table the report read; it is the default, and its cost is linear
in rows read, which section 6 puts in the low thousands per week-long period.
:data:`FINGERPRINT_AGGREGATE` remains available for an interactive spot-check,
and a report generated that way carries :data:`AGGREGATE_STATEMENT` in the
header -- in both renderings -- saying in terms that its fingerprint does not
establish identity of content.

**Non-homogeneity is announced, never averaged over.** More than one
``detector_version`` in the period, or a ``policy_revision_id`` that changed
inside it, means the period contains two different instruments. Section 6:
averaging across that is comparing two detectors and calling it a trend. The
banner is :meth:`ReportHeader.banner`, it is the first thing both renderers
emit, and it is emitted from the shared mapping rather than passed in by a
caller -- there is no code path through this module that renders a header
without it, and no argument that suppresses it. A homogeneous period gets the
banner too, saying so; a silent header would be indistinguishable from one
produced by code that did not look.

**The version-valued fields are sets, and stay sets.** ``detector_versions`` and
``adapter_versions`` expose every value observed in the period. ``Q-0009`` --
what cross-version compatibility means -- **is open** (section 7), so this
module's whole obligation is to expose the set; resolving it to a single value
here would answer ``Q-0009`` by inertia, and would do it invisibly, since a
collapsed set looks exactly like a period that only ever had one version.

**The queries are data.** Every query the report ran is carried as text with a
sha256 over the set, in the same spirit as the spike's ``RECONSTRUCTION_QUERIES``
(``D-0040``): a reader who disbelieves the number can run them by hand. That is
also why the digest is over the *text* -- a query whose ``>=`` became a ``>``
moves the number and moves nothing else in the header.

Nothing here writes, nothing here reads a clock: ``generated_at_ms`` is
injected, like every other instant in this package
(``time-base-policy.md`` section 2), and ``tool_version`` comes from the
package's own :mod:`claude_org_runtime.__about__` rather than a literal, so a
build cannot report a version it is not.

Out of scope, and not implied: this module composes no report. It builds the
header a report carries; the sections that produce the figures are
:mod:`.ac9`, :mod:`.latency`, :mod:`.shadow`, :mod:`.false_termination` and
:mod:`.fixtures`, and the reconcile driver that would act on any of it is not on
this branch at all.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from claude_org_runtime.__about__ import __version__ as TOOL_VERSION
from claude_org_runtime.control_plane import policy
from claude_org_runtime.control_plane.migrator import (
    PRODUCTION_APPLICATION_ID,
    applied_migrations,
)
from claude_org_runtime.measurement.reader import ControlPlaneRefusal

__all__ = [
    "AGGREGATE_STATEMENT",
    "CONTENT_STATEMENT",
    "BOUNDED_IMPUTATION_RULE",
    "SENSITIVITY_IMPUTATION_RULE",
    "FINGERPRINT_AGGREGATE",
    "FINGERPRINT_CONTENT",
    "FINGERPRINT_MODES",
    "HEADER_QUERIES",
    "TOOL_VERSION",
    "CoverageSummary",
    "DatabaseFingerprint",
    "FixtureSuiteRef",
    "ImputationRule",
    "ProvenanceRefusal",
    "FingerprintModeRefused",
    "NotAProductionDatabase",
    "PeriodRefused",
    "QueryDefinitionsRefused",
    "RevisionNotInPeriod",
    "TableNotReadable",
    "QueryCatalogue",
    "ReportHeader",
    "SchemaMigrationHead",
    "build_header",
    "coverage_from_ac9",
    "fingerprint_database",
    "fixture_suite_ref",
    "imputation_from_ac9",
    "iso8601_ms",
    "query_catalogue",
    "render_header_json",
    "render_header_markdown",
]


#: The two fingerprint modes of section 6. ``content`` is the field as
#: specified; ``aggregate`` is the rejected cheap form, kept reachable for an
#: interactive spot-check and stamped as what it is wherever it appears.
FINGERPRINT_CONTENT = "content"
FINGERPRINT_AGGREGATE = "aggregate"
FINGERPRINT_MODES = (FINGERPRINT_CONTENT, FINGERPRINT_AGGREGATE)

#: The sentence a content-mode report makes. Said here once so the renderers,
#: the JSON and the tests cannot disagree about what the digest claims.
CONTENT_STATEMENT = (
    "sha256 over the ordered rows of every table read - two reports carrying "
    "this digest were computed over the same content"
)

#: The sentence an aggregate-mode report is required by section 6 to make. It
#: is not a caveat about precision: an in-place UPDATE (a verdict projection,
#: an outbox status, a gate outcome, a usage_status backfilled by a late
#: adapter) changes the report and moves no count and no maximum, so this
#: digest can be equal across two reads that say different things.
AGGREGATE_STATEMENT = (
    "WEAKER MODE - this fingerprint does NOT establish identity of content. "
    "It is row counts plus MAX(seq)/MAX(rowid), and state this report reads is "
    "updated in place (verdict projection, outbox status, gate outcome, "
    "usage_status backfilled by a late adapter): every one of those changes "
    "the answer and none of them changes a count or a maximum"
)

#: Section 2.4's two imputation rules, in the words that section uses for them.
#: Carried in the header so a reader can recompute under a different rule, which
#: is the reason section 2.4 gives for recording them at all.
BOUNDED_IMPUTATION_RULE = (
    "missing invocations imputed at max_output_tokens * model_response_count "
    "(the caller's own per-request ceiling) - a genuine LOWER BOUND on the "
    "reduction"
)
SENSITIVITY_IMPUTATION_RULE = (
    "missing invocations imputed at the p95 of the covered distribution - an "
    "ASSUMPTION, not a bound: a percentile of the observed sample does not "
    "bound the unobserved values"
)

_BANNER_RULE = "!!" + " " + "=" * 68 + " " + "!!"


class ProvenanceRefusal(ControlPlaneRefusal):
    """A header that cannot be built truthfully, refused rather than approximated.

    Under :class:`~...migrator.ControlPlaneRefusal` like every other refusal in
    the harness: a caller catching the family catches these too, and no caller
    has to know that the provenance header has its own hierarchy in order to
    stop when it cannot be produced.
    """


class PeriodRefused(ProvenanceRefusal):
    """The half-open period is empty, inverted, or not epoch milliseconds."""


class NotAProductionDatabase(ProvenanceRefusal):
    """The file is not the production control plane the header would claim it is.

    Section 6 has ``application_id`` on the header so a report *states* which
    database it was over and that it was a production one. A header built over a
    spike database would make that statement falsely, in the one field a later
    reader would use to check it.
    """


class FingerprintModeRefused(ProvenanceRefusal):
    """An unknown fingerprint mode, or a mode this build cannot compute."""


class TableNotReadable(ProvenanceRefusal):
    """A table named for fingerprinting is not a table in this database.

    Refused rather than skipped. A skipped table hashes to nothing and the
    digest still comes out looking like a fingerprint, so a typo in the table
    list would silently narrow what the digest covers -- and narrowing it is
    exactly the failure the field exists to prevent.
    """


class QueryDefinitionsRefused(ProvenanceRefusal):
    """The query set is empty, or one name carries two different texts."""


class RevisionNotInPeriod(ProvenanceRefusal):
    """The bound policy revision was not in force anywhere in the period.

    Every latency judgement in the report is against that revision's numbers
    (``time-base-policy.md`` section 1), so a report bound to a revision the
    period never ran under is mislabelled in the field a reader would use to
    recompute it.
    """


# --------------------------------------------------------------------------
# instants
# --------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso8601_ms(instant_ms: int) -> str:
    """*instant_ms* as UTC ISO-8601 with milliseconds, e.g. ``...T00:00:00.000Z``.

    Section 6 requires the period bounds printed as **both** epoch ms and
    ISO-8601, and both for the same reason: the epoch value is what a query
    binds and the ISO value is what a human checks against the incident they
    remember. Printing one of them makes the other a mental arithmetic problem,
    and a reader doing epoch arithmetic in their head is how a report gets read
    against the wrong day.

    UTC, always, with a literal ``Z``: a local-time rendering of a period bound
    is ambiguous twice a year in exactly the way that makes two reports over
    "the same day" disagree.
    """

    if not isinstance(instant_ms, int) or isinstance(instant_ms, bool):
        raise PeriodRefused(f"{instant_ms!r} is not epoch milliseconds")
    if instant_ms < 0:
        raise PeriodRefused(
            f"{instant_ms} is before the epoch; the control plane's instants are "
            "non-negative epoch milliseconds (time-base-policy.md section 2)"
        )
    moment = _EPOCH + timedelta(milliseconds=instant_ms)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def _require_period(period_start_ms: int, period_end_ms: int) -> None:
    iso8601_ms(period_start_ms)
    iso8601_ms(period_end_ms)
    if period_end_ms <= period_start_ms:
        raise PeriodRefused(
            f"[{period_start_ms}, {period_end_ms}) is empty or inverted; the "
            "report period is half-open and must contain at least one "
            "millisecond (time-base-policy.md section 2, rule 4)"
        )


# --------------------------------------------------------------------------
# the fingerprint
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseFingerprint:
    """One digest over the tables a report read, and what that digest proves.

    :attr:`statement` travels with the digest rather than being looked up by a
    renderer, because the difference between the two modes is not a difference
    in strength that a reader can be expected to infer from the word
    ``aggregate``: one of them establishes identity of content and the other
    cannot, and which one produced a given report is a fact about that report.
    """

    mode: str
    digest: str
    tables: tuple[str, ...]

    @property
    def establishes_content_identity(self) -> bool:
        return self.mode == FINGERPRINT_CONTENT

    @property
    def statement(self) -> str:
        return (
            CONTENT_STATEMENT
            if self.establishes_content_identity
            else AGGREGATE_STATEMENT
        )


def fingerprint_database(
    connection: sqlite3.Connection,
    *,
    tables: Sequence[str],
    mode: str = FINGERPRINT_CONTENT,
) -> DatabaseFingerprint:
    """Fingerprint the content of *tables*, in :data:`FINGERPRINT_CONTENT` by default.

    The default is the strong mode on purpose: section 6 makes the content hash
    the field, and the aggregate form something a caller must *ask* for. A
    default that had to be argued down would put the weaker claim on every
    report written by a caller who did not know there was a choice.

    Content mode hashes, per table in a canonical order: the table name, its
    column names, then every row, ordered by all of its columns and encoded with
    a type tag and an explicit length per value. The type tag is why ``1`` and
    ``'1'`` do not collide -- SQLite's columns are not typed, and a value that
    changed from an integer to its own decimal string is a change a report can
    see. The length prefix is why ``('a', 'bc')`` and ``('ab', 'c')`` do not.
    The column names are in the hash so a schema change under a report's feet
    moves the digest even where no row moved.

    Ordering by every column rather than by ``rowid`` keeps the digest a
    function of *content*: a table rebuilt by a ``VACUUM`` renumbers rowids and
    changes nothing a report can read, and a digest that moved for that would
    cry wolf at exactly the reader who is trying to establish that two reads
    agree.

    Aggregate mode is section 6's rejected form, reproduced faithfully so that
    the thing it fails to notice is demonstrable: ``COUNT(*)`` plus ``MAX(seq)``
    where the table has a ``seq`` column and ``MAX(rowid)`` otherwise.
    """

    if mode not in FINGERPRINT_MODES:
        raise FingerprintModeRefused(
            f"{mode!r} is not a fingerprint mode; expected one of "
            f"{', '.join(FINGERPRINT_MODES)}"
        )
    if not tables:
        raise TableNotReadable(
            "no tables named for the fingerprint; a digest over nothing is "
            "equal for every database, which is the opposite of what the field "
            "asserts (measurement-harness.md section 6)"
        )

    ordered = tuple(sorted(set(tables)))
    if len(ordered) != len(tuple(tables)):
        raise TableNotReadable(
            f"the fingerprint table list repeats a table: {tuple(tables)!r}; a "
            "table hashed twice makes the digest depend on how the list was "
            "written rather than on what was read"
        )

    hasher = hashlib.sha256()
    hasher.update(mode.encode("utf-8"))
    for table in ordered:
        columns = _columns_of(connection, table)
        _feed(hasher, "T", table.encode("utf-8"))
        for column in columns:
            _feed(hasher, "C", column.encode("utf-8"))
        if mode == FINGERPRINT_CONTENT:
            _feed_rows(hasher, connection, table, columns)
        else:
            _feed_aggregate(hasher, connection, table, columns)

    return DatabaseFingerprint(
        mode=mode, digest=hasher.hexdigest(), tables=ordered
    )


def _columns_of(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        raise TableNotReadable(
            f"{table!r} is not a table in this database; the report cannot "
            "claim a fingerprint over a table it did not read"
        )
    columns = tuple(
        str(info[1])
        for info in connection.execute(f'PRAGMA table_info("{_quoted(table)}")')
    )
    if not columns:
        raise TableNotReadable(f"{table!r} has no columns to fingerprint")
    return columns


def _quoted(identifier: str) -> str:
    """SQLite identifier quoting: a literal ``"`` inside a name is doubled.

    Table names reach here from a caller's list, and the only safe way to put a
    caller-supplied identifier into SQL -- which cannot be a bound parameter --
    is to quote it explicitly rather than to trust that it looks harmless.
    """

    return identifier.replace('"', '""')


def _feed(hasher: "hashlib._Hash", tag: str, payload: bytes) -> None:
    """Append one tagged, length-prefixed field to the digest.

    Both parts are load-bearing. Without the tag an integer and its decimal
    string hash alike, and SQLite will store either in the same column. Without
    the length two adjacent values can be re-split without changing the
    concatenation, so a digest could be equal for two different rows.
    """

    hasher.update(tag.encode("ascii"))
    hasher.update(str(len(payload)).encode("ascii"))
    hasher.update(b":")
    hasher.update(payload)


def _feed_value(hasher: "hashlib._Hash", value: Any) -> None:
    if value is None:
        _feed(hasher, "N", b"")
    elif isinstance(value, int) and not isinstance(value, bool):
        _feed(hasher, "i", str(value).encode("ascii"))
    elif isinstance(value, float):
        _feed(hasher, "f", repr(value).encode("ascii"))
    elif isinstance(value, bytes):
        _feed(hasher, "b", value)
    else:
        _feed(hasher, "s", str(value).encode("utf-8"))


def _feed_rows(
    hasher: "hashlib._Hash",
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> None:
    projection = ", ".join(f'"{_quoted(column)}"' for column in columns)
    statement = (
        f'SELECT {projection} FROM "{_quoted(table)}" ORDER BY {projection}'
    )
    for row in connection.execute(statement):
        _feed(hasher, "R", str(len(row)).encode("ascii"))
        for value in row:
            _feed_value(hasher, value)


def _feed_aggregate(
    hasher: "hashlib._Hash",
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> None:
    maximum = "MAX(seq)" if "seq" in columns else "MAX(rowid)"
    row = connection.execute(
        f'SELECT COUNT(*), {maximum} FROM "{_quoted(table)}"'
    ).fetchone()
    _feed(hasher, "A", maximum.encode("ascii"))
    _feed_value(hasher, row[0])
    _feed_value(hasher, row[1])


# --------------------------------------------------------------------------
# the query set
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryCatalogue:
    """Every query the report ran, as text, plus a sha256 over the set.

    The digest is over the *text* of each query paired with its name. A query
    whose ``>=`` became a ``>`` produces a different report and changes nothing
    else in the header, so the digest is the only field that can notice it; and
    the text is carried in full because section 6's point is that a reader can
    run them by hand, which a digest alone does not permit.
    """

    definitions: Mapping[str, str]
    digest: str


def query_catalogue(definitions: Mapping[str, str]) -> QueryCatalogue:
    """Build the catalogue, refusing an empty set and a name used twice.

    An empty set is refused because a report always ran at least the header's
    own queries: an empty ``query_definitions`` would mean the report cannot
    say what it read, which is section 6's failure exactly.
    """

    if not definitions:
        raise QueryDefinitionsRefused(
            "a report ran at least one query; an empty query_definitions set "
            "leaves the reader nothing to run by hand "
            "(measurement-harness.md section 6)"
        )
    ordered = dict(sorted(definitions.items()))
    hasher = hashlib.sha256()
    for name, text in ordered.items():
        if not isinstance(name, str) or not name:
            raise QueryDefinitionsRefused(f"{name!r} is not a query name")
        if not isinstance(text, str) or not text.strip():
            raise QueryDefinitionsRefused(
                f"query {name!r} carries no text; the queries are the data "
                "here, and a named query with no body is a name"
            )
        _feed(hasher, "Q", name.encode("utf-8"))
        _feed(hasher, "S", text.encode("utf-8"))
    return QueryCatalogue(
        definitions=MappingProxyType(ordered), digest=hasher.hexdigest()
    )


def _merge_queries(
    *sets: Mapping[str, str],
) -> Mapping[str, str]:
    merged: dict[str, str] = {}
    for definitions in sets:
        for name, text in definitions.items():
            existing = merged.get(name)
            if existing is not None and existing != text:
                raise QueryDefinitionsRefused(
                    f"query name {name!r} carries two different texts; the "
                    "digest would be over one of them and the report would "
                    "have run the other"
                )
            merged[name] = text
    return merged


#: The queries this module itself runs. They are in the catalogue for the same
#: reason every other query is: a reader recomputing ``detector_versions`` needs
#: to know it was taken over ``incident.created_at_ms`` half-open, not over
#: ``resolved_at_ms``, and not over every incident in the database.
DETECTOR_VERSIONS_QUERY = """
SELECT DISTINCT detector_version
  FROM incident
 WHERE created_at_ms >= :period_start_ms
   AND created_at_ms <  :period_end_ms
 ORDER BY detector_version
"""

ADAPTER_VERSIONS_QUERY = """
SELECT DISTINCT adapter_version
  FROM ai_invocation
 WHERE started_at_ms >= :period_start_ms
   AND started_at_ms <  :period_end_ms
 ORDER BY adapter_version
"""

HEADER_QUERIES: Mapping[str, str] = MappingProxyType(
    {
        "provenance_detector_versions": DETECTOR_VERSIONS_QUERY,
        "provenance_adapter_versions": ADAPTER_VERSIONS_QUERY,
    }
)


# --------------------------------------------------------------------------
# the remaining header parts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaMigrationHead:
    """Version *and* name of the newest applied step, as section 6 asks.

    The version alone would not survive the ledger being rewritten during
    development, and the name alone does not order. Together they say which
    shape of the schema the numbers came off.
    """

    version: int
    name: str


@dataclass(frozen=True)
class FixtureSuiteRef:
    """Commit and split case count of the labelled corpus, or a stated absence.

    ``absent_reason`` rather than ``None`` for a report that measured no recall:
    a missing ``fixture_suite_ref`` reads as "the corpus was not recorded",
    which is a defect in the report, and this makes "there was no corpus in this
    report" a different, statable thing.

    The split matters (section 3.2): a miss rate over a corpus with no negatives
    is a number a detector that alarms on everything scores perfectly on, so
    ``positive`` and ``negative`` are separate fields and never one total.
    """

    commit: str | None
    positive: int | None
    negative: int | None
    content_digest: str | None
    absent_reason: str | None = None

    @classmethod
    def absent(cls, reason: str) -> "FixtureSuiteRef":
        if not reason.strip():
            raise ProvenanceRefusal(
                "state why this report carries no fixture suite; an unexplained "
                "absence is indistinguishable from a report that forgot to "
                "record one"
            )
        return cls(
            commit=None,
            positive=None,
            negative=None,
            content_digest=None,
            absent_reason=reason,
        )

    @property
    def total(self) -> int | None:
        if self.positive is None or self.negative is None:
            return None
        return self.positive + self.negative


def fixture_suite_ref(corpus: Any, *, commit: str) -> FixtureSuiteRef:
    """Section 6's ``fixture_suite_ref`` from a loaded corpus and its commit.

    *commit* is a required argument with no default and is not derived here:
    the corpus lives in the repository, its commit is a fact about the checkout
    the report ran from, and a module that guessed it (by shelling out to git,
    say) would be recording the commit of whatever tree it happened to run in
    rather than the one the cases came from.

    The corpus's own ``content_digest`` is carried alongside the commit because
    a commit identifies the tree and not the working copy: an edited label that
    was never committed changes every number the report prints and moves no
    commit at all -- the same argument section 6 makes for ``db_fingerprint``
    being content rather than counts.
    """

    if not commit.strip():
        raise ProvenanceRefusal(
            "the labelled corpus's commit is required; 'which cases' is not "
            "answered by a case count (measurement-harness.md section 6)"
        )
    composition = corpus.composition()
    return FixtureSuiteRef(
        commit=commit,
        positive=int(composition["positive"]),
        negative=int(composition["negative"]),
        content_digest=str(corpus.content_digest),
    )


@dataclass(frozen=True)
class ImputationRule:
    """The AC-9 rules in force, and the count nothing can be imputed for.

    ``unbounded_missing`` is on the header and not only in the AC-9 section
    because section 2.4 makes it disqualifying: **a report with a non-zero
    ``unbounded_missing`` count cannot support an AC-9 acceptance claim.** A
    reader who sees only the reduction rate has no way to know that, so the
    count travels with the rules it invalidates.
    """

    bounded: str
    sensitivity: str
    unbounded_missing: int

    @property
    def supports_acceptance_claim(self) -> bool:
        return self.unbounded_missing == 0


def imputation_from_ac9(report: Any) -> ImputationRule:
    """The imputation rule block for an :class:`~.ac9.Ac9Report`.

    The count is read off the report rather than recounted here: two counts of
    the same thing eventually disagree, and the one in the header is the one a
    reader would trust.
    """

    return ImputationRule(
        bounded=BOUNDED_IMPUTATION_RULE,
        sensitivity=SENSITIVITY_IMPUTATION_RULE,
        unbounded_missing=len(report.unbounded_missing),
    )


@dataclass(frozen=True)
class CoverageSummary:
    """AC-9 coverage with both counts, and the excluded-reason breakdown.

    Section 2.4 states the requirement in bold: **coverage and the
    excluded-reason breakdown are required output; a reduction rate printed
    without them is not a valid report.** Both counts, not just the ratio,
    because ``3/4`` and ``3000/4000`` are the same percentage and not the same
    evidence.
    """

    covered: int
    total: int
    excluded: Mapping[str, int]

    @property
    def ratio(self) -> float | None:
        """``None`` at an empty cohort -- not ``0.0``, which claims a measurement."""

        if self.total == 0:
            return None
        return self.covered / self.total


def coverage_from_ac9(report: Any, cohort: Any) -> CoverageSummary:
    """Coverage from an AC-9 report, with the cohort's exclusion breakdown.

    The two come from different objects because they count different things --
    invocations for coverage, runs for exclusions -- and the header carries both
    because a rate over a cohort that quietly dropped half its runs is as
    misleading as one over invocations that quietly dropped their usage records.
    """

    return CoverageSummary(
        covered=int(report.covered_count),
        total=int(report.invocation_count),
        excluded=MappingProxyType(dict(cohort.excluded_counts())),
    )


# --------------------------------------------------------------------------
# the header
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportHeader:
    """Section 6's table, field for field, and the homogeneity verdict over it.

    Both renderings are generated from :meth:`as_mapping`, so the Markdown and
    the JSON carry the same fields by construction rather than by two
    maintainers remembering the same list.
    """

    period_start_ms: int
    period_end_ms: int
    generated_at_ms: int
    tool_version: str
    db_path: str
    application_id: int
    user_version: int
    schema_migration_head: SchemaMigrationHead
    fingerprint: DatabaseFingerprint
    #: The revision every latency in this report was judged against.
    policy_revision_id: int
    #: Every revision in force anywhere in the period. More than one member is
    #: half of what makes the period non-homogeneous.
    policy_revision_ids: tuple[int, ...]
    detector_versions: tuple[str, ...]
    adapter_versions: tuple[str, ...]
    queries: QueryCatalogue
    fixture_suite: FixtureSuiteRef
    imputation: ImputationRule
    coverage: CoverageSummary
    censored: int
    censored_left: int
    unmatched: Mapping[str, int]

    @property
    def database_is_production(self) -> bool:
        return self.application_id == PRODUCTION_APPLICATION_ID

    @property
    def non_homogeneity_reasons(self) -> tuple[str, ...]:
        """Why the period is non-homogeneous, one reason per cause, in words.

        Section 6 names exactly two causes, and they are reported separately
        because the reader's next move differs: two detector versions means the
        recall numbers are two detectors' numbers, two policy revisions means
        the *budgets* moved underneath a latency comparison.
        """

        reasons: list[str] = []
        if len(self.detector_versions) > 1:
            reasons.append(
                "detector_version changed inside the period: "
                f"{', '.join(self.detector_versions)} - the latency and recall "
                "figures are two detectors' figures, and Q-0009 (cross-version "
                "compatibility) is OPEN, so this report exposes the set and "
                "does not resolve it"
            )
        if len(self.policy_revision_ids) > 1:
            reasons.append(
                "policy_revision_id changed inside the period: "
                f"{', '.join(str(revision) for revision in self.policy_revision_ids)}"
                " - the tolerances and budgets every judgement is against were "
                "not the same numbers throughout"
            )
        return tuple(reasons)

    @property
    def non_homogeneous(self) -> bool:
        return bool(self.non_homogeneity_reasons)

    def banner(self) -> tuple[str, ...]:
        """The lines that go at the top of every rendering, homogeneous or not.

        There is no argument that suppresses this and no render path that omits
        it: both renderers take it from :meth:`as_mapping`, which always carries
        it. A banner a caller can turn off is a banner that is off in the report
        that needed it.

        The homogeneous case says so rather than printing nothing, because
        silence is what a header produced by code that never checked also looks
        like.
        """

        reasons = self.non_homogeneity_reasons
        if not reasons:
            detector = (
                self.detector_versions[0]
                if self.detector_versions
                else "none observed"
            )
            revision = (
                str(self.policy_revision_ids[0])
                if self.policy_revision_ids
                else "none in force"
            )
            return (
                f"period is HOMOGENEOUS: one detector_version ({detector}), "
                f"one policy_revision_id ({revision})",
            )
        lines = [
            _BANNER_RULE,
            "!! NON-HOMOGENEOUS PERIOD - DO NOT AVERAGE ACROSS IT",
            "!! a latency comparison across a detector change is comparing two "
            "detectors",
            "!! and calling it a trend (measurement-harness.md section 6)",
        ]
        for reason in reasons:
            lines.append(f"!! - {reason}")
        lines.append(_BANNER_RULE)
        return tuple(lines)

    def as_mapping(self) -> Mapping[str, Any]:
        """The one shape both renderings are generated from.

        Ordered deliberately: the homogeneity verdict first, because a reader
        who stops after the first screen must not stop before it.
        """

        return MappingProxyType(
            {
                "non_homogeneous": self.non_homogeneous,
                "non_homogeneity_reasons": list(self.non_homogeneity_reasons),
                "banner": list(self.banner()),
                "period_start_ms": self.period_start_ms,
                "period_start_iso": iso8601_ms(self.period_start_ms),
                "period_end_ms": self.period_end_ms,
                "period_end_iso": iso8601_ms(self.period_end_ms),
                "period_bounds": "half-open [start, end)",
                "generated_at_ms": self.generated_at_ms,
                "generated_at_iso": iso8601_ms(self.generated_at_ms),
                "tool_version": self.tool_version,
                "db_path": self.db_path,
                "application_id": self.application_id,
                "application_id_hex": f"0x{self.application_id:08X}",
                "database_is_production": self.database_is_production,
                "user_version": self.user_version,
                "schema_migration_head": {
                    "version": self.schema_migration_head.version,
                    "name": self.schema_migration_head.name,
                },
                "db_fingerprint": self.fingerprint.digest,
                "fingerprint_mode": self.fingerprint.mode,
                "fingerprint_tables": list(self.fingerprint.tables),
                "fingerprint_establishes_content_identity": (
                    self.fingerprint.establishes_content_identity
                ),
                "fingerprint_statement": self.fingerprint.statement,
                "policy_revision_id": self.policy_revision_id,
                "policy_revision_ids": list(self.policy_revision_ids),
                "detector_versions": list(self.detector_versions),
                "adapter_versions": list(self.adapter_versions),
                "query_definitions": dict(self.queries.definitions),
                "query_definitions_sha256": self.queries.digest,
                "fixture_suite_ref": {
                    "commit": self.fixture_suite.commit,
                    "positive": self.fixture_suite.positive,
                    "negative": self.fixture_suite.negative,
                    "total": self.fixture_suite.total,
                    "content_digest": self.fixture_suite.content_digest,
                    "absent_reason": self.fixture_suite.absent_reason,
                },
                "imputation_rule": {
                    "bounded": self.imputation.bounded,
                    "sensitivity": self.imputation.sensitivity,
                    "unbounded_missing": self.imputation.unbounded_missing,
                    "supports_acceptance_claim": (
                        self.imputation.supports_acceptance_claim
                    ),
                },
                "coverage": {
                    "covered": self.coverage.covered,
                    "total": self.coverage.total,
                    "ratio": self.coverage.ratio,
                    "excluded": dict(self.coverage.excluded),
                },
                "censored": self.censored,
                "censored_left": self.censored_left,
                "unmatched": dict(self.unmatched),
            }
        )


def build_header(
    connection: sqlite3.Connection,
    *,
    db_path: str,
    period_start_ms: int,
    period_end_ms: int,
    generated_at_ms: int,
    policy_revision_id: int,
    fingerprint_tables: Sequence[str],
    query_definitions: Mapping[str, str],
    fixture_suite: FixtureSuiteRef,
    imputation: ImputationRule,
    coverage: CoverageSummary,
    censored: int,
    censored_left: int,
    unmatched: Mapping[str, int],
    fingerprint_mode: str = FINGERPRINT_CONTENT,
    tool_version: str = TOOL_VERSION,
) -> ReportHeader:
    """Read the database's self-description and assemble section 6's header.

    Every argument is keyword-only and none of the report-shaped ones has a
    default. That is the point of the module: a header field that could go
    missing would go missing precisely on the report that needed it, and the
    caller who forgot ``censored`` would publish a miss rate with no way for a
    reader to see that half of its episodes were cut off by the period boundary.
    ``fingerprint_mode`` and ``tool_version`` do default, to the strong mode and
    to this build's own version -- defaults that cannot be wrong in the flattering
    direction.

    *connection* is expected to be :func:`~.reader.open_for_measurement`'s, which
    is read-only by capability; this function never writes and takes no lease.
    """

    _require_period(period_start_ms, period_end_ms)
    iso8601_ms(generated_at_ms)
    if generated_at_ms < period_end_ms:
        raise PeriodRefused(
            f"generated_at_ms ({generated_at_ms}) precedes the period end "
            f"({period_end_ms}); a report cannot be generated over a period "
            "that has not closed - the rows for its last milliseconds are not "
            "written yet (measurement-harness.md section 3.5)"
        )

    application_id = int(
        connection.execute("PRAGMA application_id").fetchone()[0]
    )
    if application_id != PRODUCTION_APPLICATION_ID:
        raise NotAProductionDatabase(
            f"application_id 0x{application_id:08X} is not the production "
            f"control plane's 0x{PRODUCTION_APPLICATION_ID:08X}; this header "
            "would state that a non-production database was a production one "
            "(production-schema.md section 3)"
        )
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

    applied = applied_migrations(connection)
    if not applied:
        raise ProvenanceRefusal(
            "the schema_migration ledger is empty; there is no migration head "
            "to record, and a report over an unmigrated database is over an "
            "unknown shape"
        )
    newest = applied[-1]
    head = SchemaMigrationHead(
        version=int(newest["version"]), name=str(newest["name"])
    )

    revisions = policy.revision_over_period(
        connection,
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
    )
    if policy_revision_id not in revisions:
        raise RevisionNotInPeriod(
            f"revision {policy_revision_id} was not in force anywhere in "
            f"[{period_start_ms}, {period_end_ms}); the revisions in force were "
            f"{revisions or '(none)'}"
        )

    bounds = {
        "period_start_ms": period_start_ms,
        "period_end_ms": period_end_ms,
    }
    detector_versions = tuple(
        str(row[0])
        for row in connection.execute(DETECTOR_VERSIONS_QUERY, bounds)
    )
    adapter_versions = tuple(
        str(row[0]) for row in connection.execute(ADAPTER_VERSIONS_QUERY, bounds)
    )

    fingerprint = fingerprint_database(
        connection, tables=fingerprint_tables, mode=fingerprint_mode
    )
    queries = query_catalogue(_merge_queries(HEADER_QUERIES, query_definitions))

    if censored < 0 or censored_left < 0:
        raise ProvenanceRefusal(
            f"censored ({censored}) and censored_left ({censored_left}) are "
            "counts and cannot be negative"
        )

    return ReportHeader(
        period_start_ms=period_start_ms,
        period_end_ms=period_end_ms,
        generated_at_ms=generated_at_ms,
        tool_version=tool_version,
        db_path=str(db_path),
        application_id=application_id,
        user_version=user_version,
        schema_migration_head=head,
        fingerprint=fingerprint,
        policy_revision_id=policy_revision_id,
        policy_revision_ids=revisions,
        detector_versions=detector_versions,
        adapter_versions=adapter_versions,
        queries=queries,
        fixture_suite=fixture_suite,
        imputation=imputation,
        coverage=coverage,
        censored=censored,
        censored_left=censored_left,
        unmatched=MappingProxyType(dict(unmatched)),
    )


# --------------------------------------------------------------------------
# the two renderings
# --------------------------------------------------------------------------


def render_header_json(header: ReportHeader) -> str:
    """The JSON rendering: :meth:`ReportHeader.as_mapping`, verbatim.

    ``sort_keys`` is deliberately off: the mapping's order puts the homogeneity
    verdict first, and sorting would bury it under ``adapter_versions``.
    ``ensure_ascii`` is on -- section 6's header is printed to a console that
    may be cp932, and a non-encodable character there crashes the report rather
    than degrading it.
    """

    return json.dumps(_plain(header.as_mapping()), indent=2, ensure_ascii=True)


def render_header_markdown(header: ReportHeader) -> str:
    """The Markdown rendering: the same mapping, flattened into section 6's table.

    Nested blocks are flattened with dotted keys (``coverage.excluded.*``) so
    that every leaf of the mapping reaches the table. A renderer that skipped a
    nested block would produce a Markdown header quietly missing fields the JSON
    one carries, and the reader with the Markdown would never know.
    """

    mapping = header.as_mapping()
    lines: list[str] = list(header.banner())
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    for key, value in mapping.items():
        if key == "banner":
            continue
        for field_name, rendered in _flatten(key, value):
            lines.append(f"| `{field_name}` | {rendered} |")
    return "\n".join(lines) + "\n"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _flatten(prefix: str, value: Any) -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        if not value:
            return [(prefix, "(none)")]
        flattened: list[tuple[str, str]] = []
        for key, item in value.items():
            flattened.extend(_flatten(f"{prefix}.{key}", item))
        return flattened
    return [(prefix, _cell(value))]


def _cell(value: Any) -> str:
    """One table cell, ASCII and pipe-safe.

    A ``|`` inside a query's text would end the cell and shift every field after
    it one column left, which is a rendering that silently mislabels values --
    so it is escaped rather than trusted not to appear.
    """

    if value is None:
        rendered = "(none)"
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (list, tuple)):
        rendered = ", ".join(_cell(item) for item in value) if value else "(none)"
    else:
        rendered = str(value)
    return rendered.replace("|", "\\|").replace("\n", " ").strip()
