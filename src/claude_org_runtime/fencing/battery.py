"""The breach-probe battery -- one forbidden operation per *rule*.

D-0023 part 1 is precise about the unit: "one forbidden operation per **rule**
in the role's fence, not one per role". Per-role probing leaves most rules
unobserved, so a battery that is merely *plausible* is not the observable the
decision asks for.

The battery is therefore **derived**, never authored. :func:`probes_for` walks
the rendered fence and synthesizes one probe per rule from the rule's own
text, and it refuses to return a battery whose synthesized operand the rule
does not actually match. That refusal is the whole point: a hand-maintained
probe list drifts from the fence silently, and the drift is invisible exactly
when a new rule has been added and nobody probed it.

What this battery does and does not prove is stated plainly in
``docs/per-role-fencing.md`` and in the gate record: it observes *behaviour
against the fence Interlock rendered*. It is a deliberate weakening of item 3
accepted by a human, not an equivalent method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .rules import (
    KIND_PERMISSION_DENY,
    KIND_SANDBOX_DENY_READ,
    KIND_SANDBOX_DENY_WRITE,
    Decision,
    Fence,
    FenceRule,
    witness_subject,
)


class ProbeSynthesisError(RuntimeError):
    """A rule for which no matching forbidden operation could be synthesized.

    Fatal by design. Skipping the rule would leave it unprobed while the
    battery still reported full coverage, which is the failure this battery
    exists to make impossible.
    """


@dataclass(frozen=True)
class BreachProbe:
    """One forbidden operation, aimed at exactly one rule."""

    rule_id: str
    tool_name: str
    tool_input: Mapping[str, Any]
    description: str

    def to_json(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "tool_name": self.tool_name,
            "tool_input": dict(self.tool_input),
            "description": self.description,
        }


@dataclass(frozen=True)
class ProbeResult:
    probe: BreachProbe
    decision: Decision

    @property
    def denied(self) -> bool:
        return self.decision.denied

    @property
    def denied_by_its_own_rule(self) -> bool:
        """Denied *by the rule it targets*, not merely denied by something.

        A probe that trips a neighbouring rule would let a broken rule hide
        behind a working one and still show a green battery.
        """

        return self.decision.denied and self.decision.rule_id == self.probe.rule_id


@dataclass(frozen=True)
class BatteryReport:
    role: str
    results: tuple[ProbeResult, ...]

    @property
    def covered_rule_ids(self) -> frozenset[str]:
        return frozenset(result.probe.rule_id for result in self.results)

    @property
    def breaches(self) -> tuple[ProbeResult, ...]:
        return tuple(r for r in self.results if not r.denied_by_its_own_rule)

    @property
    def all_denied(self) -> bool:
        return not self.breaches

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "all_denied": self.all_denied,
            "probes": [
                {
                    **result.probe.to_json(),
                    "denied": result.denied,
                    "denied_by_its_own_rule": result.denied_by_its_own_rule,
                    "decided_by": result.decision.rule_id,
                }
                for result in self.results
            ],
        }


def probe_for(rule: FenceRule) -> BreachProbe:
    """Synthesize the forbidden operation for one rule."""

    subject = witness_subject(rule)
    if rule.kind == KIND_PERMISSION_DENY:
        tool_name = rule.tool
        tool_input = _payload(tool_name, subject)
        description = f"{tool_name} against {subject!r}, denied by {rule.spec!r}"
    elif rule.kind == KIND_SANDBOX_DENY_READ:
        tool_name = "Read"
        tool_input = {"file_path": subject}
        description = f"read of sandbox-denied path {subject!r}"
    elif rule.kind == KIND_SANDBOX_DENY_WRITE:
        tool_name = "Write"
        tool_input = {"file_path": subject, "content": ""}
        description = f"write to sandbox-denied path {subject!r}"
    else:  # pragma: no cover - guarded by rules.FenceRule.matches
        raise ProbeSynthesisError(f"unknown rule kind: {rule.kind}")

    probe = BreachProbe(
        rule_id=rule.rule_id,
        tool_name=tool_name,
        tool_input=tool_input,
        description=description,
    )
    if not rule.matches(probe.tool_name, probe.tool_input):
        raise ProbeSynthesisError(
            f"synthesized operation does not match its own rule: {rule.rule_id} "
            f"vs {probe.tool_input!r}"
        )
    return probe


def probes_for(fence: Fence) -> tuple[BreachProbe, ...]:
    """One probe per rule, in fence order, or an error.

    The returned tuple is the coverage claim: callers assert
    ``{p.rule_id for p in probes} == set(fence.rule_ids())``.
    """

    probes = tuple(probe_for(rule) for rule in fence.rules)
    covered = {probe.rule_id for probe in probes}
    expected = set(fence.rule_ids())
    if covered != expected:  # pragma: no cover - defensive
        missing = sorted(expected - covered)
        raise ProbeSynthesisError(f"rules left unprobed: {missing}")
    return probes


def run_battery(
    fence: Fence,
    *,
    evaluate: Callable[[str, Mapping[str, Any]], Decision] | None = None,
) -> BatteryReport:
    """Run every probe through ``evaluate`` (default: the fence itself).

    ``evaluate`` is injected so the same battery can be pointed at the fence's
    own decision function, at the deny hook as a subprocess, or at a live
    session -- one battery, several observation points, no second probe list to
    keep in step.
    """

    decide = evaluate if evaluate is not None else fence.decide
    results = tuple(
        ProbeResult(probe=probe, decision=decide(probe.tool_name, probe.tool_input))
        for probe in probes_for(fence)
    )
    return BatteryReport(role=fence.role, results=results)


def _payload(tool_name: str, subject: str) -> dict[str, Any]:
    if tool_name == "Bash":
        return {"command": subject}
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        return {"file_path": subject, "content": ""}
    if tool_name == "WebFetch":
        return {"url": subject}
    if tool_name in ("Glob", "Grep"):
        return {"pattern": subject}
    return {"file_path": subject}


def probe_ids(probes: Iterable[BreachProbe]) -> frozenset[str]:
    return frozenset(probe.rule_id for probe in probes)
