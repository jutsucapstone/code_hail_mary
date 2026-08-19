"""The org-less index from an invitation token to its organisation.

Accepting an invitation is a pre-authentication operation: the invitee has no session, so
no tenant, so the `app.current_org_id` GUC is unset. `invitations` lives in `public` under
`ENABLE` + `FORCE ROW LEVEL SECURITY`, so the policy predicate is NULL and every row is
invisible — the consuming UPDATE matches nothing and a perfectly valid invitation is
rejected as "no longer valid".

Found by clicking a real invitation link, not by review. The token hash matched the stored
row exactly and the row was unaccepted, unrevoked and unexpired; it was simply not visible
to the statement trying to consume it. That is a failure that looks like bad data and is
actually a scoping bug, which is the worst kind to debug.

A `SECURITY DEFINER` function over `invitations` would not have fixed it. `FORCE` subjects
the table owner to the policies too, so a definer function owned by the owner sees exactly
as little.

So the same shape as `auth.jutsu_ids` and `auth.identity_memberships`: a narrow, org-less
lookup living in the schema that is protected by privilege rather than by RLS. It resolves
token to organisation and nothing else. The invitation itself stays in `public`, under
RLS, where admins list it — and the consuming UPDATE still runs there, scoped, so the
atomic single-use guarantee is unchanged.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UUID = pg.UUID(as_uuid=True)

FUNCTIONS = (
    "auth.record_invitation_token(bytea, uuid, uuid)",
    "auth.resolve_invitation_token(bytea)",
)


def upgrade() -> None:
    op.create_table(
        "invitation_tokens",
        # The hash is the key: the plaintext token exists only in the invitee's inbox.
        sa.Column("token_hash", sa.LargeBinary(32), primary_key=True),
        sa.Column("invitation_id", UUID, nullable=False, unique=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # No foreign key to `invitations`, for the same reason the JUTSU ID ledger has
        # none: this table has to remain readable from an unscoped context, and a
        # cascade from an org-scoped parent would couple its lifetime to rows this
        # schema cannot see.
        schema="auth",
    )
    op.execute("ALTER TABLE auth.invitation_tokens OWNER TO jutsu_auth")

    op.execute(
        """
        CREATE FUNCTION auth.record_invitation_token(
            p_token_hash bytea, p_invitation_id uuid, p_org_id uuid
        )
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.invitation_tokens (token_hash, invitation_id, org_id)
          VALUES (p_token_hash, p_invitation_id, p_org_id)
          RETURNING invitation_id;
        $fn$;
        """
    )

    # Returns the organisation and nothing else that matters. Deliberately not the email
    # or the role: those live on the invitation row and are read under RLS once the scope
    # is set, so this function cannot become a way to read invitation contents unscoped.
    op.execute(
        """
        CREATE FUNCTION auth.resolve_invitation_token(p_token_hash bytea)
        RETURNS TABLE (invitation_id uuid, org_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          SELECT t.invitation_id, t.org_id
            FROM auth.invitation_tokens t
           WHERE t.token_hash = p_token_hash
           LIMIT 1;
        $fn$;
        """
    )

    for signature in FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO jutsu_auth")

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
    op.drop_table("invitation_tokens", schema="auth")
