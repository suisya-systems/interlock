-- ==========================================================================
--  S5 -- THE SPIKE SQLite SCHEMA SLICE
--
--  *** THIS IS A SPIKE SCHEMA. NO MIGRATION PATH IS PROMISED FROM IT. ***
--
--  D-0026: the durable output of the Agent View spike is the SessionProvider
--  interface (S1) and the tests. Every implementation the spike produces --
--  this schema named explicitly among them -- is THROWAWAY BY DEFAULT and may
--  be promoted into the real implementation only by a new D- entry that says
--  so. Being depended on by S6/S7, having survived a gate run, or simply
--  existing promotes nothing.
--
--  What that means concretely, and it is not a formality:
--
--    * Q-0001 (the real DDL, keys, indices, per-item single-writer table and
--      migration policy) STAYS OPEN. Nothing below is an answer to it. The
--      failure mode D-0026 was written against is a spike schema becoming
--      *the* schema by inertia, so that Q-0001 gets answered by accident
--      rather than by decision.
--    * There is NO migration. A database written by one revision of this file
--      is not upgraded by the next one; it is REFUSED (see the user_version
--      check in schema.py). Refusing is the point: a migration path would be
--      the first half of the promotion nobody decided on.
--    * A downstream test that needs an answer this schema deliberately does
--      not give -- incident collapse semantics (Q-0002), per-item writer
--      assignment (Q-0001) -- PARAMETERISES it. ACCEPTANCE.md section 2
--      requires that explicitly for both.
--
--  Scope: six tables -- run, session, lease, outbox, incident, action -- the
--  minimum for ACCEPTANCE.md section 1 items 2, 4, 5 and 6, and nothing those
--  items do not exercise. It is not the full Q-0001 DDL and does not aspire to
--  be one.
--
--  Two properties hold across the whole file:
--
--  D-0001, state is reconstructable by query from SQLite alone. Every column
--  below exists because some recovery query needs it after a mid-flight kill.
--  Nothing that a recovering process must know is left to live only in a
--  process: no in-memory index, no "the supervisor remembers which sessions it
--  started", no cache that a restart would have to be told about.
--
--  Time is the caller's. Every timestamp is INTEGER milliseconds since the
--  Unix epoch, UTC, NOT NULL, and carries NO DEFAULT. ACCEPTANCE.md section 2
--  injects clock skew across the lease expiry boundary; a column defaulted to
--  the database's own clock would quietly make that case untestable, and a
--  recovering process would inherit a timestamp it never chose.
-- ==========================================================================

-- --------------------------------------------------------------------------
-- run -- the unit of work. D-0001 names it as source-of-truth state; item 4
-- resumes from it.
--
-- status is deliberately unconstrained text. Which run statuses exist, and
-- which component may move a run between them, is exactly the per-item writer
-- assignment Q-0001 leaves open; a CHECK enumerating them here would answer it
-- in DDL.
-- --------------------------------------------------------------------------
CREATE TABLE run (
    run_id         TEXT    PRIMARY KEY,
    status         TEXT    NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,

    -- Types are asserted by CHECK rather than by a STRICT table: STRICT
    -- arrived in SQLite 3.37 and this project supports Python 3.10, whose
    -- bundled library is older on some platforms. A timestamp column that
    -- silently accepts a string is a recovery query that silently sorts wrong.
    CHECK (typeof(run_id) = 'text' AND typeof(status) = 'text'),
    CHECK (typeof(created_at_ms) = 'integer' AND typeof(updated_at_ms) = 'integer'),
    CHECK (length(run_id) > 0),
    CHECK (length(status) > 0),
    CHECK (updated_at_ms >= created_at_ms)
);

