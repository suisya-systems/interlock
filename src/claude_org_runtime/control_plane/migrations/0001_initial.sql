-- ==========================================================================
--  0001_initial -- the production control-plane schema
--
--  This file is step 0001 of the numbered, forward-only migration ledger
--  described in docs/production-schema.md section 3. It is AUTHORED, not
--  copied: D-0026 makes every spike implementation throwaway by default, and
--  D-0029 resolves Q-0001 by writing the production DDL on its own terms and
--  recording, per table, whether the spike's semantics are carried verbatim,
--  re-derived, or new. Where this file agrees with spike_schema.sql the
--  agreement is a re-confirmation of the reasoning behind the constraint, not
--  an inheritance -- docs/production-schema.md section 2 is the table of which
--  is which, and that table is the record Q-0001 asked for.
--
--  There is no migration path from a spike database to a production one and
--  none will be written (section 3.3). A production database is told apart
--  from a spike one by PRAGMA application_id, which the migrator stamps.
--
--  schema_migration is deliberately NOT created here. The migrator bootstraps
--  it before applying any step (section 3.1), because a step cannot record its
--  own application in a table that does not exist yet. This step is applied
--  inside one transaction together with its schema_migration row, so a failure
--  anywhere below leaves the database at the previous version rather than
--  half-migrated.
--
--  Two conventions hold across the whole file, for the reasons that produced
--  them in the spike and are unchanged here:
--
--  Time is the caller's. Every timestamp is INTEGER milliseconds since the
--  Unix epoch, UTC, and carries NO DEFAULT. ACCEPTANCE.md section 2 injects
--  clock skew across expiry boundaries; a column defaulted to the database's
--  own clock makes that case untestable and hands a recovering process a
--  timestamp it never chose. docs/time-base-policy.md section 2 rule 2 is the
--  same rule stated for the evaluation clock: :now_ms is always a parameter.
--
--  Types are asserted by CHECK, not by STRICT. STRICT arrived in SQLite 3.37
--  and this project supports Python 3.10, whose bundled library is older on
--  some platforms. A timestamp column that silently accepts a string is a
--  recovery query that silently sorts wrong.
--
--  Dependency order matters: SQLite resolves a REFERENCES clause at DML time,
--  not at CREATE time, but foreign_key_check and every reader are happier when
--  the referenced table already exists, so the sections below run
--  run/session/lease/outbox/incident/action, then the event spine, then
--  repository/pull_request/run_pr_link, then ci_observation (which references
--  both repository and event), then the watcher tables, the gate tables, the
--  policy tables, and finally ai_invocation.
-- ==========================================================================


-- ==========================================================================
--  1. The core state tables (docs/production-schema.md section 2)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- run -- the unit of work. D-0001 names it as source-of-truth state; recovery
-- resumes from it.
--
-- RE-DERIVED. The spike left status as unconstrained text *because* the writer
-- assignment was open: a CHECK enumerating the statuses would have answered
-- Q-0001 in DDL. Section 4.2 closes that question -- run.status transitions are
-- exclusively the Secretary's, fenced by the run lease epoch -- so the closed
-- set and the forward-only walk are now the schema's to enforce.
--
-- The set is created / running / suspended / completed / failed / cancelled.
-- 'suspended' is NOT terminal: a suspend is resumable, which is why the
-- forward-only trigger below admits running <-> suspended in both directions
-- while admitting no other reversal. The terminal set is completed / failed /
-- cancelled, and it is terminal in the strong sense -- a completed run does not
-- become a failed one, because a run's terminal status is what the measurement
-- harness's cohort (docs/measurement-harness.md section 2.1) and the gate
-- sweep's subject_gone rule (section 9.4) are both read out of.
-- --------------------------------------------------------------------------
CREATE TABLE run (
    run_id         TEXT    PRIMARY KEY,
    status         TEXT    NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,

    CHECK (typeof(run_id) = 'text' AND typeof(status) = 'text'),
    CHECK (typeof(created_at_ms) = 'integer' AND typeof(updated_at_ms) = 'integer'),
    CHECK (length(run_id) > 0),
    CHECK (status IN ('created', 'running', 'suspended',
                      'completed', 'failed', 'cancelled')),
    CHECK (updated_at_ms >= created_at_ms)
);

-- The CHECK above constrains the row that results, not the step that reached
-- it, so without this trigger an UPDATE could walk a run back out of a terminal
-- status and erase the completion the Secretary already published, or rewind a
-- started run to 'created' and make its whole lifetime unmeasurable.
--
-- The ranks collapse running and suspended onto the same level on purpose: a
-- suspend is a pause, not a step forward, and resuming it must not be a
-- reversal. Everything else moves up or stands still. Leaving a terminal status
-- is handled separately from the ranks, because completed -> failed has equal
-- rank and must still be refused: which terminal status a run reached is the
-- fact, and a fact is corrected by a new run, not by an UPDATE.
CREATE TRIGGER run_status_is_forward_only
BEFORE UPDATE OF status ON run
WHEN (OLD.status IN ('completed', 'failed', 'cancelled')
      AND NEW.status <> OLD.status)
  OR (CASE NEW.status
           WHEN 'created' THEN 0
           WHEN 'running' THEN 1
           WHEN 'suspended' THEN 1
           ELSE 2 END)
   < (CASE OLD.status
           WHEN 'created' THEN 0
           WHEN 'running' THEN 1
           WHEN 'suspended' THEN 1
           ELSE 2 END)
BEGIN
    SELECT RAISE(ABORT,
        'run.status walks created -> running/suspended -> terminal; a terminal run is never reopened');
END;

-- --------------------------------------------------------------------------
-- session -- the session<->run binding, persisted at spawn.
--
-- CARRIED VERBATIM. The staged binding, the one-active-binding-per-run partial
-- unique index and the observation/provider_state equality pair were derived
-- from gate item 2 under injection (docs/crash-window-orchestration.md), not
-- from convenience, and nothing in G3/G4 touches that derivation.
--
-- After a kill at any injection point, re-identification must yield EXACTLY ONE
-- session for the run, and a second writer must be refused rather than
-- admitted. That "exactly one" is enforced by the database, not by the
-- discipline of whoever writes to it: a check-then-insert in application code
-- leaves precisely the window item 2 injects into.
--
-- provider_state carries the PROVIDER'S OWN state word, uninterpreted, and
-- observation carries R4's could-not-observe case with its reason. Collapsing
-- "could not observe" into an absent state word is the v1 defect R4 records,
-- and enumerating the provider's vocabulary here would put a provider-shaped
-- assumption into a provider-neutral store.
--
-- binding_phase makes the PRE-SPAWN binding honest (D-0024). The binding is
-- committed before the process exists, so at commit time there is no provider
-- observation to record -- and recording one anyway would be the lie item 2
-- injects into. The phases are the injection points' own seams:
--
--   'prepared'           -- the identity is chosen and durably committed; no
--                           spawn has been attempted as far as this row knows.
--   'spawned'            -- the provider was asked to start the process; what
--                           identity it actually assigned is NOT yet known
--                           (exit 0 is not evidence -- D-0027).
--   'identity_confirmed' -- the provider's own read-back named the committed
--                           identity, and that read-back is itself committed.
-- --------------------------------------------------------------------------
CREATE TABLE session (
    session_id          TEXT    PRIMARY KEY,
    run_id              TEXT    NOT NULL REFERENCES run(run_id),
    provider            TEXT    NOT NULL,
    binding_phase       TEXT    NOT NULL,
    observation         TEXT    NOT NULL,
    provider_state      TEXT,
    observation_reason  TEXT,
    bound_at_ms         INTEGER NOT NULL,
    released_at_ms      INTEGER,

    CHECK (typeof(session_id) = 'text' AND typeof(run_id) = 'text'),
    CHECK (typeof(bound_at_ms) = 'integer'),
    CHECK (released_at_ms IS NULL OR typeof(released_at_ms) = 'integer'),
    CHECK (length(session_id) > 0),
    CHECK (binding_phase IN ('prepared', 'spawned', 'identity_confirmed')),
    -- A pre-read-back row may not claim an observation, and a confirmed row
    -- must carry one: the equality binds the two vocabularies both ways.
    CHECK ((binding_phase = 'identity_confirmed') = (observation = 'observed')),
    CHECK (observation IN ('observed', 'unobserved')),
    -- An observed readout carries a state word; an unobserved one carries a
    -- reason instead. Neither may be constructed empty (R4) -- and an empty
    -- string is as empty as a NULL: a reason of '' is the v1 collapse of
    -- "could not observe" into "nothing to say" wearing a different type.
    CHECK (provider_state IS NULL OR length(provider_state) > 0),
    CHECK (observation_reason IS NULL OR length(observation_reason) > 0),
    CHECK ((observation = 'observed')   = (provider_state IS NOT NULL)),
    CHECK ((observation = 'unobserved') = (observation_reason IS NOT NULL)),
    CHECK (released_at_ms IS NULL OR released_at_ms >= bound_at_ms)
);

