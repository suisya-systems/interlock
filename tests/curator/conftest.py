"""Fixtures for the Curator promotion gate tests (gate item 9).

These tests are the durable half of item 9 (D-0026): the gate implementation is
throwaway, the properties asserted here are not.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from claude_org_runtime.curator.ledger import ApprovalLedger
from claude_org_runtime.curator.gate import PromotionGate
from claude_org_runtime.curator.skill_root import skill_root_for_project
from claude_org_runtime.curator.stub import ApprovalAuthority, CuratorStub

SKILL_BODY_V1 = "---\nname: demo\ndescription: demo skill v1\n---\n\nBODY-V1\n"
SKILL_BODY_V2 = "---\nname: demo\ndescription: demo skill v2\n---\n\nBODY-V2\n"


def write_candidate_file(path: Path, body: str) -> None:
    """Write candidate material the way the Curator itself does: bytes, with no
    newline translation.

    ``Path.write_text`` opens in text mode with ``newline=None``, which on
    Windows rewrites every ``\n`` into ``\r\n``. The Curator writes candidates
    with ``write_bytes``, so a test that proposed through the Curator and then
    rewrote the file through ``write_text`` was comparing LF bytes against CRLF
    bytes: "revert the candidate to the approved content" could not restore the
    approved digest on Windows, and the gate correctly called it a mismatch.

    The digest is byte-exact on purpose -- that is the property gate item 9's
    fourth negative turns on, and normalizing newlines inside it would make the
    digest blind to a real one-byte substitution. So it is every *writer* into a
    candidate that has to be byte-exact, this helper included.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))


@dataclass
class Harness:
    """A Curator, an approval authority, a ledger and one live skill root."""

    curator: CuratorStub
    authority: ApprovalAuthority
    ledger: ApprovalLedger
    gate: PromotionGate
    skill_root: Path
    store_root: Path

    def propose(self, candidate_id: str = "demo", body: str = SKILL_BODY_V1):
        return self.curator.propose(candidate_id, {"SKILL.md": body})

    def mutate(self, candidate, body: str = SKILL_BODY_V2) -> None:
        """Rewrite the candidate on disk *after* it was approved."""

        write_candidate_file(candidate.root / "SKILL.md", body)

    def add_file(self, candidate, relative: str, body: str) -> None:
        """Add a file to the candidate on disk *after* it was approved."""

        write_candidate_file(candidate.root / relative, body)

    def refusals(self) -> list[dict]:
        return [
            {"reason": event.payload.get("reason"), **event.payload}
            for event in self.ledger.events()
            if event.event == "promotion-refused"
        ]

    def applied(self) -> list[dict]:
        return [
            event.payload
            for event in self.ledger.events()
            if event.event == "promotion-applied"
        ]

    def skill_material(self) -> dict[str, str]:
        if not self.skill_root.exists():
            return {}
        # Decoded from bytes rather than read in text mode: ``read_text``
        # translates ``\r\n`` back into ``\n``, which would hide exactly the
        # newline damage these tests exist to catch.
        return {
            path.relative_to(self.skill_root).as_posix(): path.read_bytes().decode(
                "utf-8"
            )
            for path in sorted(self.skill_root.rglob("*"))
            if path.is_file()
        }


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    project = tmp_path / "project"
    store_root = tmp_path / "candidates"
    skill_root = skill_root_for_project(project)
    ledger = ApprovalLedger(tmp_path / "approvals.jsonl")
    return Harness(
        curator=CuratorStub(store_root),
        authority=ApprovalAuthority(ledger, approver="operator"),
        ledger=ledger,
        gate=PromotionGate(skill_root, ledger),
        skill_root=skill_root.path,
        store_root=store_root,
    )
