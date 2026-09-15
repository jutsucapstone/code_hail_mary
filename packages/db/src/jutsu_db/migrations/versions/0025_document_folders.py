"""Documents remember their folder, and the words of its path (ADR 0029).

A connected drive organises knowledge by folder — "Projects/Astro Agent", "Handover" — and
that location is half of what a person asks about ("where are A's Astro Agent documents?").
Connectors dropped it: folders are skipped at ingestion because they carry no text, and the
path of the file itself was never recorded. So nothing could answer the question.

**Two columns on `documents`, not a folders table.** A folder has no text and no grant of
its own; it exists to a caller exactly when a document inside it does. Recording the path on
each document keeps a folder's visibility equal to its documents' under the ACL that already
guards them, with no second authorization path and no folder rows to keep in step with a
provider's tree.

**The path's words are rows, matched by equality.** `document_folder_words` holds each
document's path words with a btree on `(org_id, word, document_id)`. Not an expression GIN
index over the path's `tsvector`: the application role is subject to row-level security, and
PostgreSQL never uses a non-leakproof operator such as `@@` as an index condition beneath a
policy — that index served the owner and never `jutsu_app`. `text` equality is leakproof.
Words are replaced, never edited, so the application role may insert and delete them only.

**Metadata, not content.** Neither the path nor its words is part of `content_hash`, so a
moved file is the same version refreshed in place — never a re-chunk, never a re-embed.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    op.add_column("documents", sa.Column("folder_path", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("folder_uri", sa.Text(), nullable=True))

    op.create_table(
        "document_folder_words",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("word", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "word", name="pk_document_folder_words"),
        # A word belongs to one version of one document in one tenant, and goes with it.
        sa.ForeignKeyConstraint(
            ["document_id", "org_id"],
            ["documents.id", "documents.org_id"],
            ondelete="CASCADE",
            name="fk_document_folder_words_document_id_org_id",
        ),
    )
    # The folder search's index condition: leakproof equality, tenant first.
    op.create_index(
        "ix_document_folder_words_word", "document_folder_words", ["org_id", "word", "document_id"]
    )
    op.execute("ALTER TABLE document_folder_words ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_folder_words FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY document_folder_words_org_isolation ON document_folder_words "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )
    # Replaced, never edited. Migration 0002's default privileges grant UPDATE on every new
    # table, so withholding it takes the REVOKE.
    op.execute("GRANT SELECT, INSERT, DELETE ON document_folder_words TO jutsu_app")
    op.execute("REVOKE UPDATE ON document_folder_words FROM jutsu_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS document_folder_words_org_isolation ON document_folder_words")
    op.drop_table("document_folder_words")
    op.drop_column("documents", "folder_uri")
    op.drop_column("documents", "folder_path")
