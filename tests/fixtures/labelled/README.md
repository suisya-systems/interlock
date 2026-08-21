# The labelled corpus -- AC-10's ground truth (source A)

Layout, contents and grading rules: `docs/measurement-harness.md` section 3.2, `D-0039`, and the
module that loads this tree, `src/claude_org_runtime/measurement/fixtures.py`.

Two things about this directory are load-bearing and easy to undo by accident:

1. **`onset_offset_ms` is when the condition began** -- the state entry, not the tolerance
   crossing. `T` is part of `L`, not a head start on it (`time-base-policy.md` section 3.1), so
   labelling the crossing would hand every case here an extra `T` of slack and pass an alarm that
   landed at `T + L`.
2. **Negative cases are mandatory.** `D-0006` requires observation-failure fixtures beside stall
   fixtures, and a positive-only corpus lets a detector that alarms on everything score a perfect
   miss rate. `load_corpus` refuses a corpus without them; do not "fix" that refusal by deleting
   the check.

A negative case is not "the detector must emit nothing": AC-3 requires an observation outage to be
classified `OBSERVATION_UNAVAILABLE`, and a row saying exactly that is the required output. The
label's `fact_state` is what the detector is permitted to say; any other fact is the false positive.

This README and any other `README.md` in the tree are ignored by the loader. Every other stray file
is refused.
