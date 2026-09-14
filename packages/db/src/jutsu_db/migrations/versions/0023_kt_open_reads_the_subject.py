"""`kt:open` stops describing itself as granting no document (ADR 0025).

Migration 0013 described the permission as "Open a knowledge-transfer package addressed to
you. Grants no document." ADR 0025 made an opened package a read capability over its
subject's own documents, so the second sentence became false — and it is the sentence an
administrator reads when deciding who holds the permission, because `GET /v1/roles` serves
the catalogue from the database. The catalogue is migration-only since 0002, so correcting
its wording is a migration and nothing else.

Data only. No table, column, policy or grant changes, and the permission's key and role
assignments are untouched. Downgrade restores 0013's sentence exactly.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_BEFORE = "Open a knowledge-transfer package addressed to you. Grants no document."
_AFTER = (
    "Open a knowledge-transfer package addressed to you, and read its subject's own "
    "documents within the package's scope and period while it stays open."
)


def _describe(description: str) -> None:
    op.execute(
        sa.text(
            "UPDATE permissions SET description = :description WHERE key = 'kt:open'"
        ).bindparams(description=description)
    )


def upgrade() -> None:
    _describe(_AFTER)


def downgrade() -> None:
    _describe(_BEFORE)
