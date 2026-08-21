"""S8 -- the MCP endpoint: the acceptance sequence over the actual transport.

Two layers, on purpose. The in-process tests drive
:class:`~claude_org_runtime.messagebus.endpoint.Endpoint.handle` directly, so
every JSON-RPC branch is reachable without process management. The end-to-end
test then runs the whole item 6 sequence -- send, first delivery dropped,
resend, exactly one ack -- against a real ``python -m
claude_org_runtime.messagebus.endpoint`` child over real stdio, with the drop
injected at the transport boundary (``INTERLOCK_MESSAGEBUS_FAULT=
drop-first-poll``), because "worker-outbound MCP endpoint" is a claim about a
wire and one test should contain an actual wire.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_org_runtime.messagebus.endpoint import Endpoint, EndpointConfig

from ._env import EPOCH, HOLDER, RECIPIENT, RESOURCE, RUN_ID, make_bus_env

HOUR_MS = 3_600_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _config(env: dict | None = None) -> EndpointConfig:
    base = {
        "INTERLOCK_MESSAGEBUS_DB": "unused-in-process",
        "INTERLOCK_MESSAGEBUS_RESOURCE": RESOURCE,
        "INTERLOCK_MESSAGEBUS_HOLDER": HOLDER,
        "INTERLOCK_MESSAGEBUS_EPOCH": str(EPOCH),
        "INTERLOCK_MESSAGEBUS_RECIPIENT": RECIPIENT,
        "INTERLOCK_MESSAGEBUS_DESTINATION_DIR": "unused-in-process",
    }
    base.update(env or {})
    return EndpointConfig(base)


@pytest.fixture
def rt_env(tmp_path: Path):
    env = make_bus_env(tmp_path, "endpoint", now_ms=_now_ms(), ttl_ms=HOUR_MS)
    yield env
    env.close()


def send(env, message_id="task-1"):
    return env.bus.send(
        message_id=message_id,
        recipient=RECIPIENT,
        payload='{"task":"t"}',
        dedup_key=f"dk-{message_id}",
        now_ms=_now_ms(),
        epoch=EPOCH,
        run_id=RUN_ID,
    )


def call(endpoint: Endpoint, name: str, arguments: dict | None = None, mid: int = 1):
    response = endpoint.handle({
        "jsonrpc": "2.0", "id": mid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })
    result = response["result"]
    if result.get("isError"):
        return {"error": result["content"][0]["text"]}
    return json.loads(result["content"][0]["text"])


# ----------------------------------------------------------------- in-process


def test_initialize_declares_a_tool_surface(rt_env):
    endpoint = Endpoint(rt_env.bus, _config())
    response = endpoint.handle({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"},
    })
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {}}
    listed = endpoint.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["poll", "ack"]


def test_poll_then_ack_over_the_tool_surface(rt_env):
    endpoint = Endpoint(rt_env.bus, _config())
    send(rt_env)
    first = call(endpoint, "poll")
    assert [m["message_id"] for m in first["messages"]] == ["task-1"]
    acked = call(endpoint, "ack", {"message_id": "task-1"})
    assert acked["recorded"] is True
    assert call(endpoint, "poll")["messages"] == []


def test_the_drop_first_poll_fault_loses_the_response_not_the_message(rt_env):
    """The wire-drop, observed from both sides of the boundary.

    The faulted first poll answers empty -- the worker saw nothing -- while the
    database already says delivered: exactly the state a response lost on the
    wire leaves behind. The next poll re-presents, and one ack settles it.
    """

    endpoint = Endpoint(
        rt_env.bus, _config({"INTERLOCK_MESSAGEBUS_FAULT": "drop-first-poll"})
    )
    send(rt_env)
    first = call(endpoint, "poll")
    assert first == {"messages": [], "fault": "drop-first-poll"}
    status = rt_env.connection.execute(
        "SELECT status FROM outbox WHERE message_id = 'task-1'"
    ).fetchone()[0]
    assert status == "delivered"
    second = call(endpoint, "poll")
    assert [m["message_id"] for m in second["messages"]] == ["task-1"]
    assert second["messages"][0]["deduplicated"] is True
    assert call(endpoint, "ack", {"message_id": "task-1"})["recorded"] is True
    assert rt_env.effect_count("dk-task-1") == 1


def test_a_notification_never_gets_a_response(rt_env):
    """JSON-RPC framing: a message without an id must produce no output line.

    Answering a notification -- even with ``"id": null`` -- puts a stray line
    on stdout that the client matches against its next request, and the stream
    is desynchronised from then on.
    """

    endpoint = Endpoint(rt_env.bus, _config())
    for method in ("notifications/initialized", "ping", "tools/list", "nonsense"):
        assert endpoint.handle({"jsonrpc": "2.0", "method": method}) is None
    ponged = endpoint.handle({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert ponged == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_malformed_params_are_answered_not_fatal(rt_env):
    """Invalid params get -32602 and the endpoint keeps serving."""

    endpoint = Endpoint(rt_env.bus, _config())
    for bad in ([], "x", 7):
        for method in ("initialize", "tools/call"):
            response = endpoint.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": bad}
            )
            assert response["error"]["code"] == -32602
    bad_args = endpoint.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "poll", "arguments": []},
    })
    assert bad_args["error"]["code"] == -32602
    alive = endpoint.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert alive == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_a_refusal_surfaces_as_a_tool_error_not_a_crash(rt_env):
    endpoint = Endpoint(rt_env.bus, _config())
    send(rt_env)
    refused = call(endpoint, "ack", {"message_id": "task-1"})
    assert "error" in refused and "ValueError" in refused["error"]
    unknown = endpoint.handle({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert unknown["error"]["code"] == -32602


# -------------------------------------------------------------- end to end


class _Client:
    """A minimal line-delimited JSON-RPC client over a child's stdio."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        line = self._process.stdout.readline()
        assert line, "endpoint closed stdout unexpectedly"
        response = json.loads(line)
        assert response["id"] == self._next_id
        return response

    def notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        response = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        return json.loads(response["result"]["content"][0]["text"])

    def _write(self, msg: dict) -> None:
        self._process.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._process.stdin.flush()


