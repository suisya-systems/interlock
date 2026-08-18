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

        (candidate.root / "SKILL.md").write_text(body, encoding="utf-8")

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
        return {
            path.relative_to(self.skill_root).as_posix(): path.read_text("utf-8")
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