-- At most one ACTIVE binding per run. A released binding leaves the run free
-- for a new session, so an ordinary stop-then-resume is expressible, while a
-- second live writer for the same run is refused by the database at INSERT
-- time.
CREATE UNIQUE INDEX session_one_active_binding_per_run
    ON session(run_id) WHERE released_at_ms IS NULL;

-- Recovery reads the binding from the run side (re-identification).
CREATE INDEX session_by_run ON session(run_id);

-- The phase only ever moves forward: prepared -> spawned -> identity_confirmed.
-- A row that walked backwards would un-record a spawn or a read-back that
-- already happened, which is exactly the evidence the crash-window kill matrix
-- is read out of.
CREATE TRIGGER session_binding_phase_is_forward_only
BEFORE UPDATE OF binding_phase ON session
WHEN NOT (
       (OLD.binding_phase = NEW.binding_phase)
    OR (OLD.binding_phase = 'prepared' AND NEW.binding_phase = 'spawned')
    OR (OLD.binding_phase = 'spawned'  AND NEW.binding_phase = 'identity_confirmed')
)
BEGIN
    SELECT RAISE(ABORT,
        'session.binding_phase moves prepared -> spawned -> identity_confirmed, one step at a time');
END;

-- --------------------------------------------------------------------------
-- lease -- exclusion, and the fencing token that makes it real.
--
-- CARRIED VERBATIM. docs/lease-fencing.md is the derivation and it is
-- unaffected by anything G3/G4 adds; the watcher heartbeat (section 8.3) is a
-- new *consumer* of this table, not a change to it.
--
-- ACCEPTANCE.md section 2 is explicit that expiry discovery alone is not
-- enough: check-then-write leaves a race in which the lease expires between the
-- check and the write. Every protected write must carry the lease epoch and
-- validate it ATOMICALLY as part of the write -- which is why outbox, action,
-- event_consumption, gate_transition and watcher_liveness all carry an epoch
-- column, and why a protected write is spelled
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
-- see its own epoch valid again.
--
-- holder is an opaque claimant identity, deliberately not a role. Which
-- component may hold which resource is section 4.2's writer table, which is
-- prose and policy rather than a CHECK -- the resource names are open-ended
-- ('watcher_scope:<scope_id>' is coined by section 8.3) and a closed set here
-- would make every new fenced resource a schema change.
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
-- outbox -- the delivery record, and the resend/ack/retry evidence.
--
-- CARRIED VERBATIM, including the deliberate non-uniqueness of dedup_key.
-- Section 9.4 needed gate relays to be enqueued idempotently and gave them
-- their own identity table (gate_relay) rather than tightening this shared
-- column: exactly-once is a property of the EFFECT, and collapsing outbox rows
-- in DDL would move delivery policy into the schema.
--
-- status walks pending -> delivered -> acked and never back. retry_count is
-- durable and monotonic, which is what "restart-surviving, monotonically
-- increasing" in ACCEPTANCE.md section 2 asks a query to be able to show. Ack
-- is set once, so a duplicate or late ack changes nothing -- idempotent by
-- construction rather than by the handler remembering. Section 9.5 hangs the
-- gate relay's whole crash-window argument on that ack being set once.
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
-- there, so without this an UPDATE could walk the lifecycle backwards and erase
-- the delivery evidence the relay-gap and reconcile passes are read out of.
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
-- delivery was deduplicated under may not be rewritten under a live row. The
-- ack is recorded against this identity, so vacating it by rename would let a
-- second row take the identity of a message that was already acked -- and
-- gate_relay.message_id points here, so a rename would also silently re-aim a
-- gate's relay at somebody else's message.
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

-- Freezing the identity protects it only while the row exists: deleting an
-- acked row vacates its message_id, and the same identity can then be enqueued
-- and delivered a second time. Q-0006 (retention) is open and is not answered
-- by a DELETE.
CREATE TRIGGER outbox_rows_are_never_deleted
BEFORE DELETE ON outbox
BEGIN
    SELECT RAISE(ABORT, 'outbox rows are delivery evidence and are never deleted');
END;

-- Recovery's first question after a kill: what is enqueued and unfinished? It
-- is also the reconcile pass's orphaned-outbox query (section 5.6).
CREATE INDEX outbox_undelivered ON outbox(enqueued_at_ms) WHERE status <> 'acked';

-- --------------------------------------------------------------------------
-- incident -- D-0007's persisted packet. The AI is startable statelessly from a
-- row here (D-0003, D-0007), so the packet is in the row and not in anyone's
-- context.
--
-- CARRIED VERBATIM. Q-0002 -- whether a repeated incident condition increments
-- retry_count on the existing incident or opens a linked one, and what the
-- re-notification window is in absolute time -- is STILL OPEN, and nothing in
-- G3/G4 narrows it. So, exactly as in the spike:
--
--   * dedup_key is indexed but NOT unique. A UNIQUE constraint would force the
--     increment-in-place rule.
--   * related_incident_id exists and is nullable, so the linked-incident rule
--     is expressible too. It is a plain self-reference with no semantics
--     attached; it does not mean "collapsed into".
--   * no window, no re-notification period, no time bucket appears anywhere in
--     this file. The detection-latency budgets in policy_detection_latency are
--     onset-to-alarm ceilings, not re-notification windows, and they say
--     nothing about whether a repeat opens a row.
--
-- Both collapse rules stay expressible and neither is enforced; ACCEPTANCE.md
-- section 2 requires downstream tests to parameterise the choice.
--
-- fact_state is unconstrained text carrying D-0005's watcher fact plus the
-- detector version that produced it. The closed set lives in DECISIONS.md; a
-- CHECK duplicating it here would make a D- entry that extends the set into a
-- migration step. The G3/G4 incident classes (consumer_backlog, relay_gap,
-- watcher_silence, watcher_scope_uncovered, policy_budget_violation, ...) are
-- named in docs/time-base-policy.md section 3.2 and reach this column as data.
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
-- action -- the side-effect record. Every side effect must be applied exactly
-- once, "evidenced by an idempotency/dedup record rather than by absence of a
-- visible duplicate", and this table is that record.
--
-- CARRIED VERBATIM. exactly_once_mechanism and the one-effect-per-key partial
-- unique index are the ACCEPTANCE.md section 2 clause and are unchanged. The
-- enumeration is that clause, not a policy of this schema: SQLite cannot tell
-- "the effect completed" from "the effect never started" when the process died
-- in between, so a row that does not say how it is made exactly-once is a row
-- claiming something it cannot support.
--
-- Refusals are recorded, never dropped. ACCEPTANCE.md section 2 requires the
-- rejection of a stale writer to be itself durable -- which is what section
-- 8.3's refused heartbeat is written as: an action row in status 'refused'
-- carrying which of the two refusal causes it was.
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
-- lifetime, which is what makes the unique index durable evidence rather than a
-- snapshot.
CREATE TRIGGER action_idempotency_key_is_frozen
BEFORE UPDATE OF idempotency_key ON action
WHEN NEW.idempotency_key <> OLD.idempotency_key
BEGIN
    SELECT RAISE(ABORT, 'an action keeps the idempotency key it was recorded with');
END;

-- A refused row that can be moved back to 'pending' is a rejected attempt that
-- becomes executable again -- and the record of the rejection disappears in the
-- same statement.
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

-- The unique index constrains the rows that exist, so deleting an applied
-- action vacates its idempotency key and the same effect can be applied again --
-- the one thing this table exists to make impossible. A refused row is evidence
-- too, so nothing here is deletable; Q-0006 is open and is not answered by a
-- DELETE.
CREATE TRIGGER action_rows_are_never_deleted
BEFORE DELETE ON action
BEGIN
    SELECT RAISE(ABORT, 'action rows are exactly-once evidence and are never deleted');
END;

CREATE INDEX action_unapplied ON action(created_at_ms) WHERE status = 'pending';


