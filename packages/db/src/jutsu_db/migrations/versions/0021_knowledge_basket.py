"""The Knowledge Basket: one table of file metadata, one enum value, two permissions.

The bytes live in Google Cloud Storage (ADR 0020); this table is the only thing that
decides who may reach them. A signed URL is minted exclusively after a query against
`basket_files` under row-level security has already succeeded, so the row IS the
authorization — the bucket is a byte store with no opinion about tenants.

**`source_system` learns `basket`** the way it learned `zoom` in migration 0017, and for
the same reason: a file that yields text becomes a `RawDocument` and goes through
`persist_document`, which writes a `sources` row in that ACL namespace. Adding a knowledge
source is a migration on purpose.

**Two permissions, not one.** `basket:write` is holding a file in your own basket;
`basket:manage` is acting on somebody else's. Every role gets the first — the basket is
the employee's own workspace and gating it behind an admin permission would make the
feature useless to the people it is for — while the second follows the admin ladder.
Neither confers a document read: §17 keeps roles and ACLs apart, and a basket file is
visible through `document_acl` like everything else.

**The state column is a CHECK, not an application convention.** A pipeline with eleven
states and three failure branches is exactly the place where a typo becomes a row nobody
can query for, and the database is the only participant that cannot be bypassed by a new
call site.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

#: The lifecycle, in order. Terminal failures are separate values rather than a flag, so
#: "what happened" and "where it stopped" are one column instead of two that can disagree.
#:
#: `uploading` is the only state in which no bytes are guaranteed to exist: the row is
#: written before the signed URL is handed out, so an upload the browser abandons leaves a
#: row that the lifecycle rule and a sweeper can both recognise.
STATES = (
    "uploading",
    "uploaded",
    "validating",
    "extracting",
    "chunking",
    "embedding",
    "ready",
    # Stored, verified, and deliberately not searchable — an image, a video, an archive.
    # A separate state from `ready` because the UI must be able to say which it is
    # without re-deriving it from the MIME type.
    "stored",
    # The three ways it can end badly, kept apart because the remedy differs.
    "rejected",  # failed validation: wrong type, size mismatch, corrupt
    "failed",  # extraction or embedding failed; retryable
    "quarantined",  # refused for a security reason; NOT retryable by the employee
)

BASKET_WRITE = "basket:write"
BASKET_MANAGE = "basket:manage"

#: Every role. The basket is the employee's own workspace — see the module docstring.
WRITE_ROLES = (
    "owner",
    "super_admin",
    "hr_admin",
    "it_admin",
    "analyst",
    "viewer",
    "member",
)
MANAGE_ROLES = ("owner", "super_admin", "it_admin")


def _timestamp(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    # `ALTER TYPE ... ADD VALUE` in an autocommit block, exactly as 0017 did: Postgres
    # accepts it inside a transaction but refuses to *use* the value before commit, and
    # the table below references it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE source_system ADD VALUE IF NOT EXISTS 'basket'")

    op.create_table(
        "basket_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # Denormalised, like every tenant table since 0001 — the RLS predicate has to be
        # a column comparison rather than a correlated subquery (ADR 0002).
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        # Who uploaded it. Not the ACL principal — that lives in `document_acl` and is
        # minted by `owner_acl` — but the person the UI attributes it to and the person
        # whose basket it appears in.
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=False),
        # **Server-generated, and no user input reaches it** (ADR 0020). Unique across the
        # deployment so a bug that reused one would fail loudly instead of overwriting
        # somebody's file.
        sa.Column("object_key", sa.Text(), nullable=False),
        # As the person's filesystem had it, for display and download only. Never a path.
        sa.Column("original_filename", sa.Text(), nullable=False),
        # Lowercased, stripped of directory separators and control characters — what the
        # UI sorts and searches on, so the display name and the sort key cannot disagree.
        sa.Column("normalised_filename", sa.Text(), nullable=False),
        # What the CLIENT declared, kept because the signed URL was pinned to it and a
        # mismatch is evidence. `detected_mime` is what the bytes actually are.
        sa.Column("declared_mime", sa.String(255), nullable=False),
        sa.Column("detected_mime", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        # GCS's own crc32c, read back from the object rather than taken from the client.
        sa.Column("checksum_crc32c", sa.String(32), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="uploading"),
        # One sentence a person can act on. Never a stack trace, never a bucket path —
        # this is rendered in the browser and kept in the database (§4.9).
        sa.Column("failure_reason", sa.Text(), nullable=True),
        # The machine-readable half, for metrics and for deciding retryability.
        sa.Column("failure_kind", sa.String(48), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        # The document this file became, once it yielded text. NULL for a stored-only
        # file and for one that has not been extracted yet. `ondelete=SET NULL` rather
        # than CASCADE: superseding a document must never delete somebody's file.
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # How many characters of text came out. Zero is a real answer — a scanned PDF
        # with no text layer — and distinguishing it from NULL is what lets the UI say
        # "we could not read any text in this file" rather than "processing".
        sa.Column("extracted_chars", sa.Integer(), nullable=True),
        # Soft deletion. A file the employee removed must stop being listed and stop
        # being downloadable immediately, while the object and the audit trail survive
        # until the lifecycle rule collects it.
        _timestamp("deleted_at", nullable=True, default=False),
        sa.Column("deleted_by", UUID(as_uuid=True), nullable=True),
        sa.Column("meta_json", JSONB(), nullable=False, server_default="{}"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        # Bare suffixes. `models.py` sets "ck": "ck_%(table_name)s_%(constraint_name)s",
        # so a prefixed name here renders ck_basket_files_ck_basket_files_state — which is
        # exactly what migration 0001 shipped and 0002 wrote down not to repeat.
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in STATES) + ")",
            name="state",
        ),
        # A ceiling the database enforces even if an API bound is ever relaxed. 512 MiB
        # is comfortably past a long recording and far short of anything that would make
        # a single row a problem.
        sa.CheckConstraint("size_bytes > 0 AND size_bytes <= 536870912", name="size"),
        # A failed row must say why. Without this, "failed with no reason" is a
        # representable state and the UI has to invent a message for it.
        sa.CheckConstraint(
            "state NOT IN ('rejected', 'failed', 'quarantined') OR failure_reason IS NOT NULL",
            name="failure_has_reason",
        ),
        # Deletion is two columns that must move together.
        sa.CheckConstraint("(deleted_at IS NULL) = (deleted_by IS NULL)", name="deletion"),
    )

    op.create_index("uq_basket_files_object_key", "basket_files", ["object_key"], unique=True)
    # The list query: one employee's basket, newest first, excluding deleted.
    op.create_index(
        "ix_basket_files_owner",
        "basket_files",
        ["org_id", "owner_user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # The worker's queue-ish scan and the admin's "what is stuck" view.
    op.create_index("ix_basket_files_state", "basket_files", ["org_id", "state"])

    op.execute("ALTER TABLE basket_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE basket_files FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY basket_files_org_isolation ON basket_files "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON basket_files TO jutsu_app")

    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("key", sa.String),
            sa.column("description", sa.String),
        ),
        [
            {
                "key": BASKET_WRITE,
                "description": "Upload and manage files in your own Knowledge Basket",
            },
            {
                "key": BASKET_MANAGE,
                "description": "Act on any employee's Knowledge Basket in this organisation",
            },
        ],
    )
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_key", sa.String),
            sa.column("permission_key", sa.String),
        ),
        [{"role_key": role, "permission_key": BASKET_WRITE} for role in WRITE_ROLES]
        + [{"role_key": role, "permission_key": BASKET_MANAGE} for role in MANAGE_ROLES],
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_key IN (:w, :m)").bindparams(
            w=BASKET_WRITE, m=BASKET_MANAGE
        )
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE key IN (:w, :m)").bindparams(
            w=BASKET_WRITE, m=BASKET_MANAGE
        )
    )
    op.execute("DROP POLICY IF EXISTS basket_files_org_isolation ON basket_files")
    op.drop_index("ix_basket_files_state", table_name="basket_files")
    op.drop_index("ix_basket_files_owner", table_name="basket_files")
    op.drop_index("uq_basket_files_object_key", table_name="basket_files")
    op.drop_table("basket_files")
    # The `basket` enum value stays. Postgres cannot drop one, and an unused value is
    # harmless — migration 0017 made the same call for `zoom` and says why.
