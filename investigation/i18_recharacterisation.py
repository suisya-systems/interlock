"""Issue #18 unmediated characterisation: re-measure U27 and reproduce U32.

This is the layer where two concurrent processes on one session id are
*allowed* to appear, because that is the fact being measured. It drives the
real ``claude`` CLI directly -- Interlock deliberately out of the path -- in a
scratch working directory that never touches a real run's session. Nothing
here is imported by any test: the mediated CI proof (tests/gate_item2/,
tests/fault_injection/) takes no timing figure from this file (U34: the
window's width is not a provider constant and must not be designed on).

Method (replicating investigation/pre-spawn-fence-search.md section 4):

- U27 re-run: for each trial, one fresh UUID and two ``claude -p --session-id
  <uuid> "reply with ok"`` processes released from a common barrier; record
  both exit codes, both reported session ids, and whether the shared
  transcript carries both writers.
- U27 window sweep: a long-running holder plus a second claimant at increasing
  stagger, until the refusal appears; records where the edge lies on THIS
  machine, today.
- U32 re-run: establish a session past the admission window, SIGKILL its
  process group, then run two concurrent ``claude -p --resume <uuid>`` and
  record that neither is refused.

Usage: python3 investigation/i18_recharacterisation.py [--trials 3] [--out results.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

PROMPT = "reply with ok"
LONG_PROMPT = (
    "count slowly from 1 to 40, one number per line, thinking carefully "
    "about each"
)


def _run_claude(
    argv: list[str], cwd: Path, timeout: float = 120.0
) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            start_new_session=True,
        )
        duration = time.monotonic() - started
        stdout = completed.stdout
        session_id = None
        try:
            payload = json.loads(stdout)
            session_id = payload.get("session_id")
        except (json.JSONDecodeError, AttributeError):
            match = re.search(r'"session_id"\s*:\s*"([0-9a-f-]+)"', stdout)
            if match:
                session_id = match.group(1)
        return {
            "argv": argv,
            "returncode": completed.returncode,
            "duration_s": round(duration, 2),
            "session_id": session_id,
            "stderr_tail": completed.stderr[-500:],
        }
    except subprocess.TimeoutExpired:
        return {
            "argv": argv,
            "returncode": None,
            "duration_s": round(time.monotonic() - started, 2),
            "session_id": None,
            "stderr_tail": "TIMEOUT",
        }


def _claim_argv(claude: str, session_uuid: str, prompt: str = PROMPT) -> list[str]:
    return [claude, "-p", prompt, "--output-format", "json", "--session-id", session_uuid]


def _resume_argv(claude: str, session_uuid: str, prompt: str = PROMPT) -> list[str]:
    return [claude, "--resume", session_uuid, "-p", prompt, "--output-format", "json"]


def _transcript_path(cwd: Path, session_uuid: str) -> Path | None:
    projects = Path.home() / ".claude" / "projects"
    slug = str(cwd).replace("/", "-")
    candidate = projects / slug / f"{session_uuid}.jsonl"
    return candidate if candidate.exists() else None


def _transcript_shape(cwd: Path, session_uuid: str) -> dict:
    path = _transcript_path(cwd, session_uuid)
    if path is None:
        return {"found": False}
    user_turns = 0
    assistant_turns = 0
    session_ids = set()
    lines = 0
    for line in path.read_text().splitlines():
        lines += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("sessionId"):
            session_ids.add(event["sessionId"])
        kind = event.get("type")
        if kind == "user":
            user_turns += 1
        elif kind == "assistant":
            assistant_turns += 1
    return {
        "found": True,
        "lines": lines,
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "distinct_session_ids": sorted(session_ids),
    }


def u27_simultaneous(claude: str, cwd: Path, trials: int) -> list[dict]:
    results = []
    for trial in range(trials):
        session_uuid = str(uuid.uuid4())
        barrier = threading.Barrier(2)
        answers: list[dict | None] = [None, None]

        def racer(slot: int) -> None:
            argv = _claim_argv(claude, session_uuid)
            barrier.wait()
            answers[slot] = _run_claude(argv, cwd)

        threads = [threading.Thread(target=racer, args=(slot,)) for slot in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        results.append(
            {
                "trial": trial + 1,
                "uuid": session_uuid,
                "racers": answers,
                "both_admitted": all(
                    answer and answer["returncode"] == 0 for answer in answers
                ),
                "transcript": _transcript_shape(cwd, session_uuid),
            }
        )
        print(f"U27 trial {trial + 1}: both_admitted={results[-1]['both_admitted']}", flush=True)
    return results


def u27_window_sweep(claude: str, cwd: Path, staggers: list[float]) -> list[dict]:
    results = []
    for stagger in staggers:
        session_uuid = str(uuid.uuid4())
        holder_answer: dict | None = None

        def holder() -> None:
            nonlocal holder_answer
            holder_answer = _run_claude(
                _claim_argv(claude, session_uuid, LONG_PROMPT), cwd, timeout=180.0
            )

        thread = threading.Thread(target=holder)
        thread.start()
        time.sleep(stagger)
        claimant = _run_claude(_claim_argv(claude, session_uuid), cwd)
        thread.join()
        outcome = "admitted" if claimant["returncode"] == 0 else "refused"
        results.append(
            {
                "stagger_s": stagger,
                "claimant": claimant,
                "holder": holder_answer,
                "outcome": outcome,
            }
        )
        print(f"U27 sweep stagger={stagger}s -> {outcome} ({claimant['duration_s']}s)", flush=True)
    return results


def u32_concurrent_resume(claude: str, cwd: Path) -> dict:
    session_uuid = str(uuid.uuid4())
    holder = subprocess.Popen(
        _claim_argv(claude, session_uuid, LONG_PROMPT),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(10.0)  # deliberately past the measured window's ballpark (U28's method)
    os.killpg(holder.pid, signal.SIGKILL)
    holder.wait()
    print(f"U32: holder for {session_uuid} SIGKILLed after 10s", flush=True)

    barrier = threading.Barrier(2)
    answers: list[dict | None] = [None, None]

    def resumer(slot: int) -> None:
        argv = _resume_argv(claude, session_uuid, f"resume probe {slot}")
        barrier.wait()
        answers[slot] = _run_claude(argv, cwd, timeout=180.0)

    threads = [threading.Thread(target=resumer, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    both_admitted = all(answer and answer["returncode"] == 0 for answer in answers)
    print(f"U32: both_resumes_admitted={both_admitted}", flush=True)
    return {
        "uuid": session_uuid,
        "resumers": answers,
        "both_admitted": both_admitted,
        "transcript": _transcript_shape(cwd, session_uuid),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--staggers", default="0.5,1.0,2.0,3.0,5.0",
        help="comma-separated stagger seconds for the window sweep",
    )
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="i18-characterisation-"))
    version = subprocess.run(
        [args.claude, "--version"], capture_output=True, text=True
    ).stdout.strip()
    print(f"CLI: {version}; scratch cwd: {scratch}", flush=True)

    report = {
        "cli_version": version,
        "machine": os.uname().nodename,
        "scratch_cwd": str(scratch),
        "u27_simultaneous": u27_simultaneous(args.claude, scratch, args.trials),
        "u27_window_sweep": u27_window_sweep(
            args.claude, scratch, [float(s) for s in args.staggers.split(",")]
        ),
        "u32_concurrent_resume": u32_concurrent_resume(args.claude, scratch),
    }
    out = args.out or str(scratch / "results.json")
    Path(out).write_text(json.dumps(report, indent=2))
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
