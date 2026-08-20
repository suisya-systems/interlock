# The run-start routing point — contract of the item 10 rehearsal

**Status:** rehearsal artifact (Issue #23, gate item 10, D-0022).

> **A REHEARSAL AGAINST A SYNTHETIC COUNTERPARTY (D-0022). NOT A DISCHARGE: GATE ITEM 10 IS
> DISCHARGED AT THE CANARY ITSELF, WITH LIVE V1 AS THE COUNTERPARTY. Q-0005 REMAINS OPEN: NO
> NUMERIC GO/NO-GO CRITERION IS STATED OR USED HERE.**

The implementation in `src/claude_org_runtime/canary/` is throwaway by default (D-0026); the
durable outputs are this contract and the tests in `tests/canary/`. The discharge point for item
10 is the same audit run **at the canary itself, with live v1 as the counterparty**, under canary
duration, sample size and go/no-go criteria settled by `Q-0005` — which **remains open**: nothing
below states one, and no number a rehearsal test uses is a criterion.

**Refs.** `ACCEPTANCE.md` §1 item 10; `DECISIONS.md` D-0013 (canary shape: one worker,
run-boundary rollback), D-0022 (scoped exception: rehearse now, discharge at the canary), D-0026
(spike output is throwaway by default), D-0001 (state reconstructable by query); `Q-0005`
(unresolved — canary duration, sample size, numeric go/no-go, and the treatment of runs in flight
*on Interlock* at rollback).

## The property under rehearsal

**Rollback is a routing change, not a data migration.** That sentence is what makes the canary
cheap enough to be safe: if routing runs back to v1 required converting in-flight state or moving
records between stores, the rollback would itself be a risky deployment and the canary would have
no exit. The rehearsal makes the sentence structural — a rollback *cannot* be anything but a
routing change, because the only thing the rollback path writes is one appended routing decision.

## The boundary

The routing point sits **above both systems and above the `SessionProvider`**. The provider
contract is five verbs about sessions and carries no notion of which system owns a run; putting a
system cutover inside it would smuggle cutover semantics into the interface item 11 proved
swappable. So the routing point is its own layer, consulted **once per run, at run start, before
the first system-specific write or spawn**. It decides and records; it starts nothing — the
caller takes the answer to the owning system's own start path. It imports no other Interlock
module (not `session`, not `dispatcher`, not `control_plane`), which is asserted on the syntax
tree by `tests/canary/test_structural.py`: the routing point has no provider dependency and
survives C2 — or any later provider switch — untouched.

```
                        ┌── owning_system = interlock ──▶ Interlock start path ──▶ S5 store
 new run ──▶ RunStartRoutingPoint
             (once, at run start)
                        └── owning_system = synthetic_v1 ─▶ stand-in start path ─▶ JSONL store
```

## The ledger: two relations, deliberately not one

The routing ledger is a **separate SQLite file** — separate from the S5 control-plane database
(whose fingerprint-frozen schema must not be the thing a rehearsal edits) and from the
counterparty's store. It holds two relations:

1. **`routing_decision` — the policy for runs that have not started.** Append-only, enforced by
   trigger in both directions (no `UPDATE`, no `DELETE`, no back-filled sequence number). The
   newest row *is* the routing. **A rollback is one appended row here and nothing anywhere
   else** — there is no other rollback code path, no migration hook, no "move these runs back"
   API, and that absence is the design, not an omission.
2. **`run_owner` — the ledger for runs that have started.** Insert-only, one row per run, and the
   row admits **no update at all** by trigger: *no run changes owning system mid-flight* (the
   gate item's own sentence) is refused by the store, not by the discipline of whoever routes.
   Re-routing a started run to the same owner is an idempotent no-op (a router that crashed
   between the ledger write and the system start may retry); re-routing it to a different owner —
   which is what a retry after a policy flip amounts to — is a refused owner change.

Keeping policy and ledger in one mutable relation is the named failure mode: flipping the
decision would flip the recorded owner of runs already in flight. Splitting them makes the
mid-flight invariant independent of anything the policy does.

The ledger deliberately does **not** enter Q-0001's territory: `owning_system` names a *system*
(`interlock`, or the stand-in `synthetic_v1`) and never a component, a role, or a lease holder.
A run→system ledger folded into a component→state-item writer table would answer Q-0001 by
implementation. The vocabulary is closed and two-valued, and the stand-in is named
`synthetic_v1` rather than `v1` so that no ledger this rehearsal writes can later be read as
evidence obtained against the live counterparty.

## The writer audit

Item 10 asks that **no record be written by both systems**. The existing fencing write history
cannot answer that: `writer_epoch` is a lease generation — write authority over a resource — not
a which-system attribution, and it exists in only one of the two stores. The audit boundary is
therefore defined over three facts of the construction (`src/claude_org_runtime/canary/audit.py`):

- **The logical record key both stores share is the run** (`run_id`). "Written by both systems"
  means: one run with state in both stores.
- **Attribution is physical presence.** Each store is written by exactly one system, so *which
  store a record sits in* is *which system wrote it* — there is no writer column to trust or
  forge.
- **Enumeration is capture.** Each store is its system's only durable state, so listing a store's
  runs lists that system's writes in full.

The audit reads the stores themselves, never the ledger's opinion of them, then holds the ledger
to account against what it found: a run in a store whose ledger row names the other system is
**misrouted**; a run in a store with no ledger row at all is a write that **bypassed the routing
point**. Reports carry the runs found, not a bare verdict.

## The rollback comparison

"Changes only the routing decision" is asserted as: across the rollback, both run stores are
byte-identical, the `run_owner` relation is byte-identical, and the routing history has only been
appended to. The bytes compared are a **canonical serialisation** (tables in name order, rows in
canonical-encoding order, sorted-keys JSON values) rather than the raw database file: SQLite's
file bytes move with page headers, freelists and journal state that carry no facts, so raw-file
identity would false-alarm on a store nothing was written to — and a comparison that must be
forgiven its false alarms is not evidence. A bare row-set equality would be weaker the other way
(no stated encoding to be identical in). Both stores are serialised through the same encoding and
the digests compared; the routing relation is excluded by name and kept as rows, so the
comparison names *what* was appended rather than merely that something was.

## What the rehearsal shows — and what it cannot

`tests/canary/test_rehearsal.py` plays the canary shape (D-0013) against the stand-in with three
runs, so "one worker at a time on Interlock" and "exactly one new run routed to Interlock in
total" are separate assertions: a run started under the baseline decision (owned by
`synthetic_v1`), **exactly one** run started under the canary decision (owned by `interlock`),
a v1-started run finishing on the synthetic side mid-canary with its owner untouched, a writer
audit showing no dual write, a rehearsed rollback whose entire footprint is one appended
`routing_decision` row, and a post-rollback run owned by `synthetic_v1` again.

What it cannot show is exactly what D-0022 says it cannot: the counterparty is synthetic, so
nothing here exercises v1's real write paths, load, or failure modes, and the numeric questions —
canary duration, sample size, go/no-go thresholds, and what a real rollback does with runs then
in flight on Interlock (drain? finish? abort?) — are `Q-0005` and **remain open**. The rehearsal
shows only that the rollback itself does not touch such runs; it decides nothing about what
should happen to them, and deliberately provides no API or enum in which such a policy could be
expressed. The item is discharged **at the canary itself**, with live v1 as the counterparty
(D-0022); if that point is reached without the predicate met, that is a gate failure recorded as
such — deferred, not waived.
