"""Package-scoped Knowledge Basket sharing: one join table, and no new grant (ADR 0021).

A knowledge-transfer recipient could see none of the departing employee's uploaded files,
because a basket file's only ACL grant is `basket:{owner_user_id}` and `kt_documents`
resolves the *recipient's* principals — "the package contributes the period; it grants
nothing". This table closes that gap without weakening either sentence.

**A row here is a reference, not a permission.** It records that a file was attached to a
package. Whether the caller may read it is decided fresh on every request by `_open_for`,
which already re-decides binding, revocation, completion and expiry from the cookie
principal plus the code. So revoking a package closes access to its files in the same
instant, with no grant row to sweep and no window in which a revoked package still serves
bytes — and no `expires_at` here, because a second expiry that could disagree with the
package's is a bug waiting to be written.

**Both foreign keys are composite on `(id, org_id)`.** A row physically cannot reference a
package in one tenant and a file in another; the database refuses it rather than a
predicate remembering to check. `basket_files` gains the matching unique constraint here
for the same reason migration 0019 added `uq_kt_packages_id_org_id`.

No permission is created. Attaching is `kt:manage` — which already exists and already owns
package creation — or the package's own subject acting on their own files. Widening
`kt:manage` into a read over anyone's basket would be granting a permission to make a
screen work, which §17 forbids; seeing another employee's uploads stays `basket:manage`.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def upgrade() -> None:
    # The composite target the join table's FK needs. `kt_packages` got its equivalent in
    # 0019; `basket_files` never needed one until now.
    op.create_unique_constraint("uq_basket_files_id_org_id", "basket_files", ["id", "org_id"])

    op.create_table(
        "kt_package_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # Denormalised and NOT NULL, per ADR 0002: it is what the RLS policy reads, and
        # it is what makes both composite foreign keys expressible.
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("package_id", UUID(as_uuid=True), nullable=False),
        sa.Column("basket_file_id", UUID(as_uuid=True), nullable=False),
        # Who attached it. An attachment is an authorization-relevant act by a person
        # holding `kt:manage` (or by the subject over their own file), so it is recorded
        # on the row as well as in `audit_log`.
        sa.Column("attached_by", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "attached_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Detaching one file is the narrow act; revoking the package is the broad one and
        # needs nothing written here. Kept as two columns rather than a DELETE so the
        # record that a file WAS shared survives its unsharing.
        sa.Column("detached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detached_by", UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["package_id", "org_id"],
            ["kt_packages.id", "kt_packages.org_id"],
            ondelete="CASCADE",
        ),
        # CASCADE, not SET NULL: a basket file is soft-deleted (`deleted_at`), so this
        # only fires if a row is genuinely removed, and an attachment to a file that no
        # longer exists is not a fact worth keeping.
        sa.ForeignKeyConstraint(
            ["basket_file_id", "org_id"],
            ["basket_files.id", "basket_files.org_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["attached_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["detached_by"], ["users.id"], ondelete="SET NULL"),
        # Bare suffix: `models.py` renders "ck_%(table_name)s_%(constraint_name)s", so a
        # prefixed name here would produce ck_kt_package_files_ck_kt_package_files_...
        # (migration 0002 wrote this down after 0001 shipped it).
        sa.CheckConstraint("(detached_at IS NULL) = (detached_by IS NULL)", name="detachment"),
    )

    # A file is attached to a package once. Partial on `detached_at IS NULL`, so
    # detaching and re-attaching later is allowed and leaves both records.
    op.create_index(
        "uq_kt_package_files_live",
        "kt_package_files",
        ["package_id", "basket_file_id"],
        unique=True,
        postgresql_where=sa.text("detached_at IS NULL"),
    )
    # The recipient's list: everything live on one package.
    op.create_index(
        "ix_kt_package_files_package",
        "kt_package_files",
        ["org_id", "package_id"],
        postgresql_where=sa.text("detached_at IS NULL"),
    )
    # "Which packages is this file in" — what the owner's basket needs before it lets
    # somebody delete a file that a live handover depends on.
    op.create_index(
        "ix_kt_package_files_file",
        "kt_package_files",
        ["org_id", "basket_file_id"],
        postgresql_where=sa.text("detached_at IS NULL"),
    )

    op.execute("ALTER TABLE kt_package_files ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kt_package_files FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY kt_package_files_org_isolation ON kt_package_files "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON kt_package_files TO jutsu_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS kt_package_files_org_isolation ON kt_package_files")
    op.drop_table("kt_package_files")
    op.drop_constraint("uq_basket_files_id_org_id", "basket_files", type_="unique")
