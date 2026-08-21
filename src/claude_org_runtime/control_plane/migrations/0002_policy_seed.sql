-- ==========================================================================
--  0002 -- THE INITIAL TIME BASE, AS POLICY ROWS
--
--  D-0031 decides the detection latency budgets, the tolerances derived from
--  them and the reconcile period; D-0032 decides who holds the ball at each
--  gate stage and who answers for the gate type overall. Both entries insist
--  the numbers are DATA, not constants in code: the failure mode being avoided
--  is a tolerance that acquires authority by sitting in a source file, which
--  is D-0026's shape applied to numbers instead of schemas. This step is
--  therefore the only place the values enter the system, and every value below
--  is traceable to a row of docs/time-base-policy.md.
--
--  Nothing here is ever UPDATEd. Changing a tolerance means inserting a NEW
--  policy_revision and a fresh set of rows against it, so a report written
--  last month can still be recomputed under the tolerances it was actually
--  judged by (measurement-harness.md section 6). A step that edited these rows
--  in place would silently rewrite the past.
--
--  Callers that read these rows must bind a revision_id. D-0031's corollary:
--  a policy_* join without a revision_id predicate is a defect, because it
--  matches every revision ever recorded and alarms on retired tolerances.
-- ==========================================================================

-- --------------------------------------------------------------------------
-- The revision itself.
--
-- effective_at_ms = 0 is chosen deliberately and is not a placeholder. A
-- migration step cannot read a wall clock without becoming non-deterministic:
-- the same file applied to two databases would then produce two different
-- effective_at_ms values, and the checksum discipline of production-schema.md
-- section 3.2 rule 3 would be guarding bytes whose EFFECT still differed. Zero
-- is also the honest value -- this is the first revision, so there is no
-- earlier tolerance it could have superseded, and "effective since the
-- beginning of time" is exactly what a detector binding the revision effective
-- at :now_ms should find for any :now_ms at all. Every LATER revision is
-- inserted by a caller who supplies its own :now_ms, as the no-DEFAULT clock
-- rule (time-base-policy.md section 2, rule 2) requires.
--
-- The note text below is load-bearing: the rows that follow reference this
-- revision by subselecting on it, so it must stay unique and unedited.
-- --------------------------------------------------------------------------
INSERT INTO policy_revision (note, decided_by, effective_at_ms)
VALUES (
    'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided',
    'D-0031, D-0032',
    0
);

