"""The per-role fencing renderer: what it renders, and what it refuses.

The refusals carry most of the weight of this file. D-0023 part 2 makes
fail-closed Interlock's own obligation, and F2/V15/V16 record how readily this
codebase reaches for ignore-and-continue instead -- so every case below asserts
a *refusal with a named reason*, never merely that a bad value is absent from
the output.
"""

from __future__ import annotations

import json
import re

import pytest

from claude_org_runtime.fencing import (
    FenceRefusal,
    RefusalReason,
    render_fence,
    role_names,
)
from claude_org_runtime.fencing.rules import (
    KIND_PERMISSION_DENY,
    KIND_SANDBOX_DENY_READ,
    KIND_SANDBOX_DENY_WRITE,
    LAYER_PERMISSIONS,
    LAYER_SANDBOX,
)

from .conftest import mutate


class TestRenders:
    def test_every_shipped_role_renders(self, ctx, document):
        assert role_names(document)
        for role in role_names(document):
            fence = render_fence(role, ctx, document=document)
            assert fence.role == role
            assert fence.rules, f"{role} rendered an empty fence"

    def test_all_three_layers_are_present_on_the_worker_role(self, ctx, document):
        """Item 3's predicate names permission, sandbox *and* hooks as one object."""

        fence = render_fence("worker", ctx, document=document)
        kinds = {rule.kind for rule in fence.rules}
        assert KIND_PERMISSION_DENY in kinds
        assert KIND_SANDBOX_DENY_READ in kinds
        assert KIND_SANDBOX_DENY_WRITE in kinds
        assert fence.settings["hooks"]["PreToolUse"]

    def test_layers_are_labelled_so_the_battery_can_report_per_layer(self, ctx, document):
        fence = render_fence("worker", ctx, document=document)
        layers = {rule.layer for rule in fence.rules}
        assert layers == {LAYER_PERMISSIONS, LAYER_SANDBOX}

    def test_placeholders_are_substituted_everywhere(self, ctx, document):
        placeholder = re.compile(r"\{[a-z_]+\}")
        for role in role_names(document):
            fence = render_fence(role, ctx, document=document)
            leftovers = placeholder.findall(json.dumps(fence.settings))
            assert not leftovers, f"{role}: unsubstituted {leftovers}"
            for rule in fence.rules:
                assert not placeholder.search(rule.spec)

    def test_the_deny_hook_is_wired_into_every_role(self, ctx, document):
        for role in role_names(document):
            fence = render_fence(role, ctx, document=document)
            commands = [
                hook["command"]
                for group in fence.settings["hooks"]["PreToolUse"]
                for hook in group["hooks"]
            ]
            assert any(str(ctx.hook_script) in command for command in commands)
            assert any(str(ctx.fence_path) in command for command in commands)

    def test_rendering_is_deterministic(self, ctx, document):
        """Two renders must be byte-identical, or a restart diff means nothing."""

        first = render_fence("worker", ctx, document=document)
        second = render_fence("worker", ctx, document=document)
        assert first.rule_ids() == second.rule_ids()
        assert json.dumps(first.settings, sort_keys=True) == json.dumps(
            second.settings, sort_keys=True
        )

    def test_rule_ids_are_stable_and_unique(self, ctx, document):
        for role in role_names(document):
            fence = render_fence(role, ctx, document=document)
            ids = fence.rule_ids()
            assert len(ids) == len(set(ids)), f"{role} has duplicate rule ids"


class TestDiscardedAxes:
    """PORTING_LEDGER R5: the transport and pattern axes do not come across.

    They are *refused*, not ignored. A role document still carrying a discarded
    axis was authored against the old contract, so rendering it while dropping
    the axis produces a fence narrower than its author believed -- a silent
    downgrade.
    """

    @pytest.mark.parametrize("axis", ["sandbox_by_pattern", "transport", "transport_descriptor"])
    def test_a_discarded_axis_refuses_the_render(self, ctx, document, axis):
        broken = mutate(document, "worker", **{axis: {"A": {}}})
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.DISCARDED_AXIS in excinfo.value.codes

    def test_the_fencing_package_does_not_import_the_discarded_transport_module(self):
        """The ledger's other half: the carried renderer must not drag the
        ``transport.descriptor`` dependency along with it."""

        import pathlib

        package = pathlib.Path(
            __import__("claude_org_runtime.fencing", fromlist=["__file__"]).__file__
        ).parent
        for source in package.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            assert "transport.descriptor" not in text.replace(
                "``transport.descriptor``", ""
            ), f"{source.name} references the discarded transport axis"
            assert "sandbox_by_pattern" not in text or "DISCARDED" in text or '"""' in text


