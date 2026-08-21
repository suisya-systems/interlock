"""A corpus that refuses rather than shrinks, and rates that can be checked by hand.

Three properties get adversarial treatment here, because each of them fails in a
direction that looks like success:

* **A malformed case is refused, not skipped.** Every malformed shape below is
  asserted to raise, and to name the case in the message. A loader that skipped
  them would leave a green suite behind a corpus that quietly got smaller, and
  the only number that would have shown it -- the composition -- would have
  moved the flattering way.
* **A corpus with no negative case is refused at build time.** The reason is
  arithmetic, so the test is arithmetic: a detector that alarms on **every**
  case is run against a positive-only corpus and scores a perfect miss rate.
  That is exactly what :class:`NegativeCasesRequired` exists to make impossible,
  so the test builds the corpus that would let it happen and asserts the refusal.
* **The latency is exact, by construction.** Every instant a "detector" produces
  here comes from the evaluation's own :class:`SyntheticClock`, and an instant
  from anywhere else is asserted to be refused. The numbers are then checked by
  hand: a detection 45 s after a labelled onset is asserted to be 45_000, not
  "close to".

Nothing here re-implements the grading to compare against. Where a test needs a
deadline it states the arithmetic itself (onset + budget, both read from the
label the loader returned) and hands the detector an instant one millisecond on
each side of it; the expected verdict is written down by hand. The shipped
corpus's labels are checked against the **seeded policy rows** rather than
against a copy of themselves, so a label that drifts from the revision it claims
to be under fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import policy
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.fixtures import (
    DETECTED,
    FACT_STATES,
    FALSE_POSITIVE,
    LABEL_FIELDS,
    MISS,
    NONE_CLASS,
    TRUE_NEGATIVE,
    VERDICTS,
    CaseIncomplete,
    ClassDirectoryMismatch,
    ClockNotSynthetic,
    FixtureRefusal,
    IncidentBeforeOnset,
    LabelMalformed,
    NegativeCasesRequired,
    OutcomeMissing,
    PositiveCasesRequired,
    ProducedIncident,
    StrayEntryRefused,
    SyntheticClock,
    TraceMalformed,
    UnknownCaseInOutcomes,
    evaluate,
    load_case,
    load_corpus,
    render_fixture_report,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

#: The note ``0002_policy_seed.sql`` writes. Looked up by note rather than
#: assumed to be revision 1, so this survives a later seed step.
SEED_NOTE = (
    "initial time base: detection latency budgets, gate stage tolerances "
    "and gate stage owners as first decided"
)

#: Where the corpus this branch ships lives. Resolved from this file so the
#: test moves with the tree rather than depending on the working directory.
SHIPPED_CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "labelled"


def positive_label(**overrides: object) -> dict:
    """A well-formed positive label. Tests mutate exactly one field."""

    label = {
        "incident_class": "relay_gap",
        "onset_offset_ms": 30_000,
        "tolerance_ms": 180_000,
        "budget_ms": 300_000,
        "fact_state": "EXPLICIT_BLOCK",
        "must_not_recommend": ["terminate_session"],
        "provenance": "constructed_edge: a synthetic case for the loader tests",
    }
    label.update(overrides)
    return label


def negative_label(**overrides: object) -> dict:
    label = {
        "incident_class": NONE_CLASS,
        "onset_offset_ms": None,
        "tolerance_ms": None,
        "budget_ms": None,
        "fact_state": "OBSERVATION_UNAVAILABLE",
        "must_not_recommend": ["terminate_session"],
        "provenance": "constructed_edge: a synthetic outage for the loader tests",
    }
    label.update(overrides)
    return label


TRACE = [
    {"offset_ms": 0, "kind": "run_started"},
    {"offset_ms": 30_000, "kind": "gate_received"},
    {"offset_ms": 330_000, "kind": "reconcile_pass"},
]


def write_case(
    root: Path,
    class_dir: str,
    name: str,
    *,
    label: dict | str,
    trace: list[dict] | str | None = None,
) -> Path:
    """Write one ``<class>/<case>/`` directory. *label* / *trace* may be raw text."""

    case_path = root / class_dir / name
    case_path.mkdir(parents=True)
    case_path.joinpath("expected.json").write_text(
        label if isinstance(label, str) else json.dumps(label),
        encoding="utf-8",
    )
    if trace is None:
        trace = TRACE
    case_path.joinpath("trace.jsonl").write_text(
        trace
        if isinstance(trace, str)
        else "".join(json.dumps(line) + "\n" for line in trace),
        encoding="utf-8",
    )
    return case_path


RELAY_CASE = "relay_gap/stalled_relay"
OUTAGE_CASE = "observation_unavailable/probe_down"


def outcome_for(evaluation, case_id: str):
    """The graded outcome for one case, looked up by id.

    By id and never by position: :func:`load_corpus` walks the tree in sorted
    order, so ``observation_unavailable`` precedes ``relay_gap`` and an
    index-based test would assert against whichever case happened to sort first.
    """

    for outcome in evaluation.outcomes:
        if outcome.case_id == case_id:
            return outcome
    raise AssertionError(f"no outcome for {case_id}")


def minimal_corpus(root: Path) -> Path:
    """One positive and one negative case -- the smallest loadable corpus."""

    write_case(root, "relay_gap", "stalled_relay", label=positive_label())
    write_case(root, "observation_unavailable", "probe_down", label=negative_label())
    return root


# ---------------------------------------------------------------------------
# The loader refuses every malformed shape, and names the case.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("absent", ["trace.jsonl", "expected.json"])
def test_half_a_case_is_refused(tmp_path: Path, absent: str) -> None:
    """Half a case is not a smaller case; it has no correct outcome at all."""

    case = write_case(tmp_path, "relay_gap", "stalled_relay", label=positive_label())
    case.joinpath(absent).unlink()

    with pytest.raises(CaseIncomplete) as refusal:
        load_case(case)
    assert absent in str(refusal.value)
    assert "stalled_relay" in str(refusal.value)


def test_a_third_file_in_a_case_is_refused(tmp_path: Path) -> None:
    """An input nothing loads means the case is graded against less than it holds."""

    case = write_case(tmp_path, "relay_gap", "stalled_relay", label=positive_label())
    case.joinpath("notes.md").write_text("half the label lives here", encoding="utf-8")

    with pytest.raises(StrayEntryRefused) as refusal:
        load_case(case)
    assert "notes.md" in str(refusal.value)


@pytest.mark.parametrize("field", LABEL_FIELDS)
def test_every_missing_label_field_is_refused(tmp_path: Path, field: str) -> None:
    """All seven of section 3.2's fields, each proved required on its own.

    Parametrised over ``LABEL_FIELDS`` itself rather than over a list retyped
    here: a field added to the table is then covered the moment it is added, and
    a field silently dropped from the constant fails this test.
    """

    label = positive_label()
    del label[field]
    case = write_case(tmp_path, "relay_gap", "stalled_relay", label=label)

    with pytest.raises(LabelMalformed) as refusal:
        load_case(case)
    assert field in str(refusal.value)


def test_an_unknown_label_field_is_refused(tmp_path: Path) -> None:
    """A field nothing reads is a label the grader ignores."""

    case = write_case(
        tmp_path,
        "relay_gap",
        "stalled_relay",
        label=positive_label(severity="high"),
    )
    with pytest.raises(LabelMalformed) as refusal:
        load_case(case)
    assert "severity" in str(refusal.value)


def test_an_unknown_fact_state_is_refused(tmp_path: Path) -> None:
    """D-0005's set is closed; a mistyped state is a fixture nothing can satisfy."""

    case = write_case(
        tmp_path,
        "relay_gap",
        "stalled_relay",
        label=positive_label(fact_state="EXPLICITLY_BLOCKED"),
    )
    with pytest.raises(LabelMalformed) as refusal:
        load_case(case)
    assert "D-0005" in str(refusal.value)
    for state in FACT_STATES:
        assert state in str(refusal.value)


