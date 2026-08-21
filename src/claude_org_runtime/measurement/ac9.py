"""G6 -- AC-9's numerator, its companion series, coverage, and the four figures.

The failure this module is written against is a *number that flatters the thing
it is judging*, and ``docs/measurement-harness.md`` sections 2.2 and 2.4 record
three separate ways v1's measurement produced one. Every rule here is one of
those three closed off, and none of them is a preference.

**1. The unit, which is wrong in both directions.** The v1 baseline records
**3,531 unique assistant/model responses** and **4,960 AI tool calls** as
*separate* figures (``ACCEPTANCE.md`` section 5), and AC-9's target is a
reduction against the first.

* Counting **tool calls** compares Interlock against 4,960 -- a different unit
  -- and reports a reduction that does not exist.
* Counting the **invocation** (one row per "the AI was called") compares a
  coarser Interlock unit against a finer v1 numerator and **overstates** the
  reduction by exactly the tool-use factor: one incident-triggered invocation
  that makes three tool round trips returned *four* model responses. It is the
  same error as the first with the sign flipped, and it is the one that shows up
  as arithmetic rather than as opinion -- an invocation's summed
  ``output_tokens`` would exceed a per-request ``max_output_tokens``.

So the numerator is ``SUM(model_response_count)``, and both error directions are
written here rather than in a commit message because the column is exactly the
kind a later reader "simplifies" away. :data:`Ac9Report.invocation_count` is
still computed and printed -- it is the **AC-1** quantity, "zero AI turns absent
incidents" being a statement about invocations and not about responses -- and
the two series are printed side by side under their own names. Neither is ever
presented as the other. ``attempt_count`` is the *transport* axis and enters no
numerator at all: a 429 plus a successful retry is two attempts and **one**
assistant turn, and folding it in would report a flaky network as AI workload.

**2. A missing figure treated as zero.** Treating an absent ``output_tokens`` as
``0`` understates Interlock's token use and therefore *overstates* the
reduction -- a bias that always flatters the target, in the very criterion the
target is judged by. Nothing here ever does it. A non-``'reported'`` invocation
is imputed or itemised, never summed as nothing:

* imputed at ``max_output_tokens * model_response_count`` for the **bounded**
  figure, which is a genuine lower bound on the reduction because the provider
  cannot return more output than the caller allowed;
* itemised as :data:`Ac9Report.unbounded_missing` where the row records no
  ceiling, because there is nothing to bound it with. **A report with a non-zero
  ``unbounded_missing`` count cannot support an AC-9 acceptance claim**, and
  :func:`render_ac9_report` says so in its own words rather than leaving the
  reader to notice;
* itemised as :data:`Ac9Report.unconfirmed_response_count` where the row never
  finished. :func:`~claude_org_runtime.control_plane.ai_invocation.start_invocation`
  writes ``model_response_count = 1`` as a **request-time placeholder** -- the
  turns are unknowable before the provider answers -- so imputing such a row at
  ``cap * 1`` would bound a four-turn invocation at a quarter of its real
  ceiling, which is the flattering direction again. ``finished_at_ms IS NULL``
  is the discriminator that writer's docstring names.

**3. A percentile mistaken for a bound.** Section 2.4 records that this was got
wrong on the first pass and states why at length, so the reasoning is reproduced
here rather than referenced: **a percentile of the observed sample does not
bound the unobserved values.** A missing invocation may exceed the covered p95,
and it is *more* likely to, because telemetry loss correlates with exactly the
large, truncated, aborted responses that run long and lose their usage record.
Calling a p95 imputation "conservative" and then judging AC-9 by it can pass a
target the real numbers fail. The p95 figure is printed because the bounded one
is too loose to say anything about the likely truth -- but it is labelled
:data:`KIND_ASSUMPTION` everywhere it appears, and **the bounded figure is the
only one an acceptance judgement may use**.

**Cache-read tokens are their own series and enter none of the arithmetic.**
``ACCEPTANCE.md`` section 5 is explicit (1,399,565,488 in the baseline): "a
bandwidth indicator ... not new input tokens and not a billing figure". Adding
them to either token series would move AC-9 by three orders of magnitude on a
quantity that is not a token cost at all.

**The four figures print together or not at all** (section 2.4: "a reduction
rate printed without them is not a valid report"). :meth:`Ac9Report.figures`
returns all four, each carrying what *kind* of number it is, and
:func:`render_ac9_report` has no mode that emits a subset.

**No verdict.** ``Q-0005`` -- canary duration, sample size, numeric exit
criteria -- is open, and ``ACCEPTANCE.md`` section 3 says in terms that AC-9's
targets "are not the same thing as canary go/no-go thresholds, and this document
does not convert one into the other". A harness that printed a verdict would
convert them, answering an open question by inertia. The targets print as
targets, the cohort size prints beside every rate, and the reader judges.

**Read-only, no clock.** The connection is the one
:func:`~claude_org_runtime.measurement.reader.open_for_measurement` returns --
read-only by capability, not by this module's good behaviour -- and every
instant is the caller's ``now_ms``.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

# Both imports are of writer-owned vocabulary, not of a write capability: the
# harness's read-only property lives on the connection (reader.py) and this
# module never hands its connection anywhere. Importing beats copying for the
# same reason cohort.py gives -- a second copy of a closed set agrees with the
# original right up until the day it matters.
from claude_org_runtime.control_plane.ai_invocation import USAGE_STATUSES
from claude_org_runtime.measurement.cohort import RunCohort
from claude_org_runtime.measurement.reader import ControlPlaneRefusal

__all__ = [
    "Ac9MeasurementRefused",
    "Ac9Report",
    "BaselineRefused",
    "Figure",
    "KIND_ASSUMPTION",
    "KIND_FACT",
    "KIND_LOWER_BOUND",
    "MeasuredBaseline",
    "OUTPUT_TOKEN_REDUCTION_TARGET",
    "PROMPT_REDUCTION_TARGET",
    "UnknownUsageStatusInLedgerRefused",
    "V1_MEASURED_BASELINE",
    "measure_ac9",
    "render_ac9_report",
]


#: AC-9's targets, as **targets**. They are the Issue's stated aims
#: (``ACCEPTANCE.md`` section 5), to be confirmed by measurement; they are not
#: canary exit criteria and nothing here compares a figure against them to
#: produce an outcome. See the module docstring's last point on ``Q-0005``.
PROMPT_REDUCTION_TARGET = 0.95
OUTPUT_TOKEN_REDUCTION_TARGET = 0.90

#: What kind of number a figure is. Section 2.4's table has a "status of the
#: number" column for a reason: the four figures are not four estimates of the
#: same thing, and the difference between the last two is load-bearing.
KIND_FACT = "fact"
KIND_LOWER_BOUND = "lower bound on the reduction"
KIND_ASSUMPTION = "assumption, NOT a bound"


class Ac9MeasurementRefused(ControlPlaneRefusal):
    """Base of this module's refusals; see :mod:`claude_org_runtime.measurement`."""


