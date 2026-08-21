"""The AI invocation ledger -- the units, the ceiling, and what a missing figure is.

``docs/measurement-harness.md`` sections 2.2-2.4 from the API side.
``tests/control_plane/test_production_schema.py`` already pins what the table's
``CHECK`` constraints do to hand-written ``INSERT``s; what is unproven until here
is that :mod:`~claude_org_runtime.control_plane.ai_invocation` can never *reach*
one of them with a row a caller believed was legal, and that every figure the
report will read means what section 2.4 says it means.

The cases that would each cost a real result if the module got them wrong:

* the **ceiling is per request times the response count**, so a tool-using
  invocation whose summed output exceeds one request's cap is legal and the
  same output against a single response is refused -- comparing against the flat
  cap would refuse every honest agentic loop;
* **retries are not responses**: a 429 plus a successful retry is two attempts
  and one assistant turn, and folding them together would report a flaky network
  as AI workload;
* a **missing usage record round-trips as a named status**, not as a zero, in
  both its shapes -- ``partial`` keeps the fields that did arrive, ``unavailable``
  says none did;
* an invocation with **no ``incident_id``** is recorded, because AC-1 is measured
  from these rows and refusing it would make AC-1 true by construction;
* an invocation with **no ``max_output_tokens``** is recorded and stays
  recognisable as ``unbounded_missing``, because that is the one thing that
  cannot be recovered afterwards.

Every timestamp is :data:`T0` and arithmetic on it. No *timestamp* column has a
``DEFAULT`` (``time-base-policy.md`` section 2, rule 2) and no function under
test reads a clock, so a suite whose expectations moved with the wall clock
would be asserting something the production code cannot observe. The two
columns that do carry a ``DEFAULT`` -- ``model_response_count`` and
``attempt_count``, both ``DEFAULT 1`` -- are written explicitly by both writers,
and the value the start writes into the first of them is a placeholder that
gets its own test below.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane.ai_invocation import (
    USAGE_STATUSES,
    CompletionPrecedesStartRefused,
    DuplicateInvocationRefused,
    InvocationAlreadyCompleteRefused,
    InvocationNotStartedRefused,
    MalformedAttemptCountRefused,
    MalformedCeilingRefused,
    MalformedResponseCountRefused,
    NegativeTokenCountRefused,
    OutputExceedsRequestCeilingRefused,
    ProviderUsage,
    UnknownUsageStatusRefused,
    UsageStatusContradictsTokensRefused,
    UsageWithoutRecordRefused,
    complete_invocation,
    read_invocation,
    start_invocation,
)
from claude_org_runtime.control_plane.migrator import create_production_control_plane

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

ADAPTER = "anthropic-adapter/3"
PROVIDER = "anthropic"
MODEL = "some-model"

RUN_ID = "run-1"
INCIDENT_ID = "inc-1"


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_production_control_plane(tmp_path / "production.sqlite3", now_ms=T0)
    try:
        add_run(connection)
        add_incident(connection)
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal surroundings an invocation needs
# --------------------------------------------------------------------------


def add_run(cp: sqlite3.Connection, run_id: str = RUN_ID, at: int = T0) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
        " VALUES (?, 'running', ?, ?)",
        (run_id, at, at),
    )
    return run_id


def add_incident(cp: sqlite3.Connection, incident_id: str = INCIDENT_ID, at: int = T0) -> str:
    cp.execute(
        """
        INSERT INTO incident (incident_id, run_id, session_id, fact_state,
                              detector_version, dedup_key, created_at_ms, updated_at_ms)
        VALUES (?, ?, NULL, 'stalled', 'd1', ?, ?, ?)
        """,
        (incident_id, RUN_ID, f"dedup/{incident_id}", at, at),
    )
    return incident_id


def start(cp: sqlite3.Connection, invocation_id: str = "inv-1", **kwargs) -> str:
    """Start an invocation with the ordinary, fully attributed shape."""

    fields = {
        "provider": PROVIDER,
        "model": MODEL,
        "adapter_version": ADAPTER,
        "started_at_ms": T0,
        "incident_id": INCIDENT_ID,
        "run_id": RUN_ID,
        "max_output_tokens": 1_024,
    }
    fields.update(kwargs)
    start_invocation(cp, invocation_id=invocation_id, **fields)
    return invocation_id


# --------------------------------------------------------------------------
# section 2.4 -- the request-time record, and what only it can bound
# --------------------------------------------------------------------------


def test_a_started_invocation_is_readable_before_any_usage_arrives(cp):
    # The row exists from request time, because the completion may never
    # happen -- a killed process, a provider that never answers -- and an
    # invocation nobody recorded is one the report cannot even count as missing.
    start(cp)

    row = read_invocation(cp, "inv-1")
    assert row is not None
    assert row["started_at_ms"] == T0
    assert row["finished_at_ms"] is None
    assert row["usage_status"] == "unavailable"
    assert row["output_tokens"] is None
    assert row["max_output_tokens"] == 1_024


def test_an_in_flight_invocation_is_told_from_one_that_finished_without_usage(cp):
    # Both carry usage_status 'unavailable' -- truthfully, in both cases no
    # usage record has arrived. finished_at_ms is the column that separates
    # "still running" from "finished, and the provider told us nothing", and
    # without the distinction every in-flight invocation would be counted as a
    # telemetry loss.
    start(cp, "inv-flight")
    start(cp, "inv-done")
    complete_invocation(
        cp,
        invocation_id="inv-done",
        usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
        model_response_count=1,
        finished_at_ms=T0 + 500,
    )

    assert read_invocation(cp, "inv-flight")["usage_status"] == "unavailable"
    assert read_invocation(cp, "inv-flight")["finished_at_ms"] is None
    assert read_invocation(cp, "inv-done")["usage_status"] == "unavailable"
    assert read_invocation(cp, "inv-done")["finished_at_ms"] == T0 + 500


def test_an_unfinished_invocation_carries_a_placeholder_response_count(cp):
    # start_invocation cannot know how many assistant turns the provider will
    # return, so the 1 it writes is a placeholder and not a count. It matters
    # because section 2.4 imputes a non-'reported' invocation at
    # max_output_tokens * model_response_count: a four-turn loop killed
    # mid-request would be bounded at cap * 1, a quarter of its real bound,
    # which UNDERSTATES Interlock's tokens and OVERSTATES the reduction -- the
    # one direction section 2.4 exists to refuse.
    start(cp, "inv-crashed", max_output_tokens=1_024)
    start(cp, "inv-finished", max_output_tokens=1_024)
    complete_invocation(
        cp,
        invocation_id="inv-finished",
        usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
        model_response_count=4,
        finished_at_ms=T0 + 900,
    )

    crashed = read_invocation(cp, "inv-crashed")
    finished = read_invocation(cp, "inv-finished")
    # The placeholder is the value the started row carries, and completing is
    # the only thing that replaces it with a counted figure. Both halves are
    # asserted so that a start that began writing a real count, or a completion
    # that stopped writing one, fails here.
    assert crashed["model_response_count"] == 1
    assert finished["model_response_count"] == 4

    # finished_at_ms IS NULL is the discriminator, and it is the ONLY one: the
    # two rows are otherwise identical in every column the imputation reads.
    assert crashed["finished_at_ms"] is None
    assert finished["finished_at_ms"] == T0 + 900
    assert crashed["usage_status"] == finished["usage_status"] == "unavailable"
    assert crashed["max_output_tokens"] == finished["max_output_tokens"]

    # A report that imputed the placeholder at the product would bound the
    # crashed invocation below the finished one that ran the same loop. That is
    # the flattering bias, in arithmetic: it must itemise the unfinished rows
    # separately instead.
    naive_bound = crashed["max_output_tokens"] * crashed["model_response_count"]
    assert naive_bound < finished["max_output_tokens"] * finished["model_response_count"]

    itemised_separately = cp.execute(
        "SELECT invocation_id FROM ai_invocation WHERE finished_at_ms IS NULL"
    ).fetchall()
    assert [entry[0] for entry in itemised_separately] == ["inv-crashed"]


def test_an_invocation_without_a_ceiling_is_recorded_and_stays_unbounded(cp):
    # Section 2.4: an invocation with no recorded max_output_tokens "is not
    # imputed at all: it is reported as unbounded_missing", and a report with a
    # non-zero count there cannot support an AC-9 acceptance claim. So the
    # writer must accept it -- refusing would hide a real request -- and the
    # row must stay recognisable afterwards, which is what NULL is doing here.
    start(cp, "inv-uncapped", max_output_tokens=None)
    complete_invocation(
        cp,
        invocation_id="inv-uncapped",
        usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
        model_response_count=3,
        finished_at_ms=T0 + 10,
    )

    row = read_invocation(cp, "inv-uncapped")
    assert row["max_output_tokens"] is None
    assert row["usage_status"] != "reported"
    # The pair (no ceiling, no reported usage) is the whole of the
    # unbounded_missing predicate; nothing later can supply the missing cap,
    # because the usage record that would carry it never arrived.
    unbounded = cp.execute(
        "SELECT invocation_id FROM ai_invocation"
        " WHERE usage_status <> 'reported' AND max_output_tokens IS NULL"
    ).fetchall()
    assert [entry[0] for entry in unbounded] == ["inv-uncapped"]


def test_a_zero_ceiling_is_refused_because_it_imputes_a_missing_invocation_at_nothing(cp):
    # A zero cap would make the bound max_output_tokens * model_response_count
    # equal zero -- the treat-missing-as-zero bias arriving through the very
    # column that exists to remove it. None is the honest "no cap was sent";
    # zero is a cap that was never legal.
    with pytest.raises(MalformedCeilingRefused):
        start(cp, "inv-zero-cap", max_output_tokens=0)
    with pytest.raises(MalformedCeilingRefused):
        start(cp, "inv-neg-cap", max_output_tokens=-1)
    assert read_invocation(cp, "inv-zero-cap") is None


# --------------------------------------------------------------------------
# section 2.2 -- what one prompt is, in both directions
# --------------------------------------------------------------------------


def test_the_output_ceiling_is_per_request_times_the_response_count(cp):
    # The DDL comment: "Comparing the summed output against a single request's
    # cap would fail on every tool-using invocation." 3,000 output tokens over
    # four assistant turns against a 1,024 cap is legal (ceiling 4,096) and the
    # identical output over one turn is not.
    start(cp, "inv-loop", max_output_tokens=1_024)
    complete_invocation(
        cp,
        invocation_id="inv-loop",
        usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=3_000),
        model_response_count=4,
        finished_at_ms=T0 + 60_000,
    )
    assert read_invocation(cp, "inv-loop")["output_tokens"] == 3_000

    start(cp, "inv-single", max_output_tokens=1_024)
    with pytest.raises(OutputExceedsRequestCeilingRefused):
        complete_invocation(
            cp,
            invocation_id="inv-single",
            usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=3_000),
            model_response_count=1,
            finished_at_ms=T0 + 60_000,
        )
    # Refused at the edge, so the row is untouched rather than half-written.
    refused = read_invocation(cp, "inv-single")
    assert refused["finished_at_ms"] is None
    assert refused["output_tokens"] is None


def test_the_ceiling_is_exact_at_the_product_and_refused_one_token_above(cp):
    # The bound is inclusive: the provider is allowed to return exactly what the
    # caller permitted, and a report that refused the boundary would drop honest
    # invocations out of the covered population.
    start(cp, "inv-exact", max_output_tokens=100)
    complete_invocation(
        cp,
        invocation_id="inv-exact",
        usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=300),
        model_response_count=3,
        finished_at_ms=T0 + 1,
    )
    assert read_invocation(cp, "inv-exact")["output_tokens"] == 300

    start(cp, "inv-over", max_output_tokens=100)
    with pytest.raises(OutputExceedsRequestCeilingRefused):
        complete_invocation(
            cp,
            invocation_id="inv-over",
            usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=301),
            model_response_count=3,
            finished_at_ms=T0 + 1,
        )


def test_an_uncapped_invocation_admits_any_reported_output(cp):
    # With no ceiling recorded there is nothing to compare against, and
    # inventing one here would be the harness deciding a figure it is supposed
    # to measure. The row's cost is that it can never be imputed (above).
    start(cp, "inv-nocap", max_output_tokens=None)
    complete_invocation(
        cp,
        invocation_id="inv-nocap",
        usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=1_000_000),
        model_response_count=1,
        finished_at_ms=T0 + 1,
    )
    assert read_invocation(cp, "inv-nocap")["output_tokens"] == 1_000_000


def test_a_retry_is_an_attempt_and_never_a_response(cp):
    # Section 2.2: "A 429 followed by a successful retry produced one assistant
    # turn; counting it as two would make a flaky network look like AI
    # workload." The two counters are independent columns for that reason, and
    # the ceiling scales with the response count only -- so the retried
    # invocation gets the same 1,024-token ceiling an unretried one would.
    start(cp, "inv-429", max_output_tokens=1_024)
    complete_invocation(
        cp,
        invocation_id="inv-429",
        usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=900),
        model_response_count=1,
        attempt_count=2,
        finished_at_ms=T0 + 3_000,
    )

    row = read_invocation(cp, "inv-429")
    assert row["attempt_count"] == 2
    assert row["model_response_count"] == 1

    start(cp, "inv-429-over", max_output_tokens=1_024)
    with pytest.raises(OutputExceedsRequestCeilingRefused):
        # If attempts had leaked into the response count, this 2,000-token
        # report would have been admitted against a doubled ceiling.
        complete_invocation(
            cp,
            invocation_id="inv-429-over",
            usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=2_000),
            model_response_count=1,
            attempt_count=2,
            finished_at_ms=T0 + 3_000,
        )


def test_a_response_count_below_one_is_refused(cp):
    # An invocation that reached the provider returned at least one assistant
    # turn. Zero would also zero the imputation product, so it is the ceiling
    # bug wearing a different column's name.
    start(cp, "inv-zero-responses")
    with pytest.raises(MalformedResponseCountRefused):
        complete_invocation(
            cp,
            invocation_id="inv-zero-responses",
            usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
            model_response_count=0,
            finished_at_ms=T0 + 1,
        )


def test_an_attempt_count_below_one_is_refused(cp):
    # The first send is an attempt; a zero describes an invocation that was
    # never transmitted and therefore has no usage to report at all.
    start(cp, "inv-zero-attempts")
    with pytest.raises(MalformedAttemptCountRefused):
        complete_invocation(
            cp,
            invocation_id="inv-zero-attempts",
            usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
            model_response_count=1,
            attempt_count=0,
            finished_at_ms=T0 + 1,
        )


# --------------------------------------------------------------------------
# section 2.3 -- the provider seam, and only the provider seam
# --------------------------------------------------------------------------


def test_a_reported_usage_round_trips_every_column_of_the_seam(cp):
    # Cache-read tokens are carried in their own column and are neither an
    # output nor an input figure (ACCEPTANCE.md section 5). A seam that folded
    # them into either would move a bandwidth indicator into AC-9's arithmetic.
    start(cp, "inv-full")
    complete_invocation(
        cp,
        invocation_id="inv-full",
        usage=ProviderUsage.reported(
            adapter_version="anthropic-adapter/4",
            output_tokens=512,
            input_tokens=2_048,
            cache_read_tokens=99_000,
        ),
        model_response_count=2,
        attempt_count=1,
        finished_at_ms=T0 + 7_000,
    )

    row = read_invocation(cp, "inv-full")
    assert row["usage_status"] == "reported"
    assert row["output_tokens"] == 512
    assert row["input_tokens"] == 2_048
    assert row["cache_read_tokens"] == 99_000
    assert row["model_response_count"] == 2
    assert row["finished_at_ms"] == T0 + 7_000
    # The version that PARSED the usage is what the three figures are qualified
    # by, so the completion's adapter version is the one the report's
    # adapter_versions set (section 6) will see.
    assert row["adapter_version"] == "anthropic-adapter/4"


def test_a_partial_usage_keeps_the_fields_that_did_arrive(cp):
    # 'partial' is "some fields present, output_tokens absent". Discarding the
    # input and cache figures because the headline one is missing would throw
    # away facts the report prints as their own series -- and imputing this row
    # is still correct, which is why it is not merged into 'unavailable'.
    start(cp, "inv-partial")
    complete_invocation(
        cp,
        invocation_id="inv-partial",
        usage=ProviderUsage.partial(
            adapter_version=ADAPTER,
            input_tokens=1_500,
            cache_read_tokens=42,
        ),
        model_response_count=2,
        finished_at_ms=T0 + 900,
    )

    row = read_invocation(cp, "inv-partial")
    assert row["usage_status"] == "partial"
    assert row["output_tokens"] is None
    assert row["input_tokens"] == 1_500
    assert row["cache_read_tokens"] == 42
    assert row["finished_at_ms"] == T0 + 900


def test_an_unavailable_usage_round_trips_as_a_completed_invocation_with_no_figures(cp):
    # The status is the fact. Nothing here writes a zero, because a zero would
    # be read by the report as a measured figure and would understate
    # Interlock's token use -- overstating the reduction in the criterion the
    # reduction is judged by.
    start(cp, "inv-none")
    complete_invocation(
        cp,
        invocation_id="inv-none",
        usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
        model_response_count=1,
        finished_at_ms=T0 + 20,
    )

    row = read_invocation(cp, "inv-none")
    assert row["usage_status"] == "unavailable"
    assert (row["output_tokens"], row["input_tokens"], row["cache_read_tokens"]) == (
        None,
        None,
        None,
    )
    assert row["finished_at_ms"] == T0 + 20


def test_unavailable_alongside_a_usage_figure_is_refused(cp):
    # 'unavailable' means no usage record at all, so an input figure under it is
    # evidence that one arrived and the ledger cannot say which half is wrong.
    # A record that arrived incomplete is 'partial'; that is why it exists.
    start(cp, "inv-contradiction")
    with pytest.raises(UsageWithoutRecordRefused):
        complete_invocation(
            cp,
            invocation_id="inv-contradiction",
            usage=ProviderUsage(
                usage_status="unavailable",
                adapter_version=ADAPTER,
                input_tokens=10,
            ),
            model_response_count=1,
            finished_at_ms=T0 + 1,
        )


@pytest.mark.parametrize(
    "usage",
    [
        # 'reported' with nothing to report: it would count as covered while
        # adding nothing to the token sum, which understates usage exactly as
        # imputing zero does.
        ProviderUsage(usage_status="reported", adapter_version=ADAPTER, output_tokens=None),
        # A missing-status row carrying tokens: the report imputes over it and
        # counts the invocation twice.
        ProviderUsage(usage_status="partial", adapter_version=ADAPTER, output_tokens=7),
    ],
)
def test_the_status_and_the_output_figure_must_agree(cp, usage):
    start(cp, "inv-disagree")
    with pytest.raises(UsageStatusContradictsTokensRefused):
        complete_invocation(
            cp,
            invocation_id="inv-disagree",
            usage=usage,
            model_response_count=1,
            finished_at_ms=T0 + 1,
        )


def test_a_status_outside_the_closed_set_is_refused(cp):
    # An unknown status belongs to no branch of the coverage arithmetic, so the
    # invocation would exist and appear in no denominator at all.
    assert USAGE_STATUSES == ("reported", "partial", "unavailable")
    start(cp, "inv-bad-status")
    with pytest.raises(UnknownUsageStatusRefused):
        complete_invocation(
            cp,
            invocation_id="inv-bad-status",
            usage=ProviderUsage(usage_status="probably_fine", adapter_version=ADAPTER),
            model_response_count=1,
            finished_at_ms=T0 + 1,
        )


@pytest.mark.parametrize(
    "usage",
    [
        ProviderUsage(usage_status="reported", adapter_version=ADAPTER, output_tokens=-1),
        ProviderUsage(usage_status="partial", adapter_version=ADAPTER, input_tokens=-1),
        ProviderUsage(usage_status="partial", adapter_version=ADAPTER, cache_read_tokens=-1),
    ],
)
def test_a_negative_token_count_is_refused_on_every_figure(cp, usage):
    # The DDL guards output_tokens alone; the other two are guarded here against
    # the same failure. A negative count subtracts from the period's total and
    # can only move the measured reduction upward.
    start(cp, "inv-negative")
    with pytest.raises(NegativeTokenCountRefused):
        complete_invocation(
            cp,
            invocation_id="inv-negative",
            usage=usage,
            model_response_count=1,
            finished_at_ms=T0 + 1,
        )


def test_the_seam_refuses_a_mapping_that_is_not_a_provider_usage(cp):
    # The seam is a typed object so a dict carrying a provider's own field names
    # cannot cross it. This is what keeps "nothing else in the harness is
    # provider-shaped" true rather than aspirational.
    start(cp, "inv-raw-dict")
    with pytest.raises(ValueError):
        complete_invocation(
            cp,
            invocation_id="inv-raw-dict",
            usage={"output_tokens": 5, "usage_status": "reported"},
            model_response_count=1,
            finished_at_ms=T0 + 1,
        )


# --------------------------------------------------------------------------
# section 2.2 -- AC-1 is measured from these rows
# --------------------------------------------------------------------------


def test_an_invocation_without_an_incident_is_recorded_and_identifiable(cp):
    # "Zero AI turns absent incidents" is the assertion that every row here has
    # an incident_id. Refusing the row would erase the only evidence the
    # violation happened and make AC-1 true by construction, so the writer
    # accepts it and the report itemises it.
    start(cp, "inv-orphan", incident_id=None)
    complete_invocation(
        cp,
        invocation_id="inv-orphan",
        usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=10),
        model_response_count=1,
        finished_at_ms=T0 + 5,
    )
    start(cp, "inv-attributed")

    assert read_invocation(cp, "inv-orphan")["incident_id"] is None
    violations = cp.execute(
        "SELECT invocation_id FROM ai_invocation WHERE incident_id IS NULL"
    ).fetchall()
    assert [entry[0] for entry in violations] == ["inv-orphan"]


def test_an_invocation_may_name_a_run_without_an_incident_and_the_reverse(cp):
    # Both attribution columns are nullable and independent: an invocation
    # triggered outside any run is as recordable as one whose incident was not
    # written. Making either mandatory would push the same evidence out of the
    # table.
    start(cp, "inv-run-only", incident_id=None, run_id=RUN_ID)
    start(cp, "inv-incident-only", incident_id=INCIDENT_ID, run_id=None)

    assert read_invocation(cp, "inv-run-only")["run_id"] == RUN_ID
    assert read_invocation(cp, "inv-incident-only")["run_id"] is None


# --------------------------------------------------------------------------
# append, then ONE usage fill-in (production-schema.md section 4)
# --------------------------------------------------------------------------


def test_a_second_completion_is_refused_rather_than_overwriting_the_first(cp):
    # The row takes exactly one fill-in. A second report -- a duplicated
    # callback, a re-parse, a second adapter -- is a different fact, and
    # overwriting would replace evidence with the most recent claim about it.
    start(cp, "inv-twice")
    complete_invocation(
        cp,
        invocation_id="inv-twice",
        usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=100),
        model_response_count=1,
        finished_at_ms=T0 + 10,
    )
    with pytest.raises(InvocationAlreadyCompleteRefused):
        complete_invocation(
            cp,
            invocation_id="inv-twice",
            usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=999),
            model_response_count=5,
            finished_at_ms=T0 + 20,
        )

    row = read_invocation(cp, "inv-twice")
    assert row["output_tokens"] == 100
    assert row["model_response_count"] == 1
    assert row["finished_at_ms"] == T0 + 10


def test_completing_an_invocation_that_was_never_started_is_refused(cp):
    # The fill-in is not an upsert: inserting here would invent a started_at_ms
    # out of the completion instant, giving every such invocation a zero latency
    # and no recorded ceiling.
    with pytest.raises(InvocationNotStartedRefused):
        complete_invocation(
            cp,
            invocation_id="inv-never",
            usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
            model_response_count=1,
            finished_at_ms=T0 + 1,
        )
    assert read_invocation(cp, "inv-never") is None


def test_starting_the_same_invocation_id_twice_is_refused(cp):
    # invocation_id is this single writer's idempotency key, so a repeat is not
    # a benign re-poll: it would make two invocations indistinguishable in every
    # report.
    start(cp, "inv-dup", started_at_ms=T0)
    with pytest.raises(DuplicateInvocationRefused):
        start(cp, "inv-dup", started_at_ms=T0 + 5_000)

    assert read_invocation(cp, "inv-dup")["started_at_ms"] == T0


def test_a_completion_before_its_own_start_is_refused(cp):
    # Latency is measured off started_at_ms and finished_at_ms, so a negative
    # duration is a mixed clock rather than a small number. The clock is the
    # caller's (time-base-policy.md section 2), which is what makes this
    # checkable at all.
    start(cp, "inv-backwards", started_at_ms=T0)
    with pytest.raises(CompletionPrecedesStartRefused):
        complete_invocation(
            cp,
            invocation_id="inv-backwards",
            usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
            model_response_count=1,
            finished_at_ms=T0 - 1,
        )
    assert read_invocation(cp, "inv-backwards")["finished_at_ms"] is None


def test_a_completion_at_the_start_instant_is_legal(cp):
    # The DDL admits equality and so does this: a sub-millisecond invocation is
    # unlikely, not impossible, and refusing it would drop a real row for the
    # sake of a strict inequality nothing needs.
    start(cp, "inv-instant", started_at_ms=T0)
    complete_invocation(
        cp,
        invocation_id="inv-instant",
        usage=ProviderUsage.unavailable(adapter_version=ADAPTER),
        model_response_count=1,
        finished_at_ms=T0,
    )
    assert read_invocation(cp, "inv-instant")["finished_at_ms"] == T0


# --------------------------------------------------------------------------
# the transaction boundary
# --------------------------------------------------------------------------


def test_a_refused_completion_leaves_no_open_transaction(cp):
    # Both calls take one transaction from txn.py, so a refusal raised from
    # inside the block must have rolled it back before it reached the caller. A
    # leaked write lock would stall every other writer on the spine, and the
    # symptom would appear nowhere near this module.
    start(cp, "inv-rollback")
    with pytest.raises(OutputExceedsRequestCeilingRefused):
        complete_invocation(
            cp,
            invocation_id="inv-rollback",
            usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=99_999),
            model_response_count=1,
            finished_at_ms=T0 + 1,
        )

    assert not cp.in_transaction
    # And the invocation is still completable afterwards with a truthful figure.
    complete_invocation(
        cp,
        invocation_id="inv-rollback",
        usage=ProviderUsage.reported(adapter_version=ADAPTER, output_tokens=1_000),
        model_response_count=1,
        finished_at_ms=T0 + 1,
    )
    assert read_invocation(cp, "inv-rollback")["output_tokens"] == 1_000


def test_an_unknown_run_reference_is_refused_by_the_foreign_key(cp):
    # The attribution columns are foreign keys and the connection runs with
    # PRAGMA foreign_keys = ON, so an invocation cannot be attributed to a run
    # that does not exist -- which would leave the AC-9 cohort join silently
    # short of a row it should have counted.
    with pytest.raises(sqlite3.IntegrityError):
        start(cp, "inv-ghost-run", run_id="run-does-not-exist")
    assert read_invocation(cp, "inv-ghost-run") is None
