"""The mechanical half of S1's acceptance criteria (issue #10).

Four of the criteria are properties of the *file*, not of any implementation,
and each is asserted here rather than trusted to review:

- the file says of itself that it is provisional and that promotion needs a
  ``D-`` entry (D-0021);
- **no fact-state vocabulary appears anywhere in S1** -- and the forbidden set
  is read out of ``DECISIONS.md`` rather than copied here, so that a seventh
  state added by a future ``D-`` entry is covered the day it is written;
- **no delivery verb**, with the absence documented as deliberate (D-0009);
- the typed result can never be constructed as an empty success (R4).

The rest exercise the two behavioural properties the interface itself
implements: the fail-closed spawn precondition (D-0010) and the fail-closed
workspace veto.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from claude_org_runtime.session import provider as s1

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS = REPO_ROOT / "DECISIONS.md"
S1_SOURCE = Path(s1.__file__).read_text(encoding="utf-8")


def _fact_state_names() -> list[str]:
    """Read D-0005's closed set out of DECISIONS.md.

    Copying the six names into this test would make it stale the moment a
    ``D-`` entry adds a seventh -- and D-0005 says such an entry is exactly how
    the set may grow. Parsing fails loudly rather than silently checking
    nothing: an empty or implausible parse is a test failure.
    """

    text = DECISIONS.read_text(encoding="utf-8")
    section = text.split("## D-0005 —", 1)
    assert len(section) == 2, "D-0005 not found in DECISIONS.md"
    body = section[1].split("\n## ", 1)[0]
    names = re.findall(r"^- `([A-Z][A-Z_]+)`$", body, flags=re.MULTILINE)
    assert len(names) >= 6, f"implausible fact-state parse from D-0005: {names}"
    return names


def test_file_marks_itself_provisional_and_names_what_promotes_it():
    """D-0021: provisional *in the file itself*, promoted only by a D- entry."""

    assert s1.PROVISIONAL is True
    assert "D-0021" in s1.PROMOTION_REQUIRES
    assert "D-" in s1.PROMOTION_REQUIRES
    docstring = s1.__doc__ or ""
    assert "provisional" in docstring.lower()
    assert "D-0021" in docstring
    assert "provisional" in (s1.SessionProvider.__doc__ or "").lower()


def test_no_fact_state_vocabulary_appears_anywhere_in_s1():
    """D-0005 / D-0021: conversion belongs to the detector layer, not here.

    Checked against the whole source text, including comments and docstrings,
    and against the prose spelling of each name as well as the token -- writing
    ``observation unavailable`` into a docstring maps the interface onto the
    fact-state set just as surely as importing the constant would.
    """

    offenders = []
    for name in _fact_state_names():
        pattern = re.compile(name.replace("_", "[_ -]"), re.IGNORECASE)
        if pattern.search(S1_SOURCE):
            offenders.append(name)
    assert not offenders, (
        f"fact-state vocabulary in S1: {offenders}. Provider lifecycle is "
        "carried uninterpreted; conversion belongs to the detector layer."
    )


def test_no_delivery_verb_and_the_absence_is_documented():
    """D-0009: delivery is MessageBus's, and S1 records its absence."""

    delivery_words = re.compile(
        r"deliver|send|dispatch|publish|enqueue|message|notify|prompt|keystroke",
        re.IGNORECASE,
    )
    public_methods = [
        name
        for name, _ in inspect.getmembers(s1.SessionProvider, callable)
        if not name.startswith("_")
    ]
    offenders = [name for name in public_methods if delivery_words.search(name)]
    assert not offenders, f"delivery-shaped method on SessionProvider: {offenders}"

    assert "MessageBus" in s1.DELIVERY_ABSENCE_IS_DELIBERATE
    assert "D-0009" in s1.DELIVERY_ABSENCE_IS_DELIBERATE


def test_exactly_the_five_d0009_verbs_and_no_sixth():
    """Five verbs, the probe as a precondition, and nothing else to implement."""

    assert len(s1.D0009_VERBS) == 5
    for verb, method in s1.D0009_VERBS.items():
        assert callable(getattr(s1.SessionProvider, method)), verb
        assert getattr(s1.SessionProvider, method).__doc__, f"{method} has no docstring"

    abstract = set(s1.SessionProvider.__abstractmethods__)
    hooks = set(s1.VERB_IMPLEMENTATION_HOOKS.values())
    assert hooks <= abstract
    assert abstract - hooks == {"probe_capabilities"}
    assert s1.SessionProvider.probe_capabilities.__doc__
    # start is the one verb whose public half is concrete: it is the gate.
    assert "start" not in abstract
    assert s1.VERB_IMPLEMENTATION_HOOKS["start"] == "_start_session"


