"""S7 -- the one action handler, and the mechanism it names.

.. warning::

   **Spike scaffold, throwaway by default (D-0026).** At spike time ``Q-0001``
   was open and nothing here answered it; D-0029 has since resolved it (the
   per-item single-writer table is docs/production-schema.md section 4.2, the
   DDL is ``control_plane/migrations/0001_initial.sql``), but this module still
   sits on the throwaway S5 schema and was never updated to the production one.
   The durable half of Issue ``#14`` is the suite.

Issue ``#14`` asks for **one** handler, and is specific about what makes it
count:

    One action handler that **declares** its mechanism: either (1) a
    destination-supported idempotency key, or (2) transactional commit of effect
    and record together. Where neither is achievable for a given action, the gap
    is explicit and the action requires a **human gate** (D-0004) rather than
    automatic recovery. If the chosen handler turns out to be such a case, say
    so and pick a different one -- **do not paper over it**.

So the choice of handler is itself part of the deliverable, and it is recorded
here rather than in a commit message.

**What was chosen, and why.** :class:`NotifyDestinationHandler` declares
``'destination_idempotency_key'``. Its counterparty is a
:class:`~claude_org_runtime.control_plane.destination.Destination`, which
deduplicates on the key and keeps its own record of the effect. That is the only
one of the three mechanisms whose evidence satisfies ``ACCEPTANCE.md`` section 2
without argument: *a case that asserts exactly-once for an external effect using
only our own rows does not pass*, and this handler's exactly-once claim is read
back out of a store this process does not write transactionally.

**What was rejected, and why -- because the rejection is the interesting half.**

*Transactional commit of effect and record together* (mechanism 2) was the
obvious first candidate, and it is genuinely the stronger mechanism where it
applies: it collapses the ambiguous window instead of tolerating it. It was not
chosen because a handler demonstrating it truthfully would need an effect that
lives in the **same** transaction as its record -- that is, an effect inside our
own SQLite. An effect internal to the control plane is a fine thing to have, but
using it to discharge item 4 would be exactly the reading of the criterion the
gate rules out: the mid-flight kill it is meant to survive is the one where the
effect is external and its result was not recorded, and an effect that commits
with its own record cannot be killed in that window by construction. It would
pass by not being the case under test.

That reasoning is now enforced rather than merely written down:
:class:`~claude_org_runtime.control_plane.outbox.HandlerRegistry` **refuses** a
handler declaring ``'transactional_with_record'`` outright, because
:class:`~claude_org_runtime.control_plane.outbox.Outbox` commits the action row
before calling the handler and hands it no transaction to commit an effect
inside. A handler could declare the mechanism and be admitted while the
execution path could not possibly provide it -- which is the same undeclared-
guarantee failure the registration check exists to prevent, arriving through the
one branch that looks declared. The mechanism stays in the vocabulary, since it
is ``ACCEPTANCE.md``'s and the DDL's rather than this module's; what is refused
is claiming it here.

*A human gate* (D-0004) was not chosen because for this action it would be
false. The gate is for actions where **neither** mechanism is achievable, and
the honesty the issue asks for cuts both ways: claiming a human gate for an
effect whose destination does support an idempotency key would understate what
is provable just as badly as claiming exactly-once for one that does not.
:class:`HumanGatedHandler` therefore exists as a **declaration**, not as a
second delivery path -- see its own docstring.

**The handler is deliberately thin.** Almost everything an outbox handler might
be tempted to do -- the retry count, the fence, the pending action row, the
delivered transition -- belongs to :class:`~claude_org_runtime.control_plane.outbox.Outbox`,
which does it in an order chosen so the kill windows are real. What is left here
is the effect and the key it is applied under, which is precisely the part that
differs between one handler and the next.
"""

from __future__ import annotations

from .destination import DeliveryReceipt, Destination, DestinationRefusal
from .outbox import ActionHandler, HandlerRegistry, OutboxMessage

__all__ = [
    "HUMAN_GATED_RECIPIENT",
    "NOTIFY_RECIPIENT",
    "HumanGatedHandler",
    "NotifyDestinationHandler",
    "spike_registry",
]

#: The ``outbox.recipient`` value :class:`NotifyDestinationHandler` serves.
#:
#: A recipient name, not a role name. Which component sends to which recipient
#: was the per-item writer assignment ``Q-0001`` left open at spike time (S5
#: kept every role out of the DDL for the same reason), and D-0029 has since
#: answered it in the production schema -- but this spike scaffold was never
#: migrated onto it, so the recipient name here is still a name, not a role.
NOTIFY_RECIPIENT = "external-notify"

