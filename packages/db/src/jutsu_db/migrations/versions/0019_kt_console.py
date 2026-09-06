"""The KT console: what a recipient keeps between visits, and one limiter for every budget.

Migration 0013 made a knowledge-transfer package a scoped, expiring, revocable window, and
`jutsu_api.kt._open_for` re-decides that window on every request. Nothing was kept
between requests: a question asked in the workspace lived in React state and died with
the tab. This migration adds the four things a recipient legitimately owns across
visits, and deliberately nothing else (`docs/adr/0016-kt-console.md`):

* `kt_conversations` / `kt_messages` — the recipient's own record of what they asked
  and what they were told. **Citations are stored as references** (`chunk_id`,
  `document_id`, marker), never as copied evidence text, so replaying a conversation
  re-checks the caller's ACL through `GET /v1/evidence/{chunk_id}` rather than
  re-reading a document they may no longer see. The answer prose itself is what the
  recipient was shown at the time; it is theirs, scoped to them, and closed the moment
  the package is revoked or expires because every read runs `_open_for` first.
* `kt_bookmarks` — a saved claim, document, message or free-text question, with an
  optional private note. Private means private: nothing in this table is read by the
  admin lifecycle and nothing here is ever fed to a model.
* `kt_progress` — `seen | done | unclear` against an item key, which is what the learning
  path, "still unclear" and coverage read. A marker table, not a copy of the item.

**No KT session table.** The brief that motivated this work asked for a "KT session
distinct from the URL"; the existing design is stronger — per-request revalidation
through `_open_for` — so a session row would be a second authorization state that could
drift from the first. The ADR records the reasoning.

**Every new table binds to its package AND its tenant structurally.** `kt_packages`
gains `UNIQUE (id, org_id)` purely so these tables can hold composite foreign keys on
`(kt_package_id, org_id)`, exactly as `employee_profiles` does against `users` (0002).
A conversation row cannot name a package from another organisation even before RLS is
consulted — the same belt-and-policy shape as `chunks.org_id` (ADR 0002).

**One limiter, many buckets.** `search_budget` (0011) counted searches for one
`(org_id, user_id)`. The KT claim endpoint is a 40-bit code space with no throttle and
the handover summary is a paid model call with no budget, and the alternative to
extending 0011 was a second counter table with a second atomic statement — two limiters
is how one of them stops being maintained. So the table gains a `bucket` column in its
key and keeps its name for the three tests that already read it; the module docstring in
`rate_limit.py` says the name is historical.

**`extraction_runs` gains the index `_LATEST_RUN_JOIN` was always asking for.** Every
KT knowledge read correlates `extraction_runs.stats_json->>'document_id'` per claim row
with no index on that expression. Adding one changes no result and is additive.

Downgrade is exact except for budget rows in the new buckets, which cannot survive a key
that no longer names the bucket; they are counters for windows already elapsing, so
dropping them loses nothing anybody can act on (the same argument 0011 makes).

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

ORG_PREDICATE = "org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

#: In dependency order; dropped in reverse.
KT_TABLES = ("kt_conversations", "kt_messages", "kt_bookmarks", "kt_progress")


def _org_policy(table: str) -> None:
    """ENABLE + FORCE + one policy, matching every tenant table since 0001."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_org_isolation ON {table} "
        f"USING ({ORG_PREDICATE}) WITH CHECK ({ORG_PREDICATE})"
    )


def _timestamp(name: str, *, nullable: bool = False, default: bool = True) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.func.now() if default else None,
        nullable=nullable,
    )


