"""OAuth hardening: PKCE, a state that expires, and a home for sync checkpoints.

Three columns on `connections`, each closing a gap the live-connection audit named:

* `oauth_code_verifier` — the PKCE secret minted at `start_connection` and spent at the
  callback. Stored server-side like `oauth_state` (the browser never sees it; only the
  provider sees its S256 digest), so an intercepted authorization code is useless
  without a value that never left this database.
* `oauth_state_expires_at` — the state parameter was single-use but immortal: a
  `connecting` row abandoned in January could still complete in June. A state is now
  spendable for fifteen minutes, after which the callback treats it exactly like one
  that never existed.
* `sync_cursor` — per-connection incremental checkpoints for provider fetchers (page
  tokens, delta links, `updated_since` watermarks), JSONB because every provider names
  its cursor differently. Source-walk cursors stay on `sources.last_sync_cursor`; this
  is the connection-scoped half the OAuth providers need.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("connections", sa.Column("oauth_code_verifier", sa.String(128)))
    op.add_column("connections", sa.Column("oauth_state_expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "connections",
        sa.Column("sync_cursor", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("connections", "sync_cursor")
    op.drop_column("connections", "oauth_state_expires_at")
    op.drop_column("connections", "oauth_code_verifier")
