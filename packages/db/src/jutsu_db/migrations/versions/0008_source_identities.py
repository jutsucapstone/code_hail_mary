"""Source identities, and the ACL principal that is not an email address.

`document_acl.principal_id` has always been documented as matching `users.external_id`.
Two things were wrong with that, and the second is the one that forces a schema change:

  * `users.external_id` is **never written**. Nothing in `apps/api` populates it, so every
    user's ACL principal is NULL. Migration 0002 made that deliberate and fail-closed — a
    pilot user has no IdP subject, so NULL means "matches no grant" — which is correct as
    a default and useless as an authorisation model.
  * **One column cannot hold six identities.** A JUTSU user simultaneously is a Google
    `sub`, an Entra `oid`, a Slack `U0…`, a GitHub numeric id, an Atlassian `accountId`
    and an email address in a mail corpus. `external_id` is singular. The mismatch is not
    "email versus subject", it is cardinality.

So the principal moves to `source_identities`: one row per (organisation, user, source
system), holding the **provider-native immutable subject**. `document_acl.principal_id`
keeps its shape and gains a namespace — `{source_system}:{subject}` — which is why this
migration adds a table and does not touch `document_acl` at all. See ADR 0010.

**Also fixed here: `user_groups` had no `org_id` and no RLS.** It feeds the group half of
the §12 retrieval filter, which makes it an authorisation input with no database-level
tenant isolation. It gets both, for the same reason `source_identities` has them from the
first line.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

#: Tables this migration puts behind row-level security. Kept as data so the upgrade, the
#: downgrade and the tests cannot disagree about which ones are protected.
RLS_TABLES = ("source_identities", "user_groups")


def upgrade() -> None:
    # ------------------------------------------------------------------ source_identities
    op.create_table(
        "source_identities",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        # Denormalised for RLS, exactly as ADR 0002 argues for chunks and document_acl:
        # the alternative is a correlated subquery through `users` on the hot path that
        # every retrieval query pays.
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        # The connector's own enum. A subject only means anything inside the system that
        # issued it — a Slack `U0…` and a GitHub id are different namespaces that happen
        # to be strings.
        # postgresql.ENUM with create_type=False, not sa.Enum. `sa.Enum` has no
        # create_type parameter — it swallows the keyword and still emits
        # `CREATE TYPE source_system AS ENUM ()`, which fails against the type migration
        # 0001 already created. Found by running it.
        sa.Column(
            "source_system",
            postgresql.ENUM(name="source_system", create_type=False),
            nullable=False,
        ),
        # The provider-native IMMUTABLE subject. Never an email address, except for the
        # local mail corpus where the address genuinely is the subject the source issues
        # (ADR 0008, ADR 0010).
        sa.Column("subject", sa.String(255), nullable=False),
        # Revocation is a flag rather than a delete, so offboarding leaves an audit trail.
        # Authorisation reads `is_active`, so flipping it takes effect on the next request
        # with nothing to invalidate (§17 test 3).
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "linked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        # How the link was established — an OAuth grant, a directory sync, an admin
        # action. §17 wants every ACL change auditable, and "who claimed this subject"
        # is the question an incident asks first.
        sa.Column("linked_by", sa.String(64), nullable=False, server_default="system"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Composite FK to (users.id, users.org_id), so the denormalised org_id cannot
        # drift from the user's. Requires the target unique constraint added below.
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_source_identities_user_id_org_id",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], ondelete="CASCADE", name="fk_source_identities_org_id"
        ),
        # One subject maps to at most one user *within an organisation*. Scoped to the
        # org rather than global on purpose: the same Slack workspace id can legitimately
        # appear in two tenants that both connect the same workspace, and a global unique
        # constraint would make the second tenant's link fail.
        sa.UniqueConstraint(
            "org_id", "source_system", "subject", name="uq_source_identities_org_system_subject"
        ),
    )

    # Resolution reads "every active identity for this user"; the ACL join reads
    # "which user owns this subject". One index for each direction.
    op.create_index(
        "ix_source_identities_org_id_user_id", "source_identities", ["org_id", "user_id"]
    )
    op.create_index(
        "ix_source_identities_org_id_source_system_subject",
        "source_identities",
        ["org_id", "source_system", "subject"],
    )

    # ------------------------------------------------------------------ user_groups
    #
    # `user_groups` shipped as (user_id, group_external_id) with no org_id and no RLS. It
    # supplies the group array in §12's filter, so it is an authorisation input that the
    # database was not isolating. Backfilling from `users` is exact: every row's owner
    # already determines its organisation.
    op.add_column("user_groups", sa.Column("org_id", UUID(as_uuid=True), nullable=True))
    op.execute(
        "UPDATE user_groups SET org_id = users.org_id FROM users WHERE users.id = user_groups.user_id"
    )
    # Any row whose user no longer exists cannot be attributed to a tenant, and an
    # unattributable authorisation row is worse than a missing one.
    op.execute("DELETE FROM user_groups WHERE org_id IS NULL")
    op.alter_column("user_groups", "org_id", nullable=False)

    op.create_foreign_key(
        "fk_user_groups_user_id_org_id",
        "user_groups",
        "users",
        ["user_id", "org_id"],
        ["id", "org_id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_user_groups_org_id_user_id", "user_groups", ["org_id", "user_id"])
    op.create_index(
        "ix_user_groups_org_id_group_external_id",
        "user_groups",
        ["org_id", "group_external_id"],
    )

    # ------------------------------------------------------------------ RLS
    #
    # Enabled AND forced. FORCE is the half that is easy to omit and it is the half that
    # matters: without it the policy is skipped for the table owner, and dev connects as
    # the owner — so every isolation test would pass against an unenforced policy and the
    # leak would appear only in production under a different role (ADR 0003).
    #
    # `NULLIF(…, '')` because `current_setting(…, true)` returns NULL only until the GUC
    # has been set once; afterwards a fresh transaction reads an empty string, and
    # `''::uuid` raises rather than filtering. Unset and reset must both fail closed.
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} "
            f"USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid) "
            f"WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)"
        )

    # The application role needs access to the new table. Guarded on existence because the
    # role is created by infrastructure, not by a migration — it is cluster-wide and its
    # password belongs in Secret Manager.
    op.execute(
        """
        DO $do$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON source_identities TO jutsu_app;
          END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_user_groups_org_id_group_external_id", table_name="user_groups")
    op.drop_index("ix_user_groups_org_id_user_id", table_name="user_groups")
    op.drop_constraint("fk_user_groups_user_id_org_id", "user_groups", type_="foreignkey")
    op.drop_column("user_groups", "org_id")

    op.drop_index(
        "ix_source_identities_org_id_source_system_subject", table_name="source_identities"
    )
    op.drop_index("ix_source_identities_org_id_user_id", table_name="source_identities")
    op.drop_table("source_identities")