class UnknownUsageStatusInLedgerRefused(Ac9MeasurementRefused):
    """A row's ``usage_status`` is outside :data:`USAGE_STATUSES`.

    Every branch of the coverage arithmetic is keyed on that column: a row is
    covered (``'reported'``) or it is imputed. A status this build does not know
    belongs to neither branch, and the two available silent answers are both
    biased -- treating it as covered adds a row to the coverage numerator that
    contributed no tokens, and treating it as missing imputes over a figure that
    may already be there. Refusing is the only reading that does not invent one.
    """


class BaselineRefused(Ac9MeasurementRefused):
    """The v1 baseline a reduction is computed against is unusable.

    A reduction is a statement about two numbers, and a baseline with no runs or
    no responses in it makes the statement vacuous rather than large. Refusing
    keeps a division by zero from arriving downstream as an infinite reduction.
    """


@dataclass(frozen=True)
class MeasuredBaseline:
    """The v1 figures AC-9's reduction is measured against.

    Verbatim from ``ACCEPTANCE.md`` section 5's measured baseline
    (2026-07-18..2026-07-25 dogfood), and normalised per 100 runs *here* so that
    the normalisation happens once and against the same run count the figures
    were measured over.

    ``tool_calls`` is carried and **never used in any arithmetic**. It is the
    4,960 of section 2.2's first error direction, kept visible so that a reader
    comparing an Interlock figure against it can see, in the same object, that
    it is the wrong unit.
    """

    completed_runs: int
    model_responses: int
    output_tokens: int
    tool_calls: int
    cache_read_tokens: int
    source: str

    def __post_init__(self) -> None:
        if self.completed_runs <= 0:
            raise BaselineRefused(
                f"the baseline records {self.completed_runs} completed runs; a "
                "per-100-run normalisation needs a positive run count, and a "
                "reduction against a baseline of nothing is not a small number, "
                "it is no number"
            )
        if self.model_responses <= 0 or self.output_tokens <= 0:
            raise BaselineRefused(
                f"the baseline records {self.model_responses} model responses "
                f"and {self.output_tokens} output tokens; a reduction against a "
                "zero baseline would print as 100 percent no matter what "
                "Interlock did"
            )

    @property
    def model_responses_per_100_runs(self) -> float:
        """The prompt figure AC-9's prompt half is a reduction from."""

        return self.model_responses * 100.0 / self.completed_runs

    @property
    def output_tokens_per_100_runs(self) -> float:
        """The token figure AC-9's token half is a reduction from."""

        return self.output_tokens * 100.0 / self.completed_runs


