"""S1 -- the **provisional** ``SessionProvider`` interface.

.. warning::

   **This file is spike scaffold, not a settled contract (D-0021).** It is
   written during the provider spike so that gate item 11 has something to
   substitute against; §2 of ``docs/proposals/agent-view-gate-scaffold.md``
   records that until this file exists, item 11 has nothing to measure. It is
   promoted to a settled contract **only by a later ``D-`` entry** -- not by
   being imported, not by an implementation depending on it, and not by having
   survived a gate run. Nothing here is load-bearing by inertia.

What the interface carries, and why each piece is here:

five verbs (D-0009)
    ``start``, ``list_sessions``, ``read_state``, ``stop``, ``resume``. D-0009
    names these five and only these five for top-level worker sessions, with no
    signatures, no state model and no error contract -- which is precisely the
    hole this file fills. See :data:`D0009_VERBS`.

a provider-neutral lifecycle / availability readout
    :class:`SessionReadout` carries the backend's **own** state string,
    uninterpreted, plus an explicit *could not observe* case
    (:class:`Observation`). The interface deliberately does not enumerate
    provider states: an enumeration written from one provider's vocabulary is
    an Agent-View-shaped (or ``claude -p``-shaped) assumption smuggled into a
    provider-neutral contract.

a typed error / unavailable result that is never an empty one (R4)
    :class:`Ok` / :class:`Failure`. R4 records that the v1 reader collapsed a
    read failure into an *empty result*, which made "could not observe" and
    "observed nothing happening" indistinguishable downstream. Here the two are
    different types, and neither can be constructed empty.

a capability / version probe with a fail-closed spawn precondition (D-0010)
    :meth:`SessionProvider.probe_capabilities` plus
    :func:`check_spawn_precondition`. On an incompatible -- or simply
    **unprobed** -- provider, a new spawn is *refused*, not attempted with
    degraded assumptions.

Two prohibitions, both load-bearing, both mechanically asserted in
``tests/session/test_provider_contract.py``:

**No fact-state vocabulary appears in this file.** D-0005 fixes a closed set of
*watcher fact* names whose predicates and precedence ``Q-0012`` leaves open.
Folding a provider's own lifecycle words into that set inside a provisional
interface would either lose information or answer ``Q-0012`` by implementation.
Conversion from provider lifecycle to watcher fact belongs to the detector
layer, where it is fixture-testable and versioned (D-0005, D-0007, Q-0009).
The test reads the closed set out of ``DECISIONS.md`` and asserts none of its
names occurs in this module's source.

**No message-delivery verb appears in this file, and the absence is
deliberate.** Delivery, ack, dedup and message identity are ``MessageBus``'s
under D-0009 and are built as S8; binding delivery to the session backend is
exactly the v1 coupling D-0009 exists to break. What S1 records for delivery is
therefore the *absence* of the verb -- the property gate items 6 and 11 exist
to check. See :data:`DELIVERY_ABSENCE_IS_DELIBERATE` and
:data:`CAPABILITY_ASSIGNMENTS`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Mapping, Protocol, Sequence, TypeVar, Union, runtime_checkable

# --------------------------------------------------------------------------
# Provisional marking (D-0021)
# --------------------------------------------------------------------------

#: This interface is spike scaffold. See the module docstring and D-0021.
PROVISIONAL = True

#: What it takes to make this a settled contract. Nothing weaker counts.
PROMOTION_REQUIRES = (
    "a later D- entry in DECISIONS.md that promotes S1 to a settled contract "
    "(D-0021). Use by an implementation, by the gate, or by the control plane "
    "does not promote it."
)

T = TypeVar("T")


# --------------------------------------------------------------------------
# The result type (R4)
# --------------------------------------------------------------------------


class ContractViolation(ValueError):
    """A value this interface forbids was constructed. Never recovered from.

    Raised at construction time rather than checked by the caller, because the
    failure R4 names is silent: an empty result that reads as a successful
    observation of nothing. A caller that could have checked would not have
    known to.
    """


class FailureKind(Enum):
    """Closed vocabulary for *why* a call did not produce a value.

    Provider-neutral by construction: these classify Interlock's relationship
    to the provider, not the provider's own words for its own states. A
    provider's raw text belongs in :attr:`Failure.provider_detail`, never
    folded into one of these names.
    """

    #: The provider could not be reached, or answered in a way not parseable.
    BACKEND_UNREACHABLE = "backend-unreachable"
    #: The capability / version probe says this provider is not usable (D-0010).
    INCOMPATIBLE_PROVIDER = "incompatible-provider"
    #: The named session is not one this provider knows about.
    UNKNOWN_SESSION = "unknown-session"
    #: The provider refused the operation (permissions, its own preconditions).
    REFUSED_BY_PROVIDER = "refused-by-provider"
    #: The call did not complete within the caller's bound.
    TIMED_OUT = "timed-out"
    #: The provider answered, but not in a shape this interface can interpret.
    UNINTERPRETABLE_RESPONSE = "uninterpretable-response"


@dataclass(frozen=True)
class Ok(Generic[T]):
    """A call that produced a value. The value is always present.

    ``Ok(None)`` is a :class:`ContractViolation`: "succeeded, and here is
    nothing" is the shape R4 forbids. An *empty collection* is still a legal
    value -- ``Ok(())`` from :meth:`SessionProvider.list_sessions` means the
    provider was reached and holds zero sessions, which is a fact, not a
    failure. The distinction is the whole point: emptiness is only ever
    expressible as an explicit success carrying an empty collection, and can
    never be the representation of a failure, because failures are a different
    type that cannot be constructed without a reason.
    """

    value: T

    def __post_init__(self) -> None:
        if self.value is None:
            raise ContractViolation(
                "Ok(None) is forbidden (R4): a call that produced nothing is a "
                "Failure with a reason, not an empty success"
            )


@dataclass(frozen=True)
class Failure:
    """A call that did not produce a value, and always says why.

    Never empty: :attr:`kind` comes from a closed vocabulary and
    :attr:`detail` must carry text a human can act on. ``provider_detail``
    holds the backend's own words verbatim -- including anything it wrote to
    ``stderr`` -- so that a failure carries the raw evidence forward instead of
    summarising it away.
    """

    kind: FailureKind
    detail: str
    provider_detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FailureKind):
            raise ContractViolation(f"kind must be a FailureKind, got {self.kind!r}")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ContractViolation(
                "Failure.detail must be non-empty (R4): a failure without a "
                "reason is the empty result this interface exists to forbid"
            )


#: What every verb returns. Callers discriminate on the type, not on emptiness.
ProviderResult = Union[Ok[T], Failure]


# --------------------------------------------------------------------------
# The provider-neutral lifecycle / availability readout
# --------------------------------------------------------------------------


class Observation(Enum):
    """Whether the provider's state for a session could be observed at all.

    Two values, and the second is the one that matters: D-0006 requires the
    system to tolerate degraded observation rather than restore fidelity by
    reaching into internals (D-0010), so "could not observe" must be
    representable as a *readout*, not only as a failed call. A child that is
    alive but has emitted nothing parseable is exactly this case, and
    collapsing it into an error or an empty result is the R4 defect again.
    """

    OBSERVED = "observed"
    COULD_NOT_OBSERVE = "could-not-observe"


@dataclass(frozen=True)
class SessionReadout:
    """One session as the provider currently reports it.

    :attr:`provider_state` is the backend's **own** state word, carried
    uninterpreted. This interface neither enumerates nor ranks those words: it
    has no closed set of its own to offer, and inventing one would bake a
    single provider's vocabulary into a provider-neutral contract. Anything
    that needs a judgement -- lifecycle, waiting, finished -- gets it from the
    detector layer, which is versioned and fixture-tested, not from here.
    """

    session_id: str
    observation: Observation
    provider_state: str | None = None
    could_not_observe_reason: str | None = None
    provider_detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ContractViolation("SessionReadout.session_id must be non-empty")
        if not isinstance(self.observation, Observation):
            raise ContractViolation(
                f"observation must be an Observation, got {self.observation!r}"
            )
        if self.observation is Observation.OBSERVED:
            if not isinstance(self.provider_state, str) or not self.provider_state.strip():
                raise ContractViolation(
                    "an observed readout must carry the provider's own state "
                    "string; an observation of nothing is COULD_NOT_OBSERVE"
                )
            if self.could_not_observe_reason is not None:
                raise ContractViolation(
                    "an observed readout must not also carry a reason for not "
                    "observing"
                )
        else:
            if self.provider_state is not None:
                raise ContractViolation(
                    "a readout that could not observe must not carry a provider "
                    "state: an unobserved state is not a state"
                )
            if (
                not isinstance(self.could_not_observe_reason, str)
                or not self.could_not_observe_reason.strip()
            ):
                raise ContractViolation(
                    "COULD_NOT_OBSERVE must say why (R4): a bare could-not-observe "
                    "is indistinguishable from an empty result"
                )


@dataclass(frozen=True)
class StartRequest:
    """What a caller must supply to start a top-level worker session.

    Deliberately minimal and provider-neutral. ``session_id`` is the caller's
    to choose, because an identity assigned by the provider after the spawn
    cannot be committed before it; ``settings`` carries whatever
    per-role configuration the provider takes, opaque to this interface.
    """

    session_id: str
    workspace: str
    role: str
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("session_id", "workspace", "role"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"StartRequest.{name} must be non-empty")


# --------------------------------------------------------------------------
# The capability / version probe and its fail-closed spawn precondition (D-0010)
# --------------------------------------------------------------------------

#: Capabilities a provider must expose *through its public surface* for this
#: interface to be usable. Named after the five verbs plus the structured
#: readout they depend on. D-0010: a capability the public surface does not
#: expose is out of scope or a gate failure -- never a reason to reach into
#: internals.
REQUIRED_CAPABILITIES = frozenset(
    {
        "session.start",
        "session.list",
        "session.read-state",
        "session.stop",
        "session.resume",
        "session.structured-readout",
    }
)


@dataclass(frozen=True)
class CapabilityReport:
    """What a probe of the provider's public surface found.

    :attr:`provider_version` is recorded rather than parsed: version *churn* is
    what D-0010 is defending against, so the report says which build was seen
    and lets the capability set, not a version comparison, decide usability.
    """

    provider_version: str
    supported: frozenset[str]
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider_version, str) or not self.provider_version.strip():
            raise ContractViolation(
                "CapabilityReport.provider_version must be non-empty: an "
                "unidentified provider is an unusable one (D-0010)"
            )
        if not isinstance(self.supported, frozenset):
            raise ContractViolation("CapabilityReport.supported must be a frozenset")

    @property
    def missing(self) -> frozenset[str]:
        """Required capabilities this provider did not report."""

        return frozenset(REQUIRED_CAPABILITIES - self.supported)

    @property
    def compatible(self) -> bool:
        """True only when nothing required is missing. Silence is not consent."""

        return not self.missing


class SpawnRefused(RuntimeError):
    """A new spawn was refused because the provider is not known to be usable.

    D-0010 scopes fail-closed to *new* spawns and says nothing about sessions
    already running; what an incompatible probe implies for those is open
    (``Q-0020``) and this interface does not answer it by implementation.
    """


def check_spawn_precondition(probe: ProviderResult[CapabilityReport] | None) -> CapabilityReport:
    """Refuse a new spawn unless the probe positively says the provider is usable.

    Fail closed means the *absence* of a good answer refuses, so every one of
    these refuses: never probed (``None``), a probe that failed, a probe whose
    result is not one of this interface's two result types, one carrying
    something that is not a :class:`CapabilityReport`, and one that is a report
    but reports something required as missing. Only the last case -- a real
    report with nothing missing -- returns, and it returns the report so the
    caller records which build it committed to.

    The type checks are not belt-and-braces. Annotations are not enforced at
    runtime, so without them a duck-typed object whose ``compatible`` happens
    to be true would spawn, and a malformed result would raise
    ``AttributeError`` -- an exception the caller is not told to expect and may
    handle as an ordinary error. Both are ways for the one precondition D-0010
    puts in the way of a spawn to be stepped over silently.

    Raises:
        SpawnRefused: in every case except a compatible report.
    """

    if probe is None:
        raise SpawnRefused(
            "no capability probe has been run; a spawn is refused rather than "
            "attempted on an unknown provider (D-0010)"
        )
    if isinstance(probe, Failure):
        raise SpawnRefused(
            f"capability probe failed ({probe.kind.value}): {probe.detail}"
        )
    if not isinstance(probe, Ok):
        raise SpawnRefused(
            f"capability probe returned {probe!r}, which is neither Ok nor "
            "Failure; an uninterpretable probe refuses the spawn"
        )
    report = probe.value
    if not isinstance(report, CapabilityReport):
        raise SpawnRefused(
            f"capability probe returned Ok({report!r}), which is not a "
            "CapabilityReport; a spawn is refused rather than trusting it"
        )
    if not report.compatible:
        raise SpawnRefused(
            f"provider {report.provider_version!r} is missing required "
            f"capabilities: {sorted(report.missing)}"
        )
    return report


# --------------------------------------------------------------------------
# Workspace lifecycle: observe, and veto (D-0021's third capability)
# --------------------------------------------------------------------------


class WorkspaceVerdict(Enum):
    """Whether a workspace lifecycle transition may proceed."""

    ALLOW = "allow"
    VETO = "veto"


@dataclass(frozen=True)
class WorkspaceTransition:
    """A workspace lifecycle transition the provider is about to make.

    ``kind`` is the provider's own word for the transition (creating a tree,
    removing one, moving a worker onto another), carried uninterpreted for the
    same reason :class:`SessionReadout` carries provider states uninterpreted.
    """

    session_id: str
    workspace: str
    kind: str
    provider_detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("session_id", "workspace", "kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractViolation(f"WorkspaceTransition.{name} must be non-empty")


@dataclass(frozen=True)
class WorkspaceDecision:
    """A verdict, and -- when it is a veto -- always a reason."""

    verdict: WorkspaceVerdict
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, WorkspaceVerdict):
            raise ContractViolation(f"verdict must be a WorkspaceVerdict, got {self.verdict!r}")
        if not isinstance(self.reason, str):
            raise ContractViolation(
                f"reason must be a string, got {self.reason!r}. Checked before it "
                "is read, so a malformed reason is this interface's own error "
                "rather than an AttributeError from somewhere downstream."
            )
        if self.verdict is WorkspaceVerdict.VETO and not self.reason.strip():
            raise ContractViolation(
                "a veto must say why: an unexplained veto is an empty result "
                "wearing a decision's clothes (R4)"
            )


@runtime_checkable
class WorkspaceLifecycleObserver(Protocol):
    """Anything that wants a say before a workspace transition happens."""

    def on_workspace_transition(self, transition: WorkspaceTransition) -> WorkspaceDecision:
        """Return :class:`WorkspaceDecision`. Raising is treated as a veto."""


# --------------------------------------------------------------------------
# Where the three previously-unassigned capabilities landed (D-0021)
# --------------------------------------------------------------------------

OWNER_MESSAGE_BUS = "MessageBus (D-0009; built as S8, issue #19)"
OWNER_SESSION_PROVIDER = "SessionProvider -- this interface (S1, issue #10)"
OWNER_NEITHER_CONTRACT = "neither contract -- no owner exists"


@dataclass(frozen=True)
class CapabilityAssignment:
    """One capability, its named owner, and whether it lives in this file.

    Owning a capability and putting it in S1 are different things, and the
    difference is the point: message delivery has an owner and is *absent*
    here.
    """

    capability: str
    owner: str
    in_this_interface: bool
    reason: str


#: The three capabilities D-0021 records as belonging to neither contract as
#: written. Each gets a named owner here rather than being settled by inertia.
CAPABILITY_ASSIGNMENTS = (
    CapabilityAssignment(
        capability="deliver a message to a worker (gate item 6)",
        owner=OWNER_MESSAGE_BUS,
        in_this_interface=False,
        reason=(
            "D-0009 separates delivery from session management because v1 bound "
            "them together -- messages travelled as keystrokes into a pane, so a "
            "shadow observer could not watch delivery without stealing it. "
            "Delivery is therefore MessageBus's and is built as S8. What S1 "
            "records is the absence of the verb, which is the property gate "
            "items 6 and 11 exist to check."
        ),
    ),
    CapabilityAssignment(
        capability=(
            "read back a session's effective permission / sandbox / hook "
            "configuration (gate item 3)"
        ),
        owner=OWNER_NEITHER_CONTRACT,
        in_this_interface=False,
        reason=(
            "No public surface returns a session's effective configuration, so "
            "the capability cannot be placed in either contract without "
            "inventing a surface that does not exist -- and D-0010 forbids "
            "reaching into internals to manufacture one. Recorded as unowned. "
            "What exists instead is a deliberate weakening accepted by a human "
            "under D-0023: the permission mode alone has a partial readback via "
            "the provider's own structured startup event, and hooks and sandbox "
            "have only the behavioural breach-probe battery. Both live in "
            "src/claude_org_runtime/fencing/ (S10, issue #9), which narrows the "
            "gap rather than closing it -- diffing our own rendered inputs "
            "proves what we wrote, not what the provider loaded."
        ),
    ),
    CapabilityAssignment(
        capability="observe or veto a workspace lifecycle transition (gate item 7)",
        owner=OWNER_SESSION_PROVIDER,
        in_this_interface=True,
        reason=(
            "Genuinely the provider's: only the party that manages workspaces "
            "can announce a transition before making it. It is carried here as "
            "an observation / veto surface -- not as a sixth verb, since D-0009 "
            "names five. Under the current provider no other party owns the "
            "working tree, so the surface may have no producer at all; it exists "
            "so that a provider with its own supervisor can be adopted without "
            "the capability silently having nowhere to go."
        ),
    ),
)

#: Delivery's absence is a designed property of this interface, not an omission.
DELIVERY_ABSENCE_IS_DELIBERATE = (
    "This interface has no verb that sends anything to a worker. Delivery, ack, "
    "dedup and message identity belong to MessageBus (D-0009) and are built as "
    "S8. Adding a delivery verb here would rebuild the v1 coupling in which "
    "replacing the session backend also changed delivery semantics -- and it "
    "would make gate items 6 and 11 unmeasurable, since what they check is "
    "precisely that no such edge exists."
)

#: D-0009's five verbs, mapped to the public method that renders each one. The
#: mapping is data so that "exactly these five, no more" can be asserted
#: mechanically. ``start`` is public but not abstract: it carries the
#: fail-closed gate and delegates to the abstract ``_start_session``, which is
#: the half an implementation writes (see :data:`VERB_IMPLEMENTATION_HOOKS`).
D0009_VERBS = {
    "start": "start",
    "list": "list_sessions",
    "obtain structured state of": "read_state",
    "stop": "stop",
    "resume": "resume",
}

#: The method a subclass implements for each verb. Identical to
#: :data:`D0009_VERBS` except for ``start``, whose public half is the gate.
VERB_IMPLEMENTATION_HOOKS = dict(D0009_VERBS, start="_start_session")


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------


class SessionProvider(ABC):
    """Start, list, read the state of, stop and resume top-level worker sessions.

    **Provisional (D-0021).** See the module docstring; promotion requires a
    ``D-`` entry.

    Five verbs, and no sixth. :meth:`probe_capabilities` is not a verb -- it is
    the precondition D-0010 requires before any of them may spawn anything, and
    it is abstract because a provider that cannot say what it supports cannot be
    used fail-closed.

    Every verb returns :data:`ProviderResult`: an :class:`Ok` carrying a value,
    or a :class:`Failure` carrying a reason. No verb signals failure by
    returning an empty collection, and none raises to report an ordinary
    provider-side problem; exceptions are reserved for programmer error
    (:class:`ContractViolation`) and for the refused spawn
    (:class:`SpawnRefused`), which is a refusal to act rather than a failed
    action.
    """

    #: Observers registered for workspace lifecycle transitions.
    _workspace_observers: list[WorkspaceLifecycleObserver]

    def __init__(self) -> None:
        self._workspace_observers = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse a subclass that overrides a gate rather than implementing it.

        :meth:`start` carries the fail-closed spawn precondition (D-0010) and
        :meth:`require_spawnable` carries the check itself; a provider that
        replaces either has removed the precondition while still presenting as
        a ``SessionProvider``. Overriding is refused at class-definition time
        rather than caught in review, because the failure is silent at runtime:
        such a provider behaves correctly in every test that does not
        deliberately break the probe.

        The check is on the method the **completed MRO resolves**, not on
        ``cls.__dict__``. ``class P(StartMixin, SessionProvider)`` puts no
        ``start`` in ``P.__dict__`` at all, yet the mixin's ``start`` is the one
        that runs -- so a dict-only check would wave through the very bypass it
        exists to stop, and this one was found by executing it rather than by
        reading the code.
        """

        super().__init_subclass__(**kwargs)
        for gate in ("start", "require_spawnable"):
            canonical = SessionProvider.__dict__[gate]
            resolved = getattr(cls, gate, None)
            resolved = getattr(resolved, "__func__", resolved)
            if resolved is not canonical:
                raise ContractViolation(
                    f"{cls.__name__} resolves {gate}() to {resolved!r} rather "
                    "than the one carrying the fail-closed spawn precondition "
                    "(D-0010) -- whether by overriding it directly or by "
                    "inheriting an override earlier in the MRO. Implement "
                    "_start_session() instead."
                )

    # -- the capability probe and its precondition (D-0010) ----------------

    @abstractmethod
    def probe_capabilities(self) -> ProviderResult[CapabilityReport]:
        """Ask the provider's **public surface** what it is and what it supports.

        Public surface only (D-0010): no internal state directories, no private
        sockets, no unpublished formats. An implementation that cannot answer
        from the public surface returns a :class:`Failure` -- which refuses the
        next spawn -- rather than guessing.
        """

    def require_spawnable(self) -> CapabilityReport:
        """Probe, and refuse to spawn unless the answer says the provider is usable.

        Concrete on purpose: fail-closed is a property of the contract, not of
        each implementation's diligence, so implementations of :meth:`start`
        call this rather than re-deriving it. See
        :func:`check_spawn_precondition` for the four cases.

        Raises:
            SpawnRefused: on an unusable, unreachable or unprobed provider.
        """

        return check_spawn_precondition(self.probe_capabilities())

    # -- the five verbs (D-0009) -------------------------------------------

    def start(self, request: StartRequest) -> ProviderResult[SessionReadout]:
        """Start one top-level worker session. **The verb, and the gate.**

        This method is deliberately *not* the one implementations write. It
        runs :meth:`require_spawnable` first and only then delegates to
        :meth:`_start_session`, so that the provider is never asked to create
        anything on an unprobed, unreachable or incompatible backend (D-0010).

        Making it a helper an implementation is asked to call would have made
        fail-closed a property of each implementation's diligence, which is
        exactly what it must not be: the one implementation that forgets is the
        one that spawns against a provider nobody has checked, and it would
        pass every test that only exercises the happy path.
        :meth:`__init_subclass__` refuses a subclass that overrides this
        method, so the gate cannot be removed by accident either.

        A successful start returns the readout the provider gives for the
        session it just created, which may legitimately be
        :attr:`Observation.COULD_NOT_OBSERVE` -- a session can exist before it
        has said anything about itself.

        Raises:
            SpawnRefused: before the provider is asked to create anything.
        """

        self.require_spawnable()
        return self._start_session(request)

    @abstractmethod
    def _start_session(self, request: StartRequest) -> ProviderResult[SessionReadout]:
        """Create the session. Called by :meth:`start` **after** the gate passes.

        This is the ``start`` verb's implementation half. It may assume the
        capability probe has just succeeded, and must not be called directly by
        anything outside this class -- calling it directly is how a caller
        would spawn past the precondition.
        """

    @abstractmethod
    def list_sessions(self) -> ProviderResult[Sequence[SessionReadout]]:
        """List the sessions this provider currently holds.

        ``Ok(())`` means the provider was reached and holds none. A provider
        that could not be reached returns :class:`Failure` -- the two must stay
        distinguishable (R4), because in v1 they were not.
        """

    @abstractmethod
    def read_state(self, session_id: str) -> ProviderResult[SessionReadout]:
        """Obtain the structured state of one session.

        A session that exists but cannot be read yields ``Ok`` carrying a
        :attr:`Observation.COULD_NOT_OBSERVE` readout with its reason -- not a
        :class:`Failure`, since the call itself succeeded, and not an empty
        value. :class:`Failure` is for a call that did not happen or whose
        answer could not be interpreted.
        """

    @abstractmethod
    def stop(self, session_id: str) -> ProviderResult[SessionReadout]:
        """Stop one session, and report what the provider says about it afterwards.

        The readout is returned rather than a bare acknowledgement because a
        provider's acceptance of a stop is not evidence that the session
        stopped.
        """

    @abstractmethod
    def resume(self, session_id: str) -> ProviderResult[SessionReadout]:
        """Re-enter an existing session after a worker or supervisor restart.

        This interface makes no exclusivity promise for resume: whether the
        provider prevents a second concurrent re-entry is a property of the
        provider, and the control plane's lease -- not this call -- is what
        keeps a run single-writer. An implementation must be correct with any
        provider-side refusal assumed absent.
        """

    # -- workspace lifecycle (not a verb; see CAPABILITY_ASSIGNMENTS) -------

    def register_workspace_observer(self, observer: WorkspaceLifecycleObserver) -> None:
        """Register a party that may veto workspace lifecycle transitions."""

        self._workspace_observers.append(observer)

    def evaluate_workspace_transition(
        self, transition: WorkspaceTransition
    ) -> WorkspaceDecision:
        """Ask every observer, and let any one of them veto.

        Fail closed in the same shape as the spawn precondition: an observer
        that raises, or returns something that is not a
        :class:`WorkspaceDecision`, vetoes. An observer whose own failure let a
        transition through would be worse than no observer, because its
        presence is what the caller is relying on.

        **Every observer is asked, including after a veto has been recorded.**
        The capability this surface carries is *observe or veto* -- an observer
        that keeps its own record of attempted transitions is doing the first
        half, and short-circuiting on the first veto would make what it sees
        depend on the registration order of parties it knows nothing about. The
        first veto is the one returned; later ones are still collected so the
        decision can say how many parties objected.
        """

        first_veto: WorkspaceDecision | None = None
        veto_count = 0
        for observer in self._workspace_observers:
            try:
                decision = observer.on_workspace_transition(transition)
            except Exception as exc:  # noqa: BLE001 - any failure is a veto
                decision = WorkspaceDecision(
                    WorkspaceVerdict.VETO,
                    f"observer {type(observer).__name__} raised {exc!r}",
                )
            if not isinstance(decision, WorkspaceDecision):
                decision = WorkspaceDecision(
                    WorkspaceVerdict.VETO,
                    f"observer {type(observer).__name__} returned {decision!r}, "
                    "which is not a WorkspaceDecision",
                )
            if decision.verdict is WorkspaceVerdict.VETO:
                veto_count += 1
                if first_veto is None:
                    first_veto = decision
        if first_veto is None:
            return WorkspaceDecision(WorkspaceVerdict.ALLOW)
        if veto_count == 1:
            return first_veto
        return WorkspaceDecision(
            WorkspaceVerdict.VETO,
            f"{first_veto.reason} (and {veto_count - 1} further veto(es))",
        )
