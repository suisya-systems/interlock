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

from dataclasses import replace

import pytest

from claude_org_runtime.curator.gate import RefusalReason
from claude_org_runtime.curator.records import ApprovalRecord

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
    (candidate.root / "extra.md").write_text("payload\n", encoding="utf-8")

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

    assert harness.gate.promote(candidate, "demo", approval).allowed
    assert harness.skill_material() == {"demo/SKILL.md": SKILL_BODY_V1}


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