-- --------------------------------------------------------------------------
-- policy_detection_latency -- one row per incident class of
-- time-base-policy.md section 3.2.
--
-- reconcile_period_ms is 120000 on every row: section 3.3 derives P = 120 s
-- from min(L - T) = 2 min and notes that a class with a large L - T MAY later
-- be moved to a coarser multiple. None is moved here, because a coarser period
-- is a cost optimisation and there is no measured pass cost yet to justify one
-- -- and choosing one now would be deciding policy in a migration.
--
-- budget_kind (see the column's own comment in 0001) exists because
-- lease_orphan's L is 2 x the lease's own TTL, and an absolute-millisecond
-- column cannot hold "twice whatever that lease's TTL was". Its budget_ms is a
-- MULTIPLE, not a duration, and the T + P <= L CHECK deliberately does not
-- apply to it; the policy_budget_violation reconcile pass asserts the same
-- inequality per subject instead, once the subject's TTL is known.
--
-- Every absolute row below satisfies T + P <= L by construction. The three
-- rows that sit exactly on the bound (relay_gap, ci_outcome_undrained) are on
-- it because P was derived FROM them; they are the binding constraint, not a
-- near miss.
-- --------------------------------------------------------------------------
INSERT INTO policy_detection_latency
    (revision_id, incident_class, threshold_kind, threshold_value,
     reconcile_period_ms, budget_ms, budget_kind)
VALUES
    -- T is the TIGHTEST stage tolerance of section 4 (received, 3 min); the
    -- per-stage values live in policy_gate_stage_tolerance and the class row
    -- carries the one that binds the period. L = 5 min is v1's 3-minute
    -- relay-gap loop plus headroom for one missed pass.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'relay_gap', 'absolute_ms', 180000, 120000, 300000, 'absolute_ms'),

    -- A relay enqueued and never acked is a delivery-layer fault, aged over
    -- gate_relay joined to outbox rather than over the stage, which is
    -- legitimately unchanged; same 5 min budget, same pass.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'relay_delivery_stall', 'absolute_ms', 120000, 120000, 300000, 'absolute_ms'),

    -- The canary's run completes via a PR (#64); learning of a completion
    -- later than v1 did is precisely the AC-10 regression, so this shares the
    -- tightest budget.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'ci_outcome_undrained', 'absolute_ms', 180000, 120000, 300000, 'absolute_ms'),

    -- The same fact generalised to non-CI consumers; a head-of-line backlog is
    -- a slower-moving condition than one missed relay, so both T and L relax.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'consumer_backlog', 'absolute_ms', 300000, 120000, 600000, 'absolute_ms'),

    -- Relative to THAT scope's expected_interval_ms, never precomputed: three
    -- missed polls distinguish a stopped watcher from a slow one, and baking a
    -- millisecond value here would mis-age every scope with a different
    -- interval. v1's equivalent failure went unnoticed for 20 days, so any
    -- bounded L is the improvement.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'watcher_silence', 'scope_interval_multiple', 3, 120000, 600000, 'absolute_ms'),

    -- A COUNT, not a duration: five consecutive failures. Separate from
    -- silence because the remedy differs -- a broken credential, not a dead
    -- process.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'watcher_error_streak', 'consecutive_count', 5, 120000, 600000, 'absolute_ms'),

    -- T = 0 because there is nothing to wait for: an enabled scope with no
    -- liveness row at all is wrong the moment it exists.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'watcher_scope_uncovered', 'absolute_ms', 0, 120000, 600000, 'absolute_ms'),

    -- Deliberately generous: NO_ACTIVITY_EVIDENCE is not an anomaly (D-0005,
    -- D-0006), the p90 run is 2.55 h, and quiet stretches are ordinary. This
    -- class asks for an assessment and never pronounces a verdict.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'session_no_evidence', 'absolute_ms', 600000, 120000, 900000, 'absolute_ms'),

    -- D-0006: an outage of the observation path must not be able to look like
    -- a fleet-wide worker failure, so it is its own class with its own alarm.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'observation_unavailable', 'absolute_ms', 300000, 120000, 600000, 'absolute_ms'),

    -- The only relative BUDGET in the set: L = 2 x the lease's own TTL, so
    -- budget_ms carries the multiple 2 and budget_kind says how to read it.
    -- Staleness here is defined by the TTL, so expressing either side of the
    -- inequality in absolute milliseconds would be asserting something about a
    -- lease whose TTL this row cannot see.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'lease_orphan', 'lease_ttl_multiple', 1, 120000, 2, 'lease_ttl_multiple');

