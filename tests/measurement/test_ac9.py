"""AC-9's numerator taken apart: the unit, the imputations, and the four figures.

``docs/measurement-harness.md`` sections 2.2 and 2.4 describe a measurement that
can be wrong in several directions while looking entirely reasonable, so the
cases here are built to *separate* those directions rather than to exercise the
happy path. A cohort where invocations, model responses and attempts all happen
to be equal would pass under every wrong reading of the numerator, which is the
defect and not the fixture.

Three of the tests are the load-bearing ones:

* the numerator test builds a population where the three candidate units differ
  and asserts the exact value of each, so counting the wrong one cannot be green;
* the bound test constructs data whose *true* token total is known to the test
  and unknown to the harness, and asserts the ordering
  ``bounded <= true < sensitivity`` -- the p95 imputation sitting **below** the
  truth is precisely the failure section 2.4 says was made on the first pass,
  and a test that only checked "sensitivity is printed" would not see it. The
  true figure is obtained by measuring a second database through the same
  function rather than by arithmetic pasted into the test;
* the verdict test greps the module's own rendered output, so a pass/fail string
  added anywhere in it fails here rather than at a design review.

Every invocation is written through :mod:`...control_plane.ai_invocation`, never
by hand-written SQL: a test that inserts its own rows is testing a copy of the
writer's rules, and the placeholder ``model_response_count`` this suite cares
about is the writer's behaviour and not the DDL's.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.ai_invocation import (
    ProviderUsage,
    complete_invocation,
    start_invocation,
)
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement.ac9 import (
    KIND_ASSUMPTION,
    KIND_LOWER_BOUND,
    OUTPUT_TOKEN_REDUCTION_TARGET,
    PROMPT_REDUCTION_TARGET,
    V1_MEASURED_BASELINE,
    Ac9Report,
    BaselineRefused,
    MeasuredBaseline,
    UnknownUsageStatusInLedgerRefused,
    measure_ac9,
    render_ac9_report,
)
from claude_org_runtime.measurement.cohort import select_cohort
from claude_org_runtime.measurement.reader import open_for_measurement

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS
NOW = PERIOD_END + 1

ADAPTER = "anthropic-adapter/3"
PROVIDER = "anthropic"
MODEL = "some-model"
CAP = 1_024


# --------------------------------------------------------------------------
# helpers -- the smallest legal surroundings an invocation needs
# --------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    connection = create_production_control_plane(path, now_ms=T0)
    connection.close()
    return path


def writer(path: Path) -> sqlite3.Connection:
    """An ordinary writable connection, deliberately separate from the harness's.

    The measurement handle cannot write, which is the point of it; every row
    these tests need therefore arrives through a second connection that can.
    """

    return sqlite3.connect(path, isolation_level=None)


def add_cohort_run(cp: sqlite3.Connection, run_id: str) -> str:
    """A run whose entire lifetime lies inside the period: cohort membership."""

    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
        " VALUES (?, 'completed', ?, ?)",
        (run_id, PERIOD_START + 1, PERIOD_START + 2),
    )
    return run_id


def add_incident(cp: sqlite3.Connection, incident_id: str, run_id: str) -> str:
    cp.execute(
        """
        INSERT INTO incident (incident_id, run_id, session_id, fact_state,
                              detector_version, dedup_key, created_at_ms,
                              updated_at_ms)
        VALUES (?, ?, NULL, 'stalled', 'd1', ?, ?, ?)
        """,
        (incident_id, run_id, f"dedup/{incident_id}", T0, T0),
    )
    return incident_id


def invoke(
    cp: sqlite3.Connection,
    invocation_id: str,
    *,
    run_id: str,
    incident_id: str | None,
    usage_status: str = "reported",
    output_tokens: int | None = None,
    input_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    max_output_tokens: int | None = CAP,
    model_response_count: int = 1,
    attempt_count: int = 1,
    finish: bool = True,
) -> str:
    """One invocation, written through the real writer.

    ``finish=False`` leaves the row as :func:`start_invocation` wrote it --
    ``finished_at_ms IS NULL`` and the request-time placeholder
    ``model_response_count = 1`` -- which is the shape the report has to itemise
    rather than impute.
    """

    start_invocation(
        cp,
        invocation_id=invocation_id,
        provider=PROVIDER,
        model=MODEL,
        adapter_version=ADAPTER,
        started_at_ms=PERIOD_START + 10,
        incident_id=incident_id,
        run_id=run_id,
        max_output_tokens=max_output_tokens,
    )
    if not finish:
        return invocation_id
    if usage_status == "reported":
        usage = ProviderUsage.reported(
            adapter_version=ADAPTER,
            output_tokens=output_tokens,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
        )
    elif usage_status == "partial":
        usage = ProviderUsage.partial(
            adapter_version=ADAPTER,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
        )
    else:
        usage = ProviderUsage.unavailable(adapter_version=ADAPTER)
    complete_invocation(
        cp,
        invocation_id=invocation_id,
        usage=usage,
        model_response_count=model_response_count,
        attempt_count=attempt_count,
        finished_at_ms=PERIOD_START + 20,
    )
    return invocation_id


def measure(path: Path, **kwargs) -> Ac9Report:
    """Select the cohort and measure it, through the read-only handle."""

    connection = open_for_measurement(path)
    try:
        cohort = select_cohort(
            connection,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=NOW,
        )
        return measure_ac9(connection, cohort, now_ms=NOW, **kwargs)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# section 2.2 -- the unit, wrong in both directions
# --------------------------------------------------------------------------


def test_the_numerator_sums_model_responses_not_invocations_and_not_attempts(db):
    # The three candidate units are made to differ on purpose. Counting the
    # invocation overstates the reduction by the tool-use factor; counting
    # attempts reports a flaky network as AI workload; only the response count
    # is on the same basis as the baseline's 3,531 model responses.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        # one plain turn, one three-tool-round-trip invocation, one 429 + retry
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=10)
        invoke(
            cp,
            "inv-2",
            run_id="run-1",
            incident_id="inc-1",
            output_tokens=40,
            model_response_count=4,
        )
        invoke(
            cp,
            "inv-3",
            run_id="run-1",
            incident_id="inc-1",
            output_tokens=10,
            attempt_count=2,
        )
    finally:
        cp.close()

    report = measure(db)

    assert report.model_response_total == 6  # 1 + 4 + 1
    assert report.invocation_count == 3  # the AC-1 series, not the numerator
    assert report.attempt_total == 4  # 1 + 1 + 2, and in no numerator
    # The three are genuinely distinct here, so a wrong unit cannot coincide
    # with the right one and pass.
    assert len({report.model_response_total, report.invocation_count, report.attempt_total}) == 3


def test_a_retry_adds_an_attempt_and_no_assistant_turn(db):
    # Section 2.2: a 429 followed by a successful retry produced ONE assistant
    # turn. attempt_count is the transport axis and stops there.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(
            cp,
            "inv-1",
            run_id="run-1",
            incident_id="inc-1",
            output_tokens=10,
            attempt_count=7,
        )
    finally:
        cp.close()

    report = measure(db)

    assert report.attempt_total == 7
    assert report.model_response_total == 1
    # And the prompt figure is normalised from the responses, not the attempts:
    # one response over one cohort run is 100 per 100 runs, not 700.
    assert report.model_responses_per_100_runs == 100.0


def test_an_invocation_with_no_incident_is_itemised_as_an_ac1_violation(db):
    # AC-1 is "zero AI turns absent incidents". A count would say the assertion
    # broke and nothing about where; the id is the evidence (section 2.2).
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-ok", run_id="run-1", incident_id="inc-1", output_tokens=10)
        invoke(cp, "inv-orphan", run_id="run-1", incident_id=None, output_tokens=10)
    finally:
        cp.close()

    report = measure(db)

    assert report.ac1_violations == ("inv-orphan",)
    # Still counted: a violation is not excused from the numerator, or AC-9
    # would improve every time AC-1 broke.
    assert report.model_response_total == 2
    assert "inv-orphan" in render_ac9_report(report)


def test_invocations_of_runs_outside_the_cohort_are_not_measured(db):
    # The denominator and the numerator must count the same runs, which is the
    # whole argument of section 2.1.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-in")
        cp.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
            " VALUES ('run-out', 'running', ?, ?)",
            (PERIOD_START + 1, PERIOD_START + 2),
        )
        add_incident(cp, "inc-1", "run-in")
        invoke(cp, "inv-in", run_id="run-in", incident_id="inc-1", output_tokens=10)
        invoke(cp, "inv-out", run_id="run-out", incident_id="inc-1", output_tokens=999)
    finally:
        cp.close()

    report = measure(db)

    assert report.cohort_size == 1
    assert report.invocation_count == 1
    assert report.observed_output_tokens == 10


def test_an_invocation_naming_no_run_is_counted_apart_and_enters_no_rate(db):
    # It cannot be attributed to a run cohort, so it is in no rate -- but "the
    # AI ran and we could not say for which run" is evidence, not an absence.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=10)
        invoke(cp, "inv-loose", run_id=None, incident_id="inc-1", output_tokens=500)
    finally:
        cp.close()

    report = measure(db)

    assert report.unattributed_invocations == 1
    assert report.invocation_count == 1
    assert report.observed_output_tokens == 10


# --------------------------------------------------------------------------
# section 2.4 -- coverage, and why a missing figure is never zero
# --------------------------------------------------------------------------


def test_coverage_is_both_counts_and_a_percentage(db):
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=10)
        invoke(cp, "inv-2", run_id="run-1", incident_id="inc-1", output_tokens=10)
        invoke(cp, "inv-3", run_id="run-1", incident_id="inc-1", usage_status="unavailable")
        invoke(cp, "inv-4", run_id="run-1", incident_id="inc-1", usage_status="partial")
    finally:
        cp.close()

    report = measure(db)

    assert (report.covered_count, report.invocation_count) == (2, 4)
    assert report.coverage_ratio == 0.5
    rendered = render_ac9_report(report)
    assert "2 of 4 invocations" in rendered
    assert "50.00 percent" in rendered


def test_a_missing_usage_record_is_never_counted_as_zero_tokens(db):
    # Treating the missing row as 0 would leave bounded == observed. It is
    # imputed at the caller's own ceiling times the turns the invocation made.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
        invoke(
            cp,
            "inv-2",
            run_id="run-1",
            incident_id="inc-1",
            usage_status="unavailable",
            max_output_tokens=500,
            model_response_count=2,
        )
    finally:
        cp.close()

    report = measure(db)

    assert report.observed_output_tokens == 100
    assert report.bounded_output_tokens == 100 + 500 * 2
    assert report.bounded_output_tokens != report.observed_output_tokens
    # A larger token total is a SMALLER reduction: the bound is on the safe side.
    assert report.bounded_reduction < report.observed_reduction


def test_the_ceiling_is_per_request_so_a_multi_turn_missing_row_is_imputed_at_the_product(db):
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(
            cp,
            "inv-1",
            run_id="run-1",
            incident_id="inc-1",
            usage_status="partial",
            max_output_tokens=300,
            model_response_count=4,
        )
    finally:
        cp.close()

    report = measure(db)

    # Not 300: a four-turn invocation was allowed 300 output tokens per request.
    assert report.bounded_output_tokens == 1_200


def test_the_bounded_figure_bounds_the_truth_where_the_p95_does_not(db, tmp_path):
    # Section 2.4's first-pass error, made concrete. The covered sample is a
    # cluster of small responses; the invocation whose usage was lost is a large
    # one -- which is the correlation the section names, telemetry loss going
    # with exactly the long, truncated responses. The p95 of the covered sample
    # therefore lands BELOW the true value and the "conservative" sensitivity
    # figure reports a reduction better than the truth.
    def populate(path: Path, *, lost: bool) -> None:
        cp = writer(path)
        try:
            add_cohort_run(cp, "run-1")
            add_incident(cp, "inc-1", "run-1")
            for index in range(10):
                invoke(
                    cp,
                    f"inv-small-{index}",
                    run_id="run-1",
                    incident_id="inc-1",
                    output_tokens=100,
                )
            if lost:
                invoke(
                    cp,
                    "inv-large",
                    run_id="run-1",
                    incident_id="inc-1",
                    usage_status="unavailable",
                    max_output_tokens=8_000,
                )
            else:
                invoke(
                    cp,
                    "inv-large",
                    run_id="run-1",
                    incident_id="inc-1",
                    output_tokens=5_000,
                    max_output_tokens=8_000,
                )
        finally:
            cp.close()

    populate(db, lost=True)
    lost = measure(db)

    # The truth is measured by the same code over the same population with the
    # usage record present, rather than by arithmetic copied into the test.
    truth_path = tmp_path / "truth.sqlite3"
    create_production_control_plane(truth_path, now_ms=T0).close()
    populate(truth_path, lost=False)
    truth = measure(truth_path)
    assert truth.coverage_is_complete

    assert lost.covered_p95_output_tokens == 100  # the small cluster
    assert lost.sensitivity_output_tokens < truth.observed_output_tokens
    assert lost.bounded_output_tokens > truth.observed_output_tokens

    # The ordering that matters: the bound never claims a better reduction than
    # the truth, and the sensitivity figure does.
    assert lost.bounded_reduction <= truth.observed_reduction
    assert lost.sensitivity_reduction > truth.observed_reduction


def test_the_p95_is_the_nearest_rank_observed_value(db):
    # Nearest rank returns a value some invocation actually exhibited; an
    # interpolating definition would add a second assumption on top of the one
    # the sensitivity figure already is.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        for index in range(20):
            invoke(
                cp,
                f"inv-{index:02d}",
                run_id="run-1",
                incident_id="inc-1",
                output_tokens=index + 1,
            )
        invoke(cp, "inv-missing", run_id="run-1", incident_id="inc-1", usage_status="unavailable")
    finally:
        cp.close()

    report = measure(db)

    # ceil(0.95 * 20) = 19, so the 19th of 1..20 ascending.
    assert report.covered_p95_output_tokens == 19


def test_an_unbounded_missing_row_disqualifies_the_acceptance_claim(db):
    # No ceiling was recorded at request time and no usage record arrived, so
    # there is nothing this row can honestly be bounded at.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
        invoke(
            cp,
            "inv-uncapped",
            run_id="run-1",
            incident_id="inc-1",
            usage_status="unavailable",
            max_output_tokens=None,
        )
    finally:
        cp.close()

    report = measure(db)

    assert report.unbounded_missing == ("inv-uncapped",)
    assert report.supports_acceptance_claim is False
    # Nothing was invented for it: the bounded total is the covered total.
    assert report.bounded_output_tokens == report.observed_output_tokens
    rendered = render_ac9_report(report)
    assert "CANNOT support an AC-9 acceptance claim" in rendered
    assert "inv-uncapped" in rendered


def test_an_unfinished_row_is_itemised_rather_than_imputed_at_the_placeholder(db):
    # start_invocation writes model_response_count = 1 as a REQUEST-TIME
    # placeholder. Imputing a killed four-turn invocation at cap * 1 would bound
    # it at a quarter of its ceiling -- understating Interlock's tokens and
    # overstating the reduction, which is the direction section 2.4 refuses.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
        invoke(
            cp,
            "inv-inflight",
            run_id="run-1",
            incident_id="inc-1",
            max_output_tokens=4_096,
            finish=False,
        )
    finally:
        cp.close()

    report = measure(db)

    assert report.unconfirmed_response_count == ("inv-inflight",)
    assert report.unbounded_missing == ()  # it HAS a ceiling; the count is the problem
    assert report.bounded_output_tokens == 100  # not 100 + 4096 * 1
    assert report.supports_acceptance_claim is False
    assert "inv-inflight" in render_ac9_report(report)


def test_full_coverage_makes_the_four_figures_coincide_and_the_report_says_so(db):
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
        invoke(cp, "inv-2", run_id="run-1", incident_id="inc-1", output_tokens=200)
    finally:
        cp.close()

    report = measure(db)

    assert report.coverage_ratio == 1.0
    assert report.coverage_is_complete
    assert (
        report.observed_output_tokens
        == report.bounded_output_tokens
        == report.sensitivity_output_tokens
    )
    assert (
        report.observed_reduction
        == report.bounded_reduction
        == report.sensitivity_reduction
    )
    assert report.supports_acceptance_claim is True
    assert "coincide" in render_ac9_report(report)


def test_cache_read_tokens_move_none_of_the_ac9_numbers(db, tmp_path):
    # ACCEPTANCE.md section 5: a bandwidth indicator, "not new input tokens and
    # not a billing figure". At 1.4e9 in the baseline it would swamp every AC-9
    # figure it were added to.
    def populate(path: Path, *, cache_read: int | None) -> None:
        cp = writer(path)
        try:
            add_cohort_run(cp, "run-1")
            add_incident(cp, "inc-1", "run-1")
            invoke(
                cp,
                "inv-1",
                run_id="run-1",
                incident_id="inc-1",
                output_tokens=100,
                input_tokens=7,
                cache_read_tokens=cache_read,
            )
        finally:
            cp.close()

    populate(db, cache_read=None)
    without = measure(db)

    loud_path = tmp_path / "loud.sqlite3"
    create_production_control_plane(loud_path, now_ms=T0).close()
    populate(loud_path, cache_read=1_399_565_488)
    withcache = measure(loud_path)

    assert withcache.cache_read_tokens_total == 1_399_565_488
    assert without.cache_read_tokens_total == 0
    # Every AC-9 figure is identical across the two.
    assert withcache.observed_output_tokens == without.observed_output_tokens
    assert withcache.bounded_output_tokens == without.bounded_output_tokens
    assert withcache.sensitivity_output_tokens == without.sensitivity_output_tokens
    assert withcache.input_tokens_total == without.input_tokens_total == 7
    assert [figure.value for figure in withcache.figures()] == [
        figure.value for figure in without.figures()
    ]
    assert withcache.prompt_reduction == without.prompt_reduction


# --------------------------------------------------------------------------
# what the harness refuses to decide
# --------------------------------------------------------------------------


VERDICT_WORDS = (
    "pass",
    "passes",
    "passed",
    "passing",
    "fail",
    "fails",
    "failed",
    "failure",
    "go",
    "no-go",
    "nogo",
    "verdict",
    "accepted",
    "rejected",
    "green",
    "red",
)


def test_the_rendered_report_carries_no_verdict_word(db):
    # Q-0005 (canary duration, sample size, numeric exit criteria) is open, and
    # ACCEPTANCE.md section 3 refuses to convert AC-9's targets into go/no-go
    # thresholds. A harness that printed a verdict would convert them by
    # inertia, so the prohibition is asserted against the rendered bytes rather
    # than against an intention.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
        invoke(
            cp,
            "inv-2",
            run_id="run-1",
            incident_id="inc-1",
            usage_status="unavailable",
            max_output_tokens=None,
        )
        invoke(cp, "inv-3", run_id="run-1", incident_id=None, finish=False)
    finally:
        cp.close()

    rendered = render_ac9_report(measure(db))

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(word) for word in VERDICT_WORDS) + r")\b",
        re.IGNORECASE,
    )
    assert pattern.search(rendered) is None, pattern.findall(rendered)


def test_the_rendered_report_is_ascii_only(db):
    # The cp932 console rule: one em-dash turns a report into a
    # UnicodeEncodeError on the terminal it is read from.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
    finally:
        cp.close()

    rendered = render_ac9_report(measure(db))

    rendered.encode("ascii")  # raises if anything non-ASCII slipped in
    rendered.encode("cp932")


def test_the_targets_print_as_targets_beside_the_cohort_size(db):
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_cohort_run(cp, "run-2")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
    finally:
        cp.close()

    rendered = render_ac9_report(measure(db))

    assert "Targets (targets, not thresholds; Q-0005 is open)" in rendered
    assert f"{PROMPT_REDUCTION_TARGET * 100:.2f} percent" in rendered
    assert f"{OUTPUT_TOKEN_REDUCTION_TARGET * 100:.2f} percent" in rendered
    # Every rate carries the cohort size: four figures plus the prompt half.
    assert rendered.count("cohort size 2 runs") == 5


def test_the_four_figures_print_together_each_labelled_with_its_kind(db):
    # Section 2.4 makes the breakdown required output; there is deliberately no
    # accessor that returns a subset.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
        invoke(cp, "inv-2", run_id="run-1", incident_id="inc-1", usage_status="unavailable")
    finally:
        cp.close()

    report = measure(db)
    figures = report.figures()

    assert [figure.label for figure in figures] == [
        "coverage",
        "observed output-token reduction",
        "bounded output-token reduction",
        "sensitivity output-token reduction",
    ]
    assert figures[2].kind == KIND_LOWER_BOUND
    assert figures[3].kind == KIND_ASSUMPTION
    rendered = render_ac9_report(report)
    for figure in figures:
        assert f"{figure.label}:" in rendered
        assert figure.kind in rendered
    # The assumption is labelled where it appears AND restated in prose, since
    # section 2.4 requires it everywhere.
    assert "ASSUMPTION and NOT a bound" in rendered


def test_an_empty_cohort_computes_no_rate_rather_than_a_zero(db):
    # No runs terminated inside the period. "Not computable" and "zero" are
    # different statements and only the first is true.
    report = measure(db)

    assert report.cohort_size == 0
    assert report.invocation_count == 0
    assert report.coverage_ratio is None
    assert report.coverage_is_complete is False
    assert all(figure.value is None for figure in report.figures())
    assert "not computable" in render_ac9_report(report)


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_an_unreadable_usage_status_is_refused_rather_than_placed(db):
    # The CHECK makes this unreachable through the schema, so the condition is
    # forged the same way test_cohort forges an unknown run status: a database
    # written by a build with a wider vocabulary. Neither silent answer is
    # unbiased -- "covered" adds a row that contributed no tokens to the
    # coverage numerator, "missing" imputes over a figure that may be there.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
        cp.execute("PRAGMA ignore_check_constraints = ON")
        cp.execute("UPDATE ai_invocation SET usage_status = 'probably-fine'")
    finally:
        cp.close()

    with pytest.raises(UnknownUsageStatusInLedgerRefused) as refusal:
        measure(db)
    assert "probably-fine" in str(refusal.value)


def test_a_baseline_with_no_runs_is_refused(db):
    # A reduction against a baseline of nothing is not a large number, it is no
    # number, and a division by zero downstream would print as infinity.
    with pytest.raises(BaselineRefused):
        MeasuredBaseline(
            completed_runs=0,
            model_responses=3531,
            output_tokens=567_839,
            tool_calls=4960,
            cache_read_tokens=0,
            source="a baseline over no runs",
        )


def test_the_shipped_baseline_is_the_measured_one_from_acceptance(db):
    # If these drift from ACCEPTANCE.md section 5 the reduction is against a
    # number nobody measured.
    assert V1_MEASURED_BASELINE.completed_runs == 195
    assert V1_MEASURED_BASELINE.model_responses == 3531
    assert V1_MEASURED_BASELINE.output_tokens == 567_839
    # 4,960 tool calls is section 2.2's first error direction: carried, and used
    # in no arithmetic.
    assert V1_MEASURED_BASELINE.tool_calls == 4960


def test_measuring_writes_nothing(db):
    # The read-only capability is the reader's, but a module that tried to write
    # would still be a defect, and the file's bytes are how that is proved.
    cp = writer(db)
    try:
        add_cohort_run(cp, "run-1")
        add_incident(cp, "inc-1", "run-1")
        invoke(cp, "inv-1", run_id="run-1", incident_id="inc-1", output_tokens=100)
    finally:
        cp.close()

    before = db.read_bytes()
    render_ac9_report(measure(db))
    assert db.read_bytes() == before
