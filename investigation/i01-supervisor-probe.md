# I-01 — the `claude -p` supervisor surface Interlock will own

**Status:** answered — the supervisor verbs work through documented flags, with four hazards named
**Date:** 2026-08-18
**Task:** `interlock-i01-supervisor-probe-20260818` (issue #6, phase 1a, scaffold S4)
**Scope:** propose-only. This note records experiments and *proposes* readings for gate items 1, 3
and 8. It decides nothing. No `D-` entry is created here and no design document is edited.
**Refs:** issue #6; `D-0010` (public CLI only, fail closed), `D-0025`, `D-0026` (throwaway),
`D-0027` (C2 is the spike's `SessionProvider`); `ACCEPTANCE.md` §1 items 1, 3, 8;
`investigation/u1-session-id-bg-experiment.md`, `investigation/pre-spawn-fence-search.md`.
**Harness:** `investigation/i01_supervisor_probe.py`, `investigation/i01_internals_free_negative.sh`.

---

## 1. Verdict

Under C2, Interlock's process supervision *is* the session lifecycle. Every supervisor verb the role
needs — spawn, structured-state read, signal-terminate, reap — works through documented flags on CLI
2.1.234, and the internals-free negative was **executed**, not asserted. That is the positive half.

The negative half is four hazards, none of which a supervisor can be written without knowing:

| # | Hazard | Evidence |
|---|--------|----------|
| **H1** | **The CLI does not reap its own children; whether they die is up to them.** An MCP server that exits when its stdin closes does go away with the CLI. One that does not survives `SIGTERM`, `SIGINT` **and** `SIGKILL` delivered to the CLI's pid. Only a kill of the **process group** reaps it either way, so the supervisor cannot make the outcome depend on a third party's politeness. | §3.5, §3.10 |
| **H2** | **`-p` children survive the supervisor's death and keep working.** After `SIGKILL` of the parent, both children were reparented to `init` (pid 1 equivalent, 837), ran to completion, wrote their full result, and exited 0. Nothing reclaimed them. There is no public CLI surface that enumerates or stops a running `-p` child. | §3.6 |
| **H3** | **Exit 0 is evidence of nothing, again — and now on the interrupt path.** A `SIGINT`-interrupted run exits **0** while its own JSON says `is_error: true`, `terminal_reason: "aborted_streaming"`. So does `--add-dir /nonexistent` (silently ignored), and so does `claude capabilities`, which is not a subcommand at all and is silently executed as a **prompt**. | §3.4, §3.1 |
| **H4** | **The `--session-id` claim is scoped to the cwd-derived project directory.** The same session id, refused in the directory that created it, is **admitted in a different cwd** — producing two transcripts carrying one session id. This admits a second writer with no race at all. | §3.8 |

Three questions the issue named are answered:

| # | Question | Result |
|---|----------|--------|
| **U34** | What bounds the admission window? | **Session persistence, not the first API response.** On this machine the transcript file appears at **2.92 s** and `system/init` at **2.83 s**, while the first assistant token arrives at **19.0 s**. The refusal boundary falls between the 2.5 s (admitted) and 3.0 s (refused) sweep rows — i.e. on top of persistence, an order of magnitude before the model answers. |
| **U36** | What is the refusal keyed to? | **The persisted transcript, within the project directory derived from `cwd`.** A session created with `--no-session-persistence` is re-claimable (exit 0); a persisted one is refused; the same persisted id is admitted from another cwd. No second file carrying the id was found anywhere under the config directory, so no separate lock file is implied. One half is left open: whether deleting the transcript releases the claim — see §3.8.4. |
| **U3** | Is there a public effective-configuration readback? | **Partly, and it needs a normalisation rule.** The `system/init` event on `--output-format stream-json` reports the effective `permissionMode`, the effective `tools` list, `mcp_servers` with status, `model`, `cwd`, `skills`, `plugins`, `agents` and a `capabilities` array. It reports **no hooks and no sandbox** key. Two runs of an *identical* configuration returned 107 and 128 tools, the entire difference being one MCP server that was `pending` in one and `connected` in the other — so a naive diff is unsound, and requiring every server to read `connected` first is the fix (§3.9). |

Gate-item-8's provider-side input is unambiguous and cheap: obtaining state from N children is
**not** a blocking operation in the supervisor. A full non-blocking sweep of 8 children costs about
**0.1 ms**; per-child cost is flat from N=1 to N=8 (§3.7).

---

## 2. Environment, and which side of the boundary each restriction was applied to

```
$ claude --version
2.1.234 (Claude Code)

$ which claude
/home/happy_ryo/.local/bin/claude
```

Same build as `u1-session-id-bg-experiment.md` and `pre-spawn-fence-search.md`, so all three notes
compose without a version caveat.

Working directory for every spawn was a scratch directory outside the repository, deliberately **not**
a git repository:

```
/tmp/claude-1000/-home-happy-ryo-work-org-workers-interlock--worktrees-interlock-i01-supervisor-probe-20260818/5f5c0e12-fd19-49af-a6e8-98790a6e3fd5/scratchpad/i01
```

**Sandbox statement, as issue #6 requires.** Two different restrictions are in play and they must not
be confused.

1. **This worker's own sandbox was lifted for every model-backed command in this note.** The fence
   search established (§2 of that note) that under the worker sandbox a `-p` run succeeds and returns
   a `session_id` while **no transcript is ever written**. Since §3.8 now shows the refusal is keyed
   to the persisted transcript, running any refusal probe under that sandbox would have manufactured
   a false negative. Every command below therefore ran with the sandbox disabled, deliberately.
2. **The internals-free negative applies its restriction to the harness process only** (§3.10). The
   CLI's per-user config directory is replaced by an empty `tmpfs` in the harness's mount namespace
   and every `/tmp/claude-http-*.sock` is replaced by an empty regular file; the **child** is spawned
   through a nested `bwrap` that binds the real directory and the real sockets back at their real
   paths. Denying the *child* its own state is the trap the fence search fell into, and is not what
   was done here.

One command was refused by this worker's own auto-mode classifier and was **not** worked around; it
is reported as blocked in §3.8.4.

**Capability probe (D-0010).** There is no `capabilities` subcommand. `claude capabilities` is
silently interpreted as a **prompt** and runs a model turn, exit 0 — recorded in §3.1 because it is
itself a fail-open. The documented probes are `--version` and `doctor`, both recorded verbatim, plus
the `capabilities` array inside the `system/init` stream event.

**Model quota spent.** 66 model-backed `-p` invocations (63 left a persisted transcript under the two
scratch project slugs; the remainder were non-persisted, refused pre-model, or killed mid-turn). Of
these, 43 returned a `total_cost_usd` field, summing to **USD 6.02**. Prompts were trivial throughout
(`"reply with ok"`, or a 40-item counting prompt where a live holder was needed). The per-run cost of
a *trivial* prompt ranged from **0.037 to 0.76 USD**, which is itself a C2 finding: the child loads
the invoking user's entire configuration (18k–38k cache-creation tokens per spawn), so spawn cost is
governed by inherited configuration, not by the prompt. See §5.4.

---

## 3. Transcript

Records are written verbatim, one JSON object per step, by
`investigation/i01_supervisor_probe.py`. Excerpts below are quoted from those records; long token
arrays are elided at `...` and nothing else is edited.

**Provenance, since the harness was corrected mid-investigation.** A review pass found four
measurement defects (§3.7, §3.10). §3.7's concurrency figures and §3.10's negative were **re-run**
against the corrected harness and the numbers quoted here are the re-run ones. §§3.1-3.6, 3.8 and
3.9 were recorded before the correction, and none of the four defects touches them: they concern the
scenario's signal target, its post-reap descendant scan, the observer's claim path (disabled in the
run that produced §3.8.2), and the concurrency reader. The corrected harness was re-verified against
a stub binary so that every subcommand still records what it recorded then.

### 3.1 E1 — capability and version probe

```
$ claude --version
2.1.234 (Claude Code)
--- EXIT CODE: 0 ---

$ claude doctor
Claude Code doctor

Running: native (2.1.234)
Commit: 7215ba60b06d
Platform: linux-x64
Path: /home/happy_ryo/.local/share/claude/versions/2.1.234
Config install method: native
Search: OK (bundled)
Auto-updates: enabled
Auto-update channel: latest
Last update attempt: success -> 2.1.234 (2026-08-18)

Remote Control
Control this session from claude.ai/code or the Claude mobile app

No installation issues found.

For a full setup checkup that can also fix issues, run /doctor in a Claude Code session.
--- EXIT CODE: 0 ---
```

`claude capabilities` — what D-0010 asks a probe to try first — is **not a subcommand**:

```
$ claude capabilities
(a multi-paragraph model-written answer describing what the session can do)
--- EXIT CODE: 0 ---
```

The positional argument is the prompt, so an unrecognised subcommand becomes a **billed model turn
that exits 0**. A capability probe written as "run `claude <verb>` and check the exit code" would
report every verb as supported. The probe must be `--version` / `doctor` / the `init` event's
`capabilities` array, all of which are documented, and it must parse output rather than trust rc.

The `capabilities` array reported by `system/init` on this build:

```json
["interrupt_receipt_v1", "interrupt_cancel_queued_v1", "msg_lifecycle_v1"]
```

### 3.2 E2 — spawn: what a parent must supply

Eight spawn variants, all exit 0. What matters for reproducibility:

| Case | argv (after `claude`) | rc | `session_id` returned |
|------|----------------------|----|-----------------------|
| text | `-p "reply with ok"` | 0 | not reported (plain text `ok`) |
| json | `-p "reply with ok" --output-format json` | 0 | yes |
| stream-json | `-p ... --output-format stream-json --verbose` | 0 | yes, in every event |
| settings file | `... --settings <path>` | 0 | yes |
| settings inline | `... --settings '{"permissions":{"deny":["Bash"]}}'` | 0 | yes |
| permission mode | `... --permission-mode plan` | 0 | yes |
| chosen identity | `... --session-id <uuid>` | 0 | the requested uuid |
| minimal env | `... --output-format json`, env = `PATH` + `HOME` only | 0 | yes |

Four things a parent must supply, established by this matrix:

1. **`--output-format json` or `stream-json` is mandatory for a machine-readable state.** The default
   `text` format returns the model's answer with no identity, no status and no cost. `--verbose` is
   additionally required for `stream-json` to emit the pre-result events.
2. **`cwd` is not merely cosmetic — it selects the session's project namespace,** and therefore the
   scope of the identity claim (§3.8) and of the transcript location.
3. **A minimal environment of `PATH` and `HOME` suffices** for a spawn to succeed. Nothing else in
   the parent's environment is required, which is what a supervisor wants.
4. **`--settings` accepts a path or an inline JSON string,** but see §3.4: a *malformed* inline
   string is reported as a missing **file**, so the diagnostic misdirects.

The child inherits the invoking user's configuration by default. In the `stream-json` case the very
first two events are a `SessionStart` hook firing from the user's own settings:

```
{"type":"system","subtype":"hook_started","hook_id":"36f27132-...","hook_name":"SessionStart:startup","hook_event":"SessionStart",...}
{"type":"system","subtype":"hook_response","hook_id":"36f27132-...","hook_name":"SessionStart:startup","output":"","stdout":"","stderr":"","exit_cod...
```

A reproducible spawn therefore has to pin the setting sources explicitly (`--setting-sources`,
`--safe-mode`, `--bare`) rather than rely on the argv alone. Not doing so is what makes a trivial
spawn cost up to 0.76 USD (§5.4).

### 3.3 E3 — streams

**Framing.** `stream-json` is line-delimited JSON, one complete object per line, and every line
parsed on the first attempt. Arrival is incremental, not batched at exit:

```
t=0.956  system/hook_started
t=0.956  system/hook_response
t=0.981  system/init
t=3.203  assistant
t=3.209  rate_limit_event
t=3.212  result/success
n_lines=6   all_line_delimited_json=true   stderr=""
```

So a supervisor can read state as it arrives; it does not have to wait for exit. Note the
`rate_limit_event` type, which reports quota status mid-stream:

```json
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1787040600,"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"org_level_disabled","isUsingOvera...
```

**Flush on abnormal termination.** Two cases, and they differ:

```
SIGKILL  rc=-9    lines received before the signal: 6 (last line complete)
                  bytes received after the signal: 0
SIGTERM  rc=143   lines received before the signal: 3 (last line complete)
                  bytes received after the signal: 1145, ending in a complete line
```

On `SIGTERM` the CLI flushes a graceful tail — an explicit interrupt marker followed by a full result
object:

```json
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"[Request interrupted by user]"}]},"parent_tool_use_id":null,"session_id":"6a915e20-f817-4893-b3ed-f80223079928","uuid":"303ad103-...","timestamp":"2026-08-18T06:11:50.319Z"}
{"is_error":true,"duration_api_ms":0,"num_turns":2,"stop_reason":null,"session_id":"6a915e20-...","total_cost_usd":0,...}
```

On `SIGKILL` nothing follows. In **both** cases every line received was a **complete** line: no
truncated JSON object was ever observed, so a supervisor may treat a partial trailing line as
"nothing arrived" rather than as corruption. That is a framing property worth relying on, and it
should be re-checked on version bump.

**`stderr`-only content.** The refusal reproduces U1's E5 exactly, and it is the case that matters:

```
$ claude -p "reply with ok" --output-format json --session-id 1cc2b601-3074-4008-bb3b-64fd0dde19fc
--- stdout ---
(empty)
--- stderr ---
Error: Session ID 1cc2b601-3074-4008-bb3b-64fd0dde19fc is already in use.
--- EXIT CODE: 1 ---
```

A supervisor that reads only `stdout` sees an empty string in the one case it most needs to see. Both
streams must be captured separately, and `stderr` must be retained on non-zero rc.

### 3.4 E4 — the exit-code table

Every cause the issue names, plus the ones encountered along the way. `subtype`, `is_error` and
`terminal_reason` are read from the JSON body where one was produced.

| Cause | rc | stdout | `is_error` | `terminal_reason` | `subtype` |
|-------|----|--------|-----------|-------------------|-----------|
| success | **0** | JSON result | `false` | `completed` | `success` |
| unknown flag (`--no-such-flag-i01`) | **1** | empty | — | — | — |
| invalid value for a known flag (`--permission-mode nonsense`) | **1** | empty | — | — | — |
| settings file missing | **1** | empty | — | — | — |
| settings inline malformed | **1** | empty | — | — | — |
| invalid `--session-id` (not a UUID) | **1** | empty | — | — | — |
| flag-combination refusal (`--include-partial-messages` without stream-json) | **1** | empty | — | — | — |
| session id **already in use** | **1** | empty | — | — | — |
| model/API failure (`--model claude-does-not-exist-i01`) | **1** | JSON result | `true` | `api_error` | **`success`** |
| budget exhausted (`--max-budget-usd 0.000001`) | **1** | JSON result | `true` | `budget_exhausted` | `error_max_budget_usd` |
| `--add-dir /nonexistent/...` | **0** | JSON result | `false` | `completed` | `success` |
| interrupted by **SIGINT** | **0** | JSON result | `true` | `aborted_streaming` | `error_during_execution` |
| interrupted by **SIGTERM** | **143** | JSON result | `true` | `aborted_streaming` | `error_during_execution` |
| interrupted by **SIGKILL** | **-9** (killed by signal) | empty | — | — | — |

Verbatim, the three that matter most:

```
$ claude -p "reply with ok" --output-format json --model claude-does-not-exist-i01
--- stdout ---
{"is_error":true,...,"stop_reason":"stop_sequence","session_id":"2e1444f1-...","total_cost_usd":0,
 ...,"subtype":"success","terminal_reason":"api_error",
 "result":"There's an issue with the selected model (claude-does-not-ex..."}
--- stderr ---
[claude-code:unrecognized_model] {"model":"claude-does-not-exist-i01","query_source":"sdk"}
--- EXIT CODE: 1 ---
```

```
# SIGINT delivered to a mid-turn child
--- stdout ---
{"is_error":true,"duration_api_ms":0,"num_turns":2,"stop_reason":null,
 "session_id":"cd6c4549-1cbb-4a8a-b503-aa94808c23c9","total_cost_usd":0,...,
 "subtype":"error_during_execution","terminal_reason":"aborted_streaming"}
--- stderr ---
(empty)
--- EXIT CODE: 0 ---
```

```
$ claude -p "reply with ok" --output-format json --settings '{not json'
--- stderr ---
Error: Settings file not found: {not json
--- EXIT CODE: 1 ---
```

**Which causes are distinguishable by exit code alone.** Almost none.

- **rc 1 is a bucket**, not a diagnosis. It covers argument errors, a missing settings file, an
  invalid UUID, the identity refusal, an unusable model and an exhausted budget. Four of those
  produce **empty stdout** and are distinguishable only by parsing `stderr` prose; two produce a JSON
  body. Nothing in the exit code separates "you passed a bad flag" from "another writer holds this
  session".
- **rc 0 does not mean the work happened.** A `SIGINT`-interrupted run exits 0 with `is_error: true`.
  `--add-dir` pointing at a nonexistent directory is ignored silently, exit 0. `claude capabilities`
  runs a model turn against a prompt the caller never intended, exit 0. This is the third independent
  observation of the same shape, after U1 §5.2 and the fence search's A1/A4, and it is now on the
  **interrupt** path — the exact path a supervisor exercises when it cancels work.
- **rc 143 and rc -9 are the supervisor's own signals coming back**, and they are the only rc values
  that carry real information, because the supervisor sent the signal.
- `subtype` is **not** a reliable discriminator: the model-failure case reports
  `subtype: "success"` alongside `is_error: true`. The machine-readable pair that did hold across
  every case is **`is_error` plus `terminal_reason`**.

**Proposed supervisor rule.** Treat the exit code as a *hint* and the parsed JSON body as the
*verdict*; on rc 0 with no parseable body, or a body with `is_error: true`, record the run as
**failed**, not succeeded. This is the C2 instance of D-0027's general rule that exit 0 must be read
back rather than trusted.

### 3.5 E5 — signals and process topology (H1)

Each child was started with `--mcp-config` naming a trivial stdio MCP stub, so the CLI would have a
child process of its own to leave behind, and was placed in its own process group
(`start_new_session=True`).

| Signal | Delivered to | rc | Child dead? | CLI's own child (MCP stub) alive 2 s later? |
|--------|--------------|----|-------------|---------------------------------------------|
| SIGTERM | child pid | 143 | yes | **yes** |
| SIGINT | child pid | 0 | yes | **yes** |
| SIGKILL | child pid | -9 | yes | **yes** |
| SIGKILL | **process group** | -9 | yes | **no** |

Verbatim for the last two rows:

```
signal=SIGKILL delivered_to="child pid"
  descendants_before: [{"pid":1963416,"ppid":1963336,"cmdline":"/usr/bin/python3 .../i01-mcp-server.py"}]
  rc=-9  child_alive_after=false
  survivors_after_2s: [{"pid":1963416, ...}]        <-- orphaned

signal=SIGKILL delivered_to="process group"
  descendants_before: [{"pid":1963539,"ppid":1963459,"cmdline":"/usr/bin/python3 .../i01-mcp-server.py"}]
  rc=-9  child_alive_after=false
  survivors_after_2s: []                            <-- reaped
```

The CLI spawned exactly one descendant here (the MCP stub, because `--strict-mcp-config` excluded
the user's own servers); no other subprocesses appeared. That descendant survives **every** signal
aimed at the CLI's pid, including SIGKILL.

**One qualification, established by §3.10 and stated here so H1 is not overread.** The stub used in
this experiment is *deliberately* badly behaved: on stdin EOF it sleeps rather than exiting. In
§3.10, where the user's real MCP servers were inherited (`renga mcp-peer`, a `bun` server), those
children were gone two seconds after the CLI was SIGTERMed. So the precise finding is not "MCP
servers always survive" but **"the CLI does not reap them; a server that exits on stdin EOF goes
away, and one that does not, stays"**. The CLI is not the thing making the difference.

**A supervisor must therefore own the process group, not the pid.** Spawn with
`start_new_session=True` (or `setsid`) and terminate with `killpg`. A pid-only stop leaves the
child's children running whenever those children do not choose otherwise, and their politeness is
not a property the supervisor controls or can test in advance.

### 3.6 E6 — the supervisor-kill case (H2)

The C2 analogue of U4. A small parent process spawned two `claude -p` children with a long prompt,
was left running 8 s, then `SIGKILL`ed. Run twice: with the children in their own sessions
(`start_new_session=True`) and in the parent's (`False`). **The results are identical.**

```
child_new_session=true   parent=1973160  children=[1973161, 1973162]
  before kill            : both alive, ppid=1973160
  3 s after parent SIGKILL: both alive, ppid=837          <-- reparented to init
  48 s after             : both gone (they finished on their own)
  output sizes before kill: {orphan-0.out: 0, orphan-1.out: 0}
  output sizes after      : {orphan-0.out: 4087, orphan-1.out: 3327}
  kept_writing: true
  orphan-0.out: {"is_error":false,"duration_api_ms":20374,"num_turns":1,"stop_reason":"end_turn",
                 "session_id":"f7ec4939-736a-4b2e-92a4-44e3704188c1","total_cost_usd":0.08931,...}

child_new_session=false  parent=1979703  children=[1979704, 1979705]
  3 s after parent SIGKILL: both alive, ppid=837
  kept_writing: true
  orphan-0.out: {"is_error":false,...,"session_id":"e3f7b1eb-03b7-4deb-bc6e-e9d3064dc086",...}
```

**The answer is unambiguous: `-p` children survive the supervisor's death, keep running, keep
writing, complete their turn successfully, and are reparented to init.** Nothing reclaims them. They
exit only when their own work finishes.

Two consequences a design must absorb:

1. **The hazard is not a leaked process; it is a leaked *writer*.** Each orphan completed a full turn
   and appended to its transcript after Interlock was gone. Under C2 that is precisely U4's ungraceful
   shape: work continues with nobody to record it, and the restarted supervisor has no row saying it
   happened.
2. **There is no public reclaim path.** `claude agents` lists *background* sessions; a `-p` child
   holds no roster row (re-confirmed in §4). So a restarted supervisor cannot ask the CLI "what of
   mine is still running". Whatever reclaim exists must be Interlock's own — a pid/pgid recorded
   durably before the spawn, plus liveness checks after restart — which is the same durable-first
   discipline `ACCEPTANCE.md` §2 already requires for the fencing token.

### 3.7 E7 — concurrency and state-readout latency (gate item 8)

N concurrent children, trivial prompt, `stream-json`. Two readouts were measured per sweep:
**liveness** (`poll()` plus a `/proc` existence check) and **structured state** (drain whatever
complete stream-json lines are buffered, without blocking on any child).

| Workers | Sweeps | Liveness per child (median / p95 / max, ms) | Structured per child (median / p95 / max, ms) | Whole-sweep total (ms) |
|---------|--------|--------------------------------------------|-----------------------------------------------|------------------------|
| 1 | 39 | 0.0250 / 0.0331 / 0.0372 | 0.0231 / 0.0348 / 0.0484 | 0.025 + 0.024 |
| 4 | 68 | 0.0060 / 0.0265 / 0.0815 | 0.0029 / 0.0246 / 0.0429 | 0.043 + 0.033 |
| 8 | 78 | 0.0050 / 0.0272 / 0.0691 | 0.0021 / 0.0222 / 0.0587 | 0.066 + 0.040 |

All children exited 0 in every configuration, each delivering the same 6 stream-json lines; spawn of
all 8 took 0.0 s of parent time (fork/exec only).

**Reading.** Per-child readout cost is **flat** from 1 to 8 workers — it does not degrade with
concurrency — and a complete sweep of 8 children costs about **0.1 ms**. Obtaining state from N
children is therefore **not** a blocking operation in the supervisor, provided the readout is
non-blocking. Under C1 this question was about a daemon serialising status queries (U6); under C2 the
answer is that the supervisor is free unless it makes itself unfree.

**How it makes itself unfree, stated as the real risk.** The control that was supposed to
demonstrate this — a naive per-child `readline()` — measured nothing: every child was already
finished when the control ran, so `readline()` hit EOF at once. The harness now records that fact
alongside each sample (`{"child_was_live": false, "ms": 0.017}`, and so on for all eight), so the
control is **explicitly inconclusive** rather than quietly reassuring. The magnitude can still be
bounded from measured data elsewhere in this note: on a live child the first stream line arrives at
~1.0-1.2 s (§3.3) and the first *assistant* event at ~19 s (§3.8.2). A supervisor that loops over
children with blocking reads therefore serialises on the slowest child's next event — seconds per
child, not microseconds — and at N=8 that is the difference between a 0.1 ms sweep and a
multi-second one. The structural claim item 8 needs ("intake is not behind worker monitoring") is
satisfied by the non-blocking readout and falsified by a blocking one; it is a property of
Interlock's code, not of the provider. #21 should re-measure the control against a live child.

**A methodology note that belongs with the numbers.** The first version of this readout used
`select()` on the child's buffered text stream and then `readline()`. That is unsound: one
`readline()` can pull several lines into user-space buffer, after which the fd is no longer readable
while complete lines still sit unread — and it showed, as children reporting 4 or 5 of their 6
lines. The harness now reads the raw fd non-blockingly and frames lines itself; every child now
accounts for all 6. The corrected sweep costs above are marginally *higher* than the first run's,
which is the expected direction: the earlier reader was doing less work than it claimed.

These are one-machine, one-load figures under WSL2, recorded as such.

### 3.8 E8 — the admission window, re-measured, and what it is keyed to

#### 3.8.1 The sweep (U27 re-measured)

Same shape as the fence search §4.3: a long-running holder claims a UUID, a second claimant follows
after a stagger. Fresh UUID per row.

| Stagger | Second claimant | rc | Time to result | Returned `session_id` |
|---------|-----------------|----|----------------|------------------------|
| 0.5 s | **admitted** | 0 | 6.89 s | same as holder's |
| 1.0 s | **admitted** | 0 | 5.52 s | same as holder's |
| 1.5 s | **admitted** | 0 | 5.85 s | same as holder's |
| 2.0 s | **admitted** | 0 | 5.37 s | same as holder's |
| 2.5 s | **admitted** | 0 | 6.27 s | same as holder's |
| 3.0 s | refused | 1 | 0.35 s | — |
| 4.0 s | refused | 1 | 0.37 s | — |

The holder completed with exit 0 and kept its id in every row. Refusal, when it happens, is fast and
pre-model (0.35-0.37 s).

**The window on this machine is between 2.5 s and 3.0 s wide.** That is consistent with the fence
search's "roughly 2 to 3 s" and sharpens it by one row. It is recorded as a **one-machine, one-load
figure and not as a provider constant** — the two notes agree only because they ran on the same
machine.

#### 3.8.2 U34 — what bounds it

An observer process (explicitly **not** the supervisor harness; it reads internal paths on purpose)
watched one holder and recorded when each landmark occurred. **No second claimant was issued during
the observation** (`--claim-every` disabled): a synchronous claimant would have stopped the scan for
several seconds inside the very interval being measured, and an admitted one would have created a
file carrying the same uuid, so the landmark times could no longer have been attributed to the
holder. The refusal boundary is taken from the independent sweep in §3.8.1 instead. (The harness's
claim path is now asynchronous for the same reason.)

```
uuid = d196dd2e-3f6e-4a7a-bbca-402c3c0f6b4c
first stream-json line   : 1.189 s
system/init event        : 2.827 s
transcript file appears  : 2.917 s   (54699 bytes at first sight)
first assistant event    : 19.046 s
paths under the config dir carrying this uuid: exactly one (the transcript)
```

Set against the sweep: admission is still granted at 2.5 s and refused at 3.0 s, and the transcript
file appears at **2.92 s**, effectively simultaneously with `system/init`.

**U34 is answered: the window is bounded by session persistence — the transcript's creation — and
not by the first API response,** which arrives at 19 s, roughly seven times later. The practical
consequence for a retry-delay design is that the bound is an I/O-and-startup latency, so it will move
with disk, machine load and startup work (this build fired a `SessionStart` hook before `init`); it
is *not* pinned to model latency and must not be estimated from it.

Precision caveat: the transcript's first-seen time is bounded by the observer's poll loop, which
walks the config directory each iteration, so 2.917 s is an upper bound with a granularity of tens of
milliseconds, not an exact creation timestamp.

#### 3.8.3 U36 — what the refusal is keyed to, and the new hazard H4

Three probes:

```
(a) --no-session-persistence, then re-claim the same id normally
    first  : rc=0, session_id = 65f78cab-0321-4eea-99cd-a634e2270ec5
    reclaim: rc=0  ADMITTED

(b) control: a normally persisted session, then re-claim it in the same cwd
    first  : rc=0, session_id = 3a31bc2a-a36b-4d96-854c-26c3c8733f2b
    reclaim: rc=1  REFUSED
             Error: Session ID 3a31bc2a-a36b-4d96-854c-26c3c8733f2b is already in use.

(c) the same persisted id, re-claimed from a DIFFERENT cwd
    reclaim: rc=0  ADMITTED, stderr empty
```

And the state left on disk after (c):

```
$ find <config-dir>/projects -name "*3a31bc2a-a36b-4d96-854c-26c3c8733f2b*"
54268 bytes  .../projects/<...>-scratchpad-i01/3a31bc2a-a36b-4d96-854c-26c3c8733f2b.jsonl
56103 bytes  .../projects/<...>-scratchpad-i01-other-cwd/3a31bc2a-a36b-4d96-854c-26c3c8733f2b.jsonl
```

**Two transcripts, one session id, two project directories.**

So the refusal is keyed to **the persisted transcript, within the project directory derived from
`cwd`**. Not to a live process (it outlives one, per U28). Not to a global registry (a different cwd
is admitted). And no separate lock file is implied: across the whole config directory exactly one
path carried the session id (§3.8.2).

**H4 is a second admission route, and it needs no race at all.** The fence search showed two writers
could share an id inside a ~2-3 s window. This shows two writers can share an id at *any* stagger, as
long as they run in different directories — which is exactly what a worktree-per-worker arrangement
produces. A retry that re-spawns a run in a different worktree with the same session id is not
protected by the refusal in any degree.

This is a **provider fact only**. It does not re-open item 2, which D-0027 already failed on Agent
View and whose single-writer half `ACCEPTANCE.md` §2 already assigns to Interlock's own fencing
token. What it does is remove any temptation to treat the `-p` refusal as a *partial* substitute for
that token: it is not partial, it is orthogonal.

#### 3.8.4 What was not established, and why

The remaining U36 half — whether **deleting the transcript releases the claim** — was attempted and
**blocked by this worker's own auto-mode classifier** (the step moves a file under the CLI's config
directory). Per the task's standing instruction, the block was reported rather than worked around.
The question therefore stays open, and it matters: if removal releases the claim, on-disk state can
clear or spoof the claim, which bears on U29 and on anything that treats the id as durable. It is
proposed below as **U38**.

### 3.9 E9 — effective-configuration readback (U3, item 3)

Three runs, reading the `system/init` event out of `--output-format stream-json`.

Keys present in every `init` event:

```
agents, analytics_disabled, apiKeySource, capabilities, claude_code_version, cwd,
fast_mode_disabled_reason, fast_mode_state, mcp_servers, memory_paths, messaging_socket_path,
model, output_style, permissionMode, plugins, product_feedback_disabled, session_id, skills,
slash_commands, subtype, terminal_slash_commands, tools, type, uuid
```

The readback is **effective**, not an echo of argv — the flags change what is reported:

| Case | `permissionMode` reported | `tools` diff vs the control |
|------|---------------------------|------------------------------|
| identical-control-a | `auto` | — (107 tools) |
| identical-control-b | `auto` | — (128 tools) |
| `--settings <deny Bash,WebFetch> --permission-mode plan` | `plan` | removed `Bash`, `WebFetch`; added `Glob`, `Grep` |
| `--disallowed-tools Bash --permission-mode acceptEdits` | `acceptEdits` | removed `Bash`; added `Glob`, `Grep` |

`mcp_servers` reports each server with a `status`, and `capabilities`, `claude_code_version` and
`memory_paths` are reported too.

**So a public effective-configuration readback exists — for permission mode, the effective tool set,
and MCP servers. It does not exist for hooks or sandbox.** There is no `hooks` key, no `sandbox`
key, and no `permissions` key: the *rules* are not reported, only the tool list they produce. That
matters because item 3's predicate names "permission / sandbox / hook configuration" as one object.

**And the `tools` array is not stable across identical runs — but the instability is explained, and
that changes what item 3 should do about it.** The first version of this experiment compared three
runs with three *different* configurations and inferred instability from their differences; that
inference was unsound, because the flags could have caused every difference. Two runs of an
**identical** configuration were therefore added as a control:

```
identical-control-a : 107 tools   mcp_servers: [... "claude.ai Slack": pending   ...]
identical-control-b : 128 tools   mcp_servers: [... "claude.ai Slack": connected ...]

in a but not b: (nothing)
in b but not a: ListMcpResourcesTool, ReadMcpResourceDirTool, ReadMcpResourceTool,
                mcp__claude_ai_Slack__* (18 tools)      -- 21 tools in total
permissionMode a/b: auto / auto
```

**Two runs of one configuration do return different `tools` arrays**, and the whole difference is
the tool family of the one MCP server whose `status` differs. So `init` is emitted before every MCP
server has finished connecting, and `tools` is a snapshot of whatever was connected at that instant.

That is a better result than "unstable", because the same event carries the explanation: **the
`mcp_servers` statuses are the normalisation rule.** A comparison that requires every server to read
`connected` before it trusts the `tools` array — or that compares only the non-MCP tools — is sound;
a naive diff is not. It also corrects the earlier reading of `Glob` and `Grep`: they appear in both
flag cases and in neither control run, so they are caused by the flags, not by timing.

**Proposed reading for item 3.** The equality check in `ACCEPTANCE.md` §1 item 3 becomes *partly*
runnable as written — `permissionMode` can be diffed across a restart directly. It does **not**
become fully runnable, for two independent reasons: hooks and sandbox are absent from every public
surface examined, and the `tools` array is unstable across runs and so would produce false
mismatches without a normalisation rule that does not yet exist. **D-0023's weakening of item 3 is
therefore still needed**, narrowed: the breach-probe battery remains the observable for hooks and
sandbox, while permission mode gains a direct readback. This note proposes; the decision is a human's.

### 3.10 E10 — the internals-free negative, executed

The negative required by issue #6 was run as `investigation/i01_internals_free_negative.sh`. The
restriction is applied to **the harness process**, and the child is handed its state back:

```
### deny paths (applied to the harness only):
    /home/happy_ryo/.claude
    /tmp/claude-http-20399de59ae35ef6.sock
    /tmp/claude-http-49f634442d952827.sock
    /tmp/claude-http-67ececdf5ea6c18e.sock

### harness runs under:
    bwrap --dev-bind / / --bind <config-dir> <alias> --tmpfs <config-dir>
          --bind <sock> <alias-sock> --bind <empty-file> <sock>   (per socket)

### child argv is prefixed with:
    bwrap --dev-bind / / --bind <alias> <config-dir> --bind <alias-sock> <sock> ...
```

**A — control, harness unrestricted:**

```json
{"label":"unrestricted","harness_pid":2172396,"child_wrapper":[],
 "harness_view":{"/home/happy_ryo/.claude":{"kind":"dir","entries":34},
                 "/tmp/claude-http-...sock":{"errno":6,"strerror":"No such device or address"}, ...}}
{"label":"unrestricted","requested_session_id":"42b3f2f6-c8b3-42c5-aa53-546adbb993bd",
 "spawned_pid":2172397,"signalled_pid":2172397,"signalled_the_wrapper":false,
 "init_session_id":"42b3f2f6-c8b3-42c5-aa53-546adbb993bd","n_events_before_signal":4,
 "event_types":["system","system","system","assistant"],"first_event_s":0.952,
 "rc":143,"reaped":true,
 "watched_before_signal":[{"pid":2172476,"ppid":2172397,"cmdline":"renga mcp-peer"},
                          {"pid":2172483,"ppid":2172397,"cmdline":"bun .../claude-peers-mcp/server.ts"}],
 "survivors_2s_after_reap":[],"tail_len":2082}
```

**B — negative, harness denied and child not:**

```json
{"label":"restricted","harness_pid":2172540,
 "harness_view":{"/home/happy_ryo/.claude":{"kind":"dir","entries":0},
                 "/tmp/claude-http-...sock":{"kind":"file","first_bytes":0}, ...}}
{"label":"restricted","requested_session_id":"56d6de52-1a08-424d-a2d0-8d77456b362d",
 "spawned_pid":2172541,"signalled_pid":2172542,"signalled_the_wrapper":false,
 "init_session_id":"56d6de52-1a08-424d-a2d0-8d77456b362d","n_events_before_signal":4,
 "event_types":["system","system","system","assistant"],"first_event_s":1.103,
 "rc":143,"reaped":true,
 "watched_before_signal":[{"pid":2172542,"ppid":2172541,"cmdline":"claude -p Count from 1 to 40, ... --session-id 56d6de52-..."},
                          {"pid":2172624,"ppid":2172542,"cmdline":"renga mcp-peer"},
                          {"pid":2172632,"ppid":2172542,"cmdline":"bun .../claude-peers-mcp/server.ts"}],
 "survivors_2s_after_reap":[],"tail_len":2053}
```

The harness sees **0 entries** where it saw 34, and each socket is an empty regular file rather than
a socket. Spawn, structured read (the `init` event carries the requested id in both), the graceful
SIGTERM tail, **exit 143 in both**, reap, and zero survivors — identical on both sides of the
restriction.

Two things about *how* this was measured matter enough to state, because the first version of this
experiment got both wrong and a review caught them:

1. **The signal goes to the CLI, not to the wrapper.** Under B the spawned pid is `bwrap`
   (`2172541`); the CLI is its child (`2172542`). Signalling the spawned pid would have exercised
   `bwrap`'s termination behaviour and not the CLI's — and it did, in the first run, which is why
   that run reported `rc: -15` under restriction against `143` unrestricted. The harness now resolves
   the CLI's pid (`signalled_the_wrapper: false` in both rows) and the two sides agree exactly.
2. **Descendants are enumerated before the signal, not after.** Once the parent is reaped its
   surviving descendants have been reparented and can no longer be found from its pid, so a
   post-reap scan reports "no survivors" whether or not any survived. `watched_before_signal` is now
   captured first and each pid re-checked afterwards. Doing so also revealed what the earlier scan
   had missed entirely: the CLI's inherited MCP children (`renga mcp-peer`, a `bun` server), which
   is the evidence behind H1's qualification in §3.5.

**C — observer, outside the harness: did the restricted run's child keep normal access?**

```
restricted run requested session id: 56d6de52-1a08-424d-a2d0-8d77456b362d
<config-dir>/projects/<...>-scratchpad-i01/56d6de52-1a08-424d-a2d0-8d77456b362d.jsonl  58298 bytes
```

Yes: the child wrote a full transcript to the **real** config directory. This is the control the
fence search's §2 trap demands — the restriction was on the harness, and the child's own state was
demonstrably intact, so the run is not a silent false negative.

**A run-to-completion control.** The same trivial spawn was additionally run to normal completion
both ways, to compare the success path as well as the termination path:

```
{"label":"unrestricted","rc":0,"is_error":false,"has_session_id":true,
 "json_keys":["api_error_status","duration_api_ms","duration_ms","fast_mode_disabled_reason",
  "fast_mode_state","is_error","modelUsage","num_turns","permission_denials","result","session_id",
  "stop_reason","subtype","terminal_reason","time_to_request_ms","total_cost_usd","ttft_ms",
  "ttft_stream_ms","type","usage","uuid"],
 "harness_sees_config_entries":34}

{"label":"restricted","rc":0,"is_error":false,"has_session_id":true,
 "json_keys":[ ...identical list... ],
 "harness_sees_config_entries":0}
```

Identical rc, identical `is_error`, identical key set, session id present in both — with the config
directory empty for the harness in one and populated in the other.

**A syscall-level negative, for the "does not currently read them" objection.** The unrestricted
harness was traced (parent process only, so the child runs untraced and normally):

```
$ strace -e trace=openat,connect,execve -o harness.log python3 mini.py ...
harness-only trace: 101 syscalls
references to the config dir or any claude socket: 1
  openat(AT_FDCWD, "/home/happy_ryo/.claude", O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_DIRECTORY) = 3
```

The single hit is the **self-check itself** — the line that counts the config directory's entries in
order to report `harness_sees_config_entries`. Outside the measurement, the harness process opens
nothing under the config directory and connects to no socket, across its whole life.

**What this negative does and does not prove.** It proves the harness's supervisor path is
internals-free under execution, not merely by inspection, and that the child was unrestricted while
the harness was restricted. It does **not** prove an unforgeable denial: the harness runs in the
mount namespace that also holds the alias paths the child wrapper uses, so a harness that *wanted*
the config directory could reach it through the alias. Making the grant one-way would need a
privileged mechanism this environment does not have unprivileged; every kernel restriction available
here (namespaces, seccomp, Landlock) is inherited by children, which would deny the child too and
recreate the fence search's trap. The honest statement is: **the documented internal paths are denied
to the harness at their real locations, the child demonstrably keeps them, and the harness is shown
by syscall trace not to reach for them by any route.**

---

## 4. Cleanup

- **Background sessions started: zero.** Every spawn in this note was `-p`, which holds no roster
  row. The roster afterwards shows the same three background rows as U1's E1 baseline, plus rows for
  interactive sessions belonging to other workers running concurrently; none is from this
  experiment.
- **Stray processes: none.** After the final probe, no process matching this experiment's scratch
  path remained. Every child started here was terminated and reaped, including the deliberately
  orphaned pairs in §3.6, which completed on their own and were re-checked as gone at t+48 s. The MCP
  stubs orphaned by §3.5's pid-only kills were killed explicitly by the harness between cases.
- **Two intermediate interruptions are disclosed.** The first admission-window sweep was killed by a
  2-minute tool timeout, and one `strace` attempt timed out at 6m40s; the process table was checked
  after each and no `claude` child of this experiment survived either.
- **Transcripts left on disk:** 63 `.jsonl` files under two scratch project slugs
  (`...-scratchpad-i01` and `...-scratchpad-i01-other-cwd`), the second created by §3.8.3's
  different-cwd probe. As in the two prior notes, `-p` runs leave transcripts and hold no roster row
  and no process.
- **No worktree was created** (the scratch cwd is not a git repository). Nothing was written inside
  the Interlock worktree except this note and the two harness files.

---

## 5. What this means — proposed reading

Propose-only, per the scope statement. Items 1, 3 and 8 are named because issue #6 supplies their
provider-side inputs; none is discharged here.

### 5.1 Item 1 — the public CLI alone can start, read, stop and reap a worker

**Proposed: the supervisor half is satisfied for C2, and the negative is executed.** Spawn,
machine-parseable state read from published output, signal-terminate and reap all work with
documented flags; argv, output and exit codes are recorded verbatim (§3.2-§3.5); the internals-free
negative was run against the harness with the child unrestricted (§3.10); and the CLI version and
capability-probe output are recorded (§3.1, §2).

Item 1 is **not** discharged by this note: it also requires **resume**, which is #7's evidence and
needs a real multi-turn session.

Two provisos a reader should carry forward:

1. The state read is machine-parseable only under `--output-format json` / `stream-json --verbose`.
   The default `text` format is not a supervisor surface.
2. "Stop and reap" means the **process group** (§3.5). A pid-only stop satisfies the letter of item 1
   while leaving the child's children running.

### 5.2 Item 3 — effective configuration

**Proposed: partial readback exists; D-0023's weakening is still needed, and can be narrowed.**
`permissionMode` is directly readable and reflects the flags. The effective tool list is readable but
is a snapshot taken while MCP servers are still connecting, so it is diffable only under the
normalisation the same event supplies — every `mcp_servers` entry `connected`, or MCP tools excluded
from the comparison (§3.9). Hooks and sandbox are reported by no public surface examined. So item 3's
equality check becomes runnable for permission mode, runnable-with-normalisation for the tool set,
and remains unrunnable for hooks and sandbox. #9's breach battery therefore cannot be reduced to a
complement on this evidence, and D-0023 stands — but the part of item 3 it has to cover is smaller
than it was.

### 5.3 Item 8 — Secretary responsiveness under worker load

**Proposed: the provider imposes no serialisation, and the obligation is entirely Interlock's.**
Reading state from 8 concurrent children costs about 0.1 ms per sweep with flat per-child cost
(§3.7). There is no daemon to serialise queries, which is C2's structural advantage on this item.
The item's "no blocking dependency" clause therefore has to be shown against Interlock's own readout
code: a `select`-based reader satisfies it, a blocking per-child `readline()` violates it by seconds
per child. The blocking control run here was inconclusive and is flagged as such; #21 should
re-measure it against a live child rather than a finished one.

### 5.4 A C2 cost fact the plan should absorb

A `-p` spawn inherits the invoking user's whole configuration — CLAUDE.md, skills, plugins, MCP
servers, `SessionStart` hooks (§3.2). Measured cost for the identical trivial prompt
`"reply with ok"` ranged from **0.037 USD** (warm cache) to **0.76 USD** (cold cache, 38k
cache-creation tokens), and 66 such invocations cost USD 6.02 in total. Under C2 the supervisor pays
this per spawn, and it is governed by inherited configuration rather than by the task. Pinning
`--setting-sources` (or `--bare` / `--safe-mode`) is therefore both a **reproducibility** and a
**cost** decision, and belongs in the spawn contract rather than in an operator's habits.

### 5.5 What is *not* claimed

- Nothing here re-opens **item 2**. D-0027 failed it on Agent View, and §3.8.3's per-cwd finding is
  reported as a provider fact, not as a route back. It reinforces D-0027 part 3: the exclusion has to
  be Interlock's own.
- Nothing here is evidence about the **SDK** (C3) or about **resume** (#7). Every result is about the
  `-p` CLI entry path on 2.1.234.
- The window width, the readout latencies and the U34 landmark times are **one machine, one load,
  WSL2**. They are inputs to a design parameter, not constants.
- The **exit-code table is complete for the causes issue #6 names** and for those met in passing. It
  is not claimed exhaustive over all causes the CLI can produce.

---

## 6. New unverified items this raises

Proposed additions to the Appendix A register, continuing past U37. The report proposes; it does not
assign.

| # | Question | Why it matters |
|---|----------|----------------|
| **U38** | Does **removing the persisted transcript release the "already in use" claim**? Attempted here and **blocked by this worker's own classifier** (§3.8.4); not answered. | Closes U36's remaining half. If removal releases the claim, on-disk state can clear or spoof it, and the id is not durable in the way U28's positive suggested. |
| **U39** | *(answered here, listed for the register)* Is the `-p` claim **global or per-project**? **Per-project** — the same id is admitted from a different `cwd`, leaving two transcripts under one session id (§3.8.3). | A second admission route needing no race. A worktree-per-worker arrangement plus a retry reproduces it deterministically. |
| **U40** | Is there **any** public surface reporting a running worker's effective **hooks** and **sandbox** configuration? None was found (§3.9); only permission mode, the tool list and MCP status are exposed. | Decides how far D-0023's weakening of item 3 must extend, and whether #9's battery can ever shrink. |
| **U41** | *(partly answered here)* The `init` **`tools`** array differs between two runs of an identical configuration, and the difference is exactly the tool family of the one MCP server reported `pending` rather than `connected` (§3.9). Does requiring every `mcp_servers` entry to read `connected` make the array a sound equality oracle, or are there other sources of drift — plugin load order, deferred tools, skills? | Decides whether item 3's tool-set diff is usable at all. A flapping oracle is worse than no check, and the normalisation has to be shown sufficient rather than assumed. |
| **U42** | Is **SIGINT exiting 0** with `is_error: true` intentional and stable across versions, or an artifact? (§3.4) | A supervisor's cancel path runs through exactly this case. If a future version changes it, code that reads the body rather than rc is unaffected — which is the argument for writing it that way now. |
| **U43** | Is there any public CLI way to **enumerate or reclaim running `-p` children** after a supervisor restart? `claude agents` covers background sessions only, and §3.6's orphans held no roster row. | Determines whether C2's restart-side reclaim can use the provider at all, or must be entirely Interlock's durable pid/pgid record. |
| **U44** | Does `--no-session-persistence` mean a session has **no claim at all** for its whole life (§3.8.3a), and if so, is any `-p` mode both non-persistent and fenced? | Rules a mode in or out for any use that assumed the refusal applies universally. |
| **U45** | Does the U34 bound — window closes at transcript creation, ~2.9 s here — hold on other machines, disks and loads, and does startup work (a `SessionStart` hook fired before `init` here) move it? | The retry-delay parameter is derived from this. §3.8.2 answers the *mechanism*; the *number* is one machine. |

---

## 7. Bottom line

The `claude -p` supervisor surface is usable through documented flags, and this note executed rather
than asserted the internals-free negative: with the harness's own view of the CLI config directory
emptied and its sockets neutered, and the child handed all three back, spawn, structured read,
signal-terminate and reap were identical, the child wrote a real transcript, and a syscall trace of
the harness shows it reaches for none of it.

What a C2 supervisor must be written to survive is now named. Its child's children outlive every
signal aimed at the child's pid, so the unit of termination is the process group. Its `-p` children
outlive **it** — reparented to init, still working, still writing, finishing successfully with no row
anywhere saying so — and no public surface reclaims them. Exit 0 continues to mean nothing, and now
means nothing on the interrupt path specifically, where a `SIGINT`-cancelled run reports success to
the shell and failure in its own JSON body. And the identity claim that the fence search found to be
non-atomic within ~2-3 s turns out to be scoped to the working directory, so a retry in a different
worktree shares an id with no race required.

The one genuinely good piece of news for the gate is item 8: reading state from eight concurrent
children costs about a tenth of a millisecond, flat per child. Under C2 the provider serialises
nothing, and whether the Secretary stays responsive is decided by Interlock's own readout code.

`ACCEPTANCE.md` §2's fencing token, validated atomically on every protected write, was already the
only exclusion on the record after D-0027. Nothing measured here supplements it, and §3.8.3 removes
the last reason to imagine the provider's refusal is a partial version of it.

Verdicts and any `D-` entry are left to a human.