-- --------------------------------------------------------------------------
-- session -- the session<->run binding, persisted at spawn. This is item 2's
-- durable record: after a kill at any injection point, re-identification must
-- yield EXACTLY ONE session for the run, and a second writer must be refused
-- rather than admitted.
--
-- That "exactly one" is enforced by the database, not by the discipline of
-- whoever writes to it -- see session_one_active_binding_per_run below. A
-- check-then-insert in application code leaves precisely the window item 2
-- injects into.
--
-- provider_state carries the PROVIDER'S OWN state word, uninterpreted, and
-- observation carries R4's could-not-observe case with its reason. Both come
-- straight from SessionReadout (S1): collapsing "could not observe" into an
-- absent state word is the v1 defect R4 records, and enumerating the provider's
-- vocabulary here would put an Agent-View-shaped (or claude -p-shaped)
-- assumption into a provider-neutral store. Conversion to a D-0005 fact state
-- belongs to the detector layer, versioned and fixture-tested.
-- --------------------------------------------------------------------------
CREATE TABLE session (
    session_id          TEXT    PRIMARY KEY,
    run_id              TEXT    NOT NULL REFERENCES run(run_id),
    provider            TEXT    NOT NULL,
    observation         TEXT    NOT NULL,
    provider_state      TEXT,
    observation_reason  TEXT,
    bound_at_ms         INTEGER NOT NULL,
    released_at_ms      INTEGER,

    CHECK (typeof(session_id) = 'text' AND typeof(run_id) = 'text'),
    CHECK (typeof(bound_at_ms) = 'integer'),
    CHECK (released_at_ms IS NULL OR typeof(released_at_ms) = 'integer'),
    CHECK (length(session_id) > 0),
    CHECK (observation IN ('observed', 'unobserved')),
    -- an observed readout carries a state word; an unobserved one carries a
    -- reason instead. Neither may be constructed empty (R4) -- and an empty
    -- string is as empty as a NULL: a reason of '' is the v1 collapse of
    -- "could not observe" into "nothing to say" wearing a different type.
    CHECK (provider_state IS NULL OR length(provider_state) > 0),
    CHECK (observation_reason IS NULL OR length(observation_reason) > 0),
    CHECK ((observation = 'observed')   = (provider_state IS NOT NULL)),
    CHECK ((observation = 'unobserved') = (observation_reason IS NOT NULL)),
    CHECK (released_at_ms IS NULL OR released_at_ms >= bound_at_ms)
);

-- Item 2, enforced: at most one ACTIVE binding per run. A released binding
-- (released_at_ms set) leaves the run free for a new session, so an ordinary
-- stop-then-resume is expressible, while a second live writer for the same run
-- is refused by the database at INSERT time.
CREATE UNIQUE INDEX session_one_active_binding_per_run
    ON session(run_id) WHERE released_at_ms IS NULL;

