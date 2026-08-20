# The lease, the fencing token, and what each destination can do about a stale one

**Scope.** S6 (Issue `#13`), phase 4 of the Agent View spike. Prerequisite for gate items 4 and 5;
the injection matrix itself is `#16`. Implementation:
`src/claude_org_runtime/control_plane/lease.py`, tests `tests/control_plane/test_lease.py`.

**Status: spike scaffold, throwaway by default (D-0026).** The implementation may be discarded and
its tests are the durable half. Nothing here is promoted by having discharged a gate item, and
`Q-0001` — which component may hold which resource — stays open: `holder` is an opaque claimant
identity throughout and deliberately never a role.

---

## 1. The rule, and the wrong answer it is written against

`ACCEPTANCE.md` §2 states both in one breath: **expiry discovery alone is insufficient**, because
the lease can expire between the check and the write. So the module offers no `is_held()` for a
caller to consult before writing, and `Lease.looks_live_at()` says in its own docstring that it must
never gate a write. Every protected write carries the lease epoch and validates it **inside the
write**:

```sql
UPDATE outbox
   SET status = 'delivered', writer_epoch = :fence_epoch
 WHERE message_id = :message_id
   AND EXISTS (SELECT 1 FROM lease
                WHERE resource = :fence_resource AND holder = :fence_holder
                  AND epoch = :fence_epoch AND expires_at_ms > :fence_now_ms)
```

