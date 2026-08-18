# Curator promotion gate — gate item 9

**Status:** discharges gate item 9 in full (D-0022), independently of the Agent View verdict.
**Refs:** Issue #22; `DECISIONS.md` D-0018 (Curator output never reaches a skill without human
approval), D-0022 (item 9 is discharged in full, in parallel), D-0026 (the tests are durable, the
implementation is throwaway); `investigation/u8-skill-hot-reload-probe.md` (U8).

---

## 1. Where the gate sits, and why there

Item 9 makes the gate's *layer* a precondition of its own negatives: "the five negatives below can
all pass against a gate placed in the wrong layer". U8 decides the layer.

U8 is answered **affirmative**: a running Claude Code session re-reads skill material from disk
(documentation search plus a direct runtime probe on CLI 2.1.234 — transcript in
`investigation/u8-skill-hot-reload-probe.md`). An edited body, an edited description, and a skill
directory created after session start were all live inside one running session with no restart. The
probe additionally found that a mid-session directory is **loadable before it is listed**, so even
"has the session noticed it yet?" is not a boundary anything can be built on.

So: **writing the file is the promotion.** The gate is therefore the write itself
(`curator/gate.py`), not a `promote()` policy step that some other code is trusted to call first.
Concretely:

```
CuratorStub ──writes──> candidate store          (never skill material)
                            │
                     human approval               (a ledger append; writes nothing)
                            │
                    PromotionGate.promote  ──the only write into .claude/skills/──>  live skill material
```

`PromotionGate` is the only module that performs a filesystem write targeting skill material, and
the only module allowed to *name* a skill root at all (`curator/skill_root.py`). Both properties are
machine-checked; see §4.

## 2. The approval record

An approval names an immutable candidate version by content digest:

| field | why it is in the record |
|---|---|
| `approval_id` | what the ledger is keyed by; an approval that is not in the ledger does not exist |
| `candidate_id` | which candidate was approved — checked so a valid approval cannot be replayed at another one |
| `content_digest` | **which bytes** were approved (`sha256` over every relative path and every byte of the candidate tree) |
| `target` | where under the skill root those bytes were approved to land |
| `approver`, `approved_at`, `note` | who and when |

`record_digest` covers all of the above, so a recorded approval cannot be edited in flight into an
approval of something else — the gate compares the presented record against the recorded one.

The candidate is re-digested **from disk at write time**, never trusted from whatever produced it.
That is what turns "the candidate was mutated after approval" from an undetectable substitution into
a refusal.

## 3. The five negatives

Each is refused **and recorded** — the ledger append happens inside the gate, before the decision is
returned. Tests: `tests/curator/test_promotion_gate.py`.

| Item 9 negative | Refusal reason | Test |
|---|---|---|
| approval record absent | `approval-absent` | `test_absent_approval_is_refused_and_recorded` |
| approval forged but unrecorded | `approval-unrecorded` | `test_forged_unrecorded_approval_is_refused_and_recorded` |
| — same negative, recorded-then-edited | `approval-tampered` | `test_recorded_approval_edited_in_flight_is_refused` |
| approval revoked | `approval-revoked` | `test_revoked_approval_is_refused_and_recorded` |
| candidate mutated after approval | `digest-mismatch` | `test_candidate_mutated_after_approval_is_refused_and_recorded` |
| valid approval replayed at another candidate | `candidate-mismatch` | `test_valid_approval_replayed_at_another_candidate_is_refused` |
| — same negative, replayed at another target | `target-mismatch` | `test_valid_approval_replayed_at_another_target_is_refused` |

Because U8 puts the gate at the write, every one of those tests asserts *two* things: that the
decision was a refusal, and that **nothing landed in the live skill directory**. A gate that returned
"refused" while leaving bytes on disk would have promoted the candidate whatever it called itself,
and that is exactly the wrong-layer failure item 9 warns about.

Three further properties are asserted because they are where a digest-only or an
approval-existence-only design quietly fails:

- a replay is caught **even when the two candidates are byte-identical** (the digest alone would wave
  it through);
- a mutation that only **adds** a file is refused, though every approved byte is still intact;
- reverting the mutation restores the approval's validity — the digest names bytes, not history.

## 4. Path audit and the negative build test

