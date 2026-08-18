"""Gate item 9's five negatives, against the layer U8's answer requires.

U8 (``investigation/u8-skill-hot-reload-probe.md``): a running Claude Code
session re-reads skill material from disk, so the filesystem write *is* the
promotion. Every assertion below therefore checks two things -- that the
decision was a refusal *and* that nothing landed in the live skill directory.
A gate that returned "refused" while leaving bytes on disk would have promoted
the candidate, whatever it called itself.

Each negative also asserts the refusal was **recorded**, which is the half of
the acceptance criterion an in-memory return value cannot satisfy.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from claude_org_runtime.curator.gate import RefusalReason
from claude_org_runtime.curator.records import ApprovalRecord, Candidate

from .conftest import SKILL_BODY_V1, SKILL_BODY_V2


def test_positive_control_approved_promotion_lands(harness):
    """The gate is not merely refusing everything."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")

    decision = harness.gate.promote(candidate, "demo", approval)

    assert decision.allowed
    assert harness.skill_material() == {"demo/SKILL.md": SKILL_BODY_V1}
    applied = harness.applied()
    assert len(applied) == 1
    assert applied[0]["approval_id"] == approval.approval_id
    assert applied[0]["content_digest"] == candidate.digest()
    assert harness.refusals() == []


# -- negative 1: approval record absent -----------------------------------


def test_absent_approval_is_refused_and_recorded(harness):
    candidate = harness.propose()

    decision = harness.gate.promote(candidate, "demo", None)

    assert not decision.allowed
    assert decision.reason == RefusalReason.APPROVAL_ABSENT
    assert harness.skill_material() == {}
    refusals = harness.refusals()
    assert [r["reason"] for r in refusals] == [RefusalReason.APPROVAL_ABSENT]
    assert refusals[0]["candidate_id"] == "demo"
    assert refusals[0]["target"] == "demo"


# -- negative 2: approval forged but unrecorded ---------------------------


def test_forged_unrecorded_approval_is_refused_and_recorded(harness):
    """A record the caller made up. It never passed through a human."""

    candidate = harness.propose()
    forged = ApprovalRecord(
        approval_id="forged-0001",
        candidate_id=candidate.candidate_id,
        content_digest=candidate.digest(),  # correct digest -- still not approved
        target="demo",
        approver="operator",
        approved_at="2026-08-18T00:00:00+00:00",
    )

    decision = harness.gate.promote(candidate, "demo", forged)

    assert not decision.allowed
    assert decision.reason == RefusalReason.APPROVAL_UNRECORDED
    assert harness.skill_material() == {}
    refusals = harness.refusals()
    assert refusals[0]["approval_id"] == "forged-0001"
    assert refusals[0]["presented_record_digest"] == forged.record_digest()


def test_recorded_approval_edited_in_flight_is_refused(harness):
    """A real approval, then its fields rewritten before it reaches the gate.

    This is the same negative from the other side: what makes an approval count
    is the ledger, not the object the caller is holding.
    """

    candidate = harness.propose()
    other = harness.curator.propose("other", {"SKILL.md": SKILL_BODY_V2})
    approval = harness.authority.approve(candidate, "demo")

    tampered = replace(
        approval,
        candidate_id=other.candidate_id,
        content_digest=other.digest(),
        target="other",
    )
    decision = harness.gate.promote(other, "other", tampered)

    assert not decision.allowed
    assert decision.reason == RefusalReason.APPROVAL_TAMPERED
    assert harness.skill_material() == {}
    refusal = harness.refusals()[0]
    assert refusal["presented_record_digest"] != refusal["recorded_record_digest"]


# -- negative 3: approval revoked -----------------------------------------


def test_revoked_approval_is_refused_and_recorded(harness):
    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    harness.authority.revoke(approval, reason="withdrawn after review")

    decision = harness.gate.promote(candidate, "demo", approval)

    assert not decision.allowed
    assert decision.reason == RefusalReason.APPROVAL_REVOKED
    assert harness.skill_material() == {}
    assert harness.refusals()[0]["approval_id"] == approval.approval_id


