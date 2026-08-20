# Interlock

**Interlock is a durable control plane for a coding-agent organization.**
It exists so that a small organization of coding agents can be delegated to,
observed, recovered, and audited without a language model being permanently
on watch.

Three properties define it. They are decisions, not aspirations:
[`CHARTER.md`](./CHARTER.md) states them, and [`DECISIONS.md`](./DECISIONS.md)
holds the context and consequences behind every `D-00NN` cited here.

- **SQLite is the single source of truth** (D-0001). Runs, tasks, sessions,
  leases, incidents, assessments, actions and the outbox live in SQLite. AI
  context and any UI are projections. The invariant this buys — that after a
  mid-flight kill the system resumes from SQLite, from unresolved incidents,
  without double execution — is the design's obligation, not yet a
  demonstrated property: gate items 4 and 5 are the proofs, and both are
  still pending in [`docs/gate-record.md`](docs/gate-record.md).
- **Monitoring is deterministic** (D-0002). The resident LLM monitoring loop is
  retired; the program-side event loop and a low-frequency reconcile loop stay.
  Deleting every loop is explicitly *not* the goal.
- **Semantic judgement is on demand** (D-0003). The Dispatcher AI starts only
  for incidents that are semantically ambiguous. With no incident, there are
  zero AI turns.

This is not an AI agent framework. Kubernetes-style orchestration, large agent
farms, and a resident AI watcher are named non-goals (D-0015, D-0017).

## Status

**Pre-implementation.** Interlock is working through the eleven-item Agent View
gate in [`ACCEPTANCE.md`](./ACCEPTANCE.md) §1, which D-0019 makes a precondition
for feature work. The vehicle is the session-provider spike decomposed in
[`docs/plans/spike-issue-decomposition.md`](docs/plans/spike-issue-decomposition.md);
the per-item verdicts, their evidence, and the provider each was obtained
against live in [`docs/gate-record.md`](docs/gate-record.md).

The provider under test is **C2** — Interlock-supervised `claude -p`
subprocesses. Gate item 2 failed on Agent View (C1) on 2026-08-18, and C2 was
adopted in its place (D-0027, D-0025).

Slices landed so far:

| Slice | What it is |
|---|---|
| S1 | The provisional `SessionProvider` interface — `session/provider.py` (D-0021) |
| S3 | The stub provider over local child processes — `session/stub_provider.py` |
| S5 | The spike SQLite schema, marked as a spike schema — `control_plane/spike_schema.sql` |
| S6 | The lease, with the fencing token validated atomically inside each protected write — `control_plane/lease.py`, [`docs/lease-fencing.md`](docs/lease-fencing.md) |
| S7 | The outbox: resend, ack, dedup, and one handler that names its exactly-once mechanism — `control_plane/outbox.py` |
| S10 | The per-role fencing renderer, its rule-derived breach battery, and the fail-closed spawn precondition — `fencing/`, [`docs/per-role-fencing.md`](docs/per-role-fencing.md) |
| item 9 | The Curator promotion gate at the filesystem write, with digest-pinned approvals — `curator/`, [`docs/curator-promotion-gate.md`](docs/curator-promotion-gate.md) |

Per D-0026 the spike's durable output is **the interface and the tests**; the
implementations behind them are throwaway by default. The suite is the
acceptance surface, not a byproduct — read a slice's tests before its module.

**What was removed.** PR #46 purged the Discard-bucket fork residue: the tmux /
wezterm / herdr / renga terminal-adapter layer, the pane-control broker surface
built on it, the transport descriptor that derived its allowlists from that
surface, and the reference role prompts. So the `org` and `broker` CLI groups
and the `claude_org_runtime.prompts` module no longer exist.
[`PORTING_LEDGER.md`](./PORTING_LEDGER.md) records the outcome per path, and its
Purge record states what each surviving module lost.

## Lineage

