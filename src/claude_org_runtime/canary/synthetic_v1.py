"""The synthetic counterparty -- a stand-in for v1, and only a stand-in.

.. warning::

   **This is not v1.** It is a deliberately small append-only run store that
   plays v1's structural part in the item 10 rehearsal: a second system with
   its own store, into which runs the routing point assigns to
   ``synthetic_v1`` are written. It reproduces none of v1's behaviour, load
   or failure modes, which is exactly why the rehearsal it enables is not a
   discharge -- item 10 is discharged at the canary itself, with **live v1**
   as the counterparty (D-0022). Throwaway by default (D-0026).

The store is a JSON-lines file rather than SQLite, on purpose: the
counterparty's store should look like *another system's* store, not like a
second copy of ours -- v1's durable state was files, not a database -- and a
format this dumb keeps anyone from mistaking the stand-in for an
implementation. One record per line, keys sorted, no in-place mutation:
finishing a run appends a ``run_finished`` record rather than editing the
``run_started`` one.

Every write path of the synthetic system lands in this file. That closure is
what makes the writer audit's enumeration of the store a capture of *all*
synthetic-side writes rather than a sample (see :mod:`.audit`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

__all__ = ["SyntheticStoreRefusal", "SyntheticV1RunStore"]


class SyntheticStoreRefusal(Exception):
    """The synthetic store refused a write or an open. Nothing was written."""


class SyntheticV1RunStore:
    """The stand-in system's run store: an append-only JSON-lines file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def create(cls, path: str | Path) -> "SyntheticV1RunStore":
        """Create an empty store, refusing to clobber anything that exists --
        the same explicit-creation discipline as both real stores (R3)."""

        target = Path(path)
        # O_EXCL, not exists()-then-write: the check-then-create window would
        # let a racing creator's store be truncated by the loser -- the same
        # race the ledger and S5 close the same way.
        try:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError as error:
            raise SyntheticStoreRefusal(
                f"{target} already exists; refusing to create over it"
            ) from error
        return cls(target)

    def start_run(self, run_id: str, *, now_ms: int) -> None:
        """Append a ``run_started`` record.

        :raises SyntheticStoreRefusal: if *run_id* was already started; the
            synthetic system, like the real ones, does not start a run twice.
        """

        if run_id in self.run_ids():
            raise SyntheticStoreRefusal(f"run {run_id!r} was already started in this store")
        self._append({"record": "run_started", "run_id": run_id, "at_ms": now_ms})

    def finish_run(self, run_id: str, *, now_ms: int) -> None:
        """Append a ``run_finished`` record for a run this store started.

        :raises SyntheticStoreRefusal: for a run never started here, or
            already finished -- either would fabricate history.
        """

        started, finished = self._started_and_finished()
        if run_id not in started:
            raise SyntheticStoreRefusal(f"run {run_id!r} was never started in this store")
        if run_id in finished:
            raise SyntheticStoreRefusal(f"run {run_id!r} is already finished in this store")
        self._append({"record": "run_finished", "run_id": run_id, "at_ms": now_ms})

    def run_ids(self) -> tuple[str, ...]:
        """Every run this system has written a record for, sorted. This is
        the store's answer to the writer audit's question."""

        return tuple(sorted({record["run_id"] for record in self.records()}))

    def records(self) -> tuple[Mapping[str, Any], ...]:
        """The records, in file order.

        :raises SyntheticStoreRefusal: for a missing or unparseable file --
            refused, never read as empty (R3 applies to the stand-in too,
            because an audit over a store read as empty is an audit that
            proves nothing).
        """

        if not self._path.is_file():
            raise SyntheticStoreRefusal(f"{self._path} does not exist; refusing to read")
        records = []
        for line_number, line in enumerate(
            self._path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise SyntheticStoreRefusal(
                    f"{self._path}:{line_number} is not a record: {error}; a "
                    "broken store is refused, not read as empty"
                ) from error
            if not isinstance(record, dict) or "run_id" not in record:
                raise SyntheticStoreRefusal(
                    f"{self._path}:{line_number} carries no run_id; refusing to audit around it"
                )
            records.append(record)
        return tuple(records)

    def _started_and_finished(self) -> tuple[set, set]:
        started, finished = set(), set()
        for record in self.records():
            if record.get("record") == "run_started":
                started.add(record["run_id"])
            elif record.get("record") == "run_finished":
                finished.add(record["run_id"])
        return started, finished

    def _append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        # A crash can leave a byte-complete final record missing only its
        # newline; records() still reads that store, so a legitimate append
        # must not fuse itself onto the torn tail and turn a readable store
        # into a refused one.
        tail = self._path.read_bytes()[-1:]
        if tail and tail != b"\n":
            line = "\n" + line
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
