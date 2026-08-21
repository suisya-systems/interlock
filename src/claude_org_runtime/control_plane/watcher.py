"""G3 -- watcher liveness: the expected roster, and the fenced unconditional trace.

``docs/production-schema.md`` section 8 and ``D-0035``. The failure this module
is written against is ``tools/relay_scan.py``'s: a broken cron accumulated
undelivered events for twenty days, and nothing in the database said so, because
a watcher that stops writes no row and *no row looks exactly like a clean scan*.
A single ``last_heartbeat_at`` column cannot make four distinctions the incident
needs, and each of the four has its own v1 history:

1. **"polled, nothing changed" versus "poll failed"** -- collapsed, a watcher
   that fails fast looks healthy for as long as it keeps failing;
2. **a replaced watcher's late heartbeat** -- an instance nobody relies on can
   keep proving its own liveness;
3. **a missing watcher** -- an absence writes nothing, so it is invisible;
4. **partial coverage** -- one instance covering three of five scopes reads
   exactly like one covering five.

So there are two tables and this module is their pair of writers.
:func:`register_scope` maintains ``watcher_scope``, the **expected roster**, and
it is what turns "no row" from invisible into a query
(:func:`uncovered_scopes`). :func:`heartbeat` writes ``watcher_liveness`` on
**every attempt**, including the ones that observed nothing, which is what keeps
distinction 1 expressible.

**The fence is inside the write, and the resource is derived inside the write.**
``ACCEPTANCE.md`` section 2: expiry discovery alone is insufficient, because the
lease can expire between the check and the write -- so the heartbeat validates
the scope lease's holder and epoch as a clause of its own statement, the
single-statement shape :mod:`claude_org_runtime.control_plane.lease` establishes.
Section 8.3 then goes one step further than that module has to: the lease
resource is **computed from the scope** as ``'watcher_scope:' || :scope_id``
rather than accepted as a parameter. :func:`heartbeat` therefore has no
``resource`` argument, deliberately and permanently. A separate parameter would
let a watcher holding scope B's lease heartbeat scope A -- the row is written, an
uncovered scope looks healthy, and the ``watcher_silence`` predicate the fence
exists to protect is silenced by the very write it was meant to reject. The API
makes that unrepresentable rather than merely discouraged, and
``test_watcher.py`` proves it.

**Why an upsert rather than an UPDATE.** A newly registered scope has no
liveness row, so a bare ``UPDATE`` changes zero rows on the first heartbeat of
every scope -- and zero rows is also how a stale writer is refused. Bootstrap
would be permanently indistinguishable from rejection. Both arms carry the same
fence, so the insert arm is not a way around it.

**Zero rows has exactly two causes and they are read, never assumed**: the lease
is no longer ours, or a higher epoch already holds the row. :func:`heartbeat`
disambiguates them with one follow-up read inside the same transaction and
records the refusal as an ``action`` row in ``status='refused'`` carrying which
of the two it was -- ``ACCEPTANCE.md`` section 2 requires the rejection of a
stale writer to be itself durable, and a refused heartbeat is never silently
dropped. A third path reaches the same refusal by a different mechanism: a
*different* holder arriving at an *equal* epoch satisfies the upsert's
``holder_epoch <= :epoch`` and is then aborted by the
``watcher_liveness_epoch_is_monotonic`` trigger, so it surfaces as an exception
instead of as zero rows. It is the same stale writer and it is recorded the same
way.

**Both policy reads bind the effective revision.** ``D-0031``'s corollary is
that a ``policy_*`` join without a ``revision_id`` predicate is a defect: it
matches every revision ever recorded and alarms on retired tolerances. The
predicate is written once, as :data:`EFFECTIVE_REVISION_SQL`, and both
:func:`silent_scopes` and :func:`error_streak_scopes` splice that one text.

**Silence and an error streak are different incident classes** and this module
keeps them separate queries because their remedies differ: a dead process versus
a broken credential. Collapsing them would produce one alarm that names neither.

Every timestamp is an integer of milliseconds since the Unix epoch and comes
from the caller. Nothing here reads a clock -- no schema column has a
``DEFAULT`` for the same reason (``ACCEPTANCE.md`` section 2 injects clock skew
across expiry boundaries, and a database-supplied timestamp makes that
untestable).
"""

from __future__ import annotations

