"""An Interlock-initiated restart preserves the fence.

Issue #9's first criterion, re-pointed by C2: "Under C2 the restart in question
is Interlock respawning a ``-p`` child from persisted state -- **there is no
other kind**." D-0027 removed the provider-supervisor restart path entirely,
and `#8` -- the issue that would have probed it -- was closed as **moot, not
passed**. So the whole of "survives restart" is the respawn modelled here.

The criterion's own wording fixes the method: the fence is shown preserved "by
the breach battery denying every rule after restart as it did before". Not by
comparing rule counts, not by trusting the persisted file -- by re-running the
battery on the far side.

What this cannot show is stated in ``docs/per-role-fencing.md`` and in the gate
record: the rendered-input diff proves what Interlock wrote, not what the
provider loaded. That gap is item 3's residual.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from claude_org_runtime.fencing import (
    FenceLedger,
    FenceStateError,
    FencedSpawner,
    diff_fences,
    probes_for,
    read_fence,
    render_fence,
    role_names,
    run_battery,
    write_fence,
)


@pytest.fixture
def ledger(tmp_path):
    return FenceLedger(tmp_path / "ledger.jsonl")


def spawn(ctx, document, ledger, role="worker"):
    calls = []
    outcome = FencedSpawner(ledger=ledger, document=document).spawn(
        role, ctx, lambda plan: calls.append(plan) or {"pid": 1}
    )
    assert outcome.admitted
    return outcome


class TestTheBatteryHoldsAcrossRestart:
    def test_every_rule_is_denied_before_and_after_an_interlock_respawn(
        self, ctx, document, ledger
    ):
        first = spawn(ctx, document, ledger)
        before = run_battery(first.fence)
        assert before.all_denied

        # The crash. Interlock is gone; the persisted fence is all that is left.
        restored = read_fence(first.plan.fence_path)

        # The respawn, from persisted state.
        after = run_battery(restored)
        assert after.all_denied
        assert after.covered_rule_ids == before.covered_rule_ids
        for a, b in zip(after.results, before.results):
            assert a.probe.rule_id == b.probe.rule_id
            assert a.decision.rule_id == b.decision.rule_id

    def test_every_role_holds_across_restart(self, ctx, document, tmp_path):
        for role in role_names(document):
            role_ctx = replace(ctx, fence_path=tmp_path / role / "fence.json")
            outcome = spawn(role_ctx, document, FenceLedger(tmp_path / f"{role}.jsonl"), role)
            restored = read_fence(outcome.plan.fence_path)
            report = run_battery(restored)
            assert report.all_denied, role
            assert report.covered_rule_ids == set(outcome.fence.rule_ids())

    def test_a_re_render_after_restart_matches_the_persisted_fence(
        self, ctx, document, ledger
    ):
        """The rendered-input diff the issue asks for.

        Interlock respawning from persisted state may either re-render or read
        back; the two must agree, or "the fence survived" would depend on which
        path the restart happened to take.
        """

        outcome = spawn(ctx, document, ledger)
        persisted = read_fence(outcome.plan.fence_path)
        re_rendered = render_fence("worker", ctx, document=document)
        diff = diff_fences(persisted, re_rendered)
        assert diff.identical, diff.to_json()

    def test_the_diff_reports_a_fence_that_did_change(self, ctx, document, ledger):
        """The diff has to be capable of saying no, or its yes means nothing."""

        from .conftest import mutate

        outcome = spawn(ctx, document, ledger)
        weakened = mutate(
            document,
            "worker",
            permissions={
                "allow": [],
                "deny": list(document["roles"]["worker"]["permissions"]["deny"])[:2],
            },
        )
        diff = diff_fences(outcome.fence, render_fence("worker", ctx, document=weakened))
        assert not diff.identical
        assert diff.removed_rules
        assert diff.settings_changed

    def test_a_rule_dropped_across_restart_shows_up_as_an_unprobed_gap(
        self, ctx, document, ledger
    ):
        """The failure mode the criterion exists to catch: a restart that comes
        back with a *smaller* fence and no error anywhere."""

        outcome = spawn(ctx, document, ledger)
        payload = json.loads(outcome.plan.fence_path.read_text(encoding="utf-8"))
        dropped = payload["rules"].pop(3)
        outcome.plan.fence_path.write_text(json.dumps(payload), encoding="utf-8")

        restored = read_fence(outcome.plan.fence_path)
        after = run_battery(restored)
        # The battery on the far side is still green -- it can only probe the
        # rules it was given. It is the *diff* that catches the loss, which is
        # exactly why the criterion asks for both.
        assert after.all_denied
        missing = set(outcome.fence.rule_ids()) - after.covered_rule_ids
        assert missing
        diff = diff_fences(outcome.fence, restored)
        assert not diff.identical
        assert diff.removed_rules
        dropped_id = f"{dropped['layer']}:{dropped['kind']}:{dropped['tool']}:{dropped['spec']}"
        assert dropped_id in diff.removed_rules


class TestPersistenceFailsClosed:
    def test_a_truncated_fence_file_is_an_error_not_a_smaller_fence(self, ctx, document, ledger):
        outcome = spawn(ctx, document, ledger)
        raw = outcome.plan.fence_path.read_text(encoding="utf-8")
        outcome.plan.fence_path.write_text(raw[: len(raw) // 2], encoding="utf-8")
        with pytest.raises(FenceStateError):
            read_fence(outcome.plan.fence_path)

    def test_a_fence_with_no_rules_is_rejected_on_read(self, tmp_path):
        path = tmp_path / "fence.json"
        path.write_text(
            json.dumps({
                "format": 1,
                "role": "worker",
                "role_kind": "worker",
                "permission_mode": "default",
                "rules": [],
                "settings": {},
            }),
            encoding="utf-8",
        )
        with pytest.raises(FenceStateError):
            read_fence(path)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("layer", "typo-layer"),
            ("kind", "typo-kind"),
            ("spec", None),
            ("tool", None),
            ("spec", ""),
            ("layer", 7),
        ],
    )
    def test_a_corrupted_rule_field_is_rejected_rather_than_coerced(
        self, ctx, document, ledger, field, value
    ):
        """Valid JSON is not a valid fence.

        Coercing these fields with ``str()`` fails in the silent direction: a
        mistyped ``layer`` is skipped by the decision function and a ``null``
        spec becomes the string ``"None"`` and matches nothing. Either removes
        a denial while the hook goes on treating the fence as sound.
        """

        outcome = spawn(ctx, document, ledger)
        payload = json.loads(outcome.plan.fence_path.read_text(encoding="utf-8"))
        payload["rules"][2][field] = value
        outcome.plan.fence_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(FenceStateError):
            read_fence(outcome.plan.fence_path)

    def test_a_rule_that_is_not_an_object_is_rejected(self, ctx, document, ledger):
        outcome = spawn(ctx, document, ledger)
        payload = json.loads(outcome.plan.fence_path.read_text(encoding="utf-8"))
        payload["rules"][0] = "Bash(git push *)"
        outcome.plan.fence_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(FenceStateError):
            read_fence(outcome.plan.fence_path)

    def test_an_unknown_format_version_is_rejected(self, ctx, document, ledger):
        outcome = spawn(ctx, document, ledger)
        payload = json.loads(outcome.plan.fence_path.read_text(encoding="utf-8"))
        payload["format"] = 99
        outcome.plan.fence_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(FenceStateError):
            read_fence(outcome.plan.fence_path)

    def test_publication_is_atomic(self, ctx, document, tmp_path):
        """A hook reading a half-written fence would enforce a *subset* of it.

        The rename makes that impossible; this pins that no partial file is
        left behind under the published name.
        """

        fence = render_fence("worker", ctx, document=document)
        path = write_fence(fence, tmp_path / "fence.json")
        assert path.is_file()
        assert not list(path.parent.glob("*.tmp"))
        assert read_fence(path).rule_ids() == fence.rule_ids()

    def test_republishing_replaces_cleanly(self, ctx, document, tmp_path):
        fence = render_fence("worker", ctx, document=document)
        path = write_fence(fence, tmp_path / "fence.json")
        write_fence(render_fence("curator", ctx, document=document), path)
        assert read_fence(path).role == "curator"


class TestRestartDoesNotWidenTheFence:
    def test_probes_are_identical_objects_across_restart(self, ctx, document, ledger):
        outcome = spawn(ctx, document, ledger)
        before = [p.to_json() for p in probes_for(outcome.fence)]
        after = [p.to_json() for p in probes_for(read_fence(outcome.plan.fence_path))]
        assert before == after

    def test_permission_mode_survives_and_is_never_upgraded_to_bypass(
        self, ctx, document, ledger
    ):
        outcome = spawn(ctx, document, ledger)
        restored = read_fence(outcome.plan.fence_path)
        assert restored.permission_mode == outcome.fence.permission_mode
        assert restored.permission_mode != "bypassPermissions"
