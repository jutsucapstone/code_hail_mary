"""Identity, org membership, RBAC and the audit split.

The control plane for the pilot onboarding platform: who a person is, which organisation
they belong to, what they may do, and what was done. Data-plane visibility is unchanged —
`document_acl` still matches `users.external_id`, and nothing here grants a document read.

Three things in this migration are load-bearing and are easy to "simplify" into a leak:

1. The `auth` schema is org-less BY NECESSITY. The magic-link path resolves an email
   before any organisation is known, and the employee path resolves a JUTSU ID before
   any organisation is known, so neither can be scoped by the `app.current_org_id` GUC.
   Because it cannot be protected by RLS, it is protected by PRIVILEGE instead:
   `jutsu_auth` owns it, `jutsu_app` gets EXECUTE on four narrow SECURITY DEFINER
   functions and NO table-level access at all. `jutsu_app` therefore cannot enumerate
   identities, sessions or JUTSU IDs even with arbitrary SQL.

   `jutsu_app` is deliberately NOT granted membership of `jutsu_auth`. Membership plus
   `SET ROLE` would look equivalent and is not: any statement on any pooled connection
   could then issue `SET ROLE jutsu_auth` and read every tenant's rows. That is the same
   shape of failure as ADR 0003 — a control that looks enforced and is a convention.

2. A `SECURITY DEFINER` function over an RLS table would NOT have worked as a
   substitute. `FORCE ROW LEVEL SECURITY` subjects the table *owner* to the policies
   too, so such a function returns zero rows with no error, and the login path fails
   closed in a way that reads as "user not found". Hence real tables in a role-gated
   schema rather than a clever function over `users`.

3. `GRANT ... ON ALL TABLES` in migration 0001 was a SNAPSHOT of the tables that existed
   then. Every table created here would otherwise have no privileges at all. Re-granting
   is not enough on its own either — `ALTER DEFAULT PRIVILEGES` covers future tables, and
   the catalogue tables then have their write privileges revoked back off.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB

#: Tables gaining org isolation here. `orgs` is absent because its tenant key is `id`,
#: not `org_id`, and so needs a differently-shaped policy (applied separately below).
NEW_RLS_TABLES = (
    "users",
    "user_roles",
    "employee_profiles",
    "invitations",
    "onboarding_steps",
    "audit_log",
)

#: Catalogue tables. Seeded by this migration and then made read-only to the application,
#: so a compromised request path cannot mint a role or widen a permission at runtime —
#: only a migration can. This is why permissions are data rather than a hardcoded enum.
CATALOGUE_TABLES = ("roles", "permissions", "role_permissions")

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

# Check constraints below are named with the BARE suffix. models.py sets the convention
# "ck": "ck_%(table_name)s_%(constraint_name)s", so passing a bare "status" renders
# ck_orgs_status. Migration 0001 passed the already-prefixed name and so shipped names
# like ck_chunks_ck_chunks_char_range; those are left alone rather than rewritten, but
# new constraints use the convention as it was designed.


def _org_policy(table: str) -> None:
    """ENABLE + FORCE + one policy, matching 0001 exactly.

    FORCE is not optional: without it the policy is skipped for the table owner, and the
    migration itself connects as the owner, so every test would pass against an
    unenforced policy.
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_org_isolation ON {table} "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )


