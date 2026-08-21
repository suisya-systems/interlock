"""One transaction, taken up front, shared by every writer on the spine.

``docs/production-schema.md`` section 5.4 and ``D-0030`` do not say "these
writes should be atomic"; they say the append **is** one transaction --- the
event, the per-consumer ``event_consumption`` rows, the ``outbox`` row for every
delivery subscriber and any typed side table commit together or none of them do.
That is what removes v1's push-vs-poll duplication: an event that exists with no
delivery record is exactly the window the second delivery path was invented to
paper over. So the boundary itself has to be a single, named, reviewable thing
rather than a ``BEGIN`` that each module spells its own way.

Three properties are load-bearing, and each is here rather than in a convention.

**``BEGIN IMMEDIATE``, not ``BEGIN``.** A deferred transaction takes the write
lock at its first *write*, so two appenders can both start, both read the
subscription table, and only then discover the conflict --- one of them having
already made decisions from a snapshot that the other invalidated. Taking the
lock up front makes the collision happen at the first statement, where the loser
has decided nothing yet. Section 5.4 requires the subscriber ``SELECT`` to be
inside the same transaction as the fan-out write for precisely this reason, and
a deferred ``BEGIN`` would make that requirement satisfiable in letter while
leaving the race in place.

**``isolation_level`` must already be ``None``, and this is checked, not
assumed.** With the driver's default, :mod:`sqlite3` opens a transaction of its
own before a DML statement and commits it at the next DDL or at
``connection.commit()`` --- which means a multi-statement invariant can be
committed a step at a time by code that never asked for it. The failure is
silent and only visible as a half-written spine after a crash, so a connection
that is not in autocommit mode is refused here instead. :func:`in_autocommit`
is the one-liner for callers that open their own connection.

**Nesting joins rather than nests.** SQLite has no nested transactions (only
savepoints), and the operations that compose --- ``mark_skipped``, which settles
a consumption *and* appends the ``consumption_skipped`` event that makes the
skip distinguishable from a consumer quietly dropping work --- must land in one
transaction, not two. So an inner :func:`transaction` on a connection that is
already in a transaction joins the outer one: it does not ``BEGIN``, does not
``COMMIT``, and lets an exception travel outward to the owner that will roll the
whole thing back. The alternative --- an inner ``COMMIT`` --- would publish half
of an invariant that the outer block was still building.

Nothing anywhere else in the control plane calls ``connection.commit()``. The
commit is here, once.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

__all__ = [
    "TransactionUsageError",
    "current_scope",
    "in_autocommit",
    "transaction",
]

#: The mutable state of the transaction currently open on a connection, keyed
#: by ``id(connection)``.
#:
#: A caller sometimes has to know whether two calls are in *the same*
#: transaction --- section 5.4's back-fill covers "a subscription added in the
#: same transaction as the registration", which is a question about the
#: boundary, not about whether some transaction happens to be open. Answering
#: it from ``connection.in_transaction`` is wrong in exactly one reachable way:
#: two consecutive ``transaction()`` blocks on one connection both report
#: ``True`` inside, so state left by the first is read by the second as if it
#: were its own.
#:
#: So the scope is created by the block that issues ``BEGIN IMMEDIATE`` and
#: removed by the same block in its ``finally``: it cannot outlive its
#: transaction, and there is no clearing step elsewhere that could be forgotten.
#: That also disposes of the ``id()`` reuse hazard --- CPython reuses an id only
#: after the object is freed, and the connection is held alive by the running
#: block for the entire lifetime of its entry here.
_SCOPES: Dict[int, Dict[str, Any]] = {}


def current_scope(connection: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Return the mutable state of the transaction open on *connection*.

    ``None`` when no :func:`transaction` block is open --- and, deliberately,
    the *same* dict for an inner block that joined an outer one, which is what
    makes "were these two calls in one transaction?" answerable at all. Keys
    belong to the module that writes them; prefix them so two callers sharing a
    transaction cannot collide.
    """

    return _SCOPES.get(id(connection))


class TransactionUsageError(ValueError):
    """The connection cannot carry an explicit transaction as it is configured.

    A programming error rather than a runtime condition: the caller handed over
    a connection whose ``isolation_level`` still lets the driver open and commit
    transactions on its own, so the guarantee this module exists to provide
    could not be given. Raised before any statement runs.
    """


def in_autocommit(connection: sqlite3.Connection) -> sqlite3.Connection:
    """Put *connection* in autocommit mode and return it, for chaining.

    Autocommit here means the *driver's* implicit transactions are off, which is
    what lets :func:`transaction` own every boundary. It does not mean writes
    are unprotected --- outside a :func:`transaction` block each statement is
    its own SQLite transaction, which is the correct granularity for a single
    fenced ``UPDATE`` and the wrong one for an append.
    """

    connection.isolation_level = None
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as ONE SQLite transaction, ``BEGIN IMMEDIATE`` .. ``COMMIT``.

    Commits on clean exit, rolls back on any exception --- including one raised
    by the caller's own code inside the block, which is how a typed refusal
    (a stale fencing epoch, a duplicate fact) unwinds every row the block had
    written so far without the caller having to undo anything by hand.

    If *connection* is already inside a transaction the block **joins** it: no
    ``BEGIN``, no ``COMMIT``, and exceptions propagate to whoever owns the
    outermost block. Composed operations therefore commit once, at the outer
    boundary, and a failure anywhere in them leaves nothing behind.

    :raises TransactionUsageError: if ``connection.isolation_level`` is not
        ``None``.
    """

    if connection.isolation_level is not None:
        raise TransactionUsageError(
            "transaction() requires a connection in autocommit mode "
            f"(isolation_level is None), got {connection.isolation_level!r}; "
            "the driver would otherwise commit a step of a multi-statement "
            "invariant on its own -- call in_autocommit(connection) first"
        )

    if connection.in_transaction:
        # Joined, not nested: the owner of the outermost block commits or rolls
        # back, and this block must not do either on its behalf. It owns the
        # scope too, so this block neither creates nor drops one.
        yield connection
        return

    key = id(connection)
    scope: Dict[str, Any] = {}
    _SCOPES[key] = scope
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")
    finally:
        # Dropped by the block that made it, on every exit path, so no scope can
        # be read by the next transaction on this connection.
        if _SCOPES.get(key) is scope:
            del _SCOPES[key]
