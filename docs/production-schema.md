# The production control-plane schema — promotion, migration, writer assignment, and the G3/G4 DDL

**Scope.** The design half of Issues `#64` (G3: CI outcome ingestion, run↔PR linkage) and `#65`
(G4: worker-escalation ledger). This document is the **schema design accompanying the first
implementation Issue** that `Q-0001` names as the thing that would settle it. It resolves `Q-0001`
and defines the event-spine consumption contract, the CI observation identity, the run↔PR linkage,
the watcher liveness record, and the `Gate` entity.

**Status: design, not implementation.** No code and no `spike_schema.sql` change accompanies this
document. It exists so that the implementation Issues can be started against a settled DDL rather
than deriving one while writing it — which is the failure mode `D-0026` was written against.

**Decisions filed from this document.** `D-0029` (production schema and migration policy, resolving
`Q-0001`), `D-0030` (event spine consumption contract), `D-0033` (CI observation identity and
ordering), `D-0034` (run↔PR linkage), `D-0035` (watcher liveness), `D-0036` (Gate state machine),
`D-0037` (ack-gated relay stages).

**Companion documents.** [`time-base-policy.md`](./time-base-policy.md) holds every number this
document refers to as a tolerance or interval (`D-0031`, `D-0032`); nothing numeric is decided here.
[`measurement-harness.md`](./measurement-harness.md) holds the AC-9/AC-10 ground-truth contract that
reads these tables.

---

## 1. What this document decides, and the two things it deliberately does not

It decides: where the production DDL lives, how a live database is migrated, which component may
write each state item, and the columns/keys/indices/constraints for the entities G3 and G4 need.

It does **not** decide retention or scrubbing of any row below (`Q-0006`, open — every table here is
append-or-mark, never delete, which is the same posture the spike takes and is not an answer to
`Q-0006`), and it does **not** decide incident collapse semantics (`Q-0002`, open — `incident` is
reproduced below with its dedup key still non-unique, exactly as the spike has it, so both collapse
rules stay expressible).

---

## 2. Promotion is authorship, not inheritance

`D-0026` says the spike's implementations, `spike_schema.sql` named explicitly among them, are
throwaway by default and may be promoted only by a new `D-` entry. There is a weak reading of that
sentence — write the `D-` entry, then copy the file — which would satisfy the letter and defeat the
purpose. The purpose is that `Q-0001` gets answered **on its own terms**, and a copy answers it by
inertia with a `D-` number stapled to the front.

