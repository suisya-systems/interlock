"""The public effective-configuration readback, and its normalisation rule.

U3 was answered in ``investigation/i01-supervisor-probe.md`` §3.9: the
``system/init`` event reports ``permissionMode``, ``tools`` and ``mcp_servers``
-- and **no hooks and no sandbox key**. Two consequences are pinned here:

1. The readback is usable for permission mode, so item 3's equality check
   becomes *partly* runnable as written across an Interlock restart.
2. The ``tools`` array is **not** stable across identical runs. §3.9 measured
   107 vs 128 tools between two runs of one configuration, the entire
   difference being the tool family of a single MCP server reported ``pending``
   in one and ``connected`` in the other. A naive diff is therefore unsound,
   and the fix is in the same event: require every server ``connected``, or
   exclude MCP tools.

The last test is the point of the file: the comparison **refuses** rather than
returning a verdict it cannot support. A flapping oracle is worse than no
check.
"""

from __future__ import annotations

import json

import pytest

from claude_org_runtime.fencing.readback import (
    ReadbackUnsound,
    compare_readbacks,
    parse_init_event,
    read_init_event,
)


def init_event(*, mode="default", tools, servers):
    return {
        "type": "system",
        "subtype": "init",
        "session_id": "11112222-3333-4444-5555-666677778888",
        "permissionMode": mode,
        "tools": list(tools),
        "mcp_servers": [{"name": n, "status": s} for n, s in servers],
    }


# The §3.9 measurement, reduced to its shape: one configuration, two runs, and
# the whole difference is one server's tool family plus the three MCP-coupled
# tool names that arrive with it.
CONTROL_A = init_event(
    mode="auto",
    tools=["Bash", "Read", "Write"],
    servers=[("claude.ai Slack", "pending")],
)
CONTROL_B = init_event(
    mode="auto",
    tools=[
        "Bash",
        "Read",
        "Write",
        "ListMcpResourcesTool",
        "ReadMcpResourceTool",
        "ReadMcpResourceDirTool",
        "mcp__claude_ai_Slack__slack_read_channel",
    ],
    servers=[("claude.ai Slack", "connected")],
)


class TestParsing:
    def test_the_init_event_is_found_in_a_stream(self):
        lines = [
            json.dumps({"type": "system", "subtype": "other"}),
            "",
            "not json",
            json.dumps(CONTROL_A),
        ]
        readback = read_init_event(lines)
        assert readback.permission_mode == "auto"

    def test_a_stream_with_no_init_event_is_an_error_not_an_empty_readback(self):
        with pytest.raises(ReadbackUnsound):
            read_init_event([json.dumps({"type": "assistant"})])

    def test_mcp_server_statuses_are_read(self):
        readback = parse_init_event(CONTROL_A)
        assert readback.mcp_servers == (("claude.ai Slack", "pending"),)
        assert not readback.all_servers_connected


class TestAnIncompleteReadbackIsUnsoundNotEqual:
    """The falsest of false positives.

    If a missing ``permissionMode`` defaulted to ``None`` and a missing
    ``tools`` to ``()``, two *empty* readbacks would compare **equal** and the
    restart check would report that the fence survived a restart it never
    observed.
    """

    def test_a_readback_with_no_permission_mode_is_refused(self):
        with pytest.raises(ReadbackUnsound):
            parse_init_event({"type": "system", "subtype": "init", "tools": []})

    def test_a_readback_with_no_tools_is_refused(self):
        with pytest.raises(ReadbackUnsound):
            parse_init_event(
                {"type": "system", "subtype": "init", "permissionMode": "default"}
            )

    def test_a_malformed_tools_field_is_refused(self):
        with pytest.raises(ReadbackUnsound):
            parse_init_event(
                {"type": "system", "subtype": "init", "permissionMode": "d", "tools": "Bash"}
            )

    def test_two_empty_events_cannot_be_compared_into_equality(self):
        with pytest.raises(ReadbackUnsound):
            parse_init_event({"type": "system", "subtype": "init"})


class TestTheNormalisationRule:
    def test_two_runs_of_one_configuration_differ_before_normalisation(self):
        """§3.9's measurement, restated: the raw arrays are not equal."""

        assert set(CONTROL_A["tools"]) != set(CONTROL_B["tools"])

    def test_excluding_mcp_tools_makes_them_equal(self):
        comparison = compare_readbacks(
            parse_init_event(CONTROL_A), parse_init_event(CONTROL_B)
        )
        assert comparison.equal
        assert comparison.normalisation == "mcp-tools-excluded"
        assert not comparison.added_tools and not comparison.removed_tools

    def test_requiring_connected_refuses_when_a_server_is_pending(self):
        """The refusal, not a false mismatch.

        A restart reported as a fence change because a server was slow to
        connect would be a false failure of item 3, and a caller who learned to
        ignore it would be ignoring the real ones too.
        """

        with pytest.raises(ReadbackUnsound):
            compare_readbacks(
                parse_init_event(CONTROL_A),
                parse_init_event(CONTROL_B),
                require_connected=True,
            )

    def test_requiring_connected_compares_the_full_list_when_it_can(self):
        both_connected = init_event(
            mode="auto",
            tools=CONTROL_B["tools"],
            servers=[("claude.ai Slack", "connected")],
        )
        comparison = compare_readbacks(
            parse_init_event(CONTROL_B),
            parse_init_event(both_connected),
            require_connected=True,
        )
        assert comparison.equal
        assert comparison.normalisation == "all-servers-connected"


class TestWhatTheReadbackCanAndCannotSettle:
    def test_permission_mode_is_diffable_across_a_restart(self):
        before = parse_init_event(init_event(mode="default", tools=["Bash"], servers=[]))
        after = parse_init_event(init_event(mode="default", tools=["Bash"], servers=[]))
        assert compare_readbacks(before, after).permission_mode_equal

    def test_a_changed_permission_mode_is_caught(self):
        before = parse_init_event(init_event(mode="default", tools=["Bash"], servers=[]))
        after = parse_init_event(
            init_event(mode="bypassPermissions", tools=["Bash"], servers=[])
        )
        comparison = compare_readbacks(before, after)
        assert not comparison.permission_mode_equal
        assert not comparison.equal

    def test_a_genuinely_changed_non_mcp_tool_set_is_caught(self):
        before = parse_init_event(init_event(mode="auto", tools=["Bash", "Read"], servers=[]))
        after = parse_init_event(init_event(mode="auto", tools=["Read"], servers=[]))
        comparison = compare_readbacks(before, after)
        assert not comparison.tools_equal
        assert comparison.removed_tools == ("Bash",)

    def test_the_readback_carries_no_hooks_and_no_sandbox(self):
        """The residual, asserted rather than described.

        This is why D-0023's weakening of item 3 is still needed: the two
        layers the breach battery is the *only* observable for are absent from
        the one public surface that reports anything at all.
        """

        readback = parse_init_event(CONTROL_A)
        assert not hasattr(readback, "hooks")
        assert not hasattr(readback, "sandbox")
        assert set(CONTROL_A) & {"hooks", "sandbox", "permissions"} == set()
