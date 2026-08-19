"""Authentication operations, as SECURITY DEFINER functions.

Migration 0002 established the boundary: `jutsu_app` holds no table privilege in the
`auth` schema and reaches it only through narrow functions owned by `jutsu_auth`. That
boundary is what forces every auth operation to be expressed here rather than as SQL in
the request path — which turns out to be a benefit, because two of them have to be atomic
and the function is the only place to guarantee it.

**The OTP attempt budget is a compare-and-set, not a CHECK constraint.**

`CHECK (attempts <= max_attempts)` is not a concurrency control. Under READ COMMITTED, N
concurrent verifications of the same challenge all read `attempts = 0`, each tests a
different guess, and the counter lands at 1. An attacker firing a thousand requests per
round spends one attempt per round instead of a thousand, which collapses a six-digit
space from ~10^6 guesses to ~10^3 rounds. `auth.consume_attempt` increments and filters in
a single statement, so the database serialises it: zero rows returned *is* the rejection.

The same shape protects single-use. `auth.consume_challenge` is a conditional UPDATE, so
two simultaneous redemptions of one magic link cannot both mint a session — the second
sees no row.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

#: Signatures, used for ownership, grants and the downgrade. Kept in one place so the
#: three cannot fall out of step — a function created but never granted is a runtime
#: "permission denied" that no test of the migration itself would catch.
FUNCTIONS = (
    "auth.upsert_identity(bytea)",
    "auth.create_challenge(uuid, bytea, text, bytea, bytea, timestamptz, integer)",
    "auth.consume_attempt(bytea)",
    "auth.consume_challenge(uuid)",
    "auth.create_session(uuid, uuid, uuid, bytea, bytea, timestamptz, timestamptz)",
    "auth.revoke_session(bytea)",
    "auth.revoke_sessions_for_user(uuid)",
    "auth.touch_session(uuid, timestamptz)",
)


def upgrade() -> None:
    # An identity is the cross-organisation person. Upsert rather than select-then-insert:
    # two simultaneous first-time sign-ins for the same address would otherwise race and
    # one would fail on the unique index.
    op.execute(
        """
        CREATE FUNCTION auth.upsert_identity(p_email_hmac bytea)
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          WITH inserted AS (
            INSERT INTO auth.identities (id, email_hmac)
            VALUES (gen_random_uuid(), p_email_hmac)
            ON CONFLICT (email_hmac) DO NOTHING
            RETURNING id
          )
          SELECT id FROM inserted
          UNION ALL
          SELECT id FROM auth.identities WHERE email_hmac = p_email_hmac
          LIMIT 1;
        $fn$;
        """
    )

    # `p_identity_id` is nullable on purpose. A challenge is recorded for an UNREGISTERED
    # address too, so the work done — and therefore the response time — is identical
    # either way. Skipping it for unknown addresses would turn this endpoint into an
    # account-existence oracle regardless of what the response body says.
    op.execute(
        """
        CREATE FUNCTION auth.create_challenge(
            p_identity_id uuid,
            p_email_hmac bytea,
            p_purpose text,
            p_token_hash bytea,
            p_code_hash bytea,
            p_expires_at timestamptz,
            p_max_attempts integer
        )
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.login_challenges (
            id, identity_id, email_hmac, purpose, token_hash, code_hash,
            expires_at, max_attempts
          )
          VALUES (
            gen_random_uuid(), p_identity_id, p_email_hmac, p_purpose, p_token_hash,
            p_code_hash, p_expires_at, p_max_attempts
          )
          RETURNING id;
        $fn$;
        """
    )

    # Spend one attempt, atomically. Returning no row covers every rejection — unknown
    # token, already consumed, expired, budget exhausted — deliberately without saying
    # which, so the caller cannot distinguish "wrong code" from "no such challenge".
    op.execute(
        """
        CREATE FUNCTION auth.consume_attempt(p_token_hash bytea)
        RETURNS TABLE (challenge_id uuid, identity_id uuid, code_hash bytea, purpose text)
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          UPDATE auth.login_challenges
             SET attempts = attempts + 1
           WHERE token_hash = p_token_hash
             AND consumed_at IS NULL
             AND expires_at > now()
             AND attempts < max_attempts
          RETURNING id, identity_id, code_hash, purpose;
        $fn$;
        """
    )

    # Single-use, enforced by the WHERE clause rather than by reading first.
    op.execute(
        """
        CREATE FUNCTION auth.consume_challenge(p_challenge_id uuid)
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          UPDATE auth.login_challenges
             SET consumed_at = now()
           WHERE id = p_challenge_id
             AND consumed_at IS NULL
          RETURNING id;
        $fn$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION auth.create_session(
            p_identity_id uuid,
            p_user_id uuid,
            p_org_id uuid,
            p_token_hash bytea,
            p_csrf_hash bytea,
            p_expires_at timestamptz,
            p_idle_expires_at timestamptz
        )
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          INSERT INTO auth.sessions (
            id, identity_id, user_id, org_id, token_hash, csrf_hash,
            expires_at, idle_expires_at
          )
          VALUES (
            gen_random_uuid(), p_identity_id, p_user_id, p_org_id, p_token_hash,
            p_csrf_hash, p_expires_at, p_idle_expires_at
          )
          RETURNING id;
        $fn$;
        """
    )

    # Revocation is a timestamp, not a delete: an expired-then-reused token must be
    # distinguishable from one that never existed when reading the trail afterwards.
    op.execute(
        """
        CREATE FUNCTION auth.revoke_session(p_token_hash bytea)
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          UPDATE auth.sessions
             SET revoked_at = now()
           WHERE token_hash = p_token_hash
             AND revoked_at IS NULL
          RETURNING id;
        $fn$;
        """
    )

    # Deactivating a person must end their sessions. Without this, revoking access leaves
    # every open tab working until its own expiry — which is the gap between "we removed
    # them" and "they stopped being able to read things".
    op.execute(
        """
        CREATE FUNCTION auth.revoke_sessions_for_user(p_user_id uuid)
        RETURNS integer
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
        DECLARE
          affected integer;
        BEGIN
          UPDATE auth.sessions
             SET revoked_at = now()
           WHERE user_id = p_user_id
             AND revoked_at IS NULL;
          GET DIAGNOSTICS affected = ROW_COUNT;
          RETURN affected;
        END;
        $fn$;
        """
    )

    # Idle timeout is extended only when it has actually moved, so a burst of requests
    # does not write to the sessions row on every single one. The threshold lives in the
    # caller; this is the narrow write it is allowed to make.
    op.execute(
        """
        CREATE FUNCTION auth.touch_session(p_session_id uuid, p_idle_expires_at timestamptz)
        RETURNS uuid
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, auth AS $fn$
          UPDATE auth.sessions
             SET idle_expires_at = p_idle_expires_at
           WHERE id = p_session_id
             AND revoked_at IS NULL
             AND expires_at > now()
          RETURNING id;
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
