# The measurement harness — AC-9's denominator, AC-10's ground truth, and what makes a report reproducible

**Scope.** The design half of Issue `#67` (G6). Defines what AC-9 and AC-10 are measured over, where
their ground truth comes from, how the AC-7 canary divergence report correlates two systems, and
what a report must record about itself to be reproducible.

**Status: design, not implementation.** Decisions filed from this document: `D-0038` (AC-9
denominator and coverage), `D-0039` (AC-10 ground truth), `D-0040` (report provenance).

**Companion documents.** [`production-schema.md`](./production-schema.md) is the schema this reads;
[`time-base-policy.md`](./time-base-policy.md) holds the budgets AC-10 is judged against.

---

## 1. The instrument is read-only, and that is enforced

Carried verbatim from v1's `tools/org_metrics_report.py`, whose header records why: the ordinary
connect helper applies `journal_mode=WAL` and "would happily run forward migrations", both of which
are writes a report tool must never trigger. So the harness opens the database with the SQLite
`mode=ro` URI **and** `PRAGMA query_only=ON`, and it never calls `migrate_control_plane`
(`production-schema.md` §3.2 rule 5 keeps migration a separate, explicit call precisely so this is
possible).

`ACCEPTANCE.md` §3 condition 5 requires the shadow path to be read-only **enforced by capability,
not by convention**. Two connections, two enforcements: the mode is the capability, and the harness
holds no lease and no writer epoch, so even a bug cannot produce a fenced write.

---

## 2. AC-9 — reduction in AI prompts and output tokens

### 2.1 The denominator

AC-9 is stated "per 100 worker runs", which is a normalisation, not a cohort. The cohort has to be
decided, and the review named the choice: started, completed, or canary-owned.

> **The cohort is: runs that reached a terminal status inside the report period, and that were
> Interlock-owned for their entire life.**

Three reasons, in order of weight:

1. **A started-run cohort is right-censored by construction.** A run that is still in flight at the
   period's end has produced some of its prompts and not others; counting it deflates the per-run
   figure by exactly the amount of work it has not done yet. With a median run of 0.66 h and a p90
   of 2.55 h against report periods measured in days, that bias is small but it is always in the
   flattering direction, which is the kind of bias a target must not have.
2. **Ownership is decided once, at run start** (`D-0013`, `ACCEPTANCE.md` §3 conditions 3 and 4), so
   "Interlock-owned for its entire life" is not an extra filter — it is automatic, and it is stated
   only so that the report can assert it rather than assume it.
3. **The v1 baseline is a completed-run figure.** The measured baseline normalises 195 *completed*
   runs to roughly 1,576 dispatcher ticks per 100 runs. A comparison against a started-run cohort
   would not be against that number.

Runs excluded from the cohort are **not** silently dropped. Every run whose terminal transition is
absent or outside the period appears in an `excluded` bucket with a reason:
`in_flight_at_period_end`, `started_before_period` (its prompts are partly outside the window),
`v1_owned`, `terminal_status_unknown`.

`started_before_period` deserves its own note because it is the period-crossing case the review
asked about: a run that started before the window and completed inside it has prompts on both sides
of the boundary. It is excluded from the **rate** and reported in its own bucket, rather than being
included with a partial numerator. The alternative — attributing prompts to the window they occurred
in and the run to the window it completed in — makes numerator and denominator count different
things, which is how a rate silently stops meaning anything.

### 2.2 What one "AI prompt" is

> **One AI prompt = one Dispatcher AI *invocation*: one incident-triggered model turn boundary.**

Concretely: one row in the invocation ledger (§2.3), identified by `invocation_id`.

- **Transport retries of the same invocation count once.** A 429 followed by a successful retry is
  one prompt; counting it as two would make a flaky network look like an AI workload increase.
- **Tool-call round trips inside one invocation do not add prompts.** This is the trap that makes or
  breaks the comparison: the v1 baseline records **3,531 unique assistant/model responses** and
  **4,960 AI tool calls** as *separate* figures. The comparable numerator is the response count, not
  the tool-call count. A harness that counted tool calls would be comparing 4,960 against a number
  built from invocations and would report a reduction that does not exist.
