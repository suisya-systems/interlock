"""Content digests that name an immutable candidate version.

The approval record names *what was approved*, not merely *that something was
approved*. That distinction is the whole point of gate item 9's fourth negative
(candidate mutated after approval): an approval record which merely exists is
satisfied by any bytes at all.

The digest is taken over the candidate's whole file tree -- every relative path
and every byte -- so that renaming a file inside the candidate, adding a file, or
changing one byte all produce a different version name.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Mapping

# Domain separation: a candidate digest must never collide with a bare file
# digest of the same bytes.
_TREE_PREFIX = b"interlock.curator.candidate.v1\n"
_ALGORITHM = "sha256"


def content_digest(data: bytes) -> str:
    """Digest of a single blob, as ``"sha256:<hex>"``."""

    return f"{_ALGORITHM}:{hashlib.sha256(data).hexdigest()}"


def candidate_digest(files: Mapping[str, bytes]) -> str:
    """Digest of a candidate given as ``{relative posix path: bytes}``.

    The encoding is length-prefixed so that no combination of path names and
    contents can be re-partitioned into a different tree with the same digest.
    """

    hasher = hashlib.sha256()
    hasher.update(_TREE_PREFIX)
    for relpath in sorted(files):
        payload = files[relpath]
        name = relpath.encode("utf-8")
        hasher.update(f"{len(name)}\n".encode("ascii"))
        hasher.update(name)
        hasher.update(f"{len(payload)}\n".encode("ascii"))
        hasher.update(payload)
    return f"{_ALGORITHM}:{hasher.hexdigest()}"


def read_tree(root: Path) -> dict[str, bytes]:
    """Read ``root`` into ``{relative posix path: bytes}``.

    Symlinks are refused rather than followed: a candidate that can point
    outside itself is not an immutable snapshot of anything.
    """

    root = Path(root)
    files: dict[str, bytes] = {}
    for path in _walk(root):
        if path.is_symlink():
            raise ValueError(f"candidate contains a symlink: {path}")
        relative = path.relative_to(root).as_posix()
        files[relative] = path.read_bytes()
    return files


def digest_tree(root: Path) -> str:
    """Digest the candidate directory ``root`` as it exists on disk *now*."""

    return candidate_digest(read_tree(root))


def _walk(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        yield path
