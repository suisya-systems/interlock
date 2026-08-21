"""G6 -- the AI invocation ledger: a missing usage record is a fact with a name.

``docs/measurement-harness.md`` sections 2.2-2.4 and ``D-0038`` are the design
this module writes to; nothing here decides anything they left open. The table
already ships in ``migrations/0001_initial.sql``; this is its only writer.

**The failure this module is written against.** v1 measured "AI prompts" from
whatever the log happened to contain, and its own baseline records the trap in
two numbers that were never reconciled: **3,531 unique assistant/model
responses** and **4,960 AI tool calls**. AC-9's target is a reduction against
the first of those. A ledger that counts the other unit -- in *either*
direction -- reports a reduction that does not exist:

* counting **tool calls** compares Interlock against 4,960 and invents a
  reduction out of the ratio between two different units;
* counting the **invocation** (one row per "the AI was called") compares a
  coarser Interlock unit against a finer v1 numerator and *overstates* the
  reduction by exactly the tool-use factor -- the same error with the sign
  flipped. It is also the one that shows up as arithmetic rather than as
  opinion: an invocation's summed ``output_tokens`` would exceed a per-request
  ``max_output_tokens``, which is a contradiction, not a debate.

So ``model_response_count`` is **assistant turns the provider returned inside
this invocation: 1 plus one per tool round trip**, it is supplied by the
component that ran the loop and counted them, and it is neither the tool-call
count nor the constant 1. Section 2.2 says getting it wrong breaks AC-9 in both
directions; a later reader tempted to "simplify" it to an invocation count is
looking at the second bullet above.

``attempt_count`` is the *transport* axis and is deliberately unrelated to it: a
429 followed by a successful retry is two attempts and **one** assistant turn.
Folding retries into the response count would make a flaky network read as AI
workload, which is a regression in AC-9 caused by the ledger rather than by the
system.

**Why the ceiling is a column written at request time.** Section 2.4: treating a
missing ``output_tokens`` as ``0`` understates Interlock's token use and
therefore *overstates* the reduction -- a bias that always flatters the target,
in the criterion the target is judged by. The report's honest answer is to
impute a missing invocation at ``max_output_tokens * model_response_count``,
which is a genuine lower bound because the provider cannot return more output
than the caller allowed. That imputation is only available if the caller's
ceiling was recorded **before** the request, since by hypothesis no usage record
ever came back to read it from. :func:`start_invocation` therefore writes it,
and an invocation started without one is *permitted* -- the caller may genuinely
have sent no cap -- but is then permanently un-imputable and is what the report
itemises as ``unbounded_missing``. It stays recognisable in the row:
``max_output_tokens IS NULL``, forever, because nothing here ever fills it in
afterwards from a usage record that would not bound anything.

**The provider seam is five columns and stops there.** Section 2.3: usage
figures are the one provider-shaped thing in the harness. :class:`ProviderUsage`
is that seam -- ``output_tokens`` / ``input_tokens`` / ``cache_read_tokens``,
plus the ``usage_status`` naming how complete the record was and the
``adapter_version`` qualifying all three. Nothing else in this module or in the
harness above it is provider-shaped, and no provider vocabulary crosses the
seam: an adapter translates, it does not widen. ``cache_read_tokens`` rides
along the same seam and is *neither* an output nor an input figure
(``ACCEPTANCE.md`` section 5, 1,399,565,488 in the baseline: "a bandwidth
indicator ... not new input tokens and not a billing figure"), so it is stored
in its own column and never added to either.

**AC-1 is measured from these rows, so a missing ``incident_id`` is recorded,
not refused.** "Zero AI turns absent incidents" is the assertion that every row
here carries one (section 2.2). Refusing an invocation that names no incident
would destroy the only evidence the violation ever existed and make AC-1 true by
construction -- the measurement equivalent of counting a structural zero as a
triumph. :func:`start_invocation` writes the row and the report itemises it.

**Append, then one usage fill-in** (``production-schema.md`` section 4, the
writer table). ``invocation_id`` is the idempotency key and the writer is single
*by construction* -- the Dispatcher AI is on-demand and incident-triggered
(``D-0003``) -- which is why, unlike ``watcher_liveness``, no lease epoch is
fenced inside these statements: there is no second writer to fence against, and
inventing an epoch column here would imply a concurrency this component does not
have. What is enforced instead is that the fill-in happens **once**:
:func:`complete_invocation` refuses a second completion rather than overwriting
the first, because a re-reported usage record is a different fact and the first
one is evidence.

A started-but-unfinished row carries ``usage_status = 'unavailable'`` -- true at
that instant, since no usage record has arrived -- and is told apart from an
invocation that *finished* without usage by ``finished_at_ms IS NULL``. That is
the distinction, and it is why the completion writes the timestamp even when the
usage it carries is empty.

Both calls take one transaction from :mod:`.txn`: the completion reads the row's
ceiling and started instant and then writes against them, and a ceiling read
outside the write could be stale by the time it is compared.

Every timestamp is an integer of milliseconds since the Unix epoch and comes
from the caller. Nothing here reads a clock, and no *timestamp* column has a
``DEFAULT`` (``time-base-policy.md`` section 2, rule 2) -- which is the rule
that matters, because a defaulted timestamp would be filled from the database's
own clock and silently leave the caller's time base. Non-timestamp columns are
a different question and two of them do carry one: ``model_response_count`` and
``attempt_count`` are ``DEFAULT 1`` in the DDL. Both writers below name those
columns explicitly anyway, so the default is never what lands in a row this
module wrote.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .schema import ControlPlaneRefusal
from .txn import transaction

__all__ = [
    "USAGE_STATUSES",
    "AiInvocationRefused",
    "AiInvocationUsageError",
    "DuplicateInvocationRefused",
    "InvocationAlreadyCompleteRefused",
    "InvocationNotStartedRefused",
    "MalformedAttemptCountRefused",
    "MalformedCeilingRefused",
    "MalformedResponseCountRefused",
    "NegativeTokenCountRefused",
    "OutputExceedsRequestCeilingRefused",
    "ProviderUsage",
    "UnknownUsageStatusRefused",
    "UsageStatusContradictsTokensRefused",
    "UsageWithoutRecordRefused",
    "CompletionPrecedesStartRefused",
    "complete_invocation",
    "read_invocation",
    "start_invocation",
]


#: The closed ``usage_status`` vocabulary, mirrored from the table's own CHECK.
#:
#: The three members are three *different* facts and section 2.4 is built on
#: keeping them apart: ``'reported'`` is a complete usage record, ``'partial'``
#: is some fields present with ``output_tokens`` absent, ``'unavailable'`` is no
#: usage record at all. Collapsing the last two into "missing" would lose the
#: input and cache figures a partial record did deliver; collapsing either into
#: a zero output is the bias the whole section exists to refuse.
USAGE_STATUSES = ("reported", "partial", "unavailable")

#: The status a row is born with. No usage record has arrived at request time,
#: which is exactly what ``'unavailable'`` says, so the start needs no fourth
#: member to describe itself -- and a fourth member would be a state the
#: table's CHECK does not admit.
_STATUS_AT_START = "unavailable"


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


class AiInvocationRefused(ControlPlaneRefusal):
    """An invocation write was refused at the edge; nothing was written.

    A subclass of :class:`~.schema.ControlPlaneRefusal` because the answer is
    the same one the rest of the control plane gives: state a fact that cannot
    be recorded truthfully rather than record an approximation of it.

    **What is behind each subclass, exactly.** Most -- but not all -- of them
    restate a constraint the DDL also holds, and the duplication there is
    deliberate: the constraint can only say "your database rejected something",
    while the edge knows *which* figure the caller got wrong and says so.

    * Backed by a ``CHECK`` in ``ai_invocation``:
      :class:`UnknownUsageStatusRefused`,
      :class:`UsageStatusContradictsTokensRefused`,
      :class:`MalformedCeilingRefused`, :class:`MalformedResponseCountRefused`,
      :class:`MalformedAttemptCountRefused`,
      :class:`OutputExceedsRequestCeilingRefused`,
      :class:`CompletionPrecedesStartRefused`, and
      :class:`NegativeTokenCountRefused` **for ``output_tokens`` only** -- the
      ``input_tokens`` and ``cache_read_tokens`` halves of it are this module's
      alone, as that subclass's own docstring records.
    * Backed by the ``invocation_id`` PRIMARY KEY rather than a ``CHECK``:
      :class:`DuplicateInvocationRefused`.
    * Enforced by this module and nothing else:
      :class:`UsageWithoutRecordRefused` (a status of ``'unavailable'`` beside
      an input or cache figure is a perfectly legal row to the DDL),
      :class:`InvocationNotStartedRefused` (an ``UPDATE`` matching no row is
      not an error in SQL, it is a silent no-op), and
      :class:`InvocationAlreadyCompleteRefused`.

    **Where the refusal happens relative to the write lock.** The checks on the
    caller's own arguments -- the vocabulary, the status/token agreement, the
    negative counts, the ceiling and the two counts -- run before
    :func:`~.txn.transaction` is entered and therefore before the lock is taken.
    Four do not, because they compare the caller's figures against the stored
    row and that comparison is only sound inside the same transaction as the
    write: :class:`DuplicateInvocationRefused`,
    :class:`InvocationAlreadyCompleteRefused`,
    :class:`CompletionPrecedesStartRefused` and
    :class:`OutputExceedsRequestCeilingRefused` are all raised with
    ``BEGIN IMMEDIATE`` already held, which rolls the transaction back and
    leaves nothing written.

    **The set-once discipline lives here, not in the DDL.** ``ai_invocation``
    carries no trigger at all: unlike ``outbox_delivery_is_set_once``,
    ``action_apply_is_set_once`` and ``gate_transition_rows_are_immutable``, a
    completed row here can be ``UPDATE``d or ``DELETE``d by any writer that goes
    around this module, and only :func:`complete_invocation` refusing a second
    fill-in makes the one-usage-record rule hold. The DDL is quoted verbatim
    from ``measurement-harness.md`` section 2.3, so an
    ``ai_invocation_usage_is_set_once`` trigger is the natural backstop if the
    design later wants one -- flagged here, not taken, because adding it would
    be a schema decision and this is its implementation.
    """


class UnknownUsageStatusRefused(AiInvocationRefused):
    """``usage_status`` is outside :data:`USAGE_STATUSES`.

    An unknown status would be counted by no branch of the coverage arithmetic
    in section 2.4, so the invocation would silently leave both the covered and
    the imputed populations -- a row that exists and is in no denominator.
    """


class UsageStatusContradictsTokensRefused(AiInvocationRefused):
    """The status and the presence of ``output_tokens`` disagree.

    The DDL states it as an equivalence -- ``(usage_status = 'reported') =
    (output_tokens IS NOT NULL)`` -- and both halves cost a real result.
    ``'reported'`` with no tokens puts an invocation in coverage's numerator
    while contributing nothing to the token sum, which understates Interlock's
    usage exactly as imputing zero would. A non-``'reported'`` row *with*
    tokens is a figure the report will impute over and therefore double.
    """


class UsageWithoutRecordRefused(AiInvocationRefused):
    """``'unavailable'`` was reported alongside a usage figure.

    Section 2.3 defines ``'unavailable'`` as **no usage record at all**. A row
    carrying an input or cache-read count under that status is evidence that a
    record did arrive, so one of the two is wrong and the ledger cannot say
    which. The honest report of a record that arrived with only some fields is
    ``'partial'``, which is why that member exists.
    """


class NegativeTokenCountRefused(AiInvocationRefused):
    """A token count is negative.

    The DDL guards ``output_tokens`` alone; the other two are guarded here for
    the same reason and against the same failure. A negative count is not a
    smaller number than zero in this arithmetic -- it *subtracts* from the
    period's total and can only move the measured reduction upward, which is
    once more the direction that flatters the target.
    """


class MalformedCeilingRefused(AiInvocationRefused):
    """``max_output_tokens`` is not a positive integer.

    ``0`` is the value that matters: it would make the bound
    ``max_output_tokens * model_response_count`` equal zero, so a missing
    invocation would be imputed at nothing at all. That is the "treat missing as
    zero" bias of section 2.4 arriving through the one column that exists to
    prevent it. ``None`` is legal and different -- it is the honest
    ``unbounded_missing`` -- and only a recorded ceiling has to be a real one.
    """


class MalformedResponseCountRefused(AiInvocationRefused):
    """``model_response_count`` is below 1.

    An invocation that reached the provider returned at least one assistant
    turn, so zero is not a smaller count but a different claim. It would also
    zero the imputation product, exactly as a zero ceiling does.
    """


class MalformedAttemptCountRefused(AiInvocationRefused):
    """``attempt_count`` is below 1.

    The first send is an attempt. A zero here would describe an invocation that
    was never transmitted, which has no usage record to complete.
    """


class OutputExceedsRequestCeilingRefused(AiInvocationRefused):
    """The reported output exceeds ``max_output_tokens * model_response_count``.

    **The ceiling is per request, and an invocation makes
    ``model_response_count`` of them**, so the invocation's ceiling is the
    product. The DDL says in terms that comparing the summed output against a
    single request's cap "would fail on every tool-using invocation" -- so this
    refusal must be computed against the product, and a future simplification to
    the flat cap would refuse every honest agentic loop while looking stricter.

    Reaching it the other way round means the caller's own arithmetic is
    inconsistent -- the provider cannot return more than it was allowed -- and
    the bound in section 2.4 stops being a bound if such a row is stored.
    """


class CompletionPrecedesStartRefused(AiInvocationRefused):
    """``finished_at_ms`` is earlier than the row's ``started_at_ms``.

    Latency is measured off these two columns, and a negative duration is not a
    small one: it is a clock the caller mixed. ``time-base-policy.md`` section 2
    puts the clock in the caller's hands precisely so this is checkable here.
    """


class InvocationNotStartedRefused(AiInvocationRefused):
    """No invocation with this id was ever started.

    The completion is a fill-in, never an upsert. Inserting the row here instead
    would manufacture a ``started_at_ms`` out of the completion instant and hand
    every such invocation a zero latency and no recorded ceiling.
    """


class InvocationAlreadyCompleteRefused(AiInvocationRefused):
    """This invocation's usage was already filled in once.

    ``production-schema.md`` section 4 allows the row exactly one usage fill-in.
    A second report is a *different* fact -- a retried parse, a duplicated
    callback, a second adapter -- and overwriting would replace evidence with
    the most recent claim about it, which is the arrival-order last-write-wins
    the control plane refuses everywhere else.
    """


class DuplicateInvocationRefused(AiInvocationRefused):
    """An invocation with this id was already started.

    ``invocation_id`` is the idempotency key of a single writer, so a repeat is
    not a benign re-poll: it is either a caller reusing an id (and about to make
    two invocations indistinguishable in every report) or a lost response
    treated as a new request. Both need the caller to know.
    """


class AiInvocationUsageError(ValueError):
    """The caller used this module in a way that would break its guarantees.

    A programming error rather than a refusable fact -- a non-integer clock, an
    empty identifier -- and therefore not part of the refusal hierarchy a caller
    handles.
    """


# --------------------------------------------------------------------------
# argument checks
# --------------------------------------------------------------------------


def _require_identifier(field: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AiInvocationUsageError(
            f"{field} must be a non-empty string, got {value!r}"
        )


def _require_optional_identifier(field: str, value: Any) -> None:
    if value is not None:
        _require_identifier(field, value)


def _require_int(field: str, value: Any) -> None:
    # bool is excluded explicitly because it is an int in Python: a
    # started_at_ms of True stores 1, a timestamp in 1970 that no typeof CHECK
    # can catch because SQLite sees a perfectly good integer.
    if isinstance(value, bool) or not isinstance(value, int):
        raise AiInvocationUsageError(
            f"{field} must be an int, got {value!r}; the clock and the counts "
            "are the caller's and are never derived from the database"
        )


def _require_optional_count(field: str, value: Any) -> None:
    if value is None:
        return
    _require_int(field, value)
    if value < 0:
        raise NegativeTokenCountRefused(
            f"{field} must not be negative, got {value}; a negative count "
            "subtracts from the period's token total and can only move the "
            "measured reduction upward (measurement-harness.md section 2.4)"
        )


# --------------------------------------------------------------------------
# the provider seam
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderUsage:
    """What a provider adapter reports back, and the whole of what it reports.

    Five fields, matching section 2.3's seam exactly: the three token figures,
    the ``usage_status`` saying how complete the record was, and the
    ``adapter_version`` that qualifies all of them. Anything a provider says
    that is not one of these is the adapter's business and stops at this
    boundary -- the report's ``adapter_versions`` set (section 6) is how a
    change in the translation is made visible, and it is only meaningful while
    the translation happens on the provider's side of the seam.

    ``model_response_count`` and ``attempt_count`` are deliberately **not**
    here. They are counted by the component that ran the loop, not parsed out of
    a usage record: a provider that reports usage per request cannot tell us how
    many requests our loop made, and a provider whose response never arrived
    still made an attempt we must count.

    Construct through :meth:`reported`, :meth:`partial` or :meth:`unavailable`
    rather than by hand -- each one makes its status and its tokens agree by
    construction, which is the invariant the DDL states as an equivalence.
    """

    usage_status: str
    adapter_version: str
    output_tokens: int | None = None
    input_tokens: int | None = None
    cache_read_tokens: int | None = None

    @classmethod
    def reported(
        cls,
        *,
        adapter_version: str,
        output_tokens: int,
        input_tokens: int | None = None,
        cache_read_tokens: int | None = None,
    ) -> "ProviderUsage":
        """A complete usage record: the provider returned an output figure."""

        return cls(
            usage_status="reported",
            adapter_version=adapter_version,
            output_tokens=output_tokens,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
        )

    @classmethod
    def partial(
        cls,
        *,
        adapter_version: str,
        input_tokens: int | None = None,
        cache_read_tokens: int | None = None,
    ) -> "ProviderUsage":
        """A record arrived, without ``output_tokens``.

        The fields that *did* arrive are kept: they are facts, and discarding
        them because the headline figure is missing would throw away the input
        and cache series the report prints in their own right.
        """

        return cls(
            usage_status="partial",
            adapter_version=adapter_version,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
        )

    @classmethod
    def unavailable(cls, *, adapter_version: str) -> "ProviderUsage":
        """No usage record at all -- the case section 2.4 imputes a bound for.

        It takes no token arguments on purpose: there is nothing to carry, and a
        parameter would invite a caller to pass a zero that the report would
        then read as a measured figure.
        """

        return cls(usage_status="unavailable", adapter_version=adapter_version)


def _validate_usage(usage: ProviderUsage) -> None:
    """Check the seam's own invariants before any of it reaches a statement."""

    if not isinstance(usage, ProviderUsage):
        raise AiInvocationUsageError(
            f"usage must be a ProviderUsage, got {usage!r}; the provider seam "
            "is a typed object so that a mapping with a provider's own field "
            "names cannot cross it"
        )
    _require_identifier("usage.adapter_version", usage.adapter_version)
    if usage.usage_status not in USAGE_STATUSES:
        raise UnknownUsageStatusRefused(
            f"usage_status must be one of {USAGE_STATUSES}, got "
            f"{usage.usage_status!r}; an unknown status belongs to no branch of "
            "the coverage arithmetic and would leave the invocation in no "
            "denominator at all"
        )
    _require_optional_count("usage.output_tokens", usage.output_tokens)
    _require_optional_count("usage.input_tokens", usage.input_tokens)
    _require_optional_count("usage.cache_read_tokens", usage.cache_read_tokens)

    if (usage.usage_status == "reported") != (usage.output_tokens is not None):
        raise UsageStatusContradictsTokensRefused(
            f"usage_status {usage.usage_status!r} and output_tokens "
            f"{usage.output_tokens!r} disagree: 'reported' means the provider "
            "returned an output figure and every other status means it did not. "
            "A 'reported' row without tokens counts as covered while adding "
            "nothing to the sum; a missing-status row with tokens is imputed "
            "over and counted twice"
        )
    if usage.usage_status == "unavailable" and (
        usage.input_tokens is not None or usage.cache_read_tokens is not None
    ):
        raise UsageWithoutRecordRefused(
            "usage_status 'unavailable' means no usage record at all "
            "(measurement-harness.md section 2.3), but input_tokens "
            f"{usage.input_tokens!r} / cache_read_tokens "
            f"{usage.cache_read_tokens!r} say one arrived; report a record that "
            "arrived incomplete as 'partial'"
        )


