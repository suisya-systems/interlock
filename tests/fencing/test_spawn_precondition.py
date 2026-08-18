"""Interlock's fail-closed spawn precondition.

Issue #9's third criterion: "A deliberately broken configuration (config
deleted, hook path unresolvable, sandbox profile absent) causes a **refused**
spawn -- never a downgraded one -- and the refusal is recorded durably."

Three assertions per broken case, and the middle one is the one that matters:

1. the outcome is a refusal with a named reason,
2. **the spawner was never invoked** -- not invoked with a narrowed fence, not
   invoked with a warning logged; not invoked,
3. the refusal is on disk, flushed, before the caller was told anything.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from claude_org_runtime.fencing import (
    EVENT_ADMITTED,
    FenceLedger,
    FencedSpawner,
)
from claude_org_runtime.fencing.renderer import RefusalReason
from claude_org_runtime.fencing.spawn import (
    REASON_BATTERY_INCOMPLETE,
    REASON_PROBE_UNSYNTHESIZABLE,
)

from .conftest import mutate


class RecordingSpawner:
    """A spawner that records the fact it was called at all."""

    def __init__(self):
        self.calls = []

    def __call__(self, plan):
        self.calls.append(plan)
        return {"pid": 4242}


@pytest.fixture
def ledger(tmp_path):
    return FenceLedger(tmp_path / "fence-ledger.jsonl")


class TestASoundFenceSpawns:
    def test_a_good_configuration_admits(self, ctx, document, ledger):
        spawner = RecordingSpawner()
        outcome = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, spawner
        )
        assert outcome.admitted
        assert len(spawner.calls) == 1
        assert outcome.result == {"pid": 4242}
        assert [e["event"] for e in ledger.events() if e["event"] == EVENT_ADMITTED]

    def test_the_plan_publishes_the_fence_the_hook_will_read(self, ctx, document, ledger):
        spawner = RecordingSpawner()
        outcome = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, spawner
        )
        plan = outcome.plan
        assert plan.fence_path.is_file()
        assert plan.settings_path.is_file()
        published = json.loads(plan.fence_path.read_text(encoding="utf-8"))
        assert published["rules"]
        assert json.loads(plan.settings_path.read_text(encoding="utf-8")) == dict(
            outcome.fence.settings
        )

    def test_the_cli_args_pass_permission_mode_explicitly(self, ctx, document, ledger):
        """i01 §3.9: ``permissionMode`` is the one part of the fence the
        provider reads back, so it is the one part a restart can be checked
        against directly. It is passed as a flag rather than left implicit."""

        outcome = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, RecordingSpawner()
        )
        args = outcome.plan.cli_args()
        assert "--permission-mode" in args
        assert args[args.index("--permission-mode") + 1] == outcome.fence.permission_mode
        assert "--settings" in args


class TestBrokenConfigurationsRefuse:
    """The three classes issue #9 names, each asserted the same way."""

    def _refuse(self, ledger, document, ctx, role="worker"):
        spawner = RecordingSpawner()
        outcome = FencedSpawner(ledger=ledger, document=document).spawn(role, ctx, spawner)
        return outcome, spawner

    def test_config_deleted(self, ctx, ledger):
        outcome, spawner = self._refuse(ledger, {"roles": {}}, ctx)
        assert not outcome.admitted
        assert spawner.calls == []
        assert RefusalReason.ROLE_ABSENT in outcome.codes

    def test_hook_path_unresolvable(self, ctx, document, ledger, tmp_path):
        broken_ctx = replace(ctx, hook_script=tmp_path / "vanished.py")
        outcome, spawner = self._refuse(ledger, document, broken_ctx)
        assert not outcome.admitted
        assert spawner.calls == []
        assert RefusalReason.HOOK_UNRESOLVABLE in outcome.codes

    def test_sandbox_profile_absent(self, ctx, document, ledger):
        outcome, spawner = self._refuse(
            ledger, mutate(document, "worker", sandbox=None), ctx
        )
        assert not outcome.admitted
        assert spawner.calls == []
        assert RefusalReason.SANDBOX_PROFILE_ABSENT in outcome.codes

    def test_a_refusal_is_never_a_narrowed_spawn(self, ctx, document, ledger):
        """The negative that the whole criterion turns on.

        A "best effort" renderer would hand the spawner a fence with the broken
        part dropped. Nothing may reach the spawner at all.
        """

        outcome, spawner = self._refuse(
            ledger, mutate(document, "worker", sandbox=None), ctx
        )
        assert spawner.calls == []
        assert outcome.fence is None
        assert outcome.plan is None

    def test_no_fence_or_settings_file_is_published_on_a_refusal(
        self, ctx, document, ledger
    ):
        """A published fence from a refused spawn would be picked up by a hook
        on the next start and enforced as though it had been approved."""

        self._refuse(ledger, mutate(document, "worker", sandbox=None), ctx)
        assert not ctx.fence_path.exists()
        assert not (ctx.fence_path.parent / "settings.local.json").exists()