def upgrade() -> None:
    # ------------------------------------------------------------------ auth role
    #
    # Created here rather than in infra/docker/initdb, because that script runs ONLY on
    # first initialisation of an empty data volume — every existing developer database
    # would silently miss the role, and a guarded skip would leave the login path failing
    # at runtime with "permission denied for schema auth". NOLOGIN, so there is no
    # password and therefore no secret to manage.
    op.execute(
        """
        DO $do$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_auth') THEN
            CREATE ROLE jutsu_auth NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
          END IF;
        END $do$;
        """
    )

    op.execute("CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION jutsu_auth")

    # ------------------------------------------------------------------ auth tables
    #
    # No RLS on any of these: they are org-less by necessity (see the module docstring),
    # and are protected by privilege instead.

    op.create_table(
        "identities",
        sa.Column("id", UUID, primary_key=True),
        # HMAC, never the address. The org-less lookup key must not be a readable email:
        # this schema sits outside every tenant boundary, so a leak here is a leak of the
        # whole customer list. The plaintext lives on `users.email`, under RLS.
        sa.Column("email_hmac", sa.LargeBinary(32), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        schema="auth",
    )

    op.create_table(
        "login_challenges",
        sa.Column("id", UUID, primary_key=True),
        # Nullable: a challenge is created for an UNREGISTERED address too, so that the
        # work done — and therefore the response time — is identical either way. Without
        # this the endpoint is an account-existence oracle no matter what it returns.
        sa.Column("identity_id", UUID, sa.ForeignKey("auth.identities.id", ondelete="CASCADE")),
        sa.Column("email_hmac", sa.LargeBinary(32), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False, unique=True),
        sa.Column("code_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts <= max_attempts", name="attempt_ceiling"),
        schema="auth",
    )
    op.create_index(
        "ix_login_challenges_expires_at", "login_challenges", ["expires_at"], schema="auth"
    )

    op.create_table(
        "sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "identity_id",
            UUID,
            sa.ForeignKey("auth.identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The org the session is scoped to. This is what makes a claim-free cookie
        # possible: the browser carries an opaque handle, and the org is resolved here,
        # server-side, so there is nothing in the cookie for a frontend route to branch on.
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False, unique=True),
        # Double-submit CSRF partner. Stored hashed for the same reason as the session
        # token: a database read must not yield a usable credential.
        sa.Column("csrf_hash", sa.LargeBinary(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        schema="auth",
    )
    op.create_index("ix_sessions_identity_id", "sessions", ["identity_id"], schema="auth")
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], schema="auth")

    # The JUTSU ID allocation ledger.
    #
    # A real table, org-less, for three reasons the brief does not state: the employee
    # entry flow resolves an ID before any org is known; global uniqueness must hold
    # outside every tenant boundary; and it must OUTLIVE the user row so an ID is never
    # reissued after erasure.
    #
    # `user_id` and `org_id` deliberately carry NO foreign keys. `orgs -> users` is
    # ON DELETE CASCADE, so an FK here would free every ID an organisation ever held the
    # moment it was deleted — which is the single thing this ledger exists to prevent.
    # Dangling references after erasure are the correct outcome: the row is a tombstone.
    op.create_table(
        "jutsu_ids",
        sa.Column("jutsu_id", sa.String(24), primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("user_id", UUID),
        sa.Column("kind", sa.String(3), nullable=False),
        sa.Column(
            "allocated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('EMP', 'ADM', 'HR')", name="kind"),
        # Crockford base32 for the random part: I, L, O and U are excluded, so a
        # mis-transcribed character is recoverable rather than ambiguous. Eight characters
        # over a 32-symbol alphabet is exactly 40 bits, which is why five CSPRNG bytes map
        # onto it with zero modulo bias — a 31-symbol set would need rejection sampling.
        sa.CheckConstraint(
            r"jutsu_id ~ '^JUTSU-(EMP|ADM|HR)-[0-9ABCDEFGHJKMNPQRSTVWXYZ]{8}$'",
            name="format",
        ),
        # Composite-FK target for users.jutsu_id.
        sa.UniqueConstraint("jutsu_id", "org_id", name="uq_jutsu_ids_jutsu_id_org_id"),
        schema="auth",
    )
    op.create_index("ix_jutsu_ids_org_id", "jutsu_ids", ["org_id"], schema="auth")

    # Ownership has to follow the schema, not just sit beside it.
    #
    # The migration runs as the database owner, so it is the owner of everything it
    # creates — including these tables. A SECURITY DEFINER function executes as ITS OWN
    # owner (jutsu_auth), which has no privilege on tables owned by someone else, so the
    # resolvers below would be denied on their own schema. Verified the hard way:
    # "permission denied for table sessions" raised from inside auth.resolve_session.
    #
    # Transferring ownership is what makes the definer indirection actually work, and it
    # keeps the boundary intact — jutsu_app still holds no table privilege here at all.
    for _auth_table in ("identities", "login_challenges", "sessions", "jutsu_ids"):
        op.execute(f"ALTER TABLE auth.{_auth_table} OWNER TO jutsu_auth")

    # ------------------------------------------------------------------ orgs
    op.add_column("orgs", sa.Column("domain", sa.String(255)))
    op.add_column(
        "orgs", sa.Column("status", sa.String(16), nullable=False, server_default="active")
    )
    op.add_column("orgs", sa.Column("size_band", sa.String(16)))
    op.add_column(
        "orgs",
        sa.Column("settings_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "orgs",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_check_constraint("status", "orgs", "status IN ('active', 'suspended', 'closed')")
    # Duplicate-organisation detection (§2 of the brief). Partial, so a closed org does
    # not permanently reserve a domain.
    op.execute(
        "CREATE UNIQUE INDEX uq_orgs_domain_active ON orgs (lower(domain)) "
        "WHERE domain IS NOT NULL AND status <> 'closed'"
    )

    # `orgs` is tenant-scoped on `id`, not `org_id`, so it needs its own policy shape.
    op.execute("ALTER TABLE orgs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE orgs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY orgs_org_isolation ON orgs "
        "USING (id = NULLIF(current_setting('app.current_org_id', true), '')::uuid) "
        "WITH CHECK (id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)"
    )

    # ------------------------------------------------------------------ users
    #
    # `external_id` becomes NULLABLE, and that is a security decision, not a convenience.
    # It is the ACL principal that `document_acl.principal_id` matches. A pilot user has
    # no IdP principal yet, so NULL means "matches no ACL grant" — the user sees no
    # documents at all. That is the §2 invariant holding, not a bug, and it is why the
    # JUTSU ID must never be written here.
    op.alter_column("users", "external_id", existing_type=sa.String(255), nullable=True)
    op.add_column("users", sa.Column("identity_id", UUID))
    op.add_column("users", sa.Column("jutsu_id", sa.String(24)))
    op.add_column(
        "users", sa.Column("status", sa.String(16), nullable=False, server_default="invited")
    )
    op.add_column(
        "users",
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column("users", sa.Column("activated_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("deactivated_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("last_activity_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "status", "users", "status IN ('invited', 'active', 'suspended', 'deactivated')"
    )
    # Composite-FK target, so every org-scoped child can prove it belongs to the same org
    # as its user. Referential integrity checks bypass row security entirely, so a plain
    # `REFERENCES users(id)` would NOT stop org A attaching a row to org B's user.
    op.create_unique_constraint("uq_users_id_org_id", "users", ["id", "org_id"])
    op.create_unique_constraint("uq_users_jutsu_id", "users", ["jutsu_id"])
    op.create_foreign_key(
        "fk_users_jutsu_id_org_id",
        "users",
        "jutsu_ids",
        ["jutsu_id", "org_id"],
        ["jutsu_id", "org_id"],
        referent_schema="auth",
    )
    op.create_index("ix_users_org_id_status", "users", ["org_id", "status"])

    # ------------------------------------------------------------------ RBAC catalogue
    op.create_table(
        "roles",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        # Spaced order, deliberately NOT unique. The escalation rule is strict —
        # an admin may only grant a role ranked strictly BELOW their own — so equal ranks
        # express genuine peers: HR Admin and IT Admin have disjoint powers and neither
        # may promote anyone into the other. A unique rank would force a false total
        # order and hand one of them authority over the other. The gaps leave room to
        # insert a role later without renumbering.
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
    )
    op.create_table(
        "permissions",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("description", sa.Text, nullable=False),
    )
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_key",
            sa.String(32),
            sa.ForeignKey("roles.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "permission_key",
            sa.String(64),
            sa.ForeignKey("permissions.key", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("role_key", sa.String(32), sa.ForeignKey("roles.key"), nullable=False),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("granted_by", UUID),
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_user_roles_user_id_org_id",
        ),
    )
    op.create_index("ix_user_roles_org_id_role_key", "user_roles", ["org_id", "role_key"])

    # ------------------------------------------------------------------ employee data
    #
    # Split from `users` rather than merged into it. An IT Admin or Organization Owner is
    # a `users` row with NO profile — department, designation and joining date are
    # meaningfully NOT NULL only for employees. The split also means the employee writes
    # this table during onboarding while an admin writes the identity row, so UPDATE
    # authorisation can be reasoned about per table, and HR contact data gets its own
    # erasure surface.
    op.create_table(
        "employee_profiles",
        sa.Column("user_id", UUID, primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("employee_code", sa.String(64)),
        sa.Column("department", sa.String(128)),
        sa.Column("designation", sa.String(128)),
        sa.Column("joining_date", sa.Date),
        sa.Column("phone_e164", sa.String(20)),
        sa.Column("skills", pg.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("responsibilities", sa.Text),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_employee_profiles_user_id_org_id",
        ),
    )

    op.create_table(
        "onboarding_steps",
        sa.Column("user_id", UUID, primary_key=True),
        sa.Column("step_key", sa.String(48), primary_key=True),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_onboarding_steps_user_id_org_id",
        ),
    )

    # ------------------------------------------------------------------ invitations
    op.create_table(
        "invitations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role_key", sa.String(32), sa.ForeignKey("roles.key"), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False, unique=True),
        sa.Column("invited_by", UUID, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    # Partial, so re-inviting an address whose earlier invitation was accepted, revoked
    # or expired is allowed while two live invitations for one address are not. A plain
    # UNIQUE would make a revoked invitation block the address forever.
    op.execute(
        "CREATE UNIQUE INDEX uq_invitations_org_email_live ON invitations (org_id, lower(email)) "
        "WHERE accepted_at IS NULL AND revoked_at IS NULL"
    )

    # ------------------------------------------------------------------ audit
    op.add_column(
        "audit_log", sa.Column("actor_type", sa.String(16), nullable=False, server_default="user")
    )
    op.add_column("audit_log", sa.Column("correlation_id", sa.String(64)))
    op.add_column(
        "audit_log", sa.Column("outcome", sa.String(16), nullable=False, server_default="success")
    )
    op.create_check_constraint(
        "outcome", "audit_log", "outcome IN ('success', 'denied', 'failure')"
    )
    op.create_index("ix_audit_log_correlation_id", "audit_log", ["correlation_id"])

    # Personal data is split OFF the immutable row rather than stored on it.
    #
    # The audit event itself must be tamper-evident, so UPDATE and DELETE are revoked
    # below. But §17 commits the project to DPDP/GDPR erasure that cascades, and an IP
    # address is personal data — if it lived on the immutable row, erasure would be
    # physically impossible. Keeping it in a deletable side table means erasure removes
    # the context and leaves the event with an opaque actor_id, which is exactly what a
    # forensic trail should look like afterwards.
    op.create_table(
        "audit_actor_context",
        sa.Column(
            "audit_id",
            sa.BigInteger,
            sa.ForeignKey("audit_log.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("org_id", UUID, nullable=False),
        sa.Column("actor_ip", pg.INET),
        sa.Column("user_agent", sa.Text),
    )

    for table in NEW_RLS_TABLES:
        _org_policy(table)
    _org_policy("audit_actor_context")

    # ------------------------------------------------------------------ seed catalogue
    _seed_rbac()

    # ------------------------------------------------------------------ auth functions
    _create_auth_functions()

    # ------------------------------------------------------------------ immutability
    #
    # A trigger, not application discipline. The JUTSU ID is a permanent public
    # identifier: reassigning one silently re-points every human reference to it.
    op.execute(
        """
        CREATE FUNCTION auth.reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
          RAISE EXCEPTION 'jutsu_id is immutable once allocated';
        END;
        $fn$;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_jutsu_ids_immutable BEFORE UPDATE OF jutsu_id ON auth.jutsu_ids "
        "FOR EACH ROW WHEN (OLD.jutsu_id IS DISTINCT FROM NEW.jutsu_id) "
        "EXECUTE FUNCTION auth.reject_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_users_jutsu_id_immutable BEFORE UPDATE OF jutsu_id ON users "
        "FOR EACH ROW WHEN (OLD.jutsu_id IS NOT NULL "
        "AND OLD.jutsu_id IS DISTINCT FROM NEW.jutsu_id) "
        "EXECUTE FUNCTION auth.reject_mutation()"
    )

    # ------------------------------------------------------------------ grants
    _apply_grants()


def _seed_rbac() -> None:
    """Seed roles, permissions and the matrix.

    Seeded as data rather than compiled into the application so that the database is the
    runtime authority and the grants below can make it read-only. A test asserts the code
    enum and these rows are identical, so the two cannot drift.
    """
    roles = [
        ("owner", "Organization Owner", 100, "Full control, including billing and deletion."),
        ("super_admin", "Super Admin", 80, "Full administrative control except org deletion."),
        ("hr_admin", "HR Admin", 60, "Manages people: invitations, profiles, lifecycle."),
        ("it_admin", "IT Admin", 60, "Manages integrations and organisation settings."),
        ("analyst", "Analyst", 40, "Reads aggregate insight; cannot administer."),
        ("viewer", "Viewer", 20, "Read-only access to what their ACLs already permit."),
        ("member", "Member", 10, "An onboarded employee with no administrative rights."),
    ]
    op.bulk_insert(
        sa.table(
            "roles",
            sa.column("key", sa.String),
            sa.column("name", sa.String),
            sa.column("rank", sa.Integer),
            sa.column("description", sa.Text),
        ),
        [{"key": k, "name": n, "rank": r, "description": d} for k, n, r, d in roles],
    )

    permissions = [
        ("org:read", "View organisation profile and overview."),
        ("org:delete", "Close the organisation. Owner only."),
        ("org:update", "Change organisation settings."),
        ("member:read", "List and view people in the organisation."),
        ("member:invite", "Invite a person to the organisation."),
        ("member:update", "Change a person's profile or lifecycle state."),
        ("member:assign_role", "Grant or revoke a role."),
        ("integration:read", "View integration connection state."),
        ("integration:connect", "Connect or reconnect an organisation integration."),
        ("integration:revoke", "Disconnect an organisation integration."),
        ("audit:read", "Read the audit log."),
        ("profile:self_update", "Edit one's own profile."),
        ("integration:self_manage", "Connect or disconnect one's own accounts."),
    ]
    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("key", sa.String),
            sa.column("description", sa.Text),
        ),
        [{"key": k, "description": d} for k, d in permissions],
    )

    everyone = ["profile:self_update", "integration:self_manage"]
    matrix: dict[str, list[str]] = {
        "owner": [p for p, _ in permissions],
        # Everything the owner has except closing the organisation — that is the one
        # power the role exists to withhold.
        "super_admin": [p for p, _ in permissions if p != "org:delete"],
        "hr_admin": [
            "org:read",
            "member:read",
            "member:invite",
            "member:update",
            "member:assign_role",
            "audit:read",
            *everyone,
        ],
        "it_admin": [
            "org:read",
            "org:update",
            "member:read",
            "integration:read",
            "integration:connect",
            "integration:revoke",
            "audit:read",
            *everyone,
        ],
        "analyst": ["org:read", "member:read", "integration:read", *everyone],
        "viewer": ["org:read", *everyone],
        "member": [*everyone],
    }
    rows = [
        {"role_key": role, "permission_key": perm}
        for role, perms in matrix.items()
        for perm in sorted(set(perms))
    ]
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_key", sa.String),
            sa.column("permission_key", sa.String),
        ),
        rows,
    )


def _create_auth_functions() -> None:
    """The only way `jutsu_app` reaches the `auth` schema.

    Each is SECURITY DEFINER, owned by `jutsu_auth`, pinned to a fixed `search_path` so a
    caller cannot shadow a table name, parameterised by a hash or an id, and returns at
    most one row. `jutsu_app` gets EXECUTE and nothing else — so it cannot enumerate
    identities, sessions or JUTSU IDs even with arbitrary SQL.
    """
    op.execute(
        """
        CREATE FUNCTION auth.resolve_session(p_token_hash bytea)
        RETURNS TABLE (session_id uuid, identity_id uuid, user_id uuid, org_id uuid,
                       csrf_hash bytea, expires_at timestamptz, idle_expires_at timestamptz)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          SELECT s.id, s.identity_id, s.user_id, s.org_id, s.csrf_hash,
                 s.expires_at, s.idle_expires_at
            FROM auth.sessions s
           WHERE s.token_hash = p_token_hash
             AND s.revoked_at IS NULL
             AND s.expires_at > now()
             AND s.idle_expires_at > now()
           LIMIT 1;
        $fn$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION auth.resolve_jutsu_id(p_jutsu_id text)
        RETURNS TABLE (org_id uuid, user_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          SELECT j.org_id, j.user_id
            FROM auth.jutsu_ids j
           WHERE j.jutsu_id = upper(p_jutsu_id)
             AND j.revoked_at IS NULL
           LIMIT 1;
        $fn$;
        """
    )
    # Allocation. ON CONFLICT DO NOTHING rather than catching a unique violation: an
    # IntegrityError would poison the enclosing registration transaction, aborting every
    # statement after it. Returning zero rows is a clean "try another candidate".
    op.execute(
        """
        CREATE FUNCTION auth.reserve_jutsu_id(p_jutsu_id text, p_org_id uuid, p_kind text)
        RETURNS text
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.jutsu_ids (jutsu_id, org_id, kind)
          VALUES (upper(p_jutsu_id), p_org_id, p_kind)
          ON CONFLICT (jutsu_id) DO NOTHING
          RETURNING jutsu_id;
        $fn$;
        """
    )
    # Binding an allocated id to a user is a compare-and-set, so two concurrent claims
    # cannot both succeed: the second sees zero rows.
    op.execute(
        """
        CREATE FUNCTION auth.claim_jutsu_id(p_jutsu_id text, p_org_id uuid, p_user_id uuid)
        RETURNS text
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          UPDATE auth.jutsu_ids
             SET user_id = p_user_id
           WHERE jutsu_id = upper(p_jutsu_id)
             AND org_id = p_org_id
             AND user_id IS NULL
             AND revoked_at IS NULL
          RETURNING jutsu_id;
        $fn$;
        """
    )

    for fn in (
        "auth.resolve_session(bytea)",
        "auth.resolve_jutsu_id(text)",
        "auth.reserve_jutsu_id(text, uuid, text)",
        "auth.claim_jutsu_id(text, uuid, uuid)",
    ):
        op.execute(f"ALTER FUNCTION {fn} OWNER TO jutsu_auth")


def _apply_grants() -> None:
    """Privileges for the application role.

    Migration 0001's `GRANT ... ON ALL TABLES` was a snapshot: without this block every
    table created above would be unreadable. `ALTER DEFAULT PRIVILEGES` then covers
    tables added by later migrations — and because it also auto-grants writes on any
    FUTURE catalogue table, the catalogue REVOKEs are re-applied explicitly rather than
    relied upon to survive.
    """
    op.execute(
        """
        DO $do$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO jutsu_app;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO jutsu_app;

            -- Covers tables and sequences created by future migrations, so this class of
            -- omission cannot recur.
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
              GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO jutsu_app;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
              GRANT USAGE, SELECT ON SEQUENCES TO jutsu_app;

            -- The catalogue is migration-owned. Read it, never write it.
            REVOKE INSERT, UPDATE, DELETE ON roles, permissions, role_permissions FROM jutsu_app;

            -- What finally makes the audit_log docstring's word "immutable" true. Until
            -- now the application role held UPDATE and DELETE on it.
            REVOKE UPDATE, DELETE ON audit_log FROM jutsu_app;

            -- The auth schema is reachable only through the four definer functions.
            -- No USAGE on the schema, no table privileges: EXECUTE alone.
            GRANT EXECUTE ON FUNCTION auth.resolve_session(bytea) TO jutsu_app;
            GRANT EXECUTE ON FUNCTION auth.resolve_jutsu_id(text) TO jutsu_app;
            GRANT EXECUTE ON FUNCTION auth.reserve_jutsu_id(text, uuid, text) TO jutsu_app;
            GRANT EXECUTE ON FUNCTION auth.claim_jutsu_id(text, uuid, uuid) TO jutsu_app;
            GRANT USAGE ON SCHEMA auth TO jutsu_app;
          END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $do$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_app') THEN
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
              REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM jutsu_app;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public
              REVOKE USAGE, SELECT ON SEQUENCES FROM jutsu_app;
            GRANT UPDATE, DELETE ON audit_log TO jutsu_app;
          END IF;
        END $do$;
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_users_jutsu_id_immutable ON users")
    op.execute("DROP TRIGGER IF EXISTS trg_jutsu_ids_immutable ON auth.jutsu_ids")

    op.execute("DROP POLICY IF EXISTS audit_actor_context_org_isolation ON audit_actor_context")
    op.execute("ALTER TABLE audit_actor_context NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_actor_context DISABLE ROW LEVEL SECURITY")
    for table in NEW_RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS orgs_org_isolation ON orgs")
    op.execute("ALTER TABLE orgs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE orgs DISABLE ROW LEVEL SECURITY")

    op.drop_table("audit_actor_context")
    op.drop_index("ix_audit_log_correlation_id", table_name="audit_log")
    op.drop_constraint("outcome", "audit_log", type_="check")
    op.drop_column("audit_log", "outcome")
    op.drop_column("audit_log", "correlation_id")
    op.drop_column("audit_log", "actor_type")

    op.execute("DROP INDEX IF EXISTS uq_invitations_org_email_live")
    op.drop_table("invitations")
    op.drop_table("onboarding_steps")
    op.drop_table("employee_profiles")
    op.drop_index("ix_user_roles_org_id_role_key", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")

    op.drop_index("ix_users_org_id_status", table_name="users")
    op.drop_constraint("fk_users_jutsu_id_org_id", "users", type_="foreignkey")
    op.drop_constraint("uq_users_jutsu_id", "users", type_="unique")
    op.drop_constraint("uq_users_id_org_id", "users", type_="unique")
    op.drop_constraint("status", "users", type_="check")
    for column in (
        "last_activity_at",
        "deactivated_at",
        "activated_at",
        "created_at",
        "status",
        "jutsu_id",
        "identity_id",
    ):
        op.drop_column("users", column)
    # Reverting to a NOT NULL `external_id` is only possible if no row violates it, and
    # this migration's whole purpose is to allow rows that do: a pilot user has no IdP
    # subject yet. Those rows are exactly the ones 0001's schema cannot represent, so
    # removing them is the faithful reversal rather than an incidental cleanup.
    #
    # It is nonetheless destructive, and silently so, which is why it is called out here
    # rather than left for someone to discover: downgrading past 0002 deletes every
    # account created through the pilot onboarding flow. Anything else would mean
    # inventing an `external_id`, and a fabricated ACL principal is far worse than a
    # deleted row — it would match grants that were never meant for that person.
    #
    # Found by an integration test rather than by review: the first reversibility check
    # ran against an empty database, where this passes.
    op.execute("DELETE FROM users WHERE external_id IS NULL")
    op.alter_column("users", "external_id", existing_type=sa.String(255), nullable=False)

    op.execute("DROP INDEX IF EXISTS uq_orgs_domain_active")
    op.drop_constraint("status", "orgs", type_="check")
    for column in ("updated_at", "settings_json", "size_band", "status", "domain"):
        op.drop_column("orgs", column)

    # CASCADE takes the four definer functions, the trigger function and the tables with
    # it. The role is left in place: it is cluster-wide and may own objects elsewhere.
    op.execute("DROP SCHEMA IF EXISTS auth CASCADE")