def upgrade() -> None:
    # --------------------------------------------------------------- one limiter
    #
    # `bucket` joins the key. Existing rows are the `search` bucket by definition — the
    # default is what they were counting — so no backfill statement is needed.
    op.add_column(
        "search_budget",
        sa.Column("bucket", sa.String(32), nullable=False, server_default="search"),
    )
    op.drop_constraint("pk_search_budget", "search_budget", type_="primary")
    op.create_primary_key("pk_search_budget", "search_budget", ["org_id", "user_id", "bucket"])

    # ------------------------------------------------------- package identity
    op.create_unique_constraint("uq_kt_packages_id_org_id", "kt_packages", ["id", "org_id"])

    # ------------------------------------------------------- kt_conversations
    op.create_table(
        "kt_conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kt_package_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        #: The first question, trimmed — what the history list shows. Never generated.
        sa.Column("title", sa.String(200)),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("archived_at", nullable=True, default=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["kt_package_id", "org_id"],
            ["kt_packages.id", "kt_packages.org_id"],
            ondelete="CASCADE",
            name="fk_kt_conversations_package",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_kt_conversations_user",
        ),
    )
    op.create_index(
        "ix_kt_conversations_recent",
        "kt_conversations",
        ["org_id", "kt_package_id", "user_id", "updated_at", "id"],
    )
    _org_policy("kt_conversations")

    # ---------------------------------------------------------- kt_messages
    op.create_table(
        "kt_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        #: The recipient's question, or the gated answer they were shown. An answer with
        #: `insufficient_evidence` stores the refusal sentence the UI rendered, so the
        #: history reads as it happened.
        sa.Column("content", sa.Text, nullable=False),
        #: `[{marker, chunk_id, document_id, document_title, source_system}]` —
        #: references only. Titles are display data the caller already saw; the span
        #: is re-fetched under ACL when they click.
        sa.Column("citations_json", JSONB, nullable=False, server_default="[]"),
        sa.Column("insufficient_evidence", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["kt_conversations.id"], ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="role"),
    )
    op.create_index(
        "ix_kt_messages_conversation",
        "kt_messages",
        ["org_id", "conversation_id", "created_at", "id"],
    )
    _org_policy("kt_messages")

    # --------------------------------------------------------- kt_bookmarks
    op.create_table(
        "kt_bookmarks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kt_package_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        #: The claim, document or message id. NULL exactly for a free-text question.
        sa.Column("ref_id", UUID(as_uuid=True)),
        #: The recipient's private note, or the question itself.
        sa.Column("note", sa.Text),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["kt_package_id", "org_id"],
            ["kt_packages.id", "kt_packages.org_id"],
            ondelete="CASCADE",
            name="fk_kt_bookmarks_package",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_kt_bookmarks_user",
        ),
        sa.CheckConstraint("kind IN ('claim', 'document', 'message', 'question')", name="kind"),
        sa.CheckConstraint(
            "(kind = 'question' AND ref_id IS NULL AND note IS NOT NULL) "
            "OR (kind <> 'question' AND ref_id IS NOT NULL)",
            name="shape",
        ),
    )
    op.create_index(
        "ix_kt_bookmarks_owner",
        "kt_bookmarks",
        ["org_id", "kt_package_id", "user_id", "created_at", "id"],
    )
    # One bookmark per referent per person per package. Questions are free text, carry
    # no referent, and may legitimately repeat — hence the partial index.
    op.create_index(
        "uq_kt_bookmarks_ref",
        "kt_bookmarks",
        ["kt_package_id", "user_id", "kind", "ref_id"],
        unique=True,
        postgresql_where=sa.text("ref_id IS NOT NULL"),
    )
    _org_policy("kt_bookmarks")

    # ---------------------------------------------------------- kt_progress
    op.create_table(
        "kt_progress",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kt_package_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        #: `claim:{uuid}` | `document:{uuid}` | `step:{key}` — the learning path and
        #: the knowledge tabs agree on this spelling in `jutsu_api.kt_workspace`.
        sa.Column("item_key", sa.String(160), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        _timestamp("updated_at"),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["kt_package_id", "org_id"],
            ["kt_packages.id", "kt_packages.org_id"],
            ondelete="CASCADE",
            name="fk_kt_progress_package",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_kt_progress_user",
        ),
        sa.PrimaryKeyConstraint("kt_package_id", "user_id", "item_key", name="pk_kt_progress"),
        sa.CheckConstraint("state IN ('seen', 'done', 'unclear')", name="state"),
    )
    _org_policy("kt_progress")

    # ------------------------------------------- the join every KT read pays for
    #
    # `_LATEST_RUN_JOIN` (jutsu_api.kt) picks, per document, the latest finished run
    # whose stats name that document. Additive; the read model is unchanged.
    op.execute(
        "CREATE INDEX ix_extraction_runs_document_started ON extraction_runs "
        "((stats_json->>'document_id'), started_at DESC) WHERE finished_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_extraction_runs_document_started")

    for table in reversed(KT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.drop_table(table)

    op.drop_constraint("uq_kt_packages_id_org_id", "kt_packages", type_="unique")

    # Counters in the new buckets cannot share a key that no longer names the bucket.
    # They describe windows that are already elapsing; see the module docstring.
    op.execute("DELETE FROM search_budget WHERE bucket <> 'search'")
    op.drop_constraint("pk_search_budget", "search_budget", type_="primary")
    op.drop_column("search_budget", "bucket")
    op.create_primary_key("pk_search_budget", "search_budget", ["org_id", "user_id"])