class TestTheChildStartsOutsideTheLedgerLock:
    def test_the_ledger_is_not_held_while_the_child_runs(self, ctx, document, ledger):
        """A synchronous spawner must not serialize every other role behind it.

        ``spawner`` for a real ``claude -p`` session is a blocking
        ``subprocess.run``. Holding the cross-process ledger lock for its whole
        duration would block every other role -- including one trying to record
        a **refusal**, which is the one thing that must never wait on a
        long-running success.
        """

        observed = {}

        def spawner(plan):
            # Inside the spawner, another FencedSpawner must be able to take
            # the lock and record its own refusal.
            other = FencedSpawner(
                ledger=ledger, document=mutate(document, "curator", sandbox=None)
            )
            observed["outcome"] = other.spawn("curator", ctx, RecordingSpawner())
            return {"pid": 7}

        outcome = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, spawner
        )
        assert outcome.admitted
        assert observed["outcome"].admitted is False
        assert ledger.refusals()

    def test_the_admission_is_recorded_before_the_child_starts(self, ctx, document, ledger):
        seen = {}

        def spawner(plan):
            seen["events"] = [e["event"] for e in ledger.events()]
            return None

        FencedSpawner(ledger=ledger, document=document).spawn("worker", ctx, spawner)
        assert EVENT_ADMITTED in seen["events"]


class TestPublicationIsAllOrNothing:
    def test_a_failed_settings_write_leaves_no_fence_behind(
        self, ctx, document, ledger, monkeypatch
    ):
        """Half a publication is not "nothing published".

        A fence left on disk by a spawn that was then refused would be read by
        the hook on the next start and enforced as though it had been admitted
        -- the refusal invariant would be satisfied in the ledger and violated
        on the filesystem.
        """

        spawner = RecordingSpawner()
        fenced = FencedSpawner(ledger=ledger, document=document)
        monkeypatch.setattr(
            FencedSpawner,
            "_write_settings",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("no space left on device")),
        )
        outcome = fenced.spawn("worker", ctx, spawner)
        assert not outcome.admitted
        assert spawner.calls == []
        assert not ctx.fence_path.exists()
        assert ledger.refusals()


    def test_a_failed_republish_restores_the_previous_fence(
        self, ctx, document, ledger, monkeypatch
    ):
        """A refused respawn must not disarm the session that is already live.

        Unlinking the replacement would leave the running session with no
        fence at all, and every hook call denying, until the next successful
        publication -- a refusal that breaks more than it prevents.
        """

        first = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, RecordingSpawner()
        )
        assert first.admitted
        original = ctx.fence_path.read_bytes()

        monkeypatch.setattr(
            FencedSpawner,
            "_write_settings",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
        )
        outcome = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, RecordingSpawner()
        )
        assert not outcome.admitted
        assert ctx.fence_path.read_bytes() == original


