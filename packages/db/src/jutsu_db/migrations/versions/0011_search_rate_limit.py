"""A per-caller budget for the one endpoint that spends money on demand.

`POST /v1/search` embeds the caller's question before it can search, which is a paid
Vertex request per call. Nothing bounded how many. The per-request `TokenLedger` bounds
*one* request; it says nothing about a thousand of them, and the endpoint is reachable by
every authenticated employee because `retrieval:query` is held by every role.

**Why a table and not a process counter.** The API runs as more than one Cloud Run
instance, and a limiter in Python memory is per instance: N instances means N times the
limit, and an autoscaler makes the real ceiling a function of traffic. The counter has to
live where every instance can see it, and the only such store the API already talks to is
this database.

**Shaped after `auth.registration_budget` (migration 0007), with two deliberate
differences.**

*It is a tenant table under RLS, not an org-less one in `auth`.* Search is org-scoped, so
non-negotiable 7 applies — `org_id` on every row — and the policy below is what enforces
it rather than every call site remembering to. It also avoids the trap ADR 0012 records:
a `SECURITY DEFINER` function over a FORCE-RLS table returns zero rows with no error, so
the registration pattern's helper function would be exactly the wrong shape here. The
application role writes this table directly, under the GUC it already sets.

*The key is `(org_id, user_id)`, not a hash.* `registration_budget` keys on an HMAC
because at that point in the flow there is no user yet and the address must not be
stored. Here the caller is authenticated and both ids are already rows in this database,
so hashing them would obscure the table without protecting anything.

The counting statement itself is taken unchanged in spirit from
`auth.spend_registration_budget`: one `INSERT … ON CONFLICT DO UPDATE … RETURNING`, so the
read, the window roll and the increment are a single atomic statement. Split into a
SELECT and an UPDATE, concurrent requests each read the old value and each conclude they
are under the limit — which is the failure mode a rate limiter exists to prevent.

Additive only. No existing table, column, policy, permission or grant is touched, and no
explicit GRANT is needed here: migration 0002 set `ALTER DEFAULT PRIVILEGES` for
`jutsu_app` on future tables in `public`, which is why 0010 added `jobs` without one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_budget",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        # When the current window opened. Rolled forward by the counting statement
        # rather than by a scheduled job: a fixed window that needs a sweeper is a
        # limiter that stops working the day the sweeper does.
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spent", sa.Integer, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id", "user_id", name="pk_search_budget"),
    )

    # Same predicate as every other tenant table since 0001. NULLIF is load-bearing:
    # `current_setting(.., true)` returns NULL only until the GUC is first set, and an
    # empty string afterwards, where `''::uuid` raises instead of filtering.
    op.execute("ALTER TABLE search_budget ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE search_budget FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY search_budget_org_isolation ON search_budget "
        "USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid) "
        "WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)"
    )


def downgrade() -> None:
    # Reversible without qualification: the table holds only counters for windows that
    # have already elapsed or are about to. Dropping it loses no history anybody can
    # act on, and the next request opens a fresh window.
    op.execute("DROP POLICY IF EXISTS search_budget_org_isolation ON search_budget")
    op.drop_table("search_budget")
