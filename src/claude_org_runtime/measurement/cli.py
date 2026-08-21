"""``claude-org-runtime measure report`` -- the harness's only entry point.

Mounted into the top-level CLI by :mod:`claude_org_runtime.cli`, and runnable as
``python -m claude_org_runtime.measurement.cli`` for direct testing.

Three failures shape this module, and each one is closed by a mechanism rather
than by a rule an operator has to remember.

**1. A report tool that writes.** ``measurement-harness.md`` section 1 records
where this comes from: v1's ``tools/org_metrics_report.py`` documents that the
ordinary connect helper applies ``journal_mode=WAL`` and "would happily run
forward migrations", both of which are writes -- so a report run against
production mutated production. This module therefore imports
:func:`~.reader.open_for_measurement` and **nothing else that opens a database**:
it does not import :mod:`sqlite3`, does not know the URI shape, and has no path
that could ask for a writable handle. ``ACCEPTANCE.md`` section 3 condition 5
asks for read-only by capability, and a CLI that could construct a writable
connection has a convention instead.

**2. A clock read below the boundary.** ``time-base-policy.md`` section 2 rule 2
puts the clock in the caller's hands. It is read **once**, in :func:`run`, and
injected downwards; every function under it takes ``now_ms``. A second read would
put two instants in one report -- the cohort selected at one, the provenance
header stamped at another -- and the report would name neither.
:func:`_require_epoch_ms` holds the same line ``migrator._require_epoch_ms``
holds, ``bool`` exclusion included, because ``--now-ms`` reaches it from a string
an operator typed.

**3. A per-report declaration silently defaulted.** The grace value, the v1
shadow input, the labelled corpus and the fingerprint mode are declared per
report (sections 3.5, 3.3, 6). Each is an explicit argument here. Where one can
be derived -- grace from the policy revision's reconcile period -- the derivation
is stamped as its source in the report rather than presented as a declaration,
and where one cannot be derived, its absence is stated in words that travel in
the rendered output.

**ASCII only.** Every string in this file, ``argparse`` help text included,
reaches ``--help`` on a cp932 console. A single em-dash there is a
``UnicodeEncodeError`` that kills the process, and ``pytest``'s
``redirect_stdout`` captures UTF-8 and cannot see it, so
``tests/measurement/test_render.py`` encodes every help string to cp932 and a
subprocess runs ``--help`` for real.

**No verdict.** ``Q-0005`` is open (section 7). This command prints measurements
and returns 0 when it produced a report; the exit code is "the report was
produced", never "the numbers were acceptable".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from .fixtures import load_corpus
from .provenance import (
    FINGERPRINT_CONTENT,
    FINGERPRINT_MODES,
    FixtureSuiteRef,
    fixture_suite_ref,
)
from .reader import ControlPlaneRefusal, open_for_measurement
from .render import (
    MARKDOWN,
    RENDERINGS,
    V1ShadowInput,
    build_measurement_report,
    render,
)

__all__ = [
    "NO_CORPUS_REASON",
    "NO_SHADOW_REASON",
    "add_arguments",
    "add_subparsers",
    "build_report_from_args",
    "main",
    "run",
]


#: Stated in the report when no labelled corpus was named. ``FixtureSuiteRef``
#: refuses an unexplained absence for the reason this sentence exists: a missing
#: corpus reference reads as a report that forgot to record one.
NO_CORPUS_REASON = (
    "no labelled corpus was named on the command line (--fixture-corpus), and "
    "this report measures no recall figure that one would qualify"
)

#: Stated in the report when no v1 shadow input was named. D-0013 leaves no
#: v1-owned run in this database, so an empty v1_owned bucket with no note is a
#: claim about v1 that this database cannot support.
NO_SHADOW_REASON = (
    "no v1 shadow input was named on the command line (--v1-shadow-run-ids), so "
    "the v1_owned exclusion bucket is empty for want of an input rather than "
    "because v1 owned no run in this period"
)

# ASCII only: these reach --help on a cp932 console.
_DB_HELP = (
    "path to the production control plane database. Opened read-only by "
    "capability (mode=ro plus PRAGMA query_only) and never migrated."
)
_PERIOD_START_HELP = (
    "start of the report period, epoch milliseconds, inclusive."
)
_PERIOD_END_HELP = (
    "end of the report period, epoch milliseconds, exclusive. The period is "
    "half-open [start, end)."
)
_NOW_HELP = (
    "the clock, epoch milliseconds, stamped as generated_at_ms and used to "
    "check the period has closed. Read once from the system clock when "
    "omitted; nothing below this command reads a clock."
)
_FINGERPRINT_HELP = (
    "database fingerprint mode. 'content' (default) hashes the ordered rows of "
    "every table read and establishes identity of content. 'aggregate' is the "
    "weaker form: it hashes counts and maxima only, it does NOT establish "
    "identity of content (an in-place UPDATE moves no count), and a report made "
    "with it is stamped as such in both renderings."
)
_GRACE_HELP = (
    "observation-window grace in milliseconds, declared for this report. "
    "Omitted, it is resolved from the policy revision in force as one reconcile "
    "period and the report records that this is where it came from."
)
_SHADOW_HELP = (
    "path to a JSON file holding the v1 shadow input: a list of v1-owned run "
    "ids, or an object with a 'run_ids' list. Those runs are excluded from the "
    "AC-9 cohort as v1_owned. Omitted, the report states that it had no shadow "
    "input rather than reporting an empty bucket unexplained."
)
_CORPUS_HELP = (
    "path to the labelled fixture corpus root, recorded in the header as "
    "fixture_suite_ref. Requires --fixture-commit."
)
_COMMIT_HELP = (
    "commit of the checkout the labelled corpus came from. Not derived: a "
    "commit read from whatever tree this process runs in would name the wrong "
    "cases."
)
_FORMAT_HELP = (
    "rendering to write. Both carry the same facts, including the section 6 "
    "provenance header; 'markdown' also carries the human narrative as fenced "
    "blocks and 'json' as string fields."
)


def _require_epoch_ms(now_ms: int) -> None:
    """Reject a clock value that is not an integer count of milliseconds.

    The same guard ``migrator._require_epoch_ms`` applies to a write, applied
    here to a read for the same reason: ``bool`` is an ``int`` in Python, so
    ``now_ms=True`` is the instant 1 ms after the epoch and every period check
    downstream would compare against it without complaint.
    """

    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise TypeError(
            f"now_ms must be an int of epoch milliseconds, got "
            f"{type(now_ms).__name__}; the clock is read once at this boundary "
            "and injected, never read again below it"
        )


def _read_shadow_run_ids(path: Path) -> tuple[str, ...]:
    """The v1 run ids in *path*, as a list or under a ``run_ids`` key.

    Both shapes are accepted and neither is guessed at: anything else refuses,
    because a file this function could not read as run ids would otherwise become
    an empty shadow input, which is the flattering answer arriving as absent data.
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("run_ids")
    if not isinstance(payload, list) or not all(
        isinstance(item, str) for item in payload
    ):
        raise ControlPlaneRefusal(
            f"{path} does not hold the v1 shadow input: expected a JSON list of "
            "run id strings, or an object with a 'run_ids' list of them"
        )
    return tuple(payload)


