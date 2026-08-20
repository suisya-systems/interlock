#!/usr/bin/env python3
"""I-16 item 8 rehearsal: stub Secretary intake latency, idle vs under load.

Throwaway harness for suisya-systems/interlock issue #21 (D-0026). **This is a
rehearsal, not a discharge** (D-0022): the discharge point is the real
Secretary under genuine worker load, before the canary starts, against a
threshold settled by Q-0011. No numeric pass criterion appears here — the
outputs are a recorded baseline-vs-load comparison and the re-measured U6
blocking control, nothing else.

It reuses issue #6's harness (`i01_supervisor_probe.py`) for spawning and for
the non-blocking line framing, per the design review: the load generator runs
`claude -p` workers under this process's own supervision, and
`src/claude_org_runtime/session/` is not touched or imported.

The scenario (subcommand `rehearse`):

  1. Baseline: request→response latency of the stub intake at idle.
  2. Load: N workers at the spike-slice cap (default 8) plus one long-running
     task in flight, all live `claude -p` children supervised by a 100 ms
     non-blocking sweep; plus one open incident whose consumer is parked
     awaiting a (stub) Dispatcher AI judgement for the whole window.
  3. Intake latency is re-sampled during the load window.
  4. The U6 C2 fold-in, re-measured against LIVE children: the supervisor
     thread switches to a naive blocking per-child readline while intake
     latency keeps being sampled — the serialisation cost lands in the
     supervisor, and the intake samples show whether it propagates.
  5. Children run to completion; per-child result JSON is kept and
     `total_cost_usd` summed, so the spend is recorded (C2 has no free
     surface).

Each step writes one verbatim JSON record to $I16_OUT (default ./results-i16)
and echoes it to stdout, in i01's record format.

Usage:
    PYTHONPATH=../src python3 i16_item8_rehearsal.py smoke
    PYTHONPATH=../src python3 i16_item8_rehearsal.py rehearse --workers 8
"""

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i01_supervisor_probe as i01  # noqa: E402  (the #6 harness, reused)

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
from claude_org_runtime.secretary import IntakeQueue, SecretaryIntake  # noqa: E402

OUT_DIR = os.environ.get("I16_OUT", "./results-i16")
i01.OUT_DIR = OUT_DIR  # rec() writes into this run's own directory

#: Worker prompt: long enough that the child is mid-turn for the whole
#: measurement window (first assistant event ~19 s on this machine, #6 §3.8.2).
WORKER_PROMPT = (
    "Count from 1 to 40, writing one short sentence about each number. "
    "Take your time."
)

#: The long-running task in flight: same shape, more work, so it outlives the
#: workers and is still running when they finish.
LONG_TASK_PROMPT = (
    "Count from 1 to 120, writing one short sentence about each number. "
    "Take your time."
)


def rec(step, obj):
    i01.rec(step, obj)


# --------------------------------------------------------------------------
# latency sampling and stats
# --------------------------------------------------------------------------

def sample_burst(intake, n):
    """Submit n requests, return per-request latency in ms.

    Bracketed OUTSIDE submit(): the receipt's own stamps exclude the
    receipt's construction and return, which at sub-microsecond scale is a
    material share of the response path (codex round 4).
    """
    out = []
    for _ in range(n):
        t0 = time.monotonic_ns()
        intake.submit({"kind": "status-request"})
        out.append((time.monotonic_ns() - t0) / 1e6)
    return out


