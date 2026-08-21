"""What ``repo_link`` must keep true -- the 2026-08-06 regression, and section 7's cardinality.

These tests exist for two things that a reading of the DDL alone does not give.

The first is the **2026-08-06 regression**. v1's run->PR tools defaulted an
omitted ``--repo`` to the cwd repository and recorded renga PR #302 with
claude-org-ja PR #302's branch, commit and merge time, exiting ``ok``. The tests
named for it assert the *absence* of the defaulting -- against the ``CHECK``, so
no writer can name a working-directory guess, and against the Python signature,
so no caller can pass one. An absence is exactly the kind of property that
rots quietly when someone later adds a convenience argument, which is why it is
asserted rather than documented.

The second is **section 7's cardinality**, whose three questions get three
different answers (several PRs per run, across repositories; several runs per
PR; one live ``primary`` per run). Each is one test named as the property, and
each would pass just as well against a schema that answered a *different* one of
the three -- so all three are here, not a representative one.

Every timestamp is :data:`T0` plus arithmetic. The schema gives no timestamp
column a ``DEFAULT`` because the caller owns the clock; a suite whose
expectations moved with the wall clock could not assert an ordering boundary.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from claude_org_runtime.control_plane import repo_link
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.control_plane.repo_link import (
    ObservedPullRequest,
    PullRequestObservationRefused,
    RepoResolutionError,
    RunPrLinkRefused,
    link_run_pr,
    observe_pull_request,
    primary_link,
    resolve_repository,
    unlink_run_pr,
    upsert_repository,
)

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_MERGE = "c" * 40


@pytest.fixture
def cp(tmp_path: Path):
    connection = create_production_control_plane(tmp_path / "production.sqlite3", now_ms=T0)
    try:
        yield connection
    finally:
        connection.close()


# --------------------------------------------------------------------------
# helpers -- the smallest legal row of each kind
# --------------------------------------------------------------------------


def add_run(cp, run_id: str = "run-1", at: int = T0) -> str:
    cp.execute(
        "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
        (run_id, "running", at, at),
    )
    return run_id


def add_repo(cp, repo_id: str = "repo-a", owner: str = "aainc", name: str = "renga",
             provider_repo_id: str | None = "R_kgDO0001", at: int = T0) -> str:
    return upsert_repository(
        cp,
        repo_id=repo_id,
        owner=owner,
        name=name,
        provider_repo_id=provider_repo_id,
        now_ms=at,
    )


def add_scope(cp, scope_id: str, *, repo_id: str, pr_id: str, at: int = T0,
              retired_at_ms: int | None = None) -> str:
    cp.execute(
        "INSERT INTO watcher_scope (scope_id, scope_kind, repo_id, pr_id, expected_interval_ms,"
        " enabled, registered_at_ms, retired_at_ms) VALUES (?, 'ci_pull_request', ?, ?, ?, 1,"
        " ?, ?)",
        (scope_id, repo_id, pr_id, 60_000, at, retired_at_ms),
    )
    return scope_id


def observe(cp, *, repo_id: str, pr_number: int, head_sha: str = SHA_A, state: str = "open",
            at: int, event_id: str | None = None, **kwargs) -> ObservedPullRequest:
    """One provider observation, with the fields the state implies filled in."""

    if state == "merged":
        kwargs.setdefault("merge_commit_sha", SHA_MERGE)
        kwargs.setdefault("merged_at_ms", at)
        kwargs.setdefault("closed_at_ms", at)
    elif state == "closed":
        kwargs.setdefault("closed_at_ms", at)
    return observe_pull_request(
        cp,
        repo_id=repo_id,
        pr_number=pr_number,
        head_sha=head_sha,
        state=state,
        observed_at_ms=at,
        ingested_at_ms=at,
        event_id=event_id or f"evt-{repo_id}-{pr_number}-{at}",
        producer="gh-watcher",
        **kwargs,
    )


def rows(cp, sql: str, params: tuple = ()) -> list[dict]:
    cursor = cp.execute(sql, params)
    cursor.row_factory = sqlite3.Row
    return [dict(row) for row in cursor.fetchall()]


# --------------------------------------------------------------------------
# the cardinality decision (section 7.3)
# --------------------------------------------------------------------------


def test_one_run_may_hold_several_pull_requests_across_repositories(cp) -> None:
    run = add_run(cp)
    left = add_repo(cp, "repo-a", "aainc", "renga", "R_left")
    right = add_repo(cp, "repo-b", "aainc", "claude-org-ja", "R_right")
    a = observe(cp, repo_id=left, pr_number=302, at=T0).pr_id
    b = observe(cp, repo_id=right, pr_number=302, at=T0 + 1).pr_id
    assert a != b

    link_run_pr(cp, run_id=run, pr_id=a, role="primary",
                resolution="project_registry", linked_at_ms=T0 + 2)
    link_run_pr(cp, run_id=run, pr_id=b, role="supporting",
                resolution="explicit_operator", linked_at_ms=T0 + 3)

    linked = rows(cp, "SELECT pr_id, role FROM run_pr_link WHERE run_id = ? ORDER BY pr_id", (run,))
    assert linked == [
        {"pr_id": a, "role": "primary"},
        {"pr_id": b, "role": "supporting"},
    ]


def test_one_pull_request_may_be_touched_by_several_runs(cp) -> None:
    first = add_run(cp, "run-1")
    second = add_run(cp, "run-2")
    repo = add_repo(cp)
    pr = observe(cp, repo_id=repo, pr_number=7, at=T0).pr_id

    link_run_pr(cp, run_id=first, pr_id=pr, role="primary",
                resolution="project_registry", linked_at_ms=T0 + 1)
    link_run_pr(cp, run_id=second, pr_id=pr, role="primary",
                resolution="project_registry", linked_at_ms=T0 + 2)

    holders = rows(cp, "SELECT run_id FROM run_pr_link WHERE pr_id = ? ORDER BY run_id", (pr,))
    assert [row["run_id"] for row in holders] == [first, second]


def test_exactly_one_link_per_run_is_primary_at_a_time(cp) -> None:
    run = add_run(cp)
    repo = add_repo(cp)
    first = observe(cp, repo_id=repo, pr_number=1, at=T0).pr_id
    second = observe(cp, repo_id=repo, pr_number=2, at=T0 + 1).pr_id

    link_run_pr(cp, run_id=run, pr_id=first, role="primary",
                resolution="project_registry", linked_at_ms=T0 + 2)
    with pytest.raises(RunPrLinkRefused):
        link_run_pr(cp, run_id=run, pr_id=second, role="primary",
                    resolution="project_registry", linked_at_ms=T0 + 3)

    # A supporting link alongside the primary is not the constrained case.
    link_run_pr(cp, run_id=run, pr_id=second, role="supporting",
                resolution="project_registry", linked_at_ms=T0 + 4)
    assert primary_link(cp, run_id=run)["pr_id"] == first


def test_a_repointed_run_keeps_both_links_as_history(cp) -> None:
    run = add_run(cp)
    repo = add_repo(cp)
    abandoned = observe(cp, repo_id=repo, pr_number=1, at=T0).pr_id
    adopted = observe(cp, repo_id=repo, pr_number=2, at=T0 + 1).pr_id

    link_run_pr(cp, run_id=run, pr_id=abandoned, role="primary",
                resolution="project_registry", linked_at_ms=T0 + 2)
    unlink_run_pr(cp, run_id=run, pr_id=abandoned, unlinked_at_ms=T0 + 3,
                  unlink_reason="superseded by a rebased pull request")
    link_run_pr(cp, run_id=run, pr_id=adopted, role="primary",
                resolution="explicit_operator", linked_at_ms=T0 + 4)

    assert primary_link(cp, run_id=run)["pr_id"] == adopted
    history = rows(
        cp,
        "SELECT pr_id, unlinked_at_ms, unlink_reason FROM run_pr_link"
        " WHERE run_id = ? ORDER BY linked_at_ms",
        (run,),
    )
    assert history == [
        {
            "pr_id": abandoned,
            "unlinked_at_ms": T0 + 3,
            "unlink_reason": "superseded by a rebased pull request",
        },
        {"pr_id": adopted, "unlinked_at_ms": None, "unlink_reason": None},
    ]


def test_an_unlink_records_a_reason_or_does_not_happen(cp) -> None:
    run = add_run(cp)
    repo = add_repo(cp)
    pr = observe(cp, repo_id=repo, pr_number=1, at=T0).pr_id
    link_run_pr(cp, run_id=run, pr_id=pr, role="primary",
                resolution="project_registry", linked_at_ms=T0 + 1)

    with pytest.raises(ValueError):
        unlink_run_pr(cp, run_id=run, pr_id=pr, unlinked_at_ms=T0 + 2, unlink_reason="")
    with pytest.raises(RunPrLinkRefused):
        unlink_run_pr(cp, run_id=run, pr_id="pr-that-was-never-linked",
                      unlinked_at_ms=T0 + 2, unlink_reason="typo")
    assert primary_link(cp, run_id=run) is not None


# --------------------------------------------------------------------------
# the 2026-08-06 regression (sections 7.1 and 7.4)
# --------------------------------------------------------------------------


def test_resolution_has_no_value_meaning_we_guessed_from_the_working_directory(cp) -> None:
    """Asserted twice: against the CHECK, and against the API's own signature."""

    assert repo_link.RESOLUTIONS == (
        "project_registry",
        "explicit_operator",
        "provider_event",
    )

    run = add_run(cp)
    repo = add_repo(cp)
    pr = observe(cp, repo_id=repo, pr_number=1, at=T0).pr_id
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute(
            "INSERT INTO run_pr_link (run_id, pr_id, role, resolution, linked_at_ms)"
            " VALUES (?, ?, 'primary', 'cwd_default', ?)",
            (run, pr, T0 + 1),
        )
    with pytest.raises(RunPrLinkRefused):
        link_run_pr(cp, run_id=run, pr_id=pr, role="primary",
                    resolution="cwd_default", linked_at_ms=T0 + 1)

    # And there is no argument through which a working directory could arrive.
    forbidden = ("cwd", "dir", "path", "default", "current", "fallback")
    for function in (resolve_repository, upsert_repository, link_run_pr, observe_pull_request):
        for parameter in inspect.signature(function).parameters:
            assert not any(word in parameter for word in forbidden), (function, parameter)