So the production schema is **authored**, not copied, and this document records for every table
whether its semantics are *carried verbatim* from the spike, *re-derived* (the spike's shape was
re-examined and either changed or re-confirmed against `Q-0001`'s own question), or *new*.
`Q-0001` asks for exactly this record: "which v1 semantics are carried verbatim and which are
re-derived".

| Table | Disposition | Note |
|---|---|---|
| `run` | **re-derived** | The spike left `status` unconstrained text *because* the writer assignment was open. §4 closes it, so the production table carries a `CHECK` on a closed status set and a forward-only trigger. |
| `session` | **carried verbatim** | The staged binding (`prepared` → `spawned` → `identity_confirmed`), the one-active-binding-per-run partial unique index, and the observation/`provider_state` equality pair are re-confirmed unchanged. They were derived from gate item 2 under injection (`docs/crash-window-orchestration.md`), not from convenience. |
| `lease` | **carried verbatim** | Epoch monotonicity, holder-change raising the epoch, resource immutability, no-delete. `docs/lease-fencing.md` is the derivation and it is unaffected by anything here. |
| `outbox` | **carried verbatim** | Including the deliberate non-uniqueness of `dedup_key`; §9.4 adds gate relay identity in a separate table rather than by tightening this one. |
| `incident` | **carried verbatim** | `Q-0002` is still open; nothing here narrows it. |
| `action` | **carried verbatim** | `exactly_once_mechanism` and the one-effect-per-key partial unique index are the `ACCEPTANCE.md` §2 clause and are unchanged. |
| `task` | **new** | Named by `D-0001` but absent from the spike (the gate items did not exercise it). Out of scope for G3/G4; §12 records it as a known hole rather than inventing it here. |
| `assessment` | **new** | Same: named by `D-0001`, absent from the spike, not exercised by G3/G4. §12. |
| `event`, `event_consumption`, `consumer`, `consumer_subscription` | **new** | §5. |
| `repository`, `pull_request`, `run_pr_link` | **new** | §7. |
| `ci_observation` | **new** | §6. |
| `watcher_scope`, `watcher_liveness` | **new** | §8. |
| `gate`, `gate_transition`, `gate_relay` | **new** | §9. |
| `policy_*` | **new** | Tolerances and owner mappings as data. Defined in [`time-base-policy.md`](./time-base-policy.md); the DDL is §10 here. |

Two conventions are carried across the whole production schema because the reasons that produced
them in the spike are unchanged:

- **Time is the caller's.** Every timestamp is `INTEGER` milliseconds since the Unix epoch, UTC,
  and carries **no `DEFAULT`**. `ACCEPTANCE.md` §2 injects clock skew across expiry boundaries; a
  column defaulted to the database's own clock makes that case untestable and hands a recovering
  process a timestamp it never chose.
- **Types are asserted by `CHECK`, not by `STRICT`.** `STRICT` arrived in SQLite 3.37 and this
  project supports Python 3.10, whose bundled library is older on some platforms.

---

## 3. Migration policy

### 3.1 Shape

The production DDL lives in `src/claude_org_runtime/control_plane/migrations/` as **numbered,
forward-only steps**: `0001_initial.sql`, `0002_....sql`, …. There is no single
`production_schema.sql` that is edited in place; step `0001` is the initial schema and every later
change is its own file. A schema whose current state can only be read by running the migrations is
harder to review, so the build also emits a generated `docs/schema-current.sql` from a freshly
migrated empty database — generated, never edited, and never the thing that is applied.

```sql
CREATE TABLE schema_migration (
    version        INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL,
    checksum       TEXT    NOT NULL,   -- sha256 of the step file's bytes
    applied_at_ms  INTEGER NOT NULL,

    CHECK (typeof(version) = 'integer' AND version > 0),
    CHECK (length(name) > 0),
    CHECK (length(checksum) = 64),
    CHECK (typeof(applied_at_ms) = 'integer')
);

CREATE TRIGGER schema_migration_rows_are_never_deleted
BEFORE DELETE ON schema_migration
BEGIN
    SELECT RAISE(ABORT, 'a migration record is the evidence the step ran; it is never deleted');
END;

CREATE TRIGGER schema_migration_rows_are_immutable
BEFORE UPDATE ON schema_migration
BEGIN
    SELECT RAISE(ABORT, 'a migration record is written once');
END;
```

`PRAGMA application_id` is set to a value **distinct from the spike's**, so a spike database and a
production database are never mistaken for one another by any tool that opens either. `PRAGMA
user_version` mirrors `MAX(version)` in `schema_migration`; the table is the authority and the
pragma is the cheap check.

### 3.2 Rules

1. **Forward-only. There are no down migrations.** A rollback is a restore of the database file, not
   a reverse step. This is the same posture `ACCEPTANCE.md` §3 takes for the canary — rollback is a
   routing change, not a data migration — and a reverse step that has never been exercised is a
   promise the recovery path cannot keep.
2. **One step, one transaction.** Each step is applied inside a single transaction together with its
   `schema_migration` row. A step that fails leaves the database at the previous version, not
   half-migrated. (SQLite supports transactional DDL, so this is achievable rather than aspirational
   — the one exception, `PRAGMA foreign_keys` toggling around a table rebuild, is called out in the
   step file when a step needs it.)
3. **An already-applied step is checksum-verified on every open.** If the bytes of `0003_x.sql`
   differ from the `checksum` recorded when it was applied, the open is **refused**. Editing a
   historical migration is how two databases silently diverge while both report the same version.
4. **A database ahead of the code is refused, never downgraded.** Opening a `version = 7` database
   with code that knows steps up to `0005` is a `ControlPlaneRefusal`, in the same family as the
   spike's `open_control_plane` refusals.
5. **A database behind the code is migrated, and the migration is a separate, explicit call.**
   `open_control_plane` does not migrate as a side effect of opening; `migrate_control_plane` does.
   This is the same separation the spike enforces between opening and creating, and for the same
   reason: `tools/org_metrics_report.py` in v1 documents the trap directly — a read-only report tool
   that used the ordinary connect helper "would happily run forward migrations". The measurement
   harness ([`measurement-harness.md`](./measurement-harness.md)) opens `mode=ro` with
   `PRAGMA query_only=ON` and can therefore never be the thing that migrates production.
6. **Corrupt state is refused, never recovered as empty.** Carried unchanged from the spike (R3).

### 3.3 What is *not* migrated

There is no migration from a spike database to a production database, and none will be written.
`spike_schema.sql`'s own header promises no migration path, `D-0026` says being depended on
promotes nothing, and `D-0013` says the cutover happens at the run boundary with no state
conversion of in-flight runs. A converter would be the fallback plan `ACCEPTANCE.md` §3 condition 6
says does not exist.

---

## 4. The SoT entity list and the per-item single-writer table

### 4.1 The entity list, extended

`D-0001` names `run`, `task`, `session`, `lease`, `incident`, `assessment`, `action`, `outbox`.
G3 and G4 add entities that are unambiguously source-of-truth state — they are resumed from,
audited, and their absence is what a recovery query notices. `D-0029` extends the list with:

`event`, `event_consumption`, `consumer`, `consumer_subscription`, `repository`, `pull_request`,
`run_pr_link`, `ci_observation`, `watcher_scope`, `watcher_liveness`, `gate`, `gate_transition`,
`gate_relay`, `ai_invocation` (G6's AC-9 ledger — DDL in
[`measurement-harness.md`](./measurement-harness.md) §2.3).

`Gate` in particular was absent from the list, which the design review flagged: `#64` and `#65`
both treat it as first-class, and `docs/parity-audit.md` §1.2 makes human gates a first-class entity
as an operator direction. It is named here rather than arriving as an implementation detail.

### 4.2 The writer table

`Q-0001` asks for "a single-writer table per state item". The rule needs one distinction the
question does not make, and without it the table cannot be written honestly:

> **Single-writer discipline governs state items that are *updated in place*. Append-only tables are
> governed by identity uniqueness instead.**

An append-only table with a unique identity has no lost-update window to protect: two producers
appending the same fact collide on the unique index and one is refused, which is the outcome
single-writer discipline exists to produce. Requiring a single appender in addition would make the
event spine unusable — `#64`'s whole point is that several producers write CI outcomes to one spine.

| State item | Mutability | Single writer | Fence |
|---|---|---|---|
| `run.status` | in-place | **Secretary** | run lease epoch |
| `run` (creation) | append | Secretary | — |
| `session` binding phase | in-place, forward-only | **Supervisor** | session lease epoch |
| `lease` | in-place (CAS) | the acquiring claimant | epoch monotonicity trigger |
| `outbox.status` / `retry_count` | in-place, forward-only | **the delivery worker holding the outbox lease** | `writer_epoch` validated inside the write |
| `outbox` (enqueue) | append | any producer | `message_id` primary key |
| `incident` | in-place | **Dispatcher Core** | core lease epoch |
| `assessment` | append | Dispatcher AI | — |
| `action` | in-place | **Secretary or a privileged runtime handler** (never the Dispatcher AI — `D-0004`, AC-6) | `writer_epoch` + one-effect-per-key index |
| `event` | append | any registered producer | `dedup_key` unique index (§5.2) |
| `event_consumption.status` | in-place | **the named consumer of that row** | consumer lease epoch |
| `consumer` / `consumer_subscription` | in-place | **Dispatcher Core** (registration) | core lease epoch |
| `ci_observation` | append | the CI watcher holding the scope lease | identity unique index (§6.2) |
| `pull_request.head_sha` / `.state` | in-place, monotonic projection | **the CI watcher holding that repository's scope lease** | scope lease epoch |
| `repository` | in-place (rename absorption) | **Dispatcher Core** | core lease epoch |
| `run_pr_link` | in-place (unlink) | **Secretary** | run lease epoch |
| `watcher_scope` | in-place | **Dispatcher Core** | core lease epoch |
| `watcher_liveness` | in-place | **the watcher instance holding that scope's lease** | scope lease epoch, validated inside the write (§8.3) |
| `gate` (projection columns) | in-place | **Dispatcher Core** | core lease epoch |
| `gate_transition` | append, immutable | any actor, *through* Dispatcher Core's append path | `writer_epoch` recorded; §9.3 |
| `gate_relay` | append | Dispatcher Core | `(gate_id, to_stage)` primary key |
| `ai_invocation` | append, then one usage fill-in | **the component that invokes the Dispatcher AI** (single by construction: the AI is on-demand and incident-triggered — `D-0003`) | `invocation_id` primary key |
| `schema_migration` | append, immutable | the migrator | exclusive transaction |

Two rows deserve a sentence each.

**`run.status` stays exclusively the Secretary's.** `Q-0001` records that v1's 2026-07-20 review
required exactly this and that neither 2026-08-17 comment restated it, leaving it unstated for
Interlock. It is restated here. Concretely it means the CI watcher does **not** move a run to
`completed` when it sees a merge: it appends a `pr_merged` event, and the Secretary — as a consumer
of that event (§5) — makes the transition. The run-completion-on-merge path in v1
(`tools/run_complete_on_merge.py`) collapsed those two roles, and the collapse is what let a
repo-resolution mistake write a foreign PR's metadata onto a run row (§7.1).

**`gate_transition` is appended through Dispatcher Core even when the actor is a human.** The actor
is recorded (`actor_kind`, `actor_id`); the *writer* is Core, because the transition's admissibility
is a deterministic check against the transition table (§9.3) and `D-0008` puts deterministic
evaluation in Core's row. A human answering a question is an actor, not a writer to SQLite.

---

## 5. The event spine

### 5.1 The problem this shape is answering

`#64` asks for a single event spine: CI outcomes are written once and every consumer (secretary AI,
delivery, completion transition) reads from it, which removes v1's push-vs-poll duplication by
construction. The design review found the gap in that sentence: **"undrained" has no meaning until
it is defined per consumer.** With one `drained_at` column on `event`, the first consumer to finish
marks the row drained and hides every other consumer's backlog — which reproduces
`tools/relay_scan.py`'s documented failure (134 terminal events accumulating undelivered for twenty
days) with a different mechanism, because a silent no-op again leaves nothing behind.

