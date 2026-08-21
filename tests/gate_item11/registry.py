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
from claude_org_runtime.session.provider import SessionProvider, SessionReadout
from claude_org_runtime.session.stub_provider import LocalProcessSessionProvider


def _always_available() -> str | None:
    return None


def _never_disqualified(readout: SessionReadout) -> str | None:
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
    #: Why the bound session's readout proves the backend was *not* live, or
    #: ``None`` when it qualifies. Consulted by the provider plugin after the
    #: bound session is observed, and per-provider on purpose: judging a
    #: state word takes that provider's vocabulary, which is exactly the
    #: knowledge item 11 confines to this package. Without it, a backend
    #: whose child dies at spawn (a broken install, say) would qualify as
    #: "live" and green the whole measurement while measuring nothing.
    disqualified: Callable[[SessionReadout], str | None] = _never_disqualified


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


def _claude_cli_disqualified(readout: SessionReadout) -> str | None:
    """A child that died without ever producing structured output.

    ``exited-<rc>`` is S2's word for exactly that case: no init event, no
    result, only an exit disposition -- which is what a present-but-broken
    install (unauthenticated, no network) produces, with the actual refusal
    on stderr. Such a backend answers every probe and still cannot sustain a
    single session, so it must abort the measurement (D-0010) rather than
    green a run whose header claims a live backend. A session that *spoke*
    and then finished -- any state from its own output, errors included -- is
    a session the backend really ran, and qualifies.
    """

    state = readout.provider_state or ""
    if state.startswith("exited-"):
        return (
            f"the bound session's child died without producing structured "
            f"output (state {state!r}); its stderr: "
            f"{str(readout.provider_detail.get('stderr_tail', ''))!r}"
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
        disqualified=_claude_cli_disqualified,
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