def test_free_text_provenance_is_refused(tmp_path: Path) -> None:
    """The field answers "did this happen or did we imagine it", and must be countable."""

    case = write_case(
        tmp_path,
        "relay_gap",
        "stalled_relay",
        label=positive_label(provenance="seemed like a good idea"),
    )
    with pytest.raises(LabelMalformed):
        load_case(case)

    # The same field with a known kind and free detail after a colon loads.
    ok = write_case(
        tmp_path,
        "relay_gap",
        "from_an_accident",
        label=positive_label(provenance="accident: incident of 2026-08-02"),
    )
    assert load_case(ok).expected.provenance.startswith("accident")


def test_must_not_recommend_must_be_a_list_of_strings(tmp_path: Path) -> None:
    """An empty list is allowed and says so; a bare string is not a list of one."""

    case = write_case(
        tmp_path,
        "relay_gap",
        "stalled_relay",
        label=positive_label(must_not_recommend="terminate_session"),
    )
    with pytest.raises(LabelMalformed):
        load_case(case)

    empty = write_case(
        tmp_path,
        "relay_gap",
        "nothing_forbidden",
        label=positive_label(must_not_recommend=[]),
    )
    assert load_case(empty).expected.must_not_recommend == ()


@pytest.mark.parametrize(
    "field", ["onset_offset_ms", "tolerance_ms", "budget_ms"]
)
def test_a_negative_case_may_not_carry_a_window(tmp_path: Path, field: str) -> None:
    """A ``none`` case has no condition, so no state entry and no budget.

    A window on a negative case would suggest a false positive counts only
    inside it; an alarm on a healthy worker is wrong at every offset.
    """

    case = write_case(
        tmp_path,
        "observation_unavailable",
        "probe_down",
        label=negative_label(**{field: 60_000}),
    )
    with pytest.raises(LabelMalformed) as refusal:
        load_case(case)
    assert field in str(refusal.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"onset_offset_ms": None},
        {"budget_ms": None},
        {"onset_offset_ms": -1},
        {"budget_ms": 0},
        {"onset_offset_ms": "30000"},
        {"onset_offset_ms": True},
    ],
)
def test_a_positive_case_needs_real_numbers(tmp_path: Path, overrides: dict) -> None:
    """A positive case with no usable onset or budget can never be graded."""

    case = write_case(
        tmp_path, "relay_gap", "stalled_relay", label=positive_label(**overrides)
    )
    with pytest.raises(LabelMalformed):
        load_case(case)