`protected_write()` accepts only a `FencedStatement`, which `fenced_update()` / `fenced_insert()`
alone can issue. The builders take **no SQL text from a caller** (#42): a statement is composed from
typed column / operator / value objects — `param()`, `value()`, `increment()`, `fence_epoch`, and
predicates from `eq()` / `ne()` / `is_null()` / `and_()` — and the builder renders every character of
SQL itself. The table is picked from a closed set (a name is interpolated, and one carrying its own
SQL can comment the fence out of the statement entirely); column and parameter names must be bare
identifiers, which cannot close a parenthesis, open a comment, or smuggle a quote; and a `value()`
constant is rendered by SQLite's own quoting rules, so however it is spelled it is data, not
structure. The builders also refuse to assign the columns a row is attributed by, and an
update to `action` carries `applied_at_ms IS NULL`, so finished evidence is added to and never
replaced — a substring check over SQL text is not enough, because a statement can carry
`FENCE_SQL` verbatim inside a `SET` expression while its `WHERE` gates nothing, change its row under
a stale token, and report a positive `rowcount`. The builders put the fence in the write's own
predicate and nowhere else, and they also require `writer_epoch` to be assigned `fence_epoch` (the
mapping holds one value per column, so a second assignment cannot hide behind the first). So neither
the unfenced shape nor an unreadable history reaches the database through this module. The transaction is `BEGIN IMMEDIATE`: the write lock is held from
before the statement until after its outcome has been classified, which is what makes "the token was
stale" distinguishable from "the caller's own `WHERE` matched nothing" without a second connection
changing the lease in between.

**Why this and not the provider's refusal.** Under C2 the provider's own `already in use` refusal
has a measured admission window in which two writers both exited 0 and both wrote (U27), and the
`--resume` path — the one taken after a crash — excludes nothing at all (U32); see
`investigation/pre-spawn-fence-search.md` §5.3. This lease is therefore the only exclusion in the
system. The suite asserts there is no import edge from the lease module to
`claude_org_runtime.session`, and every case passes with the provider's refusal assumed absent
because no case involves a provider at all.

## 2. Exclusion is the epoch's, never the clock's

Time is the caller's everywhere: every function takes `now_ms`, and the schema gives no timestamp a
`DEFAULT` so the database cannot supply one of its own.

Under clock skew, two holders really can overlap in **true** time: a claimant whose clock runs fast
sees a lease as expired while its holder still believes it live, and takes it over. The rows cannot
show that, and this is the limit worth stating plainly — each claimant stamps its acquisition in its
own frame, so the recorded windows come out disjoint while the real ones are not. A timeline of lease
rows is only ever as truthful as the clocks that wrote it.

What the recorded windows *do* prove is worth having: `claimed_timeline()` plus `overlapping_claims()`
check, over the whole timeline rather than at sampled points, that no recorded instant had two
holders — a takeover is stamped at or after the previous expiry, so an overlap here would mean a
lease row mutated outside `acquire()`. The suite runs both: the recorded windows are disjoint, and the
same events re-expressed in one clock are not.

What cannot overlap is **write authority**. Taking the lease over raises the epoch, and from that
instant the older token matches nothing: `authority_timeline()` orders by epoch, never by timestamp,
and each epoch's authority ends when its successor exists. The durable half is
`write_history()` — every fenced attempt stamps the epoch it was written under, refused attempts
included — and `applied_epoch_regressions()` reads the single-writer property back out of it by
query, from SQLite alone (D-0001).

The directions of skew, each handled and each tested:

| Skew | What it does | Why it is safe |
|---|---|---|
| Claimant's clock **fast** | Takes over a lease its holder still believes live | The takeover raises the epoch; the old holder's next write is refused and the refusal recorded. True-time windows overlap and the rows cannot show it; write authority does not overlap either way. |
| Claimant's clock **slow** | Sees a live lease as *more* live | Acquisition requires `expires_at_ms <= now_ms`, so it declines to take over. The safe direction. |
| Holder's clock **slow** on renewal | Shortens its own lease | Its authority ends earlier, never later. A renewal landing at or before the acquisition is refused outright (`ClockSkewRefused`) rather than hitting the row's `expires_at_ms > acquired_at_ms` CHECK from inside what the caller thought was a renewal. |
| Holder's clock **any** after expiry | — | An expired lease is not renewable. A returning holder must re-acquire, and re-acquiring raises the epoch — which is exactly what invalidates the token it came back with. |

## 3. Refusals are recorded, never dropped

A protected write refused for a stale token becomes an `action` row in status `refused`, carrying the
reason, the epoch that was refused, and the lease row as it actually stood. Two deliberate details:

- **The refusal record is written unfenced.** The refusal exists precisely because the writer's token
  was not live, so a fenced insert could never land and the rejection would vanish exactly when it
  matters. It rides inside the same transaction as the refused attempt, so attempt and record commit
  together.
- **A write that missed its own `WHERE` records nothing.** `ProtectedWriteMissed` is a separate
  refusal from `StaleWriterRefused`; writing a "stale writer" row for a write whose target simply did
  not exist would put a rejection that never happened into the evidence gate item 5 is read out of.

Refused rows are excluded from the schema's `action_one_effect_per_key` index, so a writer that keeps
coming back is recorded every time without any of those records becoming the thing that admits a
second effect.

## 4. Destination register — where a stale token can be enforced, and where it cannot

`ACCEPTANCE.md` §2 asks that where an external destination can enforce a stale token it does, and
where it cannot, that this is **written down rather than assumed away**. The register below is the
written-down half; `DESTINATIONS` in `lease.py` is the same table as data, a destination that cannot
enforce is refused at construction unless it carries a residual, and the suite asserts the two agree
name for name. A residual that drifts out of the code is a residual nobody is holding any more.

The entry type is `DestinationFencing`, named for the property rather than the place: S7's
`control_plane.destination.Destination` is the delivery *target* itself, and the two coexist in one
package rather than one shadowing the other.

| Destination | Enforces a stale token? | How, or why not | Residual |
|---|---|---|---|
| `control_plane_sqlite` | yes | The fence is a clause of the write itself, evaluated by SQLite in the same statement, so a stale epoch changes no row and the refusal is recorded as an `action` row. | — |
| `reference_epoch_guarded_destination` | yes | `EpochGuardedDestination` keeps its own highest-epoch-seen record per resource and rejects anything below it, and deduplicates by effect key. Its own record is the evidence, which is what §2 requires of an external effect. | — |
| `session_provider_child_process` | no | A spawned `claude -p` child takes no token and keeps no effect record. Its own duplicate refusal is not a substitute: U27 measures an admission window in which two writers both exited 0 and both wrote, and U32 finds no exclusion on the `--resume` path. | Effects on it must be `transactional_with_record` — the control-plane row and the spawn decision commit together — or a human gate (D-0004). Nothing in the spike treats the provider's own refusal as a fence. |
| `worktree_filesystem` | no | A file write carries no epoch, and the filesystem has no idempotency surface to reject one with. | The control-plane row is written under the fence first and the file write is derived from it, so a stale writer never reaches the filesystem; a write that must happen the other way round is a human gate (D-0004). Gate item 7 covers the worktree lifecycle itself and is not answered here. |

## 5. Known limits, stated rather than assumed away

- **The spike schema keeps one lease row per resource and no history table.** `authority_timeline()`
  therefore reconstructs the timeline from the row states the caller observed, while the durable,
  query-answerable evidence is `write_history()` over `action`. Which table records lease history is
  `Q-0001` and open; adding one here would answer it by inertia.
- **`write_history()` reads `action`, and only `action`.** A protected write to another table — S7's
  `outbox` is the case in point — stamps `writer_epoch` on its own row, and its history is read there
  by the same shape of query. Nothing synthesises an action row per protected write, deliberately:
  `action` is the exactly-once *effect* record guarded by `action_one_effect_per_key`, and
  manufacturing a row for a write that is not an effect would corrupt the evidence gate item 4 is
  read out of. Refusals are the exception and are always recorded in `action`, whatever table was
  being written, because a refused write has no row of its own to carry the stamp.
- **The history is ordered by `rowid`**, the database's own insertion order, never by
  `created_at_ms`. The timestamp is the caller's clock — that is the point of the whole module — so
  under injected skew it can disagree with the order the writes actually happened in, and an ordering
  claim read out of a skewed clock would manufacture regressions and hide real ones.
- **`action` has no resource column**, for the same reason, so nothing in a row says which lease
  allocated its `writer_epoch`. Two resources' epochs are independent, so comparing them would report
  a valid epoch 2 for one and a valid epoch 1 for another as a violation while hiding a real
  interleaving in the same noise. The spike's way out is `effect_kind(resource, effect)`, which
  encodes the resource in `action.kind`; `write_history()` filters on it and
  `applied_epoch_regressions()` refuses a history that mixes kinds at all. This is a workaround, not
  a design — a real schema carries the resource as a column.
- **Releasing only ever shortens**, and is never a DELETE (the schema blocks the DELETE outright,
  since a deleted row would let the next acquisition restart the epoch at 1). The new expiry is
  `MIN(expires_at_ms, MAX(acquired_at_ms + 1, now_ms))`: the inner clamp keeps a clock skewed behind
  the acquisition from violating the row's `expires_at_ms > acquired_at_ms` CHECK, and the outer one
  keeps a late release from pushing an already-expired lease's expiry *forward*, which would revive
  the releasing holder's own token over an interval it had already lost. The inner clamp still leaves
  at most a one-millisecond window in which a just-released lease reads as live; it withholds the
  resource rather than handing it to a second claimant, which is the safe direction.
- **A protected write must own its transaction.** A lease operation nested inside somebody else's
  open transaction would commit on their schedule, and a recorded refusal would be exactly as durable
  as whatever they decide to do next; the module refuses rather than inheriting a transaction.
- **~~The fence is composed into caller-supplied SQL fragments by text.~~ Discharged by #42.** The
  builders now take no SQL text from a caller at all: statements are composed from typed column /
  operator / value objects and the builder renders every character itself, so the S6 lexer defences
  (literal blanking, the structural scan over fragments) are unnecessary by construction and were
  removed. What remains textual is the table name, still chosen from a closed set, and identifiers,
  which the identifier check keeps names rather than fragments.
- **The applied-evidence guard is this module's, not the schema's.** A protected write cannot restamp
  an applied `action` row, but a direct `UPDATE` outside this module still can: the schema freezes the
  idempotency key, the applied instant and the refusal, and not `writer_epoch` or `kind`. Making it a
  trigger means editing `spike_schema.sql`, which changes the schema fingerprint and therefore
  **refuses every existing database** (D-0026 promises no migration) — a decision that belongs with
  the schema and with S7, which is being built against it in parallel, rather than being slipped in
  from here.
- **SQLite cannot tell a completed side effect from one that never started** (`ACCEPTANCE.md` §2).
  The fence orders writes; it does not close that window. Every protected write names its
  exactly-once mechanism, and where neither mechanism is achievable the action is a human gate
  (D-0004) rather than an automatic retry.
