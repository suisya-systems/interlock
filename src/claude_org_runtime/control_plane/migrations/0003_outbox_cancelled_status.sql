-- ==========================================================================
--  0003 -- 'cancelled': a TERMINAL outbox status for a message nobody wants
--
--  The defect this closes. Closing a gate (withdrawn, expired, subject_gone,
--  superseded) left the relay it had enqueued sitting at 'pending' in outbox,
--  because the status vocabulary of 0001 had no state meaning "this message is
--  no longer wanted". Two consequences, both real:
--
--    (a) a delivery worker reading the outbox is still instructed to present a
--        withdrawn question, or to forward an answer to a gate that is closed;
--    (b) stalled_relays() (production-schema.md section 9.6) names that relay
--        for as long as the database lives, with an age that grows without
--        bound -- which is exactly the "alarms forever" failure that section
--        9.4's subject_gone outcome exists to end, reproduced one table over.
--
--  So the vocabulary gains a fourth status and gate closure retires the relay
--  in the same transaction as the closure (gates.py close_gate).
--
--  WHY A TABLE REBUILD. The vocabulary is a CHECK constraint, and SQLite has
--  no ALTER TABLE that adds to, drops or replaces a CHECK: the only supported
--  way to change one is the 12-step rebuild of the SQLite documentation's
--  "Making Other Kinds Of Table Schema Changes" -- create the new shape under
--  a temporary name, copy every row, drop the old table, rename, then recreate
--  the indexes and triggers, which are dropped with the table. Nothing cheaper
--  exists that is not PRAGMA writable_schema, which edits the schema behind
--  SQLite's back and is refused here: a database whose sqlite_schema was
--  hand-patched cannot be told from a corrupt one afterwards, and R3 says
--  corrupt state is refused, never quietly carried.
--
--  WHERE THE FOREIGN KEYS WENT. Three tables carry REFERENCES outbox(message_id)
--  -- event_consumption, gate_transition and gate_relay -- so between DROP
--  TABLE outbox and the rename below, their rows point at nothing. Step 1 of
--  the documented procedure is PRAGMA foreign_keys = OFF, which is a NO-OP
--  inside a transaction, and every step here runs inside one. So the pragma is
--  issued by migrator._apply_pending around the whole migration instead, and
--  each step ends with a whole-database PRAGMA foreign_key_check inside its own
--  transaction -- a WIDER check than the per-statement enforcement it replaces,
--  and one this step is verified by like every other. PRAGMA defer_foreign_keys
--  was tried first and does not work here: DROP TABLE increments the deferred
--  violation counter once per orphaned child row, and bringing the parent back
--  by ALTER TABLE ... RENAME does not decrement it, so the COMMIT fails over
--  rows that are in fact present. That was measured, not assumed.
--
--  WHAT IS DELIBERATELY NOT DONE. No cancelled_at_ms column is added. The
--  timestamp of a cancellation is already durable twice over -- gate.closed_at_ms
--  and the gate_closed / gate_expired spine event that commits with it -- and a
--  third copy in a third table is a fact that can disagree with the other two.
--  Adding a column is also a schema decision beyond the one that was decided;
--  a later step can add it if a delivery-side cancellation (one with no gate
--  behind it) ever needs its own clock.
-- ==========================================================================