def test_three_capabilities_each_have_a_named_owner():
    """D-0021: assigned explicitly, including the 'belongs to neither' answer."""

    assignments = s1.CAPABILITY_ASSIGNMENTS
    assert len(assignments) == 3
    owners = {a.owner for a in assignments}
    assert owners == {
        s1.OWNER_MESSAGE_BUS,
        s1.OWNER_NEITHER_CONTRACT,
        s1.OWNER_SESSION_PROVIDER,
    }
    for assignment in assignments:
        assert assignment.capability.strip()
        assert assignment.reason.strip()
    by_owner = {a.owner: a for a in assignments}
    # Owning a capability and putting it in S1 are different things.
    assert by_owner[s1.OWNER_MESSAGE_BUS].in_this_interface is False
    assert by_owner[s1.OWNER_NEITHER_CONTRACT].in_this_interface is False
    assert by_owner[s1.OWNER_SESSION_PROVIDER].in_this_interface is True


# -- R4: the typed result is never an empty one ---------------------------


def test_ok_cannot_carry_nothing():
    with pytest.raises(s1.ContractViolation):
        s1.Ok(None)


def test_ok_may_carry_an_empty_collection():
    """Zero sessions is a fact the provider reported, not a failure."""

    assert s1.Ok(()).value == ()


@pytest.mark.parametrize("detail", ["", "   "])
def test_failure_must_say_why(detail):
    with pytest.raises(s1.ContractViolation):
        s1.Failure(s1.FailureKind.BACKEND_UNREACHABLE, detail)


def test_failure_kind_comes_from_the_closed_vocabulary():
    with pytest.raises(s1.ContractViolation):
        s1.Failure("backend-unreachable", "provider not reachable")


# -- the readout, including its "could not observe" case ------------------


def test_observed_readout_carries_the_providers_own_state_word():
    readout = s1.SessionReadout(
        session_id="s-1", observation=s1.Observation.OBSERVED, provider_state="running"
    )
    assert readout.provider_state == "running"
    assert readout.could_not_observe_reason is None


def test_observed_readout_without_a_state_is_refused():
    with pytest.raises(s1.ContractViolation):
        s1.SessionReadout(session_id="s-1", observation=s1.Observation.OBSERVED)


def test_could_not_observe_must_say_why_and_must_not_invent_a_state():
    with pytest.raises(s1.ContractViolation):
        s1.SessionReadout(session_id="s-1", observation=s1.Observation.COULD_NOT_OBSERVE)
    with pytest.raises(s1.ContractViolation):
        s1.SessionReadout(
            session_id="s-1",
            observation=s1.Observation.COULD_NOT_OBSERVE,
            provider_state="running",
            could_not_observe_reason="no parseable output yet",
        )
    readout = s1.SessionReadout(
        session_id="s-1",
        observation=s1.Observation.COULD_NOT_OBSERVE,
        could_not_observe_reason="child alive, nothing parseable emitted yet",
    )
    assert readout.provider_state is None


# -- D-0010: the capability probe and its fail-closed spawn precondition ---


def _report(**overrides):
    fields = {
        "provider_version": "test-provider 1.0",
        "supported": frozenset(s1.REQUIRED_CAPABILITIES),
    }
    fields.update(overrides)
    return s1.CapabilityReport(**fields)


def test_a_compatible_probe_is_the_only_case_that_permits_a_spawn():
    report = check = s1.check_spawn_precondition(s1.Ok(_report()))
    assert report.compatible and not check.missing


def test_an_unprobed_provider_refuses_the_spawn():
    """Fail closed means the absence of an answer refuses (D-0010)."""

    with pytest.raises(s1.SpawnRefused):
        s1.check_spawn_precondition(None)


def test_a_failed_probe_refuses_the_spawn():
    with pytest.raises(s1.SpawnRefused):
        s1.check_spawn_precondition(
            s1.Failure(s1.FailureKind.BACKEND_UNREACHABLE, "CLI not found")
        )


def test_a_probe_missing_any_required_capability_refuses_the_spawn():
    partial = frozenset(s1.REQUIRED_CAPABILITIES - {"session.resume"})
    with pytest.raises(s1.SpawnRefused) as excinfo:
        s1.check_spawn_precondition(s1.Ok(_report(supported=partial)))
    assert "session.resume" in str(excinfo.value)


def test_an_unidentified_provider_cannot_even_report_capabilities():
    with pytest.raises(s1.ContractViolation):
        _report(provider_version="")


class _Provider(s1.SessionProvider):
    """A provider that does nothing except answer the probe as told."""

    def __init__(self, probe_result):
        super().__init__()
        self._probe_result = probe_result

    def probe_capabilities(self):
        return self._probe_result

    #: Set by :meth:`_start_session` so a test can tell whether the provider was
    #: ever asked to create anything.
    started = False

    def _start_session(self, request):
        type(self).started = True
        return s1.Ok(
            s1.SessionReadout(
                session_id=request.session_id,
                observation=s1.Observation.COULD_NOT_OBSERVE,
                could_not_observe_reason="just created, nothing emitted yet",
            )
        )

    def list_sessions(self):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def read_state(self, session_id):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def stop(self, session_id):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def resume(self, session_id):  # pragma: no cover - not exercised here
        raise NotImplementedError


def test_require_spawnable_is_the_contracts_own_gate_not_each_implementations():
    assert _Provider(s1.Ok(_report())).require_spawnable().compatible
    with pytest.raises(s1.SpawnRefused):
        _Provider(
            s1.Failure(s1.FailureKind.INCOMPATIBLE_PROVIDER, "unknown build")
        ).require_spawnable()