#: The measured baseline of ``ACCEPTANCE.md`` section 5. The run count is 195
#: *completed* runs, which is why ``cohort.py``'s denominator is a completed-run
#: cohort: a started-run cohort would not be against this number.
V1_MEASURED_BASELINE = MeasuredBaseline(
    completed_runs=195,
    model_responses=3531,
    output_tokens=567_839,
    tool_calls=4960,
    cache_read_tokens=1_399_565_488,
    source=(
        "ACCEPTANCE.md section 5, measured baseline 2026-07-18..2026-07-25 "
        "dogfood; 195 completed runs"
    ),
)


@dataclass(frozen=True)
class Figure:
    """One of section 2.4's four numbers, carrying what kind of number it is.

    The ``kind`` is not decoration. Two of the four are facts, one is a bound
    and one is an assumption, and the report is wrong -- in the way section 2.4
    describes at length -- the moment a reader takes the fourth for the third.
    ``value`` is ``None`` where the figure is not computable at all (an empty
    cohort, or a p95 with no covered sample), which is a different statement
    from zero and is rendered as one.
    """

    label: str
    kind: str
    value: float | None
    basis: str


@dataclass(frozen=True)
class Ac9Report:
    """Everything AC-9 is measured from over one cohort, and nothing decided.

    The series are separate attributes on purpose: ``model_response_total`` is
    the numerator, ``invocation_count`` is the AC-1 quantity,
    ``attempt_total`` is transport, and ``cache_read_tokens_total`` is a
    bandwidth indicator. Section 2.2 turns on their not being interchangeable.
    """

    period_start_ms: int
    period_end_ms: int
    generated_at_ms: int
    cohort_size: int
    baseline: MeasuredBaseline

    #: AC-9's numerator: assistant turns the provider returned. NOT invocations,
    #: NOT tool calls, NOT attempts.
    model_response_total: int
    #: The AC-1 series: how often an incident needed the AI at all.
    invocation_count: int
    #: The transport series. Printed, and in no numerator.
    attempt_total: int

    covered_count: int
    observed_output_tokens: int
    input_tokens_total: int
    #: Its own series. ACCEPTANCE.md section 5: not input tokens, not a billing
    #: figure. It appears in no reduction on this report.
    cache_read_tokens_total: int

    #: observed + sum(max_output_tokens * model_response_count) over the missing
    #: rows that carry a ceiling and finished.
    bounded_output_tokens: int
    #: observed + p95 * (number of missing rows). ``None`` when no covered row
    #: exists to take a p95 of.
    sensitivity_output_tokens: int | None
    covered_p95_output_tokens: int | None

    #: Missing rows with ``max_output_tokens IS NULL``: un-imputable, so a
    #: non-zero count here cannot support an AC-9 acceptance claim.
    unbounded_missing: tuple[str, ...]
    #: Missing rows with ``finished_at_ms IS NULL``: their response count is the
    #: writer's request-time placeholder, so ``cap * count`` would understate.
    unconfirmed_response_count: tuple[str, ...]
    #: Rows with no ``incident_id``: AC-1 violations, itemised by id and never
    #: folded into a count.
    ac1_violations: tuple[str, ...]
    #: Rows started in the period that name no run. Outside every run cohort, so
    #: in no rate here -- reported so that they are not silently invisible.
    unattributed_invocations: int

    # ----------------------------------------------------------------- facts

    @property
    def missing_count(self) -> int:
        """Invocations whose usage record is not ``'reported'``."""

        return self.invocation_count - self.covered_count

    @property
    def coverage_ratio(self) -> float | None:
        """``count(usage_status='reported') / count(*)``, or ``None`` if no rows.

        ``None`` rather than 1.0 for an empty ledger: "every row we have is
        covered" and "we have no rows" are different reports, and only the
        second one is true of a period the harness saw nothing in.
        """

        if self.invocation_count == 0:
            return None
        return self.covered_count / self.invocation_count

    @property
    def coverage_is_complete(self) -> bool:
        """Is every invocation covered? Section 2.4's "all four coincide" case."""

        return self.invocation_count > 0 and self.covered_count == self.invocation_count

    @property
    def supports_acceptance_claim(self) -> bool:
        """Can the bounded figure carry an AC-9 acceptance claim at all?

        ``False`` while any invocation is un-imputable, because the bounded
        figure is then not a bound over the whole cohort -- it is a bound over
        the subset that happened to be imputable, with the rest contributing
        zero, which is the treat-missing-as-zero bias wearing the bound's name.

        Two populations make it false, and section 2.4 names only the first
        explicitly:

        * :data:`unbounded_missing` -- no ceiling to impute at (section 2.4:
          such a report "cannot support an AC-9 acceptance claim");
        * :data:`unconfirmed_response_count` -- a ceiling, but multiplied by the
          writer's request-time placeholder of 1 rather than by a counted number
          of turns (``ai_invocation.start_invocation``'s docstring). Imputing
          those at ``cap * 1`` understates a crashed multi-turn invocation and
          flatters the target in the same direction, so they are itemised here
          instead of imputed, and they disqualify the claim on the same grounds.
        """

        return not self.unbounded_missing and not self.unconfirmed_response_count

    # ------------------------------------------------------------ reductions

    @property
    def model_responses_per_100_runs(self) -> float | None:
        """AC-9's prompt figure for this cohort, or ``None`` if the cohort is empty."""

        return self._per_100(self.model_response_total)

    @property
    def prompt_reduction(self) -> float | None:
        """Reduction in AI prompts against the baseline's model responses.

        The prompt half needs no imputation: ``model_response_count`` is
        ``NOT NULL`` on every row, so coverage does not enter it. What *does*
        enter it is :data:`unconfirmed_response_count` -- those rows carry the
        placeholder 1 rather than a counted figure, which can only understate
        the numerator and therefore overstate this reduction. They are itemised
        beside this figure for exactly that reason.
        """

        return _reduction(
            self.model_responses_per_100_runs,
            self.baseline.model_responses_per_100_runs,
        )

    @property
    def observed_reduction(self) -> float | None:
        """Output-token reduction over the covered invocations only.

        A fact about the covered subset and nothing more, which is why
        :meth:`figures` labels it "over N of M invocations". Taken as a figure
        about the cohort it is the treat-missing-as-zero bias exactly.
        """

        return _reduction(
            self._per_100(self.observed_output_tokens),
            self.baseline.output_tokens_per_100_runs,
        )

    @property
    def bounded_reduction(self) -> float | None:
        """The lower bound: missing imputed at ``cap * model_response_count``.

        The provider cannot return more output than the caller allowed, so this
        imputation cannot understate a missing invocation's tokens, so the
        reduction computed from it cannot overstate the real reduction. It is
        loose -- usually far above the true value -- and being loose in the safe
        direction is the property being bought. **This is the only figure an
        acceptance judgement may use**, and only when
        :attr:`supports_acceptance_claim` holds.
        """

        return _reduction(
            self._per_100(self.bounded_output_tokens),
            self.baseline.output_tokens_per_100_runs,
        )

    @property
    def sensitivity_reduction(self) -> float | None:
        """Missing imputed at the covered p95. **An assumption, not a bound.**

        A percentile of the observed sample does not bound the unobserved
        values, and telemetry loss correlates with exactly the large, truncated
        responses that would exceed it -- so this figure can sit *above* the
        truth while the bounded one, by construction, cannot. It is printed
        because the bounded figure alone says little about the likely truth, and
        it is labelled :data:`KIND_ASSUMPTION` everywhere it appears.
        """

        if self.sensitivity_output_tokens is None:
            return None
        return _reduction(
            self._per_100(self.sensitivity_output_tokens),
            self.baseline.output_tokens_per_100_runs,
        )

    def figures(self) -> tuple[Figure, ...]:
        """Section 2.4's four numbers, together, each labelled with its kind.

        There is no accessor for a subset. Section 2.4: "Coverage and the
        excluded-reason breakdown are required output. A reduction rate printed
        without them is not a valid report."
        """

        coverage = self.coverage_ratio
        return (
            Figure(
                label="coverage",
                kind=KIND_FACT,
                value=coverage,
                basis=(
                    f"{self.covered_count} of {self.invocation_count} "
                    "invocations reported a usage record"
                ),
            ),
            Figure(
                label="observed output-token reduction",
                kind=KIND_FACT + ", about the covered subset only",
                value=self.observed_reduction,
                basis=(
                    f"over {self.covered_count} of {self.invocation_count} "
                    "invocations"
                ),
            ),
            Figure(
                label="bounded output-token reduction",
                kind=KIND_LOWER_BOUND,
                value=self.bounded_reduction,
                basis=(
                    "missing invocations imputed at max_output_tokens * "
                    "model_response_count (the caller's own ceiling)"
                ),
            ),
            Figure(
                label="sensitivity output-token reduction",
                kind=KIND_ASSUMPTION,
                value=self.sensitivity_reduction,
                basis=(
                    "missing invocations imputed at the covered p95"
                    + (
                        f" of {self.covered_p95_output_tokens} tokens"
                        if self.covered_p95_output_tokens is not None
                        else " (no covered sample, so no p95)"
                    )
                ),
            ),
        )

    def _per_100(self, total: int) -> float | None:
        if self.cohort_size == 0:
            return None
        return total * 100.0 / self.cohort_size


