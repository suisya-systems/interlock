"""The provider registry item 11's measurement is parameterised over.

Gate item 11 (``ACCEPTANCE.md`` §1, issue ``#20``) claims that even if the
session backend does not hold, **only the ``SessionProvider`` need be swapped**.
Issue ``#20``'s fourth acceptance criterion says how that is evidenced: the run
must be against the *same* suite artifact as any other provider's run,
"differing only in provider fixture".

This module is that fixture's whole variable half. One entry per shipped
``SessionProvider`` implementation, and nothing else in ``tests/gate_item11``
names a provider: adding S2 the day issue ``#17`` lands is one entry here, and
:func:`shipped_providers` makes leaving it out fail the build rather than
quietly narrow the measurement to S3.
"""

from __future__ import annotations

import inspect
import pkgutil
import shutil
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Callable

from claude_org_runtime import session as session_package
from claude_org_runtime.session.claude_cli_provider import ClaudeCliSessionProvider
from claude_org_runtime.session.provider import SessionProvider
from claude_org_runtime.session.stub_provider import LocalProcessSessionProvider


def _always_available() -> str | None:
    return None


@dataclass(frozen=True)
class ProviderEntry:
    """One provider the control-plane suite can be run against.

    *scaffold* and *issue* are carried so a failing run in CI names which
    artifact it was measuring without a reader having to map class names back
    onto ``docs/gate-record.md`` §5.
    """

    id: str
    scaffold: str
    issue: str
    implementation: type[SessionProvider]
    #: Builds a ready-to-use provider rooted at a directory the caller owns.
    #: Deliberately takes the state root: a provider defaulted to a shared
    #: location would read another run's children, which is the one way a stub
    #: can make this suite lie (see ``LocalProcessSessionProvider``).
    factory: Callable[[Path], SessionProvider]
    #: Why this environment cannot run this provider, or ``None`` when it can.
    #: A backend-availability precondition, not an escape hatch: the *tests*
    #: are never skipped selectively (issue ``#20`` forbids that), only a
    #: whole provider row on a machine that does not carry its backend --
    #: exactly as the bwrap-dependent sandbox tests skip where bwrap cannot
    #: run. S3 exists so that at least one row runs everywhere.
    unavailable: Callable[[], str | None] = _always_available


def _stub(state_root: Path) -> SessionProvider:
    return LocalProcessSessionProvider(state_root)


def _claude_cli(state_root: Path) -> SessionProvider:
    # ``--model haiku`` pins the measurement's live sessions to the cheapest
    # model tier. Provider-wide spawn configuration, not per-role settings:
    # which model a *worker* runs is a role concern that arrives through
    # ``StartRequest.settings``, and nothing in this suite measures models.
    return ClaudeCliSessionProvider(state_root, base_cli_args=("--model", "haiku"))


def _claude_cli_unavailable() -> str | None:
    if shutil.which("claude") is None:
        return (
            "the claude CLI is not on PATH; the C2 provider (S2, issue #17) "
            "spawns real `claude -p` children and cannot run here"
        )
    return None


#: Every provider the measurement runs against, keyed by the handle
#: ``docs/gate-record.md`` §5 gives it.
PROVIDERS: dict[str, ProviderEntry] = {
    "S2": ProviderEntry(
        id="S2",
        scaffold="S2 -- the C2 provider over Interlock-supervised claude -p subprocesses",
        issue="#17",
        implementation=ClaudeCliSessionProvider,
        factory=_claude_cli,
        unavailable=_claude_cli_unavailable,
    ),
    "S3": ProviderEntry(
        id="S3",
        scaffold="S3 -- the stub provider over local child processes",
        issue="#11",
        implementation=LocalProcessSessionProvider,
        factory=_stub,
    ),
}

#: The provider bound when nothing names one. S3 by construction: it is the one
#: implementation that needs no Claude CLI and no network, so the measurement
#: runs identically on a developer's machine and on every CI row.
DEFAULT_PROVIDER = "S3"


def shipped_providers() -> dict[str, type[SessionProvider]]:
    """Every concrete ``SessionProvider`` in :mod:`claude_org_runtime.session`.

    Discovered rather than listed, so that a provider which ships without being
    registered above is found by the walk instead of by a reviewer.
    """

    found: dict[str, type[SessionProvider]] = {}
    for module in pkgutil.iter_modules(session_package.__path__):
        imported = import_module(f"{session_package.__name__}.{module.name}")
        for _, candidate in inspect.getmembers(imported, inspect.isclass):
            if not issubclass(candidate, SessionProvider) or candidate is SessionProvider:
                continue
            if inspect.isabstract(candidate):
                continue
            found[f"{candidate.__module__}.{candidate.__qualname__}"] = candidate
    return found