-- ==========================================================================
--  2. The event spine (docs/production-schema.md section 5)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- event -- the single spine. CI outcomes are written once and every consumer
-- reads from the same table, which removes v1's push-vs-poll duplication by
-- construction (#64).
--
-- There is deliberately NO drained_at column. With one such column the first
-- consumer to finish marks the row drained and hides every other consumer's
-- backlog -- which reproduces tools/relay_scan.py's documented failure (134
-- terminal events accumulating undelivered for twenty days) through a different
-- mechanism. Consumption is fanned out per consumer at append time instead; see
-- event_consumption.
--
-- occurred_at_ms is the source clock (when the observed thing happened, as the
-- provider reports it); ingested_at_ms is ours (when the row committed). They
-- are never conflated: docs/time-base-policy.md section 2 rule 1 evaluates every
-- tolerance against our clock only, because a provider's skew would otherwise
-- look like a relay gap and skew is not something we can bound.
--
-- seq is an AUTOINCREMENT integer and a consumer cursor over it is sound only
-- because SQLite serialises write transactions: seq is assigned in commit order
-- and a committed gap is permanent, never back-filled, so a consumer that
-- advanced past N can never be handed a row that arrives at N-1. AUTOINCREMENT
-- additionally keeps the value from being reused after a delete -- and deletes
-- are blocked anyway. This is a property the design DEPENDS on, so the
-- implementation carries a test that interleaves two appending transactions
-- (D-0030); if a future deployment ever admits concurrent writers, that test is
-- the thing that fails.
-- --------------------------------------------------------------------------
CREATE TABLE event (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    subject_kind    TEXT    NOT NULL,
    subject_id      TEXT    NOT NULL,
    run_id          TEXT             REFERENCES run(run_id),
    payload         TEXT    NOT NULL DEFAULT '{}',
    producer        TEXT    NOT NULL,
    producer_epoch  INTEGER,
    dedup_key       TEXT    NOT NULL,
    occurred_at_ms  INTEGER NOT NULL,
    ingested_at_ms  INTEGER NOT NULL,

    CHECK (typeof(event_id) = 'text' AND length(event_id) > 0),
    CHECK (length(event_type) > 0),
    CHECK (subject_kind IN ('run', 'session', 'pull_request', 'gate', 'watcher_scope', 'incident')),
    CHECK (length(subject_id) > 0),
    CHECK (length(producer) > 0),
    CHECK (producer_epoch IS NULL OR (typeof(producer_epoch) = 'integer' AND producer_epoch > 0)),
    CHECK (length(dedup_key) > 0),
    CHECK (typeof(occurred_at_ms) = 'integer' AND typeof(ingested_at_ms) = 'integer'),
    CHECK (json_valid(payload))
);

CREATE UNIQUE INDEX event_by_event_id ON event(event_id);

-- The spine is append-once-per-observed-fact. A producer that re-polls, restarts
-- mid-append, or re-fetches the same page collides here and is refused rather
-- than appending a second row for one fact. This is the identity-uniqueness half
-- of the writer rule in section 4.2, and it is what lets several producers share
-- one spine without a single-writer lease over the table.
CREATE UNIQUE INDEX event_one_row_per_fact ON event(dedup_key);

CREATE INDEX event_by_subject ON event(subject_kind, subject_id, seq);
CREATE INDEX event_by_run ON event(run_id, seq) WHERE run_id IS NOT NULL;

CREATE TRIGGER event_rows_are_immutable
BEFORE UPDATE ON event
BEGIN
    SELECT RAISE(ABORT, 'the event spine is append-only; correct a fact with a new event');
END;

CREATE TRIGGER event_rows_are_never_deleted
BEFORE DELETE ON event
BEGIN
    SELECT RAISE(ABORT, 'event rows are the spine every consumer is replayed from');
END;

-- --------------------------------------------------------------------------
-- consumer / consumer_subscription -- who the spine fans out to.
--
-- registered_from_seq is what makes a late registration's back-fill decision a
-- visible row rather than a gap somebody has to explain later: a consumer
-- registered at S gets pending rows for events > S from its registration
-- transaction onward, and if it needs history that same transaction back-fills
-- them.
-- --------------------------------------------------------------------------
CREATE TABLE consumer (
    consumer_id        TEXT    PRIMARY KEY,
    kind               TEXT    NOT NULL,
    lease_resource     TEXT    NOT NULL,
    registered_at_ms   INTEGER NOT NULL,
    registered_from_seq INTEGER NOT NULL,
    retired_at_ms      INTEGER,

    CHECK (length(consumer_id) > 0),
    -- 'delivery'   -- consumption IS an outbox delivery; the outbox row is
    --                 created in the append transaction (5.4).
    -- 'compute'    -- consumption is a state transition the consumer performs
    --                 itself and then marks.
    CHECK (kind IN ('delivery', 'compute')),
    CHECK (typeof(registered_from_seq) = 'integer' AND registered_from_seq >= 0),
    CHECK (retired_at_ms IS NULL OR retired_at_ms >= registered_at_ms)
);

CREATE TABLE consumer_subscription (
    consumer_id    TEXT    NOT NULL REFERENCES consumer(consumer_id),
    event_type     TEXT    NOT NULL,
    recipient      TEXT,             -- required when consumer.kind = 'delivery'
    added_at_ms    INTEGER NOT NULL,
    removed_at_ms  INTEGER,

    PRIMARY KEY (consumer_id, event_type),
    CHECK (length(event_type) > 0),
    CHECK (recipient IS NULL OR length(recipient) > 0),
    CHECK (removed_at_ms IS NULL OR removed_at_ms >= added_at_ms)
);

-- A cross-table invariant, so it is a trigger rather than a CHECK: outbox.recipient
-- is NOT NULL, so a 'delivery' subscription registered without one does not fail
-- at registration -- it fails later, inside the append transaction of the next
-- matching event, taking the event down with it (section 5.4 commits all or
-- nothing). Refusing the registration moves the failure to the party that can
-- fix it.
CREATE TRIGGER consumer_subscription_recipient_matches_kind_on_insert
BEFORE INSERT ON consumer_subscription
WHEN (SELECT kind FROM consumer WHERE consumer_id = NEW.consumer_id)
     IS NOT (CASE WHEN NEW.recipient IS NULL THEN 'compute' ELSE 'delivery' END)
BEGIN
    SELECT RAISE(ABORT,
        'a delivery subscription carries a recipient and a compute subscription does not');
END;

CREATE TRIGGER consumer_subscription_recipient_matches_kind_on_update
BEFORE UPDATE OF recipient, consumer_id ON consumer_subscription
WHEN (SELECT kind FROM consumer WHERE consumer_id = NEW.consumer_id)
     IS NOT (CASE WHEN NEW.recipient IS NULL THEN 'compute' ELSE 'delivery' END)
BEGIN
    SELECT RAISE(ABORT,
        'a delivery subscription carries a recipient and a compute subscription does not');
END;

-- --------------------------------------------------------------------------
-- event_consumption -- one row per (event, subscribed consumer), created in the
-- same transaction as the event. "Undrained by C" means a row here with
-- consumer_id = C and status IN ('pending','failed'); there is no global
-- undrained, and the phrase is only ever used with a consumer named.
--
-- Per-consumer rows were chosen over a per-consumer cursor because a cursor
-- cannot express "event 5 failed, event 6 succeeded" and forces head-of-line
-- blocking on every failure (D-0030).
--
-- 'skipped' exists so that a subscription a consumer decides is not applicable
-- to a particular event settles explicitly rather than sitting pending forever
-- and being reported as a backlog. A skip's reason travels in its own
-- consumption_skipped event, deliberately NOT in last_error -- a skip is not an
-- error, and a skipped row with no recorded reason anywhere would be
-- indistinguishable from a consumer quietly dropping work.
-- --------------------------------------------------------------------------
CREATE TABLE event_consumption (
    consumer_id     TEXT    NOT NULL REFERENCES consumer(consumer_id),
    event_seq       INTEGER NOT NULL REFERENCES event(seq),
    status          TEXT    NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    message_id      TEXT             REFERENCES outbox(message_id),
    last_error      TEXT,
    writer_epoch    INTEGER,
    created_at_ms   INTEGER NOT NULL,
    settled_at_ms   INTEGER,

    PRIMARY KEY (consumer_id, event_seq),
    CHECK (status IN ('pending', 'consumed', 'skipped', 'failed')),
    CHECK (attempt_count >= 0),
    CHECK (last_error IS NULL OR length(last_error) > 0),
    CHECK ((status = 'failed') = (last_error IS NOT NULL)),
    CHECK ((status IN ('consumed', 'skipped')) = (settled_at_ms IS NOT NULL)),
    CHECK (writer_epoch IS NULL OR writer_epoch > 0),
    CHECK (settled_at_ms IS NULL OR settled_at_ms >= created_at_ms)
);

CREATE TRIGGER event_consumption_attempt_count_is_monotonic
BEFORE UPDATE OF attempt_count ON event_consumption
WHEN NEW.attempt_count < OLD.attempt_count
BEGIN
    SELECT RAISE(ABORT, 'event_consumption attempt_count must not decrease');
END;

-- 'failed' is retryable and is NOT terminal: it is the durable trace of an
-- attempt that did not land, which is what distinguishes a stalled consumer
-- from a quiet one. 'consumed' and 'skipped' are terminal.
CREATE TRIGGER event_consumption_settled_is_terminal
BEFORE UPDATE OF status ON event_consumption
WHEN OLD.status IN ('consumed', 'skipped') AND NEW.status <> OLD.status
BEGIN
    SELECT RAISE(ABORT, 'a settled consumption is not reopened; append a new event instead');
END;

CREATE TRIGGER event_consumption_rows_are_never_deleted
BEFORE DELETE ON event_consumption
BEGIN
    SELECT RAISE(ABORT, 'a consumption row is the per-consumer drain evidence');
END;

-- The reconcile pass's primary query (section 5.6). Partial, so it stays small
-- even when the spine is long: a healthy system has almost no rows here.
CREATE INDEX event_consumption_undrained
    ON event_consumption(consumer_id, event_seq) WHERE status IN ('pending', 'failed');