def test_tolerance_may_not_exceed_the_budget(tmp_path: Path) -> None:
    """T is part of L, not a head start on it (time-base-policy.md section 3.1)."""

    case = write_case(
        tmp_path,
        "relay_gap",
        "stalled_relay",
        label=positive_label(tolerance_ms=300_001, budget_ms=300_000),
    )
    with pytest.raises(LabelMalformed) as refusal:
        load_case(case)
    assert "3.1" in str(refusal.value)


@pytest.mark.parametrize(
    "trace",
    [
        "not json at all\n",
        '{"kind": "gate_received"}\n',
        '{"offset_ms": "30000", "kind": "gate_received"}\n',
        '{"offset_ms": 30000}\n',
        '{"offset_ms": 30000, "kind": "gate_received"}\n{"offset_ms": 10, "kind": "x"}\n',
        "\n\n",
    ],
    ids=[
        "not_json",
        "no_offset",
        "offset_not_int",
        "no_kind",
        "offsets_go_backwards",
        "no_observations",
    ],
)
def test_every_malformed_trace_is_refused(tmp_path: Path, trace: str) -> None:
    """Refused, never skipped: a skipped case is a corpus that silently shrank."""

    case = write_case(
        tmp_path, "relay_gap", "stalled_relay", label=positive_label(), trace=trace
    )
    with pytest.raises(TraceMalformed) as refusal:
        load_case(case)
    assert "stalled_relay" in str(refusal.value)


def test_a_malformed_label_json_is_refused(tmp_path: Path) -> None:
    case = write_case(tmp_path, "relay_gap", "stalled_relay", label="{oops")
    with pytest.raises(LabelMalformed):
        load_case(case)


def test_trace_extras_are_carried_unread(tmp_path: Path) -> None:
    """The observation vocabulary belongs to the detectors, not to the grader."""

    case = write_case(
        tmp_path,
        "relay_gap",
        "stalled_relay",
        label=positive_label(),
        trace=[{"offset_ms": 0, "kind": "gate_received", "gate_id": "g-1", "n": 3}],
    )
    observation = load_case(case).observations[0]
    assert observation.fields == {"gate_id": "g-1", "n": 3}
    assert observation.offset_ms == 0


def test_a_positive_case_filed_under_another_class_is_refused(tmp_path: Path) -> None:
    """The composition table groups by directory, so a misfiled case lies in it."""

    case = write_case(
        tmp_path,
        "consumer_backlog",
        "stalled_relay",
        label=positive_label(incident_class="relay_gap"),
    )
    with pytest.raises(ClassDirectoryMismatch) as refusal:
        load_case(case)
    assert "consumer_backlog" in str(refusal.value)
    assert "relay_gap" in str(refusal.value)


