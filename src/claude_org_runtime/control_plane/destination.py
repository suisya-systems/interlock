"""S7 -- the destination, and the idempotency record that is **not ours**.

.. warning::

   **Spike scaffold, throwaway by default (D-0026).** Like the S5 schema it sits
   on, nothing in this module is promoted by being depended on, by being
   imported, or by having survived a gate run. ``Q-0001`` stays open.

Why this module exists at all, and why it is a separate file from the handler
that uses it.

``ACCEPTANCE.md`` section 2 draws a line that most of the outbox does not have
to care about, and that this file is entirely about:

    **SQLite alone cannot distinguish "the side effect completed" from "the
    side effect never started"**, because by construction the result was not
    recorded. [...] *A case that asserts exactly-once for an external effect
    using only our own rows does not pass.*

That last sentence is a rule about **evidence**, and it is the acceptance
criterion of Issue ``#14`` that is easiest to satisfy by accident and hardest to
satisfy honestly. Our ``action`` table has a unique index on
``idempotency_key``; it is trivially possible to write a test that inserts one
row, fails to insert a second, and declares exactly-once proven. Such a test
proves that *we did not record two effects*. It does not prove that *two effects
did not happen* -- and those two statements come apart at exactly the injection
point the gate cares about, the kill after the effect but before its result is
recorded.

So the counterparty is a real, separate, durable store with **its own**
deduplication, reached through :class:`Destination`, and the exactly-once
evidence is read back out of that store. It lives in its own module rather than
beside the handler so that the separation is structural and visible: a reader
looking for "where do we cheat?" can see that the ledger the assertions read is
not written by the same transaction that writes our rows. The strongest form of
that, which the suite exercises, is to **delete the control-plane database
entirely** and ask the destination how many effects it applied. The answer is
still one.

**What the filesystem implementation stands in for.** :class:`KeyedDropbox` is a
spike stand-in for a destination that supports an idempotency key -- the shape
of an HTTP API taking an ``Idempotency-Key`` header, or a queue that refuses a
duplicate ``MessageDeduplicationId``. It models the property the gate turns on,
which is that **the destination**, not the sender, refuses the second effect.
Concretely the exclusion comes from ``O_EXCL``: the operating system, not our
code and not our transaction, decides which of two racing applies created the
key. Nothing else about a real destination is modelled and nothing here should
be mistaken for a transport.

**Two-phase records, and why the ledger is not just "the file exists".** A
destination that claimed an effect it had not finished applying would be worse
than one with no record at all: the next attempt would be deduplicated away and
the effect would never happen. So a record is written and only then *completed*,
and completeness is carried in the record itself
(:data:`_COMPLETION_SENTINEL`). A key file that exists but is incomplete is an
attempt that died mid-apply; the next attempt **finishes it** rather than being
turned away by it. Reserving the key and completing the record are separate
steps here for the same reason the outbox and the action row are separate: it is
where a process dies, and pretending otherwise is how the ambiguous window gets
hidden instead of handled.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

__all__ = [
    "ATTEMPT_LOG_NAME",
    "EFFECT_SUFFIX",
    "DeliveryReceipt",
    "Destination",
    "DestinationRefusal",
    "KeyedDropbox",
]

#: Suffix of one effect record. One file per idempotency key, and the key is
#: hashed into the name because an idempotency key is an opaque string that may
#: contain separators, may be longer than a path component, and may differ from
#: another key only in case on a case-insensitive filesystem.
EFFECT_SUFFIX = ".effect.json"

#: The append-only record of every ``apply`` call, deduplicated or not.
#:
#: The effect ledger alone cannot distinguish "one attempt, one effect" from
#: "four attempts, one effect", and the second is the interesting one: it is
#: what proves the destination *refused* the duplicates rather than never having
#: been offered them. A test that shows one effect without showing the attempts
#: that were turned away has not shown deduplication happening.
ATTEMPT_LOG_NAME = "attempts.log"

#: A record ends with this byte. A file without it is an apply that died between
#: reserving the key and finishing the record -- resumable, not a duplicate.
_COMPLETION_SENTINEL = "\n"


class DestinationRefusal(Exception):
    """The destination refused an apply outright.

    Distinct from deduplication, which is a *success*: the effect is present and
    the caller may stop. A refusal means the destination will not carry the
    effect at all, and the caller must not record it as applied.
    """


@dataclass(frozen=True)
class DeliveryReceipt:
    """What the destination says about one ``apply`` call.

    This is the artifact the exactly-once claim is grounded in, so it names its
    own origin: :attr:`destination` and :attr:`receipt_ref` together are a
    handle an operator can follow to the destination's record without going
    through any table of ours.
    """

    #: The key the destination deduplicated on.
    idempotency_key: str
    #: ``True`` when the destination recognised the key and did **not** apply a
    #: second effect. The caller's correct response is to proceed exactly as if
    #: it had applied one -- that is what makes replay safe.
    deduplicated: bool
    #: The destination's identity, for the record we keep of what we talked to.
    destination: str
    #: The destination's **own** reference to its idempotency record.
    receipt_ref: str
    #: ``True`` when the key was already present under a *different* payload.
    #: The destination still applies nothing -- an idempotency key names an
    #: effect, so the same key with new content is a caller bug, not a new
    #: effect -- but it is surfaced rather than swallowed, because silently
    #: deduplicating a payload the caller did not send before would hide a
    #: dedup-key collision behind an exactly-once guarantee.
    payload_conflict: bool = False


@runtime_checkable
class Destination(Protocol):
    """An external effect target that deduplicates on an idempotency key.

    The mechanism ``'destination_idempotency_key'`` in ``ACCEPTANCE.md`` section
    2 is exactly "there is one of these behind the handler". A handler declaring
    that mechanism without a counterparty implementing this protocol is
    declaring something it cannot support.
    """

    #: Stable identity, recorded on the receipt.
    name: str

    def apply(self, idempotency_key: str, payload: str) -> DeliveryReceipt:
        """Apply the effect, or recognise that it is already applied.

        Must be safe to call any number of times with the same key: that is the
        entire content of the guarantee the handler is allowed to claim.
        """

    def effect_count(self, idempotency_key: str) -> int:
        """How many completed effects the destination holds for *key*.

        The number the gate reads. Anything other than ``1`` after a delivery
        has been acked is a failure of item 4, whatever our own rows say.
        """

    def attempt_count(self, idempotency_key: str) -> int:
        """How many times ``apply`` was called for *key*, deduplicated or not."""


class KeyedDropbox:
    """A directory as a destination with its own idempotency ledger.

    One file per idempotency key, created with ``O_EXCL`` so that the exclusion
    belongs to the operating system rather than to a check-then-write in this
    process -- the same reason the lease in ``spike_schema.sql`` validates its
    epoch inside the protected write rather than before it.
    """

    def __init__(self, root: str | Path, name: str = "keyed-dropbox") -> None:
        self.name = name
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    # -- the protocol -----------------------------------------------------

    def apply(self, idempotency_key: str, payload: str) -> DeliveryReceipt:
        if not idempotency_key:
            # An empty key is not a key: every effect would deduplicate against
            # every other one, which is the failure mode that looks most like
            # success.
            raise DestinationRefusal("an idempotency key may not be empty")

        path = self._effect_path(idempotency_key)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._log_attempt(idempotency_key, digest)

        existing = self._read_record(path)
        if existing is not None:
            # A completed record. The effect is present; applying again is the
            # thing this destination exists to refuse.
            return DeliveryReceipt(
                idempotency_key=idempotency_key,
                deduplicated=True,
                destination=self.name,
                receipt_ref=path.name,
                payload_conflict=existing.get("payload_sha256") != digest,
            )

        record = json.dumps(
            {
                "idempotency_key": idempotency_key,
                "payload_sha256": digest,
                "payload": payload,
            },
            sort_keys=True,
        )

        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Either a concurrent apply won the race and has since completed its
            # record, or a previous apply reserved the key and died before
            # completing it. Re-reading tells the two apart, and the incomplete
            # case is finished in place below rather than reported as a
            # duplicate -- turning an unfinished effect away as "already done"
            # is how a message gets lost while every record looks healthy.
            settled = self._read_record(path)
            if settled is not None:
                return DeliveryReceipt(
                    idempotency_key=idempotency_key,
                    deduplicated=True,
                    destination=self.name,
                    receipt_ref=path.name,
                    payload_conflict=settled.get("payload_sha256") != digest,
                )
            handle = os.open(path, os.O_WRONLY | os.O_TRUNC)

        try:
            os.write(handle, (record + _COMPLETION_SENTINEL).encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)
        self._fsync_root()

        return DeliveryReceipt(
            idempotency_key=idempotency_key,
            deduplicated=False,
            destination=self.name,
            receipt_ref=path.name,
        )

    def effect_count(self, idempotency_key: str) -> int:
        return 1 if self._read_record(self._effect_path(idempotency_key)) is not None else 0

    def attempt_count(self, idempotency_key: str) -> int:
        return sum(1 for key, _ in self.attempts() if key == idempotency_key)

    # -- reading the ledger back, which is what the assertions do ---------

    def effects(self) -> Sequence[str]:
        """Every idempotency key the destination holds a completed effect for."""

        keys = []
        for path in sorted(self._root.glob(f"*{EFFECT_SUFFIX}")):
            record = self._read_record(path)
            if record is not None:
                keys.append(str(record["idempotency_key"]))
        return tuple(keys)

    def payload_of(self, idempotency_key: str) -> str | None:
        """The payload the *first* completed apply carried, or ``None``."""

        record = self._read_record(self._effect_path(idempotency_key))
        return None if record is None else str(record["payload"])

    def attempts(self) -> Sequence[tuple[str, str]]:
        """``(idempotency_key, payload_sha256)`` for every apply, in order."""

        log = self._root / ATTEMPT_LOG_NAME
        if not log.exists():
            return ()
        rows = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            entry = json.loads(line)
            rows.append((str(entry["idempotency_key"]), str(entry["payload_sha256"])))
        return tuple(rows)

    # -- internals --------------------------------------------------------

    def _effect_path(self, idempotency_key: str) -> Path:
        stem = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._root / f"{stem}{EFFECT_SUFFIX}"

    def _read_record(self, path: Path) -> dict | None:
        """The record at *path* if it is complete, else ``None``.

        Incompleteness is not corruption: it is an apply that died after
        reserving the key. Reporting it as absent is what lets the next attempt
        finish the effect instead of being deduplicated against a promise
        nobody kept.
        """

        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if not raw.endswith(_COMPLETION_SENTINEL):
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _log_attempt(self, idempotency_key: str, digest: str) -> None:
        line = json.dumps(
            {"idempotency_key": idempotency_key, "payload_sha256": digest},
            sort_keys=True,
        )
        # Appended before the effect is applied, so an attempt that dies
        # mid-apply is still counted. An attempt log that only recorded
        # successes could not distinguish a duplicate that was refused from one
        # that was never made.
        handle = os.open(self._root / ATTEMPT_LOG_NAME, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(handle, (line + "\n").encode("utf-8"))
            os.fsync(handle)
        finally:
            os.close(handle)

    def _fsync_root(self) -> None:
        # A record whose file exists only in the directory cache is a durable
        # claim that is not durable -- the same reason schema.py sets
        # synchronous = FULL. Directory fsync is POSIX-only; on Windows the
        # handle fsync above is what there is.
        try:
            handle = os.open(self._root, os.O_RDONLY)
        except OSError as error:  # pragma: no cover - platform dependent
            if error.errno in (errno.EACCES, errno.EISDIR, errno.EINVAL, errno.ENOTSUP):
                return
            raise
        try:
            os.fsync(handle)
        except OSError:  # pragma: no cover - platform dependent
            pass
        finally:
            os.close(handle)