def test_the_acceptance_sequence_end_to_end_over_stdio(rt_env, tmp_path):
    """Item 6's headline case with a real child process on the wire."""

    send(rt_env)
    rt_env.connection.commit()

    repo_src = Path(__file__).resolve().parents[2] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_src) + os.pathsep + env.get("PYTHONPATH", "")
    env.update({
        "INTERLOCK_MESSAGEBUS_DB": str(rt_env.db_path),
        "INTERLOCK_MESSAGEBUS_RESOURCE": RESOURCE,
        "INTERLOCK_MESSAGEBUS_HOLDER": HOLDER,
        "INTERLOCK_MESSAGEBUS_EPOCH": str(EPOCH),
        "INTERLOCK_MESSAGEBUS_RECIPIENT": RECIPIENT,
        # The child publishes effects into the same destination directory the
        # parent's dropbox reads, so effect_count below is the child's ledger.
        "INTERLOCK_MESSAGEBUS_DESTINATION_DIR": str(
            tmp_path / "destination-endpoint"
        ),
        "INTERLOCK_MESSAGEBUS_FAULT": "drop-first-poll",
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "claude_org_runtime.messagebus.endpoint"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        client = _Client(process)
        initialized = client.request(
            "initialize", {"protocolVersion": "2025-06-18"}
        )
        assert initialized["result"]["serverInfo"]["name"] == "interlock-messagebus"
        client.notify("notifications/initialized")

        dropped = client.call_tool("poll")
        assert dropped == {"messages": [], "fault": "drop-first-poll"}

        resent = client.call_tool("poll")
        assert [m["message_id"] for m in resent["messages"]] == ["task-1"]

        acked = client.call_tool("ack", {"message_id": "task-1"})
        assert acked["recorded"] is True
        again = client.call_tool("ack", {"message_id": "task-1"})
        assert again["recorded"] is False

        assert client.call_tool("poll")["messages"] == []
    finally:
        process.stdin.close()
        process.wait(timeout=15)

    assert rt_env.effect_count("dk-task-1") == 1
    status = rt_env.connection.execute(
        "SELECT status FROM outbox WHERE message_id = 'task-1'"
    ).fetchone()[0]
    assert status == "acked"