def test_an_unresolvable_repository_fails_to_link_rather_than_defaulting(cp) -> None:
    """The 2026-08-06 regression: nothing is guessed, and nothing is written.

    On that date the resolution of an omitted repository fell back to the cwd's,
    renga PR #302 was recorded with claude-org-ja PR #302's facts, and the tool
    exited ``ok``. Here the home repository exists and holds a PR of the very
    same number, which is the condition under which the incident corrupted
    silently rather than failing loudly -- and the resolution still refuses.
    """

    add_run(cp)
    home = add_repo(cp, "repo-home", "aainc", "claude-org-ja", "R_home")
    observe(cp, repo_id=home, pr_number=302, at=T0)

    with pytest.raises(RepoResolutionError):
        resolve_repository(cp, owner="aainc", name="renga")
    with pytest.raises(RepoResolutionError):
        resolve_repository(cp, provider_repo_id="R_renga")
    with pytest.raises(RepoResolutionError):
        resolve_repository(cp)

    assert rows(cp, "SELECT run_id FROM run_pr_link") == []
    assert resolve_repository(cp, owner="aainc", name="claude-org-ja") == home


def test_a_rename_is_absorbed_on_the_existing_row_and_observations_stay_attached(cp) -> None:
    repo = add_repo(cp, "repo-a", "aainc", "renga", "R_stable", at=T0)
    pr = observe(cp, repo_id=repo, pr_number=302, at=T0 + 1).pr_id

    absorbed = upsert_repository(
        cp,
        repo_id="repo-a-would-be-new",
        owner="aainc-labs",
        name="renga-next",
        provider_repo_id="R_stable",
        now_ms=T0 + 2,
    )

    assert absorbed == repo
    assert len(rows(cp, "SELECT repo_id FROM repository")) == 1
    assert rows(cp, "SELECT owner, name, updated_at_ms FROM repository") == [
        {"owner": "aainc-labs", "name": "renga-next", "updated_at_ms": T0 + 2}
    ]
    assert rows(cp, "SELECT repo_id FROM pull_request WHERE pr_id = ?", (pr,)) == [
        {"repo_id": repo}
    ]
    assert resolve_repository(cp, owner="aainc-labs", name="renga-next") == repo