So consumption is **fanned out at append time**: one row per (event, subscribed consumer), created
in the same transaction as the event.

### 5.2 `event`

```sql
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
```

**Why `seq` is an `AUTOINCREMENT` integer and why a cursor over it is safe.** A consumer cursor is
only sound if the sequence cannot develop a gap that is filled in *later* — otherwise a consumer
that advanced past `N` would never see a row that arrives at `N-1`. SQLite serialises write
transactions (one writer at a time, in both rollback-journal and WAL mode), so `seq` is assigned in
commit order and a committed gap is permanent, never back-filled. `AUTOINCREMENT` additionally
guarantees the value is never reused after a delete — and deletes are blocked anyway.

This is a property the design **depends on**, so it is not left as folklore: the implementation
Issue carries a test that opens two connections, interleaves two appending transactions, and asserts
that no committed `seq` is ever observed out of commit order. If a future deployment ever puts this
database behind something that admits concurrent writers, that test is the thing that fails.

`occurred_at_ms` is the source clock (when the observed thing happened, as the provider reports it);
`ingested_at_ms` is ours (when the row committed). They are never conflated — see
[`time-base-policy.md`](./time-base-policy.md) §2 for which one each tolerance and each latency
measurement uses.

### 5.3 `consumer`, `consumer_subscription`, `event_consumption`

```sql
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
```

`skipped` exists so that a subscription that a consumer decides is not applicable to a particular
event settles explicitly rather than sitting `pending` forever and being reported as a backlog.
A skip records its reason in `last_error`'s sibling position — deliberately not `last_error`, since
a skip is not an error; the reason travels in the consumption's own audit event. (A `skipped` row
with no recorded reason would be indistinguishable from a consumer quietly dropping work, so the
implementation Issue carries the requirement that a skip append a `consumption_skipped` event.)

### 5.4 The append transaction

This is the contract the design review asked for by name — event append and outbox enqueue in one
transaction. Appending one event is **exactly one transaction** containing:

1. `INSERT INTO event (...)`. A `UNIQUE` violation on `dedup_key` means the fact is already on the
   spine; the whole transaction is abandoned and the append is reported as an idempotent no-op.
   Nothing else in the transaction is conditional on anything the producer remembers.
2. `SELECT` the currently-subscribed consumers for this `event_type` — **read inside the same
   transaction**, so a concurrent subscription change cannot interleave between the fan-out decision
   and the fan-out write.
3. For each subscribed consumer, `INSERT INTO event_consumption (..., status='pending')`.
4. For each subscribed consumer with `kind='delivery'`, additionally `INSERT INTO outbox
   (message_id, recipient, payload, dedup_key='event/<event_id>/<consumer_id>', status='pending',
   …)` and set the consumption row's `message_id` to it.
5. Any typed side table the event carries (`ci_observation`, §6) `INSERT`ed with its `event_seq`.

The whole thing commits or none of it does. Consequences worth stating explicitly:

- **There is no window in which an event exists with no delivery record.** That window is precisely
  v1's push-vs-poll duplication: the best-effort push and the relay scan were two delivery paths
  because neither alone was transactional with the fact. Here the enqueue *is* part of the append,
  so the outbox is the only delivery path and the reconcile pass is a backstop over the same rows
  rather than a second path.
- **This is a `transactional_with_record` mechanism** in `ACCEPTANCE.md` §2's enumeration, for the
  step from *fact* to *enqueued*. The step from *enqueued* to *delivered* remains the outbox's
  `destination_idempotency_key` mechanism, unchanged. Two steps, two mechanisms, each named.
- **A late subscription does not retroactively fan out.** A consumer registered at
  `registered_from_seq = S` gets `pending` rows for events `> S` from its registration transaction
  onward; if it needs history, the registration transaction itself back-fills consumption rows for
  `seq > registered_from_seq` in that same transaction. Either way the decision is made once, in
  one transaction, and is visible in the rows — never as a gap that someone later has to explain.

### 5.5 What "undrained" means

For a consumer `C`, an event is **undrained by C** iff a row exists in `event_consumption` with
`consumer_id = C` and `status IN ('pending','failed')`. There is no global "undrained"; the phrase
is only ever used with a consumer named.

Three derived quantities the reconcile pass and the measurement harness both use:

- **Backlog depth for C** — `COUNT(*)` of C's undrained rows.
- **Drain frontier for C** — `MIN(event_seq)` over C's undrained rows. This is the cursor-shaped
  view, derived rather than stored, so it can never disagree with the rows.
- **Head-of-line age for C** — `now_ms - event.ingested_at_ms` at the drain frontier. This is the
  quantity the `consumer_backlog` incident class ages against
  ([`time-base-policy.md`](./time-base-policy.md) §3).

### 5.6 The reconcile pass

`D-0002` keeps a low-frequency reconcile loop; `#64` requires it to cover undrained events and
watcher liveness. Its G3/G4 obligations, each a deterministic query with no AI in the path:

| Pass | Query | On a hit |
|---|---|---|
| Undrained events | consumption rows `pending`/`failed` whose head-of-line age exceeds the class tolerance | Raise a `consumer_backlog` incident against the consumer, and re-attempt `failed` rows |
| Orphaned outbox | outbox rows not `acked` older than the delivery tolerance | Re-attempt; the retry count is already durable and monotonic |
| Watcher silence | §8.4 | Raise a `watcher_silence` incident against the scope |
| Scope coverage | §8.4 | Raise a `watcher_scope_uncovered` incident |
| Gate relay gaps | §9.5 | Raise a `relay_gap` incident against the gate |
| **Acked-but-unadvanced gate relays** | §9.4 | Complete the advance transition (recovery, not an incident) |

The last row is the crash-window recovery step and is the reason the reconcile pass is not only a
detector. It is idempotent: the advance it completes is guarded by the same transition-admissibility
check as any other advance, so running it twice is a no-op.

---

## 6. CI observation: identity and ordering

### 6.1 The two failures being designed against

The review named both. First, **identity**: without one, a re-poll, a CI rerun, a PR head update and
a late arrival are indistinguishable, and the spine's dedup key has nothing to be made of. Second,
**ordering**: arrival-order last-write-wins lets a stale verdict overwrite a newer one, which
violates `D-0006`'s verdict honesty in the most direct way available — reporting a red PR as green
because the red observation was slower.

