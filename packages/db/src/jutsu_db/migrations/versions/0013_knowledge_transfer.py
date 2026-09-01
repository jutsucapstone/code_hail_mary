"""Knowledge-transfer packages: two permissions and one table.

A KT package is a **scoped, expiring, revocable introduction** between a departing (or
transitioning) employee's context and a named recipient. What it is NOT — and the schema
is shaped to make this unforgeable — is an access key:

* The code (`KT-JUTSU-XXXXXXXX`) finds a row **inside the caller's organisation only**;
  RLS makes a foreign tenant's code indistinguishable from a typo.
* `recipient_email` / `recipient_user_id` bind the package to one person. An unbound
  package binds to its FIRST claimer; after that everyone else gets a 404 — holding the
  code proves nothing once it is claimed.
* `expires_at` and `revoked_at` end access on the server, whatever the browser cached.
* Nothing in this table touches `document_acl`. What a recipient can read inside the KT
  workspace is decided by THEIR OWN grants, per query, exactly as everywhere else. A
  package narrows presentation (period, scope); it never widens authorization.

Permissions, in the catalogue because 0002 made the catalogue migration-only:

* `kt:manage` — owner, super_admin, hr_admin. Transitions are a people act, HR's
  domain; IT governs connections instead.
* `kt:open` — every role. The typical recipient is a brand-new Member; gate opening on
  anything administrative and the one person KT exists for cannot use it. The package's
  own binding is the real gate.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

MANAGE = "kt:manage"
OPEN = "kt:open"

MANAGE_ROLES = ("owner", "super_admin", "hr_admin")
ALL_ROLES = ("owner", "super_admin", "hr_admin", "it_admin", "analyst", "viewer", "member")


def upgrade() -> None:
    op.bulk_insert(
        sa.table("permissions", sa.column("key", sa.String), sa.column("description", sa.Text)),
        [
            {
                "key": MANAGE,
                "description": "Create, list, revoke and complete knowledge-transfer packages.",
            },
            {
                "key": OPEN,
                "description": (
                    "Open a knowledge-transfer package addressed to you. Grants no document."
                ),
            },
        ],
    )
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_key", sa.String),
            sa.column("permission_key", sa.String),
        ),
        [{"role_key": role, "permission_key": MANAGE} for role in MANAGE_ROLES]
        + [{"role_key": role, "permission_key": OPEN} for role in ALL_ROLES],
    )

    op.create_table(
        "kt_packages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        #: The shareable identifier. Crockford alphabet, same as JUTSU IDs, so the same
        #: normalisation forgives the same transcription mistakes.
        sa.Column("kt_code", sa.String(24), nullable=False, unique=True),
        sa.Column("subject_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        #: Stored state only. "expired" and "claimed" are derived from expires_at and
        #: recipient_user_id at read time, in one place, so two surfaces cannot disagree
        #: about what a package currently is.
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        #: The categories included, validated by the API against what the backend can
        #: actually serve — never a free-text taxonomy.
        sa.Column("scope", JSONB, nullable=False, server_default="[]"),
        sa.Column("period_start", sa.DateTime(timezone=True)),
        sa.Column("period_end", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recipient_email", sa.String(320)),
        sa.Column("recipient_user_id", UUID(as_uuid=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("counts_json", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('active', 'revoked', 'completed')", name="status"),
        sa.CheckConstraint(r"kt_code ~ '^KT-JUTSU-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{8}$'", name="code"),
    )
    op.create_index("ix_kt_packages_org_created", "kt_packages", ["org_id", "created_at"])

    op.execute("ALTER TABLE kt_packages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kt_packages FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY kt_packages_org_isolation ON kt_packages "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS kt_packages_org_isolation ON kt_packages")
    op.drop_table("kt_packages")
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_key IN (:m, :o)").bindparams(
            m=MANAGE, o=OPEN
        )
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE key IN (:m, :o)").bindparams(m=MANAGE, o=OPEN)
    )