import sqlite3
import uuid
from types import MappingProxyType
from typing import Any, Mapping

from .lease import Lease, StaleWriterRefused, effect_kind, read_lease
from .txn import transaction

__all__ = [
    "EFFECTIVE_REVISION_SQL",
    "HEARTBEAT_RESULTS",
    "HeartbeatRefused",
    "ScopeNotRegistered",
    "SCOPE_KINDS",
    "WatcherRefusal",
    "WatcherUsageError",
    "error_streak_scopes",
    "heartbeat",
    "register_scope",
    "retire_scope",
    "scope_lease_resource",
    "silent_scopes",
    "uncovered_scopes",
]


#: The prefix half of the lease resource a scope's watcher must hold. It is a
#: constant so that the Python helper and the SQL below cannot drift: the
#: statement composes the same string with ``||``, and a heartbeat that computed
#: one name while the lease was taken under another would be refused forever for
#: a reason nothing in the rows would explain.
SCOPE_LEASE_PREFIX = "watcher_scope:"

#: The closed result set of ``watcher_liveness.last_result``, mirrored from the
#: table's own CHECK. ``observed_no_change`` is the member the single-column
#: form loses, and losing it is distinction 1 above.
HEARTBEAT_RESULTS = ("observed_change", "observed_no_change", "error")

#: Mirrored from ``watcher_scope``'s CHECK. Kept here so a bad kind is a typed
#: refusal from this module rather than an integrity error from three frames
#: down inside a statement the caller believed was a registration.
SCOPE_KINDS = ("ci_pull_request", "ci_repository")

#: The effective policy revision, as the one text both policy reads splice.
#:
#: ``D-0031``: a ``policy_*`` join without a ``revision_id`` predicate matches
#: every revision ever recorded, so a retired tolerance keeps alarming next to
#: the live one. Writing the predicate once is what keeps the two queries from
#: diverging -- the failure of the alternative is silent, because a query that
#: forgot the predicate still returns rows.
#:
#: ``ORDER BY effective_at_ms DESC, revision_id DESC`` and not by
#: ``effective_at_ms`` alone: two revisions may share an instant (a correction
#: filed the same millisecond), and the later ``revision_id`` is the later
#: decision. Without the tiebreak the pair would resolve arbitrarily and the
#: detector's tolerance would depend on SQLite's row order.
EFFECTIVE_REVISION_SQL = """(SELECT revision_id FROM policy_revision
                              WHERE effective_at_ms <= :now_ms
                              ORDER BY effective_at_ms DESC, revision_id DESC
                              LIMIT 1)"""


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


class WatcherRefusal(Exception):
    """A watcher operation was refused. Nothing was written past the refusal."""


class ScopeNotRegistered(WatcherRefusal):
    """The named scope is not on the roster.

    Raised instead of letting the foreign key fire from inside the heartbeat
    upsert, because the upsert's own failure vocabulary is "zero rows means a
    stale writer" and a missing scope is not a stale writer. Conflating them
    would put an invented refusal into the evidence the roster is read out of.
    """


class HeartbeatRefused(StaleWriterRefused):
    """A heartbeat was refused because its writer was not the live one.

    A subclass of the lease module's refusal because it *is* one: the token was
    validated inside the write, the write changed nothing, and the rejection is
    durable before this is raised. :attr:`action_id` names the ``action`` row in
    ``status='refused'`` and :attr:`observed` is the scope's lease row as it
    actually stood.

    :attr:`cause` is the disambiguation section 8.3 requires to be read rather
    than assumed, and it is one of:

    ``'lease_not_held'``
        The fence's ``EXISTS`` failed: at ``now_ms`` this holder/epoch did not
        hold ``watcher_scope:<scope_id>``. The commonest shape is a watcher
        heartbeating a scope it never held -- including one holding a *different*
        scope's lease, which is why the resource is derived and not passed.
    ``'epoch_superseded'``
        The fence held, but the liveness row already carries a higher
        ``holder_epoch``. A replaced watcher returning with its old token.
    ``'epoch_not_raised_by_new_holder'``
        A different holder arrived at an equal epoch. It passes the upsert's
        ``holder_epoch <= :epoch`` and is aborted by
        ``watcher_liveness_epoch_is_monotonic``, so it reaches us as an
        integrity error rather than as zero rows -- the same stale writer, the
        same durable refusal.
    """

    def __init__(
        self,
        message: str,
        *,
        action_id: str,
        observed: Lease | None,
        cause: str,
    ) -> None:
        super().__init__(message, action_id=action_id, observed=observed)
        self.cause = cause


