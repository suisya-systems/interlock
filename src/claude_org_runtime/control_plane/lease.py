"""S6 -- the lease, and the fencing token validated atomically with each write.

.. warning::

   **Spike scaffold, throwaway by default (D-0026).** This implementation may be
   discarded; its *tests* are the durable half. Nothing here is promoted into the
   real implementation by having discharged a gate item, and ``Q-0001`` -- which
   component may hold which resource -- stays open: :data:`holder` is an opaque
   claimant identity here and deliberately not a role.

``ACCEPTANCE.md`` section 2 states the requirement and names the wrong answer in
the same breath: **expiry discovery alone is insufficient**, because the lease
can expire between the check and the write. So there is no ``is_held()`` that a
caller is expected to consult before writing. Every protected write carries the
lease epoch and validates it **inside the write**, as one statement:

.. code-block:: sql

    UPDATE outbox
       SET status = 'delivered', writer_epoch = :fence_epoch
     WHERE message_id = :message_id
       AND EXISTS (SELECT 1 FROM lease
                    WHERE resource = :fence_resource AND holder = :fence_holder
                      AND epoch = :fence_epoch AND expires_at_ms > :fence_now_ms)

:func:`protected_write` refuses any statement that does not carry
:data:`FENCE_SQL` verbatim, so the unfenced shape cannot reach the database
through this module at all.

**Why the epoch and not the expiry is what a write validates.** Time is the
caller's throughout -- every function takes ``now_ms`` and the database has no
clock of its own (the schema gives no timestamp a DEFAULT for exactly this
reason). Under clock skew two holders really can overlap in *true* time: a
claimant whose clock runs fast sees a lease as expired while its holder still
believes it live, and takes it over. Worse, the rows cannot show that: each
claimant stamps its acquisition in its own frame, so the recorded windows come
out disjoint while the real ones are not. A timeline of lease rows is only ever
as truthful as the clocks that wrote it.

What cannot overlap is **write authority**, because taking the lease over raises
the epoch and the old token then matches nothing. The exclusion is the fence's,
never the clock's -- see :func:`authority_timeline`, which orders by epoch, and
:func:`claimed_timeline`, which shows what the clocks claimed and is not the
same thing.

**No test here may lean on a provider refusing a duplicate.** Under C2 the
provider's own ``already in use`` refusal has a measured admission window (U27)
and the ``--resume`` path excludes nothing at all (U32,
``investigation/pre-spawn-fence-search.md`` section 5.3). This module therefore
imports nothing from :mod:`claude_org_runtime.session` and must keep working
with the provider's refusal assumed absent; the suite asserts the absence of
that import edge rather than describing it.

**Refusals are recorded, never dropped.** A write refused for a stale token is
an ``action`` row in status ``refused`` carrying the reason, the epoch that was
refused, and the lease row as it actually stood. That record is written
**unfenced**, deliberately: a refusal that could only be recorded by a live
holder is a refusal that vanishes exactly when it matters.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

__all__ = [
    "DESTINATIONS",
    "EXACTLY_ONCE_MECHANISMS",
    "FENCE_SQL",
    "WRITE_HISTORY_QUERY",
    "Authority",
    "Claim",
    "ClockSkewRefused",
    "DestinationFencing",
    "DestinationRejectedStaleToken",
    "EpochGuardedDestination",
    "FencedStatement",
    "Lease",
    "LeaseHeld",
    "LeaseNotHeld",
    "LeaseRefusal",
    "LeaseUsageError",
    "ProtectedWrite",
    "PROTECTED_TABLES",
    "ProtectedWriteMissed",
    "StaleWriterRefused",
    "UnfencedStatement",
    "acquire",
    "applied_epoch_regressions",
    "authority_timeline",
    "claimed_timeline",
    "effect_kind",
    "epoch_regressions",
    "fenced_insert",
    "fenced_update",
    "overlapping_claims",
    "read_lease",
    "resource_of_kind",
    "release",
    "renew",
    "write_history",
]


#: The fence, as the exact text a protected statement must carry. It is a
#: constant rather than a template because the check in :func:`protected_write`
#: is a substring test: a fence assembled by string surgery at the call site is a
#: fence that can be assembled slightly wrong, and the failure would be invisible
#: in the row that results.
FENCE_SQL = (
    "EXISTS (SELECT 1 FROM lease\n"
    "                    WHERE resource = :fence_resource\n"
    "                      AND holder = :fence_holder\n"
    "                      AND epoch = :fence_epoch\n"
    "                      AND expires_at_ms > :fence_now_ms)"
)

#: The parameter names :func:`protected_write` binds itself. A caller's own
#: parameters may not use them -- silently overwriting the fence's resource or
#: epoch with the caller's value would leave a statement that still *looks*
#: fenced.
FENCE_PARAMS = ("fence_resource", "fence_holder", "fence_epoch", "fence_now_ms")

#: The three answers ``ACCEPTANCE.md`` section 2 accepts to "how is this effect
#: made exactly-once?", mirrored from the ``action`` table's CHECK. Every
#: protected write names one; a write that cannot name one is a human gate
#: (D-0004), not an automatic retry.
EXACTLY_ONCE_MECHANISMS = (
    "destination_idempotency_key",
    "transactional_with_record",
    "human_gate",
)

#: The write history, as data, so it can be run by hand against a database
#: recovered from a crash (D-0001). ``action`` has no resource column -- which
#: component owns which state item is ``Q-0001`` and open -- so the caller names
#: the effect *kind* it wants the history of, and :func:`effect_kind` is how a
#: kind carries the resource whose epochs its rows were written under -- which is
#: also what lets this filter by resource across every effect taken under one
#: lease.
#:
#: The order is ``rowid``, the database's own insertion order, and **not**
#: ``created_at_ms``. The timestamp is the caller's clock (that is the point of
#: the whole module), so under the skew ``ACCEPTANCE.md`` section 2 injects it can
#: disagree with the order the rows were actually written in -- and an ordering
#: claim read out of a skewed clock would manufacture regressions that never
#: happened and hide ones that did.
WRITE_HISTORY_QUERY = """
    SELECT rowid AS write_seq, action_id, kind, status, writer_epoch,
           refusal_reason, created_at_ms, applied_at_ms
      FROM action
     WHERE writer_epoch IS NOT NULL
       AND (:kind IS NULL OR kind = :kind)
       AND (:resource IS NULL
            OR substr(kind, -(length(:resource) + 1)) = '@' || :resource)
     ORDER BY write_seq
"""


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


class LeaseRefusal(Exception):
    """A lease operation was refused. Nothing was written past the refusal."""


class LeaseHeld(LeaseRefusal):
    """Acquisition refused: the resource has a live holder at the caller's clock."""


