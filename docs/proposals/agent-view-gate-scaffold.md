# Proposal — Agent View gate scaffold (Q-0021) and fallback `SessionProvider` (Q-0004)

**Status: proposal. Propose-only.** This document decides nothing. It adds no `D-` entry, changes
no `Q-` status, and amends none of `CHARTER.md`, `DECISIONS.md`, `ACCEPTANCE.md`, or
`PORTING_LEDGER.md`. It exists so a human can decide **Q-0021** and **Q-0004**, and it stops at
options, evidence, and trade-offs. Section 6, *Decisions requested*, lists what is being asked.

**Scope.**

- **Q-0021** — for each of the eleven Agent View gate items in `ACCEPTANCE.md` §1, what is the
  minimum scaffold that makes the item pass/fail checkable, and in what order should the scaffold be
  built.
- **Q-0004** — if the gate fails, which concrete alternative `SessionProvider` candidates exist, how
  do they score against the obligations `D-0009` and gate item 11 impose, and what evidence is still
  missing before one can be chosen.

**Evidence discipline.** Every factual statement about what the Agent View public CLI can do is
either (a) backed by a verbatim quote from official documentation or from `--help` / `--json` output
actually run on this machine, with the source named in §1 and §7, or (b) placed in the *Unverified
register* (Appendix A) and never used as the basis of a recommendation. Where the two disagree, the
disagreement is reported rather than resolved (see §1.5, F5). No behaviour was inferred from a
capability's plausibility.

**Cross-references** are by stable ID (`D-00NN`, `Q-00NN`, `AC-N`, gate item number) per the
convention in `ACCEPTANCE.md`. Gate item numbers below always mean `ACCEPTANCE.md` §1.

---

## 1. Evidence base

### 1.1 Verification log

| What | How | When |
|---|---|---|
| Local CLI identity | `claude --version` → `2.1.234 (Claude Code)` | 2026-08-18 |
| Top-level surface | `claude --help` | 2026-08-18 |
| Agent-view surface | `claude agents --help` | 2026-08-18 |
| Structured state readout | `claude agents --json`, `claude agents --json --all` (read-only; nothing spawned, stopped, or removed) | 2026-08-18 |
| Hidden session subcommands | `claude attach --help`, `claude logs --help`, `claude stop --help`, `claude kill --help`, `claude rm --help`, `claude respawn --help`, `claude daemon --help` | 2026-08-18 |
| Official reference | <https://code.claude.com/docs/en/cli-reference> | 2026-08-18 |
| Official agent-view page | <https://code.claude.com/docs/en/agent-view> | 2026-08-18 |
| Official headless page | <https://code.claude.com/docs/en/headless> | 2026-08-18 |
| Candidate-backend docs | Agent SDK, Claude Code on the web, self-hosted environments, devcontainer, GitHub Actions, Managed Agents (URLs in §7) | 2026-08-18 |

Two methodological notes, because they affect how much weight the rows below carry.

1. **No session was mutated.** Nothing in this investigation started, stopped, attached to, removed,
   or respawned a session, and no daemon was stopped. Every question whose only answer is an
   experiment is therefore in Appendix A, not in §1.2.
2. **Summarised fetches were re-fetched verbatim.** A first pass over the agent-view page was
   returned through a summarising layer and asserted that untracked files follow the
   uncommitted-changes path on session deletion. A verbatim re-fetch does **not** contain that
   statement. It has been removed and moved to Appendix A. Treat this as the calibration example for
   the whole document: a summary of a source is not a source.

### 1.2 Verified — the Agent View public CLI surface

| # | Fact | Source |
|---|---|---|
| V1 | `claude --bg` / `--background` starts a session as a background agent and returns immediately. | `claude --help` (2.1.234); cli-reference |
| V2 | After backgrounding, the CLI prints the short ID and the management commands: `backgrounded · 7c5dcf5d · flaky-test-fix` followed by `claude agents`, `claude attach`, `claude logs`, `claude stop` lines. | agent-view |
| V3 | `claude agents --json` prints active sessions — *interactive and background* — as a JSON array and exits, and does not require a TTY. `--all` also includes completed background sessions. `--cwd <path>` filters by start directory. | `claude agents --help`; cli-reference |
| V4 | Observed `--json --all` records on this machine carried `id`, `cwd`, `kind`, `startedAt`, `sessionId`, `name`, `state`, with `state` values `blocked`, `done`, `failed` present in one roster. The docs additionally describe `pid` and `status` while the process is alive, and `waitingFor` when blocked (`permission prompt` / `input needed` / `sandbox request` / `worker request` / `dialog open`). | `claude agents --json --all` (2.1.234); agent-view |
| V5 | `claude attach <id>`, `claude logs <id>`, `claude stop <id>` (alias `claude kill`), `claude rm <id>`, `claude respawn <id>` (and `respawn --all`) exist and are documented. They are **not** listed in the top-level `claude --help` `Commands:` block on 2.1.234. | cli-reference; each subcommand's `--help` (2.1.234) |
| V6 | `claude respawn` restarts a background session, running or stopped, **with its conversation intact**. | cli-reference |
| V7 | `claude stop` keeps the conversation; the local help directs you to resume via `claude attach <id>`. `claude rm` removes the session row; the transcript stays on the machine and remains reachable through `claude --resume`. | `claude stop --help` (2.1.234); cli-reference |
| V8 | Background sessions are hosted by a per-user supervisor that starts automatically, stops a finished session's process after roughly an hour unattached, and itself exits when every session has finished and no terminal is connected. It restarts a session whose process exits unexpectedly. | agent-view |
| V9 | `claude daemon status` prints the supervisor's pid, version, uptime, socket directory and worker count, exiting 1 when it is not running. `claude daemon stop --any` stops the supervisor *and* the sessions it hosts; `--keep-workers` leaves them running for the next supervisor to reconnect to. Service install is disabled in this version — "the daemon runs on demand and exits when the last client disconnects". | cli-reference; `claude daemon --help` (2.1.234) |
| V10 | Before editing files, a background session moves itself into an isolated git worktree under `.claude/worktrees/`, and worktree isolation is then enforced for the session and its subagents. Isolation is skipped when the session is already in a linked worktree, when the directory is not a git repository and no `WorktreeCreate` hook is configured, or when the write is outside the working directory. It can be disabled with `worktree.bgIsolation: "none"`. | agent-view |
| V11 | The two deletion paths differ. Agent view removes a Claude-created worktree **including uncommitted changes**. `claude rm` **keeps** the worktree, and the session row, when it has uncommitted changes. **Neither** path removes a worktree with unpushed commits, or one another running session claims or has locked. A worktree the user created is left in place either way. | agent-view |
| V12 | For a hook-created directory outside any repository, agent view runs `WorktreeRemove` and **refuses the delete if there is no such hook**. | agent-view |
| V13 | Permission mode, model, effort, and carried configuration flags (`--mcp-config`, `--strict-mcp-config`, `--settings`, `--add-dir`, `--plugin-dir`, `--fallback-model`, `--allow-dangerously-skip-permissions`) **persist** when the supervisor stops and restarts a session's process. A session launched with `bypassPermissions` stays in it rather than falling back to the directory's `defaultMode`. | agent-view |
| V14 | A background session reads its settings from the directory it runs in, as if `claude` had been started there. It does **not** inherit gateway endpoint variables such as `ANTHROPIC_BASE_URL` from the shell that started the supervisor. | agent-view |
| V15 | Settings files that fail validation are **silently ignored** in `-p` / non-interactive mode, with no error shown. | `claude --help` (2.1.234), `-p` entry |
| V16 | Without `--bare`, a `claude -p` session runs the hooks in a project's `.claude/settings.json` and connects the servers in its `.mcp.json` even in a folder that was never trusted. | headless |
| V17 | Every documented path for sending input into a *running* background session is interactive: the agent-view peek panel ("Type a reply in the peek panel and press `Enter` to send it to that session") or `claude attach`. A reply that cannot be delivered is saved and sent as the session's next prompt when its process starts again. | agent-view |
| V18 | `--bg` **cannot** be combined with `-p` / `--print`. | cli-reference |
| V19 | `claude --bg --exec '<command>'` runs a shell command as a background job **instead of** a Claude session. | agent-view |
| V20 | `--name` sets the session's display name in agent view; it is a display name, not an identity. | agent-view |
| V21 | `--session-id <uuid>` uses a specific session ID for the conversation and must be a valid UUID. `-r` / `--resume` resumes by session ID; from v2.1.223 the ID search covers every project on the machine, not only the current one. `--fork-session` creates a new ID instead of reusing the original. | `claude --help` (2.1.234); cli-reference |
| V22 | `--output-format json` (print mode) returns the result, session ID and metadata; `stream-json` emits a `system/init` event carrying session metadata and, from v2.1.205, an optional `capabilities` array intended for feature detection **instead of** version-string comparison. | headless |
| V23 | SIGTERM to a `claude -p` run aborts the turn, kills the Bash process tree, runs `SessionEnd` hooks, and exits 143. | headless |
| V24 | The CLI's `--permission-mode` accepts six choices: `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`. This is **not** the same set the Agent SDK documents (W3a) — the CLI has `manual` and no `default`. | `claude --help` (2.1.234) |
| V25 | Agent view is in research preview: "The interface and keyboard shortcuts may change as the feature evolves." Background sessions are local — preserved across sleep, stopped when the machine shuts down — and consume subscription usage the same as interactive sessions. | agent-view |
| V26 | The only stated qualifier on `claude agents --json` anywhere is "(for scripting; does not require a TTY)". No versioning statement, deprecation policy, schema document, or compatibility guarantee accompanies it. | `claude agents --help` (2.1.234); cli-reference; agent-view |