class TestTheRefusalIsRecordedDurably:
    def test_the_refusal_is_on_disk_with_its_reasons(self, ctx, document, ledger):
        FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, RecordingSpawner()
        )
        FencedSpawner(ledger=ledger, document=mutate(document, "worker", sandbox=None)).spawn(
            "worker", ctx, RecordingSpawner()
        )
        refusals = ledger.refusals()
        assert len(refusals) == 1
        codes = {r["code"] for r in refusals[0]["reasons"]}
        assert RefusalReason.SANDBOX_PROFILE_ABSENT in codes
        assert refusals[0]["role"] == "worker"

    def test_the_record_survives_a_fresh_reader(self, ctx, document, tmp_path):
        """"Recorded durably" is taken literally: a refusal lost on crash is a
        refusal that was not recorded, and the crash is exactly when it is
        wanted."""

        path = tmp_path / "ledger.jsonl"
        FencedSpawner(
            ledger=FenceLedger(path), document=mutate(document, "worker", hooks=None)
        ).spawn("worker", ctx, RecordingSpawner())
        assert FenceLedger(path).refusals()

    def test_every_refusal_is_recorded_not_just_the_first(self, ctx, document, ledger):
        for _ in range(3):
            FencedSpawner(
                ledger=ledger, document=mutate(document, "worker", sandbox=None)
            ).spawn("worker", ctx, RecordingSpawner())
        assert len(ledger.refusals()) == 3

    def test_the_ledger_is_append_only(self, ctx, document, ledger):
        FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, RecordingSpawner()
        )
        first = ledger.path.read_text(encoding="utf-8")
        FencedSpawner(ledger=ledger, document=document).spawn(
            "curator", ctx, RecordingSpawner()
        )
        assert ledger.path.read_text(encoding="utf-8").startswith(first)


class TestTheSpawnerSelfChecks:
    def test_a_rule_whose_probe_cannot_be_synthesized_refuses_and_is_recorded(
        self, ctx, document, ledger, monkeypatch
    ):
        """A rule the battery cannot aim at is a rule nothing observes.

        Letting the synthesis error escape would skip the durable record
        entirely -- a spawn that neither happened nor was written down.
        """

        import claude_org_runtime.fencing.spawn as spawn_module
        from claude_org_runtime.fencing.battery import ProbeSynthesisError

        def boom(*_a, **_k):
            raise ProbeSynthesisError("no witness for this rule")

        monkeypatch.setattr(spawn_module, "run_battery", boom)
        spawner = RecordingSpawner()
        outcome = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, spawner
        )
        assert not outcome.admitted
        assert spawner.calls == []
        assert REASON_PROBE_UNSYNTHESIZABLE in outcome.codes
        assert ledger.refusals()

    def test_a_fence_that_fails_its_own_battery_refuses_the_spawn(
        self, ctx, document, ledger, monkeypatch
    ):
        """Shipping a fence Interlock cannot itself prove is the same class of
        error as shipping no fence."""

        import claude_org_runtime.fencing.spawn as spawn_module
        from claude_org_runtime.fencing.battery import BatteryReport, ProbeResult
        from claude_org_runtime.fencing.rules import Decision

        real = spawn_module.run_battery

        def sabotage(fence, **kwargs):
            report = real(fence, **kwargs)
            broken = ProbeResult(
                probe=report.results[0].probe, decision=Decision(denied=False)
            )
            return BatteryReport(role=report.role, results=(broken,) + report.results[1:])

        monkeypatch.setattr(spawn_module, "run_battery", sabotage)
        spawner = RecordingSpawner()
        outcome = FencedSpawner(ledger=ledger, document=document).spawn(
            "worker", ctx, spawner
        )
        assert not outcome.admitted
        assert spawner.calls == []
        assert REASON_BATTERY_INCOMPLETE in outcome.codes
        assert ledger.refusals()