def measure_ac9(
    connection: sqlite3.Connection,
    cohort: RunCohort,
    *,
    now_ms: int,
    baseline: MeasuredBaseline = V1_MEASURED_BASELINE,
) -> Ac9Report:
    """Measure AC-9 over *cohort*, deciding nothing.

    *connection* must be the read-only handle from
    :func:`~claude_org_runtime.measurement.reader.open_for_measurement`. This
    function issues ``SELECT`` statements and nothing else, and reads no clock:
    *now_ms* is stamped into the report as ``generated_at_ms`` for the
    provenance header (section 6, ``D-0040``).

    The cohort's invocations are the ``ai_invocation`` rows whose ``run_id`` is
    in :attr:`RunCohort.run_ids`. Rows naming **no** run cannot be attributed to
    any run cohort and so enter no rate; they are counted in
    :data:`Ac9Report.unattributed_invocations` over the report period rather than
    dropped, because a row the harness declined to attribute is still evidence
    that the AI ran.

    :raises UnknownUsageStatusInLedgerRefused: if a row carries a
        ``usage_status`` this build cannot place in the coverage arithmetic.
    """

    rows = _read_cohort_invocations(connection, cohort.run_ids)

    model_response_total = 0
    attempt_total = 0
    covered_count = 0
    observed_output_tokens = 0
    input_tokens_total = 0
    cache_read_tokens_total = 0
    imputed_bounded_tokens = 0
    covered_values: list[int] = []
    unbounded_missing: list[str] = []
    unconfirmed: list[str] = []
    ac1_violations: list[str] = []
    missing_count = 0

    for row in rows:
        invocation_id = row["invocation_id"]
        usage_status = row["usage_status"]
        if usage_status not in USAGE_STATUSES:
            raise UnknownUsageStatusInLedgerRefused(
                f"invocation {invocation_id!r} carries usage_status "
                f"{usage_status!r}, outside the closed set "
                f"{', '.join(USAGE_STATUSES)} the ai_invocation CHECK enumerates; "
                "the coverage arithmetic has a covered branch and an imputed "
                "branch and this row belongs to neither, so the harness will not "
                "guess which bias to introduce"
            )

        # The numerator, and the two series that are NOT it. attempt_count is
        # summed for its own line only: a 429 plus a successful retry is two
        # attempts and one assistant turn (section 2.2).
        model_response_total += row["model_response_count"]
        attempt_total += row["attempt_count"]

        # AC-1 is this measurement from the other side: the assertion is that
        # every row carries an incident_id. A row without one is ITEMISED, not
        # counted -- a count of violations tells a reader that AC-1 failed and
        # nothing about where to look (section 2.2).
        if row["incident_id"] is None:
            ac1_violations.append(invocation_id)

        # Input and cache-read are carried as their own series. cache_read in
        # particular never touches the output arithmetic below: ACCEPTANCE.md
        # section 5 calls it a bandwidth indicator, "not new input tokens and
        # not a billing figure", and at 1.4e9 in the baseline it would swamp
        # every AC-9 figure it were added to.
        input_tokens_total += row["input_tokens"] or 0
        cache_read_tokens_total += row["cache_read_tokens"] or 0

        if usage_status == "reported":
            output_tokens = row["output_tokens"]
            covered_count += 1
            observed_output_tokens += output_tokens
            covered_values.append(output_tokens)
            continue

        # Everything below here is a MISSING invocation, and the one thing that
        # never happens to it is being added as zero (section 2.4).
        missing_count += 1
        if row["finished_at_ms"] is None:
            # model_response_count on an unfinished row is start_invocation's
            # request-time placeholder of 1, not a counted number of turns, so
            # cap * count would bound a crashed four-turn invocation at a
            # quarter of its ceiling -- understating Interlock and overstating
            # the reduction. Itemised instead; it disqualifies the acceptance
            # claim (Ac9Report.supports_acceptance_claim).
            unconfirmed.append(invocation_id)
        elif row["max_output_tokens"] is None:
            # No ceiling was recorded at request time, and by hypothesis no
            # usage record ever arrived to read one from, so there is nothing
            # this row can honestly be bounded at (section 2.4).
            unbounded_missing.append(invocation_id)
        else:
            # The ceiling is PER REQUEST and the invocation made
            # model_response_count of them, so the invocation's ceiling is the
            # product -- the same product the ai_invocation CHECK enforces
            # against a reported figure.
            imputed_bounded_tokens += (
                row["max_output_tokens"] * row["model_response_count"]
            )

    p95 = _p95(covered_values)
    # The p95 imputation needs no ceiling, so it covers ALL missing rows,
    # including the two itemised populations the bounded figure cannot reach.
    # That the two figures are computed over different populations is not an
    # oversight: it is the difference between "what can be bounded" and "what
    # can be guessed at", and the report prints the itemisations beside both.
    sensitivity_output_tokens = (
        None if p95 is None else observed_output_tokens + p95 * missing_count
    )

    return Ac9Report(
        period_start_ms=cohort.period_start_ms,
        period_end_ms=cohort.period_end_ms,
        generated_at_ms=now_ms,
        cohort_size=cohort.denominator,
        baseline=baseline,
        model_response_total=model_response_total,
        invocation_count=len(rows),
        attempt_total=attempt_total,
        covered_count=covered_count,
        observed_output_tokens=observed_output_tokens,
        input_tokens_total=input_tokens_total,
        cache_read_tokens_total=cache_read_tokens_total,
        bounded_output_tokens=observed_output_tokens + imputed_bounded_tokens,
        sensitivity_output_tokens=sensitivity_output_tokens,
        covered_p95_output_tokens=p95,
        unbounded_missing=tuple(unbounded_missing),
        unconfirmed_response_count=tuple(unconfirmed),
        ac1_violations=tuple(ac1_violations),
        unattributed_invocations=_count_unattributed(
            connection,
            period_start_ms=cohort.period_start_ms,
            period_end_ms=cohort.period_end_ms,
        ),
    )


