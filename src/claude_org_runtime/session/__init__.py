"""Session management contracts. Currently S1 alone, and S1 is provisional.

``SessionProvider`` (D-0009) lives here; ``MessageBus`` deliberately does not
-- delivery is a separate contract built as S8, and the separation is the
point (see ``provider.DELIVERY_ABSENCE_IS_DELIBERATE``).
"""

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
    "CapabilityAssignment",
    "CapabilityReport",
    "ContractViolation",
    "Failure",
    "FailureKind",
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
]