- **AC-1 is the same measurement from the other side.** "Zero AI turns absent incidents" is the
  assertion that every invocation row has an `incident_id`; the harness reports any row without one
  as an AC-1 violation rather than folding it into the count.

Output tokens are the provider-reported output/completion tokens for the invocation. **Cache-read
tokens are not output tokens and are not input tokens**; `ACCEPTANCE.md` §5 says so explicitly
(1,399,565,488 in the baseline, "a bandwidth indicator … not new input tokens and not a billing
figure"). They are reported as their own series and never enter the AC-9 arithmetic.

### 2.3 The invocation ledger and the provider adapter

AC-9's token half is the one place `#67` calls provider-shaped: usage figures come from the
provider's own reporting. The seam is a single adapter that fills three columns; everything else in
the harness is provider-neutral.

```sql
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
    CHECK (output_tokens IS NULL OR max_output_tokens IS NULL
           OR output_tokens <= max_output_tokens),
    CHECK (attempt_count >= 1),
    CHECK (finished_at_ms IS NULL OR finished_at_ms >= started_at_ms)
);

CREATE INDEX ai_invocation_by_period ON ai_invocation(started_at_ms);
CREATE INDEX ai_invocation_by_run ON ai_invocation(run_id);
```

`usage_status` exists so that a missing usage record is a **fact with a name** rather than an
absence. That is the whole point of the next section.

### 2.4 Coverage, and why a missing figure is never zero

> **Coverage and the excluded-reason breakdown are required output. A reduction rate printed without
> them is not a valid report.**

Treating a missing `output_tokens` as `0` understates Interlock's token use and therefore
*overstates* the reduction — a bias that always flatters the target, in the criterion the target is
being judged by. The harness therefore reports four numbers where a naive one reports one, and
labels each with what kind of number it is:

| Figure | Definition | Status of the number |
|---|---|---|
| **Coverage** | `count(usage_status='reported') / count(*)` over the cohort's invocations, printed as a percentage with both counts | Fact |
| **Observed reduction** | Computed over covered invocations only, explicitly labelled "over N of M invocations" | Fact about the covered subset |
| **Bounded reduction** | Missing invocations imputed at each one's **`max_tokens` ceiling** — the per-invocation output cap the caller sent to the provider | **A genuine lower bound on the reduction** |
| **Sensitivity reduction** | Missing invocations imputed at the **p95 of the covered distribution** | **An assumption, not a bound** |

The distinction between the last two rows is load-bearing and was got wrong on the first pass, so it
is stated explicitly. **A percentile of the observed sample does not bound the unobserved values.**
A missing invocation may exceed the covered p95, and it is more likely to if telemetry loss
correlates with large responses — a truncated or aborted response is exactly the kind that both
loses its usage record and runs long. Calling a p95 imputation "conservative" and then judging AC-9
by it can pass a target that the real numbers fail.

What *is* a bound is the request's own `max_tokens`: the provider cannot return more output tokens
than the caller allowed, so imputing a missing invocation at its recorded ceiling cannot understate
it. That is the figure the acceptance judgement uses. It is loose — usually far above the real
value — and being loose in the safe direction is the property being bought. Where an invocation has
no recorded `max_output_tokens`, it is not imputed at all: it is reported as `unbounded_missing`,
and a report with a non-zero `unbounded_missing` count **cannot support an AC-9 acceptance claim**
and says so. That is why the ceiling is a column on `ai_invocation` written at request time rather
than something read back from a usage record that, by hypothesis, never arrived.

The p95 figure is still printed, as a sensitivity estimate, because the bounded figure alone is too
loose to be informative about the likely truth. It is labelled an assumption everywhere it appears,
and the imputation rule is recorded in the report header (§6) so a reader can recompute under a
different one.

If coverage is 100% all four figures coincide and the harness says so.

**The harness does not decide pass or fail.** It prints the cohort size alongside every rate, and it
prints AC-9's targets (≥95% prompts, ≥90% output tokens) as targets. Whether a given cohort size is
large enough to judge on is canary exit criteria, which is `Q-0005` and open; inventing a threshold
here would be answering `Q-0005` by inertia.

---

## 3. AC-10 — where the ground truth comes from

### 3.1 The problem

Interlock's own tables cannot contain a miss. A missed condition produces no incident row, so an
aggregate over `incident` counts what was detected and is structurally blind to what was not. The
same applies to latency: the incidents that exist are the fast ones by definition if the slow ones
were dropped. Any harness that reads only our rows measures its own recall as 100%.

Ground truth has to come from **outside the thing being measured**, and there are exactly two
sources available.

### 3.2 Source A — the labelled fixture suite

AC-2 already requires known lifecycle / wait / error / relay / terminal determinations to be captured
as fixtures and reproduced by deterministic tests. G6 extends that corpus with **labels**: each
fixture carries, alongside its observation trace, the expected outcome.

```
fixtures/<class>/<case>/
  trace.jsonl        -- the observations, each with an offset in ms from t0
  expected.json      -- the label
```

`expected.json`:

| Field | Meaning |
|---|---|
| `incident_class` | Which class should be raised, or `none` for a negative case |
| `onset_offset_ms` | When the condition crossed its tolerance, relative to `t0` |
| `budget_ms` | The `L` from the policy revision under test |
| `fact_state` | The `D-0005` state the detector should classify to |
| `must_not_recommend` | Recommendations that would be wrong for this case (e.g. `terminate` on an observation outage) |
| `provenance` | Where the case came from: an accident, a dogfood capture, or a constructed edge |

Then:

- **A miss** is a fixture with a non-`none` `incident_class` for which the detector produced no
  matching incident within `onset_offset_ms + budget_ms` of `t0`.
- **A false positive** is a fixture labelled `none` that produced an incident.
- **Detection latency** is `incident.created_at_ms - (t0 + onset_offset_ms)`, on a synthetic clock,
  so the number is exact rather than sampled.
- **A false termination** is a fixture where a recommendation in `must_not_recommend` was produced
  *and applied* — see §3.4.

**Negative cases are mandatory, not optional.** `D-0006` requires observation-failure fixtures
alongside stall fixtures, and a corpus of only positive cases would let a detector that alarms on
everything score a perfect miss rate. The suite's composition is reported (§6) so that a
recall improvement bought by widening every predicate shows up as a false-positive regression in the
same table.

The fixture suite is the ground truth that exists **before** the canary, which matters because AC-10
is a gate on the canary and the shadow source is only available during it.

### 3.3 Source B — shadow reconciliation

During the shadow period both systems observe the same world. The comparison is not row-to-row —
the two systems have different schemas and different vocabularies — but **episode to episode**,
where an episode is one real-world condition as seen by one system.

**Correlation keys**, per subject class, chosen so both systems can compute them from what they
already store:

| Subject class | Correlation key | Interlock source | v1 source |
|---|---|---|---|
| CI outcome | `(provider, owner/repo lowercased, pr_number, head_sha)` | `ci_observation` joined to `repository` | `events` rows joined to `runs.pr_url`, normalised |
| PR merge | `(provider, owner/repo lowercased, pr_number)` | `pull_request` | `events` (`pr_merged`) |
| Worker escalation / relay | `(run_id, nth escalation of that run by ordered receipt time)` | `gate` ordered by `created_at_ms` | `.state/pending_decisions.json` entries ordered by `received_at` |
| Session liveness | `(run_id, onset bucket of 60 s)` | `incident` joined to `session` | v1 notification records |

The escalation key needs a word of explanation, because it is the one that is not a natural key on
either side: v1's register has its own entry id that Interlock never sees, so the join is positional
within a run. That is sound as long as both systems saw the same escalations in the same order,
which is exactly what a divergence would violate — so an ordering mismatch shows up as unmatched
episodes rather than as a silently wrong pairing. This is a known weakness of the key and is
recorded as such in the report rather than smoothed over.

**Unmatched buckets are first-class output.** v1's own reporter established the policy — its CI↔run
join is "a 3-stage fallback (never a silent drop)" ending in "an explicit `unmatched` bucket" — and
it is carried:

| Bucket | Meaning |
|---|---|
| `both` | Matched; latency and outcome are compared |
| `interlock_only` | Interlock raised an episode v1 did not — a candidate *improvement*, or a false positive |
| `v1_only` | v1 raised an episode Interlock did not — a candidate **miss**, and the number AC-10 turns on |
| `unmatched_key` | An episode whose correlation key could not be computed on one side |
| `censored` | The episode's observation window extends beyond the report period (§3.5) |

`v1_only` is a *candidate* miss, not a miss: v1 raising something Interlock did not can also mean v1
false-positived. Each `v1_only` episode is therefore classified — by the ground-truth labels where a
fixture covers the same shape, and otherwise listed for human adjudication with its evidence
attached. The report never silently converts `v1_only` into a miss count, and it never silently
discards it either.

### 3.4 False termination, counted at the applied effect

The review's point is decisive: `D-0004` and AC-6 mean the Dispatcher AI cannot terminate anything.
Counting AI recommendations, or watcher candidates, would compare Interlock's recommendations
against v1's *executions* — and counting Interlock's executions of a capability it does not have
would report a structural zero as a triumph.

> **A false termination is an `action` row with `kind='terminate_session'` and `status='applied'`
> whose subject was not, in fact, stuck.**

"Not in fact stuck" is decided by the ground truth, in that order of preference: the fixture label,
then the subject's own subsequent evidence (a session that resumed productive activity after the
termination window was not stuck), then human adjudication. Where none of the three settles it, the
episode is `undetermined` and appears in its own bucket — `D-0006`'s "cannot determine is a
legitimate outcome" applied to the measurement instead of to the detection.