def test_revocation_after_a_successful_promotion_still_blocks_the_next_one(harness):
    """Revocation is not retroactive over bytes already written -- it is the
    *next* write it has to stop, which is the one the gate controls."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    assert harness.gate.promote(candidate, "demo", approval).allowed

    harness.authority.revoke(approval, reason="withdrawn")
    decision = harness.gate.promote(candidate, "demo", approval)

    assert not decision.allowed
    assert decision.reason == RefusalReason.APPROVAL_REVOKED
    # The earlier, approved bytes are still there; nothing new was written.
    assert harness.skill_material() == {"demo/SKILL.md": SKILL_BODY_V1}


# -- negative 4: candidate mutated after approval -------------------------


def test_candidate_mutated_after_approval_is_refused_and_recorded(harness):
    """The reason the digest exists: an approval that merely *exists* would
    accept these bytes."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    approved_digest = approval.content_digest
    harness.mutate(candidate, SKILL_BODY_V2)

    decision = harness.gate.promote(candidate, "demo", approval)

    assert not decision.allowed
    assert decision.reason == RefusalReason.DIGEST_MISMATCH
    assert harness.skill_material() == {}
    refusal = harness.refusals()[0]
    assert refusal["approved_digest"] == approved_digest
    assert refusal["observed_digest"] == candidate.digest()
    assert refusal["observed_digest"] != approved_digest


def test_mutation_that_only_adds_a_file_is_refused(harness):
    """Appending a file leaves every approved byte intact and is still a
    different candidate version."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    harness.add_file(candidate, "extra.md", "payload\n")

    decision = harness.gate.promote(candidate, "demo", approval)

    assert decision.reason == RefusalReason.DIGEST_MISMATCH
    assert harness.skill_material() == {}


def test_mutation_reverted_before_promotion_is_allowed(harness):
    """The digest names bytes, not history: restoring the approved content
    restores the approval's validity."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    harness.mutate(candidate, SKILL_BODY_V2)
    assert not harness.gate.promote(candidate, "demo", approval).allowed
    harness.mutate(candidate, SKILL_BODY_V1)

    # Byte-exact, not merely text-equal: a "revert" that lands CRLF where the
    # approval named LF is a different version, and this assertion is what says
    # so on a platform whose text mode would translate the newlines.
    assert (candidate.root / "SKILL.md").read_bytes() == SKILL_BODY_V1.encode("utf-8")
    assert harness.gate.promote(candidate, "demo", approval).allowed
    assert harness.skill_material() == {"demo/SKILL.md": SKILL_BODY_V1}


# -- line endings are part of the version --------------------------------


def test_curator_writes_candidate_text_without_newline_translation(harness):
    """The Curator is the reference writer: what it puts on disk is the bytes it
    was handed, on every platform. If it translated newlines, the digest taken
    right after ``propose`` would already name something the caller never
    proposed."""

    candidate = harness.propose()

    on_disk = (candidate.root / "SKILL.md").read_bytes()
    assert on_disk == SKILL_BODY_V1.encode("utf-8")
    assert b"\r\n" not in on_disk