-- --------------------------------------------------------------------------
-- The new shape. Carried CHARACTER FOR CHARACTER from 0001 except for the
-- three lines called out below: a rebuild that silently re-authors the table
-- it is rebuilding is how a constraint disappears without a decision.
-- --------------------------------------------------------------------------
CREATE TABLE outbox_rebuilt_0003 (
    message_id       TEXT    PRIMARY KEY,
    run_id           TEXT    REFERENCES run(run_id),
    recipient        TEXT    NOT NULL,
    payload          TEXT    NOT NULL,
    dedup_key        TEXT    NOT NULL,
    status           TEXT    NOT NULL,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    writer_epoch     INTEGER,
    enqueued_at_ms   INTEGER NOT NULL,
    delivered_at_ms  INTEGER,
    acked_at_ms      INTEGER,

    CHECK (typeof(message_id) = 'text' AND typeof(dedup_key) = 'text'),
    CHECK (typeof(retry_count) = 'integer' AND typeof(enqueued_at_ms) = 'integer'),
    CHECK (writer_epoch IS NULL OR typeof(writer_epoch) = 'integer'),
    CHECK (delivered_at_ms IS NULL OR typeof(delivered_at_ms) = 'integer'),
    CHECK (acked_at_ms IS NULL OR typeof(acked_at_ms) = 'integer'),
    CHECK (length(message_id) > 0),
    CHECK (length(recipient) > 0),
    CHECK (length(dedup_key) > 0),

    -- CHANGED (1 of 3): the widened vocabulary.
    CHECK (status IN ('pending', 'delivered', 'acked', 'cancelled')),

    CHECK (retry_count >= 0),
    CHECK (writer_epoch IS NULL OR writer_epoch > 0),

    -- CHANGED (2 of 3): 0001 had
    --     CHECK ((status IN ('delivered', 'acked')) = (delivered_at_ms IS NOT NULL))
    -- which is an IF AND ONLY IF, and 'cancelled' breaks the "only if" half in
    -- one direction only: a relay cancelled after it was sent keeps its
    -- delivered_at_ms, so status would be 'cancelled' with the timestamp set.
    -- Keeping the constraint for the three statuses it can still speak about,
    -- and saying nothing about 'cancelled', is deliberate: cancellation is
    -- terminal but it is NOT an erasure, so a cancelled row may carry a
    -- delivery timestamp (cancelled after delivery) or not (cancelled while
    -- pending), and both are true statements about what happened. The one
    -- thing still forbidden everywhere is a 'pending' row that claims a
    -- delivery, which the CASE keeps refusing.
    CHECK (CASE status
             WHEN 'pending'   THEN delivered_at_ms IS NULL
             WHEN 'delivered' THEN delivered_at_ms IS NOT NULL
             WHEN 'acked'     THEN delivered_at_ms IS NOT NULL
             WHEN 'cancelled' THEN 1
           END),

    -- CHANGED (3 of 3): unchanged in text, load-bearing in a new way. Because
    -- acked_at_ms is set if and only if status = 'acked', a cancelled row can
    -- never carry an ack -- which is the schema's own statement of the rule
    -- close_gate implements: an ACKED relay is never cancelled. The answer
    -- arrived; retiring the row that carries it would delete the evidence that
    -- the stage advance in section 9.5 is justified by.
    CHECK ((status = 'acked') = (acked_at_ms IS NOT NULL)),

    CHECK (acked_at_ms IS NULL OR acked_at_ms >= delivered_at_ms),
    CHECK (delivered_at_ms IS NULL OR delivered_at_ms >= enqueued_at_ms)
);

-- Every column, by name. SELECT * would bind this copy to the column ORDER of
-- whatever shape happens to be on disk, and a step whose meaning depends on
-- column order is a step that stops meaning the same thing after any later
-- rebuild.
INSERT INTO outbox_rebuilt_0003
    (message_id, run_id, recipient, payload, dedup_key, status, retry_count,
     writer_epoch, enqueued_at_ms, delivered_at_ms, acked_at_ms)
SELECT message_id, run_id, recipient, payload, dedup_key, status, retry_count,
       writer_epoch, enqueued_at_ms, delivered_at_ms, acked_at_ms
  FROM outbox;

-- outbox_rows_are_never_deleted guards rows, and DROP TABLE is not a DELETE:
-- BEFORE DELETE triggers do not fire for it. The rows are not being deleted in
-- any case -- they were copied above and come back under the same name below,
-- with the same message_id, retry_count, delivered_at_ms and acked_at_ms.
DROP TABLE outbox;

ALTER TABLE outbox_rebuilt_0003 RENAME TO outbox;

-- --------------------------------------------------------------------------
-- The triggers and the index, recreated. Dropping the table dropped all of
-- them, so anything not restored here is a constraint silently repealed by a
-- migration -- the exact failure the checksum discipline of section 3.2 exists
-- to make impossible to do twice.
-- --------------------------------------------------------------------------

-- Carried verbatim from 0001.
CREATE TRIGGER outbox_retry_count_is_monotonic
BEFORE UPDATE OF retry_count ON outbox
WHEN NEW.retry_count < OLD.retry_count
BEGIN
    SELECT RAISE(ABORT, 'outbox retry_count must not decrease');
END;