def test_a_negative_case_may_sit_under_any_class(tmp_path: Path) -> None:
    """The directory of a ``none`` case names the detector it is aimed at.

    "An outage that must not raise ``relay_gap``" is a real and necessary
    fixture, so the class-directory rule is one-sided on purpose.
    """

    case = write_case(tmp_path, "relay_gap", "outage_here", label=negative_label())
    assert load_case(case).case_id == "relay_gap/outage_here"


# ---------------------------------------------------------------------------
# The corpus as a whole.
# ---------------------------------------------------------------------------


def test_a_corpus_with_no_negative_case_is_refused(tmp_path: Path) -> None:
    """The refusal is the only thing standing between AC-10 and a loud detector."""

    write_case(tmp_path, "relay_gap", "stalled_relay", label=positive_label())
    write_case(tmp_path, "consumer_backlog", "backed_up", label=positive_label(
        incident_class="consumer_backlog"
    ))

    with pytest.raises(NegativeCasesRequired) as refusal:
        load_corpus(tmp_path)
    assert "D-0006" in str(refusal.value)


def test_the_refused_positive_only_corpus_would_have_scored_perfectly(
    tmp_path: Path,
) -> None:
    """Why the refusal is a refusal: the arithmetic, run.

    A detector that raises **every** class on **every** case is the thing a
    corpus is supposed to catch. Graded over positives alone it has a miss rate
    of zero -- a perfect score -- which is why the corpus that would let it
    happen must not be loadable at all. The same detector, once one negative
    case exists, is a false positive.
    """

    write_case(tmp_path, "relay_gap", "stalled_relay", label=positive_label())
    with pytest.raises(NegativeCasesRequired):
        load_corpus(tmp_path)

    write_case(tmp_path, "observation_unavailable", "probe_down", label=negative_label())
    corpus = load_corpus(tmp_path)

    clock = SyntheticClock(T0)
    alarms_on_everything = {
        case.case_id: (
            ProducedIncident(
                incident_class="relay_gap",
                fact_state="EXPLICIT_BLOCK",
                created_at_ms=clock.at(60_000),
            ),
        )
        for case in corpus.cases
    }
    evaluation = evaluate(corpus, clock=clock, outcomes=alarms_on_everything)

    assert evaluation.miss_rate() == 0.0  # perfect, and meaningless on its own
    assert evaluation.false_positive_rate() == 1.0  # what the negative case says


def test_a_corpus_with_no_positive_case_is_refused(tmp_path: Path) -> None:
    """A corpus that cannot express a miss is not AC-10's ground truth either."""

    write_case(tmp_path, "observation_unavailable", "probe_down", label=negative_label())
    with pytest.raises(PositiveCasesRequired):
        load_corpus(tmp_path)


