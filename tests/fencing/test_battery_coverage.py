"""The breach-probe battery, and the coverage claim it is allowed to make.

Issue #9's second criterion: "Every rule in every role's fence has a probe, and
each probe is **denied**. Coverage is asserted mechanically against the
rendered fence -- a hand-maintained probe list that silently drifts from the
fence is a failure of this criterion."

So the tests here do two different jobs, and the second is the one that lasts:

1. assert coverage and denial for the fence as it stands today, and
2. assert that coverage is *derived*, by adding a rule to a fence at runtime
   and requiring the battery to grow with it. A suite that only did (1) would
   pass forever against a probe list that had stopped tracking the fence.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.fencing import (
    ProbeSynthesisError,
    probe_for,
    probes_for,
    render_fence,
    role_names,
    run_battery,
)
from claude_org_runtime.fencing.rules import (
    KIND_PERMISSION_DENY,
    KIND_SANDBOX_DENY_WRITE,
    LAYER_PERMISSIONS,
    LAYER_SANDBOX,
    Fence,
    FenceRule,
)


def _fence(ctx, document, role):
    return render_fence(role, ctx, document=document)


class TestCoverageIsMechanical:
    def test_every_rule_of_every_role_has_exactly_one_probe(self, ctx, document):
        for role in role_names(document):
            fence = _fence(ctx, document, role)
            probes = probes_for(fence)
            assert {p.rule_id for p in probes} == set(fence.rule_ids())
            assert len(probes) == len(fence.rules), f"{role}: probes are not 1:1 with rules"

    def test_every_probe_is_denied_by_the_rule_it_targets(self, ctx, document):
        """Denied *by its own rule*, not merely denied.

        A probe that trips a neighbouring rule would let a broken rule hide
        behind a working one, and the battery would still be green.
        """

        for role in role_names(document):
            report = run_battery(_fence(ctx, document, role))
            assert report.all_denied, [b.probe.rule_id for b in report.breaches]
            for result in report.results:
                assert result.decision.rule_id == result.probe.rule_id

    def test_coverage_grows_when_a_rule_is_added(self, ctx, document):
        """The anti-drift assertion.

        This is the test that would fail if someone replaced the derived
        battery with a hand-written list. The list would still cover today's
        fence; it would not cover a rule invented here.
        """

        fence = _fence(ctx, document, "worker")
        extra = FenceRule(
            LAYER_PERMISSIONS, KIND_PERMISSION_DENY, "Bash", "shutdown --now *"
        )
        grown = Fence(
            role=fence.role,
            role_kind=fence.role_kind,
            permission_mode=fence.permission_mode,
            rules=fence.rules + (extra,),
            settings=fence.settings,
        )
        before = run_battery(fence)
        after = run_battery(grown)
        assert extra.rule_id not in before.covered_rule_ids
        assert extra.rule_id in after.covered_rule_ids
        assert after.all_denied
        assert len(after.results) == len(before.results) + 1

    def test_a_rule_whose_probe_cannot_be_synthesized_is_an_error_not_a_gap(self):
        """Fatal, not skipped.

        Skipping the rule would leave it unprobed while the battery still
        reported full coverage -- the exact failure this battery exists to make
        impossible.
        """

        broken = FenceRule(LAYER_PERMISSIONS, "no-such-kind", "Bash", "x")
        with pytest.raises(Exception) as excinfo:
            probe_for(broken)
        assert isinstance(excinfo.value, (ProbeSynthesisError, ValueError))

    def test_the_battery_reports_a_breach_rather_than_hiding_it(self, ctx, document):
        """A rule that does not deny its own probe must surface as a breach.

        Constructed by handing the battery a fence whose rule cannot match the
        operand its own kind implies: the report must be red, not silently
        short.
        """

        fence = _fence(ctx, document, "worker")
        blind = Fence(
            role=fence.role,
            role_kind=fence.role_kind,
            permission_mode=fence.permission_mode,
            rules=fence.rules,
            settings=fence.settings,
        )
        report = run_battery(blind, evaluate=lambda *_: _never_denies())
        assert not report.all_denied
        assert len(report.breaches) == len(fence.rules)


def _never_denies():
    from claude_org_runtime.fencing.rules import Decision

    return Decision(denied=False)


class TestProbesAreForbiddenOperations:
    def test_a_probe_operand_is_inert(self, ctx, document):
        """Probes are evaluated, never executed -- but they must still be safe
        to read in a log or paste into a shell by accident."""

        for role in role_names(document):
            for probe in probes_for(_fence(ctx, document, role)):
                blob = repr(probe.tool_input)
                assert "rm -rf /" not in blob
                assert ";" not in blob and "&&" not in blob

    def test_sandbox_write_probes_target_the_denied_path(self, ctx, document):
        fence = _fence(ctx, document, "worker")
        writes = [r for r in fence.rules if r.kind == KIND_SANDBOX_DENY_WRITE]
        assert writes
        for rule in writes:
            probe = probe_for(rule)
            assert probe.tool_name == "Write"
            assert probe.tool_input["file_path"].startswith(rule.spec)

    def test_a_probe_aimed_at_one_role_does_not_certify_another(self, ctx, document):
        """Per-role probing is what D-0023 rules out; this pins the difference.

        The roles do not have the same fences, so their probe sets must not be
        interchangeable.
        """

        worker = set(p.rule_id for p in probes_for(_fence(ctx, document, "worker")))
        secretary = set(p.rule_id for p in probes_for(_fence(ctx, document, "secretary")))
        assert worker != secretary
        assert worker - secretary


class TestLayerOrdering:
    def test_a_sandbox_deny_is_not_overridable_by_the_permission_layer(self, ctx, document):
        fence = _fence(ctx, document, "worker")
        sandbox_rules = [r for r in fence.rules if r.layer == LAYER_SANDBOX]
        assert sandbox_rules
        for rule in sandbox_rules:
            probe = probe_for(rule)
            decision = fence.decide(probe.tool_name, probe.tool_input)
            assert decision.layer == LAYER_SANDBOX
