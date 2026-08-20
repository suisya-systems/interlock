"""Record what a pytest run collected, what it decided, and what it read.

Item 11's exit condition is "zero test modifications required", and the way to
measure it is to run the same suite twice -- once with a provider bound, once
without -- and compare. That comparison is only worth anything if it can tell
apart *the same suite behaving the same way* from *a different suite*, so this
plugin records both halves:

``outcomes``
    every collected test id, with the outcome of each of its phases. Test ids
    rather than a count: a run that lost a test and gained another would keep
    its total.

``artifact``
    the SHA-256 of every file the run collected from. Issue ``#20``'s fourth
    criterion asks that the two runs be against the *same suite artifact*; a
    digest is how that is evidenced rather than asserted.

Loaded with ``-p tests.gate_item11.outcome_recorder`` and told where to write by
:data:`REPORT_ENV`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

#: Where the report is written. Absent means the plugin records nothing, so
#: loading it in an ordinary run is harmless.
REPORT_ENV = "INTERLOCK_ITEM11_REPORT"

_outcomes: dict[str, dict[str, str]] = {}
_files: set[str] = set()


def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items) -> None:
    for item in items:
        _outcomes.setdefault(item.nodeid, {})
        path = getattr(item, "path", None)
        if path is not None:
            _files.add(str(path))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    phases = _outcomes.setdefault(report.nodeid, {})
    # "passed" in setup and teardown is not interesting on its own, but losing
    # it would hide a test that started passing only because its fixture stopped
    # running -- which is exactly the shape a provider binding could take.
    phases[report.when] = report.outcome


def _digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    destination = os.environ.get(REPORT_ENV)
    if not destination:
        return
    report: dict[str, Any] = {
        "exitstatus": int(exitstatus),
        "outcomes": _outcomes,
        "artifact": {
            os.path.basename(path): _digest(path) for path in sorted(_files)
        },
    }
    Path(destination).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