-- ==========================================================================
--  3. run<->PR linkage (docs/production-schema.md section 7)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- repository -- identity, so that a PR number is resolved against the right
-- repository and not against the cwd's.
--
-- The incident this is designed against is dated: on 2026-08-06 v1's run->PR
-- tools defaulted an omitted --repo to `gh repo view` -- the cwd repository,
-- always claude-org-ja for the Secretary -- so a cross-repo run's PR number was
-- resolved against the wrong repository, and renga PR #302 was recorded with
-- claude-org-ja PR #302's branch, commit and merge time. The tool exited ok.
-- Whether it corrupted silently or failed loudly depended only on whether the
-- home repo happened to own that number.
--
-- Identity is repo_id, never a URL string. owner/name are mutable (GitHub
-- renames and transfers preserve the repository), so the slug is a lookup key
-- matched case-insensitively, while provider_repo_id -- GitHub's immutable node
-- id -- is the identity when it is available. A rename is absorbed by updating
-- owner/name on the existing row, which keeps every historical observation
-- attached to the same repository; storing the URL, as v1's pr_url did, means a
-- rename silently forks the identity and the metrics join has to guess.
--
-- provider is CHECKed to 'github' alone on purpose: #64 says gh is the
-- interface to GitHub, and the target shape is a thin seam plus one
-- substitution test, not everything abstracted. A second provider widens this
-- CHECK in a later migration step and adds its substitution test there.
-- --------------------------------------------------------------------------
CREATE TABLE repository (
    repo_id           TEXT    PRIMARY KEY,
    provider          TEXT    NOT NULL,
    provider_repo_id  TEXT,
    owner             TEXT    NOT NULL,
    name              TEXT    NOT NULL,
    created_at_ms     INTEGER NOT NULL,
    updated_at_ms     INTEGER NOT NULL,

    CHECK (length(repo_id) > 0),
    CHECK (provider IN ('github')),
    CHECK (length(owner) > 0 AND length(name) > 0),
    CHECK (provider_repo_id IS NULL OR length(provider_repo_id) > 0),
    CHECK (updated_at_ms >= created_at_ms)
);

-- Case is preserved in the columns and folded in the index.
-- tools/resolve_run_repo.py keeps a case-preserving twin of its matcher
-- precisely because the value is handed to `gh --repo` and recorded in
-- payloads; folding it in storage would corrupt those uses, and not folding it
-- in the index would admit two rows for one repository.
CREATE UNIQUE INDEX repository_by_slug ON repository(provider, lower(owner), lower(name));
CREATE UNIQUE INDEX repository_by_provider_id
    ON repository(provider, provider_repo_id) WHERE provider_repo_id IS NOT NULL;

-- --------------------------------------------------------------------------
-- pull_request -- the projection of a provider PR.
--
-- (repo_id, pr_number) is a sound natural key because GitHub never reuses a PR
-- number within a repository. That settles the "recreated PR" case without
-- extra machinery: a recreated PR has a new number and is therefore a new row,
-- and the old row remains as the record of what happened.
--
-- head_event_seq is what makes a head update auditable -- the verdict
-- projection in section 6.3 turns on head_sha, so the event that moved it must
-- be identifiable, not merely a timestamp.
-- --------------------------------------------------------------------------
CREATE TABLE pull_request (
    pr_id                TEXT    PRIMARY KEY,
    repo_id              TEXT    NOT NULL REFERENCES repository(repo_id),
    pr_number            INTEGER NOT NULL,
    provider_pr_id       TEXT,
    head_sha             TEXT    NOT NULL,
    head_observed_at_ms  INTEGER NOT NULL,
    head_event_seq       INTEGER NOT NULL REFERENCES event(seq),
    state                TEXT    NOT NULL,
    merge_commit_sha     TEXT,
    merged_at_ms         INTEGER,
    closed_at_ms         INTEGER,
    created_at_ms        INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL,

    CHECK (typeof(pr_number) = 'integer' AND pr_number > 0),
    CHECK (length(head_sha) = 40 AND head_sha = lower(head_sha)),
    CHECK (state IN ('open', 'merged', 'closed')),
    CHECK (merge_commit_sha IS NULL OR (length(merge_commit_sha) = 40
           AND merge_commit_sha = lower(merge_commit_sha))),
    CHECK ((state = 'merged') = (merged_at_ms IS NOT NULL)),
    CHECK ((state = 'merged') = (merge_commit_sha IS NOT NULL)),
    CHECK ((state IN ('merged', 'closed')) = (closed_at_ms IS NOT NULL)),
    CHECK (updated_at_ms >= created_at_ms)
);

CREATE UNIQUE INDEX pull_request_identity ON pull_request(repo_id, pr_number);

-- ONLY 'merged' is terminal. A closed, unmerged PR can be reopened on the
-- provider with the same repository and number, and forbidding that here would
-- leave the reopened PR permanently recorded as closed -- and, because the
-- watcher scope is retired when a PR goes terminal, permanently unwatched too.
-- So closed -> open is admitted, and it is the projection of a real provider
-- event rather than an edit: the update clears closed_at_ms (the CHECK above
-- requires it) and re-activates the scope by clearing watcher_scope.retired_at_ms
-- in the same transaction.
-- The head projection is monotonic in the PROVIDER's order, not in ours.
-- ci_current_verdict selects evidence by pull_request.head_sha (section 6.3
-- rule 1), so a late-arriving older head observation that overwrote this column
-- would make superseded CI evidence current again -- the same last-write-wins
-- defect rule 4 removes from the verdict projection, reached through the column
-- the verdict projection depends on. A head CHANGE therefore requires the
-- provider's observation time to advance, and our own append order to advance
-- with it; re-observing the SAME head may refresh the timestamp and no more.
CREATE TRIGGER pull_request_head_is_monotonic
BEFORE UPDATE OF head_sha, head_observed_at_ms, head_event_seq ON pull_request
WHEN (NEW.head_sha <> OLD.head_sha
      AND NOT (NEW.head_observed_at_ms > OLD.head_observed_at_ms
               AND NEW.head_event_seq > OLD.head_event_seq))
  OR NEW.head_observed_at_ms < OLD.head_observed_at_ms
  OR NEW.head_event_seq < OLD.head_event_seq
BEGIN
    SELECT RAISE(ABORT,
        'a pull request head only moves forward in the provider''s own order; a late older observation is evidence, not a projection');
END;

CREATE TRIGGER pull_request_merge_is_terminal
BEFORE UPDATE OF state ON pull_request
WHEN OLD.state = 'merged' AND NEW.state <> 'merged'
BEGIN
    SELECT RAISE(ABORT, 'a merged pull request does not reopen; its merge is a fact');
END;

-- --------------------------------------------------------------------------
-- run_pr_link -- the cardinality decision, made in DDL.
--
-- A run may have several PRs, across repositories (the cross-repo case is real;
-- the 2026-08-06 incident is one, and a run that opens a follow-up PR is
-- ordinary). A PR may belong to several runs (a later fix run legitimately
-- touches an earlier run's PR; forbidding it would push the second run to
-- fabricate a duplicate PR row). What makes completion unambiguous despite both
-- is that at most ONE 'primary' link per run is live at a time: when the
-- primary link's PR reaches state='merged', the Secretary -- as the compute
-- consumer of pr_merged -- may move the run to its terminal status. Supporting
-- links never drive a run transition.
-- --------------------------------------------------------------------------
CREATE TABLE run_pr_link (
    run_id          TEXT    NOT NULL REFERENCES run(run_id),
    pr_id           TEXT    NOT NULL REFERENCES pull_request(pr_id),
    role            TEXT    NOT NULL,
    resolution      TEXT    NOT NULL,
    linked_at_ms    INTEGER NOT NULL,
    unlinked_at_ms  INTEGER,
    unlink_reason   TEXT,

    PRIMARY KEY (run_id, pr_id),
    CHECK (role IN ('primary', 'supporting')),
    -- How the repository was resolved. The absence of a cwd-default member is
    -- the 2026-08-06 incident encoded: there is no value this column can hold
    -- that means "we guessed from the working directory". A run whose
    -- repository cannot be resolved by one of the three FAILS TO LINK, and the
    -- failure is an incident, not a default -- v1's tool raises
    -- RepoResolutionError for exactly this reason, "so the caller can exit
    -- non-zero instead of writing a foreign repo's PR onto the run", and this
    -- closed set is that sentence made unfalsifiable.
    CHECK (resolution IN ('project_registry', 'explicit_operator', 'provider_event')),
    CHECK (unlink_reason IS NULL OR length(unlink_reason) > 0),
    CHECK ((unlinked_at_ms IS NOT NULL) = (unlink_reason IS NOT NULL)),
    CHECK (unlinked_at_ms IS NULL OR unlinked_at_ms >= linked_at_ms)
);

