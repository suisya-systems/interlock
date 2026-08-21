"""The supervisor-side join between the control plane and a session provider.

D-0009 keeps ``SessionProvider`` and the control plane as two contracts with
"an explicit join between session and run" as the named cost. This package is
that join for the crash-window work (gate item 2, issue ``#18``): it composes
the lease, the staged session binding and the S1 provider verbs into the
commit-before-spawn orchestration, and it is the only place the two sides meet.
``control_plane`` still imports nothing from ``session`` (the leak tests keep
that true), and ``session`` still imports nothing from ``control_plane``.

Spike status: throwaway by default (D-0026); the durable half is the tests.
"""

from .session_orchestrator import (
    IdentityUnconfirmed,
    LoserTerminated,
    OrchestrationOutcome,
    OrchestrationRefused,
    ProviderStartFailed,
    SessionOrchestrator,
    default_identity_confirmation,
)

__all__ = [
    "IdentityUnconfirmed",
    "LoserTerminated",
    "OrchestrationOutcome",
    "OrchestrationRefused",
    "ProviderStartFailed",
    "SessionOrchestrator",
    "default_identity_confirmation",
]