### 1.3 Verified — the candidate backends (used in §5)

| # | Fact | Source |
|---|---|---|
| W1 | The Agent SDK is described as "A library that runs the agent loop in your own process, in Python or TypeScript"; other languages are directed to run the CLI as a subprocess with `-p` and `--output-format json`. | agent-sdk/overview |
| W1a | **That phrase is about ownership of the loop, not process topology.** The Python SDK reference documents a `transport` option as an "Optional custom transport for communicating with **the CLI process**", and its `Transport` base class as the way to "communicate with the Claude process over a custom channel (for example, a remote connection **instead of a local subprocess**)", with `end_input()` described as closing "stdin for subprocess transports". The default arrangement is therefore a spawned CLI child process, not agent execution inside the host interpreter. | agent-sdk/python |
| W3a | The Agent SDK documents six permission modes — `default`, `dontAsk`, `acceptEdits`, `bypassPermissions`, `plan`, `auto`. Compare V24: the CLI's set differs (it has `manual`, and no `default`), so mode names are not portable between the two surfaces. | agent-sdk/permissions |
| W2 | The SDK resumes and forks by a session ID read from the result message's `session_id`; `listSessions()` / `list_sessions()` and `getSessionInfo()` / `get_session_info()` enumerate and inspect sessions on disk; transcripts live under `~/.claude/projects/<encoded-cwd>/`. Session files are machine-local; the doc offers a session-store adapter, moving the file, or **not relying on session resume at all**. | agent-sdk/sessions |
| W3 | SDK permission evaluation order is hooks → deny rules → ask rules → permission mode → allow rules → `canUseTool`. **A hook can deny outright and that deny applies even in `bypassPermissions`.** A hook `allow` does not skip deny/ask rules. | agent-sdk/permissions |
| W4 | The only documented per-role permission mechanism in the SDK is subagent inheritance with a per-`AgentDefinition` override, and that override does not apply when the parent is in `bypassPermissions`, `acceptEdits`, or `auto`. | agent-sdk/permissions |
| W5 | `claude -p "msg" --cloud <session-id> --output-format json` queues a message to an existing cloud session and returns `{ok, session_id, url}`; `stream-json` is not supported for this form. Claude Code on the web is research preview; sessions stop after inactivity and the VM is reclaimed. | claude-code-on-the-web |
| W6 | In a self-hosted environment, a runner claims a queued session and holds a **lease**; polling doubles as the heartbeat, and roughly 60 s without polling requeues the session to another runner. A runner serves one user at a time — the first claimed session locks it to that account. Public beta, Team/Enterprise, off by default. | self-hosted-environments |
| W7 | A dev container runs commands inside the container while edits appear in the local repository, and `--dangerously-skip-permissions` is usable there because the container runs as a non-root user; the CLI rejects that flag as root. The docs state a dev container is **not** an exfiltration boundary. | devcontainer |
| W8 | Managed Agents is an Anthropic-hosted beta behind the `managed-agents-2026-04-01` beta header. Sessions have four statuses — `idle`, `running`, `rescheduling`, `terminated` — and a session that finishes goes `idle`, not `terminated`. A running session must be interrupted before it can be archived or deleted; delete removes record, events and sandbox. Tools and MCP servers (including permission policies) can be replaced mid-session, but only while idle. Because state is stored server-side it is **not eligible for Zero Data Retention or a HIPAA BAA**. | managed-agents/overview, /sessions, /session-operations, /reference |

### 1.4 Repository-side facts that constrain the scaffold

| # | Fact | Source |
|---|---|---|
| R1 | The existing queue store implements `UNDELIVERED → CLAIMED → DELIVERED` with lease-based claim/reap, mode-epoch fencing and idempotent `DELIVERED` — but its authoritative rows are **in memory and the journal is not replayed**, so a daemon kill loses queue state. The ledger classifies it `carry (invariant) / rewrite (mechanism)` for exactly this reason. | `PORTING_LEDGER.md`, `src/claude_org_runtime/broker/store.py` row |
| R2 | `tests/broker/test_delivery.py` is the largest directly relevant test file — claim-then-confirm, claim-respecting drain, lease-reap recovery, mode-epoch fencing — and is simultaneously the message-identity/ack/dedup invariant set and the fault-injection matrix the gate names. It is not pane-free: 59 pane identifiers across 118 test functions. | `PORTING_LEDGER.md`, `tests/broker/test_delivery.py` row |
| R3 | The dedup module's corruption handling must **not** carry: "a broken state file recovers as empty" permits already-applied effects to replay once dedup state is authoritative. The rewritten store must fail closed or rebuild from durable records. | `PORTING_LEDGER.md`, `src/claude_org_runtime/attention/dedup.py` row |
| R4 | `attention/readers.py` collapses read failure into an *empty result*, which makes `OBSERVATION_UNAVAILABLE` indistinguishable from `NO_ACTIVITY_EVIDENCE`; the rewritten reader must return a typed unavailable result. | `PORTING_LEDGER.md`, `attention/readers.py` row |
| R5 | Per-role permission / sandbox / hooks generation carries (`settings/generator.py`), as does the symlink-crossing sandbox breach probe (`settings/sandbox_doctor.py`); the discarded parts are the `transport.descriptor` dependency and the A/B/C `sandbox_by_pattern` axis. | `PORTING_LEDGER.md`, settings rows |
| R6 | `tests/attention/test_broker_journal_contract.py` is the one accident-derived fixture that pins a producer↔consumer contract end to end. | `PORTING_LEDGER.md` |

### 1.5 Five findings that change the shape of the gate

These are the load-bearing consequences of §1.2. Each is a fact plus its implication; the implication
is a proposal, not a decision.

**F1 — There is no non-interactive path to deliver a message into a running background session
(V17, V18).** Every documented input path is the agent-view peek panel or `claude attach`, and `--bg`
cannot be combined with `-p`. This is not a gap in the gate's evidence; it is a structural fact about
the provider, and it settles how gate item 6 must be read. `MessageBus` cannot be layered *on top of*
the Agent View surface at all, because that surface has no ingress. The only shape left is
**worker-outbound**: the worker connects to Interlock's bus as a client (an MCP server supplied at
dispatch via `--mcp-config`, which `claude agents` accepts as a dispatch default), and delivery is a
pull from the worker rather than a push from the control plane. Read that way, `D-0009`'s separation
of `SessionProvider` from `MessageBus` is not merely prudent decoupling — for this provider it is the
*only* workable arrangement, and gate item 6's "statically assert the `MessageBus` implementation has
no dependency edge to the `SessionProvider`" becomes cheap to satisfy because no such edge is
available to build.

**F2 — Nothing promises fail-closed on missing or corrupt configuration, and there is affirmative
evidence of fail-open (V13, V15, V16).** The documented guarantee is *persistence* across supervisor
restart, which is the first half of gate item 3 and is well evidenced. The second half — "fail closed
rather than falling back to default permissions when configuration is missing" (`D-0017`) — has no
supporting documentation, and two facts point the other way: settings files that fail validation are
silently ignored in non-interactive mode, and project hooks and MCP servers load even in an untrusted
folder. The only documented refusal-to-start in this area is the one-time `bypassPermissions`
disclaimer gate, which is a consent gate, not a config-integrity gate. **Implication:** fail-closed
cannot be inherited from the harness. Interlock must own it, as a *spawn precondition* — validate the
rendered per-role configuration and refuse to spawn — plus a `PreToolUse` hook as the in-session
backstop, since a hook deny is documented to run first and to apply even under `bypassPermissions`
(W3). This is an Interlock obligation whichever provider wins, so building it is not
provider-specific work.

**F3 — Session identity is reported after the fact, and pre-assigning it is unverified (V2, V21;
Appendix A/U1).** `--bg` prints the short ID after the spawn; `--session-id <uuid>` exists as a
top-level flag but no source states that it composes with `--bg`, and the cli-reference entry for
`--bg` enumerates what it *can* be combined with (`--exec`, `--agent`) and one exclusion (`-p`)
without mentioning `--session-id`. This is the single riskiest unknown in the whole gate, because gate
item 2's crash window is exactly the interval between "spawn issued" and "identity known". If the ID
can be chosen in advance, the binding commits **before** the spawn and the window closes by
construction. If it cannot, the design needs a durable *spawn-intent* row plus a reconciliation rule
that adopts at most one session per intent — a strictly harder proof. **Implication:** this is the
one experiment worth running before anything else is built; it is a single command and it decides a
design, not a detail. Until it is run, no scaffold should be designed to *depend* on pre-assigned IDs.

