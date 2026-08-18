"""The public effective-configuration readback, and its normalisation rule.

U3 was answered in ``investigation/i01-supervisor-probe.md`` §3.9: the
``system/init`` event on ``--output-format stream-json`` reports the effective
``permissionMode``, ``tools`` and ``mcp_servers`` -- and **no hooks and no
sandbox key**. That is why D-0023's weakening of item 3 is still needed, and
why it is *narrowed* rather than removed: permission mode gains a direct
readback, hooks and sandbox keep the breach battery as their only observable.

§3.9 also found the normalisation rule this module implements. Two runs of an
*identical* configuration returned 107 and 128 tools; the entire difference was
the tool family of one MCP server reported ``pending`` in one run and
``connected`` in the other. So ``init`` is emitted before every MCP server has
finished connecting, and a naive ``tools`` diff is unsound. A comparison is
sound only if every server reads ``connected``, or if MCP tools are excluded
from it. :func:`compare_readbacks` refuses to answer rather than answering
unsoundly -- a flapping oracle is worse than no check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

MCP_TOOL_PREFIX = "mcp__"
# Tool names the CLI exposes only once an MCP server is connected. i01 §3.9
# saw these three arrive together with the server's own tool family.
MCP_COUPLED_TOOLS = frozenset(
    {"ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool"}
)


class ReadbackUnsound(RuntimeError):
    """The readback cannot be compared soundly, and no verdict is invented."""


@dataclass(frozen=True)
class InitReadback:
    """The parts of ``system/init`` that bear on the fence."""

    session_id: str | None
    permission_mode: str | None
    tools: tuple[str, ...]
    mcp_servers: tuple[tuple[str, str], ...]

    @property
    def all_servers_connected(self) -> bool:
        return all(status == "connected" for _, status in self.mcp_servers)

    def stable_tools(self) -> frozenset[str]:
        """The tool set with every MCP-conditioned name removed (§3.9)."""

        return frozenset(
            name
            for name in self.tools
            if not name.startswith(MCP_TOOL_PREFIX) and name not in MCP_COUPLED_TOOLS
        )


def parse_init_event(payload: Mapping[str, Any]) -> InitReadback:
    """Parse one ``system/init`` event, refusing an incomplete one.

    Defaulting a missing ``permissionMode`` to ``None`` and a missing ``tools``
    to ``()`` would make two *empty* readbacks compare **equal**, and the
    comparison would report that the fence survived a restart it never
    observed. An absent field is an unsound readback, not a comparable one.
    """

    for key in ("permissionMode", "tools"):
        if key not in payload:
            raise ReadbackUnsound(f"system/init event has no {key!r}")
    if not isinstance(payload["permissionMode"], str) or not payload["permissionMode"]:
        raise ReadbackUnsound(f"permissionMode is not a mode: {payload['permissionMode']!r}")
    if not isinstance(payload["tools"], list):
        raise ReadbackUnsound(f"tools is not a list: {payload['tools']!r}")

    servers: list[tuple[str, str]] = []
    for entry in payload.get("mcp_servers") or ():
        if isinstance(entry, Mapping):
            servers.append((str(entry.get("name", "")), str(entry.get("status", "unknown"))))
    tools = tuple(str(t) for t in payload.get("tools") or ())
    return InitReadback(
        session_id=_opt_str(payload.get("session_id")),
        permission_mode=_opt_str(payload.get("permissionMode")),
        tools=tools,
        mcp_servers=tuple(servers),
    )


def read_init_event(stream: Iterable[str]) -> InitReadback:
    """First ``{"type": "system", "subtype": "init"}`` line of a stream-json run."""

    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping) and payload.get("type") == "system":
            if payload.get("subtype") == "init":
                return parse_init_event(payload)
    raise ReadbackUnsound("no system/init event in the stream")


@dataclass(frozen=True)
class ReadbackComparison:
    permission_mode_equal: bool
    tools_equal: bool
    normalisation: str
    added_tools: tuple[str, ...]
    removed_tools: tuple[str, ...]

    @property
    def equal(self) -> bool:
        return self.permission_mode_equal and self.tools_equal


def compare_readbacks(
    before: InitReadback,
    after: InitReadback,
    *,
    require_connected: bool = False,
) -> ReadbackComparison:
    """Compare two readbacks across an Interlock-initiated restart.

    ``require_connected=True`` demands every MCP server read ``connected`` in
    both runs and then compares the full tool list. The default instead drops
    MCP-conditioned names, which is sound without waiting on connection state
    and is what a restart check should use: a restart must not be reported as a
    fence change because a server was slow.
    """

    if require_connected:
        if not (before.all_servers_connected and after.all_servers_connected):
            raise ReadbackUnsound(
                "an MCP server was not 'connected'; the tools array is a snapshot of "
                "whatever had connected at that instant (i01 §3.9) and cannot be diffed"
            )
        left, right = frozenset(before.tools), frozenset(after.tools)
        normalisation = "all-servers-connected"
    else:
        left, right = before.stable_tools(), after.stable_tools()
        normalisation = "mcp-tools-excluded"

    return ReadbackComparison(
        permission_mode_equal=before.permission_mode == after.permission_mode,
        tools_equal=left == right,
        normalisation=normalisation,
        added_tools=tuple(sorted(right - left)),
        removed_tools=tuple(sorted(left - right)),
    )


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) else None
