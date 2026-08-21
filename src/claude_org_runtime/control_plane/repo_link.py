"""G3 -- repository identity, the pull-request projection, and the run<->PR linkage.

``docs/production-schema.md`` sections 7.1-7.4 and ``D-0034``. The whole module
is written against one dated incident: on 2026-08-06 v1's run->PR tools
defaulted an omitted ``--repo`` to ``gh repo view`` -- the cwd repository,
always the home repo for the Secretary -- so a cross-repo run's PR number was
resolved against the wrong repository, and renga PR #302 was recorded with
claude-org-ja PR #302's branch, commit and merge time. **The tool exited ok.**
Whether it corrupted silently or failed loudly depended only on whether the home
repo happened to own that number.

Three consequences shape this surface:

* **There is no working-directory fallback, and no parameter that could become
  one.** :func:`resolve_repository` takes a provider id and/or a slug and
  nothing else; it has no ``cwd``, no ``default_repo``, no ``or_current``. The
  absence is the design -- a later reader cannot reintroduce the incident by
  passing an argument, only by editing this signature. When resolution fails it
  raises :class:`RepoResolutionError`, which is v1's own answer, kept for v1's
  own reason: "so the caller can exit non-zero instead of writing a foreign
  repo's PR onto the run".
* **Identity is ``repo_id``, never a URL string and never the slug.** ``owner``
  and ``name`` are mutable -- a GitHub rename or transfer preserves the
  repository -- so :func:`upsert_repository` absorbs a rename onto the *existing*
  row whenever the immutable ``provider_repo_id`` matches, and every historical
  observation stays attached to the same ``repo_id``. The columns keep their
  case because the value is handed to ``gh --repo`` and recorded in payloads;
  only the unique index folds it.
* **An observation of a pull request is an event first and a projection
  second.** :func:`observe_pull_request` appends to the spine through
  :func:`~.events.append_event` and writes the ``pull_request`` row as that
  append's typed side effect, so the projection and the fact it came from commit
  together or not at all. ``head_event_seq`` is what makes a head move auditable:
  the section 6.3 verdict projection selects CI evidence by ``head_sha``, so the
  event that moved the head has to be identifiable, not merely timestamped.

**A reopen is a projection of a provider event, not an edit.** ``closed -> open``
is admitted (only ``merged`` is terminal), and section 7.2 says what admitting it
costs if done by halves: section 8.2 retires a ``watcher_scope`` when its PR goes
terminal, so a reopen that clears ``closed_at_ms`` without clearing
``watcher_scope.retired_at_ms`` leaves the PR watched in name only. Both happen
in the one append transaction here.

**A no-change observation appends nothing.** Re-polling a PR whose head and
state are unchanged is not a new fact, and section 7.2 allows refreshing the
observation timestamp and no more. Spending an event row on it would put one row
per poll interval per PR on the spine -- and the trace that *does* need to record
"polled, nothing changed" already exists and is not this table:
``watcher_liveness.last_result = 'observed_no_change'`` (section 8.3).

Time is the caller's everywhere, as integer epoch milliseconds; nothing in this
module reads a clock, and no column it writes has a ``DEFAULT``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from claude_org_runtime.control_plane.events import AppendedEvent, append_event
from claude_org_runtime.control_plane.schema import ControlPlaneRefusal
from claude_org_runtime.control_plane.txn import transaction

#: How a repository was resolved, as ``run_pr_link.resolution`` CHECKs it. The
#: set is closed and the absence of a cwd-default member is the 2026-08-06
#: incident encoded: there is no value this column can hold that means "we
#: guessed from the working directory".
RESOLUTIONS = ("project_registry", "explicit_operator", "provider_event")

#: ``run_pr_link.role``. Only the ``primary`` link drives a run's completion
#: transition, and at most one is live per run at a time.
ROLES = ("primary", "supporting")

#: ``pull_request.state``. Only ``merged`` is terminal.
PR_STATES = ("open", "merged", "closed")

_SHA = re.compile(r"\A[0-9a-f]{40}\Z")

_PROVIDER = "github"


class RepoResolutionError(ControlPlaneRefusal):
    """A repository could not be resolved, and nothing was defaulted.

    v1 raises this "so the caller can exit non-zero instead of writing a foreign
    repo's PR onto the run", and that sentence is the whole reason this class
    exists rather than a ``None`` return: a caller that forgets to check a
    ``None`` writes the foreign repo's PR, which is exactly what happened on
    2026-08-06.
    """


class PullRequestObservationRefused(ControlPlaneRefusal):
    """An observation was not a projectable state of a pull request.

    The ``pull_request`` CHECKs tie ``state`` to ``merged_at_ms``,
    ``merge_commit_sha`` and ``closed_at_ms`` as biconditionals. Reaching them
    as a raw ``IntegrityError`` would tell the caller that *some* constraint
    failed; this says which fact was missing from the observation.
    """


class StalePullRequestObservation(ControlPlaneRefusal):
    """The projection moved between reading it and writing the append.

    :func:`observe_pull_request` has to know the prior state before it can name
    the event (a merge and a head move are different facts), and that read
    happens before :func:`~.events.append_event` opens its transaction. The read
    is therefore re-taken inside the transaction and compared; a disagreement
    aborts the append rather than filing an event whose type was chosen against
    a state that no longer exists.
    """


class RunPrLinkRefused(ControlPlaneRefusal):
    """A run<->PR link was refused, and no link was written.

    Covers the second live ``primary`` for one run, an unlink of a link that is
    not live, and a re-link of a ``(run_id, pr_id)`` pair the history already
    holds. Each is a caller error whose correct answer is to stop, not to
    overwrite a row that records what was believed earlier.
    """


@dataclass(frozen=True)
class ObservedPullRequest:
    """What one call to :func:`observe_pull_request` did.

    ``event`` is ``None`` exactly when ``changed`` is false -- the no-change
    re-poll, which appends nothing. When the append was a duplicate
    (``event.duplicate``) the whole transaction was abandoned, so the projection
    fields describe what *would* have changed and the database was not touched.
    """

    pr_id: str
    changed: bool
    created: bool
    head_moved: bool
    reopened: bool
    event_type: str | None
    event: AppendedEvent | None
    reactivated_scopes: tuple[str, ...]


# --------------------------------------------------------------------------
# repository identity
# --------------------------------------------------------------------------


def upsert_repository(
    connection: sqlite3.Connection,
    *,
    repo_id: str,
    owner: str,
    name: str,
    now_ms: int,
    provider_repo_id: str | None = None,
    provider: str = _PROVIDER,
) -> str:
    """Record a repository, absorbing a rename or transfer onto the existing row.

    Returns the ``repo_id`` the repository actually has, which is *not*
    necessarily the one passed: when ``provider_repo_id`` matches a row already
    present, that row is the repository, and its identity wins. Absorbing rather
    than inserting is what keeps every historical ``pull_request``,
    ``ci_observation`` and ``watcher_scope`` attached across a rename; the
    alternative -- a new row for the new slug -- forks the identity silently and
    leaves the metrics join to guess, which is the defect ``D-0034`` names in
    v1's stored ``pr_url``.

    Case is preserved in ``owner``/``name`` and folded only in the lookup index,
    because the value is handed to ``gh --repo`` and recorded in payloads.

    :raises RepoResolutionError: if *repo_id* already names a different
        repository, or if the rename would collide with another row's slug.
        Both mean two identities are being merged by accident, and a wrong
        merge here is indistinguishable downstream from the 2026-08-06 incident.
    """

    _require_epoch_ms(now_ms=now_ms)
    _require_text(repo_id=repo_id, owner=owner, name=name)
    if provider_repo_id is not None and not provider_repo_id:
        raise RepoResolutionError("provider_repo_id, when given, must be a non-empty string")

    with transaction(connection) as txn:
        existing = None
        if provider_repo_id is not None:
            existing = _one(
                txn,
                "SELECT * FROM repository WHERE provider = ? AND provider_repo_id = ?",
                (provider, provider_repo_id),
            )
        if existing is None:
            existing = _one(
                txn,
                "SELECT * FROM repository"
                " WHERE provider = ? AND lower(owner) = lower(?) AND lower(name) = lower(?)",
                (provider, owner, name),
            )

        if existing is None:
            claimed = _one(
                txn,
                "SELECT provider, owner, name FROM repository WHERE repo_id = ?",
                (repo_id,),
            )
            if claimed is not None:
                raise RepoResolutionError(
                    f"repo_id {repo_id!r} already names "
                    f"{claimed['owner']}/{claimed['name']}; a repository identity is never "
                    "reassigned, because every observation ever attached to it would move too"
                )
            txn.execute(
                "INSERT INTO repository (repo_id, provider, provider_repo_id, owner, name,"
                " created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_id, provider, provider_repo_id, owner, name, now_ms, now_ms),
            )
            return repo_id

        resolved_id = str(existing["repo_id"])
        # provider_repo_id is learned once and never rewritten: a row whose
        # immutable id changed is two repositories, not a rename.
        if (
            provider_repo_id is not None
            and existing["provider_repo_id"] is not None
            and str(existing["provider_repo_id"]) != provider_repo_id
        ):
            raise RepoResolutionError(
                f"{owner}/{name} is already recorded as {resolved_id} with provider id "
                f"{existing['provider_repo_id']!r}; a slug that moves to a different immutable "
                "id is a different repository reusing the name, not a rename"
            )
        merged_provider_repo_id = (
            existing["provider_repo_id"] if provider_repo_id is None else provider_repo_id
        )
        try:
            txn.execute(
                "UPDATE repository SET owner = ?, name = ?, provider_repo_id = ?,"
                " updated_at_ms = ? WHERE repo_id = ?",
                (
                    owner,
                    name,
                    merged_provider_repo_id,
                    max(now_ms, int(existing["updated_at_ms"])),
                    resolved_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise RepoResolutionError(
                f"{owner}/{name} is already held by another repository row; renaming "
                f"{resolved_id} onto it would merge two identities ({error})"
            ) from error
        return resolved_id


def resolve_repository(
    connection: sqlite3.Connection,
    *,
    owner: str | None = None,
    name: str | None = None,
    provider_repo_id: str | None = None,
    provider: str = _PROVIDER,
) -> str:
    """Resolve a repository to its ``repo_id``, or refuse.

    The immutable ``provider_repo_id`` is tried first and the slug second,
    case-insensitively, because the slug is a lookup key and the provider id is
    the identity. **There is no third fallback**, and deliberately no parameter
    that could grow into one: the 2026-08-06 incident was a defaulted ``--repo``,
    so the safety here is the absence of the argument, not a check on its value.

    :raises RepoResolutionError: if no identifier was supplied, if only half a
        slug was supplied, or if nothing matched. A caller that cannot name the
        repository must exit non-zero.
    """

    if provider_repo_id is not None:
        row = _one(
            connection,
            "SELECT repo_id FROM repository WHERE provider = ? AND provider_repo_id = ?",
            (provider, provider_repo_id),
        )
        if row is not None:
            return str(row["repo_id"])

    if (owner is None) != (name is None):
        raise RepoResolutionError(
            "a slug lookup needs both owner and name; half a slug is not a repository"
        )
    if owner is not None and name is not None:
        row = _one(
            connection,
            "SELECT repo_id FROM repository"
            " WHERE provider = ? AND lower(owner) = lower(?) AND lower(name) = lower(?)",
            (provider, owner, name),
        )
        if row is not None:
            return str(row["repo_id"])

    if provider_repo_id is None and owner is None and name is None:
        raise RepoResolutionError(
            "a repository is resolved by provider id or by owner/name and by nothing else; "
            "there is no working-directory default (2026-08-06)"
        )
    wanted = provider_repo_id if provider_repo_id is not None else f"{owner}/{name}"
    raise RepoResolutionError(
        f"no repository matches {wanted!r}; refusing to default, because the caller exiting "
        "non-zero is the only alternative to writing a foreign repository's PR onto the run"
    )


# --------------------------------------------------------------------------
# the pull-request projection
# --------------------------------------------------------------------------


def observe_pull_request(
    connection: sqlite3.Connection,
    *,
    repo_id: str,
    pr_number: int,
    head_sha: str,
    state: str,
    observed_at_ms: int,
    ingested_at_ms: int,
    event_id: str,
    producer: str,
    producer_epoch: int | None = None,
    provider_pr_id: str | None = None,
    merge_commit_sha: str | None = None,
    merged_at_ms: int | None = None,
    closed_at_ms: int | None = None,
    run_id: str | None = None,
    dedup_key: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> ObservedPullRequest:
    """Project one provider observation of a pull request, as an event.

    The event is appended through :func:`~.events.append_event` and the
    ``pull_request`` row is written inside that append's transaction, so the
    projection cannot exist without the fact that produced it and
    ``head_event_seq`` names the event that moved the head.

    Which event the observation *is* comes from the transition, most consequential
    first: a merge is ``pr_merged``, a close is ``pr_closed``, a ``closed -> open``
    is ``pr_reopened``, and a head that moved with no state change is
    ``pr_head_updated``. The first observation of a PR is ``pr_head_updated``
    when it arrives open: the implementation's vocabulary has no ``pr_opened``,
    and the fact that matters downstream -- section 6.3 selects CI evidence by
    ``head_sha`` -- is that this head is now the head.

    *observed_at_ms* is the **provider's** clock for the observed state and
    *ingested_at_ms* is ours; section 5.2 keeps them apart because a provider's
    skew would otherwise read as a relay gap. The default ``dedup_key`` is built
    from the provider's own timestamp for the fact, so a re-poll of an unchanged
    provider state is the same key and a genuine second transition -- including a
    force-push back to a previously seen ``head_sha`` -- is a different one.

    An observation that changes neither head nor state writes nothing and
    returns ``changed=False``; "polled, nothing changed" is
    ``watcher_liveness``'s distinction to record, not the spine's.

    :raises PullRequestObservationRefused: for a state whose accompanying facts
        are missing or contradictory (a merge with no ``merge_commit_sha``, an
        open PR carrying ``closed_at_ms``, a non-lowercase or non-40-character
        ``head_sha``), and for a head move the provider's own order does not
        support.
    :raises StalePullRequestObservation: if the projection changed between the
        transition being named and the transaction that writes it.
    """

    _require_epoch_ms(observed_at_ms=observed_at_ms, ingested_at_ms=ingested_at_ms)
    _require_text(repo_id=repo_id, event_id=event_id, producer=producer)
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise PullRequestObservationRefused(
            f"pr_number must be a positive integer, got {pr_number!r}"
        )
    if not _SHA.match(head_sha or ""):
        raise PullRequestObservationRefused(
            f"head_sha must be 40 lowercase hex characters, got {head_sha!r}"
        )
    _require_state_facts(
        state=state,
        merge_commit_sha=merge_commit_sha,
        merged_at_ms=merged_at_ms,
        closed_at_ms=closed_at_ms,
    )

    pr_id = f"{repo_id}#{pr_number}"
    before = _read_pull_request(connection, repo_id=repo_id, pr_number=pr_number)
    plan = _plan(
        before=before,
        head_sha=head_sha,
        state=state,
        observed_at_ms=observed_at_ms,
    )
    if plan is None:
        return ObservedPullRequest(
            pr_id=pr_id,
            changed=False,
            created=False,
            head_moved=False,
            reopened=False,
            event_type=None,
            event=None,
            reactivated_scopes=(),
        )
    event_type, head_moved, reopened, created = plan

    fact_at_ms = {
        "pr_merged": merged_at_ms,
        "pr_closed": closed_at_ms,
    }.get(event_type, observed_at_ms)
    key = dedup_key or f"{event_type}/{repo_id}/{pr_number}/{head_sha}/{fact_at_ms}"

    body = {
        "repo_id": repo_id,
        "pr_number": pr_number,
        "pr_id": pr_id,
        "head_sha": head_sha,
        "state": state,
        "previous_state": None if before is None else before["state"],
        "previous_head_sha": None if before is None else before["head_sha"],
        "merge_commit_sha": merge_commit_sha,
        "merged_at_ms": merged_at_ms,
        "closed_at_ms": closed_at_ms,
    }
    if payload is not None:
        body.update(payload)

    reactivated: list[str] = []

    def side_effect(txn: sqlite3.Connection, seq: int) -> None:
        _write_projection(
            txn,
            seq=seq,
            expected=before,
            pr_id=pr_id,
            repo_id=repo_id,
            pr_number=pr_number,
            provider_pr_id=provider_pr_id,
            head_sha=head_sha,
            state=state,
            observed_at_ms=observed_at_ms,
            ingested_at_ms=ingested_at_ms,
            merge_commit_sha=merge_commit_sha,
            merged_at_ms=merged_at_ms,
            closed_at_ms=closed_at_ms,
            reopened=reopened,
            reactivated=reactivated,
        )

    appended = append_event(
        connection,
        event_id=event_id,
        event_type=event_type,
        subject_kind="pull_request",
        subject_id=pr_id,
        dedup_key=key,
        producer=producer,
        occurred_at_ms=observed_at_ms,
        ingested_at_ms=ingested_at_ms,
        run_id=run_id,
        producer_epoch=producer_epoch,
        payload=json.dumps(body, sort_keys=True),
        side_effect=side_effect,
    )
    if appended.duplicate:
        # The transaction was abandoned, so nothing the side effect collected
        # describes the database any more.
        reactivated.clear()
    return ObservedPullRequest(
        pr_id=pr_id,
        changed=not appended.duplicate,
        created=created and not appended.duplicate,
        head_moved=head_moved and not appended.duplicate,
        reopened=reopened and not appended.duplicate,
        event_type=event_type,
        event=appended,
        reactivated_scopes=tuple(reactivated),
    )


# --------------------------------------------------------------------------
# run <-> PR linkage
# --------------------------------------------------------------------------


def link_run_pr(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    pr_id: str,
    role: str,
    resolution: str,
    linked_at_ms: int,
) -> None:
    """Link a run to a pull request, naming how the repository was resolved.

    The linkage is many-to-many on purpose (``D-0034``): a run may hold several
    PRs across repositories, and a PR may be touched by several runs. What makes
    completion unambiguous despite both is that at most one ``primary`` link per
    run is live at a time -- so a re-point is an :func:`unlink_run_pr` with a
    recorded reason followed by a link to another PR, and both rows survive as
    the history of the re-point.

    *resolution* is checked here as well as in the DDL, because the value that
    matters is the one that is *absent*: a caller reaching for a
    working-directory guess finds no member to name it, and gets this refusal
    rather than a raw ``IntegrityError`` two layers down.

    :raises RunPrLinkRefused: for an unknown role or resolution, a second live
        primary, an unknown run or PR, or a pair the table already holds.
    """

    _require_epoch_ms(linked_at_ms=linked_at_ms)
    _require_text(run_id=run_id, pr_id=pr_id)
    if role not in ROLES:
        raise RunPrLinkRefused(f"role must be one of {ROLES}, got {role!r}")
    if resolution not in RESOLUTIONS:
        raise RunPrLinkRefused(
            f"resolution must be one of {RESOLUTIONS}, got {resolution!r}; there is no member "
            "meaning 'we guessed from the working directory' (2026-08-06)"
        )

    with transaction(connection) as txn:
        if role == "primary":
            live = _one(
                txn,
                "SELECT pr_id FROM run_pr_link"
                " WHERE run_id = ? AND role = 'primary' AND unlinked_at_ms IS NULL",
                (run_id,),
            )
            if live is not None:
                raise RunPrLinkRefused(
                    f"run {run_id!r} already has a live primary link to {live['pr_id']!r}; "
                    "re-point it by unlinking that link with a reason, so the history of the "
                    "re-point survives"
                )
        try:
            txn.execute(
                "INSERT INTO run_pr_link (run_id, pr_id, role, resolution, linked_at_ms,"
                " unlinked_at_ms, unlink_reason) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (run_id, pr_id, role, resolution, linked_at_ms),
            )
        except sqlite3.IntegrityError as error:
            raise RunPrLinkRefused(
                f"({run_id!r}, {pr_id!r}) could not be linked: {error}"
            ) from error


def unlink_run_pr(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    pr_id: str,
    unlinked_at_ms: int,
    unlink_reason: str,
) -> None:
    """Retire a live link, recording why.

    The reason is mandatory in the signature and in the DDL's biconditional
    because an unlinked link with no reason is indistinguishable from a link
    somebody deleted -- and the row exists precisely so that a re-point can be
    read back later.

    :raises RunPrLinkRefused: if there is no live link for the pair, or the
        reason is empty, or *unlinked_at_ms* precedes the link.
    """

    _require_epoch_ms(unlinked_at_ms=unlinked_at_ms)
    _require_text(run_id=run_id, pr_id=pr_id, unlink_reason=unlink_reason)

    with transaction(connection) as txn:
        updated = txn.execute(
            "UPDATE run_pr_link SET unlinked_at_ms = ?, unlink_reason = ?"
            " WHERE run_id = ? AND pr_id = ? AND unlinked_at_ms IS NULL"
            "   AND ? >= linked_at_ms",
            (unlinked_at_ms, unlink_reason, run_id, pr_id, unlinked_at_ms),
        ).rowcount
        if updated == 0:
            raise RunPrLinkRefused(
                f"no live link ({run_id!r}, {pr_id!r}) at or after its linked_at_ms to unlink; "
                "an already-unlinked link is history and is never rewritten"
            )


def primary_link(
    connection: sqlite3.Connection, *, run_id: str
) -> Mapping[str, Any] | None:
    """The run's live ``primary`` link, or ``None``.

    ``None`` is the honest answer here, unlike in :func:`resolve_repository`:
    a run with no primary PR yet is the ordinary state of every run before its
    first PR exists, and the caller's next move is to wait, not to exit.
    """

    return _one(
        connection,
        "SELECT * FROM run_pr_link"
        " WHERE run_id = ? AND role = 'primary' AND unlinked_at_ms IS NULL",
        (run_id,),
    )


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _plan(
    *,
    before: Mapping[str, Any] | None,
    head_sha: str,
    state: str,
    observed_at_ms: int,
) -> tuple[str, bool, bool, bool] | None:
    """Name the transition: ``(event_type, head_moved, reopened, created)``.

    ``None`` means the observation restates what is already recorded. The order
    of the tests is the order of consequence -- a merge that also moved the head
    is a merge, because ``pr_merged`` is what the Secretary consumes to complete
    a run and filing it as ``pr_head_updated`` would strand the run.
    """

    if before is None:
        if state == "merged":
            return ("pr_merged", True, False, True)
        if state == "closed":
            return ("pr_closed", True, False, True)
        return ("pr_head_updated", True, False, True)

    was_state = str(before["state"])
    was_head = str(before["head_sha"])
    head_moved = head_sha != was_head

    if state == "merged" and was_state != "merged":
        return ("pr_merged", head_moved, False, False)
    if was_state == "merged" and state != "merged":
        raise PullRequestObservationRefused(
            f"{before['pr_id']} is recorded merged and a merge is a fact; an observation "
            f"reporting {state!r} is either a different pull request or a bad read"
        )
    if state == "closed" and was_state == "open":
        return ("pr_closed", head_moved, False, False)
    if state == "open" and was_state == "closed":
        return ("pr_reopened", head_moved, True, False)
    if head_moved:
        if observed_at_ms <= int(before["head_observed_at_ms"]):
            raise PullRequestObservationRefused(
                f"{before['pr_id']} head {was_head} was observed at "
                f"{before['head_observed_at_ms']}; a head move claimed at {observed_at_ms} is a "
                "late arrival, which is evidence and not a projection (section 7.2)"
            )
        return ("pr_head_updated", True, False, False)
    return None


def _write_projection(
    txn: sqlite3.Connection,
    *,
    seq: int,
    expected: Mapping[str, Any] | None,
    pr_id: str,
    repo_id: str,
    pr_number: int,
    provider_pr_id: str | None,
    head_sha: str,
    state: str,
    observed_at_ms: int,
    ingested_at_ms: int,
    merge_commit_sha: str | None,
    merged_at_ms: int | None,
    closed_at_ms: int | None,
    reopened: bool,
    reactivated: list[str],
) -> None:
    """Write the row, and on a reopen un-retire the scope in the same breath.

    The prior row is re-read here and compared with what the transition was
    named against: the naming read happened before the transaction opened, and a
    projection that moved in between would have its event mislabelled. The
    comparison is on the three columns the naming used.
    """

    current = _read_pull_request(txn, repo_id=repo_id, pr_number=pr_number)
    if _identity(current) != _identity(expected):
        raise StalePullRequestObservation(
            f"{pr_id} changed between naming this observation and writing it "
            f"({_identity(expected)} -> {_identity(current)}); the event would carry the "
            "wrong type, so nothing is written"
        )

    if current is None:
        txn.execute(
            "INSERT INTO pull_request (pr_id, repo_id, pr_number, provider_pr_id, head_sha,"
            " head_observed_at_ms, head_event_seq, state, merge_commit_sha, merged_at_ms,"
            " closed_at_ms, created_at_ms, updated_at_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pr_id,
                repo_id,
                pr_number,
                provider_pr_id,
                head_sha,
                observed_at_ms,
                seq,
                state,
                merge_commit_sha,
                merged_at_ms,
                closed_at_ms,
                ingested_at_ms,
                ingested_at_ms,
            ),
        )
        return

    txn.execute(
        "UPDATE pull_request SET head_sha = ?, head_observed_at_ms = ?, head_event_seq = ?,"
        " state = ?, provider_pr_id = COALESCE(?, provider_pr_id), merge_commit_sha = ?,"
        " merged_at_ms = ?, closed_at_ms = ?, updated_at_ms = ?"
        " WHERE pr_id = ?",
        (
            head_sha,
            max(observed_at_ms, int(current["head_observed_at_ms"])),
            max(seq, int(current["head_event_seq"])),
            state,
            provider_pr_id,
            merge_commit_sha,
            merged_at_ms,
            closed_at_ms,
            max(ingested_at_ms, int(current["updated_at_ms"])),
            pr_id,
        ),
    )

    if not reopened:
        return
    # Section 7.2: section 8.2 retired this PR's scope when it went terminal, so
    # a reopen that clears closed_at_ms and stops there leaves the PR watched in
    # name only. Same transaction, or the reconcile pass's scope-coverage query
    # is left to find it.
    scopes = _all(
        txn,
        "SELECT scope_id FROM watcher_scope WHERE pr_id = ? AND retired_at_ms IS NOT NULL",
        (pr_id,),
    )
    for scope in scopes:
        txn.execute(
            "UPDATE watcher_scope SET retired_at_ms = NULL WHERE scope_id = ?",
            (scope["scope_id"],),
        )
        reactivated.append(str(scope["scope_id"]))


def _one(
    connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]
) -> dict[str, Any] | None:
    """One row as a plain dict, or ``None``.

    The rows are read by name through a per-cursor ``row_factory`` rather than
    by relying on the connection's: the production connection is handed to this
    module by the migrator, which sets pragmas and nothing else, and a module
    that mutated the shared connection's ``row_factory`` would change what every
    other caller's ``SELECT`` returns.
    """

    rows = _all(connection, sql, params)
    return rows[0] if rows else None


def _all(
    connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]
) -> tuple[dict[str, Any], ...]:
    cursor = connection.execute(sql, params)
    cursor.row_factory = sqlite3.Row
    return tuple(dict(row) for row in cursor.fetchall())


def _read_pull_request(
    connection: sqlite3.Connection, *, repo_id: str, pr_number: int
) -> Mapping[str, Any] | None:
    return _one(
        connection,
        "SELECT * FROM pull_request WHERE repo_id = ? AND pr_number = ?",
        (repo_id, pr_number),
    )


def _identity(row: Mapping[str, Any] | None) -> tuple[Any, ...] | None:
    if row is None:
        return None
    return (row["state"], row["head_sha"], row["head_event_seq"])


def _require_state_facts(
    *,
    state: str,
    merge_commit_sha: str | None,
    merged_at_ms: int | None,
    closed_at_ms: int | None,
) -> None:
    """The ``pull_request`` biconditionals, refused by name instead of by CHECK."""

    if state not in PR_STATES:
        raise PullRequestObservationRefused(f"state must be one of {PR_STATES}, got {state!r}")
    if (state == "merged") != (merged_at_ms is not None):
        raise PullRequestObservationRefused(
            f"state {state!r} and merged_at_ms {merged_at_ms!r} disagree; a merge carries its "
            "own time and nothing else does"
        )
    if (state == "merged") != (merge_commit_sha is not None):
        raise PullRequestObservationRefused(
            f"state {state!r} and merge_commit_sha {merge_commit_sha!r} disagree"
        )
    if merge_commit_sha is not None and not _SHA.match(merge_commit_sha):
        raise PullRequestObservationRefused(
            f"merge_commit_sha must be 40 lowercase hex characters, got {merge_commit_sha!r}"
        )
    if (state in ("merged", "closed")) != (closed_at_ms is not None):
        raise PullRequestObservationRefused(
            f"state {state!r} and closed_at_ms {closed_at_ms!r} disagree; a merged or closed "
            "pull request is closed and an open one is not"
        )


def _require_epoch_ms(**values: int) -> None:
    """Reject a clock value that is not an integer count of milliseconds.

    ``bool`` is excluded explicitly: it is an ``int`` in Python, so
    ``observed_at_ms=True`` would store ``1`` -- a timestamp in 1970 that the
    ``typeof`` CHECK cannot catch, because SQLite sees a perfectly good integer.
    """

    for label, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                f"{label} must be an int of epoch milliseconds, got {type(value).__name__}; "
                "the clock is the caller's and is never read from the database"
            )


def _require_text(**values: str) -> None:
    for label, value in values.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a non-empty string, got {value!r}")