**F4 — `--exec` gives a token-free lifecycle probe, for the part of the lifecycle that is not about a
conversation (V19).** `claude --bg --exec '<command>'` runs a shell command as a background job
**instead of** a Claude session. Within that limit it is genuinely useful: the supervisor's own
behaviour — spawn, roster listing, structured-state readout, stop, removal, restart after an
unexpected exit, behaviour under an ungraceful daemon kill, and readout latency with N jobs running —
can be exercised deterministically with a script whose behaviour we control (`sleep`, `exit 1`,
`trap`), at zero model cost and with no subscription-quota consumption, repeatably in CI.

**What `--exec` cannot establish, and must not be used for.** A shell job has no conversation and no
Claude in the loop, so three things are out of its reach and need **model-backed** `--bg` sessions:

- *Conversation resume.* `claude respawn` is documented as restarting a session "with its conversation
  intact" (V6). A job has no conversation, so a passing `--exec` respawn says nothing about O5.
- *Worktree isolation and its lifecycle.* V10 is explicit that the move into `.claude/worktrees/`
  happens "Before editing files, Claude moves the session into an isolated git worktree" — an action
  of the agent, not of the supervisor. Gate item 7 therefore cannot be discharged with jobs at all.
- *Session identity for a conversation.* See F3 and U1: `--session-id` identifies a conversation, and a
  job does not have one.

So `--exec` lowers the cost of the *supervisor-facing* half of Tier 1 (and of item 8's load
generation) but not of items 1-resume or 7, which must be budgeted as real sessions consuming
subscription quota (V25). *Caveat:* `--exec` does not appear in `claude --help` on 2.1.234, so its
presence on this build is Appendix A/U2.

**F5 — Documentation and local help disagree about `claude rm` (V5, V11).** `claude rm --help` on
2.1.234 says flatly "Delete a background session and its worktree", while the agent-view page says
`claude rm` keeps the worktree when it has uncommitted changes and that neither path removes one with
unpushed commits. Both are primary sources and they cannot both be a complete description.
**Implication:** gate item 7 must be discharged by *observation of the running build*, not by reading
either source, and the discrepancy is itself a data point about how much of this surface is
contract and how much is current behaviour — which bears directly on `D-0010`'s fail-closed posture
and on V26's absence of any compatibility guarantee.

**F6 — If U1 fails, gate item 2 fails, and that is a `Q-0004` trigger rather than a design problem to
absorb.** It is tempting to treat "identity cannot be pre-assigned" as an inconvenience to be handled
with a spawn-intent row and a reconciliation pass that adopts the matching session afterwards. It is
not. `ACCEPTANCE.md` item 2's predicate is that "A single-writer violation at any injection point is a
gate failure", and post-hoc attribute matching cannot establish the absence of a violation: the only
attributes available (`cwd`, `startedAt`, `name`) are all learned *after* the spawn, `name` is a
display name by construction (V20), and the injection point that matters — a kill between spawn and
commit, followed by a retry — is exactly the one that can leave two live sessions matching one intent
before any reconciler runs. Adoption can pick a winner; it cannot prove the loser never wrote.

What *would* rescue it is a pre-spawn fence rather than a post-hoc match: some identifier or token
committed to SQLite before the spawn that the second writer's protected writes must carry and that the
first commit invalidates — the same fencing-token discipline `ACCEPTANCE.md` §2 already requires of
leases. Whether the provider offers any such handle if `--session-id` does not compose is not known
from any source. **Implication:** phase 0's experiment is not a detail-gathering step; a negative
result is a candidate gate failure that routes directly to `Q-0004`, and the honest thing is to say so
in advance rather than to soften item 2 after the fact.

---

## 2. A prior question the gate exposes

Gate item 11 says "only the `SessionProvider` need be swapped" and proposes to demonstrate it by
implementing a second provider behind "the same contract". **That contract does not yet exist in
writing.** `D-0009` names five verbs — start, list, obtain structured state of, stop, resume — with
no signatures, no state model, and no error contract. Three capabilities the gate items lean on are
not among those five, and it is not stated which contract owns them:

| Capability | Needed by | Which contract owns it? |
|---|---|---|
| Deliver a message to a worker | item 6 | `MessageBus` — and per F1 it must be worker-outbound, so arguably neither owns an ingress |
| Read back a session's *effective* permission / sandbox / hook configuration | item 3 | Unassigned; no public readback exists (Appendix A/U3) |
| Observe or veto a workspace lifecycle transition | item 7 | Unassigned; `WorktreeCreate` / `WorktreeRemove` hooks are the only evidenced handle (V10, V12) |

This is not a defect in `ACCEPTANCE.md`; it is the natural consequence of `Q-0021` being open. But it
means **item 11 has nothing to substitute against until the interface is written down**, and it makes
"write the `SessionProvider` interface" the first scaffold artifact rather than a by-product. It is
raised as Decision 2 in §6.

---

## 3. Q-0021 — minimum scaffold, item by item

### 3.1 Classifying the eleven items by what they presuppose

`Q-0021` and `ACCEPTANCE.md` §1 both draw the line as *items 1–3 are spike-checkable, items 4–6, 9,
10 and 11 presuppose what they test*. That split leaves items 7 and 8 unplaced, and it puts item 2 on
the wrong side: item 2's own verification method says "Persist the session↔run binding in SQLite at
spawn", which is not something a thin CLI harness has. This proposal suggests a **three-tier**
classification instead. The tiers are ordered by what has to exist, not by difficulty.

| Tier | Meaning | Items |
|---|---|---|
| **T1 — provider probe** | Dischargeable by a throwaway harness that only drives the public CLI and observes the filesystem. No Interlock control-plane code. | 1, 3, 7 |
| **T2 — minimal durable core** | Requires a small but real slice: a SQLite schema, a lease, an outbox, one action handler, and a `SessionProvider` interface with two implementations. | 2, 4, 5, 6, 11 |
| **T3 — organisational context** | Requires a counterpart that is not a session backend at all: a Secretary intake, a Curator promotion path, or v1 as a rollback counterparty. | 8, 9, 10 |

Two observations follow, and both are consequential.

- **Item 2 moves from T1 to T2.** Its crash-window proof needs a durable binding row and a
  supervisor to kill. It is the *cheapest* T2 item — one table and one kill harness — but it is not a
  T1 item, and treating it as one is how a gate gets quietly weakened.
- **Item 9 tests nothing about the session backend.** `ACCEPTANCE.md` §4 already lists which items
  must be re-run in full against a new provider — 1, 2, 3, 7, 8, 10 — and item 9 is deliberately
  absent from that list. Item 9's dependency is on the Curator promotion path and the approval
  record, both of which are Interlock-internal. It can therefore be discharged entirely
  independently of the Agent View verdict, in parallel, by whoever is free. Whether it *should* be
  part of the Agent View spike at all is Decision 3.

### 3.2 Scaffold component inventory

The per-item table in §3.3 refers to these named artifacts. They are the whole build surface being
proposed; nothing else is required by any item.

| ID | Artifact | Rough size |
|---|---|---|
| **S1** | `SessionProvider` interface: five verbs, a session state model mapped onto `D-0005`'s closed fact-state set, a typed error/unavailable result (never an empty one — R4), and a capability/version probe with a fail-closed spawn precondition (`D-0010`). | interface + docstrings; ~150 LOC |
| **S2** | Agent View provider implementing S1 over `--bg` / `agents --json` / `stop` / `respawn`, with a tolerant parser (V26 — no schema guarantee). | ~300 LOC |
| **S3** | Stub provider implementing S1 over local child processes, with no Claude in the loop. Deliberately trivial (gate item 11 names it). | ~150 LOC |
| **S4** | CLI probe harness, in two halves (F4): **S4-jobs** drives supervisor-facing behaviour with `--exec` jobs at zero model cost, and **S4-sessions** drives conversation resume and the worktree lifecycle with a small number of model-backed `--bg` sessions, which no job can stand in for. Both assert the internals-free negative (paths made unreadable) and record CLI version and probe output. Throwaway by construction. | ~250 LOC + fixtures |
| **S5** | SQLite schema slice: `run`, `session`, `lease`, `outbox`, `incident`, `action` — the minimum for the items below, not the full `Q-0001` DDL, explicitly marked as a spike schema. | ~200 LOC |
| **S6** | Lease with a fencing token validated atomically as part of each protected write (`ACCEPTANCE.md` §2 requires this; check-then-write is named as insufficient). | ~150 LOC |
| **S7** | Outbox with resend, ack, dedup key, durable retry count, and one action handler that declares its exactly-once mechanism (destination idempotency key *or* transactional commit). | ~350 LOC |
| **S8** | `MessageBus` as a worker-outbound MCP endpoint (F1), plus the static no-dependency-edge assertion item 6 demands. | ~250 LOC |
| **S9** | Fault-injection harness: deterministic kill points (before durable write / after write before side effect / after side effect before acknowledgement), clock skew, SIGSTOP, automated and reproducible — no manual one-shots (item 5 forbids them). | ~300 LOC + cases |
| **S10** | Per-role fencing renderer carried from `settings/generator.py` minus the discarded transport and pattern axes (R5), plus a `PreToolUse` deny hook and a behavioural breach probe. | carry + ~200 LOC |

