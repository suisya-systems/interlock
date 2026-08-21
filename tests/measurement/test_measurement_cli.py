"""The command's three mechanisms, each tested where it could actually break.

**It cannot acquire a writable handle.** Asserting that would be easy to fake --
a test that patched ``open_for_measurement`` and asserted it was called proves
only that one code path used it, and says nothing about the error path, the
fixture loader, or a future edit that opens a second connection to "just check
something". So :func:`test_the_command_opens_no_writable_connection` patches
``sqlite3.connect`` itself for the whole run and asserts that **every** connect
the process made was a ``mode=ro`` URI opened with ``uri=True``. It is an
assertion about the call path, and a writable handle opened anywhere under the
command fails it no matter who opened it.

**It reads the clock once.** ``time.time`` is replaced by a counter, and the
count is asserted: exactly one read without ``--now-ms``, and **zero** with it.
A module that read a clock below the boundary would still produce a plausible
report -- with the cohort selected at one instant and the header stamped at
another -- and only the count catches that.

**Its help text survives a cp932 console.** Two tests, because the two ways this
breaks are different: every help string is encoded to cp932 in-process (which
catches the string), and ``--help`` is run in a real subprocess with
``PYTHONIOENCODING=cp932`` (which catches the *stream*, the thing
``redirect_stdout`` cannot see because pytest captures UTF-8).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from claude_org_runtime import cli as top_level_cli
from claude_org_runtime.control_plane.migrator import (
    create_production_control_plane,
)
from claude_org_runtime.measurement import cli as measurement_cli
from claude_org_runtime.measurement.provenance import FINGERPRINT_AGGREGATE
from claude_org_runtime.measurement.reader import ControlPlaneRefusal

from .test_render import (
    GENERATED_AT,
    PERIOD_END,
    PERIOD_START,
    T0,
    VERDICT_WORDS,
    parse_markdown,
    walk_json,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    cp = sqlite3.connect(path, isolation_level=None)
    try:
        cp.execute("PRAGMA foreign_keys = ON")
        cp.execute(
            "INSERT INTO run (run_id, status, created_at_ms, updated_at_ms)"
            " VALUES ('run-1', 'completed', ?, ?)",
            (PERIOD_START + 1_000, PERIOD_START + 2_000),
        )
    finally:
        cp.close()
    return path


def argv_for(db: Path, *extra: str) -> list[str]:
    return [
        "report",
        "--db",
        str(db),
        "--period-start-ms",
        str(PERIOD_START),
        "--period-end-ms",
        str(PERIOD_END),
        "--now-ms",
        str(GENERATED_AT),
        *extra,
    ]


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------


def test_the_subcommand_runs_end_to_end_and_renders_markdown(
    db: Path, capsys
) -> None:
    code = measurement_cli.main(argv_for(db))

    assert code == 0
    facts = parse_markdown(capsys.readouterr().out)
    assert facts["header.db_path"] == str(db)
    assert facts["header.period_start_ms"] == str(PERIOD_START)
    assert facts["header.generated_at_ms"] == str(GENERATED_AT)
    assert facts["sections.ac9.facts.cohort.denominator"] == "1"


def test_the_json_rendering_carries_the_same_facts_from_the_command(
    db: Path, capsys
) -> None:
    """The two renderings of one command invocation, compared to each other."""

    measurement_cli.main(argv_for(db))
    from_markdown = parse_markdown(capsys.readouterr().out)
    measurement_cli.main(argv_for(db, "--format", "json"))
    from_json = walk_json(json.loads(capsys.readouterr().out))

    assert from_markdown == from_json


def test_the_command_is_mounted_on_the_top_level_cli(db: Path, capsys) -> None:
    code = top_level_cli.main(["measure", *argv_for(db)])

    assert code == 0
    assert "interlock-measurement-report" in capsys.readouterr().out


def test_the_command_emits_no_verdict(db: Path, capsys) -> None:
    measurement_cli.main(argv_for(db))
    out = capsys.readouterr().out

    found = VERDICT_WORDS.findall(out)
    assert not found, f"verdict vocabulary on stdout: {sorted(set(found))}"


def test_aggregate_mode_is_stamped_weaker_on_the_command_output(
    db: Path, capsys
) -> None:
    measurement_cli.main(argv_for(db, "--fingerprint", FINGERPRINT_AGGREGATE))
    out = capsys.readouterr().out

    assert "does NOT establish identity of content" in out
    measurement_cli.main(argv_for(db))
    assert "does NOT establish identity of content" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# read-only by capability, asserted on the call path
# --------------------------------------------------------------------------


def test_the_command_opens_no_writable_connection(
    db: Path, capsys, monkeypatch
) -> None:
    opened: list[tuple[tuple, dict]] = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        opened.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    measurement_cli.main(argv_for(db))
    capsys.readouterr()

    assert opened, "the command opened no database at all"
    for args, kwargs in opened:
        assert kwargs.get("uri") is True, (args, kwargs)
        assert str(args[0]).endswith("?mode=ro"), args[0]


def test_the_command_module_imports_no_other_opener() -> None:
    """There is no ``sqlite3`` in this module to call.

    Belt to the previous test's braces: the recorder proves no writable handle
    was opened on the path a passing run takes, and this proves the module holds
    no opener that an error path or a later edit could reach.
    """

    assert not hasattr(measurement_cli, "sqlite3")
    assert measurement_cli.open_for_measurement.__module__.endswith(
        "measurement.reader"
    )


def test_a_database_the_reader_refuses_is_not_reported_over(tmp_path: Path) -> None:
    absent = tmp_path / "nothing.sqlite3"

    with pytest.raises(ControlPlaneRefusal):
        measurement_cli.main(argv_for(absent))


# --------------------------------------------------------------------------
# the clock is read once, at the boundary
# --------------------------------------------------------------------------


def test_the_clock_is_read_exactly_once_when_it_is_not_given(
    db: Path, capsys, monkeypatch
) -> None:
    reads: list[int] = []

    def counting_time() -> float:
        reads.append(1)
        return (PERIOD_END + 5_000) / 1000.0

    monkeypatch.setattr(measurement_cli.time, "time", counting_time)
    argv = [
        "report",
        "--db",
        str(db),
        "--period-start-ms",
        str(PERIOD_START),
        "--period-end-ms",
        str(PERIOD_END),
    ]
    measurement_cli.main(argv)

    facts = parse_markdown(capsys.readouterr().out)
    assert len(reads) == 1
    assert facts["header.generated_at_ms"] == str(PERIOD_END + 5_000)


def test_the_clock_is_not_read_at_all_when_it_is_given(
    db: Path, capsys, monkeypatch
) -> None:
    def refusing_time() -> float:
        raise AssertionError(
            "the command read the system clock even though --now-ms named one"
        )

    monkeypatch.setattr(measurement_cli.time, "time", refusing_time)
    measurement_cli.main(argv_for(db))

    assert capsys.readouterr().out


@pytest.mark.parametrize("value", [True, 1.5, "1700000000000"])
def test_a_clock_that_is_not_epoch_milliseconds_is_refused(
    db: Path, value
) -> None:
    """``True`` is an ``int`` in Python and would be the instant 1970-01-01T00:00:00.001Z."""

    args = argparse.Namespace(
        db=str(db),
        period_start_ms=PERIOD_START,
        period_end_ms=PERIOD_END,
        grace_ms=None,
        fingerprint="content",
        fixture_corpus=None,
        fixture_commit=None,
        v1_shadow_run_ids=None,
    )
    with pytest.raises(TypeError):
        measurement_cli.build_report_from_args(args, now_ms=value)


# --------------------------------------------------------------------------
# the per-report declarations
# --------------------------------------------------------------------------


def test_a_shadow_input_file_is_read_and_named(db: Path, tmp_path: Path, capsys) -> None:
    shadow = tmp_path / "v1.json"
    shadow.write_text(json.dumps(["run-9"]), encoding="utf-8")

    measurement_cli.main(argv_for(db, "--v1-shadow-run-ids", str(shadow)))

    facts = parse_markdown(capsys.readouterr().out)
    assert facts["sections.inputs.facts.v1_shadow.source"] == str(shadow)
    assert facts["sections.inputs.facts.v1_shadow.run_ids"] == "run-9"
    assert facts["header.coverage.excluded.v1_owned"] == "1"


def test_the_object_shape_of_the_shadow_file_is_accepted(
    db: Path, tmp_path: Path, capsys
) -> None:
    shadow = tmp_path / "v1.json"
    shadow.write_text(json.dumps({"run_ids": ["run-9"]}), encoding="utf-8")

    measurement_cli.main(argv_for(db, "--v1-shadow-run-ids", str(shadow)))

    assert (
        parse_markdown(capsys.readouterr().out)[
            "sections.inputs.facts.v1_shadow.run_ids"
        ]
        == "run-9"
    )


@pytest.mark.parametrize("payload", ["{}", "[1, 2]", '"run-9"'])
def test_an_unreadable_shadow_file_refuses_rather_than_becoming_an_empty_input(
    db: Path, tmp_path: Path, payload: str
) -> None:
    """The flattering answer here arrives as absent data, so it is refused."""

    shadow = tmp_path / "v1.json"
    shadow.write_text(payload, encoding="utf-8")

    with pytest.raises(ControlPlaneRefusal):
        measurement_cli.main(argv_for(db, "--v1-shadow-run-ids", str(shadow)))


def test_a_corpus_without_its_commit_is_refused(db: Path, tmp_path: Path) -> None:
    with pytest.raises(ControlPlaneRefusal):
        measurement_cli.main(
            argv_for(db, "--fixture-corpus", str(tmp_path / "corpus"))
        )
    with pytest.raises(ControlPlaneRefusal):
        measurement_cli.main(argv_for(db, "--fixture-commit", "c0ffee"))


def test_the_shipped_corpus_reaches_the_header(db: Path, capsys) -> None:
    corpus = REPO_ROOT / "tests" / "fixtures" / "labelled"

    measurement_cli.main(
        argv_for(
            db,
            "--fixture-corpus",
            str(corpus),
            "--fixture-commit",
            "c0ffee",
        )
    )

    facts = parse_markdown(capsys.readouterr().out)
    assert facts["header.fixture_suite_ref.commit"] == "c0ffee"
    assert facts["header.fixture_suite_ref.positive"] != "(none)"
    assert facts["header.fixture_suite_ref.negative"] != "(none)"


def test_a_declared_grace_reaches_the_report(db: Path, capsys) -> None:
    measurement_cli.main(argv_for(db, "--grace-ms", "4321"))

    facts = parse_markdown(capsys.readouterr().out)
    assert facts["sections.observation_window.facts.grace_ms"] == "4321"
    assert (
        facts["sections.observation_window.facts.grace_source"] == "declared"
    )


# --------------------------------------------------------------------------
# cp932
# --------------------------------------------------------------------------


def _help_strings(parser: argparse.ArgumentParser) -> list[str]:
    """Every help and description string reachable from *parser*."""

    found: list[str] = [parser.description or "", parser.prog or ""]
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no other way
        found.append(action.help or "")
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for choice in action.choices.values():
                found.extend(_help_strings(choice))
    return found


def test_every_help_string_encodes_to_cp932() -> None:
    """One em-dash in a help string is a UnicodeEncodeError on --help.

    ``pytest`` captures stdout as UTF-8 and cannot see it, which is why this is
    an encode assertion rather than an output assertion.
    """

    for text in _help_strings(measurement_cli.build_parser()):
        text.encode("cp932")
        assert text.isascii(), text


def test_the_help_of_the_mounted_subcommand_encodes_to_cp932() -> None:
    for text in _help_strings(top_level_cli.build_parser()):
        # The measure subtree is what this task added; the rest of the CLI is
        # not this test's to police, so only non-encodable text from the tree
        # this file mounts is asserted on.
        if "measure" in text or "measurement" in text or "fingerprint" in text:
            text.encode("cp932")


def test_help_runs_in_a_real_cp932_console() -> None:
    """The stream, not the string: run ``--help`` with a cp932 stdout for real."""

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONIOENCODING"] = "cp932"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "claude_org_runtime.cli",
            "measure",
            "report",
            "--help",
        ],
        capture_output=True,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "cp932", errors="replace"
    )
    assert b"--fingerprint" in completed.stdout
    completed.stdout.decode("cp932")