class LeaseNotHeld(LeaseRefusal):
    """Renewal or release refused: this token is not the live one any more.

    Raised when the epoch has moved on (someone took the lease over), when the
    holder differs, or -- for a renewal -- when the lease had already expired.
    An expired lease is **not** renewable: re-acquiring is what a returning
    holder must do, and re-acquiring raises the epoch, which is precisely what
    invalidates the token it came back with.
    """


class ClockSkewRefused(LeaseRefusal):
    """A renewal whose new expiry would land at or before the acquisition.

    Only reachable with the caller's clock skewed backwards past the moment the
    lease was taken. Refusing is the safe direction: the alternative is an
    ``expires_at_ms > acquired_at_ms`` CHECK violation surfacing as a generic
    integrity error from inside a write the caller believed was a renewal.
    """


class StaleWriterRefused(LeaseRefusal):
    """A protected write was refused because its fencing token was not live.

    The refusal is durable before this is raised: :attr:`action_id` names the
    ``action`` row in status ``refused`` that records it, and :attr:`observed`
    is the lease row as it actually stood at the moment of the refusal (``None``
    if the resource had no row at all).
    """

    def __init__(self, message: str, *, action_id: str, observed: "Lease | None") -> None:
        super().__init__(message)
        self.action_id = action_id
        self.observed = observed


class ProtectedWriteMissed(LeaseRefusal):
    """The fence held, but the caller's own WHERE clause matched no row.

    Distinguished from :class:`StaleWriterRefused` on purpose, and no refusal is
    recorded for it: writing a "stale writer" row for a write that missed
    because its target did not exist would put a rejection that never happened
    into the evidence gate item 5 is read out of.
    """


class UnfencedStatement(ValueError):
    """A statement was handed to :func:`protected_write` without the fence.

    A programming error, not a runtime condition: the caller wrote the
    check-then-write shape ``ACCEPTANCE.md`` section 2 rules out, and this
    module refuses to be the path by which it reaches the database.
    """


class LeaseUsageError(ValueError):
    """The caller used this module in a way that would break its guarantees."""


class DestinationRejectedStaleToken(Exception):
    """An external destination refused a write carrying an outdated epoch.

    Raised by :class:`EpochGuardedDestination`. It is the destination's own
    refusal, not ours -- which is what ``ACCEPTANCE.md`` section 2 requires for
    an external effect, since our rows cannot tell an effect that completed from
    one that never started.
    """


# --------------------------------------------------------------------------
# argument checks -- defined here because the destination register below is
# built at import time and validates itself with them
# --------------------------------------------------------------------------