-- At most ONE live primary PR per run. Supporting links are unconstrained in
-- number. A run may span repositories; a PR may be touched by several runs.
-- A run is re-pointed by unlinking the primary with a reason and linking
-- another, so the history of both survives.
CREATE UNIQUE INDEX run_pr_link_one_live_primary_per_run
    ON run_pr_link(run_id) WHERE role = 'primary' AND unlinked_at_ms IS NULL;

CREATE INDEX run_pr_link_by_pr ON run_pr_link(pr_id) WHERE unlinked_at_ms IS NULL;


-- ==========================================================================
--  4. CI observation and the verdict projection
--     (docs/production-schema.md section 6)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- ci_observation -- evidence, never overwritten.
--
-- Two failures are being designed against. Without an IDENTITY, a re-poll, a CI
-- rerun, a PR head update and a late arrival are indistinguishable, and the
-- spine's dedup key has nothing to be made of. Without ORDERING, arrival-order
-- last-write-wins lets a stale verdict overwrite a newer one -- reporting a red
-- PR as green because the red observation was slower, which is D-0006's verdict
-- honesty violated in the most direct way available.
--
-- indeterminate and no_run are separate members because collapsing them into
-- 'failed' is the v1 defect D-0006 records. tools/pr_watch.py reserves
-- indeterminate for a CONTINUED fetch failure specifically so that a transient
-- probe problem is not reported as a CI result; in D-0005 terms an indeterminate
-- observation is OBSERVATION_UNAVAILABLE and is never an anomaly, while no_run
-- is a fact about the repository, not about the change.
-- --------------------------------------------------------------------------
CREATE TABLE ci_observation (
    observation_id  TEXT    PRIMARY KEY,
    event_seq       INTEGER NOT NULL REFERENCES event(seq),
    provider        TEXT    NOT NULL,
    repo_id         TEXT    NOT NULL REFERENCES repository(repo_id),
    pr_number       INTEGER NOT NULL,
    head_sha        TEXT    NOT NULL,
    check_scope     TEXT    NOT NULL,
    scope_id        TEXT    NOT NULL,
    attempt         INTEGER NOT NULL,
    verdict         TEXT    NOT NULL,
    verdict_detail  TEXT,
    source_id       TEXT,
    observer        TEXT    NOT NULL,
    observer_epoch  INTEGER NOT NULL,
    occurred_at_ms  INTEGER NOT NULL,
    ingested_at_ms  INTEGER NOT NULL,

    -- provider is CHECKed to 'github' alone, exactly as it is on repository:
    -- D-0033 says "a second provider widens the CHECK in a migration step and
    -- brings its substitution test then". Mere non-emptiness would not be a
    -- weaker version of that rule, it would defeat the projection. provider is
    -- part of ci_observation_identity, so an unnarrowed column admits the SAME
    -- fact a second time under a spelling variant ('GITHUB'); the
    -- ci_current_verdict per-scope subquery does not discriminate on provider,
    -- so the duplicate competes on (attempt, occurred_at_ms, event_seq) and a
    -- later-timestamped bogus row wins -- a red PR projected GREEN, which is
    -- the section 6.1 verdict-honesty failure in its most direct form.
    CHECK (provider IN ('github')),
    CHECK (typeof(pr_number) = 'integer' AND pr_number > 0),
    -- a full commit SHA, lowercased at the adapter edge. An abbreviated SHA is
    -- not an identity: two heads can share a prefix, and the observation would
    -- then be attributed to the wrong head.
    CHECK (length(head_sha) = 40 AND head_sha = lower(head_sha)),
    CHECK (check_scope IN ('check_suite', 'workflow_run', 'rollup')),
    CHECK (length(scope_id) > 0),
    CHECK (typeof(attempt) = 'integer' AND attempt >= 1),
    CHECK (verdict IN (
        'passed',
        'failed',
        'cancelled',
        'timed_out',
        'no_run',          -- the provider reports no CI configured for this head
        'indeterminate'    -- OBSERVATION_UNAVAILABLE's CI shape (D-0006)
    )),
    CHECK (observer_epoch > 0),
    CHECK (typeof(occurred_at_ms) = 'integer' AND typeof(ingested_at_ms) = 'integer')
);

CREATE UNIQUE INDEX ci_observation_event ON ci_observation(event_seq);

-- The identity. Everything a re-poll would produce again is in it; everything a
-- genuinely new observation changes is in it too.
--
-- `verdict` is IN the identity, and leaving it out is the mistake that costs a
-- real result. A fetch failure records `indeterminate` for a scope; the next
-- poll succeeds and the provider says `failed`. Provider, repo, PR, head, scope
-- and attempt are all unchanged -- the rerun never happened, only our
-- observation of it improved -- so an identity without `verdict` collides, the
-- append is an idempotent no-op, and the PR stays projected `indeterminate`
-- forever with the real verdict discarded. With `verdict` in the key, a repeat
-- of the SAME answer is still refused (which is what idempotency needs) and a
-- CHANGED answer is a new observation (which is what honesty needs).
--
-- The corresponding event.dedup_key is the same tuple rendered as a string,
-- ci/<provider>/<repo_id>/<pr_number>/<head_sha>/<check_scope>/<scope_id>/<attempt>/<verdict>,
-- so the spine's uniqueness and this one are the same constraint expressed
-- twice and a re-poll is an idempotent no-op at step 1 of the append
-- transaction, before anything else in it runs.
CREATE UNIQUE INDEX ci_observation_identity
    ON ci_observation(provider, repo_id, pr_number, head_sha, check_scope, scope_id,
                      attempt, verdict);

CREATE INDEX ci_observation_by_head
    ON ci_observation(repo_id, pr_number, head_sha, attempt DESC, occurred_at_ms DESC);

-- The current verdict is a PROJECTION over the observations, not a column, so
-- it cannot drift from the rows it summarises. Only observations whose head_sha
-- equals pull_request.head_sha are eligible -- a head update invalidates prior
-- verdicts rather than letting them be overwritten -- and among the eligible
-- ones the order is (attempt DESC, occurred_at_ms DESC, event_seq DESC), which
-- is the provider's own ordering rather than our arrival order. A late arrival
-- that orders lower is stored and does not move the projection; that is what
-- makes this a projection rather than last-write-wins.
--
-- Rule 3 -- the rollup's subordinate eligibility -- is IN the view and not only
-- in the prose, because a view that returned the rollup alongside the
-- fine-grained scopes would let a stale coarse `failed` dominate the severity
-- fold (most severe under failed > timed_out > cancelled > indeterminate >
-- passed) while every real check is green.
CREATE VIEW ci_current_verdict AS
SELECT o.repo_id, o.pr_number, o.head_sha, o.check_scope, o.scope_id,
       o.verdict, o.attempt, o.occurred_at_ms, o.event_seq
  FROM ci_observation o
  JOIN pull_request p
    ON p.repo_id = o.repo_id AND p.pr_number = o.pr_number AND p.head_sha = o.head_sha
 WHERE o.observation_id = (
        SELECT o2.observation_id FROM ci_observation o2
         WHERE o2.repo_id = o.repo_id AND o2.pr_number = o.pr_number
           AND o2.head_sha = o.head_sha AND o2.check_scope = o.check_scope
           AND o2.scope_id = o.scope_id
         ORDER BY o2.attempt DESC, o2.occurred_at_ms DESC, o2.event_seq DESC
         LIMIT 1)
   -- rule 3: a rollup is the coarse fallback, never a peer of the fine-grained
   -- scopes. It drops out of the projection the moment a real scope exists for
   -- this head.
   AND (o.check_scope <> 'rollup'
        OR NOT EXISTS (SELECT 1 FROM ci_observation f
                        WHERE f.repo_id = o.repo_id AND f.pr_number = o.pr_number
                          AND f.head_sha = o.head_sha
                          AND f.check_scope IN ('check_suite', 'workflow_run')));


-- ==========================================================================
--  5. Watcher liveness (docs/production-schema.md section 8)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- watcher_scope -- the expected roster, which is what turns "no row" from
-- invisible into detectable.
--
-- A single last_heartbeat_at column cannot make four distinctions, and each has
-- a v1 incident behind it: "polled, nothing changed" vs "poll failed"; a stale
-- watcher's late heartbeat; a MISSING watcher, whose absence writes no row and
-- is therefore invisible (tools/relay_scan.py's central lesson -- a silent
-- no-op is indistinguishable from a clean scan, and the fix is an unconditional
-- trace plus an expected roster to compare against); and partial coverage.
-- This table is the roster half, watcher_liveness is the unconditional trace.
--
-- A scope is created when a run's primary PR is linked and retired when the PR
-- reaches a terminal state, so the roster is derived from work that exists
-- rather than maintained by hand. expected_interval_ms is per scope because the
-- watcher_silence threshold is a MULTIPLE of it (section 8.4); precomputing
-- that multiple into milliseconds in the policy row would bake one scope's
-- interval into a row every other scope also reads.
-- --------------------------------------------------------------------------
CREATE TABLE watcher_scope (
    scope_id              TEXT    PRIMARY KEY,
    scope_kind            TEXT    NOT NULL,
    repo_id               TEXT             REFERENCES repository(repo_id),
    pr_id                 TEXT             REFERENCES pull_request(pr_id),
    expected_interval_ms  INTEGER NOT NULL,
    enabled               INTEGER NOT NULL DEFAULT 1,
    registered_at_ms      INTEGER NOT NULL,
    retired_at_ms         INTEGER,

    CHECK (scope_kind IN ('ci_pull_request', 'ci_repository')),
    CHECK (typeof(expected_interval_ms) = 'integer' AND expected_interval_ms > 0),
    CHECK (enabled IN (0, 1)),
    CHECK ((scope_kind = 'ci_pull_request') = (pr_id IS NOT NULL)),
    -- Every scope names a subject. The pr_id biconditional above only binds the
    -- ci_pull_request kind, so without this a 'ci_repository' row could carry
    -- repo_id AND pr_id both NULL: a roster entry for nothing at all. Such a row
    -- can never be covered, because no watcher has a subject to heartbeat for,
    -- and the section 8.4 coverage query -- which reports live scopes with no
    -- fresh liveness row -- would name it as uncovered forever. A roster that
    -- permanently alarms is a roster nobody reads, which defeats the one thing
    -- this table exists to do: make a MISSING watcher detectable.
    CHECK (repo_id IS NOT NULL),
    CHECK (retired_at_ms IS NULL OR retired_at_ms >= registered_at_ms)
);

