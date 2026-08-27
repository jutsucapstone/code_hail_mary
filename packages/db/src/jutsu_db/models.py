"""SQLAlchemy models for the Postgres schema (spec §8).

Two deliberate departures from §8's column lists, both recorded as ADRs:

  * `chunks` and `document_acl` carry `org_id` (ADR 0002), so their RLS policies can be
    a plain column comparison rather than a correlated subquery on the retrieval hot
    path. A composite foreign key stops the denormalised value drifting from its parent.
  * The whole schema lives in `packages/db` rather than one of §6's seven packages
    (ADR 0001).

Enums are imported from `jutsu_core` rather than redeclared. A second copy would let the
Pydantic and SQLAlchemy layers drift, and the first symptom would be a document that
round-trips through the database with a source system the API cannot parse.

Raw source blobs live in GCS (§8) — this schema stores text and metadata only.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from jutsu_core import PiiType, SourceSystem
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: JSONB payloads are arbitrary JSON objects; the shape is enforced by the Pydantic
#: model that writes them, not by the column.
JsonDict = dict[str, object]

# Embedding width is fixed at the schema level; changing it is a new migration, not a
# config flag, because every stored vector would have to be recomputed.
EMBEDDING_DIM = 768

# Explicit naming convention so Alembic emits stable, predictable constraint names.
# Without it autogenerate invents names and every downgrade is a guess.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- tenancy


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _now()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Stable id from the customer's IdP. ACL grants are matched on this, not on `id`.
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("org_id", "external_id", name="uq_users_org_id_external_id"),
        Index("ix_users_org_id_email", "org_id", "email"),
    )


class UserGroup(Base):
    """IdP-synced group membership. Primary key is both columns (§8).

    `org_id` was added by migration 0008. It shipped without one, which meant the table
    feeding the group half of §12's retrieval filter — an authorisation input — had no
    tenant column and therefore no row-level security. The composite foreign key holds the
    denormalised value true against the user's own organisation.
    """

    __tablename__ = "user_groups"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    group_external_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: Denormalised for RLS — see ADR 0002 for the same argument on chunks.
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_user_groups_user_id_org_id",
        ),
        Index("ix_user_groups_org_id_user_id", "org_id", "user_id"),
        Index("ix_user_groups_org_id_group_external_id", "org_id", "group_external_id"),
    )


class SourceIdentity(Base):
    """Who a JUTSU user is **inside one source system** (ADR 0010).

    The ACL principal. `document_acl.principal_id` holds `{source_system}:{subject}`, and
    this table is what maps a signed-in user to the subjects they own.

    It exists because `users.external_id` could not do the job. That column is singular,
    and one person is simultaneously a Google `sub`, an Entra `oid`, a Slack `U0…`, a
    GitHub numeric id and an Atlassian `accountId`. The mismatch was never "email versus
    subject"; it was cardinality.

    **`subject` is provider-native and immutable.** Never an email address — except for
    the local mail corpus, where the address genuinely is the subject the source issues,
    and that is stated rather than assumed (ADR 0008).

    **Revocation is a flag, not a delete.** Authorisation reads `is_active`, so
    deactivating a row takes effect on the next request with nothing to invalidate — which
    is what §17 test 3 requires of a group removal, and the same reasoning applies here.
    """

    __tablename__ = "source_identities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: Denormalised for RLS, held true by the composite FK below.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_system: Mapped[SourceSystem] = mapped_column(
        Enum(SourceSystem, name="source_system", native_enum=True), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    linked_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: How the link was established — an OAuth grant, a directory sync, an admin action.
    #: §17 wants every ACL change auditable, and "who claimed this subject" is the first
    #: question an incident asks.
    linked_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    updated_at: Mapped[datetime] = _now()

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "org_id"],
            ["users.id", "users.org_id"],
            ondelete="CASCADE",
            name="fk_source_identities_user_id_org_id",
        ),
        # Scoped to the organisation, not global: two tenants may legitimately connect the
        # same Slack workspace, and a global constraint would make the second one fail.
        UniqueConstraint(
            "org_id", "source_system", "subject", name="uq_source_identities_org_system_subject"
        ),
        Index("ix_source_identities_org_id_user_id", "org_id", "user_id"),
        Index(
            "ix_source_identities_org_id_source_system_subject",
            "org_id",
            "source_system",
            "subject",
        ),
    )


# --------------------------------------------------------------------------- ingestion


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    system: Mapped[SourceSystem] = mapped_column(
        Enum(SourceSystem, name="source_system", native_enum=True), nullable=False
    )
    config_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Opaque per-connector cursor; `Connector.list_since` resumes from it.
    last_sync_cursor: Mapped[str | None] = mapped_column(Text)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    uri: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False, default="text/plain")
    author_external_id: Mapped[str | None] = mapped_column(String(255))
    #: Half the idempotency key of §4.14 — hash of the ORIGINAL body.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Detects permission drift between syncs without diffing every grant.
    acl_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Citations index into this. Chunk offsets are measured against it, never `body_masked`.
    body_original: Mapped[str] = mapped_column(Text, nullable=False)
    #: What the LLM sees. Never surfaced to a caller without an ACL check.
    body_masked: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = _now()
    #: Re-ingestion supersedes rather than overwriting (§4.4).
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )

    __table_args__ = (
        # §4.14 — idempotency. A second `make seed` must add zero rows.
        UniqueConstraint(
            "org_id", "source_id", "external_id", name="uq_documents_org_source_external"
        ),
        # Target of the composite FK from chunks and document_acl (ADR 0002).
        UniqueConstraint("id", "org_id", name="uq_documents_id_org_id"),
        Index("ix_documents_content_hash", "content_hash"),
        Index("ix_documents_org_id", "org_id"),
    )


class Chunk(Base):
    """An embedding unit.

    `text` is masked; `char_start`/`char_end` index the ORIGINAL document body. Mixing
    those two coordinate systems mis-highlights every citation, which is why the
    offsets are named for the document rather than for this row.
    """

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: Denormalised for RLS — see ADR 0002. Held true by the composite FK below.
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "org_id"],
            ["documents.id", "documents.org_id"],
            ondelete="CASCADE",
            name="fk_chunks_document_id_org_id",
        ),
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_id_ordinal"),
        CheckConstraint("char_end >= char_start", name="char_range"),
        Index("ix_chunks_document_id_ordinal", "document_id", "ordinal"),
    )


class DocumentAcl(Base):
    """Read grants copied verbatim from the source system.

    This table is the whole of §4.5: the retrieval query joins it inside the SQL, so a
    caller without a grant never has the row fetched, let alone filtered out afterwards.
    """

    __tablename__ = "document_acl"

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    #: Denormalised for RLS — see ADR 0002.
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False, default="read")

    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "org_id"],
            ["documents.id", "documents.org_id"],
            ondelete="CASCADE",
            name="fk_document_acl_document_id_org_id",
        ),
        CheckConstraint(
            "principal_type IN ('user', 'group', 'org', 'public')", name="principal_type"
        ),
        # JUTSU never requests a write scope (§4.8), so the database refuses to store one.
        CheckConstraint("permission = 'read'", name="permission_read_only"),
        Index("ix_document_acl_principal_id_document_id", "principal_id", "document_id"),
    )


class PiiVaultEntry(Base):
    """Original PII, encrypted with an org-scoped key.

    Rehydrated only on display, only for a caller who passed the ACL check (§9.1).
    """

    __tablename__ = "pii_vault"

    vault_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    pii_type: Mapped[PiiType] = mapped_column(
        Enum(PiiType, name="pii_type", native_enum=True), nullable=False
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


# --------------------------------------------------------------------------- extraction


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    #: A prompt change is a graph-affecting change (§6) — recorded on every claim.
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Token accounting lands here — the M1 gate requires the seed-run cost (§21).
    stats_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)


class ExtractionClaim(Base):
    __tablename__ = "extraction_claims"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    #: Re-running supersedes; it never overwrites (§4.4).
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extraction_claims.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        Index("ix_extraction_claims_run_id", "run_id"),
        Index("ix_extraction_claims_chunk_id", "chunk_id"),
    )


class ResolutionQueueItem(Base):
    """Entity-resolution candidates scoring below the auto-merge threshold (§11).

    Between 0.65 and 0.92 a human decides. Merges must stay reversible, so nothing here
    destructively merges anything.
    """

    __tablename__ = "resolution_queue"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    candidates_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    __table_args__ = (Index("ix_resolution_queue_org_id_state", "org_id", "state"),)


# --------------------------------------------------------------------------- ops


class Job(Base):
    """Queue row backing the staged ingestion pipeline (§9).

    Each stage is separately queued with its own idempotency key, so a failure at
    `extract` never forces a re-`fetch`.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    #: Globally unique — this is what makes re-running the pipeline a no-op (§4.14).
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    payload_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_jobs_state_locked_until", "state", "locked_until"),)


class AuditLogEntry(Base):
    """Immutable trail. Person-level risk reads, handover generation and ACL changes all
    land here (§17), and it is exportable."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: Opaque user id. Never an email or display name — §4.9 forbids PII here.
    actor_id: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    ts: Mapped[datetime] = _now()
    meta_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (Index("ix_audit_log_org_id_ts", "org_id", "ts"),)


# --------------------------------------------------------------------------- evals


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    suite: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Named so a regression can be attributed to a commit (§18).
    git_sha: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)


class EvalResult(Base):
    __tablename__ = "eval_results"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail_json: Mapped[JsonDict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (Index("ix_eval_results_run_id_metric", "run_id", "metric"),)


#: Tables carrying RLS. Kept as data so the migration and its tests cannot disagree
#: about which tables are protected.
#:
#: `source_identities` and `user_groups` joined the list in migration 0008. Both are
#: authorisation inputs — they decide which ACL principals a caller holds — so they belong
#: under the same policy as the data they gate. Adding them here also extends `test_rls.py`
#: over them for free, including the §17.6 count-leak case.
RLS_TABLES: tuple[str, ...] = (
    "documents",
    "chunks",
    "document_acl",
    "source_identities",
    "user_groups",
)