Interlock is a **lineage fork** (系譜分岐) of
[`suisya-systems/claude-org-runtime@befd309`](https://github.com/suisya-systems/claude-org-runtime/commit/befd3096110d18c928793d4862dba02e4da7ea22),
base release `v0.1.42`.

- It is **not** maintained for continuous upstream tracking; individual fixes
  may be taken in, each with recorded rationale (D-0011).
- `claude-org-ja` and the runtime 0.1 line are the **v1 / maintenance line**.
  Interlock's design is not back-ported into them (D-0012).
- What is kept is history, invariants, contracts, and accident-derived
  fixtures — a selective seed port, not a parity rewrite (D-0014).

The distribution and import names are still `claude-org-runtime` /
`claude_org_runtime`, inherited from the fork base. When and how they are
renamed to Interlock naming is an open question (`Q-0008`), so nothing here is
renamed ahead of that decision.

## Working from a checkout

There is no published release of Interlock. The `claude-org-runtime`
distribution on PyPI is the v1 / maintenance line — a different codebase
(D-0012) — so installing it does **not** get you this tree.

```sh
git clone https://github.com/suisya-systems/interlock
cd interlock
python -m venv .venv && . .venv/bin/activate
python -m pip install "jsonschema>=4.18" pytest
```

`jsonschema` is the only runtime dependency and `pytest` the only test
dependency; Python 3.10+ is required.

Install the dependencies but **not** the package: this is a Python
**src-layout** project, and running against `src/` on `PYTHONPATH` is what
stops a stale install from shadowing the tree and importing older code.

```sh
PYTHONPATH=src python -m pytest
PYTHONPATH=src python -m claude_org_runtime.cli --help
```

## CLI

The console entry point carries over from the fork base, minus the groups the
purge deleted. What remains is planners and generators that read and write
files and JSON. `claude-org-runtime` below is the installed console script;
from a checkout the equivalent is
`PYTHONPATH=src python -m claude_org_runtime.cli <group> ...`.

```sh
# Render a per-role settings.local.json from the bundled role schema:
claude-org-runtime settings generate \
    --role default \
    --worker-dir /path/to/worker \
    --claude-org-path /path/to/claude-org \
    --out /path/to/worker/.claude/settings.local.json

# Compute a Dispatcher delegation action plan:
claude-org-runtime dispatcher delegate-plan \
    --task-json .state/dispatcher/inbox/<task_id>.json \
    --panes-json panes.json \
    --state-dir .state
```

Also available: `settings show`, `sandbox doctor`, `attention scan` / `watch`,
and `migrate v1-to-v2`. [`docs/cli.md`](docs/cli.md) documents flags and exit
codes for the `dispatcher`, `settings` and `sandbox` groups; `attention` and
`migrate` have no page there yet, and its own install snippet still points at
PyPI and reports version 0.1.0 — read this section, not that one, for how to
get the tree. Otherwise `--help` on any group is authoritative.

Note that `delegate-plan` still expects renga-shaped pane JSON as *input data*
while nothing in this tree produces it any more — the transport that fed it was
purged, and the contract that replaces it has not been authored yet.

## Documents

| Document | What it is for |
|---|---|
| [`CHARTER.md`](./CHARTER.md) | Purpose, non-goals, the five roles, and the responsibility boundary table. Start here. |
| [`DECISIONS.md`](./DECISIONS.md) | The canonical, append-only decisions (`D-00NN`) and the open-questions list (`Q-00NN`). Every other document cites it by ID. |
| [`ACCEPTANCE.md`](./ACCEPTANCE.md) | The Agent View gate checklist and how each item is verified, the fault-injection matrix, and the canary / rollback conditions. |
| [`PORTING_LEDGER.md`](./PORTING_LEDGER.md) | The per-path carry / rewrite / discard record against the fork base, plus the Purge record for #46. |
| [`CHANGELOG.md`](./CHANGELOG.md) | Per-slice detail, including what each slice deliberately does *not* offer. |
| [`docs/`](docs/) | The written record for individual slices, the CLI reference, and the spike plan. |

Cite decisions by ID (`D-00NN` / `Q-00NN`), never by line number or heading
order.

## License

MIT — see [LICENSE](LICENSE).