#: The recipient :class:`HumanGatedHandler` serves. See that class: it delivers
#: nothing, on purpose.
HUMAN_GATED_RECIPIENT = "human-gated-effect"


class NotifyDestinationHandler(ActionHandler):
    """The spike's one real handler. Mechanism: a destination idempotency key.

    The effect is applied to a
    :class:`~claude_org_runtime.control_plane.destination.Destination` under a
    key the destination deduplicates. Everything the exactly-once claim rests on
    is therefore *the destination's*: this handler makes no attempt to decide
    whether a previous attempt landed, because deciding that is the thing
    ``ACCEPTANCE.md`` section 2 says cannot be done from our side.

    Concretely, the property that matters is that :meth:`apply` is safe to call
    an unbounded number of times with the same key. It is called again after a
    lost ack, again after a kill between the effect and its record, and again
    after a re-enqueue of the same dedup key -- and the destination's effect
    count stays one across all of them.
    """

    recipient = NOTIFY_RECIPIENT
    action_kind = "notify"
    exactly_once_mechanism = "destination_idempotency_key"

    def __init__(self, destination: Destination) -> None:
        if not isinstance(destination, Destination):
            # Declaring 'destination_idempotency_key' without a counterparty
            # that deduplicates one is declaring a guarantee with nothing behind
            # it, so it is refused where the claim is made rather than where it
            # would first be relied on.
            raise TypeError(
                "NotifyDestinationHandler declares "
                "'destination_idempotency_key' and so requires a Destination "
                "that deduplicates one"
            )
        self._destination = destination

    @property
    def destination(self) -> Destination:
        return self._destination

    def apply(
        self,
        message: OutboxMessage,
        idempotency_key: str,
        fencing_token: int | None = None,
        fence_scope: str | None = None,
    ) -> DeliveryReceipt:
        # The token is handed to the destination rather than checked here.
        # Checking it on this side would prove nothing: the window it closes is
        # the one where *this process* was paused past its lease, and a paused
        # process cannot notice that it was paused. Only the counterparty is
        # still running (ACCEPTANCE.md section 2: *external destinations must
        # reject a stale token where they can enforce it*).
        receipt = self._destination.apply(
            idempotency_key, message.payload, fencing_token, fence_scope
        )
        if receipt.payload_conflict:
            # The key is already bound to a different payload. The destination
            # applied nothing, which is right -- an idempotency key names an
            # effect, so the same key with new content is a dedup-key collision
            # rather than a new effect. Recording it as delivered would let the
            # collision pass as an exactly-once success, which is the failure
            # mode this whole module is built to make impossible.
            raise DestinationRefusal(
                f"{idempotency_key!r} is already applied at "
                f"{receipt.destination!r} under a different payload; the "
                f"dedup key of {message.message_id!r} collides with an "
                "effect that is not this one"
            )
        return receipt


class HumanGatedHandler(ActionHandler):
    """A declaration that neither mechanism is achievable (D-0004).

    This is **not** a second delivery path and it is not a fallback. It exists
    so that the third branch of ``ACCEPTANCE.md`` section 2's clause is
    expressible in code and provable by a test, instead of surviving only as a
    sentence in a document that a future handler author will not read.

    :meth:`apply` raises. The gap is meant to be visible: an action whose
    destination supports no idempotency key and whose effect cannot commit with
    its record is one a human decides about, and
    :class:`~claude_org_runtime.control_plane.outbox.Outbox` refuses to advance
    it -- it records the pending action and raises
    :class:`~claude_org_runtime.control_plane.outbox.HumanGateRequired` before
    any effect is attempted. Automatic recovery here would be the papering-over
    Issue ``#14`` names.
    """

    recipient = HUMAN_GATED_RECIPIENT
    action_kind = "human_gated"
    exactly_once_mechanism = "human_gate"

    def apply(
        self,
        message: OutboxMessage,
        idempotency_key: str,
        fencing_token: int | None = None,
        fence_scope: str | None = None,
    ) -> DeliveryReceipt:
        raise AssertionError(
            "a human-gated action is never applied automatically (D-0004); "
            "Outbox.attempt raises HumanGateRequired before reaching a handler"
        )


def spike_registry(destination: Destination) -> HandlerRegistry:
    """The spike's handler set: one that delivers, one that declares it cannot.

    Assembled in a function rather than at import time so that a test can build
    an independent registry -- a module-level singleton would make "a handler
    that fails registration is not registered" untestable, and that is one of
    the acceptance criteria.
    """

    registry = HandlerRegistry()
    registry.register(NotifyDestinationHandler(destination))
    registry.register(HumanGatedHandler())
    return registry