# -- the workspace lifecycle surface (item 7's capability) ----------------


def _transition():
    return s1.WorkspaceTransition(session_id="s-1", workspace="/w/one", kind="remove-tree")


def test_no_observers_allows_the_transition():
    decision = _Provider(s1.Ok(_report())).evaluate_workspace_transition(_transition())
    assert decision.verdict is s1.WorkspaceVerdict.ALLOW


def test_any_observer_may_veto_and_a_veto_always_says_why():
    provider = _Provider(s1.Ok(_report()))

    class _Vetoer:
        def on_workspace_transition(self, transition):
            return s1.WorkspaceDecision(s1.WorkspaceVerdict.VETO, "unsaved artifacts present")

    provider.register_workspace_observer(_Vetoer())
    decision = provider.evaluate_workspace_transition(_transition())
    assert decision.verdict is s1.WorkspaceVerdict.VETO
    assert "unsaved" in decision.reason

    with pytest.raises(s1.ContractViolation):
        s1.WorkspaceDecision(s1.WorkspaceVerdict.VETO)


@pytest.mark.parametrize("bad", ["raises", "returns-nonsense"])
def test_a_broken_observer_vetoes_rather_than_letting_the_transition_through(bad):
    """An observer whose own failure allowed the transition is worse than none."""

    provider = _Provider(s1.Ok(_report()))

    class _Broken:
        def on_workspace_transition(self, transition):
            if bad == "raises":
                raise RuntimeError("observer blew up")
            return "sure, go ahead"

    provider.register_workspace_observer(_Broken())
    decision = provider.evaluate_workspace_transition(_transition())
    assert decision.verdict is s1.WorkspaceVerdict.VETO
    assert decision.reason


def test_start_is_gated_by_the_base_class_not_by_the_implementation():
    """The provider is never asked to create anything on an unusable backend."""

    class _Refusing(_Provider):
        started = False

    provider = _Refusing(s1.Failure(s1.FailureKind.BACKEND_UNREACHABLE, "CLI not found"))
    with pytest.raises(s1.SpawnRefused):
        provider.start(s1.StartRequest(session_id="s-1", workspace="/w", role="worker"))
    assert _Refusing.started is False, "the spawn happened despite a failed probe"

    class _Usable(_Provider):
        started = False

    result = _Usable(s1.Ok(_report())).start(
        s1.StartRequest(session_id="s-1", workspace="/w", role="worker")
    )
    assert _Usable.started is True
    assert isinstance(result, s1.Ok)


@pytest.mark.parametrize("gate", ["start", "require_spawnable"])
def test_a_subclass_cannot_override_the_gate_away(gate):
    """Removing the precondition is refused at class-definition time."""

    with pytest.raises(s1.ContractViolation):
        type("_Ungated", (_Provider,), {gate: lambda self, *a, **k: None})


@pytest.mark.parametrize(
    "bogus",
    [
        "not a result at all",
        object(),
    ],
)
def test_a_probe_result_that_is_neither_ok_nor_failure_refuses_the_spawn(bogus):
    with pytest.raises(s1.SpawnRefused):
        s1.check_spawn_precondition(bogus)


def test_an_ok_carrying_something_that_is_not_a_report_refuses_the_spawn():
    """A duck-typed stand-in must not spawn just because it says it is compatible."""

    class _LooksCompatible:
        compatible = True
        missing = frozenset()
        provider_version = "impostor 1.0"

    with pytest.raises(s1.SpawnRefused):
        s1.check_spawn_precondition(s1.Ok(_LooksCompatible()))


def test_every_observer_is_asked_even_after_a_veto():
    """The surface carries observation as well as veto; order must not decide."""

    provider = _Provider(s1.Ok(_report()))
    seen = []

    class _Recording:
        def __init__(self, name, verdict):
            self.name = name
            self.verdict = verdict

        def on_workspace_transition(self, transition):
            seen.append(self.name)
            if self.verdict is s1.WorkspaceVerdict.VETO:
                return s1.WorkspaceDecision(s1.WorkspaceVerdict.VETO, f"{self.name} objects")
            return s1.WorkspaceDecision(s1.WorkspaceVerdict.ALLOW)

    provider.register_workspace_observer(_Recording("first", s1.WorkspaceVerdict.VETO))
    provider.register_workspace_observer(_Recording("second", s1.WorkspaceVerdict.ALLOW))
    provider.register_workspace_observer(_Recording("third", s1.WorkspaceVerdict.VETO))

    decision = provider.evaluate_workspace_transition(_transition())
    assert seen == ["first", "second", "third"]
    assert decision.verdict is s1.WorkspaceVerdict.VETO
    assert "first objects" in decision.reason
    assert "1 further veto" in decision.reason


@pytest.mark.parametrize("verdict", list(s1.WorkspaceVerdict))
def test_a_non_string_reason_is_this_interfaces_own_error(verdict):
    with pytest.raises(s1.ContractViolation):
        s1.WorkspaceDecision(verdict, None)
