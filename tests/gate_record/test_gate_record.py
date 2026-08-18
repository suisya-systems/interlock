"""Structural checks on the gate record (`docs/gate-record.md`), issue #24 / I-19.

The record is the artifact D-0022 requires: every gate item labelled either
"proven on the spike slice" or "re-proven on the real implementation". Later
issues fill it one item at a time, which is exactly the shape in which a row
goes quietly missing, a vocabulary drifts, or the scoped exception D-0022 grants
widens by one more item.

These tests are the durable half (D-0026). They check the record's *structure*,
never its verdicts: only a human puts a verdict in, and no test here can tell a
true one from a false one. What they can tell is that eleven items are present
and distinct, that the summary table and the per-item sections say the same
thing, that items 8 and 10 are not marked discharged before their named
discharge points, and that item 2's C1 failure has not been tidied away.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RECORD = Path(__file__).resolve().parents[2] / "docs" / "gate-record.md"

VERDICTS = {"discharged", "failed", "rehearsed — not discharged", "pending"}
#: D-0022's exception is scoped to these two items and nothing else may borrow it.
DEFERRED_ITEMS = (8, 10)
DEFERRED_VERDICT = "rehearsed — not discharged"
LABELS = {
    "proven on the spike slice",
    "re-proven on the real implementation",
    "n/a — failed",
    "pending",
}
PROVIDERS = {
    "C1 (Agent View)",
    "C2 (claude -p subprocesses)",
    "provider-independent",
    "pending",
}

SECTION_RE = re.compile(r"^### Item (\d+) — (.+)$", re.MULTILINE)
CODE_RE = re.compile(r"`([^`]+)`")


def _leading_tokens(value: str, vocabulary: set[str]) -> list[str]:
    """Backticked tokens from the start of a field, up to the first non-token.

    A field says its verdict first and its prose afterwards ("`failed` on C1;
    `pending` on C2"), so scanning stops at the first backticked span that is
    not part of the vocabulary — that span is a file name or a decision id, not
    a value.
    """
    tokens: list[str] = []
    for match in CODE_RE.finditer(value):
        if match.group(1) not in vocabulary:
            break
        tokens.append(match.group(1))
    return tokens


@pytest.fixture(scope="module")
def text() -> str:
    return RECORD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def table_rows(text: str) -> dict[int, list[str]]:
    """The §2 summary table, keyed by item number."""
    rows: dict[int, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 7 or not cells[0].isdigit():
            continue
        item = int(cells[0])
        # A second row for the same item would shadow the first, and every check
        # below would then inspect only the survivor.
        assert item not in rows, f"item {item} appears twice in the summary table"
        rows[item] = cells
    return rows


@pytest.fixture(scope="module")
def sections(text: str) -> dict[int, str]:
    """The §3 per-item sections, keyed by item number."""
    matches = list(SECTION_RE.finditer(text))
    out: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        item = int(match.group(1))
        assert item not in out, f"item {item} has two sections"
        out[item] = text[match.start() : end]
    return out


def _field(section: str, name: str) -> str:
    """One ``- **Name:** value`` line.

    The name may carry a parenthesised qualifier — item 2 splits its evidence
    into a C1 half and a C2 half, which is the shape a row takes when the same
    item has been attempted against two providers.
    """
    match = re.search(
        rf"^- \*\*{re.escape(name)}(?: \([^)]*\))?:\*\* (.+)$", section, re.MULTILINE
    )
    assert match, f"missing field {name!r}"
    return match.group(1)


def test_all_eleven_items_present_none_omitted_none_merged(table_rows, sections):
    assert sorted(table_rows) == list(range(1, 12))
    assert sorted(sections) == list(range(1, 12))


@pytest.mark.parametrize("item", range(1, 12))
def test_row_uses_the_closed_vocabularies(item, table_rows):
    verdict, label, provider = table_rows[item][2:5]
    for value, vocabulary in ((verdict, VERDICTS), (label, LABELS), (provider, PROVIDERS)):
        tokens = _leading_tokens(value, vocabulary)
        assert tokens, f"item {item}: no recognised value in {value!r}"


@pytest.mark.parametrize("item", range(1, 12))
def test_table_and_section_agree(item, table_rows, sections):
    """A row updated in one place and not the other is a record that lies."""
    section = sections[item]
    pairs = (
        ("Verdict", 2, VERDICTS),
        ("D-0022 label", 3, LABELS),
        ("Provider", 4, PROVIDERS),
    )
    for name, column, vocabulary in pairs:
        # Ordered, not set-compared: item 2 carries one value per provider, and a
        # table reading "failed on C1, pending on C2" must not match a section
        # reading the reverse.
        assert _leading_tokens(_field(section, name), vocabulary) == _leading_tokens(
            table_rows[item][column], vocabulary
        ), f"item {item}: {name} disagrees between the table and its section"


@pytest.mark.parametrize("item", range(1, 12))
def test_every_row_names_its_provider_and_its_evidence(item, table_rows, sections):
    assert _leading_tokens(table_rows[item][4], PROVIDERS)
    assert _field(sections[item], "Evidence").strip()
    assert table_rows[item][5].strip()


@pytest.mark.parametrize("item", (8, 10))
def test_the_scoped_exception_is_not_widened(item, table_rows, sections):
    """Items 8 and 10 are deferred, not waived — and nothing else joins them."""
    assert "discharged" not in _leading_tokens(table_rows[item][2], VERDICTS)
    assert "not discharged" in table_rows[item][2]
    assert "not discharged" in _field(sections[item], "Verdict")
    assert _field(sections[item], "Discharge point")


@pytest.mark.parametrize("item", [n for n in range(1, 12) if n not in DEFERRED_ITEMS])
def test_no_other_item_borrows_the_deferred_verdict(item, table_rows, sections):
    """Only items 8 and 10 may be rehearsed rather than discharged."""
    assert DEFERRED_VERDICT not in _leading_tokens(table_rows[item][2], VERDICTS)
    assert DEFERRED_VERDICT not in _leading_tokens(_field(sections[item], "Verdict"), VERDICTS)
    assert "not discharged" not in table_rows[item][2]


def test_the_two_discharge_points_are_named(table_rows):
    assert "before the canary starts" in table_rows[8][6].lower()
    assert "at the canary" in table_rows[10][6].lower()


def test_the_exception_is_stated_as_scoped_to_those_two_items(text):
    assert "scoped exception to D-0019, limited to items 8 and 10" in text
    assert "new decision, not an extension of this one" in text
    assert "defers the two items; it does not waive them" in text


def test_item_9_is_discharged_independently_and_provider_independent(table_rows, sections):
    assert _leading_tokens(table_rows[9][2], VERDICTS) == ["discharged"]
    assert _leading_tokens(table_rows[9][4], PROVIDERS) == ["provider-independent"]
    section = sections[9]
    assert "independently of the spike" in section
    assert "untouched by the provider switch" in section


def test_item_2_keeps_its_c1_failure(table_rows, sections):
    """The provider history is the row nobody should have to reconstruct."""
    assert "failed" in _leading_tokens(table_rows[2][2], VERDICTS)
    section = sections[2]
    assert "C1 (Agent View)" in _field(section, "Provider")
    assert "u1-session-id-bg-experiment.md" in section
    assert "pre-spawn-fence-search.md" in section
    for finding in ("U27", "U28", "U32"):
        assert finding in section
    assert "interleaved transcript is not an accepted residual" in section.lower()


def test_item_3_records_what_the_provider_switch_did_to_it(sections):
    section = sections[3]
    assert "removed by the provider switch" in section
    assert "never proven closed on Agent View" in section
    assert "closed as moot rather than passed" in section
    assert "not an equivalent method" in section


def test_item_6_states_the_f1_caveat(sections):
    section = sections[6]
    assert "trivially satisfied" in section
    assert "UI to attach in the first place" in section


def test_artifacts_are_classified_per_d0026(text):
    section = text.split("## 5. Artifact classification")[1].split("\n## ")[0]
    assert "throwaway by default" in section
    assert "new `D-` entry" in section
    assert re.search(r"S5[^|]*\|[^|]*throwaway", section)
    assert re.search(r"S1[^|]*\|[^|]*durable", section)


def test_the_failure_branch_states_its_cost(text):
    section = text.split("## 6. If the gate fails on C2")[1].split("\n## ")[0]
    assert "`Q-0004` is resolved and spent" in section
    assert "No current `D-` entry designates a third provider" in section
    assert "C3" in section