def test_a_crlf_rewrite_of_the_approved_text_is_refused(harness):
    """The same characters with different line endings are different bytes, and
    the digest names bytes.

    This is the Windows failure reproduced deliberately: the candidate is
    rewritten with CRLF -- which is precisely what a text-mode write does there
    -- and the gate must both refuse it and accept the LF bytes again once they
    are restored. Refusing is the correct behaviour; the bug was a *writer* that
    produced CRLF while claiming to reproduce the approved content."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")

    with open(candidate.root / "SKILL.md", "w", encoding="utf-8", newline="\r\n") as f:
        f.write(SKILL_BODY_V1)
    assert (candidate.root / "SKILL.md").read_bytes() != SKILL_BODY_V1.encode("utf-8")

    decision = harness.gate.promote(candidate, "demo", approval)
    assert not decision.allowed
    assert decision.reason == RefusalReason.DIGEST_MISMATCH
    assert harness.skill_material() == {}

    harness.mutate(candidate, SKILL_BODY_V1)
    assert harness.gate.promote(candidate, "demo", approval).allowed
    assert harness.skill_material() == {"demo/SKILL.md": SKILL_BODY_V1}


def test_promotion_publishes_the_approved_bytes_verbatim(harness):
    """The published file is the approved bytes, not a re-encoded rendering of
    them: the gate stages with ``open(..., "wb")`` so the promoted tree hashes
    to the digest that was approved."""

    body = "line-1\nline-2\n"
    candidate = harness.curator.propose("demo", {"SKILL.md": body})
    approval = harness.authority.approve(candidate, "demo")

    assert harness.gate.promote(candidate, "demo", approval).allowed

    published = (harness.skill_root / "demo" / "SKILL.md").read_bytes()
    assert published == body.encode("utf-8")
    assert b"\r\n" not in published


# -- negative 5: valid approval replayed against a different candidate ----


def test_valid_approval_replayed_at_another_candidate_is_refused(harness):
    approved = harness.propose("approved", SKILL_BODY_V1)
    other = harness.curator.propose("other", {"SKILL.md": SKILL_BODY_V2})
    approval = harness.authority.approve(approved, "approved")

    decision = harness.gate.promote(other, "approved", approval)

    assert not decision.allowed
    assert decision.reason == RefusalReason.CANDIDATE_MISMATCH
    assert harness.skill_material() == {}
    refusal = harness.refusals()[0]
    assert refusal["candidate_id"] == "other"
    assert refusal["approved_candidate_id"] == "approved"


def test_replay_is_caught_even_when_the_other_candidate_is_byte_identical(harness):
    """Two candidates with the same content are still two candidates. The
    digest alone would wave this one through."""

    approved = harness.propose("approved", SKILL_BODY_V1)
    twin = harness.curator.propose("twin", {"SKILL.md": SKILL_BODY_V1})
    assert approved.digest() == twin.digest()
    approval = harness.authority.approve(approved, "approved")

    decision = harness.gate.promote(twin, "approved", approval)

    assert decision.reason == RefusalReason.CANDIDATE_MISMATCH
    assert harness.skill_material() == {}


def test_valid_approval_replayed_at_another_target_is_refused(harness):
    """The other half of replay: right candidate, wrong destination. An
    approval to publish `demo` is not an approval to overwrite `code-review`."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")

    decision = harness.gate.promote(candidate, "code-review", approval)

    assert not decision.allowed
    assert decision.reason == RefusalReason.TARGET_MISMATCH
    assert harness.skill_material() == {}
    assert harness.refusals()[0]["approved_target"] == "demo"


