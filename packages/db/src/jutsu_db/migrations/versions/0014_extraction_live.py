"""Extraction goes live: RLS on its tables, plus three small columns other waves need.

Four tables have carried `org_id` since migration 0001 with no policy over it —
`pii_vault`, `extraction_runs`, `extraction_claims`, `resolution_queue` — a gap the
test suite has kept visible in `RLS_PENDING_BACKFILL` rather than silently absent.
While extraction was schema-only that was a debt; the moment a worker writes claims it
would be a live §4.7 violation, so the policies land in the same wave as the writer.
`eval_runs` stays pending: its `org_id` is nullable by design (harness runs are not
tenant rows) and scoping it is a different decision.

Also here, because they belong to the same wave of work:

* `invitations.role_title` — the free-text role *title* an inviter may attach. This is
  §1's "other option where user can write" implemented without dismantling RBAC: the
  written text becomes the invitee's displayed designation on their profile, while the
  permissions they hold come from the closed, migration-owned catalogue exactly as
  before. A title is vocabulary; a role is authority; only one of them is free text.
* `sources.stats_json` — per-run pipeline counters (fetched, unchanged, indexed,
  failed, masked spans) so the Knowledge Sources UI reports measured stage numbers
  (§11) instead of deriving them from job rows by parsing idempotency keys.
* An index for reading claims the way the product does: by type, newest first.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

BACKFILLED = ("pii_vault", "extraction_runs", "extraction_claims", "resolution_queue")


def upgrade() -> None:
    for table in BACKFILLED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} "
            f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
        )

    op.add_column("invitations", sa.Column("role_title", sa.String(128)))
    op.add_column(
        "sources",
        sa.Column("stats_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index(
        "ix_extraction_claims_org_type", "extraction_claims", ["org_id", "claim_type", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_claims_org_type", "extraction_claims")
    op.drop_column("sources", "stats_json")
    op.drop_column("invitations", "role_title")
    for table in BACKFILLED:
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