### 3.3 Per-item minimum scaffold

"Pass/fail predicate" is what makes the row a gate rather than a review. Where `ACCEPTANCE.md`
already proposes a verification method, this column says what has to *exist* for that method to run,
and flags where the evidence available makes the proposed method unrunnable as written.

| # | Tier | Minimum scaffold | Pass/fail predicate | Notes and departures |
|---|---|---|---|---|
| 1 | T1 | S4 | Every one of start / structured-state read / stop / resume completes using only documented public commands, and the harness behaves identically with `~/.claude/jobs`, internal sockets and transcript paths made unreadable. | Verbs are all present and documented (V1, V3, V5, V6, V7). Two caveats: "resume" for a background session is `claude respawn` (V6), not `--resume`, which reopens local history; and `claude attach` needs a terminal, so it is not a control-plane verb. Record the CLI version and probe output (V22 `capabilities`, else `--version`) per `D-0010`. |
| 2 | T2 | S1 + S2 + S5 + S9 | After a kill at each injection point, re-identification yields **exactly one** session per run, and a second writer is refused and the refusal recorded. | Design must not assume pre-assigned IDs until U1 is settled (F3). **If U1 fails, this item fails** unless some other *pre-spawn* idempotent identity or fence is found — see F6. Attribute-matching on `cwd` + `startedAt` + `name` is not such a fence and does not rescue it: `startedAt` is only knowable after the spawn, `name` is documented as a display name and not an identity (V20), and a crash-then-retry can leave two matching workers alive before any reconciliation runs. `ACCEPTANCE.md` states that "A single-writer violation at any injection point is a gate failure"; recording a weaker guarantee instead would be reclassifying the item, not passing it. |
| 3 | T1 | S4 + S10 | Restart preserves the fence, **and** a deliberately broken configuration causes a **refused** spawn, never a downgraded one, with the refusal recorded. A role-forbidden operation is denied after restart. | `ACCEPTANCE.md` proposes diffing the effective configuration before and after restart. **There is no public readback of effective configuration** (Appendix A/U3), so that method is not runnable as written. Proposed substitute: a *behavioural* breach probe — attempt one forbidden operation per role and assert denial — as the observable, with the config diff done on Interlock's own rendered inputs. And per F2 the fail-closed half must be Interlock's own spawn precondition; asserting it against the harness would be asserting something no source promises. |
| 4 | T2 | S5 + S6 + S7 + S9 | State is reconstructed by query from SQLite alone; work resumes from unresolved incidents; every side effect is applied exactly once, evidenced by an idempotency/dedup record. | The "supervisor" in this row is Interlock's, not Claude Code's. What the *provider's* supervisor does under an ungraceful kill is separately unknown (Appendix A/U4) and should be probed as part of S4 rather than assumed. |
| 5 | T2 | S6 + S7 + S9 | Every case in `ACCEPTANCE.md` §2 is automated and reproducible, and each external-effect case is additionally proven against the destination's own idempotency record. | The one handler in S7 must *name* its mechanism. If no candidate destination offers an idempotency key, `ACCEPTANCE.md` §2's second option — transactional commit of effect and record together — is the only route, and where neither is achievable the action needs a human gate (`D-0004`). R1/R2 supply the invariants; R3 rules out the "corrupt state recovers as empty" behaviour outright. |
| 6 | T2 | S8 + S7 + one dispatched worker | With no agent-view UI attached, a task is sent, the first delivery is dropped, and the outbox resends with exactly one ack. Repeat with the UI attached but stale; outcomes unchanged. Static assertion: no dependency edge from `MessageBus` to `SessionProvider`. | Per F1 the transport is necessarily worker-outbound; the "UI not attached" condition is trivially satisfiable because the UI is not on the delivery path at all. This makes the item *easier* than it reads, and that should be stated in the gate record rather than claimed as a strong result. |
| 7 | T1 | S4 + a git fixture | After every provider-driven lifecycle transition the public CLI exposes, working-tree content is byte-identical, or the transition is refused while unsaved work exists. | Partially answerable from documentation already (V11, V12): `claude rm` is documented non-destructive for uncommitted changes and both paths refuse on unpushed commits. But agent view's own delete is documented destructive for uncommitted changes, `claude rm --help` contradicts the doc (F5), and **untracked files are documented nowhere** (Appendix A/U5). This item must be settled by observation on the running build. `WorktreeRemove` (V12) is the only evidenced veto handle and should be probed as such. |
| 8 | T3 | A Secretary intake with an explicit queue boundary + load generator (S4 `--exec` jobs at the cap) | No Secretary response is blocked behind worker monitoring, long work, or AI judgement — shown structurally (intake and queue boundary are asynchronous) and empirically (baseline vs load latency). | Threshold unresolved (`Q-0011`); the gate check is absence of blocking plus a recorded comparison. A provider-side unknown remains: whether the daemon's control interface serialises status queries behind busy workers (Appendix A/U6). Probing that is cheap with `--exec` jobs and should be part of S4. |
| 9 | T3 | Curator stub + approval record with a content digest + a path audit | Promotion is refused with the approval absent, forged-but-unrecorded, revoked, mutated-after-approval, and replayed against a different candidate. The build fails if a bypassing code path is added. | **Zero session-backend dependency** (§3.1). Can run in parallel with, or independently of, the Agent View spike. |
| 10 | T3 | A run-start routing point + a run→owning-system ledger + a writer audit over both stores | Exactly one new run routed to Interlock; no run changes owner mid-flight; no record written by both systems; a rehearsed rollback changes only the routing decision. | Requires v1 as counterparty, so it is the least "pre-implementation" item on the list. Numeric criteria unresolved (`Q-0005`). Realistically this is proven *at* the canary, not before implementation — which is a genuine tension with `D-0019` and is raised as Decision 3. |
| 11 | T2 | S1 + S2 + S3 + the control-plane suite | The suite — SQLite SoT, fact states, incident lifecycle, `MessageBus` delivery/ack/dedup, role boundaries — runs unchanged against S3. Any test requiring modification marks a leak and must be fixed first. | Blocked on S1 existing at all (§2). Cheapest if S3 is written **first**, before S2, so no Agent-View-shaped assumption ever enters the tests. See strategy C in §3.4. |

### 3.4 Three whole-slice strategies

These differ in how much of T2 is built before the provider verdict is taken. All three assume T1 and
the F3 experiment happen first; they are not alternatives on that point.

---

**Strategy A — Probe-first, two-stage gate.**

Build S4 only (plus the F3 experiment). Take the provider verdict from T1 evidence — items 1, 3, 7
plus the item-2 identity experiment — and defer every T2/T3 item to a second stage after the verdict.

- *Scope:* S4 (both halves), S10's breach probe, the identity experiment. **Estimated 3–5
  engineer-days**, plus a small, bounded amount of subscription quota for the model-backed probes
  S4-sessions requires (F4).
- *Strengths:* Cheapest possible answer to the question that actually decides everything — "does this
  backend hold?". Nothing built is wasted if the answer is no, because S4 is throwaway by design and
  the identity/fencing questions must be re-asked of any replacement anyway. Fastest route to
  discovering a `Q-0004` situation, which is the expensive discovery to make late.
- *Weaknesses:* It does not discharge the gate. Seven of eleven items remain open when implementation
  starts, which reads the gate as an advisory rather than a precondition and is in tension with the
  plain text of `D-0019`. It also defers the one class of finding that most changes the control-plane
  design — how exactly-once behaves under injection — past the point where the design is set.
- *Over-build risk:* Lowest. Nothing durable is built.

---

**Strategy B — Minimum vertical slice.**

The shape `Q-0021` itself sketches: "a schema, a lease, an outbox, one handler". Build S1–S9 (S10 as
carried code), discharge T1 and T2, and handle T3 with narrow substitutes — item 8 against a stub
Secretary, item 9 against a Curator stub, item 10 as a *rehearsal* on a synthetic counterparty with
the real canary re-proven later.

