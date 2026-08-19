"""Add `profile:self_read`, held by every role.

`GET /v1/me` answers "who am I and what may I do". It was gated on `org:read`, which the
`member` role does not hold — so an invited employee could authenticate successfully and
then be refused their own identity, which the shell needs before it can render anything.
Reading your own profile is not an organisational privilege and should never have been
modelled as one.

This is also the first demonstration of a property migration 0002 deliberately bought:
`roles`, `permissions` and `role_permissions` are app-write-revoked, so widening the
catalogue is impossible from the request path. Only a migration can do it, and that
migration is reviewable. The cost is exactly this file; the benefit is that a compromised
handler cannot grant itself anything.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

PERMISSION = "profile:self_read"
DESCRIPTION = "Read one's own identity, role and permissions."

#: Every role, including `member`. Self-service is not a privilege — a person with no
#: administrative rights at all still has to be able to see who they are.
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
    # role_permissions cascades from permissions, but deleting explicitly keeps the
    # reversal readable and independent of that FK's ondelete staying as it is.
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_key = :p").bindparams(p=PERMISSION)
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = :p").bindparams(p=PERMISSION))