### 6.2 `ci_observation`

```sql
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

    CHECK (length(provider) > 0),
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
CREATE UNIQUE INDEX ci_observation_identity
    ON ci_observation(provider, repo_id, pr_number, head_sha, check_scope, scope_id,
                      attempt, verdict);

CREATE INDEX ci_observation_by_head
    ON ci_observation(repo_id, pr_number, head_sha, attempt DESC, occurred_at_ms DESC);
```

The corresponding `event.dedup_key` is the same tuple rendered as a string:
`ci/<provider>/<repo_id>/<pr_number>/<head_sha>/<check_scope>/<scope_id>/<attempt>/<verdict>`. So the
event-spine uniqueness and the side-table uniqueness are the same constraint expressed twice, and a
re-poll is an idempotent no-op at step 1 of the append transaction (§5.4) before anything else in it
runs.

`indeterminate` and `no_run` deserve their own line, because collapsing them into `failed` is the
v1 defect `D-0006` records. `tools/pr_watch.py` reserves `indeterminate` for a *continued* fetch
failure specifically so that a transient probe problem is not reported as a CI result; that
discipline is carried. In `D-0005` terms an `indeterminate` observation is `OBSERVATION_UNAVAILABLE`
and is never an anomaly; `no_run` is a fact about the repository, not about the change.

### 6.3 The verdict projection

Observations are evidence and are never overwritten. The **current verdict** of a PR is a
projection over them, defined so that it is monotonic in the provider's own ordering rather than in
ours:

1. Only observations whose `head_sha` equals `pull_request.head_sha` are eligible. **A head update
   invalidates prior verdicts; it does not let them be overwritten.** An observation for a
   superseded head stays in the table as evidence and is never eligible again.
2. Among eligible observations, order by `(attempt DESC, occurred_at_ms DESC, seq DESC)`. The first
   row is the projected verdict, per `check_scope`/`scope_id`.
3. A `rollup` observation is eligible only when no `check_suite`/`workflow_run` observation exists
   for that head. It is the coarse fallback `tools/pr_watch.py` reaches for on an old `gh`, not a
   peer of the fine-grained scopes.
4. **A late arrival that orders lower than the current projection is stored and does not move it.**
   That is what makes this a projection rather than last-write-wins, and it is the sentence the whole
   section exists to make true.
5. Where eligible observations disagree across scopes, the PR-level verdict is the **most severe**
   under `failed > timed_out > cancelled > indeterminate > passed`, with `no_run` treated as "no
   eligible evidence" rather than as a passing verdict. `indeterminate` outranking `passed` is
   `D-0006` again: an unobservable check is not a green one.

The projection is a view, not a column, so it cannot drift from the rows it summarises. Rule 3 —
the rollup's subordinate eligibility — is **in the view**, not only in the prose above it: a view
that returned the rollup alongside the fine-grained scopes would let a stale coarse `failed`
dominate the severity fold in rule 5 while every real check is green.

```sql
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
```

---

## 7. run↔PR linkage

### 7.1 Repository identity

The incident this is designed against is concrete and dated. On 2026-08-06, v1's run→PR tools
defaulted an omitted `--repo` to `gh repo view` — the cwd repository, always `claude-org-ja` for the
Secretary — so a cross-repo run's PR number was resolved against the wrong repository, and renga
PR #302 was recorded with claude-org-ja PR #302's branch, commit and merge time. The tool exited
`ok`. Whether it corrupted silently or failed loudly depended only on whether the home repo happened
to own that number.

Two rules follow, and both are in the DDL rather than in a convention:

```sql
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

CREATE UNIQUE INDEX repository_by_slug ON repository(provider, lower(owner), lower(name));
CREATE UNIQUE INDEX repository_by_provider_id
    ON repository(provider, provider_repo_id) WHERE provider_repo_id IS NOT NULL;
```

- **Identity is `repo_id`, never a URL string.** `owner`/`name` are mutable (GitHub renames and
  transfers preserve the repository), so the slug is a *lookup key*, matched case-insensitively,
  while `provider_repo_id` — GitHub's immutable node id — is the identity when it is available. A
  rename is absorbed by updating `owner`/`name` on the existing row, which keeps every historical
  observation attached to the same repository. Storing the URL, as v1's `pr_url` did, means a rename
  silently forks the identity and the metrics join has to guess.
- **Case is preserved in the columns and folded in the index.** `tools/resolve_run_repo.py` keeps a
  case-preserving twin of its matcher precisely because the value is handed to `gh --repo` and
  recorded in payloads; folding it in storage would corrupt those uses, and not folding it in the
  index would admit two rows for one repository.

### 7.2 `pull_request`

```sql
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
```

`(repo_id, pr_number)` is a sound natural key because GitHub never reuses a PR number within a
repository. That settles the "recreated PR" case the review raised without any extra machinery: a
recreated PR has a new number and is therefore a new row, and the old row remains as the record of
what happened.

**A reopen is a projection, not an edit**, and it has a consequence beyond this table: §8.2 retires
a watcher scope when its PR goes terminal, so a reopen must clear `watcher_scope.retired_at_ms` in
the same transaction that clears `closed_at_ms`. Without that the PR is watched again in name only.
The reconcile pass's scope-coverage query (§8.4) is the backstop that catches it if the transaction
is ever written incompletely.

`head_event_seq` is what makes a head update auditable — the projection rule in §6.3 turns on
`head_sha`, so the event that moved it must be identifiable, not merely a timestamp.

### 7.3 `run_pr_link` — the cardinality decision

```sql
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
    -- that means "we guessed from the working directory".
    CHECK (resolution IN ('project_registry', 'explicit_operator', 'provider_event')),
    CHECK (unlink_reason IS NULL OR length(unlink_reason) > 0),
    CHECK ((unlinked_at_ms IS NOT NULL) = (unlink_reason IS NOT NULL)),
    CHECK (unlinked_at_ms IS NULL OR unlinked_at_ms >= linked_at_ms)
);

-- At most ONE live primary PR per run. Supporting links are unconstrained in
-- number. A run may span repositories; a PR may be touched by several runs.
CREATE UNIQUE INDEX run_pr_link_one_live_primary_per_run
    ON run_pr_link(run_id) WHERE role = 'primary' AND unlinked_at_ms IS NULL;

CREATE INDEX run_pr_link_by_pr ON run_pr_link(pr_id) WHERE unlinked_at_ms IS NULL;
```

The cardinality question the review asked has three parts and they get three different answers:

| Question | Answer | Why |
|---|---|---|
| May one run have several PRs? | **Yes**, and across repositories | The cross-repo case is real (the 2026-08-06 incident is one), and a run that opens a follow-up PR is ordinary |
| May one PR belong to several runs? | **Yes** | A later fix run legitimately touches an earlier run's PR; forbidding it would push the second run to fabricate a duplicate PR row |
| Which PR completes the run? | **The `primary` one, and there is at most one live at a time** | This is the rule that makes completion unambiguous without forbidding the two cases above |