CREATE INDEX watcher_scope_live ON watcher_scope(scope_kind) WHERE enabled = 1 AND retired_at_ms IS NULL;

-- --------------------------------------------------------------------------
-- watcher_liveness -- the fenced, unconditional trace. One row per scope,
-- written on EVERY attempt including the ones that observed nothing.
--
-- The heartbeat write is an UPSERT whose fence is inside the statement, and
-- both arms carry it (section 8.3): a bare UPDATE affects zero rows on the
-- first heartbeat of every scope, and since zero rows is also how a stale
-- writer is refused, the bootstrap case would be permanently indistinguishable
-- from a rejection. The lease resource is DERIVED in the statement as
-- 'watcher_scope:' || scope_id rather than passed alongside the scope, because
-- a separate parameter would let a watcher holding scope B's lease heartbeat
-- scope A -- the row is written, the uncovered scope looks healthy, and
-- watcher_silence never fires for it.
-- --------------------------------------------------------------------------
CREATE TABLE watcher_liveness (
    scope_id             TEXT    PRIMARY KEY REFERENCES watcher_scope(scope_id),
    holder               TEXT    NOT NULL,
    holder_epoch         INTEGER NOT NULL,
    last_attempt_at_ms   INTEGER NOT NULL,
    last_result          TEXT    NOT NULL,
    last_success_at_ms   INTEGER,
    last_change_at_ms    INTEGER,
    last_error_at_ms     INTEGER,
    last_error           TEXT,
    consecutive_errors   INTEGER NOT NULL DEFAULT 0,
    attempt_count        INTEGER NOT NULL DEFAULT 0,

    CHECK (length(holder) > 0),
    CHECK (typeof(holder_epoch) = 'integer' AND holder_epoch > 0),
    -- The distinction that the single-column form loses. Written on EVERY
    -- attempt, including the ones that observed nothing.
    CHECK (last_result IN ('observed_change', 'observed_no_change', 'error')),
    CHECK ((last_result = 'error') = (last_error IS NOT NULL)),
    CHECK (last_error IS NULL OR length(last_error) > 0),
    -- These are IMPLICATIONS, not biconditionals, and the difference is the
    -- whole point of the row. last_success_at_ms and last_error_at_ms are
    -- HISTORY: they survive the result that did not produce them, because a
    -- watcher that has been failing for an hour still needs to say when it last
    -- worked. Writing these as `(last_result = 'error') = (last_error_at_ms IS
    -- NOT NULL)` would abort the first success-after-error and the first
    -- error-after-success -- i.e. every recovery and every failure -- which is
    -- exactly the alternation this table exists to record.
    CHECK (last_result <> 'error' OR last_error_at_ms IS NOT NULL),
    CHECK (last_result = 'error' OR last_success_at_ms IS NOT NULL),
    CHECK (consecutive_errors >= 0),
    CHECK (attempt_count >= 0)
);

-- A replaced watcher returning with its old epoch matches neither upsert arm and
-- its heartbeat is refused; this trigger is the same rule applied to the row
-- itself, so that no other write path can lower the epoch or hand a new holder
-- the previous one's token.
CREATE TRIGGER watcher_liveness_epoch_is_monotonic
BEFORE UPDATE ON watcher_liveness
WHEN NEW.holder_epoch < OLD.holder_epoch
  OR (NEW.holder <> OLD.holder AND NEW.holder_epoch <= OLD.holder_epoch)
BEGIN
    SELECT RAISE(ABORT, 'a watcher liveness epoch never decreases, and a new holder must raise it');
END;

CREATE TRIGGER watcher_liveness_attempt_count_is_monotonic
BEFORE UPDATE OF attempt_count ON watcher_liveness
WHEN NEW.attempt_count < OLD.attempt_count
BEGIN
    SELECT RAISE(ABORT, 'watcher attempt_count must not decrease');
END;


-- ==========================================================================
--  6. The Gate entity (docs/production-schema.md section 9)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- gate -- a halt that requires a decision from outside the deterministic layer,
-- made durable as an entity with a rationale, options, a deadline and an
-- outcome. #65 gives it the escalation form (worker -> Secretary -> human ->
-- worker) and #64 the merge-approval form; they share this schema.
--
-- There is no owner column, and its absence is the decision: "the standing
-- responsible party" and "whoever currently holds the ball" are two different
-- things and one column cannot be both. The standing party is a property of
-- gate_type and the ball-holder is a property of stage, so both live in
-- policy_gate_stage_owner -- which lets relay-gap reporting name a responsible
-- role deterministically without a column that means different things on
-- different rows.
--
-- deadline_at_ms is the BUSINESS deadline and is not a relay tolerance. Missing
-- it produces outcome='expired', a fact about the decision owned by whoever set
-- the deadline; a relay tolerance is a property of a STAGE and produces a
-- relay_gap incident. Separate concepts, separate storage, separate
-- consequences.
--
-- The terminal taxonomy exists because 'forwarded' alone leaves a cancelled
-- run, a withdrawn question, an expired deadline, an unanswerable question or a
-- superseded question as permanently open rows that either alarm forever or are
-- silently ignored. subject_gone is the one that would otherwise alarm forever,
-- and it needs the reconcile sweep to be used at all.
-- --------------------------------------------------------------------------
CREATE TABLE gate (
    gate_id           TEXT    PRIMARY KEY,
    gate_type         TEXT    NOT NULL,
    run_id            TEXT             REFERENCES run(run_id),
    subject_kind      TEXT    NOT NULL,
    subject_id        TEXT    NOT NULL,
    origin_event_seq  INTEGER NOT NULL REFERENCES event(seq),
    rationale         TEXT    NOT NULL,
    options           TEXT    NOT NULL DEFAULT '[]',
    deadline_at_ms    INTEGER,
    stage             TEXT    NOT NULL,
    stage_seq         INTEGER,
    stage_entered_at_ms INTEGER NOT NULL,
    outcome           TEXT,
    superseded_by     TEXT             REFERENCES gate(gate_id),
    created_at_ms     INTEGER NOT NULL,
    closed_at_ms      INTEGER,

    CHECK (gate_type IN ('worker_escalation', 'merge_approval', 'plan_approval', 'risk_approval')),
    CHECK (length(subject_id) > 0),
    CHECK (length(rationale) > 0),
    CHECK (json_valid(options) AND json_type(options) = 'array'),
    CHECK (stage IN ('received', 'presented', 'answered', 'forwarded')),
    CHECK (outcome IS NULL OR outcome IN (
        'answered_and_forwarded',
        'withdrawn',
        'subject_gone',
        'expired',
        'unanswerable',
        'superseded'
    )),
    CHECK ((outcome IS NOT NULL) = (closed_at_ms IS NOT NULL)),
    CHECK ((outcome = 'superseded') = (superseded_by IS NOT NULL) OR outcome IS NULL),
    CHECK (superseded_by IS NULL OR superseded_by <> gate_id),
    CHECK (deadline_at_ms IS NULL OR deadline_at_ms > created_at_ms),
    CHECK (closed_at_ms IS NULL OR closed_at_ms >= created_at_ms),
    CHECK (stage_entered_at_ms >= created_at_ms)
);

CREATE INDEX gate_open ON gate(stage, stage_entered_at_ms) WHERE closed_at_ms IS NULL;
CREATE INDEX gate_by_run ON gate(run_id) WHERE closed_at_ms IS NULL;