Three supporting series are reported alongside, because the headline number alone would hide where
the precision actually lives:

- `recommended_terminate` — AI recommendations, whether or not applied.
- `recommended_but_not_applied` — the recommendations a human or the Secretary declined. This is the
  visible value of the human gate, and a rising number is informative rather than alarming.
- `applied_terminate` — the denominator for the false-termination rate.

### 3.5 Observation windows and right-censoring

Every episode gets a window `[onset, onset + L_class + grace)`, half-open, with `grace` a single
declared value per report (default: one reconcile period, so an episode is not judged a miss for
losing a race with the pass that would have caught it).

> **An episode whose window is not fully inside the report period is `censored`: excluded from the
> miss and latency numerators, counted in its own bucket, and reported.**

Without this rule every report boundary manufactures misses out of episodes that were detected
fifteen seconds after the period ended, and the manufactured rate rises as the period shortens.
Censoring is also why the report prints the censored count: a report where censored episodes are a
large fraction of the total is one whose period is too short for the budgets it is judging, and the
number is what makes that visible.

The mirror case — an episode whose onset precedes the period — is excluded the same way and counted
as `censored_left`.

---

## 4. Latency reporting

Per class, the harness reports the onset-to-incident distribution (count, median, p90, max) against
two references:

- **The budget `L`** from the policy revision in force. This is the acceptance bound.
- **The v1 shadow distribution** over `both`-bucket episodes. This is the non-regression bound.