- *Scope:* S1–S9 + S10. **Estimated 10–15 engineer-days.**
- *Strengths:* Actually discharges nine of eleven items pre-implementation, with 8 and 10 explicitly
  marked "proven on the spike slice, re-proven on the real implementation" — the distinction
  `Q-0021` asks for. Produces the fault-injection suite early, which is where the design learns most.
  The T2 artifacts are the ones `PORTING_LEDGER.md` already classifies `carry (invariant)`, so the
  invariants are known before a line is written (R1–R6).
- *Weaknesses:* The slice is indistinguishable in kind from the real implementation, only smaller.
  That is the over-build hazard: a spike schema becomes the schema by inertia, and `Q-0001` gets
  answered by accident instead of by decision. Guarding against that costs discipline (see the
  mitigation below).
- *Over-build risk:* **Highest of the three, and it is the risk to manage rather than avoid.**
  Mitigation: mark S5 as a spike schema in the file itself with an explicit "no migration path is
  promised from this" note; forbid the spike slice from being promoted without a `D-` entry; and keep
  the *tests* rather than the *implementations* as the durable output.

---

**Strategy C — Contract-first, dual-provider from the first commit.**

Write S1 first. Implement S3 (the stub) **before** S2 (Agent View). Run every control-plane test
against both providers from the first test onward, in CI.

- *Scope:* Strategy B's scope, reordered, plus the cost of a second provider carried from day 1.
  **Estimated 15–22 engineer-days.**
- *Strengths:* Item 11 stops being a demonstration at the end and becomes a structural property
  maintained continuously — a leak of session-backend detail into the control plane fails CI the day
  it is introduced, rather than being discovered at the gate. This is the single most valuable
  property to hold if the Agent View verdict is genuinely uncertain, and `CHARTER.md` §5 and `D-0009`
  both say it is. It also makes a `Q-0004` swap cheap by construction, which is the whole promise of
  `D-0019`.
- *Weaknesses:* Most up-front cost, and it front-loads work whose value is contingent — if Agent View
  passes cleanly, the second provider is insurance that was never claimed. It also risks
  contract-designing in the abstract, before the provider has taught anyone what the contract needs
  to express.
- *Over-build risk:* Medium. The extra artifact (S3) is small and gate item 11 mandates it anyway;
  the real risk is over-specifying S1 before S2 exists to inform it.

---

**Comparison.**

| | A — probe-first | B — minimum slice | C — contract-first dual |
|---|---|---|---|
| Items discharged pre-implementation | 1, 3, 7 (+ partial 2) | 1–7, 9, 11 (8, 10 as rehearsals) | same as B |
| Estimated effort | 3–5 d | 10–15 d | 15–22 d |
| Faithful to `D-0019` as written | No | Largely | Largely |
| Cost if the gate fails | Near zero | Moderate — S2 and its tests rework | Low — swap S2, suite unchanged |
| Cost if the gate passes | Work still ahead | Slice may become the implementation | Second provider unclaimed |
| Over-build risk | Low | High | Medium |

**Recommendation: Strategy B, with one rule taken from C — write S1 first and implement S3 before
S2.** Call it B+; the marginal cost over B is roughly 1–2 days (S3 is ~150 LOC and item 11 requires
it regardless), and it buys C's main structural benefit without C's full front-loading. The reason
for preferring it over A is that A does not discharge the gate `D-0019` calls a precondition, and the
reason for preferring it over C is that S1 designed before any provider exists is a contract designed
from imagination. Writing S3 first, then S2, then re-running against S3, gets the discipline without
the speculation.

### 3.5 Recommended order

Ordered by dependency. Each phase has an exit condition; a phase that fails its exit condition is a
report to a human, not a reason to proceed to the next.

| Phase | Work | Discharges | Exit condition |
|---|---|---|---|
| **0** | The F3 experiment: does `--session-id <uuid>` compose with `--bg`? Must be run against a **real** `--bg` Claude session, not an `--exec` job (F4), including the collision case where the UUID is already in use. Two model-backed sessions, minutes. | Nothing directly | U1 answered either way and recorded; if it fails, F6 is triggered before anything else is built |
| **1a** | S4 (jobs) — `--exec`-based probe of the supervisor: roster, structured state, stop, removal, restart-after-unexpected-exit, ungraceful daemon kill (U4), readout latency under N jobs (U6); capability/version probe; internals-free negative | provider-side inputs for items 1, 3, 8 | Supervisor verbs work through documented commands; U2, U4, U6 answered |
| **1b** | S4 (sessions) — the same harness driven by a small number of **model-backed** `--bg` sessions, for conversation resume via `respawn` and for the worktree lifecycle: uncommitted, untracked (U5) and unpushed cases across agent-view delete vs `claude rm` (V11, F5), and whether `WorktreeRemove` (V12) can veto | items 1, 7 | Resume preserves the conversation; working tree byte-identical or transition refused on every path |
| **2** | S10 — carried fencing renderer + `PreToolUse` deny + breach probe + Interlock-side spawn precondition | item 3 | Restart preserves the fence; broken config refuses the spawn; forbidden operation denied |
| **3** | S1 → S3 → S5 — interface, stub provider, spike schema | prerequisite for 2, 4, 5, 6, 11 | Interface written; stub passes an empty suite; schema marked as spike |
| **4** | S6 + S7 — lease with fencing token; outbox with one handler declaring its exactly-once mechanism | prerequisite for 4, 5 | Handler names its mechanism |
| **5** | S9 — fault-injection harness; run the full `ACCEPTANCE.md` §2 matrix | items 4, 5 | Every case automated and reproducible; external-effect cases proven at the destination |
| **6** | S2 — Agent View provider; re-run the whole suite against it | item 2 | Exactly one session per run at every injection point; second writer refused |
| **7** | S8 — worker-outbound `MessageBus` + static no-edge assertion | item 6 | Resend and single ack with no UI attached |
| **8** | Re-run the suite against S3 unchanged | item 11 | Zero test modifications required |
| **9** | Stub Secretary + load generator; Curator stub + approval digest | items 8, 9 | No blocking dependency; all five promotion negatives refused |
| **10** | Routing point + writer audit rehearsal | item 10 (rehearsal only) | Rehearsed rollback changes only routing |

Phase 9's item 9 has no dependency on phases 1–8 and can run in parallel from day 1 (§3.1). Phases 0,
1a and 1b are the ones that can *end* the sequence early by producing a `Q-0004` situation, which is why
they come first.

---

## 4. Q-0004 — the obligations a `SessionProvider` must meet

Derived from `D-0009`'s five verbs, plus what gate items 1, 2, 3, 6, 7, 8 and 11 require of the
backend, plus `D-0010` and `D-0001`. These are the columns of the matrix in §5.3.

| ID | Obligation | Derived from |
|---|---|---|
| **O1** | Start a top-level worker session. | D-0009 |
| **O2** | List sessions. | D-0009 |
| **O3** | Obtain **structured** state, machine-parseable from published output, not scraped from rendered screen text. | D-0009, D-0010, item 1 |
| **O4** | Stop a single named session without collateral effect on others. | D-0009, item 1 |
| **O5** | Resume a stopped session. | D-0009, item 1 |
| **O6** | A stable session identity re-matchable to a run across the crash window, admitting exactly one active writer. | item 2, D-0001 |
| **O7** | Per-role permission / sandbox / hooks that survive restart; fail-closed on missing configuration must be achievable, if not by the backend then by Interlock in front of it. | item 3, D-0017 |
| **O8** | A workspace lifecycle that cannot destroy unsaved work without the control plane being able to observe or veto it. | item 7 |
| **O9** | State readout that is not serialised behind worker load. | item 8, D-0016 |
| **O10** | No dependency edge required from `MessageBus` to the provider; delivery works with the provider's UI absent. | item 6, D-0009 |
| **O11** | A capability/version probe, with fail-closed on incompatible new spawn. | D-0010 |
| **O12** | The provider is never the source of truth; nothing durable may live only in it. | D-0001 |

Note that **O7, O10 and O12 are Interlock's obligations regardless of provider**, which is precisely
why `D-0019` can claim the control-plane design survives a gate failure. They appear in the matrix to
show where a backend makes them harder, not to score the backend.

---

## 5. Q-0004 — candidates

### 5.1 A pre-filter, applied before the eleven items

Scoring a candidate that was never eligible wastes the evaluation. Three constraints look like
pre-filters rather than criteria, and each should be confirmed or dropped by a human before any
candidate is scored (Decision 7):

| Pre-filter | Why it might apply | Which candidates it removes |
|---|---|---|
| **Local execution** — worker sessions run on the operator's own machine against real local repositories. | The measured baseline is a single-machine dogfood organisation; `CHARTER.md` §2 declares agent farms a non-goal; `D-0017` caps workers at few. | C4 (cloud), C5 (Managed Agents) |
| **No server-side retention of work state** | Managed Agents stores conversation history, sandbox state and outputs server-side and is explicitly **not** eligible for ZDR or a HIPAA BAA (W8). Whether that matters is a policy question this document cannot answer. | C5 |
| **Plan/tier availability** | Self-hosted environments are public beta on Team and Enterprise, off by default, requiring an Owner to enable (W6). | C4b |