def stats(flat):
    flat = sorted(flat)
    if not flat:
        return None
    return {
        "n": len(flat),
        "min_ms": round(flat[0], 6),
        "median_ms": round(flat[len(flat) // 2], 6),
        "p95_ms": round(flat[min(len(flat) - 1, int(len(flat) * 0.95))], 6),
        "max_ms": round(flat[-1], 6),
    }


# --------------------------------------------------------------------------
# child bookkeeping
# --------------------------------------------------------------------------

class Child:
    def __init__(self, label, prompt, cwd, extra_flags):
        args = ["-p", prompt, "--output-format", "stream-json", "--verbose"]
        args += extra_flags
        self.label = label
        self.p = i01.spawn(args, cwd)
        self.reader = i01.LineReader(self.p.stdout)
        # stderr is drained too (non-blockingly, in the sweep): a child that
        # fills an unread stderr pipe would stall for a reason that is neither
        # the provider's nor the intake's, corrupting the measurement.
        self.err_reader = i01.LineReader(self.p.stderr)
        self.err_lines = []
        self.lines = []

    def live(self):
        return self.p.poll() is None

    def result_row(self):
        """The last `result` event, if the child emitted one."""
        for raw in reversed(self.lines):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("type") == "result":
                return obj
        return None


def blocking_next_line(child):
    """A naive supervisor's per-child read: block until one complete line.

    Returns (ms, had_buffered_line, was_live_before, got_line). The #6 control
    ran against finished children and was inconclusive; the caller here runs
    it against live ones and records liveness with each sample, as #6 §3.7
    asked.
    """

    reader = child.reader
    was_live = child.live()
    had_buffered = b"\n" in reader.buf
    fd = reader.fd
    t0 = time.monotonic()
    got = False
    try:
        os.set_blocking(fd, True)
        while b"\n" not in reader.buf:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                reader.eof = True
                break
            if not chunk:
                reader.eof = True
                break
            reader.buf += chunk
        if b"\n" in reader.buf:
            raw, reader.buf = reader.buf.split(b"\n", 1)
            child.lines.append(raw.decode("utf-8", "replace"))
            got = True
    finally:
        os.set_blocking(fd, False)
    ms = (time.monotonic() - t0) * 1000.0
    return round(ms, 3), had_buffered, was_live, got


def _abort(children, reason):
    """Kill every child group and exit non-zero: the run is not evidence."""
    rec("abort", {"reason": reason})
    for c in children:
        if c.live():
            try:
                os.killpg(os.getpgid(c.p.pid), 9)
            except OSError:
                pass
    raise SystemExit(2)


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

def cmd_smoke(a):
    """One cheap spawn per candidate flag set: validate flags, price a spawn."""
    for label, flags in [
        ("inherited-config", []),
        ("setting-sources-project", ["--setting-sources", "project"]),
    ]:
        r = i01.run(["-p", "reply with ok", "--output-format", "json"] + flags,
                    a.cwd, timeout=300)
        cost = None
        is_error = None
        try:
            body = json.loads(r["stdout"])
            cost = body.get("total_cost_usd")
            is_error = body.get("is_error")
        except Exception:
            pass
        rec("smoke", {"label": label, "argv": r["argv"], "rc": r["rc"],
                      "dur_s": r["dur_s"], "total_cost_usd": cost,
                      "is_error": is_error,
                      "stderr": r["stderr"][:500]})


def cmd_rehearse(a):
    extra_flags = a.child_flags.split() if a.child_flags else []
    version = i01.run(["--version"], a.cwd, timeout=60)
    rec("env", {"claude_version": version["stdout"].strip(),
                "workers": a.workers, "extra_child_flags": extra_flags,
                "worker_prompt": WORKER_PROMPT,
                "long_task_prompt": LONG_TASK_PROMPT,
                "cwd": a.cwd})

    # -- the stub Secretary window, identical in both phases -----------------
    queue = IntakeQueue(capacity=100_000)
    intake = SecretaryIntake(queue)

    judgement_started = threading.Event()
    judgement_done = threading.Event()

    def incident_consumer():
        while not judgement_done.is_set():
            batch = queue.take_batch(limit=64)
            if any(i.payload.get("kind") == "incident" for i in batch
                   if isinstance(i.payload, dict)):
                judgement_started.set()
                judgement_done.wait()  # the AI judgement, in flight
                return
            time.sleep(0.01)

    # -- phase 0: baseline at idle ------------------------------------------
    # Measured before the consumer thread exists: the codex review noted that
    # a polling consumer contending during "idle" but parked during "load"
    # would tilt the comparison against the baseline.
    baseline = []
    for _ in range(a.bursts):
        baseline.extend(sample_burst(intake, a.burst_size))
        time.sleep(0.05)
    rec("baseline_idle", {"latency": stats(baseline)})

    consumer = threading.Thread(target=incident_consumer, daemon=True)
    consumer.start()

    # -- spawn the load ------------------------------------------------------
    t_spawn = time.monotonic()
    children = [Child(f"worker-{i}", WORKER_PROMPT, a.cwd, extra_flags)
                for i in range(a.workers)]
    children.append(Child("long-task", LONG_TASK_PROMPT, a.cwd, extra_flags))
    rec("spawned", {"n": len(children),
                    "spawn_all_dur_s": round(time.monotonic() - t_spawn, 3)})

    # the open incident, awaiting Dispatcher AI judgement for the whole window
    intake.submit({"kind": "incident", "awaiting": "dispatcher-ai"})
    if not judgement_started.wait(timeout=30):
        # No stall means the scenario item 8 names was never established;
        # a baseline-vs-load record without it would be a false measurement.
        _abort(children, "judgement stall not established within 30 s")

    # -- the supervisor: non-blocking sweeps, then the blocking control ------
    sweep_rows = []          # (per-child ms list, n_live) per sweep
    blocking_rows = []
    monitor_mode = {"blocking": False}
    monitor_done = threading.Event()

    def monitor():
        while not monitor_done.is_set():
            if monitor_mode["blocking"]:
                # A drainer keeps stderr flowing while this thread blocks on
                # stdout: a child that filled its stderr pipe mid-control
                # would otherwise deadlock against the blocking read.
                stop_drain = threading.Event()

                def stderr_drain():
                    while not stop_drain.is_set():
                        for c2 in list(children):
                            c2.err_lines.extend(c2.err_reader.drain())
                        time.sleep(0.1)

                drainer = threading.Thread(target=stderr_drain, daemon=True)
                drainer.start()
                try:
                    for c in list(children):
                        ms, buffered, live, got = blocking_next_line(c)
                        blocking_rows.append({
                            "child": c.label, "ms": ms,
                            "had_buffered_line": buffered,
                            "child_was_live": live, "got_line": got,
                        })
                finally:
                    stop_drain.set()
                    drainer.join(timeout=5)
                monitor_mode["blocking"] = False
                continue
            per_child = []
            n_live = 0
            for c in children:
                t0 = time.monotonic()
                c.p.poll()
                c.lines.extend(c.reader.drain())
                per_child.append((time.monotonic() - t0) * 1000.0)
                c.err_lines.extend(c.err_reader.drain())  # untimed: hygiene,
                if c.live():                              # not the readout
                    n_live += 1
            sweep_rows.append((per_child, n_live))
            time.sleep(0.1)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()

    # -- phase 1: intake latency under load, supervisor non-blocking ---------
    time.sleep(a.warmup_s)  # let every child get properly mid-turn
    live_at_start = sum(1 for c in children if c.live())
    if live_at_start != len(children):
        # A child that died in warmup (auth, CLI, provider error) leaves the
        # run below the stated cap; measuring anyway would record the wrong
        # load scenario as if it were the required one.
        _abort(children, f"only {live_at_start}/{len(children)} children "
                         "live after warmup; load precondition not met")
    load_nb = []
    for _ in range(a.bursts):
        load_nb.extend(sample_burst(intake, a.burst_size))
        time.sleep(0.05)
    live_at_end = sum(1 for c in children if c.live())
    rec("under_load_nonblocking_supervisor", {
        "latency": stats(load_nb),
        "children_live_at_start": live_at_start,
        "children_live_at_end": live_at_end,
        # False marks the record invalid as evidence: the cap was not held
        # for the whole sampling window.
        "load_precondition_met": live_at_start == live_at_end == len(children),
        "judgement_in_flight": judgement_started.is_set()
                               and not judgement_done.is_set(),
    })

    # -- phase 2: the U6 fold-in — blocking control against live children ----
    live_before = sum(1 for c in children if c.live())
    monitor_mode["blocking"] = True
    load_blk = []
    t_blk0 = time.monotonic()
    while monitor_mode["blocking"]:
        load_blk.extend(sample_burst(intake, a.burst_size))
        time.sleep(0.05)
        if time.monotonic() - t_blk0 > 600:
            # A hung provider must not leave a partial record that looks like
            # completed evidence: mark it invalid and abort the run.
            rec("under_load_blocking_supervisor", {
                "invalid": True,
                "reason": "blocking control timed out (>600 s)",
                "blocking_control_rows_partial": blocking_rows,
            })
            monitor_done.set()
            _abort(children, "blocking control timed out (>600 s)")
    blocking_total_s = time.monotonic() - t_blk0
    rec("under_load_blocking_supervisor", {
        "latency_while_supervisor_blocked": stats(load_blk),
        "children_live_before_control": live_before,
        "blocking_control_total_s": round(blocking_total_s, 3),
        "blocking_control_rows": blocking_rows,
    })

    # -- run every child to completion; record what it cost ------------------
    deadline = time.monotonic() + a.drain_timeout_s
    while time.monotonic() < deadline and any(c.live() for c in children):
        time.sleep(0.5)
    monitor_done.set()
    mon.join(timeout=30)

    results = []
    total_cost = 0.0
    for c in children:
        if c.live():
            try:
                os.killpg(os.getpgid(c.p.pid), 9)
            except OSError:
                pass
        try:
            c.p.wait(timeout=30)
        except Exception:
            pass
        c.lines.extend(c.reader.drain())
        tail = c.reader.rest()
        if tail.strip():
            c.lines.append(tail)
        c.err_lines.extend(c.err_reader.drain())
        row = c.result_row() or {}
        cost = row.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            total_cost += cost
        results.append({
            "child": c.label, "rc": c.p.returncode,
            "is_error": row.get("is_error"),
            "terminal_reason": row.get("terminal_reason"),
            "num_turns": row.get("num_turns"),
            "total_cost_usd": cost,
            "n_lines": len(c.lines),
            "n_stderr_lines": len(c.err_lines),
        })
    rec("children_final", {"results": results,
                           "total_cost_usd_sum": round(total_cost, 4)})

    # -- sweep stats over the whole run --------------------------------------
    live_sweeps = [row for row, n_live in sweep_rows if n_live == len(children)]
    rec("nonblocking_sweep", {
        "sweeps_total": len(sweep_rows),
        "sweeps_all_children_live": len(live_sweeps),
        "per_child_ms_all_live": stats([x for row in live_sweeps for x in row]),
        "whole_sweep_ms_all_live": stats([sum(row) for row in live_sweeps]),
    })

    # -- teardown ------------------------------------------------------------
    judgement_done.set()
    consumer.join(timeout=30)
    rec("intake_final", {"refusals": len(intake.refusals()),
                         "queue_depth_left": queue.depth()})


def main():
    ap = argparse.ArgumentParser(description="I-16 item 8 rehearsal harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("smoke", help="validate child flags, price one spawn")
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_smoke)

    p = sub.add_parser("rehearse", help="baseline vs load, blocking control")
    p.add_argument("--cwd", required=True)
    p.add_argument("--workers", type=int, default=8,
                   help="spike-slice worker cap (default 8, matching #6's sweep)")
    p.add_argument("--bursts", type=int, default=20)
    p.add_argument("--burst-size", type=int, default=20)
    p.add_argument("--warmup-s", type=float, default=3.0)
    p.add_argument("--drain-timeout-s", type=float, default=420.0)
    p.add_argument("--child-flags", default="",
                   help="extra flags for every child, space separated")
    p.set_defaults(fn=cmd_rehearse)

    a = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    main()