-- stage / stage_seq are a PROJECTION of the transition history, and this trigger
-- is what stops them from becoming an independent second copy of the truth: the
-- projection may only name a transition that exists, belongs to this gate, and
-- actually landed on that stage.
CREATE TRIGGER gate_stage_matches_its_transition
BEFORE UPDATE OF stage, stage_seq ON gate
WHEN NOT EXISTS (
    SELECT 1 FROM gate_transition t
     WHERE t.seq = NEW.stage_seq
       AND t.gate_id = NEW.gate_id
       AND t.to_stage = NEW.stage
       -- 'open' is admitted alongside 'advance' because the opening transition
       -- is what establishes the projection in the first place. Admitting only
       -- 'advance' makes gate creation impossible: the gate is inserted with a
       -- null stage_seq, the 'open' transition is inserted, and nothing may then
       -- point the projection at it -- and the transition table has no
       -- received -> received advance to reach instead.
       AND t.transition_kind IN ('open', 'advance'))
BEGIN
    SELECT RAISE(ABORT,
        'gate.stage is a projection; it may only name an open or advance transition of this gate');
END;

CREATE TRIGGER gate_stage_seq_is_monotonic
BEFORE UPDATE OF stage_seq ON gate
WHEN NEW.stage_seq < OLD.stage_seq OR NEW.stage_seq IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a gate stage projection never walks backwards');
END;

-- Creation is the one moment the projection cannot be validated, because
-- gate_transition has a foreign key back to gate: the row must exist before its
-- opening transition can. So creation is forbidden from ASSERTING a projection
-- at all -- it opens at 'received' with a null stage_seq, and the opening
-- transition, inserted in the same transaction, sets it through the UPDATE path
-- where gate_stage_matches_its_transition governs. A gate may therefore never be
-- created already claiming to be presented, answered, or pointed at somebody
-- else's transition.
CREATE TRIGGER gate_opens_without_a_projection
BEFORE INSERT ON gate
WHEN NEW.stage_seq IS NOT NULL OR NEW.stage <> 'received' OR NEW.outcome IS NOT NULL
BEGIN
    SELECT RAISE(ABORT,
        'a gate opens at stage received with a null stage_seq; its opening transition sets the projection');
END;

CREATE TRIGGER gate_closure_is_terminal
BEFORE UPDATE ON gate
WHEN OLD.closed_at_ms IS NOT NULL
 AND (NEW.closed_at_ms IS NULL OR NEW.outcome <> OLD.outcome)
BEGIN
    SELECT RAISE(ABORT, 'a closed gate keeps its outcome; open a new gate instead');
END;

-- --------------------------------------------------------------------------
-- gate_transition -- the immutable history a single stage + updated_at cannot
-- hold: the time each stage was entered (occurred_at_ms on the advance row),
-- the actor, resends (which do not move the stage), corrections (which keep
-- both texts), and the verbatim answer (body on the advance to 'answered',
-- never paraphrased and never overwritten).
--
-- occurred_at_ms and recorded_at_ms are separate because a human answers at one
-- moment and the answer becomes durable at another: the relay tolerance for the
-- NEXT stage is measured from recorded_at_ms (our clock,
-- docs/time-base-policy.md section 2), while the human-facing latency is
-- measured from occurred_at_ms.
--
-- There is no backwards edge. A question that needs re-asking after being
-- answered is a NEW gate, linked by gate.superseded_by, not a rewind -- a rewind
-- would destroy the aging basis the relay-gap detector reads. The admissible
-- edge table itself is enforced in application code inside the appending
-- transaction, because its preconditions include "the relay's outbox row is
-- acked", which is a join a trigger cannot express.
-- --------------------------------------------------------------------------
CREATE TABLE gate_transition (
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    gate_id             TEXT    NOT NULL REFERENCES gate(gate_id),
    transition_kind     TEXT    NOT NULL,
    from_stage          TEXT,
    to_stage            TEXT    NOT NULL,
    actor_kind          TEXT    NOT NULL,
    actor_id            TEXT    NOT NULL,
    writer_epoch        INTEGER,
    message_id          TEXT             REFERENCES outbox(message_id),
    body                TEXT,
    supersedes_seq      INTEGER          REFERENCES gate_transition(seq),
    occurred_at_ms      INTEGER NOT NULL,
    recorded_at_ms      INTEGER NOT NULL,

    -- 'open'       -- the gate comes into existence (from_stage IS NULL)
    -- 'advance'    -- the stage moves; only this kind may move gate.stage
    -- 'resend'     -- the same relay is attempted again; the stage does not move
    -- 'correction' -- a previously recorded body is corrected; supersedes_seq set
    -- 'close'      -- the gate reaches a terminal outcome
    CHECK (transition_kind IN ('open', 'advance', 'resend', 'correction', 'close')),
    CHECK (from_stage IS NULL OR from_stage IN ('received', 'presented', 'answered', 'forwarded')),
    CHECK (to_stage IN ('received', 'presented', 'answered', 'forwarded')),
    CHECK ((transition_kind = 'open') = (from_stage IS NULL)),
    -- The actor is recorded, but the WRITER is Dispatcher Core even when the
    -- actor is a human: admissibility is a deterministic check against the
    -- transition table and D-0008 puts deterministic evaluation in Core's row.
    -- A human answering a question is an actor, not a writer to SQLite.
    CHECK (actor_kind IN ('worker', 'secretary', 'human', 'dispatcher_core', 'system')),
    CHECK (length(actor_id) > 0),
    CHECK (writer_epoch IS NULL OR writer_epoch > 0),
    CHECK (body IS NULL OR length(body) > 0),
    CHECK ((transition_kind = 'correction') = (supersedes_seq IS NOT NULL)),
    CHECK (supersedes_seq IS NULL OR supersedes_seq < seq),
    CHECK (typeof(occurred_at_ms) = 'integer' AND typeof(recorded_at_ms) = 'integer')
);

CREATE INDEX gate_transition_by_gate ON gate_transition(gate_id, seq);

CREATE TRIGGER gate_transition_rows_are_immutable
BEFORE UPDATE ON gate_transition
BEGIN
    SELECT RAISE(ABORT, 'a gate transition is history; correct it with a correction transition');
END;

CREATE TRIGGER gate_transition_rows_are_never_deleted
BEFORE DELETE ON gate_transition
BEGIN
    SELECT RAISE(ABORT, 'gate transition history is the relay-gap evidence');
END;

-- --------------------------------------------------------------------------
-- gate_relay -- a relay stage advances on the ACK, never on the send.
--
-- Advancing before the send loses the relay to a kill after the commit: the
-- gate looks presented when nobody saw it. Advancing after the send as its own
-- write re-sends on recovery: the human sees the question twice. Neither is
-- fixed by reordering, because the gap is between a durable write and an
-- external effect -- the case ACCEPTANCE.md section 2 says SQLite alone cannot
-- resolve. So the relay uses the outbox and the stage follows the ack, and the
-- reconcile pass completes an acked relay whose advance never landed.
--
-- The (gate_id, to_stage) primary key is what makes the ENQUEUE idempotent: a
-- restarted Secretary re-enqueuing the same relay collides and takes the
-- existing message_id, so retries accumulate on one outbox row (retry_count,
-- already durable and monotonic) rather than producing a second message. This
-- is deliberately NOT done by making outbox.dedup_key unique -- that column is
-- non-unique on purpose, and gate relays get their own identity table instead
-- of changing a shared table's semantics.
--
-- Only 'presented' and 'forwarded' are relayed stages. 'presented' is the human
-- window's durable acknowledgement that the gate entered the human-visible
-- queue -- not a read receipt, which is unobservable here and could not be
-- evaluated deterministically as a tolerance. So the presented -> answered leg
-- has no relay tolerance (a slow human is not a gap); what governs it is the
-- gate's own deadline_at_ms.
-- --------------------------------------------------------------------------
CREATE TABLE gate_relay (
    gate_id         TEXT    NOT NULL REFERENCES gate(gate_id),
    to_stage        TEXT    NOT NULL,
    message_id      TEXT    NOT NULL REFERENCES outbox(message_id),
    enqueued_at_ms  INTEGER NOT NULL,

    PRIMARY KEY (gate_id, to_stage),
    CHECK (to_stage IN ('presented', 'forwarded'))
);

CREATE UNIQUE INDEX gate_relay_by_message ON gate_relay(message_id);


-- ==========================================================================
--  7. Policy data (docs/production-schema.md section 10)
--     The values live in docs/time-base-policy.md and are inserted by the
--     migration step that accompanies them, carrying the D- entry that decided
--     them in policy_revision.decided_by.
-- ==========================================================================

-- Policy rows are versioned rather than updated in place, because the
-- measurement harness must be able to say which tolerances a past report was
-- computed under. Changing a tolerance is a new revision_id, never an UPDATE.
-- Every reader binds a revision explicitly -- the detector the one effective
-- now, a report the one effective over its period -- and a policy_* join
-- without a revision_id predicate is a defect: it matches every historical
-- tolerance for the subject and emits one incident per revision ever recorded,
-- some of them alarming on a tolerance retired months ago.
CREATE TABLE policy_revision (
    revision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    note           TEXT    NOT NULL,
    decided_by     TEXT    NOT NULL,   -- the D- entry that set these values
    effective_at_ms INTEGER NOT NULL,

    CHECK (length(note) > 0),
    CHECK (length(decided_by) > 0)
);

