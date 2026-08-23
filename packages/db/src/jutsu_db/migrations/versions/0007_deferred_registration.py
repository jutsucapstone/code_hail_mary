"""Registration stops creating an organisation before anyone has proved anything.

Until now `POST /v1/orgs/register` wrote the tenant, its owner, that person's role and
their JUTSU ID in one transaction, and *then* emailed a code. Nothing compared the work
address to the claimed domain. So an unauthenticated caller could post
`{work_email: eve@evil.example, company_domain: microsoft.com}`, receive the code in
their own inbox, redeem it, and hold an Owner session over an organisation holding a
domain they have no connection to — permanently, because `uq_orgs_domain_active` then
refuses the real company. The identical-202 response closed the HTTP oracle and left the
squat wide open.

This migration provides the state that lets creation wait for the code.

**Four things here are load-bearing and are easy to "simplify" into a hole:**

1. **The staging row is keyed on the token hash, not on the challenge or the address.**
   The plaintext token exists only in the recipient's inbox, exactly as
   `auth.invitation_tokens` (0006) puts it. That is what turns "the work address matches
   the claimed domain" from a fact about our own code path into a fact somebody proved:
   the payload cannot be unlocked without the mail. Keying on `email_hmac` instead would
   let a stranger's later staging POST overwrite a victim's pending row, including the
   terms acceptance recorded against it.

2. **There is no uniqueness, index or reservation on the claimed domain.** Two people at
   one company staging at once is legal and expected. A `UNIQUE (domain)` here would be
   an enumeration oracle reachable with no mailbox at all — post a domain, read the
   conflict — and simultaneously a denial of registration, since a stranger could hold
   the row and lock the real company out for its lifetime. That is strictly worse than
   the squat this migration exists to close. `uq_orgs_domain_active` at commit time is
   the single authority on who gets a domain.

3. **Consumption is a compare-and-set, matching `auth.consume_challenge`.** Ten minutes
   is long enough to want a resend, and a resend means two live challenges against one
   staged payload. Without single-use, both redemptions create an organisation and the
   second burns a JUTSU ID on a tenant nobody asked for. Zero rows returned *is* the
   rejection, and the database serialises it.

4. **`login_challenges.purpose` gains a CHECK.** `auth.consume_attempt` has returned
   `purpose` since 0003 and nothing has ever read it. Once one challenge namespace has
   two consumers, an unconstrained free-text column is a purpose-confusion bug waiting
   for a typo: `'regsiter'` would be accepted on write and match nothing on read, which
   fails open into "code is not valid" for every legitimate registrant.

**The staged payload is plaintext at rest, and that is a deliberate, bounded exception.**
`auth.identities` stores `email_hmac` and never an address, because it is a permanent,
org-less index of every customer — a leak there is the customer list. A staging row is a
different risk: it lives ten minutes, is deleted on consumption, is reaped if abandoned,
is owned by `jutsu_auth` with no grant to `jutsu_app`, and is reachable only through the
two definer functions below. Encrypting it needs a key-management dependency the stack
does not have (CLAUDE.md fixes the stack), so the boundary here is privilege and
lifetime rather than cryptography. Recorded so the next person weighs it deliberately
rather than discovering it.

**Existing domains are grandfathered, and the schema now says so.** Every row in `orgs`
today was claimed without proof, so `domain_verified_at` backfills to NULL rather than
to `now()`. Backfilling it as verified would launder every existing squat into a
verified claim, including any planted before this migration ran.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UUID = pg.UUID(as_uuid=True)
JSONB = pg.JSONB

#: Signatures, used for ownership, grants and the downgrade. Kept in one place so the
#: three cannot fall out of step — a function created but never granted is a runtime
#: "permission denied" that no test of the migration itself would catch.
FUNCTIONS = (
    "auth.stage_registration(bytea, uuid, bytea, text, jsonb, timestamptz)",
    "auth.consume_pending_registration(bytea)",
    "auth.spend_registration_budget(bytea, interval, integer)",
    "auth.record_registration_event(bytea, text, text)",
    "auth.reap_expired_registrations()",
)

#: The industries offered on the form. A CHECK rather than a lookup table: the set is
#: small, closed and only ever read as a label, so a table would add a join and a
#: migration for every future value without buying referential meaning.
INDUSTRIES = (
    "consulting",
    "technology",
    "finance",
    "healthcare",
    "manufacturing",
    "government",
    "other",
)


def upgrade() -> None:
    # ------------------------------------------------------------------ orgs
    #
    # Optional, and genuinely optional: nothing in the product reads them yet. They are
    # here because onboarding is the only moment someone will ever be willing to answer,
    # and because timezone, regional defaults and data residency all need them later.
    # Constrained at the database rather than only in Pydantic, so a future writer that
    # is not the registration route cannot put free text in them.
    op.add_column("orgs", sa.Column("country", sa.String(2)))
    op.add_column("orgs", sa.Column("industry", sa.String(32)))
    op.create_check_constraint(
        "country_alpha2", "orgs", "country IS NULL OR country ~ '^[A-Z]{2}$'"
    )
    op.create_check_constraint(
        "industry",
        "orgs",
        "industry IS NULL OR industry IN (" + ", ".join(f"'{value}'" for value in INDUSTRIES) + ")",
    )

    # NULL means "claimed, never proved" — the honest description of every row that
    # exists when this migration runs.
    op.add_column("orgs", sa.Column("domain_verified_at", sa.DateTime(timezone=True)))

    # ------------------------------------------------------------------ terms
    #
    # One row per acceptance event, not one per organisation. A version bump requires a
    # fresh acceptance, and the history of who accepted what has to survive it — an
    # UPDATE-in-place record cannot answer "what did they agree to in March".
    #
    # `accepted_at` is when the box was ticked; `recorded_at` is when the row was
    # written. They are minutes apart because acceptance happens before the email is
    # verified, and conflating them would make the legal record assert a moment the
    # person demonstrably was not at a keyboard.
    op.create_table(
        "terms_acceptances",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("org_id", UUID, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document", sa.String(32), nullable=False),
        # No server_default. A version that defaults is a version nobody chose, and the
        # whole point of the column is that it names a specific published document.
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "document IN ('terms', 'privacy')", name="ck_terms_acceptances_document"
        ),
    )
    op.create_index(
        "ix_terms_acceptances_org_id_user_id", "terms_acceptances", ["org_id", "user_id"]
    )

    op.execute("ALTER TABLE terms_acceptances ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE terms_acceptances FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY terms_acceptances_org_isolation ON terms_acceptances "
        "USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid) "
        "WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)"
    )
    # Evidence, like audit_log. Insertable once, never rewritten.
    op.execute(
        """
        DO $do$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_app') THEN
            REVOKE UPDATE, DELETE ON terms_acceptances FROM jutsu_app;
          END IF;
        END $do$;
        """
    )

    # ------------------------------------------------------------------ purpose
    op.create_check_constraint(
        "purpose", "login_challenges", "purpose IN ('sign_in', 'register')", schema="auth"
    )

    # ------------------------------------------------------------------ staging
    op.create_table(
        "pending_registrations",
        # The hash is the key: the plaintext token exists only in the registrant's inbox,
        # so the payload cannot be reached without the mail that carried it.
        sa.Column("token_hash", sa.LargeBinary(32), primary_key=True),
        # Binds the payload to one specific challenge. Redeeming any other challenge —
        # a sign-in, a second registration — resolves to a different hash and finds
        # nothing, which is what makes purpose confusion structurally impossible rather
        # than merely checked.
        sa.Column("challenge_id", UUID, nullable=False, unique=True),
        # Asserted equal to the identity the redeemed challenge resolved to. Belt and
        # braces against a future path that separates the two.
        sa.Column("email_hmac", sa.LargeBinary(32), nullable=False),
        # Canonical A-label form, already lowercased by the application. Stored so the
        # domain check can be re-run against the consumed row rather than against
        # anything the second request supplied.
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        # Deliberately NO foreign key to auth.login_challenges: consuming a challenge is
        # an UPDATE, not a delete, but the reaper removes expired rows from both tables
        # independently and a cascade would couple their lifetimes.
        schema="auth",
    )
    # For the reaper only. Never a lookup path — the primary key is the only way in.
    op.create_index(
        "ix_pending_registrations_expires_at",
        "pending_registrations",
        ["expires_at"],
        schema="auth",
    )
    op.execute("ALTER TABLE auth.pending_registrations OWNER TO jutsu_auth")

    # ------------------------------------------------------------------ throttle
    #
    # There is no rate limiting anywhere in this service, and staging sends mail to an
    # address the caller names. Without a budget it is an open relay: ten thousand POSTs
    # are ten thousand messages, ten thousand identity rows and ten thousand staged
    # payloads.
    #
    # Keyed on the HMAC, never the address, so the table cannot become the customer list
    # by another name. Same compare-and-set shape as `auth.consume_attempt`: the count
    # and the comparison are one statement, so concurrent requests cannot each read the
    # old value and all decide they are under the limit.
    op.create_table(
        "registration_budget",
        sa.Column("subject", sa.LargeBinary(32), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spent", sa.Integer, nullable=False, server_default="0"),
        schema="auth",
    )
    op.execute("ALTER TABLE auth.registration_budget OWNER TO jutsu_auth")

    # ------------------------------------------------------------------ events
    #
    # `audit_log.org_id` is NOT NULL and the table is under FORCE RLS, so nothing can be
    # written there before an organisation exists — which is precisely the window this
    # migration opens. Widening that column to nullable would put unscoped rows in a
    # tenant table and force every existing query to learn about them.
    #
    # So a separate org-less sink, holding the HMAC and the claimed domain and nothing
    # else. A staged registration that is never completed leaves a trail; a domain
    # rejected for mismatch leaves a trail; neither leaves a name or an address.
    op.create_table(
        "registration_events",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email_hmac", sa.LargeBinary(32), nullable=False),
        sa.Column("domain", sa.String(255)),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="auth",
    )
    op.create_index(
        "ix_registration_events_occurred_at", "registration_events", ["occurred_at"], schema="auth"
    )
    op.execute("ALTER TABLE auth.registration_events OWNER TO jutsu_auth")

    # ------------------------------------------------------------------ functions
    op.execute(
        """
        CREATE FUNCTION auth.stage_registration(
            p_token_hash bytea,
            p_challenge_id uuid,
            p_email_hmac bytea,
            p_domain text,
            p_payload jsonb,
            p_expires_at timestamptz
        )
        RETURNS bytea
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.pending_registrations
                 (token_hash, challenge_id, email_hmac, domain, payload, expires_at)
          VALUES (p_token_hash, p_challenge_id, p_email_hmac, p_domain, p_payload, p_expires_at)
          RETURNING token_hash;
        $fn$;
        """
    )

    # Single-use, in one statement, so two redemptions of one staged registration cannot
    # both create an organisation. Zero rows returned is the rejection — expired, already
    # consumed and never-existed are one answer, because distinguishing them would tell a
    # caller which half to work on.
    op.execute(
        """
        CREATE FUNCTION auth.consume_pending_registration(p_token_hash bytea)
        RETURNS TABLE (challenge_id uuid, email_hmac bytea, domain text, payload jsonb)
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          UPDATE auth.pending_registrations p
             SET consumed_at = now()
           WHERE p.token_hash = p_token_hash
             AND p.consumed_at IS NULL
             AND p.expires_at > now()
          RETURNING p.challenge_id, p.email_hmac, p.domain, p.payload;
        $fn$;
        """
    )

    # Returns what remains after spending one. A caller that gets 0 or less has been
    # refused; the row is still updated, so a refused caller does not get a free retry.
    op.execute(
        """
        CREATE FUNCTION auth.spend_registration_budget(
            p_subject bytea, p_window interval, p_limit integer
        )
        RETURNS integer
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.registration_budget (subject, window_start, spent)
          VALUES (p_subject, now(), 1)
          ON CONFLICT (subject) DO UPDATE
             SET window_start = CASE
                   WHEN auth.registration_budget.window_start < now() - p_window
                   THEN now() ELSE auth.registration_budget.window_start END,
                 spent = CASE
                   WHEN auth.registration_budget.window_start < now() - p_window
                   THEN 1 ELSE auth.registration_budget.spent + 1 END
          RETURNING p_limit - spent;
        $fn$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION auth.record_registration_event(
            p_email_hmac bytea, p_domain text, p_outcome text
        )
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.registration_events (email_hmac, domain, outcome)
          VALUES (p_email_hmac, p_domain, p_outcome)
          RETURNING id;
        $fn$;
        """
    )

    # An expiry column with nothing deleting it is a comment, not a control. Called from
    # the worker; returns the count so a scheduled run can be observed rather than
    # assumed.
    op.execute(
        """
        CREATE FUNCTION auth.reap_expired_registrations()
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
        DECLARE
          removed integer;
        BEGIN
          DELETE FROM auth.pending_registrations WHERE expires_at < now();
          GET DIAGNOSTICS removed = ROW_COUNT;
          DELETE FROM auth.login_challenges WHERE expires_at < now();
          DELETE FROM auth.registration_budget WHERE window_start < now() - interval '1 day';
          RETURN removed;
        END;
        $fn$;
        """
    )

    for signature in FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} OWNER TO jutsu_auth")

    # S608: the interpolated values are the module-level FUNCTIONS tuple above — fixed
    # signatures written in this file, never request data. Bandit cannot distinguish a
    # constant from user input, and a parameterised form is not available for GRANT.
    op.execute(
        f"""
        DO $do$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_app') THEN
            {"".join(f"GRANT EXECUTE ON FUNCTION {sig} TO jutsu_app; " for sig in FUNCTIONS)}
          END IF;
        END $do$;
        """  # noqa: S608
    )


def downgrade() -> None:
    for signature in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    op.drop_table("registration_events", schema="auth")
    op.drop_table("registration_budget", schema="auth")
    op.drop_table("pending_registrations", schema="auth")

    op.drop_constraint("ck_login_challenges_purpose", "login_challenges", schema="auth")

    op.execute("DROP POLICY IF EXISTS terms_acceptances_org_isolation ON terms_acceptances")
    op.drop_table("terms_acceptances")

    op.drop_column("orgs", "domain_verified_at")
    op.drop_constraint("ck_orgs_industry", "orgs")
    op.drop_constraint("ck_orgs_country_alpha2", "orgs")
    op.drop_column("orgs", "industry")
    op.drop_column("orgs", "country")
