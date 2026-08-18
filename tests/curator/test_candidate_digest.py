"""The digest has to name a *version*, not just some content.

Gate item 9's fourth negative only works if the digest changes whenever the
candidate does -- including the changes that leave every existing byte intact.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.curator.digest import (
    candidate_digest,
    content_digest,
    digest_tree,
    read_tree,
)


def test_digest_is_algorithm_prefixed():
    assert content_digest(b"x").startswith("sha256:")
    assert candidate_digest({"a": b"x"}).startswith("sha256:")


def test_candidate_digest_is_stable_and_order_independent():
    first = candidate_digest({"a.md": b"one", "b.md": b"two"})
    second = candidate_digest({"b.md": b"two", "a.md": b"one"})
    assert first == second


def test_candidate_digest_differs_from_a_bare_content_digest():
    """Domain separation: a one-file candidate is not the same object as the
    file's bytes, so the two digests must not be interchangeable."""

    assert candidate_digest({"SKILL.md": b"body"}) != content_digest(b"body")


@pytest.mark.parametrize(
    "mutated",
    [
        {"SKILL.md": b"body!"},  # content changed
        {"SKILL.MD": b"body"},  # path renamed
        {"SKILL.md": b"body", "extra.md": b""},  # file added, even an empty one
        {},  # file removed
    ],
)
def test_any_change_to_the_tree_changes_the_digest(mutated):
    original = candidate_digest({"SKILL.md": b"body"})
    assert candidate_digest(mutated) != original


def test_length_prefixing_defeats_path_content_reshuffling():
    """Without length prefixes these two trees would serialize identically."""

    left = candidate_digest({"ab": b"c"})
    right = candidate_digest({"a": b"bc"})
    assert left != right


def test_digest_tree_reads_the_directory_as_it_is_now(tmp_path):
    root = tmp_path / "candidate"
    (root / "nested").mkdir(parents=True)
    (root / "SKILL.md").write_bytes(b"v1")
    (root / "nested" / "ref.md").write_bytes(b"ref")

    before = digest_tree(root)
    assert read_tree(root) == {"SKILL.md": b"v1", "nested/ref.md": b"ref"}

    (root / "SKILL.md").write_bytes(b"v2")
    assert digest_tree(root) != before


def test_symlinked_candidate_content_is_refused(tmp_path):
    """A candidate that can point outside itself is not an immutable snapshot,
    and the gate refuses to digest one rather than following the link."""

    root = tmp_path / "candidate"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"elsewhere")
    (root / "SKILL.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        digest_tree(root)
