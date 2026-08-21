"""Section 7's five holes, bound to the suite so that filling one silently fails.

The failure this file is written against is not a bug in any module. It is the
way a *stated* hole stops being stated: someone adds a rate, wants to know
whether the rate is good, and writes the comparison -- and
``docs/measurement-harness.md`` section 7's "``Q-0005`` stays open" becomes an
answer nobody decided, arriving as a default. The same drift closes ``Q-0009``
(a report that picks a detector version), ``Q-0011`` (a Secretary latency
threshold invented in the module that already has milliseconds in it) and the
harness's read-only property (one convenient backfill of an ``ai_invocation``
row).

Two properties here are **discovery-driven on purpose**, because a test that
reads a hand-written list of modules covers exactly the modules that existed on
the day it was written, and the module that fills a hole is by definition a
later one:

* every public ``render_*`` in the package is discovered by walking the package
  (:func:`_public_renderers`), and a renderer with no entry in
  :data:`REPORT_FACTORIES` **fails** rather than being skipped -- so a new
  report cannot reach a reader without its rendering being read for a verdict;
* every ``.py`` under the package is parsed and every ``execute`` /
  ``executemany`` / ``executescript`` call in it is classified
  (:func:`_statements_executed`), so a new module that writes is caught by a
  test that never heard of it.

The verdict vocabulary is matched with word boundaries: the reports
legitimately contain ``ongoing``, ``category`` and ``coverage``, and a pattern
that fired on those would be turned off within a week.

Nothing here writes: reports are built over an empty migrated production
database through the same :func:`~...reader.open_for_measurement` handle the
harness uses, so a factory that needed a write could not be written.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterator

import pytest

import claude_org_runtime.measurement as measurement_package
from claude_org_runtime.control_plane import policy
from claude_org_runtime.control_plane.migrator import create_production_control_plane
from claude_org_runtime.measurement import (
    ac9,
    canary,
    cohort,
    false_termination,
    fixtures,
    latency,
    provenance,
    render,
    shadow,
    windows,
)
from claude_org_runtime.measurement.reader import open_for_measurement

T0 = 1_700_000_000_000  # an arbitrary fixed epoch-milliseconds instant
DAY_MS = 86_400_000
PERIOD_START = T0
PERIOD_END = T0 + DAY_MS
GENERATED_AT = PERIOD_END + 1_000

PACKAGE_ROOT = Path(measurement_package.__file__).resolve().parent
CORPUS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "labelled"

#: The go/no-go vocabulary section 7 keeps out of every rendering. ``go`` and
#: the pass/fail families only -- ground-truth words the documents *do* define
#: (``stuck``, ``miss``, ``false_positive``, ``VIOLATED``) are findings about a
#: subject, not a judgement on the report, and are deliberately not here.
VERDICT_WORDS = re.compile(
    r"\b("
    r"pass|passes|passed|passing|"
    r"fail|fails|failed|failing|"
    r"go|no-go|nogo|"
    r"accept|accepted|acceptable|reject|rejected|"
    r"threshold|thresholds|exit criteri(?:on|a)"
    r")\b",
    re.IGNORECASE,
)

#: Names that would answer ``Q-0005`` or ``Q-0011`` by existing. A constant
#: called ``*_TARGET`` is allowed (``ac9`` prints AC-9's stated aims as aims);
#: a constant called ``*_THRESHOLD`` is not, because nothing prints a threshold
#: -- a threshold exists to be compared against.
FORBIDDEN_NAME = re.compile(
    r"(THRESHOLD|CUTOFF|EXIT_CRITERI|GO_NO_GO|MIN_SAMPLE|SAMPLE_SIZE"
    r"|MINIMUM_(COHORT|SAMPLE|RUNS|EPISODES))",
    re.IGNORECASE,
)

#: ``windows.EpisodeWindow.threshold_kind`` is not an invented number and must
#: not read as one: it carries *which rule the policy row declared*
#: (``absolute_ms`` / ``consecutive_count`` / a multiple), read from
#: ``policy_detection_latency`` at the caller-resolved revision. A ``*_kind``
#: name is a discriminator over declared policy data; the thing section 7
#: forbids is a magnitude this harness chose. Only the ``_kind`` suffix is
#: spared, so ``MISS_RATE_THRESHOLD`` is still caught.
DESCRIBES_A_DECLARED_RULE = re.compile(r"_kind$")

#: Statement verbs that change the database. ``PRAGMA``-with-assignment and the
#: transaction verbs are handled separately: they are how ``reader.py`` *proves*
#: the file refuses writes, and that proof is the one place they are allowed.
WRITE_VERBS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
        "CREATE",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "VACUUM",
        "ATTACH",
        "DETACH",
        "REINDEX",
    }
)
TRANSACTION_VERBS = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"})
READ_VERBS = frozenset({"SELECT", "WITH", "EXPLAIN"})

#: The read-only proof, and nothing else, may set a pragma and open a
#: transaction: it exists to attempt a write that a ``mode=ro`` file must
#: refuse (``reader.prove_read_only``'s docstring,
#: ``D-0040``). Named function by function rather than module by module, so a
#: second function added to ``reader.py`` is still covered.
#: ``reader.measurement_snapshot`` and ``reader._undo_the_probe`` are here for a
#: second reason, and it is not a write either: a report has to hold one read
#: transaction across all of its reads or its fingerprint attests a state its
#: figures did not come from (``measurement-harness.md`` section 6). BEGIN,
#: ROLLBACK and the probe's SAVEPOINT/RELEASE are transaction control over reads;
#: no statement inside them writes, which is what the rest of this scan proves.
WRITE_PROBE_EXEMPTIONS = frozenset(
    {
        ("reader", "_arm_and_verify_both_mechanisms"),
        ("reader", "_require_query_only"),
        ("reader", "prove_read_only"),
        ("reader", "_undo_the_probe"),
        ("reader", "measurement_snapshot"),
    }
)


# --------------------------------------------------------------------------
# fixtures -- an empty production database, read through the harness's opener
# --------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "production.sqlite3"
    create_production_control_plane(path, now_ms=T0).close()
    return path


def _revision_id(path: Path) -> int:
    connection = open_for_measurement(path)
    try:
        return policy.effective_revision_id(connection, now_ms=PERIOD_START)
    finally:
        connection.close()


def _reading(path: Path) -> sqlite3.Connection:
    return open_for_measurement(path)


# --------------------------------------------------------------------------
# one factory per public renderer -- a renderer with no factory fails
# --------------------------------------------------------------------------


def _ac9_report(path: Path):
    connection = _reading(path)
    try:
        selected = cohort.select_cohort(
            connection,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=GENERATED_AT,
        )
        return ac9.measure_ac9(connection, selected, now_ms=GENERATED_AT)
    finally:
        connection.close()


def _window_report(connection: sqlite3.Connection, revision_id: int):
    return windows.classify_episodes(
        connection,
        revision_id=revision_id,
        period_start_ms=PERIOD_START,
        period_end_ms=PERIOD_END,
        episodes=(),
    )


def _latency_report(path: Path):
    revision_id = _revision_id(path)
    connection = _reading(path)
    try:
        return latency.measure_latency(
            connection,
            windows=_window_report(connection, revision_id),
            detections={},
            shadow=latency.no_shadow_reference(
                "this period lies outside the shadow window"
            ),
            now_ms=GENERATED_AT,
        )
    finally:
        connection.close()


def _false_termination_report(path: Path):
    connection = _reading(path)
    try:
        return false_termination.measure_false_termination(
            connection,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=GENERATED_AT,
            fixture_labels={},
            subsequent_evidence={},
            human_adjudications={},
        )
    finally:
        connection.close()


def _fixture_evaluation(_path: Path):
    corpus = fixtures.load_corpus(CORPUS_ROOT)
    return fixtures.evaluate(
        corpus,
        clock=fixtures.SyntheticClock(T0),
        outcomes={case.case_id: () for case in corpus.cases},
    )


def _shadow_reconciliation(_path: Path):
    return shadow.reconcile(
        period_start_ms=PERIOD_START,
        period_end_ms=PERIOD_END,
        interlock_episodes=(),
        v1_reference=shadow.V1Reference.attests_empty(source="v1-shadow-adapter@1"),
        censored_ids=(),
        fixture_labels={},
    )


def _canary_report(path: Path):
    connection = _reading(path)
    try:
        return canary.measure_canary_divergence(
            connection,
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            interlock_episodes=(),
            v1_reference=shadow.V1Reference.attests_empty(source="v1-shadow-adapter@1"),
            censored_ids=frozenset(),
            fixture_labels={},
            v1_writer_ledger=canary.V1WriterLedger.attests_empty(source="v1:.state"),
            v1_ownership=canary.V1OwnershipInput.attests_empty(
                source="v1-owner-export"
            ),
        )
    finally:
        connection.close()


def _report_header(path: Path):
    connection = _reading(path)
    try:
        return provenance.build_header(
            connection,
            db_path=str(path),
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            generated_at_ms=GENERATED_AT,
            policy_revision_id=_revision_id(path),
            fingerprint_tables=("run", "incident", "ai_invocation"),
            query_definitions={"caller_incidents": "SELECT count(*) FROM incident"},
            fixture_suite=provenance.FixtureSuiteRef.absent(
                "no fixture recall in this report"
            ),
            imputation=provenance.ImputationRule(
                bounded=provenance.BOUNDED_IMPUTATION_RULE,
                sensitivity=provenance.SENSITIVITY_IMPUTATION_RULE,
                unbounded_missing=0,
            ),
            coverage=provenance.CoverageSummary(covered=0, total=0, excluded={}),
            censored=0,
            censored_left=0,
            unmatched={},
        )
    finally:
        connection.close()


def _measurement_report(path: Path):
    connection = _reading(path)
    try:
        return render.build_measurement_report(
            connection,
            db_path=str(path),
            period_start_ms=PERIOD_START,
            period_end_ms=PERIOD_END,
            now_ms=GENERATED_AT,
            fixture_suite=provenance.FixtureSuiteRef.absent(
                "no fixture recall in this report"
            ),
            v1_shadow=render.V1ShadowInput.absent(
                "this period lies outside the shadow window"
            ),
        )
    finally:
        connection.close()


#: renderer qualified name -> how to build the thing it renders. Keys are
#: ``module.function``, matched against discovery below; adding a renderer
#: without adding a key fails :func:`test_every_public_renderer_is_bound_here`.
REPORT_FACTORIES: dict[str, Callable[[Path], object]] = {
    "ac9.render_ac9_report": _ac9_report,
    "latency.render_latency_report": _latency_report,
    "false_termination.render_false_termination_report": _false_termination_report,
    "fixtures.render_fixture_report": _fixture_evaluation,
    "shadow.render_shadow_reconciliation": _shadow_reconciliation,
    "canary.render_canary_divergence_report": _canary_report,
    "provenance.render_header_markdown": _report_header,
    "provenance.render_header_json": _report_header,
    "render.render_markdown": _measurement_report,
    "render.render_json": _measurement_report,
}


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def _measurement_modules() -> tuple[object, ...]:
    """Every module in the package, imported. Not a list: a walk."""

    found = []
    for info in pkgutil.iter_modules([str(PACKAGE_ROOT)]):
        found.append(
            importlib.import_module(f"{measurement_package.__name__}.{info.name}")
        )
    assert found, "the package walk found no modules, so every test here is vacuous"
    return tuple(found)


def _public_renderers() -> dict[str, Callable[..., str]]:
    renderers: dict[str, Callable[..., str]] = {}
    for module in _measurement_modules():
        short = module.__name__.rsplit(".", 1)[-1]
        for name, value in vars(module).items():
            if not name.startswith("render_") or name.startswith("_render"):
                continue
            if not callable(value) or getattr(value, "__module__", None) != module.__name__:
                continue
            renderers[f"{short}.{name}"] = value
    return renderers


def _module_sources() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        yield path.stem, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _leading_verb(sql: str) -> str:
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.split(None, 1)[0].upper().strip("(;")
    return ""


class _Sources:
    """Every statement text a module could be handing ``execute``.

    Statements in this package arrive four ways -- a literal, a module-level
    constant, an entry in a ``QUERY_DEFINITIONS`` mapping, and a dataclass field
    (``RecordClass.sql``) -- and a scan that understood only the first would
    report the other three as "not inspectable" until someone widened the
    exemptions instead of the resolver. So each form is resolved to the text
    that is actually executed; anything left unresolved is a failure, because an
    uninspectable statement is where a write would sit unread.
    """

    def __init__(self, tree: ast.Module) -> None:
        self.names: dict[str, ast.AST] = {}
        self.mappings: dict[str, dict[str, ast.AST]] = {}
        self.field_texts: list[str] = []

        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            for target in targets:
                if not isinstance(target, ast.Name) or value is None:
                    continue
                self.names[target.id] = value
                literal = value
                if isinstance(literal, ast.Call) and len(literal.args) == 1:
                    # MappingProxyType({...}): the mapping is the argument, and
                    # a resolver that stopped at the wrapper would report every
                    # QUERY_DEFINITIONS lookup as uninspectable.
                    literal = literal.args[0]
                if isinstance(literal, ast.Dict):
                    entries: dict[str, ast.AST] = {}
                    for key, item in zip(literal.keys, literal.values):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            entries[key.value] = item
                    self.mappings[target.id] = entries

            # ``sql=`` on a dataclass construction: the text a ``.sql``
            # attribute access will hand to execute.
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "sql":
                        text = self.text_of(keyword.value, depth=0)
                        if text is not None:
                            self.field_texts.append(text)

    def text_of(self, node: ast.AST, *, depth: int = 0) -> str | None:
        if depth > 4:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    if part.value.strip():
                        return part.value
            return None
        if isinstance(node, ast.Name):
            bound = self.names.get(node.id)
            return None if bound is None else self.text_of(bound, depth=depth + 1)
        if isinstance(node, ast.Subscript):
            container = node.value
            key = node.slice
            if isinstance(container, ast.Name) and isinstance(key, ast.Constant):
                entry = self.mappings.get(container.id, {}).get(key.value)
                if entry is not None:
                    return self.text_of(entry, depth=depth + 1)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.text_of(node.left, depth=depth + 1)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "format":
                # ``QUERY.format(event_types=...)`` expands an IN list's
                # placeholders; the verb is the template's.
                return self.text_of(node.func.value, depth=depth + 1)
        return None

    def texts_for_argument(self, node: ast.AST) -> list[str] | None:
        """Every text this argument can evaluate to, or ``None`` if unknown."""

        if isinstance(node, ast.Attribute) and node.attr == "sql":
            # ``record_class.sql`` over a declared set of record classes: each
            # declared sql is executed, so each is classified.
            return list(self.field_texts) or None
        text = self.text_of(node)
        return None if text is None else [text]


def _statements_executed() -> Iterator[tuple[str, str, str, str | None]]:
    """``(module, enclosing function, verb, text)`` for every execute call."""

    for short, tree in _module_sources():
        sources = _Sources(tree)

        enclosing: dict[ast.AST, str] = {}
        for holder in ast.walk(tree):
            if isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(holder):
                    enclosing.setdefault(child, holder.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in {"execute", "executemany", "executescript"}:
                continue
            if not node.args:
                continue
            texts = sources.texts_for_argument(node.args[0])
            where = enclosing.get(node, "<module>")
            if texts is None:
                yield short, where, "", None
                continue
            for text in texts:
                yield short, where, _leading_verb(text), text


# --------------------------------------------------------------------------
# hole 1 and hole 3 -- no verdict, no threshold, anywhere
# --------------------------------------------------------------------------


def test_every_public_renderer_is_bound_here() -> None:
    """A renderer this file has never heard of is an unread rendering.

    Discovery, not a list: the point of the test is the module that does not
    exist yet.
    """

    discovered = set(_public_renderers())
    bound = set(REPORT_FACTORIES)
    assert discovered == bound, (
        "every public renderer under measurement/ must be built and read for a "
        "verdict by this file; unbound: "
        f"{sorted(discovered - bound)}; stale: {sorted(bound - discovered)}"
    )


@pytest.mark.parametrize("qualified_name", sorted(REPORT_FACTORIES))
def test_no_renderer_emits_verdict_vocabulary(qualified_name: str, db: Path) -> None:
    """``Q-0005`` stays open, and a rendering is where it would quietly close.

    Each report is built for real over an empty production database and
    rendered, then read for go/no-go words. A module may say ``Q-0005`` is open
    -- several do -- but the property under test is the rendering, because that
    is what a reader sees.
    """

    renderer = _public_renderers()[qualified_name]
    rendered = renderer(REPORT_FACTORIES[qualified_name](db))

    offending = [
        match.group(0)
        for match in VERDICT_WORDS.finditer(rendered)
        # A statement that a threshold does NOT exist is the hole being stated,
        # which is the opposite of the failure: the words appear only inside
        # the sentence naming the open question.
        if "Q-0005" not in _context(rendered, match.start())
    ]
    assert not offending, (
        f"{qualified_name} emitted verdict vocabulary {sorted(set(offending))}; "
        "section 7 keeps Q-0005 open, and a harness that prints a verdict "
        "answers it by inertia"
    )
    assert rendered.isascii(), f"{qualified_name} broke the cp932 rule"


def _context(text: str, index: int, *, radius: int = 240) -> str:
    return text[max(0, index - radius) : index + radius]


def test_no_module_names_a_threshold_or_a_sample_size_minimum() -> None:
    """A name is enough: ``MIN_COHORT_SIZE`` answers Q-0005 by existing.

    Walks the package, so a later module carrying one is caught without this
    test being updated.
    """

    offending: list[str] = []
    for module in _measurement_modules():
        short = module.__name__.rsplit(".", 1)[-1]
        for name in vars(module):
            if name.startswith("__"):
                continue
            if getattr(vars(module)[name], "__module__", module.__name__) != (
                module.__name__
            ):
                continue  # imported vocabulary belongs to the module that owns it
            if DESCRIBES_A_DECLARED_RULE.search(name):
                continue
            if FORBIDDEN_NAME.search(name):
                offending.append(f"{short}.{name}")
    assert not offending, (
        f"{offending} name an exit criterion or a sample-size minimum; "
        "measurement-harness.md section 7 leaves Q-0005 and Q-0011 open"
    )


def test_no_target_constant_is_ever_compared() -> None:
    """AC-9's targets print as targets. A comparison is a verdict with no name.

    ``ac9`` holds ``PROMPT_REDUCTION_TARGET`` and
    ``OUTPUT_TOKEN_REDUCTION_TARGET`` so a reader sees what the aim was; the
    moment one of them appears in a comparison, the harness has decided whether
    the aim was met, which is exactly what ``ACCEPTANCE.md`` section 3 refuses
    to do.
    """

    offending: list[str] = []
    for short, tree in _module_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            for operand in [node.left, *node.comparators]:
                name = None
                if isinstance(operand, ast.Name):
                    name = operand.id
                elif isinstance(operand, ast.Attribute):
                    name = operand.attr
                if not name or DESCRIBES_A_DECLARED_RULE.search(name):
                    continue
                if name.endswith("_TARGET") or FORBIDDEN_NAME.search(name):
                    offending.append(f"{short}:{node.lineno}:{name}")
    assert not offending, (
        f"{offending} compare a target against a measured figure, which turns "
        "AC-9's stated aim into a canary exit threshold (Q-0005)"
    )


# --------------------------------------------------------------------------
# hole 5 -- the harness never writes, and ai_invocation least of all
# --------------------------------------------------------------------------


def test_no_module_executes_a_write_statement() -> None:
    """Every statement the package executes is a read.

    Parsed rather than grepped, so an ``INSERT`` inside a docstring (there are
    several, explaining what the writers do) does not fire and an ``INSERT``
    built by an f-string does. The only exemptions are the functions of
    ``reader.py``'s read-only proof, which attempt a write *in order to be
    refused*, and its read snapshot, whose BEGIN/ROLLBACK are transaction
    control over reads (see :data:`WRITE_PROBE_EXEMPTIONS`); a statement whose
    text cannot be read statically fails too,
    because an uninspectable statement is where a write would hide.
    """

    seen = 0
    offending: list[str] = []
    for short, function, verb, text in _statements_executed():
        seen += 1
        where = f"{short}.{function}"
        if text is None:
            offending.append(f"{where}: statement not statically inspectable")
            continue
        if verb in READ_VERBS:
            continue
        exempt = (short, function) in WRITE_PROBE_EXEMPTIONS
        if verb == "PRAGMA":
            if "=" in text and not exempt:
                offending.append(f"{where}: sets a pragma ({text.strip()!r})")
            continue
        if verb in TRANSACTION_VERBS and exempt:
            continue
        if verb in WRITE_VERBS or verb in TRANSACTION_VERBS:
            offending.append(f"{where}: {verb}")
            continue
        offending.append(f"{where}: unrecognised statement verb {verb!r}")

    assert seen > 20, f"only {seen} executed statements found; the scan is not working"
    assert not offending, (
        f"{offending}: the measurement harness is read-only (D-0040, "
        "measurement-harness.md section 1), and ai_invocation's single-writer "
        "property (D-0003, section 7) holds only while nothing here writes"
    )


def test_ac9_states_that_it_never_writes_ai_invocation() -> None:
    """Hole 5 is a property of the code plus a sentence saying why it matters.

    The property is tested above. This asserts the reason is written down where
    the next person to want a backfill will read it.
    """

    docstring = ac9.__doc__ or ""
    assert "D-0003" in docstring
    assert "ai_invocation" in docstring
    assert "single writer" in docstring


# --------------------------------------------------------------------------
# hole 2 -- Q-0009 exposed, not decided
# --------------------------------------------------------------------------


def test_the_header_exposes_the_detector_version_set_and_decides_nothing(
    db: Path,
) -> None:
    """``Q-0009`` stays open: the set is published, compatibility is not ruled on."""

    header = _report_header(db)
    assert isinstance(header.detector_versions, tuple)
    mapping = header.as_mapping()
    assert "detector_versions" in str(mapping)
    assert "Q-0009" in (provenance.__doc__ or "")

    fields = {name for name in dir(header) if not name.startswith("_")}
    decided = {
        name
        for name in fields
        if re.search(r"compatib|homogene", name, re.IGNORECASE)
        and name not in {"non_homogeneity_reasons", "non_homogeneous"}
    }
    assert not decided, (
        f"{sorted(decided)} would decide what cross-version compatibility means; "
        "Q-0009 leaves that open and the header only exposes the set and flags "
        "a non-homogeneous period"
    )


# --------------------------------------------------------------------------
# hole 3 -- Q-0011 belongs to gate item 8
# --------------------------------------------------------------------------


def test_latency_states_that_secretary_window_latency_is_not_its_measurement() -> None:
    """The module with the milliseconds in it is where a Q-0011 threshold would land."""

    docstring = latency.__doc__ or ""
    assert "Q-0011" in docstring
    assert "gate item 8" in docstring

    names = {name for name in dir(latency) if not name.startswith("_")}
    assert not {name for name in names if "secretary" in name.lower()}, (
        "a Secretary series here would make this harness the owner of Q-0011's "
        "measurement, which section 7 assigns to gate item 8"
    )


# --------------------------------------------------------------------------
# hole 4 -- the positional escalation key, and its failures kept visible
# --------------------------------------------------------------------------


def test_the_positional_caveat_reaches_the_reader_of_the_reconciliation() -> None:
    """A weak join is only safe while the reader is told it is weak.

    The caveat must name the failure mode (unmatched, not mispaired) and the
    consequence (replace the key before trusting the numbers), and must appear
    in the rendering -- a constant nobody prints is documentation of a hole
    that the report does not have.
    """

    caveat = shadow.POSITIONAL_KEY_CAVEAT
    assert "positional" in caveat
    assert "unmatched" in caveat
    assert "replacing" in caveat

    # An escalation whose run_id is missing: it cannot be keyed, so it lands in
    # the bucket section 7 says to read as "the key needs replacing". The
    # rendering of THAT report is where the caveat has to appear.
    unkeyable = shadow.ShadowEpisode(
        episode_id="escalation-with-no-run",
        subject_class=shadow.SUBJECT_WORKER_ESCALATION,
        shape="gate_refused",
        onset_ms=PERIOD_START + 1,
        key_gap="the gate row carries no run_id, so there is nothing to order by",
    )
    report = shadow.reconcile(
        period_start_ms=PERIOD_START,
        period_end_ms=PERIOD_END,
        interlock_episodes=(unkeyable,),
        v1_reference=shadow.V1Reference.attests_empty(source="v1-shadow-adapter@1"),
        censored_ids=(),
        fixture_labels={},
    )
    assert report.positional_caveat == caveat
    assert unkeyable.episode_id in [
        episode.episode_id for episode in report.unmatched_key
    ]
    rendered = shadow.render_shadow_reconciliation(report)
    assert caveat.split(".")[0] in rendered, (
        "the reconciliation renders without its own caveat, so a run of "
        "unmatched escalation episodes would read as a detector problem"
    )
    assert shadow.UNMATCHED_KEY in report.counts(), (
        "the unmatched bucket must be reported even at zero -- it is the signal "
        "that the key needs replacing"
    )


def test_a_positional_key_is_marked_positional_on_the_key_itself() -> None:
    """Discovery again: every subject class the module declares positional says so."""

    for subject_class in shadow.POSITIONAL_SUBJECT_CLASSES:
        key = shadow.CorrelationKey(subject_class=subject_class, parts=("a", "1"))
        assert key.positional, f"{subject_class} is positional and must say so"
