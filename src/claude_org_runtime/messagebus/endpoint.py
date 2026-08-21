"""S8 -- the worker-outbound MCP endpoint over one :class:`MessageBus`.

.. warning::

   **Spike scaffold, throwaway by default (D-0026).** The MCP surface here is
   the minimum that makes "worker-outbound" a demonstrated fact rather than a
   diagram: a stdio server a worker connects to as a client, exposing exactly
   the two verbs a recipient has -- ``poll`` and ``ack``.

**Why worker-outbound.** Per F1 there is no non-interactive path to deliver a
message *into* a running background session, so delivery is a pull: the worker
runs this endpoint as one of its MCP servers and calls ``poll``. Nothing here
pushes, nudges, or injects; an idle worker that never polls simply leaves its
rows due, visible to any operator via the outbox tables.

**Transport shape.** Line-delimited JSON-RPC over stdio (one message per line),
the same framing as :mod:`claude_org_runtime.broker.channel_sidecar`, which is
this repo's precedent for a hand-rolled MCP server. Unlike that sidecar this
server *does* declare tools -- it is a tool surface, not a push channel.

**Configuration is env-driven** (no argparse; the worker's MCP config sets
env), all ASCII:

- ``INTERLOCK_MESSAGEBUS_DB`` -- path to the control-plane SQLite database.
- ``INTERLOCK_MESSAGEBUS_RESOURCE`` / ``INTERLOCK_MESSAGEBUS_HOLDER`` /
  ``INTERLOCK_MESSAGEBUS_EPOCH`` -- the lease identity this endpoint's writes
  are fenced under. The endpoint does not acquire or renew the lease; lease
  orchestration is the control plane's (Issue ``#18``'s side), and a stale
  epoch surfaces as ``StaleWriterRefused`` out of ``poll``, refused durably.
- ``INTERLOCK_MESSAGEBUS_RECIPIENT`` -- the one recipient this endpoint serves.
  ``poll`` is pinned to it; a worker cannot pull another recipient's queue
  through this surface.
- ``INTERLOCK_MESSAGEBUS_DESTINATION_DIR`` -- directory for the spike
  destination (:class:`~claude_org_runtime.control_plane.destination.KeyedDropbox`)
  behind the registered handler.
- ``INTERLOCK_MESSAGEBUS_FAULT`` -- test-only fault injection.
  ``drop-first-poll``: the first ``poll`` runs its delivery attempts (rows
  become delivered-but-unacked) but the response body reports no messages --
  the wire-drop of a first delivery, reproduced at the transport boundary so
  the resend-and-single-ack acceptance case runs end to end over real stdio.

**No session edge.** This module, like the rest of the package, imports
nothing from :mod:`claude_org_runtime.session`. It has no idea whether the
worker polling it is alive, wedged, or replaced; it does not want to know, and
the static assertion in ``tests/messagebus/test_import_graph.py`` makes not
knowing a build-enforced property.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict

from ..control_plane.destination import KeyedDropbox
from ..control_plane.handlers import NOTIFY_RECIPIENT, spike_registry
from ..control_plane.schema import open_control_plane
from .bus import MessageBus

__all__ = [
    "EndpointConfig",
    "Endpoint",
    "main",
]

_SUPPORTED_PROTO = frozenset((
    "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05",
))
_DEFAULT_PROTO = "2025-06-18"

_FAULT_DROP_FIRST_POLL = "drop-first-poll"

_TOOLS = (
    {
        "name": "poll",
        "description": (
            "Pull every message currently due for this endpoint's recipient. "
            "Each returned message is marked delivered; call ack per message "
            "once it is handled. An unacked message is presented again on the "
            "next poll."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ack",
        "description": (
            "Settle one delivered message by id. Idempotent: repeating an ack "
            "changes nothing and reports recorded=false."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    },
)


class EndpointConfig:
    """The env contract, read once and validated loudly."""

    def __init__(self, env: dict[str, str]) -> None:
        self.db_path = env.get("INTERLOCK_MESSAGEBUS_DB", "")
        self.resource = env.get("INTERLOCK_MESSAGEBUS_RESOURCE", "")
        self.holder = env.get("INTERLOCK_MESSAGEBUS_HOLDER", "")
        self.recipient = env.get("INTERLOCK_MESSAGEBUS_RECIPIENT", NOTIFY_RECIPIENT)
        self.destination_dir = env.get("INTERLOCK_MESSAGEBUS_DESTINATION_DIR", "")
        self.fault = env.get("INTERLOCK_MESSAGEBUS_FAULT", "")
        self.epoch: int | None
        try:
            self.epoch = int(env.get("INTERLOCK_MESSAGEBUS_EPOCH", ""))
        except ValueError:
            self.epoch = None

    def missing(self) -> list[str]:
        gaps = []
        if not self.db_path:
            gaps.append("INTERLOCK_MESSAGEBUS_DB")
        if not self.resource:
            gaps.append("INTERLOCK_MESSAGEBUS_RESOURCE")
        if not self.holder:
            gaps.append("INTERLOCK_MESSAGEBUS_HOLDER")
        if self.epoch is None:
            gaps.append("INTERLOCK_MESSAGEBUS_EPOCH (unset or not an integer)")
        if not self.destination_dir:
            gaps.append("INTERLOCK_MESSAGEBUS_DESTINATION_DIR")
        return gaps


def _now_ms() -> int:
    return int(time.time() * 1000)


class Endpoint:
    """The JSON-RPC message handler; transport-free so tests can drive it."""

    def __init__(self, bus: MessageBus, config: EndpointConfig) -> None:
        self._bus = bus
        self._config = config
        self._polls_answered = 0

    # ------------------------------------------------------------- tools
    def _tool_poll(self) -> dict:
        envelopes = self._bus.poll(
            self._config.recipient,
            now_ms=_now_ms(),
            epoch=self._config.epoch,
        )
        self._polls_answered += 1
        if (
            self._config.fault == _FAULT_DROP_FIRST_POLL
            and self._polls_answered == 1
        ):
            # The attempts above already ran: the rows are delivered and
            # unacked, exactly as if this response were lost on the wire.
            return {"messages": [], "fault": _FAULT_DROP_FIRST_POLL}
        return {"messages": [asdict(envelope) for envelope in envelopes]}

    def _tool_ack(self, arguments: dict) -> dict:
        message_id = arguments.get("message_id", "")
        outcome = self._bus.ack(
            message_id, now_ms=_now_ms(), recipient=self._config.recipient
        )
        return {
            "message_id": outcome.message_id,
            "recorded": outcome.recorded,
            "acked_at_ms": outcome.acked_at_ms,
            "clock_clamped": outcome.clock_clamped,
        }

    # ---------------------------------------------------------- JSON-RPC
    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        if "id" not in msg:
            # A JSON-RPC notification never receives a response; answering one
            # (even with "id": null) desynchronises the stdio stream, because
            # the client matches the stray line against its next request. The
            # only notification this server cares about, initialized, needs no
            # action here; every other one is ignored.
            return None
        mid = msg["id"]

        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            # A long-running endpoint must answer malformed parameters, not
            # die of them: an array or scalar params is the caller's error,
            # reported as invalid params with the transport intact.
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602,
                              "message": "params must be an object"}}

        if method == "initialize":
            want = params.get("protocolVersion", _DEFAULT_PROTO)
            proto = want if want in _SUPPORTED_PROTO else _DEFAULT_PROTO
            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "interlock-messagebus", "version": "0.1.0"},
                    "instructions": (
                        "Worker-outbound message bus. Call poll to pull due "
                        "messages for this recipient; call ack per message "
                        "once handled."
                    ),
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": list(_TOOLS)}}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32602,
                                  "message": "arguments must be an object"}}
            try:
                if name == "poll":
                    payload = self._tool_poll()
                elif name == "ack":
                    payload = self._tool_ack(arguments)
                else:
                    return {
                        "jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32602,
                                  "message": f"unknown tool: {name}"},
                    }
            except Exception as exc:
                # A refusal (stale writer, undelivered ack, unknown message)
                # is a tool-level error the worker should see verbatim, not a
                # transport failure.
                return {
                    "jsonrpc": "2.0", "id": mid,
                    "result": {
                        "isError": True,
                        "content": [{
                            "type": "text",
                            "text": f"{type(exc).__name__}: {exc}",
                        }],
                    },
                }
            return {
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(payload, ensure_ascii=True),
                    }],
                },
            }
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601,
                          "message": f"method not found: {method}"}}


def _serve(endpoint: Endpoint, stdin, stdout) -> None:
    for raw in stdin:
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            # Valid JSON that is not a message object gets the same silence as
            # malformed JSON: one bad line must not take the transport down.
            continue
        resp = endpoint.handle(msg)
        if resp is not None:
            stdout.write((json.dumps(resp, ensure_ascii=True) + "\n").encode("utf-8"))
            stdout.flush()


def main() -> int:
    config = EndpointConfig(dict(os.environ))
    gaps = config.missing()
    if gaps:
        print("FATAL: missing env: " + ", ".join(gaps), file=sys.stderr, flush=True)
        return 2
    connection = open_control_plane(config.db_path)
    dropbox = KeyedDropbox(config.destination_dir, name="messagebus-endpoint")
    registry = spike_registry(dropbox)
    bus = MessageBus(
        connection,
        resource=config.resource,
        holder=config.holder,
        registry=registry,
    )
    endpoint = Endpoint(bus, config)
    try:
        _serve(endpoint, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
