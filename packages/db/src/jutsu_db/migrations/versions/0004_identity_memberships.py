"""The org-less membership index.

Sign-in starts with an email address and nothing else. Resolving it to an organisation
requires reading `users`, which is exactly what row-level security prevents — and cannot
be worked around, because the GUC that would scope the query is the very thing we are
trying to derive. `FORCE ROW LEVEL SECURITY` also closes the obvious escape: a
`SECURITY DEFINER` function owned by the table owner is still subject to the policy, so
it would return zero rows and the login path would fail closed looking like "no such
user".

So the pre-authentication lookup lives in the schema that is already org-less and already
protected by privilege rather than by RLS. This is the same reasoning that put
`auth.jutsu_ids` there: a deliberate, narrow index for the one query that runs before any
tenant is known.

It is a denormalised copy of `(users.identity_id, users.org_id, users.id)` and carries no
foreign keys, for the same reason the ledger does not — `orgs -> users` is ON DELETE
CASCADE, and a cascade here would delete the row that tells us where a person belongs
while their `users` row was being replaced. Consistency is maintained by the registration
and invitation-acceptance paths, both of which write it inside the same transaction as
the `users` row, and by `test_membership_index_matches_users`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UUID = pg.UUID(as_uuid=True)

FUNCTIONS = (
    "auth.record_membership(uuid, uuid, uuid)",
    "auth.resolve_memberships(uuid)",
)


def upgrade() -> None:
    op.create_table(
        "identity_memberships",
        sa.Column("identity_id", UUID, primary_key=True),
        sa.Column("org_id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # One identity resolves to at most one user per organisation. Without this a
        # duplicated row would make sign-in ambiguous rather than wrong, which is harder
        # to notice.
        sa.UniqueConstraint("user_id", name="uq_identity_memberships_user_id"),
        schema="auth",
    )
    op.execute("ALTER TABLE auth.identity_memberships OWNER TO jutsu_auth")

    op.execute(
        """
        CREATE FUNCTION auth.record_membership(
            p_identity_id uuid, p_org_id uuid, p_user_id uuid
        )
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.identity_memberships (identity_id, org_id, user_id)
          VALUES (p_identity_id, p_org_id, p_user_id)
          ON CONFLICT (identity_id, org_id) DO UPDATE SET user_id = EXCLUDED.user_id
          RETURNING user_id;
        $fn$;
        """
    )

    # Returns every organisation the identity belongs to. A person may be in more than
    # one — they are two `users` rows sharing one identity — so the caller has to decide
    # which to open a session against rather than assuming a single answer.
    op.execute(
        """
        CREATE FUNCTION auth.resolve_memberships(p_identity_id uuid)
        RETURNS TABLE (org_id uuid, user_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          SELECT m.org_id, m.user_id
            FROM auth.identity_memberships m
           WHERE m.identity_id = p_identity_id
           ORDER BY m.created_at;
        $fn$;
        """
    )

    for signature in FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO jutsu_auth")

    # S608: the interpolated values are the module-level FUNCTIONS tuple above — fixed
    # signatures written in this file, never request data. Bandit cannot distinguish a
    # constant from user input, and a parameterised form is not available for GRANT.
    op.execute(
        f"""
        DO $do$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_app') THEN
            {"".join(f"GRANT EXECUTE ON FUNCTION {sig} TO jutsu_app; " for sig in FUNCTIONS)}
          END IF;
        END $do$;
        """  # noqa: S608
    )


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.drop_table("identity_memberships", schema="auth")
