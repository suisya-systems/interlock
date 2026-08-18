"""The path audit, and the negative build test gate item 9 asks for by name.

Two halves, and the second is the one that matters:

* :func:`test_no_path_reaches_skill_material_outside_the_gate` runs the audit
  over the real package. It is green today and turns red the moment somebody
  adds a second route to skill material -- that is the "negative test fails the
  build if such a path is added later" criterion.
* The positive controls below prove the audit is not vacuous. A detector that
  can never fire would keep the first test green forever, which is exactly the
  failure mode the criterion is guarding against. Each control builds a
  synthetic package containing one specific bypass and asserts the audit
  reports it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import claude_org_runtime
from claude_org_runtime.curator import audit

PACKAGE_ROOT = Path(claude_org_runtime.__file__).resolve().parent


def rules(findings) -> set[str]:
    return {finding.rule for finding in findings}


# -- the audit over the real tree ----------------------------------------


def test_no_path_reaches_skill_material_outside_the_gate():
    findings = audit.audit_tree(PACKAGE_ROOT)
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_the_gate_is_the_only_importer_of_the_skill_root_module():
    """Stated separately from the audit so the failure message says which
    property broke, and so the property survives an audit refactor."""

    importers = [
        path.relative_to(PACKAGE_ROOT).as_posix()
        for path in PACKAGE_ROOT.rglob("*.py")
        if "import skill_root" in path.read_text(encoding="utf-8")
        or "from .skill_root" in path.read_text(encoding="utf-8")
    ]
    assert importers == [audit.GATE_MODULE]


def test_the_audit_cli_reports_a_clean_tree(capsys):
    assert audit.main([str(PACKAGE_ROOT)]) == 0
    assert "clean" in capsys.readouterr().out


# -- positive controls: the audit can actually fire ----------------------


def build_package(tmp_path: Path, modules: dict[str, str]) -> Path:
    """Write a synthetic package with the same layout the real audit expects."""

    root = tmp_path / "pkg"
    base = {
        "__init__.py": "",
        "curator/__init__.py": "",
        "curator/skill_root.py": "SKILL_DIRNAME = 'skills'\n",
        "curator/gate.py": "from .skill_root import SkillRoot\n",
        "curator/ledger.py": "",
        "curator/stub.py": "",
    }
    base.update(modules)
    for name, source in base.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def audit_synthetic(root: Path):
    return audit.audit_tree(
        root,
        writer_allowlist={
            "curator/gate.py": "the gate",
            "curator/ledger.py": "the ledger",
            "curator/stub.py": "the candidate store",
        },
        reader_allowlist={},
    )


def test_control_clean_synthetic_package_has_no_findings(tmp_path):
    """Baseline: without an injected bypass the same audit is silent, so the
    controls below are detecting the bypass and not the scaffolding."""

    assert audit_synthetic(build_package(tmp_path, {})) == []


def test_control_second_importer_of_the_skill_root_is_a_finding(tmp_path):
    root = build_package(
        tmp_path,
        {"curator/reflector.py": "from .skill_root import SkillRoot\n"},
    )
    findings = audit_synthetic(root)
    assert "skill-root-reference-outside-gate" in rules(findings)
    assert any(f.module == "curator/reflector.py" for f in findings)


def test_control_hardcoded_skill_path_is_a_finding(tmp_path):
    root = build_package(
        tmp_path,
        {"curator/reflector.py": "TARGET = '.claude/skills'\n"},
    )
    assert "hardcoded-skill-path" in rules(audit_synthetic(root))


def test_control_writing_through_a_skill_path_constant_is_a_finding(tmp_path):
    """The realistic bypass: name the directory once, write to it elsewhere."""

    root = build_package(
        tmp_path,
        {
            "promoter.py": """
                from pathlib import Path

                SKILLS = '.claude/skills'

                def publish(name, body):
                    Path(SKILLS, name, 'SKILL.md').write_text(body)
                """
        },
    )
    findings = audit_synthetic(root)
    assert "skill-path-write" in rules(findings)


def test_control_skill_path_write_is_reported_even_for_an_allowlisted_reader(tmp_path):
    """The reader allowlist permits *naming* skill material, never writing to
    it -- otherwise the allowlist would itself be the bypass."""

    root = build_package(
        tmp_path,
        {
            "reader.py": """
                from pathlib import Path

                TEMPLATE = '.claude/skills/org-delegate/references/t.md'

                def read_it():
                    return Path(TEMPLATE).read_text()

                def write_it(body):
                    Path(TEMPLATE).write_text(body)
                """
        },
    )
    findings = audit.audit_tree(
        root,
        writer_allowlist={
            "curator/gate.py": "the gate",
            "curator/ledger.py": "the ledger",
            "curator/stub.py": "the candidate store",
        },
        reader_allowlist={"reader.py": "read-only consumer"},
    )
    assert rules(findings) == {"skill-path-write"}


def test_control_new_writer_in_the_curator_package_is_a_finding(tmp_path):
    """A new module that writes anything at all inside the curator package is
    a finding until a human allowlists it and says what store it writes to."""

    root = build_package(
        tmp_path,
        {
            "curator/exporter.py": """
                from pathlib import Path

                def export(destination, body):
                    Path(destination).write_text(body)
                """
        },
    )
    findings = audit_synthetic(root)
    assert "unallowlisted-writer" in rules(findings)
    assert any(f.module == "curator/exporter.py" for f in findings)


def test_control_open_in_write_mode_is_a_finding(tmp_path):
    root = build_package(
        tmp_path,
        {
            "curator/exporter.py": """
                def export(destination, body):
                    with open(destination, 'w') as handle:
                        handle.write(body)
                """
        },
    )
    assert "unallowlisted-writer" in rules(audit_synthetic(root))


def test_control_open_with_a_dynamic_mode_is_treated_as_a_write(tmp_path):
    """Fail closed: a mode the audit cannot read statically is assumed to
    write, so `open(path, mode)` is not a way to hide a promotion."""

    root = build_package(
        tmp_path,
        {
            "curator/exporter.py": """
                def export(destination, mode):
                    return open(destination, mode)
                """
        },
    )
    assert "unallowlisted-writer" in rules(audit_synthetic(root))


def test_control_reading_inside_the_curator_package_is_not_a_finding(tmp_path):
    """The audit must not be so noisy that the allowlist becomes a rubber
    stamp: plain reads are silent."""

    root = build_package(
        tmp_path,
        {
            "curator/reader.py": """
                from pathlib import Path

                def load(path):
                    with open(path) as handle:
                        return handle.read() + Path(path).read_text()
                """
        },
    )
    assert audit_synthetic(root) == []


def test_control_renaming_the_gate_module_is_a_finding(tmp_path):
    """A stale allowlist must be loud. Silently allowlisting a module that no
    longer exists would turn the audit into a no-op."""

    root = build_package(tmp_path, {})
    (root / "curator" / "gate.py").rename(root / "curator" / "gate2.py")

    findings = audit_synthetic(root)
    assert "stale-allowlist" in rules(findings)


def test_control_audit_cli_exits_non_zero_on_a_finding(tmp_path, capsys):
    root = build_package(
        tmp_path,
        {"curator/reflector.py": "from .skill_root import SkillRoot\n"},
    )
    assert audit.main([str(root)]) == 1
    assert "skill-root-reference-outside-gate" in capsys.readouterr().out


@pytest.mark.parametrize(
    "module",
    [audit.SKILL_ROOT_MODULE, audit.GATE_MODULE, *audit.WRITER_ALLOWLIST],
)
def test_every_allowlisted_module_exists(module):
    assert (PACKAGE_ROOT / module).is_file()


@pytest.mark.parametrize("module", list(audit.SKILL_PATH_READERS))
def test_every_allowlisted_reader_exists_and_is_justified(module):
    assert (PACKAGE_ROOT / module).is_file()
    assert audit.SKILL_PATH_READERS[module].strip()
