-- ==========================================================================
--  THE ROUTING LEDGER -- item 10 rehearsal (Issue #23)
--
--  *** A REHEARSAL AGAINST A SYNTHETIC COUNTERPARTY (D-0022). NOT A
--  DISCHARGE: GATE ITEM 10 IS DISCHARGED AT THE CANARY ITSELF, WITH LIVE V1
--  AS THE COUNTERPARTY. Q-0005 REMAINS OPEN: NO NUMERIC GO/NO-GO CRITERION
--  IS STATED OR USED HERE. ***
--
--  This ledger is the routing point's own durable record, and it is a
--  SEPARATE store on purpose -- separate from the spike control-plane
--  database (S5) and separate from the synthetic counterparty's store. It is
--  neither system's run state; it is the record of which system OWNS each
--  run, held by the layer that sits above both. Two boundaries follow:
--
--    * It is throwaway (D-0026), like every other spike implementation, and
--      it deliberately does NOT join Q-0001's territory: no component, no
--      role, no lease holder appears here. owning_system names a SYSTEM
--      (interlock, or the synthetic v1 stand-in), never a component within
--      one -- folding this into a component->state-item writer table would
--      answer Q-0001 by implementation.
--    * It never touches the spike schema. The S5 database is refused at any
--      other shape (D-0026), and the rollback property below depends on the
--      run stores NOT changing when routing does.
--
--  Two relations, deliberately not one. A single mutable "who owns runs" row
--  would let a routing change rewrite history: flipping the decision would
--  flip the recorded owner of runs already in flight, which is exactly the
--  mid-flight owner change item 10 forbids. So:
--
--    * routing_decision is the POLICY for runs that have not started yet.
--      It is append-only; the newest row is the routing. A rollback is one
--      appended row here and nothing anywhere else -- that is the property
--      the canary is cheap because of.
--    * run_owner is the LEDGER for runs that have started. Insert-only,
--      one row per run, and the owning system of a row is immutable by
--      trigger: a run never changes owner mid-flight, whatever the policy
--      does after it started.
--
--  Time is the caller's, as everywhere in this codebase: every timestamp is
--  INTEGER milliseconds since the Unix epoch, UTC, NOT NULL, no DEFAULT.
--  Order of authority among decisions is decision_seq, never the clock.
-- ==========================================================================

-- --------------------------------------------------------------------------
-- routing_decision -- where NEW runs go, as an append-only history.
--
-- The rehearsed rollback is `INSERT INTO routing_decision` with the previous
-- owning system, and nothing else. Everything the rollback is allowed to
-- change lives in this table; the audit compares the stores across a rollback
-- excluding exactly this relation.
--
-- owning_system is a closed two-value vocabulary because the canary shape
-- (D-0013) has exactly two systems, and because the stand-in is named
-- synthetic_v1 rather than v1 so a ledger written by the rehearsal can never
-- be mistaken for one written against the live counterparty.
-- --------------------------------------------------------------------------
CREATE TABLE routing_decision (
    decision_seq   INTEGER PRIMARY KEY,
    owning_system  TEXT    NOT NULL,
    decided_at_ms  INTEGER NOT NULL,
    reason         TEXT    NOT NULL,

    CHECK (typeof(owning_system) = 'text' AND typeof(reason) = 'text'),
    CHECK (typeof(decided_at_ms) = 'integer'),
    CHECK (owning_system IN ('interlock', 'synthetic_v1')),
    CHECK (length(reason) > 0)
);

-- The newest decision is the routing, so an insert that back-fills a smaller
-- sequence number would silently change which decision is newest without
-- appending anything. An omitted decision_seq is assigned by SQLite as
-- max+1 (rows are never deleted, so rowids never recycle), and an explicit
-- one must extend the history, not rewrite its order. AFTER rather than
-- BEFORE, because an omitted INTEGER PRIMARY KEY is undefined in a BEFORE
-- INSERT trigger -- the auto-assigned value exists only after the insert --
-- and RAISE(ABORT) in an AFTER trigger still undoes the statement.
CREATE TRIGGER routing_decision_is_appended_in_order
AFTER INSERT ON routing_decision
WHEN NEW.decision_seq < (SELECT MAX(decision_seq) FROM routing_decision)
BEGIN
    SELECT RAISE(ABORT, 'routing decisions are appended in order; the newest row is the routing');
END;

-- Append-only in both directions: a decision, once taken, is history. An
-- edited decision would make "what was the routing at the time?" unanswerable
-- from the ledger, and a deleted one would erase the rollback's own evidence.
CREATE TRIGGER routing_decision_is_never_edited
BEFORE UPDATE ON routing_decision
BEGIN
    SELECT RAISE(ABORT, 'a routing decision is never edited; append a new one');
END;

CREATE TRIGGER routing_decision_is_never_deleted
BEFORE DELETE ON routing_decision
BEGIN
    SELECT RAISE(ABORT, 'routing decisions are rollback evidence and are never deleted');
END;

-- --------------------------------------------------------------------------
-- run_owner -- which system owns each STARTED run. Insert-only, one row per
-- run, owner immutable for the row's lifetime.
--
-- "No run changes owner mid-flight" is enforced here by the database, not by
-- the discipline of whoever routes: the UPDATE trigger refuses every update,
-- including a no-op one, because there is nothing on this row that is
-- legitimately updatable. Re-routing the same run to the same owner is
-- handled above this table as an idempotent no-op (a crashed router may
-- retry); re-routing it to a DIFFERENT owner is refused as an owner change.
-- --------------------------------------------------------------------------
CREATE TABLE run_owner (
    run_id         TEXT    PRIMARY KEY,
    owning_system  TEXT    NOT NULL,
    decision_seq   INTEGER NOT NULL REFERENCES routing_decision(decision_seq),
    routed_at_ms   INTEGER NOT NULL,

    CHECK (typeof(run_id) = 'text' AND typeof(owning_system) = 'text'),
    CHECK (typeof(decision_seq) = 'integer' AND typeof(routed_at_ms) = 'integer'),
    CHECK (length(run_id) > 0),
    CHECK (owning_system IN ('interlock', 'synthetic_v1'))
);

CREATE TRIGGER run_owner_never_changes_mid_flight
BEFORE UPDATE ON run_owner
BEGIN
    SELECT RAISE(ABORT, 'a run never changes owning system mid-flight (gate item 10)');
END;

-- NOTE for both delete triggers in this file: they guard the INSERT OR
-- REPLACE path only on a connection with PRAGMA recursive_triggers = ON --
-- with it off (SQLite's default) the implicit conflict-resolution DELETE
-- fires no trigger at all. The pragma is per-connection, so ledger.py sets
-- it in _configure() on every connection it hands out, and the tests
-- exercise OR REPLACE through exactly those connections.
CREATE TRIGGER run_owner_rows_are_never_deleted
BEFORE DELETE ON run_owner
BEGIN
    SELECT RAISE(ABORT, 'run ownership rows are writer-audit evidence and are never deleted');
END;