Item 9 asks for a path audit *and* for a build failure if a bypass is added later. An audit that
only inspects today's tree gives the first and not the second, so the audit is a predicate over the
source tree (`claude_org_runtime.curator.audit`, also runnable as
`python -m claude_org_runtime.curator.audit`) and it is run from
`tests/curator/test_path_audit.py`. Adding a bypass turns that test red.

Five rules:

| rule | what it stops |
|---|---|
| `skill-root-reference-outside-gate` | a second module able to address live skill directories |
| `hardcoded-skill-path` | dodging the above by spelling `.claude/skills` by hand |
| `skill-path-write` | handing a skill-material path to a write call — **no allowlist exempts this** |
| `unallowlisted-writer` | a new writer inside the curator package that nobody argued for |
| `gate-write-outside-publisher` | a write added to the gate module outside its one publishing method |

The last rule exists because the gate is the most privileged module in the package: a bypass added
*there* would be invisible to the other four, which all stop at the module boundary. Writes inside
`gate.py` are confined to `_write`.

Two allowlists, each carrying its reason in the source: `WRITER_ALLOWLIST` (modules in the curator
package allowed to write, and to *what* store) and `SKILL_PATH_READERS` (modules outside it allowed
to *name* skill material — today only `dispatcher/runner.py`, which reads the org-delegate
instruction template). Naming is allowlistable; writing is not. A stale allowlist entry is itself a
finding, so renaming the gate module cannot quietly turn the audit into a no-op.

**The audit is proved non-vacuous.** A detector that can never fire keeps the build green forever,
which is the failure mode this criterion exists to prevent. So the test file also builds synthetic
packages containing one specific bypass each — a second importer of the skill root, a hardcoded path,
a write through a path constant, a write inside an allowlisted *reader*, a new unallowlisted writer,
`open()` in write mode, `open()` with a mode the audit cannot read statically (treated as a write:
fail closed) — and asserts the audit reports each one. A clean synthetic package is asserted to
produce no findings, so the controls are detecting the bypass and not the scaffolding.

## 5. Lifetime (D-0026)

The tests and this document are the durable output. `curator/gate.py`, `curator/ledger.py`,
`curator/stub.py`, `curator/records.py`, `curator/digest.py`, `curator/skill_root.py` and
`curator/audit.py` are **spike implementations, throwaway by default**; promoting any of them into
the real implementation needs a new `D-` entry that says so. The ledger format in particular is not
a schema commitment — Q-0001 stays open.

## 6. Known limits

- The gate guards **one** skill root per instance. Deployments with several live roots need one gate
  per root; nothing here enforces that the set of gates covers every root a session watches.
- Promotion and revocation share a lock (`ApprovalLedger.transaction`) so a revocation cannot land
  between the gate's check and its write. Cross-process exclusion uses `flock`; on a platform
  without `fcntl` only threads are serialized, which is a limit of this spike.
- The gate protects against a *bypassing code path*, not against an operator with a shell. Anything
  that can write to the directory out-of-process — another tool, a plugin installer, `cp` — is
  outside its reach; that is a filesystem-permissions question, not a promotion-path question.
- The audit is static and syntactic. It catches the shapes a bypass is actually written in; it does
  not catch a path assembled at runtime from data (`getattr`, a path read from config). The
  `skill-path-write` rule follows module-level constants, not arbitrary dataflow.
- The candidate store is immutable only by convention: the Curator stub does not enforce
  write-once. The digest is what makes that safe — a mutated candidate is refused rather than
  silently promoted — but a store that rejected rewrites would fail earlier and louder. What the
  stub *does* enforce is confinement: a candidate id or file name that would escape the store is
  refused, because such a write could land in skill material directly and never pass the gate.
- Publication is a staged tree plus a single directory rename, so a session never sees a
  half-promoted mixture and a file the new candidate dropped does not stay live. Staging happens
  **outside** the watched root (beside it by default, overridable with `staging_root` for a root
  that is a mount point) — staging inside it would put a readable copy of the candidate into live
  skill material under a name the approval does not cover. The rename is atomic; the retire-then-swap
  pair around it is not, so a crash in that window can leave the target missing (recoverable by
  re-running the promotion) — it cannot leave an unapproved tree in place.
- Symlinks in a target's chain are **refused, not resolved**. `skills/demo -> skills/code-review`
  stays inside the root, so a containment check alone would accept it and an approval naming `demo`
  would overwrite `code-review`.
