"""A knowledge-transfer package can leave documents out (ADR 0027).

A package carried every one of its subject's own documents inside its period, with no way
to keep one back: a leave request, a medical note or a salary thread dated inside the window
reached the recipient with the rest. `kt_package_exclusions` is the list a curator keeps
back, and `jutsu_retrieval.search.KT_PACKAGE_PREDICATE` reads it inside every KT statement —
the vector scan, the citation door, the tabs, the counts, the workspace and the report.

**Keyed by the document's stable identity, `(source_id, external_id)`, not its row id.** A
re-sync that changes a document's text supersedes the row and writes a new id. An exclusion
recorded against the old id would silently stop applying to the new version, and the thread
somebody deliberately kept out of a handover would walk back in on the next nightly sync.
`document_id` is kept beside the key as the version the curator was looking at, for the
trail, and is never what the predicate matches on.

**A row is a narrowing, never a grant.** Deleting one re-includes a document the package's
own rule already covers; no row can bring in anything the rule does not. The composite
foreign key to `kt_packages (id, org_id)` keeps an exclusion in its package's tenant, and
the table carries `org_id` with RLS `ENABLE` + `FORCE` like every tenant table (ADR 0002).

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "kt_package_exclusions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("package_id", UUID(as_uuid=True), nullable=False),
        # The stable identity the predicate matches: what survives a new version.
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        # The version the curator saw when they excluded it. Informational only.
        sa.Column("document_id", UUID(as_uuid=True), nullable=True),
        sa.Column("excluded_by", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "excluded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["package_id", "org_id"],
            ["kt_packages.id", "kt_packages.org_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["excluded_by"], ["users.id"], ondelete="CASCADE"),
        # One exclusion per document per package. It is also the index the predicate's
        # NOT EXISTS probes, in exactly this column order.
        sa.UniqueConstraint(
            "package_id", "source_id", "external_id", name="uq_kt_package_exclusions_document"
        ),
    )

    op.execute("ALTER TABLE kt_package_exclusions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kt_package_exclusions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY kt_package_exclusions_org_isolation ON kt_package_exclusions "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )
    # No UPDATE: an exclusion is added or removed, never edited into a different document.
    # The REVOKE is what makes that true — migration 0002's default privileges grant
    # UPDATE on every new table, so leaving it out of the GRANT withholds nothing.
    op.execute("GRANT SELECT, INSERT, DELETE ON kt_package_exclusions TO jutsu_app")
    op.execute("REVOKE UPDATE ON kt_package_exclusions FROM jutsu_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS kt_package_exclusions_org_isolation ON kt_package_exclusions")
    op.drop_table("kt_package_exclusions")