# -- the boundary itself ---------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["../outside", "/etc/skills", "demo/../../escape", "  ", ".."],
)
def test_targets_that_escape_the_skill_root_are_refused(harness, target):
    """The gate guards one directory; a target that leaves it is not a
    promotion the approval covers."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, target)

    decision = harness.gate.promote(candidate, target, approval)

    assert not decision.allowed
    assert decision.reason == RefusalReason.TARGET_INVALID
    assert harness.skill_material() == {}
    assert harness.refusals()[0]["reason"] == RefusalReason.TARGET_INVALID


def test_every_refusal_is_recorded_even_with_no_prior_ledger_file(harness):
    """A refusal that is only returned, never written down, is not 'recorded'."""

    harness.ledger.path.unlink(missing_ok=True)
    candidate = harness.propose()

    harness.gate.promote(candidate, "demo", None)

    assert harness.ledger.path.exists()
    assert len(harness.refusals()) == 1


def test_curator_cannot_reach_skill_material_on_its_own(harness):
    """The stub writes candidates into the store and nowhere else."""

    harness.propose()

    assert harness.skill_material() == {}
    assert (harness.store_root / "demo" / "SKILL.md").is_file()


# -- the writer itself (review round 1) -----------------------------------


def test_curator_cannot_escape_the_candidate_store(harness):
    """A candidate id that traverses upwards would be a write into skill
    material, and a write into skill material is a promotion (U8). The Curator
    is refused the escape rather than trusted not to take it."""

    escape = f"../../{harness.skill_root.name}/evil"

    with pytest.raises(ValueError, match="traverse upwards"):
        harness.curator.propose(escape, {"SKILL.md": SKILL_BODY_V2})

    with pytest.raises(ValueError, match="traverse upwards"):
        harness.curator.propose("demo", {"../../evil/SKILL.md": SKILL_BODY_V2})

    with pytest.raises(ValueError, match="must be relative"):
        harness.curator.propose(str(harness.skill_root / "evil"), {"SKILL.md": "x"})

    assert harness.skill_material() == {}


def test_promotion_writes_the_snapshot_it_digested_not_a_later_re_read(harness):
    """The gate digests and writes one read of the candidate.

    Modelled with a Candidate that lies: it reports the approved digest while
    the bytes on disk are something else. A gate that trusted the object -- or
    that digested once and re-read at write time -- would publish the bytes
    nobody approved.
    """

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    harness.mutate(candidate, SKILL_BODY_V2)

    class LyingCandidate(Candidate):
        def digest(self) -> str:
            return approval.content_digest

    decision = harness.gate.promote(
        LyingCandidate(candidate_id=candidate.candidate_id, root=candidate.root),
        "demo",
        approval,
    )

    assert not decision.allowed
    assert decision.reason == RefusalReason.DIGEST_MISMATCH
    assert harness.skill_material() == {}


def test_promotion_publishes_exactly_the_approved_tree(harness):
    """A file the new candidate dropped must not stay live: the promoted tree
    has to be the tree the digest names, not a merge with what was there."""

    first = harness.curator.propose(
        "demo", {"SKILL.md": SKILL_BODY_V1, "extra.md": "stale\n"}
    )
    first_approval = harness.authority.approve(first, "demo")
    assert harness.gate.promote(first, "demo", first_approval).allowed
    assert set(harness.skill_material()) == {"demo/SKILL.md", "demo/extra.md"}

    (first.root / "extra.md").unlink()
    harness.mutate(first, SKILL_BODY_V2)
    second_approval = harness.authority.approve(first, "demo")

    assert harness.gate.promote(first, "demo", second_approval).allowed
    assert harness.skill_material() == {"demo/SKILL.md": SKILL_BODY_V2}


def test_promotion_leaves_no_staging_directories_behind(harness):
    """The staging tree is an implementation detail of the swap; a session
    watching this directory must not find it."""

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    assert harness.gate.promote(candidate, "demo", approval).allowed

    leftovers = [
        path.name
        for path in harness.skill_root.parent.iterdir()
        if path.name.startswith((".promote-", ".retired-"))
    ]
    assert leftovers == []
    assert [path.name for path in harness.skill_root.iterdir()] == ["demo"]


def test_nested_candidate_files_are_promoted(harness):
    candidate = harness.curator.propose(
        "demo", {"SKILL.md": SKILL_BODY_V1, "references/notes.md": "detail\n"}
    )
    approval = harness.authority.approve(candidate, "demo")

    assert harness.gate.promote(candidate, "demo", approval).allowed
    assert harness.skill_material() == {
        "demo/SKILL.md": SKILL_BODY_V1,
        "demo/references/notes.md": "detail\n",
    }


# -- store confinement and serialization (review round 2) -----------------


def test_curator_refuses_a_destination_reached_through_a_symlink(harness):
    """`..` is not the only way out of the store. A symlink already in it is a
    direct route into skill material, and it has to be refused *before* the
    bytes are written -- afterwards they would already be live."""

    harness.skill_root.mkdir(parents=True, exist_ok=True)
    harness.store_root.mkdir(parents=True, exist_ok=True)
    (harness.store_root / "demo").symlink_to(harness.skill_root, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        harness.curator.propose("demo", {"SKILL.md": SKILL_BODY_V2})

    assert harness.skill_material() == {}


def test_curator_refuses_a_symlinked_subdirectory_in_the_candidate(harness):
    harness.skill_root.mkdir(parents=True, exist_ok=True)
    (harness.store_root / "demo").mkdir(parents=True)
    (harness.store_root / "demo" / "nested").symlink_to(
        harness.skill_root, target_is_directory=True
    )

    with pytest.raises(ValueError, match="symlink"):
        harness.curator.propose("demo", {"nested/SKILL.md": SKILL_BODY_V2})

    assert harness.skill_material() == {}


def test_reusing_an_approval_id_is_refused(harness):
    """One id, one approval. A second record under the same id could never be
    spent -- the ledger answers with the first -- and a revocation would be
    ambiguous between the two."""

    candidate = harness.propose()
    harness.authority.approve(candidate, "demo", approval_id="fixed-id")

    with pytest.raises(ValueError, match="already recorded"):
        harness.authority.approve(candidate, "demo", approval_id="fixed-id")


def test_a_revocation_cannot_interleave_with_a_promotion(harness):
    """The checks and the publish share one boundary with revocation.

    A revoke racing the write is made to land *after* the promotion completes,
    rather than between the gate's revocation check and its write -- which,
    unserialized, would produce a ledger in which the revocation precedes the
    promotion it was supposed to stop.
    """

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")

    inside_write = threading.Event()
    revoke_returned = threading.Event()

    class SlowGate(type(harness.gate)):
        def _write(self, snapshot, destination):
            inside_write.set()
            # Long enough that an unserialized revoke would win the race.
            time.sleep(0.2)
            assert not revoke_returned.is_set()
            return super()._write(snapshot, destination)

    gate = SlowGate(harness.gate._skill_root, harness.ledger)

    def revoke_soon():
        inside_write.wait(timeout=5)
        harness.authority.revoke(approval, reason="racing the write")
        revoke_returned.set()

    racer = threading.Thread(target=revoke_soon)
    racer.start()
    decision = gate.promote(candidate, "demo", approval)
    racer.join(timeout=10)

    assert decision.allowed
    events = [event.event for event in harness.ledger.events()]
    assert events.index("promotion-applied") < events.index("approval-revoked")
    assert not harness.gate.promote(candidate, "demo", approval).allowed


# -- staging and symlink aliasing (review round 3) ------------------------


def test_staging_never_happens_inside_the_watched_skill_root(harness, monkeypatch):
    """A staging directory inside the root would be a complete, readable copy
    of the candidate at a name nobody approved -- and U8 found a directory
    appearing mid-session is loadable straight away."""

    from claude_org_runtime.curator import gate as gate_module

    used_dirs: list[str] = []
    real_mkdtemp = gate_module.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        used_dirs.append(str(kwargs.get("dir", args[0] if args else "")))
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(gate_module.tempfile, "mkdtemp", recording_mkdtemp)

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    assert harness.gate.promote(candidate, "demo", approval).allowed
    # Promote a second time so the retirement path is exercised too.
    second = harness.authority.approve(candidate, "demo")
    assert harness.gate.promote(candidate, "demo", second).allowed

    assert used_dirs
    root = str(harness.skill_root)
    for used in used_dirs:
        assert not used.startswith(root), used
    assert harness.skill_material() == {"demo/SKILL.md": SKILL_BODY_V1}


def test_a_symlinked_target_inside_the_root_is_refused_not_followed(harness):
    """`skills/demo -> skills/code-review` would let an approval naming `demo`
    overwrite `code-review`. Resolving the link keeps it inside the root and
    still breaks the binding the replay check holds, so the link is refused."""

    other = harness.curator.propose("other", {"SKILL.md": "ORIGINAL\n"})
    other_approval = harness.authority.approve(other, "code-review")
    assert harness.gate.promote(other, "code-review", other_approval).allowed

    (harness.skill_root / "demo").symlink_to(
        harness.skill_root / "code-review", target_is_directory=True
    )

    candidate = harness.propose()
    approval = harness.authority.approve(candidate, "demo")
    decision = harness.gate.promote(candidate, "demo", approval)

    assert not decision.allowed
    assert decision.reason == RefusalReason.TARGET_INVALID
    assert harness.skill_material()["code-review/SKILL.md"] == "ORIGINAL\n"
    assert harness.refusals()[0]["reason"] == RefusalReason.TARGET_INVALID


def test_a_store_root_that_is_itself_a_symlink_is_refused(harness, tmp_path):
    """The per-component walk starts *below* the store root, so the root has to
    be checked on its own -- otherwise a store_root pointing at skill material
    makes every candidate write a promotion."""

    from claude_org_runtime.curator.stub import CuratorStub

    harness.skill_root.mkdir(parents=True, exist_ok=True)
    aliased = tmp_path / "aliased-store"
    aliased.symlink_to(harness.skill_root, target_is_directory=True)

    with pytest.raises(ValueError, match="store root is a symlink"):
        CuratorStub(aliased).propose("demo", {"SKILL.md": SKILL_BODY_V2})

    assert harness.skill_material() == {}