class TestRefusals:
    def test_absent_role_refuses(self, ctx, document):
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("no-such-role", ctx, document=document)
        assert RefusalReason.ROLE_ABSENT in excinfo.value.codes

    def test_config_deleted_refuses(self, ctx, tmp_path):
        """"config deleted" -- the first of issue #9's three broken configurations."""

        from claude_org_runtime.fencing.renderer import load_document

        with pytest.raises(FenceRefusal) as excinfo:
            load_document(tmp_path / "gone.json")
        assert RefusalReason.DOCUMENT_UNREADABLE in excinfo.value.codes

    def test_malformed_document_refuses_rather_than_rendering_nothing(self, ctx, tmp_path):
        from claude_org_runtime.fencing.renderer import load_document

        path = tmp_path / "roles.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(FenceRefusal):
            load_document(path)

    def test_sandbox_profile_absent_refuses(self, ctx, document):
        """"sandbox profile absent" -- the second."""

        broken = mutate(document, "worker", sandbox=None)
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.SANDBOX_PROFILE_ABSENT in excinfo.value.codes

    def test_hook_path_unresolvable_refuses(self, ctx, document, tmp_path):
        """"hook path unresolvable" -- the third, and the one U42 says cannot be
        left to the hook itself.

        ``investigation/i04-pretooluse-fence-probe.md`` §5 measured the same
        missing hook failing *closed* under ``python3`` (exit 2) and *open*
        under ``bash`` (exit 127). Whether a broken fence holds therefore
        depends on the launcher, so it has to be caught before the spawn.
        """

        from dataclasses import replace

        broken_ctx = replace(ctx, hook_script=tmp_path / "not-there.py")
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", broken_ctx, document=document)
        assert RefusalReason.HOOK_UNRESOLVABLE in excinfo.value.codes

    def test_hooks_absent_refuses(self, ctx, document):
        broken = mutate(document, "worker", hooks=None)
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.HOOK_ABSENT in excinfo.value.codes

    def test_forbidden_allow_refuses(self, ctx, document):
        broken = mutate(
            document,
            "worker",
            permissions={"allow": ["Bash(git *)"], "deny": ["Bash(git push *)"]},
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.FORBIDDEN_ALLOW in excinfo.value.codes

    def test_forbidden_allow_by_regex_refuses(self, ctx, document):
        broken = mutate(
            document,
            "worker",
            permissions={"allow": ["Bash( * )"], "deny": ["Bash(git push *)"]},
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.FORBIDDEN_ALLOW in excinfo.value.codes

    def test_empty_deny_list_refuses(self, ctx, document):
        """A role with nothing denied is not a fence, and must not render as one."""

        broken = mutate(
            document,
            "worker",
            permissions={"allow": [], "deny": []},
            sandbox={"filesystem": {"denyRead": [], "denyWrite": []}},
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.EMPTY_FENCE in excinfo.value.codes

    def test_unparseable_rule_refuses_rather_than_being_skipped(self, ctx, document):
        """The ignore-and-continue case, stated directly.

        A rule that fails to parse and is dropped leaves a hole with no probe
        and no error -- indistinguishable from a fence that never had the rule.
        """

        broken = mutate(
            document,
            "worker",
            permissions={"allow": [], "deny": ["Bash(git push *)", "Bash(oops"]},
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.RULE_SYNTAX in excinfo.value.codes

    def test_a_deny_list_authored_as_a_string_refuses(self, ctx, document):
        """The mis-authoring that renders one rule per *letter*.

        ``"deny": "WebFetch"`` iterates character by character, so the fence
        gains rules for tools ``W``, ``e``, ``b`` ... each of which the
        self-battery cheerfully denies -- while the rule that was meant is
        simply absent. A green battery over the wrong rules is the worst shape
        this fence can take.
        """

        broken = mutate(
            document, "worker", permissions={"allow": [], "deny": "WebFetch"}
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.RULE_SYNTAX in excinfo.value.codes

    def test_a_sandbox_deny_list_authored_as_a_string_refuses(self, ctx, document):
        broken = mutate(
            document,
            "worker",
            sandbox={"filesystem": {"denyRead": "/etc/shadow", "denyWrite": []}},
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.RULE_SYNTAX in excinfo.value.codes

    def test_an_unlaunchable_hook_refuses(self, ctx, document):
        """The launcher is as load-bearing as the script it launches.

        i04 §5 measured an unresolvable hook failing **open** at exit 127 under
        ``bash``. A launcher that does not exist produces the same 127, so
        checking only the script leaves the identical hole one token to the
        left.
        """

        from dataclasses import replace

        broken_ctx = replace(ctx, python="python3-that-does-not-exist")
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", broken_ctx, document=document)
        assert RefusalReason.HOOK_UNRESOLVABLE in excinfo.value.codes

    def test_a_narrow_hook_matcher_refuses(self, ctx, document):
        """The quietest hole of the lot.

        With the deny hook scoped to ``"Bash"``, the fence still carries every
        Read / Write / WebFetch rule and the self-battery still denies every
        probe -- because the battery calls the decision function directly. The
        CLI simply never consults the hook for the exempted tools, and nothing
        anywhere goes red.
        """

        hooks = json.loads(json.dumps(document["roles"]["worker"]["hooks"]))
        hooks["PreToolUse"][0]["matcher"] = "Bash"
        broken = mutate(document, "worker", hooks=hooks)
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.HOOK_MATCHER_TOO_NARROW in excinfo.value.codes

    def test_every_shipped_role_scopes_the_deny_hook_to_all_tools(self, ctx, document):
        for role in role_names(document):
            fence = render_fence(role, ctx, document=document)
            for group in fence.settings["hooks"]["PreToolUse"]:
                if any(str(ctx.hook_script) in h["command"] for h in group["hooks"]):
                    assert group.get("matcher") in (None, "*", ".*", "")

    def test_a_hook_entry_that_is_not_a_command_refuses(self, ctx, document):
        """Only ``type: "command"`` entries are executed as commands.

        An entry of another type carrying a ``command`` key reads as correct
        and never runs.
        """

        hooks = json.loads(json.dumps(document["roles"]["worker"]["hooks"]))
        hooks["PreToolUse"][0]["hooks"][0]["type"] = "prompt"
        broken = mutate(document, "worker", hooks=hooks)
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.HOOK_NOT_A_COMMAND in excinfo.value.codes

    def test_unsubstituted_placeholder_refuses(self, ctx, document):
        broken = mutate(
            document,
            "worker",
            sandbox={"filesystem": {"denyWrite": ["{no_such_placeholder}/x"]}},
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.UNSUBSTITUTED_PLACEHOLDER in excinfo.value.codes

    def test_a_refusal_reports_every_reason_not_just_the_first(self, ctx, document):
        broken = mutate(
            document,
            "worker",
            sandbox=None,
            hooks=None,
            permissions={"allow": ["Bash(gh *)"], "deny": ["Bash(git push *)"]},
        )
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        codes = set(excinfo.value.codes)
        assert {
            RefusalReason.SANDBOX_PROFILE_ABSENT,
            RefusalReason.HOOK_ABSENT,
            RefusalReason.FORBIDDEN_ALLOW,
        } <= codes


class TestPermissionModeIsU15sAnswer:
    """U15's answer, as it reaches the rendered fence.

    `investigation/i04-pretooluse-fence-probe.md` §3 measured that
    ``PreToolUse`` *does* fire and *does* deny under ``bypassPermissions``. The
    renderer refuses the mode anyway, and the reason is in §4 of the same file:
    under ``bypassPermissions`` the hook is the only layer left, and a hook
    exiting 1 was measured being absorbed at exit 0 with no other signal.
    """

    def test_bypass_permissions_refuses_the_render(self, ctx, document):
        broken = mutate(document, "worker", permission_mode="bypassPermissions")
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.PERMISSION_MODE_BYPASS in excinfo.value.codes

    def test_an_unknown_permission_mode_refuses(self, ctx, document):
        broken = mutate(document, "worker", permission_mode="yolo")
        with pytest.raises(FenceRefusal) as excinfo:
            render_fence("worker", ctx, document=broken)
        assert RefusalReason.PERMISSION_MODE_INVALID in excinfo.value.codes

    def test_no_shipped_role_asks_for_bypass_permissions(self, document):
        for role, body in document["roles"].items():
            assert body.get("permission_mode") != "bypassPermissions", role
