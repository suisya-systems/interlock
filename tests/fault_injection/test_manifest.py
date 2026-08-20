"""The matrix is an enumeration, and the enumeration is checked.

Design section 4. Issue ``#15``'s wording ("the same seed hits the same point")
reads as if the seed selected injection points; it does not, and these tests are
where that is nailed down. The seed's authority is payload and schedule only.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

import pytest

from tests.fault_injection import contract, manifest as manifest_module
from tests.fault_injection.contract import ContractViolation
from tests.fault_injection.controller import repro_line


def test_the_generator_reproduces_the_frozen_matrix_exactly() -> None:
    """No generation at collection time (design 4.2).

    A helper may *produce* candidate products, but the manifest is the frozen
    literal. Adding or pruning a case is therefore always an explicit, reviewable
    diff and never a side effect of an enumeration change -- which is what stops
    a reordering from silently changing what every seed means.

    Regenerate after an intentional change with::

        python -c "import json,sys; sys.path[:0]=['.','src']; \\
          from tests.fault_injection import manifest as m; \\
          m.MANIFEST_PATH.write_text(json.dumps(m.build_manifest(), indent=2, \\
          sort_keys=True) + chr(10), encoding='utf-8')"
    """

    frozen = json.loads(manifest_module.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert frozen == manifest_module.build_manifest()


def test_the_frozen_matrix_validates() -> None:
    manifest_module.validate_manifest(manifest_module.load_manifest())


def test_every_case_id_is_unique_and_parses_back_to_its_classification() -> None:
    """``case_id`` is the re-run key, the manifest key and the report key."""

    manifest = manifest_module.load_manifest()
    ids = [case["case_id"] for case in manifest["cases"]]
    assert len(ids) == len(set(ids))
    for case in manifest["cases"]:
        segments = case["case_id"].split("__")
        assert segments[0] == "+".join(case["targets"])
        assert segments[1] == case["operation"]
        assert segments[2] == case["checkpoint"]
        assert segments[3] == case["fault"]
        assert (segments[4] if len(segments) > 4 else None) == case["variant"]
        # The seed is never part of the identity (design 4.1).
        assert "seed" not in case["case_id"]


def test_the_seed_set_covers_every_fault_kind_checkpoint_and_lane() -> None:
    """What S9 ships: at least one case per fault kind, per checkpoint, per lane.

    Populating the full ``ACCEPTANCE.md`` section 2 matrix is I-11's deliverable,
    on this schema -- so this asserts the seed set, not the matrix.
    """

    manifest = manifest_module.load_manifest()
    assert {case["fault"] for case in manifest["cases"]} == set(contract.FAULT_KINDS)
    assert set(contract.CHECKPOINTS) <= {case["checkpoint"] for case in manifest["cases"]}
    assert {case["lane"] for case in manifest["cases"]} == set(contract.LANES)


def test_every_role_is_killed_at_every_mandated_window() -> None:
    """Gate item 4: each of the three components, separately, at each window."""

    manifest = manifest_module.load_manifest()
    singles = {
        (case["targets"][0], case["checkpoint"])
        for case in manifest["cases"]
        if len(case["targets"]) == 1 and case["fault"] == "sigkill"
    }
    for role in contract.ROLES:
        for checkpoint in contract.CHECKPOINTS:
            assert (role, checkpoint) in singles, (role, checkpoint)


#: Every injection ``ACCEPTANCE.md`` section 2's table names, mapped to the fault
#: kind that discharges it. This is the table read as a checklist: if a row of
#: the acceptance surface has no case, the matrix is incomplete and the build
#: says so by name rather than by a count.
_SECTION_2_INJECTIONS: dict[str, tuple[str, ...]] = {
    "lease": (
        "sigkill-expire",   # kill the lease holder without release
        "sigstop-expire",   # expire a lease while its holder is paused, and return it
        "clock-fwd",        # skew the clock forward across the expiry boundary
        "clock-back",       # ... and backward
    ),
    "outbox-resend": (
        "drop-delivery",          # drop the delivery
        "sigkill",                # kill the sender around the write and the delivery
        "recipient-unavailable",  # hold the recipient unavailable across several attempts
    ),
    "ack": (
        "lost-ack",  # lose the ack in flight
        "dup-ack",   # duplicate the ack
        "late-ack",  # deliver the ack after the sender has restarted
        "re-ack",    # ack an already-acked message
    ),
    "dedup": (
        "dup-delivery",     # deliver the same message twice, restarting in between
        "incident-repeat",  # raise the same incident condition repeatedly
        "incident-replay",  # replay a persisted incident packet
    ),
    "single-writer": (
        "writer-race",          # two writers race for the same state item
        "sigstop-expire",       # a partitioned writer returns after its lease expired
        "resumed-writer-race",  # a resumed process and its replacement
    ),
    "observation-outage": (
        "observation-outage",  # the observation path fails or returns nothing
    ),
}


def test_every_acceptance_section_2_injection_has_a_case() -> None:
    """The matrix is the table, row by row (Issue ``#16``, gate item 5).

    Gate item 5 passes only if *every* case is automated and reproducible. The
    counting tests above check that the seed set is well formed; this one checks
    the thing the gate actually asks about -- that each injection the acceptance
    surface names by phrase has a case behind it.
    """

    manifest = manifest_module.load_manifest()
    present = {case["fault"] for case in manifest["cases"]}
    for row, injections in _SECTION_2_INJECTIONS.items():
        missing = [fault for fault in injections if fault not in present]
        assert not missing, f"ACCEPTANCE.md section 2 row {row!r} has no case for {missing}"


def test_the_incident_row_parameterises_q_0002_rather_than_answering_it() -> None:
    """Both halves of Q-0002, covered rather than chosen (ACCEPTANCE.md §2).

    The dedup row says the Issue fixes the incident *fields* and not the
    semantics: whether a repeat increments the retry count on the existing
    incident or opens a linked one is unresolved, "as is the re-notification
    window in absolute time -- both are Q-0002", and "tests must parameterise
    both rather than hard-code either".

    So the matrix runs both rules and more than one window, and one case's
    raises fall *outside* its declared window -- without that, the window would
    be a parameter that never changes an outcome, which is indistinguishable
    from a hard-coded one.
    """

    manifest = manifest_module.load_manifest()
    incident_cases = [
        case
        for case in manifest["cases"]
        if case["fault"] in manifest_module.INCIDENT_FAULTS
    ]
    assert incident_cases
    rules = {case["incident_params"]["collapse"] for case in incident_cases}
    assert rules == set(manifest_module.COLLAPSE_RULES)
    windows = {case["incident_params"]["renotify_window_ms"] for case in incident_cases}
    assert len(windows) >= 2
    assert any(
        case["incident_params"]["expect_collapse"] is False for case in incident_cases
    )
    # Q-0003 is a different question and no case settles it.
    assert all(
        case["incident_params"]["reconcile_interval_ms"] is None
        for case in incident_cases
    )
    # The dedup key is case data, and the cases do not all spell it one way --
    # Q-0002 asks what composes it, and a matrix whose keys were all one shape
    # would have answered that by inertia.
    keys = {case["incident_params"]["dedup_key"] for case in incident_cases}
    assert len({key.count("/") for key in keys}) > 1


def test_the_observation_row_asserts_one_fact_state_per_injection() -> None:
    """D-0006 is about a distinction, so a disjunction would not test it.

    A read that *fails* and a read that *returns nothing* are different facts
    about the world, and collapsing them is the defect D-0006 exists to forbid.
    Each observation case therefore declares one mode, and each mode names
    exactly one fact state.
    """

    manifest = manifest_module.load_manifest()
    cases = [case for case in manifest["cases"] if case["fault"] == "observation-outage"]
    assert cases, "the observation-outage row has no case"
    modes = {case["observation"]["mode"] for case in cases}
    # Both injections the row names, not just the one that is easier to build.
    assert modes == {contract.OBSERVATION_UNREADABLE, contract.OBSERVATION_SILENT}
    for case in cases:
        mode = case["observation"]["mode"]
        assert contract.OBSERVATION_FACT_STATES[mode] in contract.FACT_STATES
        # The case asks for the escalation it must not get. Without that, "no
        # recommendation was produced" would pass on a driver that has no
        # escalation path at all.
        assert case["observation"]["escalate_on"] == [
            contract.OBSERVATION_FACT_STATES[mode]
        ]
        assert contract.INVARIANT_NO_ANOMALY_ESCALATION in case["expected"]["queries"]


def test_every_named_invariant_is_reachable_from_some_case() -> None:
    """A name with no case behind it is vocabulary, not coverage.

    The controller now refuses an invariant name it has no assertion for, which
    catches the opposite mistake. This catches this one.
    """

    manifest = manifest_module.load_manifest()
    used = {name for case in manifest["cases"] for name in case["expected"]["queries"]}
    used |= {name for case in manifest["cases"] for name in case["expected"]["destination"]}
    unreachable = sorted(set(contract.INVARIANT_NAMES) - used - _NOT_YET_ASSERTED)
    assert not unreachable, f"no case asserts {unreachable}"


#: Invariants whose cases are still to come. ``incident-collapse`` belongs to
#: the Q-0002 parameterisation, which is a ruling this task deliberately does
#: not take on its own (see the manifest's own note); its query, its parameters
#: and its assertion are in place so the cases are a manifest diff.
_NOT_YET_ASSERTED = frozenset({contract.INVARIANT_INCIDENT_COLLAPSE})


def test_the_fast_profile_still_covers_every_fault_kind() -> None:
    """Design section 9 defines the smoke subset as "one per fault kind".

    Adding kinds without adding fast cases would quietly redefine the PR lane
    into a subset that no longer smoke-tests what the matrix injects.
    """

    manifest = manifest_module.load_manifest()
    fast = {case["fault"] for case in manifest["cases"] if "fast" in case["profiles"]}
    # Except the one kind design section 9 excludes by name: the smoke subset is
    # "singles only, no staggered", and the assertion above this file's budget
    # test enforces that exclusion from the other side.
    wanted = set(contract.FAULT_KINDS) - {"staggered-sigkill"}
    missing = sorted(wanted - fast)
    assert not missing, f"the fast profile smoke-tests no {missing} case"


def test_the_combination_subsets_are_covered() -> None:
    """"In combination" is enumerated, not implied (design 5)."""

    manifest = manifest_module.load_manifest()
    combinations = {
        tuple(case["targets"])
        for case in manifest["cases"]
        if len(case["targets"]) > 1
    }
    assert ("sup", "disp") in combinations
    assert ("disp", "sec") in combinations
    assert ("sup", "sec") in combinations
    assert ("sup", "disp", "sec") in combinations


def test_the_pruning_rule_is_recorded_in_the_header() -> None:
    """Scale is controlled by policy, not by product; what is pruned is listed."""

    manifest = manifest_module.load_manifest()
    assert manifest["pruning_rule"].strip()
    assert "cross-product" in manifest["pruning_rule"]


# ---------------------------------------------------------------------------
# the seed -- design 4.3
# ---------------------------------------------------------------------------

def test_the_per_case_seed_is_order_and_platform_independent() -> None:
    """Adding a case does not shift any other case's stream.

    Derived by sha256 over ``manifest_version || case_id || suite_seed``, so
    Python's hash randomisation and OS differences are irrelevant by
    construction -- there is no ``hash()`` anywhere in the derivation.
    """

    first = contract.case_seed(manifest_version=1, case_id="a__b__c__d", suite_seed=7)
    again = contract.case_seed(manifest_version=1, case_id="a__b__c__d", suite_seed=7)
    assert first == again
    # Pinned: the derivation is part of the contract, so a change to it is a
    # contract-version bump and not a quiet re-shuffle of every case's stream.
    assert first == 0x574FF7BDD408EA49

    # A different case, a different manifest version and a different suite seed
    # each give a different stream, and none of them disturbs the others.
    assert first != contract.case_seed(
        manifest_version=1, case_id="a__b__c__e", suite_seed=7
    )
    assert first != contract.case_seed(
        manifest_version=2, case_id="a__b__c__d", suite_seed=7
    )
    assert first != contract.case_seed(
        manifest_version=1, case_id="a__b__c__d", suite_seed=8
    )


def test_the_seed_never_appears_in_a_cases_identity() -> None:
    """The seed selects payload and schedule; the manifest selects everything else."""

    manifest = manifest_module.load_manifest()
    for case in manifest["cases"]:
        assert "suite_seed" not in case
        assert "seed" not in case


def test_the_reproduction_line_carries_everything_a_re_run_needs() -> None:
    line = repro_line(
        case_id="disp__attempt__before_durable_write__sigkill",
        suite_seed=99,
        manifest_version=manifest_module.MANIFEST_VERSION,
        resolved_skew_ms=31_000,
    )
    assert line.startswith("S9-REPRO ")
    for field in ("case_id=", "suite_seed=", "manifest_version=", "contract_version=", "resolved_skew_ms="):
        assert field in line


# ---------------------------------------------------------------------------
# validation refuses what the design says it must refuse
# ---------------------------------------------------------------------------

def _a_case() -> dict:
    return copy.deepcopy(manifest_module.load_manifest()["cases"][0])


def test_a_barrier_that_cannot_be_reached_is_refused_at_collection() -> None:
    """Never a timeout in CI (design 3.1).

    ``enqueue`` has no after-effect window -- it has no effect -- so arming one
    is a manifest error, and it is caught before any process is spawned.
    """

    case = _a_case()
    case["arms"] = {case["targets"][0]: ["enqueue@after_effect_before_record:1"]}
    with pytest.raises(ContractViolation, match="has no after_effect_before_record window"):
        manifest_module.validate_case(case)


def test_an_effect_window_case_must_name_a_destination_assertion() -> None:
    """``ACCEPTANCE.md`` section 2: our own rows are not enough there."""

    case = _a_case()
    case["checkpoint"] = contract.CHECKPOINT_AFTER_EFFECT_BEFORE_RECORD
    case["expected"]["destination"] = []
    with pytest.raises(ContractViolation, match="name a destination assertion"):
        manifest_module.validate_case(case)


def test_a_restarting_case_must_name_its_recovery_owner() -> None:
    """"Somebody recovered it" is not an assertion (design 5)."""

    case = _a_case()
    case["restart_after"] = True
    case["expected"]["recovery_owner"] = None
    with pytest.raises(ContractViolation, match="names the role whose recovery"):
        manifest_module.validate_case(case)


def test_a_same_role_skew_observed_in_flight_is_invalid_by_construction() -> None:
    """An in-flight call captured its ``now_ms`` at the call boundary (design 7)."""

    case = _a_case()
    case["skew"] = {"role": case["targets"][0], "direction": "backward", "observation": "in-flight"}
    with pytest.raises(ContractViolation, match="next operation"):
        manifest_module.validate_case(case)


def test_a_sigstop_case_off_the_linux_lane_is_refused() -> None:
    case = _a_case()
    case["fault"] = "sigstop-expire"
    case["lane"] = contract.LANE_PORTABLE
    with pytest.raises(ContractViolation, match="Linux-lane only"):
        manifest_module.validate_case(case)


def test_a_duplicate_case_id_fails_the_run_before_any_case_executes() -> None:
    manifest = manifest_module.load_manifest()
    manifest["cases"] = list(manifest["cases"]) + [copy.deepcopy(manifest["cases"][0])]
    with pytest.raises(ContractViolation, match="duplicate case_id"):
        manifest_module.validate_manifest(manifest)


def test_growth_past_a_profile_budget_fails_collection() -> None:
    """CI creep has to become an explicit budget diff (design 9)."""

    manifest = manifest_module.load_manifest()
    manifest["profiles"]["full"] = dict(manifest["profiles"]["full"], max_cases=1)
    with pytest.raises(ContractViolation, match="over its 1-case budget"):
        manifest_module.validate_manifest(manifest)


def test_a_manifest_targeting_another_contract_version_is_refused() -> None:
    manifest = manifest_module.load_manifest()
    manifest["contract_version"] = contract.FAULT_RUNNER_CONTRACT_VERSION + 1
    with pytest.raises(ContractViolation, match="fault-runner contract"):
        manifest_module.validate_manifest(manifest)


# ---------------------------------------------------------------------------
# the budgets, as numbers (design 9)
# ---------------------------------------------------------------------------

def test_the_profiles_carry_the_budgets_the_watchdogs_enforce() -> None:
    """These are harness engineering parameters, not acceptance thresholds.

    They are revisable by an ordinary reviewed diff and require no ``D-`` entry.
    Reading one *as* gate evidence would be a ruling, and goes to the secretary.
    """

    manifest = manifest_module.load_manifest()
    fast = manifest["profiles"]["fast"]
    full = manifest["profiles"]["full"]
    assert fast["max_cases"] == 25 and fast["per_case_timeout_s"] == 15
    assert fast["suite_timeout_s"] == 240
    assert full["max_cases"] == 200 and full["per_case_timeout_s"] == 30
    assert full["combination_case_timeout_s"] == 60
    assert full["suite_timeout_s"] == 1500

    fast_cases = [case for case in manifest["cases"] if "fast" in case["profiles"]]
    assert fast_cases, "the fast profile is the smoke subset, not an empty set"
    assert len(fast_cases) <= fast["max_cases"]
    # The fast profile is singles only and carries no staggered case: the 9-job
    # PR matrix never pays for the full matrix.
    assert all(len(case["targets"]) == 1 for case in fast_cases)
    assert all(case["fault"] != "staggered-sigkill" for case in fast_cases)


def test_the_off_linux_add_on_stays_inside_its_own_budget() -> None:
    manifest = manifest_module.load_manifest()
    portable = [case for case in manifest["cases"] if case["lane"] == contract.LANE_PORTABLE]
    assert len(portable) <= 20
    # Nothing signal-shaped runs on the portable lane (design 8.1).
    assert all(case["fault"] != "sigstop-expire" for case in portable)


def test_the_clock_programme_is_symbolic_and_resolves_against_the_lease_geometry() -> None:
    """Skew magnitudes are boundary-relative, not raw numbers (design 7)."""

    manifest = manifest_module.load_manifest()
    ttl = manifest["ttl_ms"]
    guard = manifest["clock_guard_ms"]
    assert contract.resolve_skew_ms("forward", ttl_ms=ttl, elapsed_ms=0) == ttl + guard
    assert contract.resolve_skew_ms("backward", ttl_ms=ttl, elapsed_ms=ttl) == -(ttl + guard)
    with pytest.raises(ContractViolation, match="never a raw millisecond count"):
        contract.resolve_skew_ms("42ms", ttl_ms=ttl, elapsed_ms=0)

    for case in manifest["cases"]:
        for programme in (case["skew"], case["claimant"]):
            if programme and "direction" in programme:
                assert programme["direction"] in ("forward", "backward")