Neither substitutes for the other, and a report states both even when one of them is unavailable —
outside the shadow period there is no v1 distribution, and the report says "no shadow reference for
this period" rather than printing the budget comparison alone under a heading that implies both.

The **ingestion lag** series (`ingested_at_ms - occurred_at_ms`, per
[`time-base-policy.md`](./time-base-policy.md) §2 rule 3) is printed beside it, so that a latency
regression caused by a slow provider is separable from one caused by us. Without it, GitHub having a
bad afternoon reads as a detection regression.

---

## 5. The AC-7 canary divergence report

AC-7 requires a shadow-period divergence report and rollback conditions to exist. The divergence
report is §3.3's episode reconciliation rendered per period, plus:

- **A writer audit** across both stores over the canary window, asserting no record was written by
  both (`ACCEPTANCE.md` §3 condition 2, verification bullet 1).
- **A run→owning-system ledger** at run start, asserting no run changed owner mid-flight
  (conditions 3, 4, 6).
- **The read-only assertion** for the shadow path, evidenced by the connection mode rather than by
  claim (condition 5).

What the report does **not** contain is a go/no-go verdict. `Q-0005` (canary duration, sample size,
numeric exit criteria) is open, `ACCEPTANCE.md` §3 says in terms that AC-9's targets "are not the
same thing as canary go/no-go thresholds, and this document does not convert one into the other",
and a harness that emitted a verdict would be converting them. It emits the measurements the verdict
will be made from.