def test_a_slug_matches_case_insensitively_while_the_columns_keep_their_case(cp) -> None:
    repo = add_repo(cp, "repo-a", "AAInc", "Renga", "R_case")

    assert rows(cp, "SELECT owner, name FROM repository") == [{"owner": "AAInc", "name": "Renga"}]
    assert resolve_repository(cp, owner="aainc", name="renga") == repo
    assert resolve_repository(cp, owner="AAINC", name="RENGA") == repo
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute(
            "INSERT INTO repository (repo_id, provider, provider_repo_id, owner, name,"
            " created_at_ms, updated_at_ms) VALUES ('repo-dup', 'github', NULL, 'aainc',"
            " 'renga', ?, ?)",
            (T0, T0),
        )


def test_a_repo_id_is_never_reassigned_to_another_repository(cp) -> None:
    add_repo(cp, "repo-a", "aainc", "renga", "R_left")
    with pytest.raises(RepoResolutionError):
        upsert_repository(cp, repo_id="repo-a", owner="aainc", name="other",
                          provider_repo_id="R_right", now_ms=T0 + 1)


# --------------------------------------------------------------------------
# the pull-request projection (section 7.2)
# --------------------------------------------------------------------------


def test_a_recreated_pull_request_is_a_new_row_and_the_old_row_survives(cp) -> None:
    repo = add_repo(cp)
    old = observe(cp, repo_id=repo, pr_number=11, at=T0).pr_id
    observe(cp, repo_id=repo, pr_number=11, head_sha=SHA_A, state="closed", at=T0 + 1)
    new = observe(cp, repo_id=repo, pr_number=12, head_sha=SHA_B, at=T0 + 2).pr_id

    assert old != new
    assert rows(
        cp, "SELECT pr_number, state, head_sha FROM pull_request ORDER BY pr_number"
    ) == [
        {"pr_number": 11, "state": "closed", "head_sha": SHA_A},
        {"pr_number": 12, "state": "open", "head_sha": SHA_B},
    ]