-- Recovery reads the binding from the run side (item 2's re-identification).
CREATE INDEX session_by_run ON session(run_id);

-- --------------------------------------------------------------------------
-- lease -- exclusion, and the fencing token that makes it real.
--
-- ACCEPTANCE.md section 2 is explicit that expiry discovery alone is not
-- enough: check-then-write leaves a race in which the lease expires between the
-- check and the write. Every protected write must carry the lease epoch and
-- validate it ATOMICALLY as part of the write -- which is why outbox and action
-- rows below carry writer_epoch, and why S6 writes
--
--     UPDATE <table> SET ... WHERE ... AND writer_epoch = :epoch
--       AND EXISTS (SELECT 1 FROM lease
--                    WHERE resource = :resource AND holder = :holder
--                      AND epoch = :epoch AND expires_at_ms > :now_ms)
--
-- as one statement rather than as a check followed by a write.
--
-- One row per resource, never deleted, epoch strictly increasing. A fencing
-- token that can go backwards is not a fence: a returning paused holder would
-- see its own epoch valid again. Both properties are triggers rather than
-- conventions, because the failure they prevent is invisible in the row that
-- results.
--
-- holder is an opaque claimant identity, deliberately not a role. WHICH
-- component may hold WHICH resource is the per-item single-writer table Q-0001
-- leaves open.
-- --------------------------------------------------------------------------
CREATE TABLE lease (
    resource        TEXT    PRIMARY KEY,
    holder          TEXT    NOT NULL,
    epoch           INTEGER NOT NULL,
    acquired_at_ms  INTEGER NOT NULL,
    expires_at_ms   INTEGER NOT NULL,

    CHECK (typeof(resource) = 'text' AND typeof(holder) = 'text'),
    CHECK (typeof(epoch) = 'integer'),
    CHECK (typeof(acquired_at_ms) = 'integer' AND typeof(expires_at_ms) = 'integer'),
    CHECK (length(resource) > 0),
    CHECK (length(holder) > 0),
    CHECK (epoch > 0),
    CHECK (expires_at_ms > acquired_at_ms)
);

-- Two rules, one trigger, and the second is the one that is easy to lose:
--
--   * an epoch never decreases, whatever the update touches; and
--   * a CHANGE OF HOLDER must raise it. A handover written as
--     "UPDATE lease SET holder = ..., expires_at_ms = ..." without naming the
--     epoch hands the replacement the previous holder's token -- and a paused
--     former holder returning with that same token is then indistinguishable
--     from the current one at any destination that validates the token rather
--     than SQLite's idea of who holds it. That is precisely the stale writer
--     the fence exists to reject.
--
-- The trigger is BEFORE UPDATE ON lease rather than BEFORE UPDATE OF epoch: a
-- trigger scoped to the epoch column does not run for the update that omits it,
-- which is the dangerous one. A renewal by the SAME holder keeps its epoch, as
-- it must -- re-acquiring is not what invalidates a token.
CREATE TRIGGER lease_epoch_is_monotonic
BEFORE UPDATE ON lease
WHEN NEW.epoch < OLD.epoch
  OR (NEW.holder <> OLD.holder AND NEW.epoch <= OLD.epoch)
BEGIN
    SELECT RAISE(ABORT, 'a lease epoch never decreases, and a new holder must raise it');
END;

-- Blocking deletion is not enough to keep epochs from restarting: SQLite lets a
-- primary key be updated, so renaming resource 'r' vacates 'r' and the next
-- INSERT takes it at epoch 1 -- the same token reuse the no-delete rule exists
-- to prevent, reached by a different statement.
CREATE TRIGGER lease_resource_is_immutable
BEFORE UPDATE OF resource ON lease
WHEN NEW.resource <> OLD.resource
BEGIN
    SELECT RAISE(ABORT, 'a lease resource is never renamed; its epoch history belongs to it');
END;

-- Releasing a lease is setting expires_at_ms into the past, never deleting the
-- row: a deleted row would let the next acquisition restart the epoch at 1 and
-- hand a returning stale holder a token that validates.
CREATE TRIGGER lease_rows_are_never_deleted
BEFORE DELETE ON lease
BEGIN
    SELECT RAISE(ABORT, 'lease rows are never deleted; expire them instead');
END;

-- --------------------------------------------------------------------------
-- outbox -- item 6's delivery record, and item 5's resend/ack/retry evidence.
--
-- status walks pending -> delivered -> acked and never back. retry_count is
-- durable and monotonic, which is what "restart-surviving, monotonically
-- increasing" in ACCEPTANCE.md section 2 asks a query to be able to show. Ack
-- is set once, so a duplicate or late ack changes nothing -- idempotent by
-- construction rather than by the handler remembering.
--
-- dedup_key is NOT unique here, deliberately. Exactly-once is a property of the
-- EFFECT, and the effect record is `action` (see action_one_effect_per_key). A
-- sender that is killed after writing an outbox row and restarts may legitimately
-- re-enqueue; collapsing those rows in DDL would move S7's delivery policy into
-- the schema.
-- --------------------------------------------------------------------------
CREATE TABLE outbox (
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
    CHECK (status IN ('pending', 'delivered', 'acked')),
    CHECK (retry_count >= 0),
    CHECK (writer_epoch IS NULL OR writer_epoch > 0),
    CHECK ((status IN ('delivered', 'acked')) = (delivered_at_ms IS NOT NULL)),
    CHECK ((status = 'acked') = (acked_at_ms IS NOT NULL)),
    CHECK (acked_at_ms IS NULL OR acked_at_ms >= delivered_at_ms),
    CHECK (delivered_at_ms IS NULL OR delivered_at_ms >= enqueued_at_ms)
);

CREATE TRIGGER outbox_retry_count_is_monotonic
BEFORE UPDATE OF retry_count ON outbox
WHEN NEW.retry_count < OLD.retry_count
BEGIN
    SELECT RAISE(ABORT, 'outbox retry_count must not decrease');
END;

-- The status CHECKs constrain the row that results, not the step that got
-- there, so without these an UPDATE could walk the lifecycle backwards and
-- erase the delivery evidence items 5 and 6 are read out of.
CREATE TRIGGER outbox_status_is_forward_only
BEFORE UPDATE OF status ON outbox
WHEN (CASE NEW.status WHEN 'pending' THEN 0 WHEN 'delivered' THEN 1 ELSE 2 END)
   < (CASE OLD.status WHEN 'pending' THEN 0 WHEN 'delivered' THEN 1 ELSE 2 END)
BEGIN
    SELECT RAISE(ABORT, 'outbox status walks pending -> delivered -> acked, never back');
END;

CREATE TRIGGER outbox_delivery_is_set_once
BEFORE UPDATE ON outbox
WHEN OLD.delivered_at_ms IS NOT NULL
 AND (NEW.delivered_at_ms IS NULL OR NEW.delivered_at_ms <> OLD.delivered_at_ms)
BEGIN
    SELECT RAISE(ABORT, 'a delivered message is delivered once');
END;

-- A resend is a new attempt on the same message identity, so the identity a
-- delivery was deduplicated under may not be rewritten under a live row.
-- The ack is recorded against this identity, so vacating it by rename would let
-- a second row take the identity of a message that was already acked.
CREATE TRIGGER outbox_message_id_is_frozen
BEFORE UPDATE OF message_id ON outbox
WHEN NEW.message_id <> OLD.message_id
BEGIN
    SELECT RAISE(ABORT, 'an outbox row keeps the message identity it was enqueued under');
END;

CREATE TRIGGER outbox_dedup_key_is_frozen
BEFORE UPDATE OF dedup_key ON outbox
WHEN NEW.dedup_key <> OLD.dedup_key
BEGIN
    SELECT RAISE(ABORT, 'an outbox row keeps the dedup key it was enqueued with');
END;

CREATE TRIGGER outbox_ack_is_set_once
BEFORE UPDATE ON outbox
WHEN OLD.acked_at_ms IS NOT NULL
 AND (NEW.acked_at_ms IS NULL OR NEW.acked_at_ms <> OLD.acked_at_ms)
BEGIN
    SELECT RAISE(ABORT, 'an acked message is acked once');
END;

-- Recovery's first question after a kill: what is enqueued and unfinished?
CREATE INDEX outbox_undelivered ON outbox(enqueued_at_ms) WHERE status <> 'acked';

-- --------------------------------------------------------------------------
-- incident -- D-0007's persisted packet, reduced to what items 4 and 5
-- exercise. The AI is startable statelessly from a row here (D-0003, D-0007),
-- so the packet is in the row and not in anyone's context.
--
-- dedup_key and retry_count are REQUIRED and non-nullable (D-0007).
--
-- What is NOT here is as deliberate as what is. Q-0002 -- whether a repeated
-- incident condition increments retry_count on the existing incident or opens a
-- linked one, and what the re-notification window is in absolute time -- is
-- OPEN, so:
--
--   * dedup_key is indexed but NOT unique. A UNIQUE constraint would force the
--     increment-in-place rule.
--   * related_incident_id exists and is nullable, so the linked-incident rule
--     is expressible too. It is a plain self-reference with no semantics
--     attached; it does not mean "collapsed into".
--   * no window, no re-notification period, no time bucket appears anywhere in
--     this file. Q-0003 has to settle tolerable detection latency first.
--
-- Both rules are therefore expressible and neither is enforced. ACCEPTANCE.md
-- section 2 requires downstream tests to parameterise the choice; a schema that
-- had picked one would have made that impossible.
--
-- fact_state is unconstrained text carrying D-0005's watcher fact plus the
-- detector version that produced it. The closed set lives in DECISIONS.md; a
-- CHECK duplicating it here would make a D- entry that extends the set a schema
-- change to a schema that promises no migration.
-- --------------------------------------------------------------------------
CREATE TABLE incident (
    incident_id          TEXT    PRIMARY KEY,
    run_id               TEXT    REFERENCES run(run_id),
    session_id           TEXT    REFERENCES session(session_id),
    fact_state           TEXT    NOT NULL,
    detector_version     TEXT    NOT NULL,
    dedup_key            TEXT    NOT NULL,
    retry_count          INTEGER NOT NULL DEFAULT 0,
    known_pattern        TEXT,
    elapsed_ms           INTEGER,
    evidence_refs        TEXT    NOT NULL DEFAULT '[]',
    recent_transitions   TEXT    NOT NULL DEFAULT '[]',
    previous_assessment  TEXT,
    previous_action_id   TEXT    REFERENCES action(action_id),
    related_incident_id  TEXT    REFERENCES incident(incident_id),
    created_at_ms        INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL,
    resolved_at_ms       INTEGER,

    CHECK (typeof(incident_id) = 'text' AND typeof(dedup_key) = 'text'),
    CHECK (typeof(retry_count) = 'integer'),
    CHECK (typeof(created_at_ms) = 'integer' AND typeof(updated_at_ms) = 'integer'),
    CHECK (elapsed_ms IS NULL OR typeof(elapsed_ms) = 'integer'),
    CHECK (resolved_at_ms IS NULL OR typeof(resolved_at_ms) = 'integer'),
    CHECK (length(incident_id) > 0),
    CHECK (length(fact_state) > 0),
    CHECK (length(detector_version) > 0),
    CHECK (length(dedup_key) > 0),
    CHECK (retry_count >= 0),
    CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CHECK (related_incident_id IS NULL OR related_incident_id <> incident_id),
    CHECK (updated_at_ms >= created_at_ms),
    CHECK (resolved_at_ms IS NULL OR resolved_at_ms >= created_at_ms)
);

CREATE TRIGGER incident_retry_count_is_monotonic
BEFORE UPDATE OF retry_count ON incident
WHEN NEW.retry_count < OLD.retry_count
BEGIN
    SELECT RAISE(ABORT, 'incident retry_count must not decrease');
END;

-- Non-unique on purpose: see the Q-0002 note above.
CREATE INDEX incident_by_dedup_key ON incident(dedup_key);

-- D-0001: "work resumes from unresolved incidents" is one query.
CREATE INDEX incident_unresolved ON incident(created_at_ms) WHERE resolved_at_ms IS NULL;

-- --------------------------------------------------------------------------
-- action -- the side-effect record. Item 4 asks that every side effect be
-- applied exactly once, "evidenced by an idempotency/dedup record rather than
-- by absence of a visible duplicate", and this table is that record.
--
-- exactly_once_mechanism is required and enumerated because ACCEPTANCE.md
-- section 2 requires every action handler to NAME which of the two mechanisms
-- it uses -- a destination-supported idempotency key, or committing the effect
-- transactionally with its durable record -- or to declare that neither is
-- achievable, in which case the action needs a human gate (D-0004). The
-- enumeration is that clause, not a policy of this schema: SQLite cannot tell
-- "the effect completed" from "the effect never started" when the process died
-- in between, so a row that does not say how it is made exactly-once is a row
-- claiming something it cannot support.
--
-- Refusals are recorded, never dropped. ACCEPTANCE.md section 2 requires the
-- rejection of a stale writer to be itself durable; a refused attempt is an
-- action row in status 'refused' carrying its reason (and, where the refusal
-- warrants triage, an incident row referencing it).
-- --------------------------------------------------------------------------
CREATE TABLE action (
    action_id               TEXT    PRIMARY KEY,
    run_id                  TEXT    REFERENCES run(run_id),
    incident_id             TEXT    REFERENCES incident(incident_id),
    kind                    TEXT    NOT NULL,
    idempotency_key         TEXT    NOT NULL,
    exactly_once_mechanism  TEXT    NOT NULL,
    status                  TEXT    NOT NULL,
    refusal_reason          TEXT,
    result                  TEXT,
    writer_epoch            INTEGER,
    created_at_ms           INTEGER NOT NULL,
    applied_at_ms           INTEGER,

    CHECK (typeof(action_id) = 'text' AND typeof(idempotency_key) = 'text'),
    CHECK (typeof(created_at_ms) = 'integer'),
    CHECK (applied_at_ms IS NULL OR typeof(applied_at_ms) = 'integer'),
    CHECK (writer_epoch IS NULL OR typeof(writer_epoch) = 'integer'),
    CHECK (length(action_id) > 0),
    CHECK (length(kind) > 0),
    CHECK (length(idempotency_key) > 0),
    CHECK (exactly_once_mechanism IN (
        'destination_idempotency_key',  -- the destination deduplicates the key
        'transactional_with_record',    -- effect and record commit together
        'human_gate'                    -- neither is achievable (D-0004)
    )),
    CHECK (status IN ('pending', 'applied', 'refused')),
    CHECK ((status = 'applied') = (applied_at_ms IS NOT NULL)),
    CHECK (refusal_reason IS NULL OR length(refusal_reason) > 0),
    CHECK ((status = 'refused') = (refusal_reason IS NOT NULL)),
    CHECK (writer_epoch IS NULL OR writer_epoch > 0),
    CHECK (applied_at_ms IS NULL OR applied_at_ms >= created_at_ms)
);

-- One effect per idempotency key. Refused attempts are excluded so that a
-- rejected stale writer can still be recorded -- repeatedly, if it keeps
-- returning -- without the record itself becoming the thing that admits a
-- second effect.
CREATE UNIQUE INDEX action_one_effect_per_key
    ON action(idempotency_key) WHERE status <> 'refused';

-- Finding the key unique among *current* values is not the same as one effect
-- per key: rewriting an applied action's key vacates it, and the next writer
-- takes it as though nothing had happened. The key is frozen for the row's
-- lifetime, which is what makes the unique index durable evidence rather than
-- a snapshot.
CREATE TRIGGER action_idempotency_key_is_frozen
BEFORE UPDATE OF idempotency_key ON action
WHEN NEW.idempotency_key <> OLD.idempotency_key
BEGIN
    SELECT RAISE(ABORT, 'an action keeps the idempotency key it was recorded with');
END;

-- A refusal is durable evidence that a writer was rejected (ACCEPTANCE.md
-- section 2). A refused row that can be moved back to 'pending' is a rejected
-- attempt that becomes executable again -- and the record of the rejection
-- disappears in the same statement.
CREATE TRIGGER action_refusal_is_terminal
BEFORE UPDATE OF status ON action
WHEN OLD.status = 'refused' AND NEW.status <> 'refused'
BEGIN
    SELECT RAISE(ABORT, 'a refused action stays refused; record a new attempt instead');
END;

CREATE TRIGGER action_apply_is_set_once
BEFORE UPDATE ON action
WHEN OLD.applied_at_ms IS NOT NULL
 AND (NEW.applied_at_ms IS NULL OR NEW.applied_at_ms <> OLD.applied_at_ms)
BEGIN
    SELECT RAISE(ABORT, 'an applied action is applied once');
END;

CREATE INDEX action_unapplied ON action(created_at_ms) WHERE status = 'pending';