The completion transition therefore reads: when the `primary` link's PR reaches `state='merged'`,
the Secretary — as the `compute` consumer of `pr_merged` — may move the run to its terminal status.
`supporting` links never drive a run transition. A run may be re-pointed by unlinking the primary
(recording a reason) and linking another; the history of both stays in the table.

### 7.4 Where `repo_id` comes from

`resolution` names the source and the closed set is the enforcement. `project_registry` is the
operator-maintained project→repository mapping, which is what `tools/resolve_run_repo.py`
established as the correct default; `explicit_operator` is a human naming the repository;
`provider_event` is a repository identity that arrived inside a provider payload we were already
consuming for that PR. There is no fallback: a run whose repository cannot be resolved by one of the
three **fails to link**, and the failure is an incident, not a default. v1's tool raises
`RepoResolutionError` for exactly this reason — "so the caller can exit non-zero instead of writing a
foreign repo's PR onto the run" — and the closed `CHECK` is that sentence made unfalsifiable.

---

## 8. Watcher liveness

### 8.1 The distinctions a single `last_heartbeat_at` cannot make

Four, all named by the review, and each has a v1 incident behind it:

1. **"Polled, nothing changed" vs "poll failed"** — collapsed, a failing watcher looks healthy for
   as long as it keeps failing quickly.
2. **A stale watcher's late heartbeat** — an old instance that was replaced can keep writing, and
   the row then proves the liveness of a process nobody is relying on.
3. **A missing watcher** — a scope that *should* be watched and has no watcher at all writes no row,
   so its absence is invisible. This is `tools/relay_scan.py`'s central lesson: a silent no-op is
   indistinguishable from a clean scan, and the fix is an *unconditional* trace plus an expected
   roster to compare against.
4. **Partial coverage** — one instance covering three of five scopes looks identical to one covering
   five.

### 8.2 `watcher_scope` — the expected roster

```sql
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
    CHECK (retired_at_ms IS NULL OR retired_at_ms >= registered_at_ms)
);

CREATE INDEX watcher_scope_live ON watcher_scope(scope_kind) WHERE enabled = 1 AND retired_at_ms IS NULL;
```

The roster is what turns "no row" from invisible into detectable. A scope is created when a run's
primary PR is linked and retired when the PR reaches a terminal state — so the roster is derived
from work that exists, not maintained by hand.

### 8.3 `watcher_liveness` — the fenced, unconditional trace

```sql
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
```

**The heartbeat write is fenced, and the fence is inside the write** — the same single-statement
shape `docs/lease-fencing.md` establishes, for the same reason (`ACCEPTANCE.md` §2: expiry discovery
alone is insufficient, because the lease can expire between the check and the write):

It is an **upsert**, not an `UPDATE`, and that is not a convenience. A newly registered scope has no
`watcher_liveness` row, so a bare `UPDATE` affects zero rows on the first heartbeat of every scope —
and since zero rows is also how a stale writer is refused, the bootstrap case would be permanently
indistinguishable from a rejection. The insert arm carries the same fence as the update arm, so
neither is a way around it:

```sql
INSERT INTO watcher_liveness (
        scope_id, holder, holder_epoch, last_attempt_at_ms, last_result,
        last_success_at_ms, last_change_at_ms, last_error_at_ms, last_error,
        consecutive_errors, attempt_count)
SELECT :scope_id, :holder, :epoch, :now_ms, :result,
       CASE WHEN :result <> 'error'         THEN :now_ms END,
       CASE WHEN :result =  'observed_change' THEN :now_ms END,
       CASE WHEN :result =  'error'         THEN :now_ms END,
       CASE WHEN :result =  'error'         THEN :error  END,
       CASE WHEN :result =  'error' THEN 1 ELSE 0 END, 1
 WHERE EXISTS (SELECT 1 FROM lease
                -- The lease resource is DERIVED from the target scope, never
                -- passed alongside it. A separate :scope_lease_resource
                -- parameter lets a watcher holding a valid lease for scope B
                -- heartbeat scope A -- the row is written, the uncovered scope
                -- looks healthy, and watcher_silence never fires for it. Binding
                -- the two in the statement makes that unrepresentable rather
                -- than merely discouraged.
                WHERE resource = 'watcher_scope:' || :scope_id
                  AND holder = :holder AND epoch = :epoch
                  AND expires_at_ms > :now_ms)
    ON CONFLICT(scope_id) DO UPDATE
   SET holder = :holder, holder_epoch = :epoch,
       last_attempt_at_ms = :now_ms, last_result = :result,
       last_success_at_ms = CASE WHEN :result <> 'error'
                                 THEN :now_ms ELSE last_success_at_ms END,
       last_change_at_ms  = CASE WHEN :result = 'observed_change'
                                 THEN :now_ms ELSE last_change_at_ms END,
       last_error_at_ms   = CASE WHEN :result = 'error'
                                 THEN :now_ms ELSE last_error_at_ms END,
       last_error         = CASE WHEN :result = 'error' THEN :error ELSE NULL END,
       consecutive_errors = CASE WHEN :result = 'error'
                                 THEN consecutive_errors + 1 ELSE 0 END,
       attempt_count      = attempt_count + 1
 WHERE watcher_liveness.holder_epoch <= :epoch
   AND EXISTS (SELECT 1 FROM lease
                WHERE resource = 'watcher_scope:' || :scope_id
                  AND holder = :holder AND epoch = :epoch
                  AND expires_at_ms > :now_ms);
```

**The lease resource name is a function of the scope**, `'watcher_scope:' || scope_id`, and the
statement computes it rather than accepting it. A watcher can therefore only ever heartbeat the
scope it actually holds: a misrouted or stale heartbeat naming a different scope finds no matching
lease and writes nothing, instead of marking an uncovered scope healthy and silencing its
`watcher_silence` predicate.

A replaced watcher returning with its old epoch matches neither arm and its heartbeat is refused —
which is distinction (2). **Zero rows affected now has exactly two causes**, and the watcher
distinguishes them by one follow-up read rather than by assuming: either the lease is no longer
ours (the `EXISTS` failed) or a higher epoch holds the row. Both are stale-writer refusals, and a
refused heartbeat is not silently dropped — the watcher records an `action` row in
`status='refused'` carrying which of the two it was, per `ACCEPTANCE.md` §2's requirement that the
rejection of a stale writer be itself durable.

### 8.4 The two liveness queries