def _fixture_suite(args: argparse.Namespace) -> FixtureSuiteRef:
    corpus = getattr(args, "fixture_corpus", None)
    commit = getattr(args, "fixture_commit", None)
    if corpus is None and commit is None:
        return FixtureSuiteRef.absent(NO_CORPUS_REASON)
    if corpus is None or commit is None:
        # Half a reference is worse than none: a commit with no corpus names a
        # tree nothing was read from, and a corpus with no commit names cases
        # nobody can find again.
        raise ControlPlaneRefusal(
            "--fixture-corpus and --fixture-commit are given together or not at "
            "all; a corpus without its commit cannot be found again, and a "
            "commit without a corpus names a tree this report read nothing from"
        )
    return fixture_suite_ref(load_corpus(Path(corpus)), commit=str(commit))


def _shadow_input(args: argparse.Namespace) -> V1ShadowInput:
    path = getattr(args, "v1_shadow_run_ids", None)
    if path is None:
        return V1ShadowInput.absent(NO_SHADOW_REASON)
    target = Path(path)
    return V1ShadowInput.observed(str(target), _read_shadow_run_ids(target))


def build_report_from_args(args: argparse.Namespace, *, now_ms: int):
    """Open the database read-only, build the report, close the handle.

    *now_ms* is a required keyword and is never defaulted here: this function is
    below the boundary, and the boundary is :func:`run`.

    The connection comes from :func:`~.reader.open_for_measurement` and from
    nowhere else. That is the whole of condition 5's enforcement in this command:
    there is no other opener imported, so there is no code path -- including an
    error path -- on which this process holds a handle that can write.
    """

    _require_epoch_ms(now_ms)
    connection = open_for_measurement(args.db)
    try:
        return build_measurement_report(
            connection,
            db_path=str(args.db),
            period_start_ms=args.period_start_ms,
            period_end_ms=args.period_end_ms,
            now_ms=now_ms,
            fixture_suite=_fixture_suite(args),
            v1_shadow=_shadow_input(args),
            grace_ms=args.grace_ms,
            fingerprint_mode=args.fingerprint,
        )
    finally:
        connection.close()


