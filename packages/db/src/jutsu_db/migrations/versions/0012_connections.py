"""Employee-owned application connections, their policies, and their credentials.

The product principle this schema serves (§2 of the UI brief): **employees connect and
authorize their own applications; administrators govern, monitor and audit those
connections**. Nothing here lets an administrator connect on an employee's behalf — a
connection row is created by its owner's session and nobody else's.

Three tables, three different jobs:

* `connections` — one row per (person, provider) attempt. The lifecycle lives in
  `status`, whose values are a CHECK constraint because the UI's state machine and the
  database's must be the same machine. One *live* connection per person per provider,
  enforced by a partial unique index — `disconnected` rows are history and do not block
  reconnecting.

* `connection_policies` — the organisation's allow/deny per provider. **Absence means
  allowed.** That is the product default (employees connect their own tools; governance
  restricts), and it is deliberate: a fail-closed default here would make every
  deployment start with thirteen "why is this disabled" tickets rather than a working
  product. Restricting is one row, and the row records who decided.

* `connection_credentials` — OAuth tokens, encrypted at the application layer before
  they arrive, in a table no API response is ever built from. Separate from
  `connections` so that serialising a connection can never drag a token along by
  accident: the dangerous columns live where no read model looks. `org_id` is
  denormalised onto it for the same reason as chunks (ADR 0002) — RLS needs it on the
  row, not through a join.

**A connection is not a source identity.** Linking a source identity grants document
visibility (`document_acl` matches against it); a connection stores the means to fetch
content. The OAuth callback proves a provider subject the way email verification proves
an address, but mapping the thirteen providers onto the seven ACL namespaces is an
authorization design decision (which namespace does a Google `sub` grant under — gmail?
drive? both?) that deserves its own ADR before any code writes it. Until then the proven
subject is stored on the connection row and grants nothing.

Additive only; default privileges from 0002 grant the application role access.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

#: The provider catalogue, as data. Adding a provider is a migration on purpose: the
#: list is part of the product contract (it drives the UI catalogue, the policy table
#: and the OAuth registry), and a CHECK constraint means a typo'd provider id is an
#: insert error rather than a row no catalogue entry will ever match.
PROVIDERS = (
    "google_drive",
    "gmail",
    "google_calendar",
    "google_meet",
    "onedrive",
    "teams",
    "sharepoint",
    "slack",
    "jira",
    "confluence",
    "github",
)

STATUSES = ("connecting", "connected", "syncing", "error", "reauth_required", "disconnected")

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def _org_policy(table: str) -> None:
    """ENABLE + FORCE + one policy, matching every tenant table since 0001."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_org_isolation ON {table} "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )


def _provider_list() -> str:
    return ", ".join(f"'{p}'" for p in PROVIDERS)


def upgrade() -> None:
    # ------------------------------------------------------------------ connections
    op.create_table(
        "connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="connecting"),
        #: How the provider names the account, for display: an address or account name.
        #: Shown to the owner and to holders of integration:read; never a token.
        sa.Column("account_label", sa.String(320)),
        #: The subject the OAuth flow proved, stored but granting nothing — see the
        #: module docstring for why it does not become a source identity here.
        sa.Column("provider_subject", sa.String(255)),
        sa.Column("scopes", JSONB, nullable=False, server_default="[]"),
        #: One-shot CSRF binding for the OAuth round trip. Set at connect, cleared at
        #: callback; unique so a state can only ever match one row.
        sa.Column("oauth_state", sa.String(64), unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        #: Classified, never the provider's error body (§4.9).
        sa.Column("last_error_kind", sa.String(32)),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(f"provider IN ({_provider_list()})", name="provider"),
        sa.CheckConstraint(
            "status IN ('connecting', 'connected', 'syncing', 'error', "
            "'reauth_required', 'disconnected')",
            name="status",
        ),
    )
    # One live connection per person per provider. Partial, so history does not block
    # reconnecting — the same shape as the live-invitation index in 0002.
    op.execute(
        "CREATE UNIQUE INDEX uq_connections_live ON connections (org_id, user_id, provider) "
        "WHERE status != 'disconnected'"
    )
    # The admin aggregate ("47 connected, 2 syncing, 1 reauth") groups by exactly this.
    op.create_index(
        "ix_connections_org_provider_status", "connections", ["org_id", "provider", "status"]
    )
    _org_policy("connections")

    # ------------------------------------------------------------ connection_policies
    op.create_table(
        "connection_policies",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("allowed", sa.Boolean, nullable=False),
        sa.Column("updated_by", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id", "provider", name="pk_connection_policies"),
        sa.CheckConstraint(f"provider IN ({_provider_list()})", name="provider"),
    )
    _org_policy("connection_policies")

    # --------------------------------------------------------- connection_credentials
    op.create_table(
        "connection_credentials",
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        #: Fernet ciphertext. The key lives in Secret Manager (JUTSU_CONNECTION_KEY);
        #: a database dump alone yields nothing usable.
        sa.Column("access_token_enc", sa.LargeBinary, nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
    )
    _org_policy("connection_credentials")


def downgrade() -> None:
    for table in ("connection_credentials", "connection_policies", "connections"):
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.drop_table(table)