```sql
-- Silence: a scope whose watcher has stopped attempting. The multiple comes from
-- policy data, bound to the EFFECTIVE revision like every other policy read
-- (section 9.6), and it is multiplied by THAT SCOPE's own expected_interval_ms
-- -- which is why the threshold is stored as a multiple and not as milliseconds.
SELECT s.scope_id,
       :now_ms - l.last_attempt_at_ms AS silent_for_ms
  FROM watcher_scope s
  JOIN watcher_liveness l ON l.scope_id = s.scope_id
  JOIN policy_detection_latency p
    ON p.incident_class = 'watcher_silence'
   AND p.revision_id = (SELECT revision_id FROM policy_revision
                         WHERE effective_at_ms <= :now_ms
                         ORDER BY effective_at_ms DESC, revision_id DESC LIMIT 1)
 WHERE s.enabled = 1 AND s.retired_at_ms IS NULL
   AND p.threshold_kind = 'scope_interval_multiple'
   AND :now_ms - l.last_attempt_at_ms > s.expected_interval_ms * p.threshold_value;

-- Coverage: a scope that should be watched and has no liveness row at all.
-- The query a heartbeat table alone cannot express.
SELECT s.scope_id
  FROM watcher_scope s
  LEFT JOIN watcher_liveness l ON l.scope_id = s.scope_id
 WHERE s.enabled = 1 AND s.retired_at_ms IS NULL
   AND l.scope_id IS NULL;
```

A third condition — a watcher that is attempting but only ever failing — is
`consecutive_errors` crossing its threshold, and it is a *different* incident class from silence,
because the remedies differ (a dead process versus a broken credential). Both classes and their
numbers are in [`time-base-policy.md`](./time-base-policy.md) §3.

---

## 9. The `Gate` entity

### 9.1 What a gate is

A gate is a **halt that requires a decision from outside the deterministic layer**, made durable as
an entity with an owner, a rationale, options, a deadline and an outcome. `docs/parity-audit.md`
§1.2 is the operator direction; `#65` gives it the escalation form (worker → Secretary → human →
worker) and `#64` gives it the merge-approval form. They share this schema, per the audit's note
that whichever draft files first carries it.

### 9.2 `gate`

```sql
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
```

**There is no `owner` column, and its absence is the decision.** The review asked whether `owner`
means the standing responsible party or whoever currently holds the ball; the honest answer is that
those are two different things and one column cannot be both. The standing party is a property of
`gate_type`; the ball-holder is a property of `stage`. Both live in policy data
(`policy_gate_stage_owner`, §10), so relay-gap reporting can name a responsible role
deterministically without a column that means different things on different rows.

**`deadline_at_ms` is the business deadline and is not a relay tolerance.** Missing it produces
`outcome='expired'` — a fact about the decision, owned by whoever set the deadline. A relay
tolerance is a property of a *stage* and produces a `relay_gap` incident. The review asked for these
to be separated; they are separate concepts, separate storage, separate consequences.

### 9.3 `gate_transition` — the immutable history

```sql
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
```

This is what a single `stage` + `updated_at` cannot hold, item by item, as the review listed them:
the time each stage was entered (`occurred_at_ms` on the `advance` row), the actor (`actor_kind` /
`actor_id`), resends (`transition_kind='resend'`, which do not move the stage), corrections
(`correction` with `supersedes_seq`), and the verbatim answer (`body` on the `advance` to
`answered`, never paraphrased and never overwritten — a changed answer is a `correction`, so both
texts survive).

`occurred_at_ms` and `recorded_at_ms` are separate because a human answers at one moment and the
answer becomes durable at another; the relay tolerance for the *next* stage is measured from
`recorded_at_ms` (our clock, §2 of the time-base document), while the human-facing latency is
measured from `occurred_at_ms`.

**The transition table.** Admissible edges, enforced in application code inside the appending
transaction (SQLite triggers can express the shape but not the ack precondition, which is a join):

| From | To | Kind | Actor | Precondition |
|---|---|---|---|---|
| — | `received` | `open` | `worker` (via `system`) | An escalation event exists on the spine |
| `received` | `presented` | `advance` | `secretary` | The `presented` relay's outbox row is `acked` (§9.4) |
| `presented` | `answered` | `advance` | `human` | A human answer is durable; `body` non-null |
| `answered` | `forwarded` | `advance` | `secretary` | The `forwarded` relay's outbox row is `acked` (§9.4) |
| any open stage | same stage | `resend` | any | A relay attempt was repeated |
| any open stage | same stage | `correction` | any | `supersedes_seq` names an earlier transition of this gate |
| `received`/`presented`/`answered` | same stage | `close` | varies | See the terminal taxonomy below |
| `forwarded` | `forwarded` | `close` | `system` | Outcome `answered_and_forwarded` |

Every other edge is inadmissible. In particular there is **no backwards edge**: a question that
needs re-asking after being answered is a *new gate*, linked by `superseded_by`, not a rewind. A
rewind would destroy the aging basis the relay-gap detector reads.

### 9.4 Terminal states and the taxonomy

`forwarded` was the only terminus in the draft, which the review flagged: a cancelled run, a
withdrawn question, an expired deadline, an unanswerable question or a superseding question each
leaves a permanently open row that either alarms forever or is silently ignored. The taxonomy:

| Outcome | Reached from | Trigger | Relay-gap detector |
|---|---|---|---|
| `answered_and_forwarded` | `forwarded` | The forward relay is acked | Closed; not aged |
| `withdrawn` | `received`, `presented`, `answered` | The worker withdraws the question | Closed; not aged |
| `subject_gone` | any open stage | The subject run/session reached a terminal state, so there is nobody to forward to | Closed; not aged. **This is the case that would otherwise alarm forever** |
| `expired` | `presented`, `answered` | `deadline_at_ms` passed and the gate's policy says expire | Closed; not aged. Expiry is recorded as an event so the decision's absence is itself visible |
| `unanswerable` | `presented` | The human declines or cannot answer | Closed; not aged. The worker is told, via the same acked relay as `forwarded` |
| `superseded` | any open stage | Another gate replaces it; `superseded_by` set | Closed; not aged |

`subject_gone` needs a mechanism, not just a name: the reconcile pass closes gates whose subject run
has reached a terminal status. Without that sweep the outcome exists in the enumeration and never
gets used, which is the same permanent-open-row problem with extra vocabulary.

### 9.5 A relay stage advances on the **ack**, never on the send

This is the crash-window rule, and it is the one place where getting the ordering wrong produces
either a duplicated question or a lost answer:

- Advance the stage *before* sending: a kill after the commit and before the send loses the relay,
  and the gate looks presented when nobody saw it.
- Advance the stage *after* sending but as its own write: a kill between the send and the commit
  re-sends on recovery, and the human sees the question twice.

Neither is fixed by ordering the two operations differently, because the gap is between a durable
write and an external effect — exactly the case `ACCEPTANCE.md` §2 says SQLite alone cannot resolve.
So the relay uses the outbox's existing machinery and the stage follows the ack:

```sql
CREATE TABLE gate_relay (
    gate_id         TEXT    NOT NULL REFERENCES gate(gate_id),
    to_stage        TEXT    NOT NULL,
    message_id      TEXT    NOT NULL REFERENCES outbox(message_id),
    enqueued_at_ms  INTEGER NOT NULL,

    PRIMARY KEY (gate_id, to_stage),
    CHECK (to_stage IN ('presented', 'forwarded'))
);

CREATE UNIQUE INDEX gate_relay_by_message ON gate_relay(message_id);
```