CREATE TABLE policy_detection_latency (
    revision_id      INTEGER NOT NULL REFERENCES policy_revision(revision_id),
    incident_class   TEXT    NOT NULL,
    threshold_kind      TEXT    NOT NULL,
    threshold_value     INTEGER NOT NULL,
    -- P for THIS class. docs/time-base-policy.md section 3.3 allows a class
    -- whose L - T is large to be evaluated on a multiple of the base reconcile
    -- period; carrying it per row is what lets the invariant below be checked at
    -- all, since a CHECK cannot reach another table for the period.
    reconcile_period_ms INTEGER NOT NULL,
    budget_ms           INTEGER NOT NULL,   -- L: onset-to-alarm ceiling; T + P <= L
    -- L is not always an absolute duration either, and this column is the
    -- symmetric partner of threshold_kind. docs/time-base-policy.md section 3.2
    -- gives lease_orphan the budget "2 x lease TTL", which no absolute
    -- millisecond value can hold: the TTL is a property of the individual lease,
    -- so precomputing it would bake one lease's TTL into a row every other lease
    -- also reads -- the same defect threshold_kind exists to prevent on the T
    -- side. Defaulted to 'absolute_ms' so that every class whose budget IS a
    -- duration reads exactly as it did before this column existed.
    --
    --   'absolute_ms'        -- L = budget_ms, in milliseconds
    --   'lease_ttl_multiple' -- L = budget_ms * (expires_at_ms - acquired_at_ms)
    budget_kind      TEXT    NOT NULL DEFAULT 'absolute_ms',

    PRIMARY KEY (revision_id, incident_class),
    -- T is not always a duration, and a single tolerance_ms column cannot say
    -- so. Three of the classes in time-base-policy.md section 3.2 are not
    -- absolute times at all: watcher_silence is a multiple of THAT SCOPE's
    -- expected_interval_ms, lease_orphan is a multiple of the lease's own TTL,
    -- and watcher_error_streak is a count of consecutive failures with no
    -- duration in it. Precomputing any of them into milliseconds would bake one
    -- scope's interval into a global row and silently mis-age every other scope.
    --
    --   'absolute_ms'             -- T = threshold_value, in milliseconds
    --   'scope_interval_multiple' -- T = threshold_value * watcher_scope.expected_interval_ms
    --   'lease_ttl_multiple'      -- T = threshold_value * (expires_at_ms - acquired_at_ms)
    --   'consecutive_count'       -- T is a COUNT, not a duration; the budget runs
    --                                from the threshold_value-th consecutive failure
    CHECK (threshold_kind IN ('absolute_ms', 'scope_interval_multiple',
                              'lease_ttl_multiple', 'consecutive_count')),
    CHECK (budget_kind IN ('absolute_ms', 'lease_ttl_multiple')),
    CHECK (threshold_value >= 0),
    CHECK (reconcile_period_ms > 0),
    CHECK (budget_ms > 0),
    -- The T + P <= L invariant is only checkable in DDL when BOTH sides are
    -- absolute. For every other combination it is a PER-SUBJECT obligation
    -- evaluated at reconcile time, because T or L depends on the subject's own
    -- interval or TTL -- that is the policy_budget_violation pass, and it
    -- asserts exactly this inequality for each live subject of a relative class.
    -- Without the budget_kind conjunct this CHECK would compare an absolute T in
    -- milliseconds against a TTL MULTIPLE in budget_ms and refuse the
    -- lease_orphan row outright, or admit it only by mangling the multiple into
    -- some assumed TTL.
    -- The FULL inequality, not `T <= L`. A row with T = L passes the weaker
    -- form and still lets the detector alarm a whole pass after its own declared
    -- ceiling, which is the ceiling being meaningless.
    CHECK (threshold_kind <> 'absolute_ms'
           OR budget_kind <> 'absolute_ms'
           OR threshold_value + reconcile_period_ms <= budget_ms)
);

CREATE TABLE policy_gate_stage_tolerance (
    revision_id   INTEGER NOT NULL REFERENCES policy_revision(revision_id),
    gate_type     TEXT    NOT NULL,
    stage         TEXT    NOT NULL,
    -- NULL = this stage is never a relay gap. It is how 'presented' opts out:
    -- the "a slow human is not a gap" case is data, not a special case in the
    -- detector's query.
    tolerance_ms  INTEGER,

    PRIMARY KEY (revision_id, gate_type, stage),
    CHECK (stage IN ('received', 'presented', 'answered', 'forwarded')),
    CHECK (tolerance_ms IS NULL OR tolerance_ms > 0)
);

CREATE TABLE policy_gate_stage_owner (
    revision_id   INTEGER NOT NULL REFERENCES policy_revision(revision_id),
    gate_type     TEXT    NOT NULL,
    stage         TEXT    NOT NULL,
    ball_holder   TEXT    NOT NULL,     -- who must act next
    standing_owner TEXT   NOT NULL,     -- who answers for the gate type overall

    PRIMARY KEY (revision_id, gate_type, stage),
    CHECK (ball_holder IN ('worker', 'secretary', 'human', 'dispatcher_core')),
    CHECK (standing_owner IN ('worker', 'secretary', 'human', 'dispatcher_core'))
);


-- ==========================================================================
--  8. The AI invocation ledger (docs/measurement-harness.md section 2.3)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- ai_invocation -- AC-9's numerator and AC-1's assertion, in one row per
-- Dispatcher AI invocation. Appended at request time and filled in once when
-- the provider's usage record arrives; the provider seam is a single adapter
-- that fills three columns and everything else in the harness is
-- provider-neutral.
--
-- usage_status exists so that a missing usage record is a FACT WITH A NAME
-- rather than an absence. Treating a missing output_tokens as 0 understates
-- Interlock's token use and therefore OVERSTATES the reduction -- a bias that
-- always flatters the target, in the criterion the target is judged by.
--
-- max_output_tokens is the caller's own ceiling, recorded at REQUEST time,
-- which is the only reason a missing invocation can be bounded at all: the
-- provider cannot return more output than the caller allowed, so imputing a
-- missing invocation at max_output_tokens * model_response_count cannot
-- understate it. A percentile of the covered sample would not bound anything --
-- telemetry loss correlates with exactly the truncated, over-long responses
-- that exceed a p95 -- so the p95 figure is printed as a sensitivity estimate
-- and never as the acceptance judgement.
--
-- AC-1 ("zero AI turns absent incidents") is the assertion that every row here
-- has an incident_id, which is why the column exists and is reported on rather
-- than folded into the count.
-- --------------------------------------------------------------------------
CREATE TABLE ai_invocation (
    invocation_id     TEXT    PRIMARY KEY,
    incident_id       TEXT             REFERENCES incident(incident_id),
    run_id            TEXT             REFERENCES run(run_id),
    provider          TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    adapter_version   TEXT    NOT NULL,
    usage_status      TEXT    NOT NULL,
    output_tokens     INTEGER,
    input_tokens      INTEGER,
    cache_read_tokens INTEGER,
    -- The output cap the CALLER sent with the request. Recorded at request
    -- time, so it is present even when no usage record ever comes back -- which
    -- is the only reason a missing invocation can be bounded at all (2.4).
    max_output_tokens INTEGER,
    -- Assistant turns the provider returned inside this invocation: 1 plus one
    -- per tool round trip. AC-9's numerator sums this column, so that Interlock
    -- is counted on the same basis as the baseline's 3,531 model responses.
    model_response_count INTEGER NOT NULL DEFAULT 1,
    attempt_count     INTEGER NOT NULL DEFAULT 1,
    started_at_ms     INTEGER NOT NULL,
    finished_at_ms    INTEGER,

    CHECK (length(provider) > 0 AND length(model) > 0),
    CHECK (length(adapter_version) > 0),
    -- 'reported'    -- the provider returned a complete usage record
    -- 'partial'     -- some fields present, output_tokens absent
    -- 'unavailable' -- no usage record at all
    CHECK (usage_status IN ('reported', 'partial', 'unavailable')),
    CHECK ((usage_status = 'reported') = (output_tokens IS NOT NULL)),
    CHECK (output_tokens IS NULL OR output_tokens >= 0),
    CHECK (max_output_tokens IS NULL OR max_output_tokens > 0),
    -- The ceiling is PER REQUEST, and an invocation makes model_response_count
    -- of them, so the invocation's ceiling is the product. Comparing the summed
    -- output against a single request's cap would fail on every tool-using
    -- invocation.
    CHECK (output_tokens IS NULL OR max_output_tokens IS NULL
           OR output_tokens <= max_output_tokens * model_response_count),
    CHECK (model_response_count >= 1),
    CHECK (attempt_count >= 1),
    CHECK (finished_at_ms IS NULL OR finished_at_ms >= started_at_ms)
);

CREATE INDEX ai_invocation_by_period ON ai_invocation(started_at_ms);
CREATE INDEX ai_invocation_by_run ON ai_invocation(run_id);
