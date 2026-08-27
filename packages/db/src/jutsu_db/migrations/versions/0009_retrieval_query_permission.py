"""Add `retrieval:query`, held by every role.

Retrieval needs a permission before it can have a route, and §17 decides which one it is:
**roles gate features, ACLs gate data, never conflate them.** Reading the corporate memory
is the feature every employee is hired to use; *which* evidence comes back is decided
entirely by `document_acl` inside the query. So this is held by every role including a
bare Member, and holding it grants access to no particular document.

Gating retrieval on an administrative permission instead would have inverted the model: a
role would decide what a person may read, the ACL filter would be a formality, and the one
sentence this product rests on would stop being true.

**The key names the query, not the evidence.** It was drafted as `evidence:read` and
`test_no_permission_grants_document_visibility` rejected it, which is that tripwire doing
exactly its job: a permission spelled after a data-plane object reads as a grant over that
object, whatever the docstring says.

Migration 0002 revoked application writes on `permissions` and `role_permissions`, so
widening the catalogue is impossible from the request path. Only a migration can do it,
and a migration is reviewable — the same property migration 0005 demonstrated.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

PERMISSION = "retrieval:query"
DESCRIPTION = "Issue a retrieval query. Results are filtered by source ACLs."

#: Every role. A Viewer and a Member ask the same questions as an Owner and get different
#: answers, because the answers are bounded by their grants rather than by their rank.
ROLES = ("owner", "super_admin", "hr_admin", "it_admin", "analyst", "viewer", "member")


def upgrade() -> None:
    op.bulk_insert(
        sa.table("permissions", sa.column("key", sa.String), sa.column("description", sa.Text)),
        [{"key": PERMISSION, "description": DESCRIPTION}],
    )
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_key", sa.String),
            sa.column("permission_key", sa.String),
        ),
        [{"role_key": role, "permission_key": PERMISSION} for role in ROLES],
    )


def downgrade() -> None:
    # Explicit rather than relying on the FK's cascade, so the reversal reads the same way
    # round as the application and does not depend on `ondelete` staying as it is.
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_key = :p").bindparams(p=PERMISSION)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = :p").bindparams(p=PERMISSION))
