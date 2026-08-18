"""Where skill material lives, and why that makes the write the gate.

U8 asked whether skills, plugins and settings are re-read by an already-running
session, or bound once at session start. Answered 2026-08-18 against Claude Code
2.1.234, by documentation search *and* by a direct runtime probe -- the full
transcript is ``investigation/u8-skill-hot-reload-probe.md``. The answer for
skills is **hot-reload**, on all three counts the probe could distinguish:

* an edited ``SKILL.md`` body is served from disk to a *running* session;
* an edited ``description`` reaches that session's skill listing;
* a skill directory created after session start is invocable, even while it is
  still missing from the listing the session reports.

The consequence stated in gate item 9: *writing a file into one of these
directories already is promotion*. A gate placed at a promotion function would
pass all five negatives and guard nothing, because the file is live the moment
it is on disk. So the gate lives at the filesystem write, and this module exists
to make "skill material" a named, greppable thing that the path audit can
enforce sole access to.

Only :mod:`claude_org_runtime.curator.gate` may import this module. That rule is
enforced by :mod:`claude_org_runtime.curator.audit` and by
``tests/curator/test_path_audit.py``, which fails the build if a second
importer appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The directory name Claude Code watches, relative to a project root, a home
# directory, or any directory passed with ``--add-dir``.
SKILL_DIRNAME = "skills"
CLAUDE_DIRNAME = ".claude"

#: Recorded for the gate record: which directories are live skill material for an
#: already-running session (gate item 9's U8 acceptance criterion). Each entry is
#: relative to the base named by the key.
LIVE_SKILL_MATERIAL = {
    "project": f"{CLAUDE_DIRNAME}/{SKILL_DIRNAME}",
    "home": f"{CLAUDE_DIRNAME}/{SKILL_DIRNAME}",
    "added_dir": f"{CLAUDE_DIRNAME}/{SKILL_DIRNAME}",
}


@dataclass(frozen=True)
class SkillRoot:
    """A directory whose contents a running session may pick up at any moment."""

    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).resolve())

    def resolve_target(self, target: str) -> Path:
        """Resolve ``target`` inside this root, refusing any escape from it.

        Raises :class:`ValueError` for absolute targets, ``..`` traversal, and
        targets whose parent chain leaves the root through a symlink.
        """

        if not target or target != target.strip():
            raise ValueError(f"empty or untrimmed target: {target!r}")
        candidate = Path(target)
        if candidate.is_absolute() or candidate.drive or candidate.root:
            raise ValueError(f"target must be relative to the skill root: {target!r}")
        if any(part == ".." for part in candidate.parts):
            raise ValueError(f"target must not traverse upwards: {target!r}")

        resolved = (self.path / candidate).resolve()
        if resolved != self.path and self.path not in resolved.parents:
            raise ValueError(f"target escapes the skill root: {target!r}")
        return resolved


def skill_root_for_project(project_dir: Path) -> SkillRoot:
    """The live skill directory of a project checkout."""

    return SkillRoot(Path(project_dir) / CLAUDE_DIRNAME / SKILL_DIRNAME)