class WatcherUsageError(ValueError):
    """The caller used this module in a way that would break its guarantees."""


# --------------------------------------------------------------------------
# argument checks
# --------------------------------------------------------------------------


def _require_identifier(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WatcherUsageError(f"{field} must be a non-empty string, got {value!r}")


def _require_int(field: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WatcherUsageError(
            f"{field} must be an int of epoch milliseconds, got {value!r}"
        )


def _rows(cursor: sqlite3.Cursor) -> tuple[Mapping[str, Any], ...]:
    """The cursor's rows as read-only mappings, whatever ``row_factory`` is set.

    The column names come from ``cursor.description`` rather than from a
    ``sqlite3.Row`` factory, because these functions are called on connections
    this module did not open and must not reconfigure.
    """

    names = [column[0] for column in cursor.description]
    return tuple(MappingProxyType(dict(zip(names, row))) for row in cursor.fetchall())


# --------------------------------------------------------------------------
# the roster
# --------------------------------------------------------------------------


def scope_lease_resource(scope_id: str) -> str:
    """The lease resource a watcher must hold to heartbeat *scope_id*.

    The name is a **function of the scope**, which is what makes a misrouted
    heartbeat impossible rather than merely unlikely: :func:`heartbeat` composes
    the same string inside its statement, so there is no argument a caller could
    pass that would aim the fence at some other scope's lease. This helper is
    for the *acquire* side -- a watcher taking the lease it is about to
    heartbeat under -- and for tests; nothing feeds its result back into
    :func:`heartbeat`.
    """

    _require_identifier("scope_id", scope_id)
    return f"{SCOPE_LEASE_PREFIX}{scope_id}"


def register_scope(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    scope_kind: str,
    expected_interval_ms: int,
    registered_at_ms: int,
    repo_id: str,
    pr_id: str | None = None,
) -> None:
    """Put *scope_id* on the expected roster.

    The roster is derived from work that exists -- a scope is registered when a
    run's primary PR is linked and retired when that PR terminates (section 8.2)
    -- and not maintained by hand, because a hand-maintained roster drifts and a
    drifted roster either alarms forever or covers nothing.

    ``expected_interval_ms`` is stored **per scope** because the
    ``watcher_silence`` threshold is a multiple of it (section 8.4). Folding the
    multiple into milliseconds in the policy row would bake one scope's interval
    into a row every other scope reads and silently mis-age all of them.

    The two shape rules are checked here rather than left to the table's CHECKs
    so that the caller gets a sentence instead of an integrity error: a
    ``ci_pull_request`` scope names a PR and a ``ci_repository`` scope does not,
    and every scope names a repository.

    :raises WatcherUsageError: on a bad kind, a missing or surplus ``pr_id``, or
        a non-positive interval.
    """

    _require_identifier("scope_id", scope_id)
    _require_identifier("repo_id", repo_id)
    _require_int("expected_interval_ms", expected_interval_ms)
    _require_int("registered_at_ms", registered_at_ms)
    if scope_kind not in SCOPE_KINDS:
        raise WatcherUsageError(
            f"scope_kind must be one of {SCOPE_KINDS}, got {scope_kind!r}"
        )
    if expected_interval_ms <= 0:
        raise WatcherUsageError(
            f"expected_interval_ms must be positive, got {expected_interval_ms}; "
            "the silence threshold is a multiple of it and a zero interval makes "
            "every scope instantly silent"
        )
    if (scope_kind == "ci_pull_request") != (pr_id is not None):
        raise WatcherUsageError(
            f"scope_kind {scope_kind!r} and pr_id {pr_id!r} disagree: a "
            "'ci_pull_request' scope names the pull request it watches and a "
            "'ci_repository' scope does not"
        )
    if pr_id is not None:
        _require_identifier("pr_id", pr_id)

    with transaction(connection) as txn:
        txn.execute(
            """
            INSERT INTO watcher_scope (scope_id, scope_kind, repo_id, pr_id,
                                       expected_interval_ms, enabled, registered_at_ms)
            VALUES (:scope_id, :scope_kind, :repo_id, :pr_id,
                    :expected_interval_ms, 1, :registered_at_ms)
            """,
            {
                "scope_id": scope_id,
                "scope_kind": scope_kind,
                "repo_id": repo_id,
                "pr_id": pr_id,
                "expected_interval_ms": expected_interval_ms,
                "registered_at_ms": registered_at_ms,
            },
        )


def retire_scope(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    retired_at_ms: int,
) -> None:
    """Take *scope_id* off the roster, without deleting anything.

    Retiring stamps ``retired_at_ms`` and leaves the liveness row where it is.
    Both live-scope predicates read ``enabled = 1 AND retired_at_ms IS NULL``, so
    a retired scope stops being uncovered and stops being silent the moment it is
    retired -- while its last trace stays readable as the evidence of what the
    watcher last saw. Deleting the row instead would take the history with it and
    make a retired scope indistinguishable from one that was never registered.

    ``enabled`` is deliberately not touched: it is the *temporarily disabled*
    axis, and collapsing the two would make a re-activation
    (``retired_at_ms = NULL``) silently leave a scope disabled.

    :raises ScopeNotRegistered: if no such scope is on the roster. A retirement
        that matched nothing is a caller working from a stale roster, and
        swallowing it would let the retirement look done.
    """

    _require_identifier("scope_id", scope_id)
    _require_int("retired_at_ms", retired_at_ms)

    with transaction(connection) as txn:
        changed = txn.execute(
            "UPDATE watcher_scope SET retired_at_ms = :retired_at_ms "
            " WHERE scope_id = :scope_id AND retired_at_ms IS NULL",
            {"scope_id": scope_id, "retired_at_ms": retired_at_ms},
        ).rowcount
        if changed <= 0:
            known = txn.execute(
                "SELECT retired_at_ms FROM watcher_scope WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            raise ScopeNotRegistered(
                f"scope {scope_id!r} is not on the roster; nothing was retired"
                if known is None
                else (
                    f"scope {scope_id!r} was already retired at {known[0]}; "
                    "retirement is not re-stamped, so the first one stays the fact"
                )
            )


# --------------------------------------------------------------------------
# the trace
# --------------------------------------------------------------------------

#: Section 8.3, verbatim in shape. Read the module docstring for why the lease
#: resource is composed here instead of bound as a parameter, and why both arms
#: carry the fence.
_HEARTBEAT_SQL = """
INSERT INTO watcher_liveness (
        scope_id, holder, holder_epoch, last_attempt_at_ms, last_result,
        last_success_at_ms, last_change_at_ms, last_error_at_ms, last_error,
        consecutive_errors, attempt_count)
SELECT :scope_id, :holder, :epoch, :now_ms, :result,
       CASE WHEN :result <> 'error'           THEN :now_ms END,
       CASE WHEN :result =  'observed_change' THEN :now_ms END,
       CASE WHEN :result =  'error'           THEN :now_ms END,
       CASE WHEN :result =  'error'           THEN :error  END,
       CASE WHEN :result =  'error' THEN 1 ELSE 0 END, 1
 WHERE EXISTS (SELECT 1 FROM lease
                WHERE resource = 'watcher_scope:' || :scope_id
                  AND holder = :holder AND epoch = :epoch
                  AND expires_at_ms > :now_ms)
    ON CONFLICT(scope_id) DO UPDATE
   SET holder = :holder, holder_epoch = :epoch,
       last_attempt_at_ms = :now_ms, last_result = :result,
       last_success_at_ms = CASE WHEN :result <> 'error'
                                 THEN :now_ms ELSE last_success_at_ms END,
       last_change_at_ms  = CASE WHEN :result = 'observed_change'
                                 THEN :now_ms ELSE last_change_at_ms END,
       last_error_at_ms   = CASE WHEN :result = 'error'
                                 THEN :now_ms ELSE last_error_at_ms END,
       last_error         = CASE WHEN :result = 'error' THEN :error ELSE NULL END,
       consecutive_errors = CASE WHEN :result = 'error'
                                 THEN consecutive_errors + 1 ELSE 0 END,
       attempt_count      = attempt_count + 1
 WHERE watcher_liveness.holder_epoch <= :epoch
   AND EXISTS (SELECT 1 FROM lease
                WHERE resource = 'watcher_scope:' || :scope_id
                  AND holder = :holder AND epoch = :epoch
                  AND expires_at_ms > :now_ms)
"""

#: The fence on its own, for the follow-up read that tells the two zero-row
#: causes apart. It is the same predicate as the statement's, evaluated inside
#: the same ``BEGIN IMMEDIATE`` transaction -- so nothing can have moved the
#: lease between the refusal and its classification.
_FENCE_PROBE_SQL = """
SELECT EXISTS (SELECT 1 FROM lease
                WHERE resource = 'watcher_scope:' || :scope_id
                  AND holder = :holder AND epoch = :epoch
                  AND expires_at_ms > :now_ms)
"""


def heartbeat(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    holder: str,
    epoch: int,
    result: str,
    now_ms: int,
    error: str | None = None,
) -> None:
    """Record one watcher attempt on *scope_id*, or refuse and record that.

    Called on **every** attempt, including the ones that observed nothing:
    ``result='observed_no_change'`` is a distinct fact from
    ``result='observed_change'`` and from ``result='error'``, and a table that
    cannot tell them apart lets a watcher that fails fast look healthy for as
    long as it keeps failing.

    There is no ``resource`` argument and there never will be one. See the module
    docstring: the scope's lease resource is composed inside the statement, so a
    watcher can only ever heartbeat the scope it actually holds.

    ``last_success_at_ms`` and ``last_error_at_ms`` are **history**. They survive
    the result that did not produce them -- a watcher failing for an hour still
    has to be able to say when it last worked -- which is why the table's
    constraints on them are implications rather than biconditionals, and why the
    ``CASE`` arms above carry the old value forward instead of nulling it.

    :raises ScopeNotRegistered: if *scope_id* is not on the roster. Checked
        before the upsert so that a missing scope reaches the caller as itself
        rather than as a foreign-key error masquerading as a refused writer.
    :raises HeartbeatRefused: if the writer was not the live one, in any of the
        three shapes :class:`HeartbeatRefused` documents. The ``action`` row
        recording the refusal is committed with the refusal, never after it.
    """

    _require_identifier("scope_id", scope_id)
    _require_identifier("holder", holder)
    _require_int("epoch", epoch)
    _require_int("now_ms", now_ms)
    if epoch <= 0:
        raise WatcherUsageError(f"epoch must be positive, got {epoch}")
    if result not in HEARTBEAT_RESULTS:
        raise WatcherUsageError(
            f"result must be one of {HEARTBEAT_RESULTS}, got {result!r}"
        )
    # The table asserts `(last_result = 'error') = (last_error IS NOT NULL)`, so
    # both halves of this are integrity errors waiting to happen. They are
    # caller mistakes, and a caller who attached a message to a success meant
    # something by it -- dropping it silently is the worse of the two answers.
    if (result == "error") != bool(error):
        raise WatcherUsageError(
            f"result={result!r} and error={error!r} disagree: an 'error' attempt "
            "carries a non-empty message and every other result carries none"
        )

    params = {
        "scope_id": scope_id,
        "holder": holder,
        "epoch": epoch,
        "result": result,
        "now_ms": now_ms,
        "error": error,
    }
    refusal: tuple[str, str, str, Lease | None] | None = None

    with transaction(connection) as txn:
        if txn.execute(
            "SELECT 1 FROM watcher_scope WHERE scope_id = ?", (scope_id,)
        ).fetchone() is None:
            raise ScopeNotRegistered(
                f"scope {scope_id!r} is not on the roster, so there is nothing to "
                "heartbeat for; register it before its watcher runs"
            )
        try:
            changed = txn.execute(_HEARTBEAT_SQL, params).rowcount
        except sqlite3.IntegrityError as abort:
            # The only integrity rule the upsert can still break here is the
            # epoch trigger: a DIFFERENT holder at an EQUAL epoch passes
            # `holder_epoch <= :epoch` and is aborted by
            # watcher_liveness_epoch_is_monotonic. RAISE(ABORT) unwinds the
            # statement and not the transaction, so the refusal below lands in
            # the same commit as the attempt it records.
            cause = "epoch_not_raised_by_new_holder"
            reason = (
                f"stale watcher heartbeat: {holder!r} presented epoch {epoch} for "
                f"scope {scope_id!r} at now_ms={now_ms}; a different holder already "
                f"holds the liveness row at that epoch and a new holder must raise "
                f"it ({abort})"
            )
            refusal = (
                _record_refusal(
                    txn,
                    scope_id=scope_id,
                    holder=holder,
                    epoch=epoch,
                    now_ms=now_ms,
                    reason=reason,
                ),
                reason,
                cause,
                read_lease(connection, scope_lease_resource(scope_id)),
            )
            changed = 0

        if refusal is None and changed <= 0:
            fence_holds = bool(txn.execute(_FENCE_PROBE_SQL, params).fetchone()[0])
            observed = read_lease(connection, scope_lease_resource(scope_id))
            if fence_holds:
                held_epoch = txn.execute(
                    "SELECT holder, holder_epoch FROM watcher_liveness WHERE scope_id = ?",
                    (scope_id,),
                ).fetchone()
                cause = "epoch_superseded"
                reason = (
                    f"stale watcher heartbeat: {holder!r} presented epoch {epoch} for "
                    f"scope {scope_id!r} at now_ms={now_ms} while holding its lease; "
                    f"the liveness row is held by {held_epoch[0]!r} at epoch "
                    f"{held_epoch[1]}"
                )
            else:
                cause = "lease_not_held"
                reason = (
                    f"stale watcher heartbeat: {holder!r} presented epoch {epoch} for "
                    f"scope {scope_id!r} at now_ms={now_ms} without holding "
                    f"{scope_lease_resource(scope_id)!r}; the lease row is "
                    f"{_describe(observed)}"
                )
            refusal = (
                _record_refusal(
                    txn,
                    scope_id=scope_id,
                    holder=holder,
                    epoch=epoch,
                    now_ms=now_ms,
                    reason=reason,
                ),
                reason,
                cause,
                observed,
            )

    if refusal is not None:
        action_id, reason, cause, observed = refusal
        raise HeartbeatRefused(
            f"the heartbeat was refused and the refusal recorded as action "
            f"{action_id!r}: {reason}",
            action_id=action_id,
            observed=observed,
            cause=cause,
        )


def _describe(lease: Lease | None) -> str:
    if lease is None:
        return "absent"
    return (
        f"held by {lease.holder!r} at epoch {lease.epoch} until "
        f"{lease.expires_at_ms}"
    )


def _record_refusal(
    connection: sqlite3.Connection,
    *,
    scope_id: str,
    holder: str,
    epoch: int,
    now_ms: int,
    reason: str,
) -> str:
    """Write the durable record of a refused heartbeat, and return its id.

    **Unfenced on purpose**, exactly as the lease module's equivalent is: the
    refusal exists *because* the writer's token was not live, so a fenced insert
    could never land and the rejection would be dropped -- the one thing
    ``ACCEPTANCE.md`` section 2 forbids of it. It rides inside the heartbeat's
    own transaction, so the attempt and the record of its rejection commit
    together or not at all.

    ``status='refused'`` is excluded from ``action_one_effect_per_key``, so a
    watcher that keeps coming back is recorded every time without any of those
    records becoming the thing that admits a second effect. The idempotency key
    still names the attempt uniquely -- a refused row is evidence, and evidence
    that collides is evidence that overwrites.
    """

    action_id = f"watcher-refusal-{uuid.uuid4().hex}"
    connection.execute(
        """
        INSERT INTO action (action_id, kind, idempotency_key, exactly_once_mechanism,
                            status, refusal_reason, writer_epoch, created_at_ms)
        VALUES (:action_id, :kind, :idempotency_key, 'transactional_with_record',
                'refused', :refusal_reason, :writer_epoch, :created_at_ms)
        """,
        {
            "action_id": action_id,
            # effect_kind composes 'watcher_heartbeat@watcher_scope:<id>', which
            # is how lease.write_history can read every effect taken under one
            # scope's lease back out of a table that has no resource column.
            "kind": effect_kind(scope_lease_resource(scope_id), "watcher_heartbeat"),
            "idempotency_key": (
                f"watcher_heartbeat/{scope_id}/{holder}/{epoch}/{now_ms}/{action_id}"
            ),
            "refusal_reason": reason,
            "writer_epoch": epoch,
            "created_at_ms": now_ms,
        },
    )
    return action_id


# --------------------------------------------------------------------------
# the incident queries -- section 8.4, plus the third condition its prose names
# --------------------------------------------------------------------------


def silent_scopes(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
) -> tuple[Mapping[str, Any], ...]:
    """Live scopes whose watcher has stopped attempting, at *now_ms*.

    The threshold is a **multiple of that scope's own**
    ``expected_interval_ms`` -- which is why the policy row stores a multiple
    and not milliseconds, and why this query multiplies rather than compares.
    A single millisecond figure would mis-age every scope whose interval differs
    from the one it was derived under.

    Silence is not an error streak (:func:`error_streak_scopes`): this predicate
    fires on the *absence* of attempts, whatever their results were, and its
    remedy is a dead process rather than a broken credential.
    """

    _require_int("now_ms", now_ms)
    return _rows(
        connection.execute(
            f"""
            SELECT s.scope_id,
                   :now_ms - l.last_attempt_at_ms AS silent_for_ms,
                   l.last_attempt_at_ms,
                   l.last_result,
                   s.expected_interval_ms,
                   p.threshold_value
              FROM watcher_scope s
              JOIN watcher_liveness l ON l.scope_id = s.scope_id
              JOIN policy_detection_latency p
                ON p.incident_class = 'watcher_silence'
               AND p.revision_id = {EFFECTIVE_REVISION_SQL}
             WHERE s.enabled = 1 AND s.retired_at_ms IS NULL
               AND p.threshold_kind = 'scope_interval_multiple'
               AND :now_ms - l.last_attempt_at_ms
                     > s.expected_interval_ms * p.threshold_value
             ORDER BY silent_for_ms DESC, s.scope_id
            """,
            {"now_ms": now_ms},
        )
    )


def uncovered_scopes(
    connection: sqlite3.Connection,
) -> tuple[Mapping[str, Any], ...]:
    """Live scopes with no liveness row at all -- the query a heartbeat table alone cannot express.

    This is ``relay_scan.py``'s lesson as a predicate. A scope nobody is watching
    writes nothing, so every question asked of the trace alone answers "fine";
    only the roster can name the absence. There is no ``now_ms`` because there is
    no waiting involved -- an enabled scope with no trace is wrong the instant it
    exists, which is why ``watcher_scope_uncovered`` carries ``T = 0`` in the
    seeded policy and why no threshold is joined here.
    """

    return _rows(
        connection.execute(
            """
            SELECT s.scope_id, s.scope_kind, s.repo_id, s.pr_id, s.registered_at_ms
              FROM watcher_scope s
              LEFT JOIN watcher_liveness l ON l.scope_id = s.scope_id
             WHERE s.enabled = 1 AND s.retired_at_ms IS NULL
               AND l.scope_id IS NULL
             ORDER BY s.scope_id
            """
        )
    )


def error_streak_scopes(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
) -> tuple[Mapping[str, Any], ...]:
    """Live scopes that are attempting punctually and only ever failing.

    The third condition of section 8.4's closing paragraph, and a **different
    incident class** from silence with a different remedy -- a broken credential
    rather than a dead process. A watcher in this state is invisible to
    :func:`silent_scopes` by construction, because it is heartbeating on time;
    that is the whole reason ``last_result`` exists.

    ``watcher_error_streak`` carries ``threshold_kind = 'consecutive_count'``:
    ``T`` is a count and not a duration, and the comparison is ``>=`` because the
    policy column's own comment defines the budget as running "from the
    ``threshold_value``-th consecutive failure" -- the fifth failure of a
    five-count threshold is the one that opens the incident, not the sixth.
    """

    _require_int("now_ms", now_ms)
    return _rows(
        connection.execute(
            f"""
            SELECT s.scope_id,
                   l.consecutive_errors,
                   l.last_error,
                   l.last_error_at_ms,
                   l.last_success_at_ms,
                   p.threshold_value
              FROM watcher_scope s
              JOIN watcher_liveness l ON l.scope_id = s.scope_id
              JOIN policy_detection_latency p
                ON p.incident_class = 'watcher_error_streak'
               AND p.revision_id = {EFFECTIVE_REVISION_SQL}
             WHERE s.enabled = 1 AND s.retired_at_ms IS NULL
               AND p.threshold_kind = 'consecutive_count'
               AND l.consecutive_errors >= p.threshold_value
             ORDER BY l.consecutive_errors DESC, s.scope_id
            """,
            {"now_ms": now_ms},
        )
    )