# --------------------------------------------------------------------------
# the writer
# --------------------------------------------------------------------------


def start_invocation(
    connection: sqlite3.Connection,
    *,
    invocation_id: str,
    provider: str,
    model: str,
    adapter_version: str,
    started_at_ms: int,
    incident_id: str | None = None,
    run_id: str | None = None,
    max_output_tokens: int | None = None,
) -> None:
    """Record an invocation at **request** time, before the provider answers.

    Everything this row needs in order to be bounded later is known now and
    nothing that is known now is left for the completion to supply, because the
    completion may never happen: a process killed mid-request, a provider that
    never returns, a usage record lost in transport. Section 2.4's whole
    argument turns on that asymmetry.

    ``max_output_tokens`` is the caller's own per-request cap and is the load-
    bearing one. With it, a missing invocation is imputed at
    ``max_output_tokens * model_response_count`` -- a genuine *lower bound* on
    the reduction, because the provider cannot return more output than it was
    allowed. Without it the invocation is un-imputable and the report itemises
    it as ``unbounded_missing``; a report with a non-zero count there cannot
    support an AC-9 acceptance claim. Passing ``None`` is therefore permitted
    and is not a shortcut: it is the honest record of a request that carried no
    cap.

    ``incident_id`` is likewise optional and likewise consequential. AC-1
    ("zero AI turns absent incidents") is the assertion that every row here
    carries one, so an invocation with none is written and reported as a
    violation rather than refused -- refusing it would erase the only evidence
    the violation happened.

    **``model_response_count`` written here is a request-time PLACEHOLDER of
    ``1``, not a count.** Nobody can know the number of assistant turns before
    the provider has answered; the real figure is supplied by the component that
    ran the loop and lands in :func:`complete_invocation`. The placeholder is
    consequential in exactly one direction, and it is the flattering one:
    section 2.4 imputes a non-``'reported'`` invocation at
    ``max_output_tokens * model_response_count``, so a four-turn invocation
    whose process was killed mid-loop would be imputed at ``cap * 1`` -- a
    quarter of its real bound. That *understates* Interlock's tokens and
    therefore *overstates* the reduction, which is the bias section 2.4 exists
    to refuse.

    So the placeholder must never be imputed at the product. ``finished_at_ms
    IS NULL`` is the discriminator -- it is what tells a never-completed row
    from one that finished -- and a row on that side of it carries a response
    count no writer has ever confirmed. A report must itemise those rows
    separately (as in-flight or abandoned) rather than fold them into the
    imputed population. The column and the DDL are left as
    ``measurement-harness.md`` section 2.3 gives them; what is fixed here is
    that the value's meaning is written down where a later reader of the table
    will find it.

    ``adapter_version`` is the version of the adapter issuing the request. It is
    ``NOT NULL`` and a row must therefore carry one from the start;
    :func:`complete_invocation` replaces it with the version that actually
    parsed the usage, since that is what the figures are qualified by.

    :raises DuplicateInvocationRefused: if the id was already started.
    :raises MalformedCeilingRefused: if ``max_output_tokens`` is not positive.
    """

    _require_identifier("invocation_id", invocation_id)
    _require_identifier("provider", provider)
    _require_identifier("model", model)
    _require_identifier("adapter_version", adapter_version)
    _require_optional_identifier("incident_id", incident_id)
    _require_optional_identifier("run_id", run_id)
    _require_int("started_at_ms", started_at_ms)
    if max_output_tokens is not None:
        _require_int("max_output_tokens", max_output_tokens)
        if max_output_tokens <= 0:
            raise MalformedCeilingRefused(
                f"max_output_tokens must be positive when recorded, got "
                f"{max_output_tokens}; a zero ceiling imputes a missing "
                "invocation at nothing, which is the treat-missing-as-zero bias "
                "the column exists to remove. Pass None to record honestly that "
                "the request carried no cap"
            )

    with transaction(connection) as txn:
        already = txn.execute(
            "SELECT started_at_ms FROM ai_invocation WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        if already is not None:
            raise DuplicateInvocationRefused(
                f"invocation {invocation_id!r} was already started at "
                f"{already[0]}; the id is this writer's idempotency key, so a "
                "repeat makes two invocations indistinguishable in every report "
                "rather than deduplicating one"
            )
        txn.execute(
            """
            INSERT INTO ai_invocation (
                    invocation_id, incident_id, run_id, provider, model,
                    adapter_version, usage_status, output_tokens, input_tokens,
                    cache_read_tokens, max_output_tokens, model_response_count,
                    attempt_count, started_at_ms, finished_at_ms)
            VALUES (:invocation_id, :incident_id, :run_id, :provider, :model,
                    :adapter_version, :usage_status, NULL, NULL,
                    -- model_response_count = 1 is a PLACEHOLDER, not a count:
                    -- the turns are unknown until the provider has answered,
                    -- and complete_invocation writes the real figure. A row
                    -- with finished_at_ms IS NULL therefore carries an
                    -- unconfirmed 1, and imputing it at max_output_tokens * 1
                    -- would bound a crashed multi-turn invocation at a
                    -- fraction of its real cap -- understating Interlock's
                    -- tokens and overstating the reduction (2.4). Both counts
                    -- are named explicitly rather than left to their DDL
                    -- DEFAULT 1, so the value is visible at the write site.
                    NULL, :max_output_tokens, 1,
                    1, :started_at_ms, NULL)
            """,
            {
                "invocation_id": invocation_id,
                "incident_id": incident_id,
                "run_id": run_id,
                "provider": provider,
                "model": model,
                "adapter_version": adapter_version,
                # True at this instant: no usage record has arrived. The row is
                # told apart from an invocation that finished without usage by
                # finished_at_ms IS NULL, not by a fourth status.
                "usage_status": _STATUS_AT_START,
                "max_output_tokens": max_output_tokens,
                "started_at_ms": started_at_ms,
            },
        )


def complete_invocation(
    connection: sqlite3.Connection,
    *,
    invocation_id: str,
    usage: ProviderUsage,
    model_response_count: int,
    finished_at_ms: int,
    attempt_count: int = 1,
) -> None:
    """Fill in the usage the provider reported, once.

    ``model_response_count`` is **assistant turns returned**: 1, plus one per
    tool round trip. It is not the number of tool calls (v1's 4,960, the figure
    AC-9 is *not* measured against) and it is not 1-per-invocation (which would
    overstate the reduction by the tool-use factor and let a summed output
    exceed a per-request cap). The component that ran the loop counts them; no
    part of this module infers it.

    ``attempt_count`` is transport retries and contributes to no response count:
    a 429 plus a successful retry is ``attempt_count=2`` and
    ``model_response_count=1``. Adding retries into the response count would
    report a flaky network as AI workload.

    The two are checked against the recorded ceiling **inside** the same
    transaction as the write, because the comparison that matters is
    ``output_tokens <= max_output_tokens * model_response_count`` and both
    operands must come from the row as it is being updated.

    :raises InvocationNotStartedRefused: if the id was never started.
    :raises InvocationAlreadyCompleteRefused: if usage was already filled in.
    :raises OutputExceedsRequestCeilingRefused: if the output exceeds the
        product of the recorded ceiling and the response count.
    """

    _require_identifier("invocation_id", invocation_id)
    _require_int("model_response_count", model_response_count)
    _require_int("attempt_count", attempt_count)
    _require_int("finished_at_ms", finished_at_ms)
    _validate_usage(usage)
    if model_response_count < 1:
        raise MalformedResponseCountRefused(
            f"model_response_count must be at least 1, got "
            f"{model_response_count}; an invocation that reached the provider "
            "returned at least one assistant turn, and a zero would also zero "
            "the imputation product a missing invocation is bounded by"
        )
    if attempt_count < 1:
        raise MalformedAttemptCountRefused(
            f"attempt_count must be at least 1, got {attempt_count}; the first "
            "send is an attempt, so a zero describes an invocation that was "
            "never transmitted and therefore has no usage to report"
        )

    with transaction(connection) as txn:
        row = txn.execute(
            """
            SELECT started_at_ms, finished_at_ms, max_output_tokens
              FROM ai_invocation
             WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
        if row is None:
            raise InvocationNotStartedRefused(
                f"invocation {invocation_id!r} was never started; the usage "
                "fill-in is not an upsert, and inserting here would invent a "
                "started_at_ms out of the completion instant -- a zero latency "
                "and no recorded ceiling for every such invocation"
            )
        started_at_ms, already_finished_at_ms, max_output_tokens = row
        if already_finished_at_ms is not None:
            raise InvocationAlreadyCompleteRefused(
                f"invocation {invocation_id!r} was already completed at "
                f"{already_finished_at_ms}; the row takes exactly one usage "
                "fill-in (production-schema.md section 4) and a second report "
                "is a different fact, not a correction of the first"
            )
        if finished_at_ms < started_at_ms:
            raise CompletionPrecedesStartRefused(
                f"finished_at_ms {finished_at_ms} precedes started_at_ms "
                f"{started_at_ms} for invocation {invocation_id!r}; latency is "
                "measured off these two columns and a negative duration is a "
                "mixed clock rather than a small number"
            )
        if usage.output_tokens is not None and max_output_tokens is not None:
            # The ceiling is PER REQUEST and the invocation made
            # model_response_count of them, so the invocation's ceiling is the
            # product. Comparing against the flat cap instead would refuse every
            # tool-using invocation -- the DDL comment says so in as many words.
            ceiling = max_output_tokens * model_response_count
            if usage.output_tokens > ceiling:
                raise OutputExceedsRequestCeilingRefused(
                    f"invocation {invocation_id!r} reports "
                    f"{usage.output_tokens} output tokens against a ceiling of "
                    f"{max_output_tokens} per request x {model_response_count} "
                    f"responses = {ceiling}; the provider cannot return more "
                    "than the caller allowed, so one of the three figures is "
                    "wrong and storing the row would stop the bounded "
                    "reduction of section 2.4 being a bound"
                )

        txn.execute(
            """
            UPDATE ai_invocation
               SET adapter_version      = :adapter_version,
                   usage_status         = :usage_status,
                   output_tokens        = :output_tokens,
                   input_tokens         = :input_tokens,
                   cache_read_tokens    = :cache_read_tokens,
                   model_response_count = :model_response_count,
                   attempt_count        = :attempt_count,
                   finished_at_ms       = :finished_at_ms
             WHERE invocation_id = :invocation_id
               AND finished_at_ms IS NULL
            """,
            {
                "invocation_id": invocation_id,
                # The version that PARSED the usage, which is what the three
                # token figures are qualified by. In every non-rolling-deploy
                # case it is the version that issued the request; when it is
                # not, the report's adapter_versions set (section 6) is what
                # makes the change visible, and it can only do that if the row
                # names the translation that actually happened.
                "adapter_version": usage.adapter_version,
                "usage_status": usage.usage_status,
                "output_tokens": usage.output_tokens,
                "input_tokens": usage.input_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "model_response_count": model_response_count,
                "attempt_count": attempt_count,
                "finished_at_ms": finished_at_ms,
            },
        )


def read_invocation(
    connection: sqlite3.Connection,
    invocation_id: str,
) -> Mapping[str, Any] | None:
    """One invocation's row, or ``None`` if the id was never started.

    A single-row read, deliberately not an aggregate: coverage, the imputations
    and the ``unbounded_missing`` itemisation are the report's arithmetic and
    the report is a separate, read-only instrument (``D-0040``). This exists so
    that a caller -- and the suite -- can see what was recorded without
    hand-writing SQL against a table it does not own.
    """

    _require_identifier("invocation_id", invocation_id)
    cursor = connection.execute(
        "SELECT * FROM ai_invocation WHERE invocation_id = ?",
        (invocation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    names = [column[0] for column in cursor.description]
    return MappingProxyType(dict(zip(names, row)))
