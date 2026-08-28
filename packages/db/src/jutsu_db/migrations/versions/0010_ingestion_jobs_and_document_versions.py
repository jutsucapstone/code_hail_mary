"""Isolate the queue by tenant, and let a document have versions.

Four changes, each forced by S8 rather than convenient for it.

**1 · `jobs` and `sources` gain RLS, enabled and FORCED.** Both have carried `org_id`
since migration 0001 and neither was isolated by the database. That is the `user_groups`
defect of ADR 0010 repeating: a table with a tenant column and no policy is protected by
whatever every call site remembers to do. S8 puts both on the hot path — `jobs` holds the
work queue, `sources` holds the connector cursor — so the gap closes before anything
depends on it.

**2 · Document uniqueness becomes partial.** `uq_documents_org_source_external` forbade
two rows sharing `(org_id, source_id, external_id)`, which makes versioning impossible:
re-ingesting changed content has nowhere to put the new row. It is replaced by a unique
index over the same columns `WHERE superseded_by IS NULL` — **one *current* document per
source identifier, and as much history behind it as the source produces.** S7's retrieval
filter already excludes superseded rows, so the read path needs no change at all.

**3 · `fk_documents_superseded_by` becomes DEFERRABLE.** This one was found by testing
the design rather than reasoning about it. Superseding means "point the old row at the
new one, then insert the new one", and with an immediate foreign key that first statement
references a row that does not exist yet:

    UPDATE documents SET superseded_by = <new> WHERE id = <old>   -- FK violation
    INSERT INTO documents (id, ...) VALUES (<new>, ...)

The alternatives were worse. Inserting the new row first puts two rows with
`superseded_by IS NULL` in the table at once, which the partial index correctly refuses.
Inserting it with a placeholder self-reference works but writes a statement into the data
that is not true, even transiently. Deferring the constraint to COMMIT keeps both
statements honest and keeps the partial index checked at every statement boundary — there
is never a moment with two current versions.

Deferred **INITIALLY IMMEDIATE**, so nothing changes for any existing caller; the
ingestion path opts in per transaction with `SET CONSTRAINTS fk_documents_superseded_by
DEFERRED`.

**4 · `jobs` gains `failure_kind`, `next_attempt_at`, `updated_at` and a claim index.**
A single `FAILED` state loses the one thing an operator needs: *why*, and whether
retrying could possibly help. `failure_kind` separates a source that was unreachable from
a document that will never parse, and `next_attempt_at` is what backoff actually writes
down.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

#: The two tables joining the policy in this migration. Local to the file on purpose —
#: `jutsu_db.RLS_TABLES` is the running total and this is the increment, exactly as
#: migration 0008 wrote it.
RLS_TABLES = ("jobs", "sources")

#: Identical to every other policy in the schema, `NULLIF` included. That is not
#: copy-paste: `current_setting(…, true)` returns NULL only until the GUC is first set and
#: an empty string afterwards, and `''::uuid` *raises* rather than filtering. Unset and
#: reset must both fail closed.
_SCOPE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} USING ({_SCOPE}) WITH CHECK ({_SCOPE})"
        )

    # ------------------------------------------------------------------ versioning
    op.drop_constraint("uq_documents_org_source_external", "documents", type_="unique")
    op.execute(
        "CREATE UNIQUE INDEX uq_documents_current_org_source_external "
        "ON documents (org_id, source_id, external_id) WHERE superseded_by IS NULL"
    )

    op.drop_constraint("fk_documents_superseded_by", "documents", type_="foreignkey")
    op.create_foreign_key(
        "fk_documents_superseded_by",
        "documents",
        "documents",
        ["superseded_by"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="IMMEDIATE",
    )

    # ------------------------------------------------------------------ job scheduling
    op.add_column("jobs", sa.Column("failure_kind", sa.String(32)))
    op.add_column("jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    op.add_column(
        "jobs",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # The claim query in `jutsu_worker.jobs`: one org, one kind, runnable now, oldest
    # first. `ix_jobs_state_locked_until` from migration 0001 stays — it serves the
    # expired-lease sweep, which asks a different question.
    op.create_index(
        "ix_jobs_org_kind_state_next_attempt",
        "jobs",
        ["org_id", "kind", "state", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_org_kind_state_next_attempt", table_name="jobs")
    op.drop_column("jobs", "updated_at")
    op.drop_column("jobs", "next_attempt_at")
    op.drop_column("jobs", "failure_kind")

    op.drop_constraint("fk_documents_superseded_by", "documents", type_="foreignkey")
    op.create_foreign_key(
        "fk_documents_superseded_by",
        "documents",
        "documents",
        ["superseded_by"],
        ["id"],
        ondelete="SET NULL",
    )

    # **This fails loudly if any document has ever been superseded**, and that is the
    # correct behaviour rather than a rough edge. The old constraint cannot represent
    # version history, so restoring it over real history would mean destroying rows. A
    # migration that silently deleted a tenant's document versions to make a constraint
    # fit would be far worse than one that refuses to run.
    op.execute("DROP INDEX uq_documents_current_org_source_external")
    op.create_unique_constraint(
        "uq_documents_org_source_external",
        "documents",
        ["org_id", "source_id", "external_id"],
    )

    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
