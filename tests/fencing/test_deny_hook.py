"""The ``PreToolUse`` deny hook -- proven to deny, not merely to run.

Issue #9 is explicit: "Assert the *effect* -- the forbidden operation did not
happen -- and never the hook's own exit code." A6 of
``investigation/pre-spawn-fence-search.md`` (U35) watched a hook exit 1 and be
absorbed, and ``investigation/i04-pretooluse-fence-probe.md`` reproduced the
same absorption on ``PreToolUse`` itself: the hook ran, exited 1, the tool call
went through, and the session exited 0.

So the division of labour in this suite is deliberate:

- **What the hook decides** is asserted here, in process and by subprocess,
  from the hook's *decision payload*.
- **That the CLI honours the decision** is not assertable in a unit test at
  all. It is measured in ``investigation/i04-pretooluse-fence-probe.md`` by
  whether the forbidden operation happened, and that file is the evidence --
  not anything below.

The exit status is asserted in exactly one place and for one reason: to pin
that this hook never uses **1**, the status measured being swallowed. That is
an assertion about our hook's contract, not evidence that a fence held.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from claude_org_runtime.fencing import probes_for, render_fence, role_names, write_fence
from claude_org_runtime.fencing.hook import EXIT_DENY, EXIT_NO_OPINION, decide_payload, main


@pytest.fixture
def published(ctx, document, tmp_path):
    fence = render_fence("worker", ctx, document=document)
    path = write_fence(fence, tmp_path / "fence.json")
    return fence, path


def run_hook_subprocess(fence_path, event):
    return subprocess.run(
        [sys.executable, "-m", "claude_org_runtime.fencing.hook", "--fence", str(fence_path)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=False,
    )


class TestTheHookDenies:
    def test_every_probe_is_denied_through_the_hook(self, published):
        """The battery, run through the hook rather than the fence object.

        Same probes, second observation point. If the hook and the fence ever
        disagreed, the enforcement path would be narrower than the probed one.
        """

        fence, path = published
        for probe in probes_for(fence):
            decision, payload = decide_payload(path, {
                "tool_name": probe.tool_name,
                "tool_input": dict(probe.tool_input),
            })
            assert decision.denied, probe.rule_id
            assert decision.rule_id == probe.rule_id
            assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_all_roles_deny_all_their_probes_through_the_hook(self, ctx, document, tmp_path):
        for role in role_names(document):
            fence = render_fence(role, ctx, document=document)
            path = write_fence(fence, tmp_path / f"fence-{role}.json")
            for probe in probes_for(fence):
                decision, _ = decide_payload(path, {
                    "tool_name": probe.tool_name,
                    "tool_input": dict(probe.tool_input),
                })
                assert decision.denied, f"{role}:{probe.rule_id}"

    def test_the_payload_carries_both_output_shapes(self, published):
        """Which shape a given CLI build honours is not something a fence
        should depend on knowing, so both are emitted."""

        fence, path = published
        probe = probes_for(fence)[0]
        _, payload = decide_payload(path, {
            "tool_name": probe.tool_name,
            "tool_input": dict(probe.tool_input),
        })
        assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert payload["decision"] == "block"
        assert payload["reason"]

    def test_an_unfenced_operation_gets_no_opinion_rather_than_an_allow(self, published):
        """The fence never says "allow".

        Saying so would make this hook an authority on *permitting*
        operations, and a bug here would then widen the worker's reach instead
        of narrowing it.
        """

        _, path = published
        decision, payload = decide_payload(path, {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/ordinary.txt"},
        })
        assert not decision.denied
        assert payload == {}


class TestFailOpenIsTestedForExplicitly:
    """Issue #9's fifth criterion, and F2/V15/V16's ignore-and-continue habit.

    Every one of these is an input the hook could plausibly shrug at. None of
    them may produce anything but a deny.
    """

    def test_missing_fence_file_denies(self, tmp_path):
        decision, payload = decide_payload(tmp_path / "absent.json", {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        })
        assert decision.denied
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_malformed_fence_file_denies(self, tmp_path):
        path = tmp_path / "fence.json"
        path.write_text("{ this is not json", encoding="utf-8")
        decision, _ = decide_payload(path, {"tool_name": "Bash", "tool_input": {}})
        assert decision.denied

    def test_fence_file_with_no_rules_denies(self, tmp_path):
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
        decision, _ = decide_payload(path, {"tool_name": "Bash", "tool_input": {}})
        assert decision.denied

    def test_event_with_no_tool_name_denies_rather_than_guessing(self, published):
        _, path = published
        decision, _ = decide_payload(path, {"tool_input": {"command": "rm -rf /"}})
        assert decision.denied

    def test_a_rule_scoped_to_a_tool_denies_an_unreadable_payload(self, published):
        """An unrecognized payload shape must not become a silent bypass."""

        fence, path = published
        decision, _ = decide_payload(path, {"tool_name": "WebFetch", "tool_input": {}})
        assert decision.denied

    def test_empty_stdin_denies(self, published):
        _, path = published
        result = subprocess.run(
            [sys.executable, "-m", "claude_org_runtime.fencing.hook", "--fence", str(path)],
            input="",
            capture_output=True,
            text=True,
            check=False,
        )
        assert json.loads(result.stdout)["decision"] == "block"

    def test_non_json_stdin_denies(self, published):
        _, path = published
        result = subprocess.run(
            [sys.executable, "-m", "claude_org_runtime.fencing.hook", "--fence", str(path)],
            input="not json at all",
            capture_output=True,
            text=True,
            check=False,
        )
        assert json.loads(result.stdout)["decision"] == "block"

    def test_an_internal_error_denies_instead_of_escaping_as_a_traceback(
        self, published, monkeypatch
    ):
        """An unhandled traceback exits **1**, and exit 1 is the status
        i04 §4 measured being absorbed. So the catch-all denies."""

        import claude_org_runtime.fencing.hook as hook_module

        _, path = published

        def boom(*_args, **_kwargs):
            raise RuntimeError("synthetic failure inside the hook")

        monkeypatch.setattr(hook_module, "read_fence", boom)
        code = main(["--fence", str(path)])
        assert code == EXIT_DENY


class TestExitStatusContract:
    """One narrow assertion, and a note about what it is not.

    This does **not** show that a denial was enforced. It shows only that this
    hook never signals a denial with exit **1**, the status
    ``investigation/i04-pretooluse-fence-probe.md`` §4 measured being absorbed
    at exit 0 with no other trace. Enforcement is measured in that file, by
    whether the forbidden operation happened.
    """

    def test_a_deny_never_uses_exit_1(self, published):
        fence, path = published
        probe = probes_for(fence)[0]
        result = run_hook_subprocess(path, {
            "tool_name": probe.tool_name,
            "tool_input": dict(probe.tool_input),
        })
        assert result.returncode != 1
        assert result.returncode == EXIT_DENY

    def test_no_opinion_exits_zero_and_says_nothing(self, published):
        _, path = published
        result = run_hook_subprocess(path, {
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/ordinary.txt"},
        })
        assert result.returncode == EXIT_NO_OPINION
        assert result.stdout.strip() == ""

    def test_a_deny_is_readable_on_both_stdout_and_stderr(self, published):
        fence, path = published
        probe = probes_for(fence)[0]
        result = run_hook_subprocess(path, {
            "tool_name": probe.tool_name,
            "tool_input": dict(probe.tool_input),
        })
        assert json.loads(result.stdout)["decision"] == "block"
        assert result.stderr.strip()


class TestTheRenderedCommandIsTheOneThatWorks:
    """Run the command the *renderer emits*, not a convenient equivalent.

    The rest of this file invokes the hook as
    ``python -m claude_org_runtime.fencing.hook``, and that is exactly how a
    real hole hid: the rendered settings invoke the file **by path**, which
    runs it with no parent package, and the relative imports at the top raised
    ``ImportError`` and exited **1** -- the status i04 §4 measured being
    absorbed. Every shipped role would have run behind an inert fence, and the
    suite would have stayed green.

    So these tests take the command string out of the rendered settings and
    execute it, with the package deliberately *not* importable from the child's
    environment.
    """

    @staticmethod
    def _rendered_command(fence):
        groups = fence.settings["hooks"]["PreToolUse"]
        return [hook["command"] for group in groups for hook in group["hooks"]][0]

    @staticmethod
    def _clean_env():
        import os

        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        return env

    def test_the_rendered_command_denies_a_probe(self, ctx, document, tmp_path):
        import shlex

        fence = render_fence("worker", ctx, document=document)
        write_fence(fence, ctx.fence_path)
        probe = probes_for(fence)[0]
        result = subprocess.run(
            shlex.split(self._rendered_command(fence)),
            input=json.dumps(
                {"tool_name": probe.tool_name, "tool_input": dict(probe.tool_input)}
            ),
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=self._clean_env(),
            check=False,
        )
        assert result.returncode != 1, result.stderr
        assert json.loads(result.stdout)["decision"] == "block"

    def test_the_rendered_command_never_exits_1_even_when_it_cannot_work(
        self, ctx, document, tmp_path
    ):
        """The failure direction that is silent.

        Exit 1 is absorbed, so a hook that breaks *must not* break that way --
        including when the thing that broke is the hook's own ability to load.
        """

        import shlex

        fence = render_fence("worker", ctx, document=document)
        command = shlex.split(self._rendered_command(fence))
        # No fence file at all: the hook cannot answer, so it must deny.
        result = subprocess.run(
            command,
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}),
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env=self._clean_env(),
            check=False,
        )
        assert result.returncode == EXIT_DENY, result.stderr
        assert json.loads(result.stdout)["decision"] == "block"

    def test_every_role_rendered_command_runs(self, ctx, document, tmp_path):
        import shlex
        from dataclasses import replace

        for role in role_names(document):
            role_ctx = replace(ctx, fence_path=tmp_path / role / "fence.json")
            fence = render_fence(role, role_ctx, document=document)
            write_fence(fence, role_ctx.fence_path)
            probe = probes_for(fence)[0]
            result = subprocess.run(
                shlex.split(self._rendered_command(fence)),
                input=json.dumps(
                    {"tool_name": probe.tool_name, "tool_input": dict(probe.tool_input)}
                ),
                capture_output=True,
                text=True,
                cwd=str(tmp_path),
                env=self._clean_env(),
                check=False,
            )
            assert result.returncode == EXIT_DENY, f"{role}: {result.stderr}"
            assert json.loads(result.stdout)["decision"] == "block", role