def test_a_head_move_records_the_event_that_moved_it(cp) -> None:
    repo = add_repo(cp)
    first = observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, at=T0)
    second = observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_B, at=T0 + 1)

    assert first.event_type == "pr_head_updated"
    assert second.event_type == "pr_head_updated" and second.head_moved
    projected = rows(cp, "SELECT head_sha, head_observed_at_ms, head_event_seq FROM pull_request")
    assert projected == [
        {"head_sha": SHA_B, "head_observed_at_ms": T0 + 1, "head_event_seq": second.event.seq}
    ]
    assert second.event.seq > first.event.seq


def test_a_late_older_head_observation_is_refused_as_evidence_not_a_projection(cp) -> None:
    repo = add_repo(cp)
    observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_B, at=T0 + 10)
    with pytest.raises(PullRequestObservationRefused):
        observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, at=T0 + 5)
    assert rows(cp, "SELECT head_sha FROM pull_request") == [{"head_sha": SHA_B}]


@pytest.mark.parametrize(
    "state, extra",
    [
        ("merged", {"merge_commit_sha": SHA_MERGE}),
        ("closed", {}),
        ("open", {}),  # the reopen, from a prior closed
    ],
)
def test_a_late_older_head_is_refused_by_name_on_every_transition(cp, state, extra) -> None:
    """The staleness of a head is a property of the head, not of the transition.

    ``_plan`` names the transition most-consequential-first, so a delayed merge,
    close or reopen that also carries an older, different ``head_sha`` used to
    return before the head-order test the bare ``pr_head_updated`` branch makes.
    The ``pull_request_head_is_monotonic`` trigger (migrations/0001_initial.sql)
    still refuses the write, so nothing stale ever landed -- but the caller got a
    raw ``IntegrityError`` saying only that *some* constraint failed, which is
    precisely the loss of naming :class:`PullRequestObservationRefused` exists to
    prevent, and the docstring's contract promises the name "for a head move the
    provider's own order does not support" on every transition alike.
    """

    repo = add_repo(cp)
    observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, at=T0 + 10)
    if state == "open":  # a reopen is only reachable from a prior close
        observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, state="closed", at=T0 + 20)
    before_state = rows(cp, "SELECT state, head_sha, head_observed_at_ms FROM pull_request")
    before_events = rows(cp, "SELECT seq FROM event")

    with pytest.raises(PullRequestObservationRefused):
        observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_B, state=state, at=T0 + 5,
                event_id="evt-late", **extra)

    # Nothing of the observation survives: not the stale head, not the newer
    # head_observed_at_ms it would have been paired with, not the event.
    assert rows(cp, "SELECT state, head_sha, head_observed_at_ms FROM pull_request") == before_state
    assert rows(cp, "SELECT seq FROM event") == before_events