def _require_identifier(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise LeaseUsageError(f"{field} must be a non-empty string, got {value!r}")


def _require_int(field: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise LeaseUsageError(
            f"{field} must be an int of epoch milliseconds, got {value!r}"
        )


# --------------------------------------------------------------------------
# the lease itself
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Lease:
    """A held lease, and the fencing token that goes with it.

    The token is ``(resource, holder, epoch)``. The timestamps are carried for
    evidence and for :func:`claimed_timeline`; they are never what a write
    validates.
    """

    resource: str
    holder: str
    epoch: int
    acquired_at_ms: int
    expires_at_ms: int

    def looks_live_at(self, now_ms: int) -> bool:
        """Whether this lease had not expired at *now_ms*, by the caller's clock.

        **Never gate a write on this.** It is the check half of the
        check-then-write shape ``ACCEPTANCE.md`` section 2 rules out: the lease
        can expire, or be taken over, between this returning ``True`` and the
        write landing. It exists for reporting and for tests that need to say
        what a clock *believed*; :func:`protected_write` validates the epoch
        inside the write instead.
        """

        return self.expires_at_ms > now_ms


def acquire(
    connection: sqlite3.Connection,
    *,
    resource: str,
    holder: str,
    now_ms: int,
    ttl_ms: int,
) -> Lease:
    """Take *resource* for *holder* until ``now_ms + ttl_ms``, or refuse.

    One statement, not a read followed by a write: an upsert whose update half
    is conditional on the existing lease having expired at the caller's clock.
    Two claimants racing therefore cannot both win -- the loser's update matches
    no row rather than overwriting the winner.

    **Every takeover raises the epoch, including a re-acquisition by the same
    holder.** A holder that was paused past its expiry and comes back must come
    back with a *new* token; if re-acquiring preserved the old epoch, the writes
    it had in flight under the old one would validate again.

    :raises LeaseHeld: if the resource has a live holder at *now_ms*.
    """

    _require_identifier("resource", resource)
    _require_identifier("holder", holder)
    _require_int("now_ms", now_ms)
    _require_int("ttl_ms", ttl_ms)
    if ttl_ms <= 0:
        raise LeaseUsageError(
            f"ttl_ms must be positive, got {ttl_ms}; a lease that expires at or "
            "before the instant it is taken is not a lease"
        )

    params = {
        "resource": resource,
        "holder": holder,
        "now_ms": now_ms,
        "expires_at_ms": now_ms + ttl_ms,
    }
    with _immediate(connection):
        cursor = connection.execute(
            """
            INSERT INTO lease (resource, holder, epoch, acquired_at_ms, expires_at_ms)
            VALUES (:resource, :holder, 1, :now_ms, :expires_at_ms)
            ON CONFLICT(resource) DO UPDATE
               SET holder = :holder,
                   epoch = lease.epoch + 1,
                   acquired_at_ms = :now_ms,
                   expires_at_ms = :expires_at_ms
             WHERE lease.expires_at_ms <= :now_ms
            """,
            params,
        )
        taken = cursor.rowcount
        current = read_lease(connection, resource)

    if not taken:
        raise LeaseHeld(
            f"lease {resource!r} is held by {current.holder!r} at epoch "
            f"{current.epoch} until {current.expires_at_ms} (now_ms={now_ms}); "
            f"{holder!r} did not take it"
            if current is not None
            else f"lease {resource!r} was not taken by {holder!r}"
        )
    assert current is not None  # the upsert reported a change, so the row exists
    return current


def renew(
    connection: sqlite3.Connection,
    lease: Lease,
    *,
    now_ms: int,
    ttl_ms: int,
) -> Lease:
    """Extend *lease* to ``now_ms + ttl_ms``, keeping its epoch, or refuse.

    A renewal by the holder keeps its epoch, as it must: re-acquiring is what
    invalidates a token, and a renewal that bumped the epoch would invalidate
    the holder's own writes in flight. The statement matches on the whole token
    **and** on the lease still being live, so a lease that expired while the
    holder was paused is not renewable -- the holder has to re-acquire, and
    re-acquiring hands it a new epoch.

    :raises LeaseNotHeld: if the token is no longer the live one.
    :raises ClockSkewRefused: if the new expiry would land at or before the
        acquisition, which needs the clock skewed backwards past it.
    """

    _require_int("now_ms", now_ms)
    _require_int("ttl_ms", ttl_ms)
    if ttl_ms <= 0:
        raise LeaseUsageError(f"ttl_ms must be positive, got {ttl_ms}")

    expires_at_ms = now_ms + ttl_ms
    if expires_at_ms <= lease.acquired_at_ms:
        raise ClockSkewRefused(
            f"renewing {lease.resource!r} at now_ms={now_ms} for {ttl_ms}ms would "
            f"expire it at {expires_at_ms}, at or before it was acquired "
            f"({lease.acquired_at_ms}); the clock has moved backwards past the "
            "acquisition and the renewal is refused rather than written"
        )

    with _immediate(connection):
        cursor = connection.execute(
            """
            UPDATE lease
               SET expires_at_ms = :expires_at_ms
             WHERE resource = :resource
               AND holder = :holder
               AND epoch = :epoch
               AND expires_at_ms > :now_ms
            """,
            {
                "resource": lease.resource,
                "holder": lease.holder,
                "epoch": lease.epoch,
                "now_ms": now_ms,
                "expires_at_ms": expires_at_ms,
            },
        )
        renewed = cursor.rowcount
        current = read_lease(connection, lease.resource)

    if not renewed:
        raise LeaseNotHeld(
            f"{lease.holder!r} cannot renew {lease.resource!r} at epoch "
            f"{lease.epoch} (now_ms={now_ms}): the live row is {_describe(current)}"
        )
    assert current is not None
    return current


def release(connection: sqlite3.Connection, lease: Lease, *, now_ms: int) -> Lease:
    """Give *lease* up by expiring it at *now_ms*, or refuse.

    Releasing is **never** a DELETE. A deleted row would let the next
    acquisition restart the epoch at 1 and hand a returning stale holder a token
    that validates; the schema blocks the DELETE outright and this is the
    supported way to end a lease early.

    **A release only ever shortens.** The new expiry is
    ``MIN(expires_at_ms, MAX(acquired_at_ms + 1, now_ms))``. Both clamps earn
    their place: the inner one keeps a clock skewed behind the acquisition from
    violating the row's own ``expires_at_ms > acquired_at_ms`` CHECK, and the
    outer one keeps a *late* release from pushing the expiry of an
    already-expired lease **forward** -- which would make the releasing holder's
    own token read live again over the interval it had already lost, and would
    withhold the resource from a claimant whose clock falls inside it. Giving a
    lease up may never be the thing that extends it.

    Releasing an already-expired lease is therefore allowed and is a no-op on
    the row, as long as nobody has taken it over. The inner clamp still leaves at
    most a one-millisecond window in which a just-released lease reads as live,
    which is the safe direction: it withholds the resource rather than handing
    it to a second claimant.

    :raises LeaseNotHeld: if the token is not the one the row carries.
    """

    _require_int("now_ms", now_ms)
    with _immediate(connection):
        cursor = connection.execute(
            """
            UPDATE lease
               SET expires_at_ms = MIN(lease.expires_at_ms,
                                       MAX(lease.acquired_at_ms + 1, :now_ms))
             WHERE resource = :resource
               AND holder = :holder
               AND epoch = :epoch
            """,
            {
                "resource": lease.resource,
                "holder": lease.holder,
                "epoch": lease.epoch,
                "now_ms": now_ms,
            },
        )
        released = cursor.rowcount
        current = read_lease(connection, lease.resource)

    if not released:
        raise LeaseNotHeld(
            f"{lease.holder!r} cannot release {lease.resource!r} at epoch "
            f"{lease.epoch}: the live row is {_describe(current)}"
        )
    assert current is not None
    return current


def read_lease(connection: sqlite3.Connection, resource: str) -> Lease | None:
    """The lease row for *resource*, or ``None`` if it has never been taken.

    A read, and only a read: nothing in this module treats its result as
    permission to write.
    """

    row = connection.execute(
        """
        SELECT resource, holder, epoch, acquired_at_ms, expires_at_ms
          FROM lease
         WHERE resource = :resource
        """,
        {"resource": resource},
    ).fetchone()
    if row is None:
        return None
    return Lease(*row)


# --------------------------------------------------------------------------
# the protected write
# --------------------------------------------------------------------------


class FencedStatement(str):
    """SQL that :func:`fenced_update` or :func:`fenced_insert` produced.

    A type, and not a substring check, because a substring check cannot tell a
    fence that **gates** the write from one parked somewhere harmless. ``UPDATE
    t SET x = CASE WHEN <fence> THEN 1 ELSE 2 END WHERE id = :id`` contains
    :data:`FENCE_SQL` verbatim, changes its row under a stale token, and reports
    a positive ``rowcount`` -- a protected write that silently is not one.

    Only the builders can produce an instance; constructing one directly is
    refused. That leaves the shape of every protected statement decided in one
    place, where the fence is appended to the write's own predicate and nowhere
    else.
    """

    __slots__ = ()

    def __new__(cls, sql: str, *, issued_by: object = None) -> "FencedStatement":
        if issued_by is not _BUILDER:
            raise UnfencedStatement(
                "a FencedStatement is issued by fenced_update() or "
                "fenced_insert(), never constructed from SQL text. The builders "
                "put the fence in the write's own predicate; a hand-written "
                "statement can carry FENCE_SQL somewhere that does not gate the "
                "write at all, and no check over the text can tell the two apart"
            )
        return super().__new__(cls, sql)


@dataclass(frozen=True)
class ProtectedWrite:
    """One fenced write, and the record its refusal would be kept as.

    *statement* must be a :class:`FencedStatement` from :func:`fenced_update` or
    :func:`fenced_insert`. Those builders are also where the ``writer_epoch``
    stamp is checked: both protected tables in the spike schema carry the
    column, and a write that does not stamp it from ``:fence_epoch`` leaves a
    history nobody can read the single-writer property out of afterwards.

    *kind* and *idempotency_key* identify the effect, and they are what a
    refusal is recorded under, so they must be meaningful for an attempt that
    never landed. Because the spike ``action`` table has no resource column
    (``Q-0001``), *kind* is also what scopes the write history to one leased
    resource -- build it with :func:`effect_kind` rather than by hand.

    *exactly_once_mechanism* is the answer ``ACCEPTANCE.md`` section 2 requires
    every handler to give; there is no default, because "the handler did not
    say" is the case the requirement exists to catch.
    """

    kind: str
    idempotency_key: str
    statement: FencedStatement
    exactly_once_mechanism: str
    # A default_factory, not MappingProxyType({}): Python 3.11's dataclasses
    # reject a mappingproxy default as mutable, so the module did not import at
    # all there while 3.10 and 3.12 accepted it. The mapping is frozen in
    # __post_init__ instead, which is where it belongs on a frozen dataclass --
    # a default that is only immutable is not the same as a field that is.
    params: Mapping[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    incident_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier("kind", self.kind)
        _require_identifier("idempotency_key", self.idempotency_key)
        if self.exactly_once_mechanism not in EXACTLY_ONCE_MECHANISMS:
            raise LeaseUsageError(
                f"exactly_once_mechanism must be one of {EXACTLY_ONCE_MECHANISMS}, "
                f"got {self.exactly_once_mechanism!r}; ACCEPTANCE.md section 2 asks "
                "every handler to name its mechanism, and an unnamed one is a human "
                "gate (D-0004) rather than an automatic retry"
            )
        if not isinstance(self.statement, FencedStatement):
            raise UnfencedStatement(
                f"the statement for {self.kind!r} was not issued by fenced_update() "
                "or fenced_insert(). A protected write validates the fencing token "
                "as part of the write; checking the lease first and writing "
                "afterwards leaves exactly the race ACCEPTANCE.md section 2 rules "
                "out -- and so does a statement that mentions the fence without "
                "letting it decide whether the row changes"
            )
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        collisions = sorted(set(self.params) & set(FENCE_PARAMS))
        if collisions:
            raise LeaseUsageError(
                f"parameters {collisions} are bound by the fence itself; a caller "
                "value under those names would replace the fence's own resource, "
                "holder, epoch or clock and leave a statement that still looks fenced"
            )


#: The key that makes :class:`FencedStatement` constructible from here and
#: nowhere else.
_BUILDER = object()

#: What "stamps the writer epoch" has to look like: the column assigned the
#: fence's own epoch parameter. Merely *mentioning* ``writer_epoch`` -- in a
#: predicate, or assigned a constant -- leaves a row whose epoch nothing
#: guarantees, and :func:`write_history` would then be reading a number that
#: does not mean what it says.
_STAMP = re.compile(r"\bwriter_epoch\s*=\s*:fence_epoch\b")

#: Every mention of the column, so a second assignment cannot hide behind the
#: first one matching.
_WRITER_EPOCH = re.compile(r"\bwriter_epoch\b")


def fenced_update(
    table: str, *, set_clause: str, where: str, stamps_writer_epoch: bool = True
) -> FencedStatement:
    """An UPDATE whose own WHERE ends in the fence. See :data:`FENCE_SQL`.

    The caller's *where* is parenthesised and ANDed with the fence, so the fence
    decides whether the row changes -- it is not merely present in the text.

    *stamps_writer_epoch* requires ``writer_epoch = :fence_epoch`` in
    *set_clause*. Turn it off only for a target that genuinely has no such
    column, and expect to say why: without the stamp the row leaves no trace of
    the epoch it was written under, and the single-writer property becomes
    unprovable after the fact rather than false.
    """

    _require_table(table)
    _require_fragment("set_clause", set_clause)
    _require_fragment("where", where)
    _require_stamp(table, set_clause, stamps_writer_epoch)
    assigned = _assigned_columns(set_clause)
    forbidden = sorted(assigned & set(_EVIDENCE_COLUMNS))
    if forbidden:
        raise UnfencedStatement(
            f"a protected write may not assign {forbidden} on {table}: those columns "
            "are what a row in the history is attributed by, and a write that "
            "rewrites them replaces evidence rather than adding to it"
        )
    # An applied action row is finished evidence. Without this an update could
    # land on one and restamp its epoch under a later lease, which would rewrite
    # the very attribution write_history() reads the single-writer property out
    # of. Composed here rather than asked of the caller: a guard the caller has
    # to remember is not a guard.
    guard = " AND applied_at_ms IS NULL" if table == "action" else ""
    return FencedStatement(
        f"UPDATE {table}\n"
        f"   SET {set_clause}\n"
        f" WHERE ({where}){guard}\n"
        f"   AND {FENCE_SQL}",
        issued_by=_BUILDER,
    )


def fenced_insert(
    table: str,
    *,
    columns: Sequence[str],
    values: Sequence[str],
    stamps_writer_epoch: bool = True,
) -> FencedStatement:
    """An INSERT ... SELECT whose WHERE is the fence. See :data:`FENCE_SQL`.

    ``INSERT ... VALUES`` cannot carry a WHERE clause, so a fenced insert is an
    ``INSERT ... SELECT``: the row is produced only if the token is live, in the
    same statement that inserts it.

    *stamps_writer_epoch* requires a ``writer_epoch`` column whose value
    expression is exactly ``:fence_epoch`` -- see :func:`fenced_update`.
    """

    _require_table(table)
    if len(columns) != len(values):
        raise LeaseUsageError(
            f"{len(columns)} column(s) but {len(values)} value expression(s)"
        )
    for index, (column, value) in enumerate(zip(columns, values)):
        _require_fragment(f"columns[{index}]", column)
        _require_fragment(f"values[{index}]", value)
    pairs = [f"{column.strip()} = {value.strip()}" for column, value in zip(columns, values)]
    _require_stamp(table, ", ".join(pairs), stamps_writer_epoch)
    return FencedStatement(
        f"INSERT INTO {table} ({', '.join(columns)})\n"
        f"SELECT {', '.join(values)}\n"
        f" WHERE {FENCE_SQL}",
        issued_by=_BUILDER,
    )


#: The tables a protected write may target: S5's six, and nothing else. The
#: table name is interpolated into the statement, and a name is not a fragment
#: the caller gets to compose -- ``"action (x) SELECT 1 WHERE 1 /*"`` would
#: comment out the builder's own columns, values and fence, leaving a statement
#: that inserts under a stale token. A closed set is the check that cannot be
#: walked past by a cleverer string.
PROTECTED_TABLES = ("run", "session", "lease", "outbox", "incident", "action")

#: What a row is *identified and attributed by*. A protected write may not assign
#: any of them: rewriting the kind of a row already in the history replaces the
#: attribution :func:`write_history` is read out of, and the identity columns are
#: frozen by the schema's own triggers for the same reason -- refused here too,
#: where the message says which rule was broken rather than which trigger fired.
#:
#: Lifecycle columns are deliberately absent. ``status``, ``delivered_at_ms`` and
#: ``applied_at_ms`` are what a protected write is usually *for*; the schema
#: keeps those forward-only and set-once, which is a different question from
#: whether a row may be re-attributed.
_EVIDENCE_COLUMNS = ("action_id", "kind", "idempotency_key", "message_id", "dedup_key")


def _require_table(table: str) -> str:
    if table not in PROTECTED_TABLES:
        raise UnfencedStatement(
            f"{table!r} is not one of the protected tables {PROTECTED_TABLES}. The "
            "table name is interpolated into the statement, so it is chosen from a "
            "closed set rather than validated as text -- a name carrying its own "
            "SQL can comment the builder's fence out of the statement entirely"
        )
    return table


def _assigned_columns(set_clause: str) -> set[str]:
    """The column names *set_clause* assigns to, read off its structure."""

    return {
        match.group(1)
        for match in re.finditer(r"(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", _outside_literals(set_clause))
    }


def _require_stamp(table: str, assignments: str, stamps_writer_epoch: bool) -> None:
    if not stamps_writer_epoch:
        return
    # Exactly one, not at least one: SQLite accepts "SET writer_epoch =
    # :fence_epoch, writer_epoch = 1" and applies the *last* assignment, so a
    # statement can satisfy a "contains the stamp" check and still store an
    # epoch the caller chose.
    mentions = len(_WRITER_EPOCH.findall(assignments))
    if mentions != 1 or not _STAMP.search(assignments):
        raise UnfencedStatement(
            f"a protected write to {table} must assign writer_epoch = :fence_epoch "
            f"exactly once; this one names the column {mentions} time(s). The "
            "single-writer property is read back out of the epoch each row was "
            "written under, so a row that carries no epoch -- or one a caller chose "
            "-- is refused here rather than found unprovable later. Pass "
            "stamps_writer_epoch=False if the target genuinely has no such column"
        )


def _outside_literals(fragment: str) -> str:
    """*fragment* with every quoted literal replaced by a blank of equal length.

    A parenthesis inside ``'('`` is text, not structure, and a scan that cannot
    tell the two apart is a scan that can be walked past: ``'(' = '(') OR 1 = 1
    AND ')' = ')'`` balances character for character while genuinely closing the
    wrapper the fence is ANDed onto. So the literals come out first, by SQLite's
    own rules -- ``'...'`` for strings with ``''`` as the escape, ``"..."`` and
    ``[...]`` for identifiers -- and the structural check then runs on what is
    left, which is all and only structure.

    :raises UnfencedStatement: if a quote is never closed, which would swallow
        the fence into a string literal.
    """

    closers = {"'": "'", '"': '"', "[": "]"}
    out = []
    quote: str | None = None
    index = 0
    while index < len(fragment):
        character = fragment[index]
        if quote is None:
            if character in closers:
                quote = closers[character]
                out.append(" ")
            else:
                out.append(character)
            index += 1
            continue
        if character == quote:
            # Doubling is SQLite's escape inside a quoted token, so a pair is
            # content and a single one ends it.
            if quote != "]" and fragment[index + 1 : index + 2] == quote:
                out.append("  ")
                index += 2
                continue
            quote = None
        out.append(" ")
        index += 1
    if quote is not None:
        raise UnfencedStatement(
            f"a quoted token is never closed; the rest of the statement -- the "
            f"fence included -- would be swallowed into it"
        )
    return "".join(out)


def _require_fragment(field: str, fragment: str) -> None:
    """Refuse a SQL fragment that could reach outside the shape it is placed in.

    The builders compose the fence into the statement by text, so a fragment that
    closes a parenthesis it did not open, or opens a comment, can put the fence
    somewhere it no longer gates the write: ``where="id = :id) OR 1 = 1 --"``
    renders as ``WHERE (id = :id) OR 1 = 1 --) AND <fence>``, and every row
    matching ``1 = 1`` changes under a stale token. Nothing about that is
    hypothetical once the fence is claimed to be structural rather than
    conventional, so the structural characters are refused outright -- after the
    quoted literals have been taken out, so that neither a comment nor a
    parenthesis can hide inside a string.

    A protected write's predicate is a predicate, not an opportunity to
    restructure the statement.
    """

    if not isinstance(fragment, str) or not fragment.strip():
        raise LeaseUsageError(f"{field} must be a non-empty SQL fragment")
    structure = _outside_literals(fragment)
    for token in ("--", "/*", "*/", ";"):
        if token in structure:
            raise UnfencedStatement(
                f"{field} contains {token!r}. A comment or statement separator in a "
                "fragment the fence is composed into can move the fence out of the "
                "write's predicate, so it is refused rather than rendered"
            )
    depth = 0
    for character in structure:
        depth += (character == "(") - (character == ")")
        if depth < 0:
            raise UnfencedStatement(
                f"{field} closes a parenthesis it did not open, which would let the "
                "fragment escape the parentheses the fence is ANDed onto"
            )
    if depth:
        raise UnfencedStatement(f"{field} leaves {depth} parenthesis(es) unclosed")


def effect_kind(resource: str, effect: str) -> str:
    """The ``action.kind`` for *effect* performed under the lease on *resource*.

    The spike ``action`` table has **no resource column** -- which component owns
    which state item is ``Q-0001`` and open -- so nothing in a row says which
    lease its ``writer_epoch`` was allocated by. Two resources' histories share a
    table and their epochs are independent, which would make any comparison
    across them meaningless.

    Encoding the resource in ``kind`` is the spike's way out, and it is a
    workaround rather than a design: a real schema would carry the resource as a
    column. :func:`write_history` filters on the composed kind, and
    :func:`applied_epoch_regressions` refuses a history that mixes kinds at all.
    """

    _require_identifier("resource", resource)
    _require_identifier("effect", effect)
    if "@" in effect:
        raise LeaseUsageError(
            f"effect {effect!r} may not contain '@'; it is the separator this kind "
            "is composed with, and an effect that used it would make the resource "
            "unrecoverable from the row"
        )
    return f"{effect}@{resource}"


def resource_of_kind(kind: str) -> str:
    """The resource :func:`effect_kind` composed *kind* for.

    :raises LeaseUsageError: if *kind* was not composed by :func:`effect_kind`.
        A row whose kind does not name a resource cannot say which lease
        allocated its epoch, and the spike ``action`` table has no other column
        that could (``Q-0001``).
    """

    _require_identifier("kind", kind)
    effect, separator, resource = kind.partition("@")
    if not separator or not effect or not resource:
        raise LeaseUsageError(
            f"kind {kind!r} was not composed by effect_kind(resource, effect), so "
            "nothing in the row says which lease its writer_epoch came from"
        )
    return resource


def protected_write(
    connection: sqlite3.Connection,
    lease: Lease,
    write: ProtectedWrite,
    *,
    now_ms: int,
    attempt_id: str | None = None,
) -> int:
    """Run *write* under *lease*, refusing and recording a stale token.

    The validation is not a step before the write; it is a clause *of* the
    write, evaluated by SQLite in the same statement under the same transaction.
    Between the token being live and the row changing there is no instant for
    the lease to expire in.

    The transaction is ``BEGIN IMMEDIATE``, so the write lock is held from
    before the statement runs until after the outcome has been classified. That
    is what makes the classification honest: when the statement changes no row,
    the fence is re-evaluated to tell "the token was stale" from "the caller's
    own WHERE matched nothing", and no other connection can have moved the lease
    in between.

    :returns: the number of rows the statement changed (never zero -- a zero is
        one of the two refusals).
    :raises StaleWriterRefused: the token was not live. An ``action`` row in
        status ``refused`` is committed **before** this is raised.
    :raises ProtectedWriteMissed: the token was live and the caller's WHERE
        matched nothing. Nothing is recorded.
    """

    _require_int("now_ms", now_ms)
    if resource_of_kind(write.kind) != lease.resource:
        # Without this, one kind could accumulate epochs allocated by several
        # different leases, and the history read back under that kind would be
        # two unrelated sequences with no way left to tell them apart.
        raise LeaseUsageError(
            f"kind {write.kind!r} names resource "
            f"{resource_of_kind(write.kind)!r} but the token is for "
            f"{lease.resource!r}; a kind is how an action row records which lease "
            "its epoch was allocated by, so the two may not disagree"
        )
    fence = {
        "fence_resource": lease.resource,
        "fence_holder": lease.holder,
        "fence_epoch": lease.epoch,
        "fence_now_ms": now_ms,
    }
    params = {**dict(write.params), **fence}
    refusal: tuple[str, str, Lease | None] | None = None

    with _immediate(connection):
        cursor = connection.execute(write.statement, params)
        changed = cursor.rowcount
        if changed <= 0:
            fence_holds = bool(
                connection.execute(
                    f"SELECT CASE WHEN {FENCE_SQL} THEN 1 ELSE 0 END", fence
                ).fetchone()[0]
            )
            observed = read_lease(connection, lease.resource)
            if fence_holds:
                # Raising here rolls the transaction back, which discards
                # nothing: the statement changed no row, and no refusal is
                # recorded for a write that was never rejected.
                raise ProtectedWriteMissed(
                    f"{write.kind!r} changed no row although the fencing token "
                    f"({lease.resource!r}, {lease.holder!r}, epoch {lease.epoch}) "
                    "was live at now_ms="
                    f"{now_ms}; the statement's own WHERE matched nothing. No "
                    "refusal was recorded -- this is not a rejected writer"
                )
            reason = (
                f"stale fencing token: {lease.holder!r} presented epoch "
                f"{lease.epoch} for {lease.resource!r} at now_ms={now_ms}; "
                f"the lease row is {_describe(observed)}"
            )
            action_id = _record_refusal(
                connection,
                write,
                lease,
                now_ms=now_ms,
                reason=reason,
                attempt_id=attempt_id,
            )
            refusal = (action_id, reason, observed)

    if refusal is not None:
        action_id, reason, observed = refusal
        raise StaleWriterRefused(
            f"{write.kind!r} was refused and the refusal recorded as action "
            f"{action_id!r}: {reason}",
            action_id=action_id,
            observed=observed,
        )
    return changed


def _record_refusal(
    connection: sqlite3.Connection,
    write: ProtectedWrite,
    lease: Lease,
    *,
    now_ms: int,
    reason: str,
    attempt_id: str | None,
) -> str:
    """Write the durable record of a refused writer, and return its id.

    **Unfenced on purpose.** The refusal exists precisely because the writer's
    token was not live, so a fenced insert here could never land -- the
    rejection would be silently dropped, which is the one thing
    ``ACCEPTANCE.md`` section 2 forbids of it. It rides inside the caller's
    transaction, so the attempt and its record commit together.

    The row is ``status = 'refused'``, which the schema's
    ``action_one_effect_per_key`` index excludes: a writer that keeps coming
    back is recorded every time without any of those records becoming the thing
    that admits a second effect.
    """

    action_id = attempt_id or f"refusal-{uuid.uuid4().hex}"
    connection.execute(
        """
        INSERT INTO action (action_id, run_id, incident_id, kind, idempotency_key,
                            exactly_once_mechanism, status, refusal_reason,
                            writer_epoch, created_at_ms)
        VALUES (:action_id, :run_id, :incident_id, :kind, :idempotency_key,
                :exactly_once_mechanism, 'refused', :refusal_reason,
                :writer_epoch, :created_at_ms)
        """,
        {
            "action_id": action_id,
            "run_id": write.run_id,
            "incident_id": write.incident_id,
            "kind": write.kind,
            "idempotency_key": write.idempotency_key,
            "exactly_once_mechanism": write.exactly_once_mechanism,
            "refusal_reason": reason,
            "writer_epoch": lease.epoch,
            "created_at_ms": now_ms,
        },
    )
    return action_id


# --------------------------------------------------------------------------
# reading the property back
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """What one lease row claimed, in wall-clock terms, while it stood."""

    resource: str
    holder: str
    epoch: int
    from_ms: int
    until_ms: int


@dataclass(frozen=True)
class Authority:
    """When one epoch could actually write, ordered by epoch rather than clock.

    ``until_ms`` is ``None`` for the last epoch observed: its authority ends at
    its expiry, but which instant that is depends on whose clock is asked, and
    this type does not pick one.
    """

    resource: str
    holder: str
    epoch: int
    from_ms: int
    until_ms: int | None


def claimed_timeline(observations: Sequence[Lease]) -> tuple[Claim, ...]:
    """What each lease row claimed, in the clock terms it was written in.

    For rows written by :func:`acquire` these windows are disjoint by
    construction -- a takeover is stamped at or after the previous holder's
    expiry, in the taker's own frame -- so :func:`overlapping_claims` over a
    whole timeline is a real check that every recorded instant had one holder.

    It is emphatically **not** a check that no two processes ever ran at once.
    Under skew the frames differ, and a true-time overlap does not appear in the
    rows at all; the suite shows that case. Reporting the recorded windows for
    what they are is the point -- the exclusion that holds regardless is
    :func:`authority_timeline`'s.
    """

    return tuple(
        Claim(
            resource=lease.resource,
            holder=lease.holder,
            epoch=lease.epoch,
            from_ms=lease.acquired_at_ms,
            until_ms=lease.expires_at_ms,
        )
        for lease in _by_epoch(observations)
    )


def authority_timeline(observations: Sequence[Lease]) -> tuple[Authority, ...]:
    """When each epoch held write authority, from the rows themselves.

    The exclusion this shows is the one that actually holds: an epoch's
    authority ends the instant the next epoch exists, whatever either clock
    said, because from then on the older token matches nothing. Ordering is by
    epoch and never by timestamp -- under skew the acquisition timestamps can go
    backwards while the epochs go forwards, and :func:`epoch_regressions` is
    where that is reported rather than smoothed over.

    *observations* is every state the lease row passed through, in any order.
    The spike schema keeps one row per resource and no history table -- which
    table records lease history is ``Q-0001`` and open -- so the caller collects
    the rows as they are written. What is durable, and readable from SQLite
    alone afterwards, is :func:`write_history`.
    """

    ordered = _by_epoch(observations)
    timeline = []
    for index, lease in enumerate(ordered):
        successor = ordered[index + 1] if index + 1 < len(ordered) else None
        timeline.append(
            Authority(
                resource=lease.resource,
                holder=lease.holder,
                epoch=lease.epoch,
                from_ms=lease.acquired_at_ms,
                until_ms=None if successor is None else successor.acquired_at_ms,
            )
        )
    return tuple(timeline)


def overlapping_claims(claims: Sequence[Claim]) -> tuple[tuple[Claim, Claim], ...]:
    """Pairs of claims by different holders whose recorded windows overlap.

    Empty is the expected answer for rows this module wrote. A non-empty answer
    means some claimant took a lease it had not seen expire -- a lease row
    mutated outside :func:`acquire`, or a second implementation of the takeover
    that dropped the expiry condition from its WHERE.
    """

    overlaps = []
    ordered = sorted(claims, key=lambda claim: (claim.epoch, claim.from_ms))
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            if first.holder == second.holder:
                continue
            if first.from_ms < second.until_ms and second.from_ms < first.until_ms:
                overlaps.append((first, second))
    return tuple(overlaps)


def epoch_regressions(timeline: Sequence[Authority]) -> tuple[tuple[Authority, Authority], ...]:
    """Consecutive epochs whose acquisition timestamps run backwards.

    Empty for rows this module wrote, and for the same reason
    :func:`overlapping_claims` is: a takeover is stamped at or after the
    previous expiry, which is itself after the previous acquisition, so the
    stamps are non-decreasing however skewed each individual clock is.

    Not a violation of the exclusion if it does fire -- the epoch is the order,
    and the epoch never goes back -- but evidence that the timeline was assembled
    from rows some other writer produced, which is worth surfacing rather than
    averaging away.
    """

    return tuple(
        (first, second)
        for first, second in zip(timeline, timeline[1:])
        if second.from_ms < first.from_ms
    )


def write_history(
    connection: sqlite3.Connection,
    *,
    resource: str | None = None,
    kind: str | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Every fenced write attempt recorded in ``action``, oldest first.

    This is the durable half of "at most one live holder": the lease table keeps
    only the current row, but every attempt that reached a protected table is
    stamped with the epoch it was written under, and a refused one is stamped
    with the epoch that was refused. :func:`applied_epoch_regressions` reads the
    single-writer property out of it by query (D-0001: from SQLite alone).

    Filter by *resource*, which is what the single-writer property is about: one
    lease's epochs, across every effect taken under it. Epochs belong to a
    resource and two resources allocate theirs independently, so an unfiltered
    history is several sequences shuffled together and no ordering claim over it
    means anything -- which is why the regression check refuses a history
    spanning more than one resource. *kind* narrows further, to a single effect.

    Rows come back in the database's own insertion order (``rowid``, exposed as
    ``write_seq``), never in the caller's clock order -- see
    :data:`WRITE_HISTORY_QUERY`.

    **This reads ``action``, and only ``action``.** A protected write to another
    table -- S7's ``outbox`` is the case in point -- stamps ``writer_epoch`` on
    *its own* row, and its history is read there by the same shape of query.
    Nothing here synthesises an action row per protected write, and that is
    deliberate: ``action`` is the exactly-once *effect* record, guarded by
    ``action_one_effect_per_key``, and manufacturing a row for a write that is
    not an effect would corrupt the evidence gate item 4 is read out of. What
    ``action`` does carry for every table is the **refusals**, because a refused
    write has no row of its own to be stamped on.
    """

    cursor = connection.execute(WRITE_HISTORY_QUERY, {"kind": kind, "resource": resource})
    try:
        columns = [column[0] for column in cursor.description]
        return tuple(dict(zip(columns, row)) for row in cursor.fetchall())
    finally:
        cursor.close()


def applied_epoch_regressions(
    history: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
    """Applied writes, in time order, whose epoch goes backwards.

    Any pair returned is a rejected writer that got in anyway: its row landed
    between two rows written under a later epoch, which is exactly the
    interleaving ``ACCEPTANCE.md`` section 2 asks the history not to contain.
    Refused rows are ignored -- they are the record that the writer was kept
    out, so their epochs are expected to be lower than what surrounds them.

    The order is the rows' own insertion order, not their timestamps: the clock
    is the caller's and the suite skews it on purpose.

    :raises LeaseUsageError: if *history* spans more than one leased resource, or
        contains a kind :func:`effect_kind` did not compose. Epochs are allocated
        per resource and two resources' sequences are unrelated, so comparing
        them would report a valid epoch 2 for one resource followed by a valid
        epoch 1 for another as a violation -- and would hide real interleavings
        behind the noise. Several *effects* under the same lease do belong in one
        history, and are kept together.
    """

    resources = {resource_of_kind(row["kind"]) for row in history}
    if len(resources) > 1:
        raise LeaseUsageError(
            f"this history spans resources {sorted(resources)}, whose epochs were "
            "allocated under different leases and are not comparable. Filter with "
            "write_history(resource=...) -- one leased resource at a time, across "
            "every effect taken under it"
        )

    applied = [row for row in history if row["status"] == "applied"]
    return tuple(
        (first, second)
        for first, second in zip(applied, applied[1:])
        if second["writer_epoch"] < first["writer_epoch"]
    )


# --------------------------------------------------------------------------
# external destinations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DestinationFencing:
    """What one place an effect lands can do about a stale token.

    Named for the property and not for the place, because S7's
    :class:`~claude_org_runtime.control_plane.destination.Destination` is the
    *place*: a delivery target with a receipt. This is a register entry about one
    -- whether a stale epoch can be refused there, and what is left over when it
    cannot.

    ``ACCEPTANCE.md`` section 2 asks that where a destination can enforce a
    stale token it does, and where it cannot, that this is written down rather
    than assumed away. This type is that sentence made unskippable: a
    destination that cannot enforce must carry a *residual*, and one that can
    must not pretend to carry one.
    """

    name: str
    enforces_stale_token: bool
    note: str
    residual: str | None = None

    def __post_init__(self) -> None:
        _require_identifier("name", self.name)
        _require_identifier("note", self.note)
        if self.enforces_stale_token and self.residual is not None:
            raise LeaseUsageError(
                f"destination {self.name!r} enforces the token, so it has no "
                "residual to record; a residual here would read as a known gap "
                "where there is none"
            )
        if not self.enforces_stale_token and not (self.residual or "").strip():
            raise LeaseUsageError(
                f"destination {self.name!r} cannot enforce the fencing token and "
                "records no residual. ACCEPTANCE.md section 2 requires the gap to "
                "be written down rather than assumed away, so the register refuses "
                "the entry rather than accepting a silent one"
            )


def _register(*destinations: DestinationFencing) -> Mapping[str, DestinationFencing]:
    register: dict[str, DestinationFencing] = {}
    for destination in destinations:
        if destination.name in register:
            raise LeaseUsageError(f"duplicate destination {destination.name!r}")
        register[destination.name] = destination
    return register


#: Where the spike's protected effects land, and what each does about a stale
#: token. ``docs/lease-fencing.md`` carries the same table for a reader, and the
#: suite asserts the two agree -- a written-down residual that drifts from the
#: code is a residual nobody is holding any more.
DESTINATIONS: Mapping[str, DestinationFencing] = _register(
    DestinationFencing(
        name="control_plane_sqlite",
        enforces_stale_token=True,
        note=(
            "The fence is a clause of the write itself, evaluated by SQLite in "
            "the same statement, so a stale epoch changes no row and the refusal "
            "is recorded as an action row."
        ),
    ),
    DestinationFencing(
        name="reference_epoch_guarded_destination",
        enforces_stale_token=True,
        note=(
            "EpochGuardedDestination: keeps its own highest-epoch-seen record "
            "per resource and rejects anything below it, and deduplicates by "
            "effect key. Its own record is the evidence, which is what "
            "ACCEPTANCE.md section 2 requires of an external effect."
        ),
    ),
    DestinationFencing(
        name="session_provider_child_process",
        enforces_stale_token=False,
        note=(
            "A spawned claude -p child takes no token and keeps no effect "
            "record. Its own duplicate refusal is not a substitute: U27 measures "
            "an admission window in which two writers both exited 0 and both "
            "wrote, and U32 finds no exclusion at all on the --resume path "
            "(investigation/pre-spawn-fence-search.md section 5.3)."
        ),
        residual=(
            "Effects on it must be transactional_with_record -- the control-plane "
            "row and the spawn decision commit together -- or a human gate "
            "(D-0004). Nothing in the spike treats the provider's own refusal as "
            "a fence."
        ),
    ),
    DestinationFencing(
        name="worktree_filesystem",
        enforces_stale_token=False,
        note=(
            "A file write carries no epoch, and the filesystem has no idempotency "
            "surface to reject one with."
        ),
        residual=(
            "The control-plane row is written under the fence first and the file "
            "write is derived from it, so a stale writer never reaches the "
            "filesystem; a write that must happen the other way round is a human "
            "gate (D-0004). Gate item 7 covers the worktree lifecycle itself and "
            "is not answered here."
        ),
    ),
)


class EpochGuardedDestination:
    """A reference external destination that enforces the fencing token itself.

    Two properties, and they are separate: it refuses an epoch below the highest
    it has seen for a resource (the fence), and it applies each effect key once
    (idempotency). Both are read out of **its own** record, never ours --
    ``ACCEPTANCE.md`` section 2 is explicit that a case certifying exactly-once
    for an external effect from our rows alone does not count.

    In-process and deliberately trivial: it stands in for a destination with an
    idempotency surface so the enforcing half of the criterion is demonstrated
    rather than asserted.
    """

    def __init__(self, name: str = "reference_epoch_guarded_destination") -> None:
        self.name = name
        self._highest_epoch: dict[str, int] = {}
        self._effects: dict[str, Any] = {}
        self.rejected: list[tuple[str, str, int]] = []

    def apply(self, *, resource: str, holder: str, epoch: int, effect_key: str, payload: Any) -> bool:
        """Apply *payload* under *effect_key*, or reject the epoch.

        :returns: ``True`` if this call produced the effect, ``False`` if the
            key had already been applied -- a duplicate delivery, absorbed.
        :raises DestinationRejectedStaleToken: if *epoch* is below the highest
            this destination has accepted for *resource*.
        """

        highest = self._highest_epoch.get(resource)
        if highest is not None and epoch < highest:
            self.rejected.append((resource, holder, epoch))
            raise DestinationRejectedStaleToken(
                f"{self.name} rejects epoch {epoch} for {resource!r} from "
                f"{holder!r}: it has already accepted epoch {highest}"
            )
        self._highest_epoch[resource] = max(epoch, highest or 0)
        if effect_key in self._effects:
            return False
        self._effects[effect_key] = payload
        return True

    def effect_count(self, effect_key: str) -> int:
        """How many times *effect_key* landed. The destination's own record."""

        return 1 if effect_key in self._effects else 0

    def highest_epoch(self, resource: str) -> int | None:
        return self._highest_epoch.get(resource)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


@contextmanager
def _immediate(connection: sqlite3.Connection) -> Iterator[None]:
    """Own a ``BEGIN IMMEDIATE`` transaction for the duration of the block.

    Immediate rather than deferred: the write lock is taken before the first
    statement, so a second connection cannot slip a lease change between the
    write and the classification of its outcome. Owning it also means the caller
    may not already be in a transaction -- a lease operation nested inside
    somebody else's transaction would commit on their schedule, and the refusal
    record would then be as durable as whatever they decide to do next.
    """

    if connection.in_transaction:
        raise LeaseUsageError(
            "this connection is already in a transaction. A lease operation owns "
            "its transaction: the atomic validation, and the durability of a "
            "recorded refusal, are both properties of the transaction it commits"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _by_epoch(observations: Sequence[Lease]) -> tuple[Lease, ...]:
    resources = {lease.resource for lease in observations}
    if len(resources) > 1:
        raise LeaseUsageError(
            f"a timeline covers one resource; got {sorted(resources)}"
        )
    ordered = sorted(observations, key=lambda lease: lease.epoch)
    seen: dict[int, Lease] = {}
    for lease in ordered:
        # Renewals restate an epoch. The last state an epoch was seen in is the
        # one that stood when the next epoch took over, so it is the one the
        # timeline is built from.
        seen[lease.epoch] = lease
    return tuple(seen[epoch] for epoch in sorted(seen))


def _describe(lease: "Lease | None") -> str:
    if lease is None:
        return "absent"
    return (
        f"holder={lease.holder!r} epoch={lease.epoch} "
        f"acquired_at_ms={lease.acquired_at_ms} expires_at_ms={lease.expires_at_ms}"
    )

