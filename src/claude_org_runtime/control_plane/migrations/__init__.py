"""The production control-plane DDL ledger -- numbered, forward-only steps.

This directory *is* the production schema (`D-0029`,
``docs/production-schema.md`` section 3). There is no ``production_schema.sql``
that is edited in place: step ``0001_initial.sql`` is the initial schema and
every later change is its own ``NNNN_name.sql`` file, applied in numeric order
by :mod:`claude_org_runtime.control_plane.migrator`.

Three rules govern what may be done to the files here, and each of them exists
because of a specific failure rather than for tidiness:

**A step that has been applied anywhere is never edited.** Its bytes are hashed
into ``schema_migration.checksum`` when it runs, and every subsequent open
re-hashes the file and refuses on a difference. Editing a historical step is how
two databases end up reporting the same ``version`` while holding different
schemas -- the divergence is silent precisely because both sides believe they
are at the same place. A correction to an applied step is a *new* step.

**There are no down migrations.** A rollback is a restore of the database file
(``docs/production-schema.md`` section 3.2 rule 1), the same posture
``ACCEPTANCE.md`` section 3 takes for the canary. A reverse step that has never
been exercised is a promise the recovery path cannot keep.

**No step converts a spike database.** ``spike_schema.sql`` promises no
migration path from itself, ``D-0026`` says being depended on promotes nothing,
and ``D-0013`` puts the cutover at the run boundary with no state conversion of
in-flight runs. The two schemas carry different ``PRAGMA application_id``
values so that no tool can confuse them.

The Python package marker exists so the directory travels with the installed
package; the ``.sql`` files, not this module, are the content.
"""

from __future__ import annotations

__all__: list[str] = []