def test_readmes_are_ignored_and_other_strays_are_refused(tmp_path: Path) -> None:
    minimal_corpus(tmp_path)
    tmp_path.joinpath("README.md").write_text("how to read this", encoding="utf-8")
    tmp_path.joinpath("relay_gap", "README.md").write_text("notes", encoding="utf-8")
    assert load_corpus(tmp_path).composition()["total"] == 2

    tmp_path.joinpath("relay_gap", "leftover.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(StrayEntryRefused) as refusal:
        load_corpus(tmp_path)
    assert "leftover.jsonl" in str(refusal.value)


def test_the_digest_is_over_content_not_counts(tmp_path: Path) -> None:
    """Editing one label changes every number a report prints and no count at all."""

    minimal_corpus(tmp_path)
    before = load_corpus(tmp_path)
    assert load_corpus(tmp_path).content_digest == before.content_digest

    tmp_path.joinpath(
        "relay_gap", "stalled_relay", "expected.json"
    ).write_text(json.dumps(positive_label(budget_ms=600_000)), encoding="utf-8")
    after = load_corpus(tmp_path)

    assert after.composition() == before.composition()  # the count did not move
    assert after.content_digest != before.content_digest  # the content did


def test_an_unknown_case_id_is_refused(tmp_path: Path) -> None:
    corpus = load_corpus(minimal_corpus(tmp_path))
    with pytest.raises(FixtureRefusal):
        corpus.case("relay_gap/no_such_case")


# ---------------------------------------------------------------------------
# The evaluator: a miss, a false positive and an exact latency, by hand.
# ---------------------------------------------------------------------------


def test_a_detection_inside_the_budget_has_an_exact_latency(tmp_path: Path) -> None:
    """Onset at +30 s, alarm at +75 s: the latency is 45_000, not "about 45 s"."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    evaluation = evaluate(
        corpus,
        clock=clock,
        outcomes={
            "relay_gap/stalled_relay": (
                ProducedIncident(
                    incident_class="relay_gap",
                    fact_state="EXPLICIT_BLOCK",
                    created_at_ms=clock.at(75_000),
                ),
            ),
            "observation_unavailable/probe_down": (),
        },
    )

    detected = outcome_for(evaluation, RELAY_CASE)
    assert detected.verdict == DETECTED
    assert detected.latency_ms == 45_000
    assert detected.deadline_ms == T0 + 30_000 + 300_000
    assert evaluation.latencies_ms() == (45_000,)
    assert evaluation.miss_rate() == 0.0


def test_an_alarm_at_the_onset_instant_is_a_zero_latency_detection(
    tmp_path: Path,
) -> None:
    """The other end of the same boundary: exactly at onset is detected, latency 0.

    A detector that alarms the instant the condition begins is the best possible
    outcome, and the refusal for a *negative* latency must not swallow it -- the
    two are one millisecond apart and one is a defect in the ground truth while
    the other is a perfect detection.
    """

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    evaluation = evaluate(
        corpus,
        clock=clock,
        outcomes={
            RELAY_CASE: (
                ProducedIncident(
                    incident_class="relay_gap",
                    fact_state="EXPLICIT_BLOCK",
                    created_at_ms=clock.at(30_000),  # the labelled onset itself
                ),
            ),
            OUTAGE_CASE: (),
        },
    )
    assert outcome_for(evaluation, RELAY_CASE).verdict == DETECTED
    assert outcome_for(evaluation, RELAY_CASE).latency_ms == 0


def test_the_deadline_is_the_last_instant_that_counts(tmp_path: Path) -> None:
    """Onset + budget exactly is detected; one millisecond later is a miss.

    The two runs differ by a single millisecond, which is the only way to catch
    a ``<=`` written as a ``<``. The late run also proves a late alarm is
    recorded as *late* rather than as silence: the two failures have nothing in
    common and the report must not conflate them.
    """

    corpus = load_corpus(minimal_corpus(tmp_path))
    label = corpus.case("relay_gap/stalled_relay").expected
    deadline_offset = label.onset_offset_ms + label.budget_ms
    assert deadline_offset == 330_000  # stated by hand, not computed twice

    on_time_clock = SyntheticClock(T0)
    on_time = evaluate(
        corpus,
        clock=on_time_clock,
        outcomes={
            "relay_gap/stalled_relay": (
                ProducedIncident(
                    incident_class="relay_gap",
                    fact_state="EXPLICIT_BLOCK",
                    created_at_ms=on_time_clock.at(deadline_offset),
                ),
            ),
            "observation_unavailable/probe_down": (),
        },
    )
    assert outcome_for(on_time, RELAY_CASE).verdict == DETECTED
    assert outcome_for(on_time, RELAY_CASE).latency_ms == 300_000

    late_clock = SyntheticClock(T0)
    late = evaluate(
        corpus,
        clock=late_clock,
        outcomes={
            "relay_gap/stalled_relay": (
                ProducedIncident(
                    incident_class="relay_gap",
                    fact_state="EXPLICIT_BLOCK",
                    created_at_ms=late_clock.at(deadline_offset + 1),
                ),
            ),
            "observation_unavailable/probe_down": (),
        },
    )
    missed = outcome_for(late, RELAY_CASE)
    assert missed.verdict == MISS
    assert missed.latency_ms is None
    assert missed.late_latency_ms == 300_001
    assert late.miss_rate() == 1.0
    assert late.latencies_ms() == ()


def test_silence_is_a_miss_with_no_late_alarm(tmp_path: Path) -> None:
    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    evaluation = evaluate(
        corpus,
        clock=clock,
        outcomes={
            "relay_gap/stalled_relay": (),
            "observation_unavailable/probe_down": (),
        },
    )
    assert outcome_for(evaluation, RELAY_CASE).verdict == MISS
    assert outcome_for(evaluation, RELAY_CASE).late_latency_ms is None
    assert evaluation.counts() == {
        DETECTED: 0,
        MISS: 1,
        FALSE_POSITIVE: 0,
        TRUE_NEGATIVE: 1,
    }
    assert set(evaluation.counts()) == set(VERDICTS)


def test_an_alarm_of_the_wrong_class_does_not_detect_the_case(tmp_path: Path) -> None:
    """A detector that raises the wrong alarm loudly has still missed."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    evaluation = evaluate(
        corpus,
        clock=clock,
        outcomes={
            "relay_gap/stalled_relay": (
                ProducedIncident(
                    incident_class="consumer_backlog",
                    fact_state="EXPLICIT_BLOCK",
                    created_at_ms=clock.at(60_000),
                ),
            ),
            "observation_unavailable/probe_down": (),
        },
    )
    outcome = outcome_for(evaluation, RELAY_CASE)
    assert outcome.verdict == MISS
    assert outcome.other_class_incidents == ("consumer_backlog",)