-- CHANGED. The status CHECKs constrain the row that results, not the step that
-- got there, so without this an UPDATE could walk the lifecycle backwards and
-- erase the delivery evidence the relay-gap and reconcile passes are read out
-- of. 0001 enforced that by ranking the three statuses and refusing any step
-- that lowered the rank -- a TOTAL ORDER. The vocabulary is no longer totally
-- ordered: 'acked' and 'cancelled' are both terminal and neither is reachable
-- from the other, so what is enforced now is a LATTICE, written out edge by
-- edge rather than as a comparison of ranks:
--
--     pending   -> delivered | cancelled
--     delivered -> acked     | cancelled
--     acked     -> (nothing)
--     cancelled -> (nothing)
--
-- The evidence argument survives the change intact, because a cancellation is
-- a terminal STATUS CHANGE and never an ERASURE. Nothing about this trigger or
-- the set-once triggers below lets a cancellation clear delivered_at_ms (that
-- is outbox_delivery_is_set_once), decrement retry_count (that is
-- outbox_retry_count_is_monotonic), or touch an ack (acked has no outgoing
-- edge at all, and the acked_at_ms CHECK above forbids the value on a
-- cancelled row anyway). So a cancelled relay still says, truthfully and
-- forever, that it was sent N times and delivered at T; what it stops saying
-- is "somebody still wants this sent".
--
-- pending -> acked directly is refused, where the 0001 rank comparison had
-- admitted it. That is a tightening and it is intended: an ack for a message
-- that was never marked delivered is either a lost delivered write or an ack
-- for something that was never sent, and both should surface here rather than
-- become a row asserting an ack with no send behind it. The delivered_at_ms
-- CHECK already made the one-statement form (set status, delivered_at_ms and
-- acked_at_ms at once) representable, and it stays representable -- what is
-- refused is arriving at 'acked' from a row still recorded as 'pending'.
CREATE TRIGGER outbox_status_is_forward_only
BEFORE UPDATE OF status ON outbox
WHEN NEW.status <> OLD.status
 AND NOT (   (OLD.status = 'pending'   AND NEW.status IN ('delivered', 'cancelled'))
          OR (OLD.status = 'delivered' AND NEW.status IN ('acked', 'cancelled')))
BEGIN
    SELECT RAISE(ABORT, 'outbox status walks pending -> delivered -> acked, or is cancelled from pending or delivered; acked and cancelled are terminal');
END;

-- Carried verbatim from 0001, and load-bearing for the paragraph above: this
-- is what makes a cancellation unable to erase the delivery it is cancelling.
CREATE TRIGGER outbox_delivery_is_set_once
BEFORE UPDATE ON outbox
WHEN OLD.delivered_at_ms IS NOT NULL
 AND (NEW.delivered_at_ms IS NULL OR NEW.delivered_at_ms <> OLD.delivered_at_ms)
BEGIN
    SELECT RAISE(ABORT, 'a delivered message is delivered once');
END;

-- Carried verbatim from 0001. A resend is a new attempt on the same message
-- identity, so the identity a delivery was deduplicated under may not be
-- rewritten under a live row. The ack is recorded against this identity, so
-- vacating it by rename would let a second row take the identity of a message
-- that was already acked -- and gate_relay.message_id points here, so a rename
-- would also silently re-aim a gate's relay at somebody else's message.
CREATE TRIGGER outbox_message_id_is_frozen
BEFORE UPDATE OF message_id ON outbox
WHEN NEW.message_id <> OLD.message_id
BEGIN
    SELECT RAISE(ABORT, 'an outbox row keeps the message identity it was enqueued under');
END;

-- Carried verbatim from 0001.
CREATE TRIGGER outbox_dedup_key_is_frozen
BEFORE UPDATE OF dedup_key ON outbox
WHEN NEW.dedup_key <> OLD.dedup_key
BEGIN
    SELECT RAISE(ABORT, 'an outbox row keeps the dedup key it was enqueued with');
END;

-- Carried verbatim from 0001.
CREATE TRIGGER outbox_ack_is_set_once
BEFORE UPDATE ON outbox
WHEN OLD.acked_at_ms IS NOT NULL
 AND (NEW.acked_at_ms IS NULL OR NEW.acked_at_ms <> OLD.acked_at_ms)
BEGIN
    SELECT RAISE(ABORT, 'an acked message is acked once');
END;

-- Carried verbatim from 0001. Freezing the identity protects it only while the
-- row exists: deleting an acked row vacates its message_id, and the same
-- identity can then be enqueued and delivered a second time. Q-0006
-- (retention) is open and is not answered by a DELETE -- nor by 'cancelled',
-- which retires a message without removing a byte of what it recorded.
CREATE TRIGGER outbox_rows_are_never_deleted
BEFORE DELETE ON outbox
BEGIN
    SELECT RAISE(ABORT, 'outbox rows are delivery evidence and are never deleted');
END;

-- CHANGED. Recovery's first question after a kill is still "what is enqueued
-- and unfinished?", and it is still the reconcile pass's orphaned-outbox query
-- (section 5.6) -- but 'unfinished' is now 'pending' or 'delivered' and not
-- merely "not acked". A cancelled message is finished: nobody is going to send
-- it, so an index that kept matching it would keep feeding it to the very
-- passes this step exists to stop it from alarming in, and half the defect
-- would remain open. The predicate is spelled as the positive IN list rather
-- than status NOT IN ('acked', 'cancelled') because SQLite may use a partial
-- index only when the query's WHERE contains the index's own predicate as a
-- term: the two readers (events.ORPHANED_OUTBOX_SQL and gates.stalled_relays)
-- carry this exact text, and the plan test asserts the index is used.
CREATE INDEX outbox_undelivered ON outbox(enqueued_at_ms)
    WHERE status IN ('pending', 'delivered');