### 5.2 The candidates

**C1 — Agent View (incumbent).** The subject of the gate. Included as the baseline column.

**C2 — Interlock-supervised `claude -p` subprocesses.** No agent view, no `claude daemon`. Interlock
spawns `claude -p --output-format stream-json --session-id <uuid>` as a child process it owns
outright, and resumes with `--resume`. Interlock's own process supervision *is* the session
lifecycle. This is the candidate for which the evidence base is strongest: `--session-id` is
documented as a caller-supplied UUID (V21) with no `--bg` interaction to worry about since `--bg` is
not used; `--resume` finds a session in any project on the machine from v2.1.223 (V21); `system/init`
carries a `capabilities` array explicitly intended for feature detection instead of version
comparison (V22), which is a near-exact fit for `D-0010`'s probe; and SIGTERM semantics are documented
down to the exit code (V23).

**C3 — Claude Agent SDK.** A library for Python and TypeScript only (W1), and Interlock is Python, so
this is available. It offers session enumeration and inspection, resume and fork (W2), and the richest
permission surface of any candidate: a documented six-step evaluation order in which a hook deny
applies even under `bypassPermissions` (W3).

On the process boundary, note what the overview's "runs the agent loop in your own process" does and
does not mean (W1a). The Python reference documents a pluggable `transport` for communicating with
*the CLI process*, whose default is a local subprocess. So C3 does **not** put worker execution inside
Interlock's own interpreter — the worker is a child process, as it is under C2 — and the objection
that C3 collides with `D-0016` (Secretary must not block) or reopens `Q-0013` ("control plane outside
the worker") **does not follow** and is withdrawn. What actually distinguishes C3 from C2 is narrower:
the SDK owns spawn, transport framing and session bookkeeping, so Interlock inherits those
implementations rather than writing them, at the cost of depending on a library API whose own
reference calls the transport seam "a low-level internal API. The interface may change in future
releases." Its other documented cost stands: session files are machine-local and the docs suggest not
relying on session resume at all (W2). Against C2, C3 is therefore a real alternative rather than a
worse one — it trades code Interlock would otherwise write for a dependency surface Interlock does not
control.

**C4 — Cloud sessions and self-hosted environments.** `claude --cloud` creates and targets cloud
sessions, and `-p "msg" --cloud <id> --output-format json` queues a message and returns a structured
result — notably the **only** candidate with a documented non-interactive *ingress* to a running
session (W5, contrast F1). Self-hosted environments add a runner model that already implements a
lease with heartbeat and 60-second requeue (W6) — conceptually close to `S6`. Against: research
preview, VM reclaimed after inactivity, deletion documented only as a UI action (W5), and
`--teleport`'s preconditions (clean tree, same repo, pushed branch) make workspace lifecycle a
first-class problem rather than an incidental one.

**C5 — Managed Agents (platform API).** The only candidate with a genuinely *designed* session API: a
four-value status enum (`idle`, `running`, `rescheduling`, `terminated`), explicit archive/delete
semantics that refuse on a running session until interrupted, mid-session tool and permission-policy
replacement while idle, and documented per-organisation rate limits (W8). That is a strong fit for
O1–O5 and unusually strong for O3. Against: beta behind a dated header with no SLA or
back-compatibility commitment, server-side state that forecloses ZDR/HIPAA, and workers that no longer
run against the operator's local repositories in the way the baseline assumes.

**C6 — A generic job supervisor hosting C2.** systemd user units, a container runtime, or a
devcontainer (W7) owning process lifecycle, restart policy and resource limits, with `claude -p`
inside. This is not an alternative to C2 so much as a decision about who owns O1/O4/O5. It supplies
restart semantics and fencing for free but adds an operational surface, and a dev container is
documented as *not* an exfiltration boundary (W7), so it does not discharge O7 by itself.

**C7 — A non-Anthropic agent CLI behind the same contract.** Codex CLI is already in this
repository's own review gate, so it is operationally proven in-house. Its value is not that it is
better; it is that it is the only candidate that does not inherit the *same vendor's* stability
posture. If the gate fails because the surface is a research preview with no compatibility guarantee
(V25, V26), a same-vendor fallback carries that risk forward. Against: a different agent is a
different worker, and `D-0014`'s carried invariants say nothing about it; treating this as a drop-in
would be a category error.

**C8 — tmux / renga panes as a session lifecycle backend.** Raised explicitly because omitting it
leaves an obvious reader question unanswered. `D-0014` discards "tmux/pane layout, pane IDs and
send-keys **as a backend contract**", and `Q-0004` notes that this makes "fall back to v1's transport"
unavailable as an automatic answer. But `D-0009`'s whole move is to split session lifecycle from
message delivery — and once `MessageBus` rides its own durable channel (F1), pane spawn/list/close as
a *session lifecycle* mechanism is arguably a different question from send-keys as a *message
transport*. `CHARTER.md` §4 already draws exactly this distinction for watcher signals: "a signal and
a backend contract are different things". Whether the same distinction rescues panes for the
`SessionProvider` role is **not** decided anywhere and should not be assumed either way — it is
Decision 9.

### 5.3 Evaluation against the obligations

Legend: **Y** documented and verified; **~** partially met or met with a named caveat; **N** not
available; **?** unknown from primary sources (see Appendix A). Rows are scored on published evidence
only — an unknown is never scored as a pass.

| | C1 Agent View | C2 `claude -p` supervised | C3 Agent SDK | C4 Cloud / self-hosted | C5 Managed Agents | C7 Non-Anthropic CLI | C8 Panes |
|---|---|---|---|---|---|---|---|
| **O1** start | Y (V1) | Y | Y (W1) | Y (W5) | Y (W8) | ? | Y |
| **O2** list | Y (V3) | Y — Interlock's own roster | Y (W2) | ~ UI-centric | Y (W8) | ? | Y |
| **O3** structured state | Y (V3, V4) but no schema guarantee (V26) | Y (V22 `stream-json` + `capabilities`) | Y (W2) | ~ (W5 json only for queueing) | **Y** — designed status enum (W8) | ? | N — screen text only |
| **O4** stop one session | Y (V5) | Y — signal, semantics documented (V23) | Y — SDK owns the child process (W1a) | ~ (W5, UI delete) | Y (W8, interrupt-then-act) | ? | Y |
| **O5** resume | Y (V6 `respawn`) | Y (V21 `--resume`) | Y (W2) — but docs advise not relying on it | ~ (W5 `--teleport` preconditions) | Y (W8 idle→running) | ? | ~ |
| **O6** identity across crash window | **?** — the decisive unknown (F3/U1) | **Y** — caller-supplied UUID before spawn (V21) | Y (W2 session_id) | ~ | Y (server-assigned, durable) | ? | N — pane IDs are not durable identity |
| **O7** per-role fence, fail closed | ~ persists (V13); fail-closed **not** promised, evidence of fail-open (V15, V16) | ~ same harness; but Interlock owns the spawn precondition end to end | ~ best surface (W3) yet per-role override void under three modes (W4) | ? | ~ mid-session policy replacement (W8), idle-only | ? | ~ same as C1/C2 |
| **O8** workspace lifecycle | ~ documented, partly contradictory (V11, F5); `WorktreeRemove` is the veto handle (V12) | **Y** — Interlock owns the working tree; no provider reclaims it | Y — same | N — VM reclaimed (W5); untracked files excluded from bundling | ~ delete destroys sandbox files (W8) | ? | Y |
| **O9** readout not serialised | ? (U6) | Y — reading Interlock's own rows | Y — worker is a child process (W1a) | ? | ~ read endpoints rate-limited, not blocked (W8) | ? | ~ |
| **O10** bus independent of provider | Y by force (F1 — no ingress exists) | Y | ~ the SDK's own transport seam invites coupling (W1a) | ~ has an ingress (W5), which is a temptation not a requirement | ~ same | ? | Y |
| **O11** capability probe | ~ `--version` only; no probe on this surface (U7) | **Y** (V22) | ~ SDK version; transport seam self-described as internal and changeable (W1a) | ? | ~ dated beta header | ? | N |
| **O12** provider not SoT | Y — Interlock's obligation | Y | Y | ~ server-side state (W5) | **N-ish** — state is server-side by design (W8) | Y | Y |

**Reading the matrix.** Two things stand out and neither is a recommendation.

- **C2 scores best on the obligations that are hardest to work around** — O6 (identity chosen before
  spawn), O8 (nobody else owns the working tree), O11 (a real capability probe). Those are exactly
  the three where C1's position is weakest or unknown. C2 is also the most `D-0010`-consistent
  candidate, since every fact above comes from documented flags rather than from `--help` archaeology.
  Its cost is that Interlock must write the process supervision C1 supplies for free.
- **C1's unknowns cluster on O6, O9 and O11** — not on the verbs. The five verbs are all present and
  documented. If the gate fails, the most likely reason on current evidence is identity across the
  crash window or the absence of a capability probe, not an inability to start and stop workers.

### 5.4 What is still missing before a choice can be made

A choice made now would be made on these gaps, so they are listed rather than filled.

1. **U1** — whether a background session's identity can be chosen before spawn. Decides O6 for C1 and
   is a one-command experiment (§3.5 phase 0).
2. **Interlock's own `SessionProvider` interface** (§2). Until it exists, "the same contract" in gate
   item 11 has no referent and candidates cannot be compared on equal terms.
3. **Whether O7's fail-closed half is achievable in front of *any* of these backends** — F2 argues it
   must be Interlock's, which if accepted removes O7 from the comparison entirely and simplifies the
   choice.
4. **The pre-filter** (§5.1). If local execution is mandatory, C4 and C5 leave the table before
   scoring and the real choice is C1 vs C2 vs C8.
5. **Churn evidence.** `D-0010`'s context cites a measured cadence of roughly one CC release every
   four days. No comparable figure exists for any candidate's *interface* stability, and V26 records
   that no compatibility guarantee is published for the surface the gate depends on. Without this, the
   candidates' churn risk is unverified and must be labelled so.
6. **Destination idempotency keys** for external effects (`ACCEPTANCE.md` §2). No candidate's
   documentation was found to state whether state-changing endpoints honour a client-supplied key. If
   none do, item 5's external-effect clause falls entirely to Interlock-side transactional design, and
   that limitation belongs in the gate record.

### 5.5 Proposed selection criteria

Offered as the criteria to apply, in this order, not as a choice:

1. **Eligibility** — survives the §5.1 pre-filter.
2. **O6 and O8 first.** Identity across the crash window and workspace-lifecycle safety are the two
   obligations where a backend's answer cannot be patched from the control-plane side. Everything else
   can be compensated for at cost; these two cannot.
3. **O11 second.** A capability probe is what makes `D-0010`'s fail-closed rule executable. Without
   one, "fail closed on incompatibility" degrades to a version allowlist, which should be stated
   explicitly rather than discovered.
4. **Independence of vendor stability posture** as a tiebreak, on the reasoning that a same-vendor
   fallback inherits the risk that triggered the fallback (C7's only real argument).
5. **Cost of the swap**, measured concretely as the number of tests in the control-plane suite that
   need modification — which is gate item 11's own metric, and which only becomes measurable once the
   suite exists.

---

## 6. Decisions requested

Each item is a decision for a human. A recommendation is given for each, with its reason; none has
been acted on.

---

**Decision 1 — Which Q-0021 strategy?**

| Option | Summary |
|---|---|
| A | Probe-first: S4 only, verdict from T1, everything else deferred (3–5 d) |
| **B+** | Minimum vertical slice, with S1 written first and S3 implemented before S2 (12–16 d) |
| B | Minimum vertical slice in natural order (10–15 d) |
| C | Contract-first dual-provider throughout (15–22 d) |

**Recommendation: B+.** It is the only option that discharges the gate `D-0019` calls a precondition
while keeping the second provider's structural benefit. A is cheaper but leaves seven items open at
implementation start, which reads `D-0019` as advisory. C front-loads an interface designed before any
provider has informed it. B+ costs roughly 1–2 days more than B for a property gate item 11 requires
anyway.

---

**Decision 2 — Is "write the `SessionProvider` interface" a scaffold artifact or a design decision?**

Gate item 11 cannot be assessed until the contract exists in writing (§2), and three capabilities the
gate leans on are unassigned to either contract.

| Option | Summary |
|---|---|
| **2a** | Treat S1 as spike scaffold: written during the spike, explicitly provisional, promoted later by a `D-` entry |
| 2b | Settle the contract as a `D-` decision *before* the spike |
| 2c | Leave it implicit and let S2 define it de facto |

**Recommendation: 2a.** 2c is how session-backend detail leaks into the control plane, which is the
exact failure gate item 11 exists to catch. 2b designs the contract before the provider has taught
anyone what it must express. 2a gets it written down early enough to be useful and marks it
provisional so it does not answer `Q-0001`-adjacent questions by inertia.

---

**Decision 3 — Which items must the Agent View spike discharge, and which are deferred?**

Item 9 has no session-backend dependency at all and is absent from `ACCEPTANCE.md` §4's re-run list.
Item 10 needs v1 as a live counterparty and is realistically proven *at* the canary.

| Option | Summary |
|---|---|
| **3a** | Spike discharges T1 + T2 (items 1–7, 11); item 9 runs in parallel, independently; item 8 as a stub-Secretary rehearsal; item 10 as a rehearsal with the real canary re-proven later — with "proven on the spike slice" vs "re-proven on the real implementation" recorded per item |
| 3b | Spike discharges all eleven, deferring implementation until item 10 has a real counterparty |
| 3c | Formally reclassify 8, 9, 10 out of the pre-implementation gate |

**Recommendation: 3a.** 3b blocks implementation on the canary, which inverts the ordering the Issue
gives. 3c weakens a gate placed deliberately, which `Q-0021` itself warns against. 3a keeps all eleven
in force while being honest that two are rehearsed rather than concluded — and it is the shape
`Q-0021` says a resolving decision would take.

---

**Decision 4 — How is gate item 3's observable defined, given no public config readback exists?**

`ACCEPTANCE.md` proposes diffing the effective configuration before and after restart. No public
surface returns it (Appendix A/U3).

| Option | Summary |
|---|---|
| **4a** | Behavioural breach probe as the observable — attempt one forbidden operation per role, assert denial — with the declarative diff run against Interlock's own rendered inputs |
| 4b | Read internal state to obtain the effective configuration |
| 4c | Treat item 3 as unprovable on this provider and fail the gate on it |

**Recommendation: 4a.** 4b violates `D-0010` outright. 4c fails the gate on a measurement problem
rather than a capability problem. 4a is also the stronger test: it asserts the fence *holds*, not that
a config file was reloaded, and the sandbox breach probe it builds on is already `carry` material
(R5).

---

**Decision 5 — Where does fail-closed live?**

Nothing documented promises fail-closed on missing or corrupt configuration, and V15/V16 are evidence
of fail-open (F2).

| Option | Summary |
|---|---|
| **5a** | Interlock owns it: validate the rendered per-role configuration and refuse to spawn, plus a `PreToolUse` deny hook as in-session backstop |
| 5b | Treat the absence of a fail-closed guarantee as a gate item 3 failure and go to `Q-0004` |
| 5c | Accept the harness's behaviour and record `D-0017`'s fail-closed clause as unmet |

**Recommendation: 5a.** The obligation is Interlock's under `D-0017` regardless of provider, so
building it is not provider-specific work and is not wasted under any `Q-0004` outcome. 5b would fail
the backend for something no candidate promises. 5c contradicts `D-0017` and `CHARTER.md` §3.4.

---

**Decision 6 — Do we design for pre-assigned session identity, or for post-hoc adoption?**

U1 is unresolved and is the riskiest single unknown (F3).

| Option | Summary |
|---|---|
| **6a** | Run the experiment first (phase 0, on a real `--bg` session including the UUID-collision case); if it succeeds, commit the binding before spawn. If it fails, search for any other **pre-spawn** fence, and if none exists, **fail item 2 and open the `Q-0004` path** rather than substituting post-hoc adoption (F6) |
| 6b | Assume pre-assignment works and design on it |
| 6c | Assume it does not and design the harder path unconditionally |

**Recommendation: 6a.** The experiment costs two short sessions and decides a design. 6b builds on an
untested assumption in the one place where being wrong is most expensive; 6c pays for a path that, per
F6, does not actually satisfy the item it is meant to satisfy. The part of 6a that matters is its tail:
a negative result must be allowed to fail the gate, because an adoption rule that picks a winner
without proving the loser never wrote is a reclassification of item 2 wearing the clothes of a
mitigation.

---

**Decision 7 — Which pre-filters apply to `Q-0004` candidates?**

| Option | Summary |
|---|---|
| **7a** | Local execution is mandatory — removes C4 and C5 before scoring |
| 7b | No pre-filter; score everything against the eleven items |
| 7c | Local execution mandatory *and* no server-side retention of work state |

**Recommendation: 7a.** The measured baseline is a single-machine dogfood organisation, `D-0017` caps
workers at few, and `CHARTER.md` §2 rules out farms — so a cloud-hosted worker fleet is a different
system, not a fallback for this one. 7c adds a policy judgement (ZDR/HIPAA) that only the operator can
make and that 7a already renders moot. 7b spends evaluation effort on candidates that would not be
adopted.

---

**Decision 8 — Which candidate is the designated second spike if the gate fails?**

| Option | Summary |
|---|---|
| **8a** | C2 — Interlock-supervised `claude -p` subprocesses |
| 8b | C3 — Agent SDK |
| 8c | C8 — panes as session lifecycle only |
| 8d | Name none; re-evaluate on the failure's specifics |

**Recommendation: 8a.** C2 scores best on precisely the obligations that cannot be compensated for
from the control-plane side (O6, O8, O11), it is the most `D-0010`-consistent candidate — every fact
about it comes from documented flags rather than from undocumented subcommands — and it is the only
candidate whose capability probe (V22) matches what `D-0010` asks for. 8b is a genuine second choice
rather than a weak one — the earlier objection that it runs the worker inside the control plane's own
process does not survive W1a and has been withdrawn — but it trades code Interlock would write once
for a dependency whose own reference calls the transport seam internal and subject to change, which is
the same class of risk that produced this gate. 8d is defensible but leaves the gate's "only the
`SessionProvider` is replaced" promise without a concrete referent, which is what `Q-0004` was opened
to fix.

---

**Decision 9 — Does `D-0014`'s discard of tmux/pane/send-keys extend to the `SessionProvider` role?**

| Option | Summary |
|---|---|
| **9a** | State that it does not extend automatically — the discard is of a *message transport* contract — but keep C8 ranked last and require a new `D-` entry to adopt it |
| 9b | State that it does extend; C8 is excluded outright |
| 9c | Leave unstated |

**Recommendation: 9a.** `CHARTER.md` §4 already makes the same distinction for watcher signals ("a
signal and a backend contract are different things"), and `D-0009`'s entire purpose is to separate
these two roles. Excluding C8 by silent extension of `D-0014` would decide by omission the exact kind
of question `Q-0022` was opened to avoid deciding by omission. 9c leaves an obvious reader question
unanswered.

---

**Decision 10 — What is the durable output of the spike?**

| Option | Summary |
|---|---|
| **10a** | Interface (S1) and tests are durable; every implementation, including S5's schema, is throwaway by default and promoted only by a `D-` entry |
| 10b | The whole slice is seed code for the implementation |
| 10c | Everything is throwaway |

**Recommendation: 10a.** This is the mitigation for Strategy B's one serious weakness. `D-0014`
already names accident-derived fixtures, fault injection and recovery tests as the things to rescue —
tests are the asset. 10b is how a spike schema silently answers `Q-0001`. 10c discards the tests the
Carry bucket explicitly wants.

---

## 7. Sources

**Local CLI**, all run 2026-08-18 against `claude` **2.1.234 (Claude Code)**, read-only:
`claude --version`, `claude --help`, `claude agents --help`, `claude agents --json`,
`claude agents --json --all`, `claude attach --help`, `claude logs --help`, `claude stop --help`,
`claude kill --help`, `claude rm --help`, `claude respawn --help`, `claude daemon --help`.

**Official documentation**, fetched 2026-08-18:

- Agent view — <https://code.claude.com/docs/en/agent-view>
- CLI reference — <https://code.claude.com/docs/en/cli-reference>
- Headless mode — <https://code.claude.com/docs/en/headless>
- Agent SDK overview — <https://code.claude.com/docs/en/agent-sdk/overview>
- Agent SDK sessions — <https://code.claude.com/docs/en/agent-sdk/sessions>
- Agent SDK permissions — <https://code.claude.com/docs/en/agent-sdk/permissions>
- Claude Code on the web — <https://code.claude.com/docs/en/claude-code-on-the-web>
- Self-hosted environments — <https://code.claude.com/docs/en/self-hosted-environments>
- Dev containers — <https://code.claude.com/docs/en/devcontainer>
- GitHub Actions — <https://code.claude.com/docs/en/github-actions>
- Managed Agents — <https://platform.claude.com/docs/en/managed-agents/overview>, `/sessions`,
  `/session-operations`, `/reference`

Note that `docs.claude.com/en/docs/claude-code/*` now issues a 301 to `code.claude.com/docs/en/*`;
the redirect targets are the URLs above.

**Repository:** `CHARTER.md`, `DECISIONS.md` (D-0001, D-0005–D-0010, D-0013, D-0014, D-0016–D-0019;
Q-0001–Q-0005, Q-0011–Q-0013, Q-0020, Q-0021), `ACCEPTANCE.md` §§1–5, `PORTING_LEDGER.md`.

---

## Appendix A — Unverified register

Nothing here is used as the basis of a recommendation. Each entry names what would settle it.

| ID | Question | Status | How to settle |
|---|---|---|---|
| **U1** | Does `--session-id <uuid>` compose with `--bg`, letting the caller choose a background session's identity before spawn? | **Unverified.** Both flags exist on 2.1.234 (V21, V1). No source states they compose. The cli-reference `--bg` entry lists `--exec` and `--agent` as combinable and `-p` as excluded; `--session-id` appears in neither list. What *is* documented is the opposite direction: the ID is printed after the fact (V2). Not tested — testing requires spawning a session. | A **real** `--bg` Claude session, not an `--exec` job: a job has no conversation and `--session-id` identifies a conversation, so a job-based probe could return a job identifier and yield a false positive (F4). Run `claude --bg --session-id <uuid> "reply with ok"`, then check `claude agents --json` for that `sessionId`; then repeat with an ID already in use to observe the collision behaviour. If the answer is no, see F6. |
| **U2** | Is `--exec` present on CLI 2.1.234? | **Unverified.** Documented on the agent-view page (V19) but absent from `claude --help` on this build. | Run `claude --bg --exec 'true'` once; if unsupported, F4's token-free half fails and all of S4 must be budgeted as model-backed sessions. |
| **U3** | Is there a public way to read back a running or resumed session's **effective** permission mode, permission/deny rules, sandbox profile, and resolved hook set as machine-readable output? | **Unverified — no such surface found.** `system/init` (V22) is documented for `-p` and reports model, tools, MCP servers and plugins, not permissions or hooks. | Search for a documented readback; if none, adopt Decision 4a. |
| **U4** | What happens to running background sessions when the supervisor dies **ungracefully** (SIGKILL, OOM, host crash), and can a restarted supervisor re-adopt them? Separately: does "the daemon runs on demand and exits when the last client disconnects" (V9) mean sessions terminate whenever no client is attached? | **Unverified.** Documentation covers graceful `claude daemon stop` and `--keep-workers` only. V8 states the supervisor restarts a session whose *process* exits unexpectedly, which is the inverse case. | Probe as part of S4, using `--exec` jobs so nothing real is at risk. |
| **U5** | What happens to **untracked** files on each session-deletion path? | **Unverified.** The agent-view page names "uncommitted changes" and "unpushed commits" as distinct cases; untracked files are never mentioned on any deletion path. A summarising fetch asserted they follow the uncommitted path; the verbatim source does not say this. (Separately verified and *not* transferable: for `--cloud` bundling, "Untracked files are not included".) | Observe directly on the running build as part of gate item 7. |
| **U6** | Does the supervisor's control interface serve status queries concurrently with busy workers, or serialise them behind worker load? Is there a documented cap on concurrent background sessions? | **Unverified.** No concurrency model is documented. | Measure with N `--exec` jobs at the intended cap while timing `claude agents --json` (part of item 8's evidence). |
| **U7** | Is there any capability probe for the background-agent/daemon surface, and what are the `capabilities` strings? | **Unverified.** The `capabilities` array is documented only for `stream-json` `system/init` in `-p` mode, from v2.1.205 (V22) — a surface `--bg` cannot use (V18). | If none exists, `D-0010`'s fail-closed rule reduces to a `claude --version` allowlist for this provider, and that reduction should be recorded rather than left implicit. |
| **U8** | Are skills, plugins and settings re-read by an already-running session when their files change on disk, or bound once at session start? | **Unverified.** Bears on gate item 9's threat model: if a running session hot-reloads skill directories, writing a file *is* promotion and the approval gate must sit at the filesystem. | Documentation search, then a direct test as part of item 9's scaffold. |
| **U9** | Do any candidate backend's state-changing endpoints honour a client-supplied idempotency key? | **Unverified.** None found for any candidate. | If none, `ACCEPTANCE.md` §2's transactional-commit option is the only route for external effects, and actions where neither is achievable need a human gate (`D-0004`). |
| **U10** | Is any stability tier, deprecation policy, or compatibility guarantee published for the local background-agent CLI surface? | **Unverified — and the absence is itself evidence.** V25 says agent view is research preview; V26 records that `--json` carries no guarantee; V5 records that five of the session subcommands are absent from top-level `--help`. | Nothing further to run; treat the surface as version-specific and gate on `claude --version`. |
| **U11** | Does the Claude Code GitHub Action produce a resumable session? | **Unverified.** The page states where output goes; it does not state that the session is non-resumable, and `claude_args` accepts any CLI argument. Absence of documentation is not documented absence. | Not pursued — the candidate is out of scope under Decision 7a. |
| **U12** | Exact SIGTERM drain semantics for self-hosted-environment runners. | **Unverified.** The page defers to a shutdown-timing section not fetched; the verified sequence covers `--retire-at`, not SIGTERM. | Fetch `self-hosted-environments-deploy#shutdown-timing` if C4 survives the pre-filter. |