def test_the_right_class_with_the_wrong_fact_is_detected_and_recorded(
    tmp_path: Path,
) -> None:
    """Section 3.2 matches on class, so this is a detection -- and it is not silent."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    evaluation = evaluate(
        corpus,
        clock=clock,
        outcomes={
            "relay_gap/stalled_relay": (
                ProducedIncident(
                    incident_class="relay_gap",
                    fact_state="NO_ACTIVITY_EVIDENCE",
                    created_at_ms=clock.at(60_000),
                ),
            ),
            "observation_unavailable/probe_down": (),
        },
    )
    outcome = outcome_for(evaluation, RELAY_CASE)
    assert outcome.verdict == DETECTED
    assert outcome.fact_state_mismatches == ("NO_ACTIVITY_EVIDENCE",)


def test_the_earliest_matching_alarm_is_the_detection(tmp_path: Path) -> None:
    """A second incident for one condition is a re-notification, not the latency."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    evaluation = evaluate(
        corpus,
        clock=clock,
        outcomes={
            "relay_gap/stalled_relay": (
                ProducedIncident(
                    incident_class="relay_gap",
                    fact_state="EXPLICIT_BLOCK",
                    created_at_ms=clock.at(200_000),
                ),
                ProducedIncident(
                    incident_class="relay_gap",
                    fact_state="EXPLICIT_BLOCK",
                    created_at_ms=clock.at(90_000),
                ),
            ),
            "observation_unavailable/probe_down": (),
        },
    )
    assert outcome_for(evaluation, RELAY_CASE).latency_ms == 60_000
    assert outcome_for(evaluation, RELAY_CASE).matching_incidents == 2


def test_an_alarm_before_its_own_onset_is_refused(tmp_path: Path) -> None:
    """A negative latency means the label or the attribution is wrong; both are defects."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    with pytest.raises(IncidentBeforeOnset) as refusal:
        evaluate(
            corpus,
            clock=clock,
            outcomes={
                "relay_gap/stalled_relay": (
                    ProducedIncident(
                        incident_class="relay_gap",
                        fact_state="EXPLICIT_BLOCK",
                        created_at_ms=clock.at(29_999),
                    ),
                ),
                "observation_unavailable/probe_down": (),
            },
        )
    assert "relay_gap/stalled_relay" in str(refusal.value)


def test_a_stall_alarm_on_an_outage_is_the_false_positive(tmp_path: Path) -> None:
    """AC-3, in both directions, over the same negative case.

    An incident carrying the labelled ``OBSERVATION_UNAVAILABLE`` fact is the
    **required** output and must not be graded as a false positive; the same
    trace read as a stall is one. A grader that demanded a negative case produce
    no row at all would fail a detector for obeying AC-3.
    """

    corpus = load_corpus(minimal_corpus(tmp_path))

    conforming_clock = SyntheticClock(T0)
    conforming = evaluate(
        corpus,
        clock=conforming_clock,
        outcomes={
            "relay_gap/stalled_relay": (),
            "observation_unavailable/probe_down": (
                ProducedIncident(
                    incident_class="observation_unavailable",
                    fact_state="OBSERVATION_UNAVAILABLE",
                    created_at_ms=conforming_clock.at(190_000),
                ),
            ),
        },
    )
    assert conforming.ids_for(TRUE_NEGATIVE) == ("observation_unavailable/probe_down",)
    assert conforming.false_positive_rate() == 0.0

    wrong_clock = SyntheticClock(T0)
    wrong = evaluate(
        corpus,
        clock=wrong_clock,
        outcomes={
            "relay_gap/stalled_relay": (),
            "observation_unavailable/probe_down": (
                ProducedIncident(
                    incident_class="session_no_evidence",
                    fact_state="NO_ACTIVITY_EVIDENCE",
                    created_at_ms=wrong_clock.at(190_000),
                ),
            ),
        },
    )
    assert wrong.ids_for(FALSE_POSITIVE) == ("observation_unavailable/probe_down",)
    assert wrong.false_positive_rate() == 1.0
    assert outcome_for(wrong, OUTAGE_CASE).fact_state_mismatches == ("NO_ACTIVITY_EVIDENCE",)


def test_a_forbidden_recommendation_counts_only_when_applied(tmp_path: Path) -> None:
    """Section 3.4: the count is at the applied effect, not at the recommendation."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    evaluation = evaluate(
        corpus,
        clock=clock,
        outcomes={
            "relay_gap/stalled_relay": (),
            "observation_unavailable/probe_down": (
                ProducedIncident(
                    incident_class="observation_unavailable",
                    fact_state="OBSERVATION_UNAVAILABLE",
                    created_at_ms=clock.at(190_000),
                    applied_recommendations=("terminate_session",),
                ),
                ProducedIncident(
                    incident_class="observation_unavailable",
                    fact_state="OBSERVATION_UNAVAILABLE",
                    created_at_ms=clock.at(200_000),
                    applied_recommendations=("notify_secretary",),
                ),
            ),
        },
    )
    assert evaluation.forbidden_applied() == (
        ("observation_unavailable/probe_down", "terminate_session"),
    )
    # The conforming fact keeps the verdict a true negative: the harm is the
    # applied action, and it is reported as its own series rather than folded
    # into the false-positive rate.
    assert outcome_for(evaluation, OUTAGE_CASE).verdict == TRUE_NEGATIVE