-- --------------------------------------------------------------------------
-- policy_gate_stage_tolerance -- the staged form #65 argues for.
--
-- A single open-past-deadline predicate would both false-alarm on slow humans
-- and miss a dropped forward. Splitting the gate's life into stages and giving
-- each its own tolerance is what makes those two outcomes separable.
--
-- The presented stage stores tolerance_ms = NULL, and that NULL is the whole
-- mechanism: "never a gap" is expressed as an absent row-value so the
-- relay-gap detector (production-schema.md section 9.6) joins this table and
-- simply does not match. If instead the query special-cased stage='presented',
-- a future gate type could be handed a human tolerance by accident, and the
-- exemption would live in the detector where nobody versions it. A slow human
-- is governed by the gate's own deadline_at_ms, whose breach is
-- outcome='expired', not a relay_gap incident.
--
-- merge_approval takes the same shape as worker_escalation (section 4): same
-- 3 min / 2 min legs, same untimed human stage. Only the standing owner
-- differs, and that lives in policy_gate_stage_owner below.
--
-- No forwarded row: the stage is terminal, the gate is closed, and there is
-- nothing left to be late for.
--
-- plan_approval and risk_approval are deliberately NOT seeded.
-- time-base-policy.md decides no numbers for them, so any tolerance inserted
-- here would be a policy decision taken in a migration file -- exactly what
-- D-0031 puts in versioned data to avoid. They are added by a later revision
-- once a D- entry decides their values.
-- --------------------------------------------------------------------------
INSERT INTO policy_gate_stage_tolerance (revision_id, gate_type, stage, tolerance_ms)
VALUES
    -- The Secretary has the request durably and has not put it in front of the
    -- human; nothing here waits on anyone outside the machine.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'worker_escalation', 'received', 180000),
    -- NULL = never a gap. See the block comment above.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'worker_escalation', 'presented', NULL),
    -- The tightest tolerance in the system: the answer is durable and the
    -- worker does not have it. This is the leg v1 actually dropped, and work
    -- is blocked on it.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'worker_escalation', 'answered', 120000),

    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'merge_approval', 'received', 180000),
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'merge_approval', 'presented', NULL),
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'merge_approval', 'answered', 120000);

-- --------------------------------------------------------------------------
-- policy_gate_stage_owner -- D-0032's answer to "is a gate's owner the
-- standing responsible party or whoever currently holds the ball?".
--
-- It is both, and that is exactly why neither is a column on gate: one field
-- cannot mean two things on different rows, and whichever meaning it took, the
-- other would have to be inferred. Worse, an owner stored on the gate row can
-- DRIFT from the stage -- the gate advances, the column does not, and a
-- relay_gap incident then names the wrong role. Deriving both from
-- (gate_type, stage) in versioned policy makes drift unrepresentable, and lets
-- a report say who the owner WAS during its period by joining the revision
-- that was effective then.
--
-- ball_holder is a function of (gate_type, stage) and is who a relay_gap
-- incident names; standing_owner is a function of gate_type alone and does not
-- change as the gate advances. worker_escalation stands with the Secretary
-- (D-0016: the single human window); merge_approval stands with the human,
-- since approving a merge is the human's decision to answer for.
--
-- No forwarded rows, for either gate type. time-base-policy.md section 4 gives
-- that cell as "--" on every column, ball holder included: the stage is
-- terminal, the gate is closed, and no one holds the ball. Seeding a row to
-- satisfy the NOT NULL ball_holder would be deciding policy in a migration
-- file, which section 1 of that document forbids -- and the invented value
-- would be the one a report cites, so the invention would not stay quiet.
--
-- Nothing needs the row. standing_owner is a function of gate_type alone, so it
-- stays readable for both types from the received / presented / answered rows
-- that remain. And the relay-gap detector never names a closed gate: it matches
-- through policy_gate_stage_tolerance, which has no forwarded row either (see
-- the block above), so there is no stage here whose ball holder it could ask
-- for.
--
-- plan_approval / risk_approval are omitted here for the same reason as above.
-- --------------------------------------------------------------------------
INSERT INTO policy_gate_stage_owner (revision_id, gate_type, stage, ball_holder, standing_owner)
VALUES
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'worker_escalation', 'received', 'secretary', 'secretary'),
    -- The human stage: the ball is genuinely theirs, which is also why the
    -- stage carries no tolerance.
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'worker_escalation', 'presented', 'human', 'secretary'),
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'worker_escalation', 'answered', 'secretary', 'secretary'),

    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'merge_approval', 'received', 'secretary', 'human'),
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'merge_approval', 'presented', 'human', 'human'),
    ((SELECT revision_id FROM policy_revision
       WHERE note = 'initial time base: detection latency budgets, gate stage tolerances and gate stage owners as first decided'),
     'merge_approval', 'answered', 'secretary', 'human');

