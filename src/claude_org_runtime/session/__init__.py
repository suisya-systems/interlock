"""Session management contracts, and the stub that implements them.

``SessionProvider`` (D-0009) lives here; ``MessageBus`` deliberately does not
-- delivery is a separate contract built as S8, and the separation is the
point (see ``provider.DELIVERY_ABSENCE_IS_DELIBERATE``).

``LocalProcessSessionProvider`` (S3) is the deliberately trivial implementation
over local child processes; ``ClaudeCliSessionProvider`` (S2) is the C2
implementation over Interlock-supervised ``claude -p`` subprocesses (D-0025,
D-0027). The contract they implement stays provisional (D-0021) whether or not
something implements it.
"""

from .claude_cli_provider import ClaudeCliSessionProvider, claude_session_uuid
from .provider import (
    CAPABILITY_ASSIGNMENTS,
    D0009_VERBS,
    DELIVERY_ABSENCE_IS_DELIBERATE,
    OWNER_MESSAGE_BUS,
    OWNER_NEITHER_CONTRACT,
    OWNER_SESSION_PROVIDER,
    PROMOTION_REQUIRES,
    PROVISIONAL,
    REQUIRED_CAPABILITIES,
    VERB_IMPLEMENTATION_HOOKS,
    CapabilityAssignment,
    CapabilityReport,
    ContractViolation,
    Failure,
    FailureKind,
    Observation,
    Ok,
    ProviderResult,
    SessionProvider,
    SessionReadout,
    SpawnRefused,
    StartRequest,
    WorkspaceDecision,
    WorkspaceLifecycleObserver,
    WorkspaceTransition,
    WorkspaceVerdict,
    check_spawn_precondition,
)
from .stub_provider import LocalProcessSessionProvider

__all__ = [
    "CAPABILITY_ASSIGNMENTS",
    "D0009_VERBS",
    "DELIVERY_ABSENCE_IS_DELIBERATE",
    "OWNER_MESSAGE_BUS",
    "OWNER_NEITHER_CONTRACT",
    "OWNER_SESSION_PROVIDER",
    "PROMOTION_REQUIRES",
    "PROVISIONAL",
    "REQUIRED_CAPABILITIES",
    "VERB_IMPLEMENTATION_HOOKS",
    "CapabilityAssignment",
    "CapabilityReport",
    "ClaudeCliSessionProvider",
    "ContractViolation",
    "Failure",
    "FailureKind",
    "LocalProcessSessionProvider",
    "Observation",
    "Ok",
    "ProviderResult",
    "SessionProvider",
    "SessionReadout",
    "SpawnRefused",
    "StartRequest",
    "WorkspaceDecision",
    "WorkspaceLifecycleObserver",
    "WorkspaceTransition",
    "WorkspaceVerdict",
    "check_spawn_precondition",
    "claude_session_uuid",
]