def test_an_instant_the_clock_did_not_mint_is_refused(tmp_path: Path) -> None:
    """The synthetic clock is structural: a wall-clock stamp cannot pass quietly."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    with pytest.raises(ClockNotSynthetic) as refusal:
        evaluate(
            corpus,
            clock=clock,
            outcomes={
                "relay_gap/stalled_relay": (
                    ProducedIncident(
                        incident_class="relay_gap",
                        fact_state="EXPLICIT_BLOCK",
                        created_at_ms=T0 + 75_123,  # arithmetic, not the clock
                    ),
                ),
                "observation_unavailable/probe_down": (),
            },
        )
    assert "3.2" in str(refusal.value)


def test_one_cases_minting_cannot_vouch_for_another(tmp_path: Path) -> None:
    """Grading mints onsets and deadlines, so every instant is checked first.

    The negative case's stamp here is the *positive* case's deadline instant. If
    the check ran interleaved with grading, grading case one would mint that
    instant and case two's foreign stamp would pass. All instants are validated
    before any case is graded, so it does not.
    """

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    with pytest.raises(ClockNotSynthetic) as refusal:
        evaluate(
            corpus,
            clock=clock,
            outcomes={
                "relay_gap/stalled_relay": (),
                "observation_unavailable/probe_down": (
                    ProducedIncident(
                        incident_class="session_no_evidence",
                        fact_state="NO_ACTIVITY_EVIDENCE",
                        created_at_ms=T0 + 330_000,
                    ),
                ),
            },
        )
    assert "observation_unavailable/probe_down" in str(refusal.value)


def test_a_case_with_no_outcome_is_refused(tmp_path: Path) -> None:
    """An absent entry must not read as "the detector produced nothing"."""

    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    with pytest.raises(OutcomeMissing) as refusal:
        evaluate(
            corpus,
            clock=clock,
            outcomes={"relay_gap/stalled_relay": ()},
        )
    assert "observation_unavailable/probe_down" in str(refusal.value)


def test_an_outcome_for_an_unknown_case_is_refused(tmp_path: Path) -> None:
    corpus = load_corpus(minimal_corpus(tmp_path))
    clock = SyntheticClock(T0)
    with pytest.raises(UnknownCaseInOutcomes) as refusal:
        evaluate(
            corpus,
            clock=clock,
            outcomes={
                "relay_gap/stalled_relay": (),
                "observation_unavailable/probe_down": (),
                "relay_gap/renamed_yesterday": (),
            },
        )
    assert "relay_gap/renamed_yesterday" in str(refusal.value)


def test_the_synthetic_clock_refuses_an_offset_before_t0() -> None:
    clock = SyntheticClock(T0)
    with pytest.raises(Exception):
        clock.at(-1)
    assert clock.minted(clock.at(0)) is True
    assert clock.minted(T0 + 1) is False


# ---------------------------------------------------------------------------
# The corpus this branch ships.
# ---------------------------------------------------------------------------


def test_the_shipped_corpus_loads_and_reports_its_composition() -> None:
    """It loads, it has negatives, and the composition is what the report prints."""

    corpus = load_corpus(SHIPPED_CORPUS)
    composition = corpus.composition()

    assert composition["total"] == len(corpus.cases)
    assert composition["positive"] >= 1
    assert composition["negative"] >= 2  # a stall negative and an outage negative
    assert composition["positive"] + composition["negative"] == composition["total"]
    assert "relay_gap/escalation_received_never_presented" in {
        case.case_id for case in corpus.cases
    }

    negatives = {case.expected.fact_state for case in corpus.negatives()}
    # D-0006's two non-anomalies, both present: an unobservable worker and a
    # quiet one. A corpus missing either lets the detector that alarms on it
    # through.
    assert {"OBSERVATION_UNAVAILABLE", "NO_ACTIVITY_EVIDENCE"} <= negatives

    terminate_cases = [
        case
        for case in corpus.cases
        if any(
            "terminate" in recommendation
            for recommendation in case.expected.must_not_recommend
        )
    ]
    assert terminate_cases, "at least one case must forbid terminating its subject"


def test_every_shipped_onset_is_an_observation_in_its_own_trace() -> None:
    """The onset is the state entry, which means it is a moment the trace contains.

    This is the check that catches the labelling error section 3.2 warns about:
    a label whose onset is the *tolerance crossing* is a number computed from
    ``T``, and it would not in general coincide with any observation. Requiring
    it to be an offset the trace actually holds keeps the label anchored to the
    condition beginning rather than to the budget.
    """

    for case in load_corpus(SHIPPED_CORPUS).positives():
        offsets = {observation.offset_ms for observation in case.observations}
        assert case.expected.onset_offset_ms in offsets, case.case_id
        # And it is not the crossing: onset + T is a different instant.
        assert case.expected.tolerance_ms > 0, case.case_id


def test_shipped_labels_match_the_policy_revision_they_claim(tmp_path: Path) -> None:
    """T and L in a label are the seeded revision's numbers, not invented ones.

    The label carries copies on purpose (``D-0031``: a past case is recomputed
    under the numbers it was judged by), and a copy nobody checks is a copy that
    drifts. Every absolute-budget class among the shipped positives is compared
    against ``policy_detection_latency`` at the seed revision here.
    """

    database = tmp_path / "control_plane.sqlite3"
    connection = create_production_control_plane(database, now_ms=T0)
    try:
        revision_id = connection.execute(
            "SELECT revision_id FROM policy_revision WHERE note = ?", (SEED_NOTE,)
        ).fetchone()[0]
        checked = 0
        for case in load_corpus(SHIPPED_CORPUS).positives():
            row = policy.detection_latency(
                connection,
                revision_id=revision_id,
                incident_class=case.expected.incident_class,
            )
            if row["budget_kind"] != "absolute_ms":
                continue  # relative classes need a subject; not a shipped case yet
            assert case.expected.budget_ms == row["budget_ms"], case.case_id
            if row["threshold_kind"] == "absolute_ms":
                assert case.expected.tolerance_ms == row["threshold_value"], (
                    case.case_id
                )
            checked += 1
        assert checked >= 1
    finally:
        connection.close()


def test_the_report_is_ascii_and_prints_composition_beside_both_rates() -> None:
    """One table, because that coupling is the measurement (section 3.2).

    ASCII is asserted by encoding to cp932: a single em-dash here crashes
    ``--help`` on a Japanese console and no UTF-8 pytest capture would notice.
    """

    corpus = load_corpus(SHIPPED_CORPUS)
    clock = SyntheticClock(T0)
    outcomes: dict[str, tuple[ProducedIncident, ...]] = {}
    for case in corpus.cases:
        if case.is_negative:
            outcomes[case.case_id] = ()
        else:
            outcomes[case.case_id] = (
                ProducedIncident(
                    incident_class=case.expected.incident_class,
                    fact_state=case.expected.fact_state,
                    created_at_ms=clock.at(case.expected.onset_offset_ms + 45_000),
                ),
            )
    text = render_fixture_report(evaluate(corpus, clock=clock, outcomes=outcomes))

    text.encode("cp932")  # raises UnicodeEncodeError on an em-dash
    text.encode("ascii")
    assert "positive cases" in text
    assert "negative cases" in text
    assert "miss rate" in text
    assert "fp rate" in text
    assert "median 45000 ms" in text
    assert corpus.content_digest in text
