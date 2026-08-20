"""The stand-in counterparty's store: append-only, and refused when broken."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_org_runtime.canary.synthetic_v1 import SyntheticStoreRefusal, SyntheticV1RunStore

T0 = 1_700_000_000_000


@pytest.fixture
def store(tmp_path: Path) -> SyntheticV1RunStore:
    return SyntheticV1RunStore.create(tmp_path / "synthetic-v1-runs.jsonl")


def test_creation_refuses_an_existing_path(tmp_path, store):
    with pytest.raises(SyntheticStoreRefusal, match="already exists"):
        SyntheticV1RunStore.create(store.path)


def test_a_run_starts_once(store):
    store.start_run("run-1", now_ms=T0)
    with pytest.raises(SyntheticStoreRefusal, match="already started"):
        store.start_run("run-1", now_ms=T0 + 1)


def test_finishing_appends_rather_than_edits(store):
    # Append-only: the run_started record survives the finish verbatim, so
    # the store's history -- like the ledger's -- is never rewritten.
    store.start_run("run-1", now_ms=T0)
    before = store.path.read_text(encoding="utf-8")
    store.finish_run("run-1", now_ms=T0 + 5)
    after = store.path.read_text(encoding="utf-8")
    assert after.startswith(before)
    assert [r["record"] for r in store.records()] == ["run_started", "run_finished"]


def test_a_finish_needs_a_start_and_happens_once(store):
    with pytest.raises(SyntheticStoreRefusal, match="never started"):
        store.finish_run("run-ghost", now_ms=T0)
    store.start_run("run-1", now_ms=T0)
    store.finish_run("run-1", now_ms=T0 + 1)
    with pytest.raises(SyntheticStoreRefusal, match="already finished"):
        store.finish_run("run-1", now_ms=T0 + 2)


def test_a_broken_store_is_refused_not_read_as_empty(tmp_path):
    # An audit over a store read as empty is an audit that proves nothing, so
    # R3's refusal discipline applies to the stand-in too.
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"record": "run_started", "run_id": "run-1", "at_ms": 1}\nnot json\n',
                      encoding="utf-8")
    with pytest.raises(SyntheticStoreRefusal, match="refused|not a record"):
        SyntheticV1RunStore(broken).records()

    with pytest.raises(SyntheticStoreRefusal, match="does not exist"):
        SyntheticV1RunStore(tmp_path / "absent.jsonl").records()


def test_a_record_without_a_run_id_is_refused(tmp_path):
    keyless = tmp_path / "keyless.jsonl"
    keyless.write_text('{"record": "run_started", "at_ms": 1}\n', encoding="utf-8")
    with pytest.raises(SyntheticStoreRefusal, match="run_id"):
        SyntheticV1RunStore(keyless).records()


def test_an_append_does_not_fuse_onto_a_torn_tail(store):
    # A crash can leave the final record byte-complete but missing its
    # newline; records() still reads that store, so the next legitimate
    # append must not weld two records onto one line and turn a readable
    # store into a refused one.
    store.start_run("run-1", now_ms=T0)
    torn = store.path.read_text(encoding="utf-8").rstrip("\n")
    store.path.write_text(torn, encoding="utf-8")
    assert len(store.records()) == 1  # readable despite the torn tail
    store.start_run("run-2", now_ms=T0 + 1)
    assert [r["run_id"] for r in store.records()] == ["run-1", "run-2"]


def test_run_ids_answer_the_audit_question(store):
    store.start_run("run-b", now_ms=T0)
    store.start_run("run-a", now_ms=T0 + 1)
    store.finish_run("run-b", now_ms=T0 + 2)
    assert store.run_ids() == ("run-a", "run-b")