def render_ac9_report(report: Ac9Report) -> str:
    """Render *report* as plain ASCII text, with no verdict in it.

    ASCII only, ``-`` never an em-dash: this reaches a cp932 console, where a
    single U+2014 turns a report into a ``UnicodeEncodeError``.

    Every rate prints with the cohort size beside it, the four figures print
    together, the targets print as targets, and there is no pass/fail string
    anywhere -- ``Q-0005`` is open and a verdict here would answer it by
    inertia (module docstring).
    """

    lines: list[str] = []
    lines.append("AC-9 measurement -- AI prompts and output tokens")
    lines.append(
        f"  period          [{report.period_start_ms}, {report.period_end_ms}) "
        "(half-open, epoch ms)"
    )
    lines.append(f"  generated at    {report.generated_at_ms}")
    lines.append(f"  cohort size     {report.cohort_size} runs")
    lines.append(f"  baseline        {report.baseline.source}")
    lines.append("")

    lines.append("Series (each counts a different thing; none substitutes for another)")
    lines.append(
        f"  model responses (AC-9 numerator) {report.model_response_total}"
        f"    per 100 runs: {_rate(report.model_responses_per_100_runs)}"
    )
    lines.append(
        f"  invocations (AC-1 quantity)      {report.invocation_count}"
        "    how often an incident needed the AI at all"
    )
    lines.append(
        f"  attempts (transport only)        {report.attempt_total}"
        "    retries; in no numerator"
    )
    lines.append(
        f"  output tokens (covered rows)     {report.observed_output_tokens}"
    )
    lines.append(f"  input tokens (own series)        {report.input_tokens_total}")
    lines.append(
        f"  cache-read tokens (own series)   {report.cache_read_tokens_total}"
        "    bandwidth indicator; in no AC-9 figure"
    )
    lines.append(
        f"  invocations naming no run        {report.unattributed_invocations}"
        "    outside every run cohort; in no rate here"
    )
    lines.append("")

    lines.append("The four figures (section 2.4 requires all four together)")
    for figure in report.figures():
        value = (
            _percent(figure.value)
            if figure.value is not None
            else "not computable"
        )
        lines.append(f"  {figure.label}: {value}  [{figure.kind}]")
        lines.append(f"      {figure.basis}")
        lines.append(f"      cohort size {report.cohort_size} runs")
    lines.append("")

    lines.append("Prompt half")
    lines.append(
        f"  reduction in AI prompts: {_percent(report.prompt_reduction)}  "
        f"[{KIND_FACT}]"
    )
    lines.append(
        "      model_response_count is NOT NULL on every row, so coverage does "
        "not enter this figure"
    )
    lines.append(f"      cohort size {report.cohort_size} runs")
    lines.append("")

    lines.append("Targets (targets, not thresholds; Q-0005 is open)")
    lines.append(
        f"  AI prompts    reduction target {_percent(PROMPT_REDUCTION_TARGET)} "
        "per 100 worker runs"
    )
    lines.append(
        f"  output tokens reduction target "
        f"{_percent(OUTPUT_TOKEN_REDUCTION_TARGET)} per 100 worker runs"
    )
    lines.append(
        "  This harness reports the measurements the judgement will be made "
        "from. It does not make the judgement."
    )
    lines.append("")

    lines.append("Itemisations (never folded into a count)")
    lines.append(
        f"  AC-1 violations - invocations with no incident_id "
        f"({len(report.ac1_violations)}):"
    )
    lines.extend(_itemise(report.ac1_violations, "none"))
    lines.append(
        f"  unbounded_missing - no max_output_tokens, so nothing to bound them "
        f"at ({len(report.unbounded_missing)}):"
    )
    lines.extend(_itemise(report.unbounded_missing, "none"))
    lines.append(
        f"  unconfirmed response count - never finished, so their "
        f"model_response_count is the writer's request-time placeholder of 1 "
        f"({len(report.unconfirmed_response_count)}):"
    )
    lines.extend(_itemise(report.unconfirmed_response_count, "none"))
    lines.append("")

    lines.append("What this report can and cannot support")
    if not report.supports_acceptance_claim:
        # Said in the report's own words, not left to the reader to infer from
        # a non-zero count several lines above (section 2.4).
        lines.append(
            "  This report CANNOT support an AC-9 acceptance claim. "
            f"{len(report.unbounded_missing)} invocation(s) recorded no output "
            f"ceiling and {len(report.unconfirmed_response_count)} never "
            "finished, so neither can be imputed at a bound. The bounded figure "
            "above is a bound over the imputable subset only, and the remainder "
            "contributes zero to it - which is the treat-missing-as-zero bias "
            "the bound exists to remove."
        )
    else:
        lines.append(
            "  Every missing invocation was imputable at its own recorded "
            "ceiling, so the bounded figure is a bound over the whole cohort. "
            "It is the only figure here an acceptance judgement may use."
        )
    if report.coverage_is_complete:
        lines.append(
            "  Coverage is 100 percent: no invocation was imputed, so the "
            "observed, bounded and sensitivity figures coincide, and all three "
            "equal the measured reduction."
        )
    lines.append(
        "  The sensitivity figure is an ASSUMPTION and NOT a bound. A "
        "percentile of the covered sample does not bound the invocations that "
        "were never observed, and telemetry loss correlates with exactly the "
        "large, truncated responses that would exceed it."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _reduction(interlock_per_100: float | None, baseline_per_100: float) -> float | None:
    """``1 - interlock/baseline``, or ``None`` when the cohort is empty.

    Not clamped. A negative reduction means Interlock used *more* than the
    baseline, and that is a measurement the report is obliged to print rather
    than floor at zero.
    """

    if interlock_per_100 is None:
        return None
    return 1.0 - (interlock_per_100 / baseline_per_100)


def _p95(values: Sequence[int]) -> int | None:
    """Nearest-rank p95 of *values*, or ``None`` for an empty sample.

    Nearest rank (``ceil(0.95 * n)``) rather than an interpolating definition
    because it returns an **observed** value: the sensitivity figure is already
    an assumption, and interpolating would add a second one that no row in the
    ledger ever exhibited. It is also reproducible byte for byte across builds,
    which ``D-0040`` asks of every figure in a report.
    """

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def _read_cohort_invocations(
    connection: sqlite3.Connection, run_ids: Sequence[str]
) -> tuple[Mapping[str, object], ...]:
    """The cohort's ``ai_invocation`` rows, ordered by id.

    Chunked at 500 because SQLite's default host-parameter ceiling is 999 and a
    cohort is however many runs a period held; a query that worked in testing
    and failed on the first busy period would be a poor place to learn that.
    ``ORDER BY invocation_id`` at the end makes the itemisations, and therefore
    the rendered report, byte-reproducible (``D-0040``).
    """

    if not run_ids:
        return ()
    columns = (
        "invocation_id, incident_id, usage_status, output_tokens, input_tokens, "
        "cache_read_tokens, max_output_tokens, model_response_count, "
        "attempt_count, finished_at_ms"
    )
    names = [name.strip() for name in columns.split(",")]
    rows: list[Mapping[str, object]] = []
    for start in range(0, len(run_ids), 500):
        chunk = run_ids[start : start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        rows.extend(
            MappingProxyType(dict(zip(names, row)))
            for row in connection.execute(
                f"SELECT {columns} FROM ai_invocation "
                f"WHERE run_id IN ({placeholders})",
                tuple(chunk),
            )
        )
    return tuple(sorted(rows, key=lambda row: row["invocation_id"]))


def _count_unattributed(
    connection: sqlite3.Connection, *, period_start_ms: int, period_end_ms: int
) -> int:
    """Invocations started in the period that name no run.

    Half-open ``[start, end)`` on ``started_at_ms``, per ``time-base-policy.md``
    section 2 rule 4. These enter no rate -- there is no run to normalise them
    over -- but the count is printed, because "the AI ran and we could not say
    for which run" is evidence and not an absence.
    """

    return connection.execute(
        """
        SELECT COUNT(*) FROM ai_invocation
         WHERE run_id IS NULL
           AND started_at_ms >= :period_start_ms
           AND started_at_ms < :period_end_ms
        """,
        {"period_start_ms": period_start_ms, "period_end_ms": period_end_ms},
    ).fetchone()[0]


def _percent(value: float | None) -> str:
    if value is None:
        return "not computable"
    return f"{value * 100:.2f} percent"


def _rate(value: float | None) -> str:
    if value is None:
        return "not computable (cohort is empty)"
    return f"{value:.2f}"


def _itemise(ids: Sequence[str], empty: str) -> list[str]:
    if not ids:
        return [f"      {empty}"]
    return [f"      {identifier}" for identifier in ids]