---

## 6. Report provenance — what a report records about itself

A report that cannot be recomputed later is an opinion. Every report carries a header block, in both
the Markdown and JSON renderings, with:

| Field | Why |
|---|---|
| `period_start_ms`, `period_end_ms` | Half-open `[start, end)`. Printed as both epoch ms and ISO-8601 |
| `generated_at_ms`, `tool_version` | Which build produced it |
| `db_path`, `application_id`, `user_version` | Which database, and that it was a production one |
| `schema_migration_head` | Version *and* name of the newest applied migration |
| `db_fingerprint`, `fingerprint_mode` | A sha256 over the ordered rows of every table read, so two reports over "the same" database are provably over the same content. The weaker aggregate mode is available and is labelled as weaker |
| `policy_revision_id` | The tolerances and owners in force (`time-base-policy.md` §1). A report is meaningless without it, since every latency judgement is against those numbers |
| `detector_versions` | The **set** of `detector_version` values observed in the period, not a single value — a period spanning a detector change contains both, and collapsing them hides it (`Q-0009` governs the compatibility rule and is open; the report's obligation is to expose the set, not to resolve it) |
| `adapter_versions` | The set of `ai_invocation.adapter_version` values, same reasoning, for the AC-9 token seam |
| `query_definitions` | Every query the report ran, as text, plus a sha256 over the set. The queries are data, in the same spirit as the spike's `RECONSTRUCTION_QUERIES`, so a reader can run them by hand |
| `fixture_suite_ref` | Commit and case count of the labelled corpus, split positive/negative |
| `imputation_rule` | The AC-9 bounded- and sensitivity-figure rules in force, and the `unbounded_missing` count (§2.4) |
| `coverage` | AC-9 coverage and the excluded-reason breakdown |
| `censored`, `censored_left` | §3.5 |
| `unmatched_*` | §3.3 |

A `detector_versions` set with more than one member, or a `policy_revision_id` that changed inside
the period, makes the period **non-homogeneous**. The report says so at the top rather than
averaging across the change, because a latency comparison across a detector change is comparing two
detectors and calling it a trend.

`db_fingerprint` is a **content** hash: a sha256 over the ordered rows of each table the report
read. The cheaper thing — row counts plus `MAX(seq)`/`MAX(rowid)` — was considered and rejected,
because it does not do the job the field exists for. Most of the state a report reads is *updated in
place*: a verdict projection, an `outbox` status, a `gate` outcome, a `usage_status` backfilled by a
late adapter. Every one of those changes the answer and none of them changes a count or a maximum,
so an aggregate fingerprint would certify two materially different reads as identical — the exact
claim the provenance header is making.

The cost is linear in the rows read, which the measured baseline puts in the low thousands per
week-long period, so it is affordable on every report. The aggregate form remains available as
`--fingerprint=aggregate` for an interactive spot-check, and a report generated that way is stamped
`fingerprint_mode: aggregate` and states in the header that its fingerprint **does not** establish
identity of content.

---

## 7. Known holes, stated rather than filled

- **`Q-0005` stays open.** No exit criterion, sample-size minimum, or go/no-go threshold appears
  anywhere above.
- **`Q-0009` stays open.** The report exposes the set of detector versions and flags a
  non-homogeneous period; it does not decide what cross-version compatibility means.
- **`Q-0011` stays open.** Secretary window latency under load is gate item 8's measurement, not
  this harness's, and no threshold is invented here.
- **The escalation correlation key is positional** (§3.3) and is the weakest join in the
  reconciliation. It is documented as such and its failures surface as unmatched episodes rather
  than as wrong pairings, which is the safe direction, but a canary that produces many
  `unmatched_key` escalation episodes is telling us the key needs replacing before the numbers mean
  anything.
- **`ai_invocation` is new state** and is named in `D-0029`'s entity list extension; it is written
  by the component that invokes the Dispatcher AI, which is a single writer by construction (the AI
  is on-demand and incident-triggered — `D-0003`).