def run(args: argparse.Namespace) -> int:
    """The clock boundary: read it once here, inject it, render, write.

    Returns 0 when a report was produced. That is a statement about this process
    and not about the numbers in the report -- ``Q-0005`` is open, and an exit
    code that meant "acceptable" would answer it (module docstring).
    """

    now_ms = args.now_ms
    if now_ms is None:
        # The only clock read in the harness. Everything below takes it as an
        # argument, so a report cannot be stamped at one instant and selected at
        # another.
        now_ms = int(time.time() * 1000)
    report = build_report_from_args(args, now_ms=now_ms)
    sys.stdout.write(render(report, args.format))
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Mount the ``report`` flags. Every per-report declaration is explicit."""

    parser.add_argument("--db", required=True, help=_DB_HELP)
    parser.add_argument(
        "--period-start-ms", type=int, required=True, help=_PERIOD_START_HELP
    )
    parser.add_argument(
        "--period-end-ms", type=int, required=True, help=_PERIOD_END_HELP
    )
    parser.add_argument("--now-ms", type=int, default=None, help=_NOW_HELP)
    parser.add_argument(
        "--fingerprint",
        choices=list(FINGERPRINT_MODES),
        default=FINGERPRINT_CONTENT,
        help=_FINGERPRINT_HELP,
    )
    parser.add_argument("--grace-ms", type=int, default=None, help=_GRACE_HELP)
    parser.add_argument(
        "--v1-shadow-run-ids", default=None, help=_SHADOW_HELP
    )
    parser.add_argument("--fixture-corpus", default=None, help=_CORPUS_HELP)
    parser.add_argument("--fixture-commit", default=None, help=_COMMIT_HELP)
    parser.add_argument(
        "--format",
        choices=list(RENDERINGS),
        default=MARKDOWN,
        help=_FORMAT_HELP,
    )
    parser.set_defaults(func=run)


def add_subparsers(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """Mount ``report`` under the caller's ``measure`` subparser."""

    report_p = sub.add_parser(
        "report",
        help=(
            "Measure one report period against a production control plane and "
            "render it. Read-only by capability; states measurements only."
        ),
    )
    add_arguments(report_p)


def build_parser() -> argparse.ArgumentParser:
    """The standalone parser, for ``python -m claude_org_runtime.measurement.cli``."""

    parser = argparse.ArgumentParser(
        prog="claude-org-runtime measure",
        description=(
            "Measurement harness for the Interlock control plane "
            "(docs/measurement-harness.md). Read-only by capability."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_subparsers(sub)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(None if argv is None else list(argv))
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess
    sys.exit(main())
