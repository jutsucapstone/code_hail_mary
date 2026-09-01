"""One ACTIVE holder per subject, so revoke-then-link can actually happen.

Migration 0008 made `(org_id, source_system, subject)` unconditionally unique, and
`identities.py` was written against that: a duplicate link raises a Conflict whose
message says a transfer "must be an explicit revoke-then-link". But revocation is a
flag, not a delete — the revoked row keeps occupying the constraint, so the second half
of that instruction could never run:

  * An administrator moving a leaver's subject to their successor got a permanent 409.
    The transfer flow the product prescribes was impossible by schema.
  * An employee whose local identity was revoked and who re-proved their mailbox
    regained nothing: the automatic link's `ON CONFLICT DO NOTHING` swallowed the
    insert silently — fail-closed against the wrong row.

The constraint becomes a partial unique INDEX over the same columns `WHERE is_active`,
exactly the shape migration 0010 gave document versioning. One *active* holder per
subject per tenant; revoked rows are history and stay put. Nothing about "one subject
is one person" weakens: a second ACTIVE claim on a subject is refused as before.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_source_identities_org_system_subject", "source_identities", type_="unique"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_source_identities_active_org_system_subject "
        "ON source_identities (org_id, source_system, subject) WHERE is_active"
    )


def downgrade() -> None:
    # **This fails loudly if any subject has ever been re-linked after a revocation**,
    # and that is the guard working rather than a rough edge — the same posture as
    # migration 0010's downgrade over superseded documents. A revoked row and its
    # successor share the three columns, so the unconditional constraint cannot
    # represent that history; restoring it would mean deleting revocation rows, and a
    # migration that silently destroyed an ACL audit trail would be far worse than one
    # that refuses to run.
    op.execute("DROP INDEX uq_source_identities_active_org_system_subject")
    op.create_unique_constraint(
        "uq_source_identities_org_system_subject",
        "source_identities",
        ["org_id", "source_system", "subject"],
    )