The sequence, and what each kill point does:

| Step | Transaction | Killed here → |
|---|---|---|
| 1. Enqueue the relay | `INSERT INTO gate_relay` + `INSERT INTO outbox (status='pending', dedup_key='gate/<gate_id>/<to_stage>')`, one transaction | Nothing sent, nothing claimed. The reconcile pass finds a pending outbox row and the delivery worker retries |
| 2. Deliver | The outbox delivery worker, unchanged | The message may or may not have landed. The destination deduplicates on `dedup_key`, so a retry is harmless — `destination_idempotency_key` in `ACCEPTANCE.md` §2's enumeration |
| 3. Ack | `outbox.acked_at_ms` set once (existing trigger) | The ack is durable and the stage has not moved yet |
| 4. Advance | `INSERT INTO gate_transition (kind='advance', message_id=…)` + `UPDATE gate SET stage, stage_seq`, one transaction | The reconcile pass finds an acked relay with no matching advance and completes it (§5.6) |

`gate_relay`'s `(gate_id, to_stage)` primary key is what makes the enqueue itself idempotent: a
restarted Secretary re-enqueuing the same relay collides and takes the existing `message_id`, so
retries accumulate on one outbox row (`retry_count`, already durable and monotonic) rather than
producing a second message. This is deliberately *not* done by making `outbox.dedup_key` unique —
the spike's comment explains why that column is non-unique on purpose, and gate relays get their own
identity table instead of changing a shared table's semantics.

**What `presented` means.** The review asked whether it is display, notification, or read receipt.
It is **the human window's durable acknowledgement that the gate has entered the human-visible
queue** — the Secretary's ack of the relay, nothing more. It is not a read receipt, because a read
receipt is unobservable in this architecture and a tolerance measured against an unobservable event
cannot be evaluated deterministically. It is not merely "the message was sent", because that is the
crash window this whole section exists to close. `D-0016` makes the Secretary the single human
window, so its queue admission is the observable boundary that exists.

Note the consequence, stated plainly rather than hidden: **`presented` proves the question reached
the human's queue, not that a human saw it.** The `presented → answered` leg therefore has no relay
tolerance (a slow human is not a gap — `#65` says so directly); what governs it is the gate's own
`deadline_at_ms`. That separation is §9.2's, and this is where it earns itself.

### 9.6 Relay-gap detection

One query per aged stage, over open gates only, with the tolerance read from policy data rather
than compiled in:

```sql
WITH effective AS (
    -- Policy rows are versioned and never updated in place (section 10), so a
    -- join that omits revision_id matches EVERY historical tolerance for the
    -- stage: one incident per revision ever recorded, some of them alarming on
    -- a tolerance that was retired months ago. The effective revision is picked
    -- first, once, and the detector joins only its rows.
    SELECT revision_id FROM policy_revision
     WHERE effective_at_ms <= :now_ms
     ORDER BY effective_at_ms DESC, revision_id DESC
     LIMIT 1)
SELECT g.gate_id, g.gate_type, g.stage, g.stage_entered_at_ms,
       :now_ms - g.stage_entered_at_ms AS age_ms
  FROM gate g
  JOIN policy_gate_stage_tolerance p
    ON p.gate_type = g.gate_type AND p.stage = g.stage
   AND p.revision_id = (SELECT revision_id FROM effective)
 WHERE g.closed_at_ms IS NULL
   AND p.tolerance_ms IS NOT NULL
   AND :now_ms - g.stage_entered_at_ms > p.tolerance_ms;
```

Every query that reads a `policy_*` table takes the same shape — the detector binds the revision
effective **now**, a report binds the revision effective over its period
([`measurement-harness.md`](./measurement-harness.md) §6). A `policy_*` join without a
`revision_id` predicate is a defect, and the implementation Issue carries a test that inserts a
second revision and asserts the detector still emits one row per gate.

`p.tolerance_ms IS NULL` is how `presented` opts out — the "slow human is not a gap" case is data,
not a special case in the query. A separate query covers the relay that was enqueued and never
acked, which is a delivery stall rather than a stage stall:

```sql
SELECT r.gate_id, r.to_stage, o.retry_count, :now_ms - r.enqueued_at_ms AS age_ms
  FROM gate_relay r
  JOIN outbox o ON o.message_id = r.message_id
 WHERE o.status <> 'acked'
   AND :now_ms - r.enqueued_at_ms > :delivery_tolerance_ms;
```

Both are deterministic, both run in the reconcile pass, and the Dispatcher AI is nowhere in either —
`#65` requires that, and `D-0008` requires it more generally: deadline evaluation is the
deterministic layer's, and Core may name a candidate without pronouncing a verdict.

---

## 10. Policy data DDL

The values live in [`time-base-policy.md`](./time-base-policy.md); the tables are here so the schema
is in one place. Policy rows are versioned rather than updated in place, because the measurement
harness must be able to say which tolerances a past report was computed under
([`measurement-harness.md`](./measurement-harness.md) §6).

```sql
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
    -- P for THIS class. Section 3.3 of time-base-policy.md allows a class whose
    -- L - T is large to be evaluated on a multiple of the base reconcile period;
    -- carrying it per row is what lets the invariant below be checked at all,
    -- since a CHECK cannot reach another table for the period.
    reconcile_period_ms INTEGER NOT NULL,
    budget_ms           INTEGER NOT NULL,   -- L: onset-to-alarm ceiling; T + P <= L

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
    CHECK (threshold_value >= 0),
    CHECK (reconcile_period_ms > 0),
    CHECK (budget_ms > 0),
    -- The T + P <= L invariant is only checkable in DDL for absolute rows. For
    -- the relative kinds it is a PER-SUBJECT obligation evaluated at reconcile
    -- time, because T depends on the subject's own interval or TTL -- see the
    -- policy_budget_violation pass below.
    -- The FULL inequality, not `T <= L`. A row with T = L passes the weaker
    -- form and still lets the detector alarm a whole pass after its own declared
    -- ceiling, which is the ceiling being meaningless.
    CHECK (threshold_kind <> 'absolute_ms'
           OR threshold_value + reconcile_period_ms <= budget_ms)
);

CREATE TABLE policy_gate_stage_tolerance (
    revision_id   INTEGER NOT NULL REFERENCES policy_revision(revision_id),
    gate_type     TEXT    NOT NULL,
    stage         TEXT    NOT NULL,
    tolerance_ms  INTEGER,              -- NULL = this stage is never a relay gap

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
```

The detector joins the **currently effective** revision; a report joins the revision that was
effective over its period. Rows are never updated or deleted — a change is a new `revision_id`.