def test_a_no_change_observation_appends_nothing(cp) -> None:
    repo = add_repo(cp)
    observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, at=T0)
    before = rows(cp, "SELECT seq FROM event")

    repeat = observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, at=T0 + 60_000)

    assert repeat.changed is False and repeat.event is None
    assert rows(cp, "SELECT seq FROM event") == before
    assert rows(cp, "SELECT head_observed_at_ms FROM pull_request") == [
        {"head_observed_at_ms": T0}
    ]


def test_a_reopen_clears_closed_at_ms_and_unretires_the_watcher_scope(cp) -> None:
    repo = add_repo(cp)
    pr = observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, at=T0).pr_id
    scope = add_scope(cp, "scope-1", repo_id=repo, pr_id=pr, at=T0)
    observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, state="closed", at=T0 + 1)
    cp.execute("UPDATE watcher_scope SET retired_at_ms = ? WHERE scope_id = ?", (T0 + 1, scope))

    reopened = observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, state="open", at=T0 + 2)

    assert reopened.event_type == "pr_reopened" and reopened.reactivated_scopes == (scope,)
    assert rows(cp, "SELECT state, closed_at_ms FROM pull_request") == [
        {"state": "open", "closed_at_ms": None}
    ]
    # Section 7.2: without the scope the pull request is watched in name only.
    assert rows(cp, "SELECT retired_at_ms FROM watcher_scope") == [{"retired_at_ms": None}]


def test_a_merged_pull_request_does_not_reopen(cp) -> None:
    repo = add_repo(cp)
    observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, at=T0)
    merged = observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, state="merged", at=T0 + 1)
    assert merged.event_type == "pr_merged"

    with pytest.raises(PullRequestObservationRefused):
        observe(cp, repo_id=repo, pr_number=1, head_sha=SHA_A, state="open", at=T0 + 2)
    with pytest.raises(sqlite3.IntegrityError):
        cp.execute("UPDATE pull_request SET state = 'open' WHERE pr_number = 1")
    assert rows(cp, "SELECT state, merged_at_ms FROM pull_request") == [
        {"state": "merged", "merged_at_ms": T0 + 1}
    ]


def test_an_observation_whose_projection_fails_leaves_no_event_behind(cp) -> None:
    """Section 5.4's all-or-nothing, reached through this module's side effect."""

    with pytest.raises(sqlite3.IntegrityError):
        observe(cp, repo_id="repo-that-does-not-exist", pr_number=1, at=T0)
    assert rows(cp, "SELECT seq FROM event") == []
    assert rows(cp, "SELECT pr_id FROM pull_request") == []


def test_a_merge_observed_without_its_merge_commit_is_refused_by_name(cp) -> None:
    repo = add_repo(cp)
    with pytest.raises(PullRequestObservationRefused):
        observe_pull_request(
            cp,
            repo_id=repo,
            pr_number=1,
            head_sha=SHA_A,
            state="merged",
            observed_at_ms=T0,
            ingested_at_ms=T0,
            event_id="evt-1",
            producer="gh-watcher",
            merged_at_ms=T0,
            closed_at_ms=T0,
        )
    assert rows(cp, "SELECT seq FROM event") == []