Because a relative threshold's `T` is only known per subject, the reconcile pass carries one more
deterministic check — **`policy_budget_violation`**: for every live subject of a relative class,
assert `T(subject) + reconcile_period_ms <= budget_ms` — the same inequality the `CHECK` above
enforces for absolute rows. A watcher scope registered with an `expected_interval_ms` so large
that three missed polls exceed the `watcher_silence` budget is a misconfiguration that would
otherwise present as a detector that is quietly slower than its stated ceiling for that one scope.
Reporting it as its own incident class keeps the budget an assertion rather than an aspiration.

---

## 11. What has been checked about this DDL

The DDL above is a design, and no implementation accompanies it — but a design whose SQL does not
parse, or whose constraints do not constrain, is not usable by the Issue that picks it up. So the
blocks in this document and in [`measurement-harness.md`](./measurement-harness.md) were applied to
an in-memory SQLite database on top of the spike schema, every parameterised query in them was
prepared, and the load-bearing constraints were exercised directly:

| Claim | Observed |
|---|---|
| A re-polled fact does not append twice (§5.2) | `UNIQUE constraint failed: event.dedup_key` |
| The spine is append-only | Both the `UPDATE` and the `DELETE` trigger fire |
| A repeated CI observation identity is refused (§6.2) | `UNIQUE constraint failed: ci_observation.provider, …` |
| A verdict outside the closed set is refused | `CHECK constraint failed: verdict IN (…)` |
| A merged PR does not reopen (§7.2) | Trigger fires |
| A second **live** primary PR per run is refused; a re-point after unlinking is accepted and both links survive (§7.3) | `UNIQUE constraint failed: run_pr_link.run_id`, then the re-point succeeds with `p1` retained as unlinked history |
| `resolution` cannot say "we guessed from the working directory" (§7.4) | `CHECK constraint failed: resolution IN ('project_registry', …)` |
| A stale watcher's heartbeat is refused by the fence (§8.3) | The current holder's fenced `UPDATE` affects 1 row; the same statement at epoch 3 against epoch 7 affects **0** |
| `gate.stage` may only name an `advance` transition of its own gate (§9.2) | Trigger fires when pointed at the `open` transition |
| A gate stage projection never walks backwards | Trigger fires |
| Gate transitions are immutable and undeletable (§9.3) | Both triggers fire |
| An outcome outside the terminal taxonomy is refused (§9.4) | `CHECK constraint failed: outcome IS NULL OR outcome IN (…)` |
| A closed gate keeps its outcome | Trigger fires |
| A second relay for the same `(gate, stage)` is refused, so the enqueue is idempotent (§9.5) | `UNIQUE constraint failed: gate_relay.gate_id, gate_relay.to_stage` |
| A migration record is written once and never deleted (§3.1) | Both triggers fire |
| A watcher bootstraps on a scope with no row, then alternates success → error → success, keeping both histories (§8.3) | The upsert affects 1 row every time; `last_success_at_ms` and `last_error_at_ms` are both non-null at the end |
| A stale watcher is still refused after the upsert change | The same statement at epoch 3 against epoch 7 affects **0** rows |
| An `indeterminate` observation is superseded by the recovered verdict, while a repeat of the same verdict is still refused (§6.2) | The recovery appends; the repeat raises `UNIQUE constraint failed`; the projection reads `failed` |
| A rollup drops out of the projection once a fine-grained scope exists (§6.3 rule 3) | `ci_current_verdict` returns only the `check_suite` row |
| A gate cannot be created already claiming a projection (§9.2) | Opening at `presented`, naming a `stage_seq`, or opening already closed all abort |
| The relay-gap detector emits one row per gate with two policy revisions on record (§9.6) | 1 row, not 2 |
| A gate can be opened end to end: create with a null projection, append the `open` transition, then point the projection at it (§9.2) | Accepted — and the projection still cannot claim a stage no transition reached, nor be asserted at creation |
| A closed, unmerged PR reopens; a merged one does not (§7.2) | `closed → open` accepted with `closed_at_ms` cleared; `merged → open` aborts |
| A scope-relative multiple, a consecutive-failure count and a TTL multiple all store losslessly, and an absolute `T` above its own budget is refused (§10) | All four rows land; the over-budget row and an unknown `threshold_kind` both raise |
| An invocation's output-token ceiling scales with its response count ([`measurement-harness.md`](./measurement-harness.md) §2.3) | 3000 tokens over 4 responses at a 1024 cap is accepted; the same 3000 over 1 response aborts |
| A watcher holding scope B's lease cannot heartbeat scope A (§8.3) | The upsert affects 1 row for B and **0** for A; A keeps no liveness row and stays `watcher_scope_uncovered` |
| A late, older head observation cannot revive superseded CI evidence (§7.2) | The newer head advances; the older one aborts, and the projection still reads the newer head |
| A `delivery` subscription without a recipient — and a `compute` subscription with one — are both refused at registration (§5.3) | Both abort; the two well-formed rows land |
| The budget `CHECK` includes the reconcile period (§10) | `T=180s + P=120s = L=300s` accepted; `T = L` and `T + P > L` both abort |
| The silence query reads its multiple from the effective policy revision and scales it by the scope's own interval (§8.4) | Quiet for 2 intervals returns nothing; quiet for 3.3 intervals returns the scope |

Two things this does **not** establish, and they are the reasons it is not a substitute for the
implementation Issue's tests. It does not exercise the `T + P ≤ L` timing behaviour, which needs the
detector. And it does not exercise the commit-order property `seq` depends on (§5.2) — that needs
two connections and interleaved transactions, and it is named in `D-0030` as a test the
implementation carries.

---

## 12. Known holes, stated rather than filled

- **`task` and `assessment`.** `D-0001` names both and neither has DDL, here or in the spike,
  because neither G3 nor G4 exercises them. They are not designed by implication: the first Issue
  that needs them writes their DDL as a migration step, against this document's conventions.
- **`Q-0002` (incident collapse, re-notification window)** stays open. `incident.dedup_key` remains
  non-unique and no window appears in any table above, so both collapse rules remain expressible and
  `ACCEPTANCE.md` §2's requirement that tests parameterise the choice still holds.
- **`Q-0006` (retention and scrubbing)** stays open. Every table here forbids `DELETE`, which is a
  posture, not a retention policy; when `Q-0006` is settled it will need a migration step and
  probably an archival table, and the no-delete triggers are the thing it will have to change on
  purpose.
- **`Q-0007` (Dispatcher AI auth identity and tier)** is what would let the writer table in §4.2 be
  enforced by permission rather than by discipline for the AI's rows. Until then, AC-6's exclusion
  of `approve`/`restart`/`close`/`reassign` from the AI's tools is the enforcement, and the writer
  table records the intent.
- **Multi-provider CI.** `repository.provider` and `ci_observation.provider` are `CHECK`ed to
  `'github'` alone. That is deliberate: `#64` says `gh` is the interface to GitHub, and gate item
  11's target shape is a thin seam plus one substitution test, not everything abstracted. A second
  provider widens the `CHECK` in a migration step and adds its substitution test at that point.
