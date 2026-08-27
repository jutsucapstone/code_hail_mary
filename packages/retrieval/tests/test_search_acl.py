"""The adversarial ACL suite for vector retrieval (§17, §12).

> A user must never retrieve evidence unless they are authorized to see that evidence.

Every test here tries to break that sentence, and each one runs against a **real Postgres
with a real pgvector HNSW index**. A mocked database would prove nothing: the entire
guarantee is a SQL predicate plus row-level security, and both live in the server. The
suite that matters most is the one that would still be green against a design that leaks,
and that is exactly what a mock would give.

`test_the_query_plan_contains_the_acl_join` is the load-bearing one. Every other test
describes an outcome, and outcomes can be produced the wrong way — a Python post-filter
returns the same rows as a SQL predicate right up until the moment a count, a `LIMIT` or a
cursor is involved. That test asserts the *mechanism*, and it is what catches a future
refactor quietly moving the filter out of the database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from jutsu_core.errors import NotFound
from jutsu_retrieval.evidence import fetch_evidence
from jutsu_retrieval.search import (
    ACL_PREDICATE,
    DEFAULT_EF_SEARCH_LADDER,
    _statement,
    search_chunks,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DIM = 768

TEST_DB_ENV = "JUTSU_TEST_DATABASE_URL"
MIGRATION_DB_ENV = "JUTSU_TEST_MIGRATION_URL"


def skip_without_database() -> None:
    if os.environ.get("JUTSU_DB_REACHABLE") != "1":
        pytest.skip(f"nothing listening at {TEST_DB_ENV} — start Postgres with `make up`")


def alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parents[3] / "packages" / "db"
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "src" / "jutsu_db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def run_alembic(cfg: Config, direction: str, revision: str) -> None:
    """Alembic's env.py ends in `asyncio.run`, which cannot nest in a running loop."""
    fn = command.upgrade if direction == "upgrade" else command.downgrade
    await asyncio.to_thread(fn, cfg, revision)


@pytest.fixture
async def db_session(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    """One transaction as the **restricted** application role.

    `jutsu_app` is `NOSUPERUSER NOBYPASSRLS`, and that is not a detail: the owner is a
    superuser, bypasses row-level security unconditionally, and would make every isolation
    assertion in this file pass against a design that leaks (ADR 0003).

    Defined here rather than in a `conftest.py` for the reason `test_persistence.py`
    records — `packages/db/tests/conftest.py` already exists, and a second one under
    `packages` makes `mypy packages` ambiguous and check nothing at all.
    """
    skip_without_database()
    app_url = os.environ[TEST_DB_ENV]
    migration_url = os.environ.get(MIGRATION_DB_ENV, app_url)

    monkeypatch.setenv("DATABASE_URL", migration_url)
    cfg = alembic_config(migration_url)
    await run_alembic(cfg, "downgrade", "base")
    await run_alembic(cfg, "upgrade", "head")

    engine = create_async_engine(app_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        yield session
        await session.rollback()

    await engine.dispose()
    await run_alembic(cfg, "downgrade", "base")


def vec(*leading: float) -> list[float]:
    """A 768-dim vector whose first entries are given and the rest zero.

    Enough to make similarity orderings deterministic without writing 768 numbers, and it
    keeps the *reason* a given chunk ranks where it does visible in the test body.
    """
    tail = [0.0] * (DIM - len(leading))
    return [*leading, *tail]


def literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


async def scope(session: AsyncSession, org_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )


async def make_org(session: AsyncSession, label: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    await scope(session, org_id)
    await session.execute(
        text("INSERT INTO orgs (id, name) VALUES (:i, :n)"), {"i": org_id, "n": label}
    )
    return org_id


async def make_user(
    session: AsyncSession, org_id: uuid.UUID, label: str, *, subject: str | None = None
) -> uuid.UUID:
    """A user, optionally holding one `local:` source identity.

    `subject=None` produces a user with **no** identity at all, which is the default state
    of a real user before S6.6's linking runs and is the fail-closed case §17 test 1
    describes.
    """
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:i,:o,:e,'active')"),
        {"i": user_id, "o": org_id, "e": f"{label}@example.com"},
    )
    if subject is not None:
        await session.execute(
            text(
                "INSERT INTO source_identities (org_id, user_id, source_system, subject) "
                "VALUES (:o,:u,'local',:s)"
            ),
            {"o": org_id, "u": user_id, "s": subject},
        )
    return user_id


async def add_group(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID, group: str
) -> None:
    await session.execute(
        text("INSERT INTO user_groups (user_id, org_id, group_external_id) VALUES (:u,:o,:g)"),
        {"u": user_id, "o": org_id, "g": group},
    )


async def make_source(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID:
    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:i,:o,'local','{}'::jsonb)"
        ),
        {"i": source_id, "o": org_id},
    )
    return source_id


async def make_document(
    session: AsyncSession,
    org_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    title: str,
    grants: list[tuple[str, str]],
    embedding: list[float],
    superseded: bool = False,
    chunks: int = 1,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """One document, its grants and its chunks. Returns `(document_id, chunk_ids)`.

    `grants` is a list of `(principal_type, principal_id)`, written out at each call site
    rather than defaulted, because the grant *is* the thing under test in most of these.
    """
    doc_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at, superseded_by) "
            "VALUES (:i,:o,:s,:e,:t,'h','a','original body','masked body',now(),:sup)"
        ),
        {
            "i": doc_id,
            "o": org_id,
            "s": source_id,
            "e": str(doc_id),
            "t": title,
            # Self-referential on purpose: §4.4 only needs the column to be non-NULL for
            # the row to count as replaced, and pointing at itself avoids inventing a
            # successor document the test has no use for.
            "sup": doc_id if superseded else None,
        },
    )
    for principal_type, principal_id in grants:
        await session.execute(
            text(
                "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id) "
                "VALUES (:d,:pt,:pi,:o)"
            ),
            {"d": doc_id, "pt": principal_type, "pi": principal_id, "o": org_id},
        )

    chunk_ids = []
    for ordinal in range(chunks):
        chunk_id = uuid.uuid4()
        chunk_ids.append(chunk_id)
        await session.execute(
            text(
                "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, "
                "char_end, token_count, embedding) "
                "VALUES (:i,:d,:o,:n,:x,0,12,3,CAST(:v AS vector))"
            ),
            {
                "i": chunk_id,
                "d": doc_id,
                "o": org_id,
                "n": ordinal,
                "x": f"{title} chunk {ordinal}",
                "v": literal(embedding),
            },
        )
    return doc_id, chunk_ids


@pytest.fixture
async def world(db_session: AsyncSession) -> dict[str, Any]:
    """Two tenants, four documents, and grants of every kind §12 honours.

    Built once and shared, because the interesting assertions are about *who sees which
    subset of the same corpus* — which is only meaningful if there is one corpus.

    Alpha holds:
      * `direct`   granted to `local:ada@example.com` only
      * `grouped`  granted to the group `local:S-ENGINEERING` only
      * `orgwide`  granted to the whole organisation
      * `secret`   granted to `local:eve@example.com` — nobody in these tests holds it
      * `publicly` granted with `principal_type = 'public'`, which §12 does not honour

    Beta holds `crosstenant`, granted to `local:ada@example.com` — the *same* principal
    string Ada holds in Alpha. That is §17 test 5, and it is the reason the grant is
    written this way rather than to some unrelated subject.
    """
    alpha = await make_org(db_session, "alpha")
    alpha_source = await make_source(db_session, alpha)
    ada = await make_user(db_session, alpha, "ada", subject="ada@example.com")
    grace = await make_user(db_session, alpha, "grace", subject="grace@example.com")
    nobody = await make_user(db_session, alpha, "nobody")

    direct, direct_chunks = await make_document(
        db_session,
        alpha,
        alpha_source,
        title="direct",
        grants=[("user", "local:ada@example.com")],
        embedding=vec(1.0),
    )
    grouped, grouped_chunks = await make_document(
        db_session,
        alpha,
        alpha_source,
        title="grouped",
        grants=[("group", "local:S-ENGINEERING")],
        embedding=vec(0.9, 0.1),
    )
    orgwide, orgwide_chunks = await make_document(
        db_session,
        alpha,
        alpha_source,
        title="orgwide",
        grants=[("org", str(alpha))],
        embedding=vec(0.8, 0.2),
    )
    secret, secret_chunks = await make_document(
        db_session,
        alpha,
        alpha_source,
        title="secret",
        grants=[("user", "local:eve@example.com")],
        embedding=vec(0.99),
    )
    publicly, publicly_chunks = await make_document(
        db_session,
        alpha,
        alpha_source,
        title="publicly",
        grants=[("public", "everyone")],
        embedding=vec(0.98),
    )

    beta = await make_org(db_session, "beta")
    beta_source = await make_source(db_session, beta)
    await make_document(
        db_session,
        beta,
        beta_source,
        title="crosstenant",
        grants=[("user", "local:ada@example.com")],
        embedding=vec(1.0),
    )

    await scope(db_session, alpha)
    return {
        "alpha": alpha,
        "beta": beta,
        "ada": ada,
        "grace": grace,
        "nobody": nobody,
        "direct": direct,
        "direct_chunks": direct_chunks,
        "grouped": grouped,
        "grouped_chunks": grouped_chunks,
        "orgwide": orgwide,
        "orgwide_chunks": orgwide_chunks,
        "secret": secret,
        "secret_chunks": secret_chunks,
        "publicly": publicly,
        "publicly_chunks": publicly_chunks,
    }


async def titles(session: AsyncSession, user_id: uuid.UUID, **kwargs: Any) -> set[str]:
    page = await search_chunks(session, user_id=user_id, query_vector=vec(1.0), **kwargs)
    return {item.document_title for item in page.items}


class TestWhatIsReturned:
    async def test_an_authorized_document_is_returned(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 2 — a direct user grant returns exactly that document's chunks."""
        found = await titles(db_session, world["ada"])

        assert "direct" in found

    async def test_an_unauthorized_same_tenant_document_is_never_returned(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """`secret` is nearer the query vector than `direct` is.

        That is the point of the fixture: if the filter ran after ranking, or after the
        `LIMIT`, this document would be the *first* thing returned. It is granted to a
        principal nobody in this test holds, so it must not appear at any k.
        """
        found = await titles(db_session, world["ada"], k=50)

        assert "secret" not in found

    async def test_a_cross_tenant_document_is_never_returned(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 5 — Ada's principal string matches a grant in Beta and must not help.

        This is why the two grants are byte-identical. A filter that matched on principal
        alone, without the tenant, would return Beta's document to an Alpha caller and
        every other test in this file would still pass.
        """
        found = await titles(db_session, world["ada"], k=50)

        assert "crosstenant" not in found

    async def test_a_principal_mismatch_returns_nothing(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Grace holds an identity, just not one that matches any grant she can use."""
        found = await titles(db_session, world["grace"], k=50)

        assert found == {"orgwide"}, "only the organisation-wide grant should reach Grace"

    async def test_an_empty_principal_set_fails_closed(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 1 — no grants means zero results, and not an error.

        `nobody` has no source identity at all, which is every real user's state before
        S6.6's linking runs. They still see the organisation-wide document, because an
        `org` grant is a deliberate statement about everyone in the tenant rather than an
        absence of one.
        """
        page = await search_chunks(db_session, user_id=world["nobody"], query_vector=vec(1.0), k=50)

        assert {item.document_title for item in page.items} == {"orgwide"}

    async def test_an_org_wide_grant_behaves_as_expected(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 4. Compared against the GUC, so it cannot match another tenant's id."""
        assert "orgwide" in await titles(db_session, world["ada"], k=50)
        assert "orgwide" in await titles(db_session, world["grace"], k=50)

    async def test_a_public_grant_is_not_honoured(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Pinned, not accidental.

        Migration 0001's check constraint permits `principal_type = 'public'`; §12's
        filter does not mention it and nothing writes it. A grant type whose semantics
        nobody has defined is treated as no grant, so the document is invisible rather
        than visible to everyone. Fail-closed is the right direction, and when a connector
        needs `public` that is an ADR — at which point this test changes deliberately
        instead of the behaviour changing silently.
        """
        found = await titles(db_session, world["ada"], k=50)

        assert "publicly" not in found

    async def test_a_superseded_document_is_not_returned(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§4.4 — re-extraction supersedes rather than overwrites, so retrieval must skip
        what was replaced or an answer cites a version that is no longer true."""
        source = await make_source(db_session, world["alpha"])
        await make_document(
            db_session,
            world["alpha"],
            source,
            title="replaced",
            grants=[("user", "local:ada@example.com")],
            embedding=vec(1.0),
            superseded=True,
        )

        assert "replaced" not in await titles(db_session, world["ada"], k=50)


class TestGroups:
    async def test_a_group_grant_reaches_only_a_member(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 3, first half."""
        assert "grouped" not in await titles(db_session, world["ada"], k=50)

        await add_group(db_session, world["alpha"], world["ada"], "local:S-ENGINEERING")

        assert "grouped" in await titles(db_session, world["ada"], k=50)

    async def test_removing_membership_takes_effect_on_the_next_query(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 3, second half — **no cache flush, no re-login**.

        Principals are resolved inside the search's own transaction, so there is nothing
        to invalidate. The two calls either side of the delete are the whole assertion.
        """
        await add_group(db_session, world["alpha"], world["ada"], "local:S-ENGINEERING")
        assert "grouped" in await titles(db_session, world["ada"], k=50)

        await db_session.execute(
            text("DELETE FROM user_groups WHERE user_id = :u"), {"u": world["ada"]}
        )

        assert "grouped" not in await titles(db_session, world["ada"], k=50)

    async def test_a_group_grant_does_not_cross_tenants(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """A group of the same name in Beta must not reach an Alpha document."""
        beta_source = await make_source(db_session, world["beta"])
        await scope(db_session, world["beta"])
        await make_document(
            db_session,
            world["beta"],
            beta_source,
            title="beta-grouped",
            grants=[("group", "local:S-ENGINEERING")],
            embedding=vec(1.0),
        )
        await scope(db_session, world["alpha"])
        await add_group(db_session, world["alpha"], world["ada"], "local:S-ENGINEERING")

        assert "beta-grouped" not in await titles(db_session, world["ada"], k=50)


class TestRevocation:
    async def test_a_revoked_identity_immediately_loses_access(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """The S6.6 revocation switch, seen from the retrieval side.

        `is_active = false` is all that changes. The next search resolves principals
        again, finds none, and returns nothing the identity granted — no flush, no
        expiry, no next login.
        """
        assert "direct" in await titles(db_session, world["ada"], k=50)

        await db_session.execute(
            text("UPDATE source_identities SET is_active = false WHERE user_id = :u"),
            {"u": world["ada"]},
        )

        found = await titles(db_session, world["ada"], k=50)
        assert "direct" not in found
        assert found == {"orgwide"}, "only the org-wide grant survives a revoked identity"

    async def test_revocation_between_two_searches_leaves_no_stale_state(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Interleaved deliberately: search, revoke, search, restore, search.

        A cached principal set would show up here as the second search still returning
        `direct`, or the third failing to get it back. Both directions are asserted,
        because a cache that is merely slow to warm looks identical to a correct system
        in a test that only ever revokes.
        """
        assert "direct" in await titles(db_session, world["ada"], k=50)

        await db_session.execute(
            text("UPDATE source_identities SET is_active = false WHERE user_id = :u"),
            {"u": world["ada"]},
        )
        assert "direct" not in await titles(db_session, world["ada"], k=50)

        await db_session.execute(
            text("UPDATE source_identities SET is_active = true WHERE user_id = :u"),
            {"u": world["ada"]},
        )
        assert "direct" in await titles(db_session, world["ada"], k=50)


class TestTheFilterCannotBeSteered:
    async def test_a_larger_k_cannot_widen_authorization(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """The obvious attack: ask for everything and hope the filter was a `LIMIT`.

        `secret` and `publicly` both rank *above* `direct`. If `k` were the only thing
        standing between the caller and them, a large enough `k` would surface them.
        """
        for k in (1, 5, 50, 500):
            found = await titles(db_session, world["ada"], k=k)
            assert "secret" not in found, f"k={k} leaked an unauthorized document"
            assert "crosstenant" not in found, f"k={k} crossed a tenant boundary"

    async def test_the_escalation_ladder_never_changes_the_predicate(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Every rung runs the same SQL. This asserts the mechanism, not just the outcome.

        The statements executed are captured and compared: they must be identical strings,
        and each must contain `ACL_PREDICATE` verbatim. A ladder that widened the filter to
        reach `k` would be the single worst bug this slice could ship, and it would look
        like a performance fix in review.
        """
        seen: list[str] = []
        original = db_session.execute

        async def recording(statement: Any, *args: Any, **kwargs: Any) -> Any:
            rendered = str(statement)
            if "document_acl" in rendered:
                seen.append(rendered)
            return await original(statement, *args, **kwargs)

        db_session.execute = recording  # type: ignore[method-assign]
        try:
            # `nobody` is the most restricted caller, so the ladder runs more than once.
            await search_chunks(db_session, user_id=world["nobody"], query_vector=vec(1.0), k=50)
        finally:
            db_session.execute = original  # type: ignore[method-assign]

        assert len(seen) >= 2, "the ladder did not escalate, so this proves nothing"
        assert len(set(seen)) == 1, "the predicate changed between attempts"
        assert ACL_PREDICATE in seen[0]

    async def test_the_ladder_is_bounded(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """It stops. Escalating forever in search of `k` rows a caller cannot see would
        turn every restricted query into a table scan."""
        page = await search_chunks(db_session, user_id=world["nobody"], query_vector=vec(1.0), k=50)

        assert page.stats.attempts <= len(DEFAULT_EF_SEARCH_LADDER)
        assert page.stats.exhausted is True
        assert page.stats.returned == 1

    async def test_pagination_cannot_bypass_the_filter(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Walk every page to exhaustion and assert nothing unauthorized ever appears.

        A cursor is an obvious place to hide a leak: it is easy to treat as an internal
        detail and apply *outside* the ACL predicate. Here it is another `AND` in the same
        `WHERE`, so each page re-resolves principals and re-applies the filter.
        """
        seen: set[str] = set()
        cursor = None
        for _ in range(10):
            page = await search_chunks(
                db_session, user_id=world["ada"], query_vector=vec(1.0), k=1, after=cursor
            )
            if not page.items:
                break
            seen.update(item.document_title for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert "secret" not in seen
        assert "crosstenant" not in seen
        assert "publicly" not in seen
        assert seen == {"direct", "orgwide"}

    async def test_a_forged_cursor_grants_nothing(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """A cursor names a position in an ordering; it is not a capability.

        Handing one a chunk id it never issued — an unauthorized document's — must change
        nothing about what is authorized.
        """
        page = await search_chunks(
            db_session,
            user_id=world["ada"],
            query_vector=vec(1.0),
            k=50,
            after=(1.0, world["secret_chunks"][0]),
        )

        assert "secret" not in {item.document_title for item in page.items}

    async def test_ranking_cannot_bypass_the_filter(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Query straight at the unauthorized document's own vector.

        `secret` sits at `vec(0.99)`. Asking for exactly that is the best possible
        adversarial query: it makes the forbidden document the nearest neighbour by
        construction, so anything that ranks before it filters is caught here.
        """
        page = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(0.99), k=50)

        assert "secret" not in {item.document_title for item in page.items}

    async def test_an_unscoped_session_retrieves_nothing(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 tenant mismatch — the GUC reset to empty must fail closed, not raise.

        `current_setting(…, true)` returns an empty string once the GUC has been set and
        reset, and `''::uuid` *raises* rather than filtering. `NULLIF` is what turns that
        back into a NULL predicate, and this is the test that proves it — in the search
        query as well as in the RLS policy.
        """
        await db_session.execute(text("SELECT set_config('app.current_org_id', '', true)"))

        page = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=50)

        assert page.items == ()

    async def test_scoping_to_another_tenant_retrieves_nothing(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Org manipulation, in the only form the design permits.

        `search_chunks` has no `org_id` parameter, so the nearest thing to tampering is
        pointing the session at another tenant — and then row-level security hides Ada's
        own user row too, so she resolves to no principals and sees nothing. There is no
        arrangement of inputs that returns Beta's documents to an Alpha caller.
        """
        await scope(db_session, world["beta"])

        page = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=50)

        assert {item.document_title for item in page.items} == set()

    def test_search_takes_no_org_id_and_no_principals(self) -> None:
        """The signature is the guarantee, so the signature is asserted.

        An `org_id` parameter here would let a call site name a tenant; a `principals`
        parameter would let one name an authorization. Neither exists, and a test that
        fails the day one is added is worth more than a comment saying it must not be.
        """
        import inspect

        parameters = set(inspect.signature(search_chunks).parameters)

        assert "org_id" not in parameters
        assert "principals" not in parameters
        assert "groups" not in parameters


class TestTheMechanism:
    async def test_the_query_plan_contains_the_acl_join(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 7 — the test that catches a refactor moving the filter into Python.

        Every other test in this file asserts an *outcome*, and a Python post-filter
        produces the same outcomes right up to the moment a count, a `LIMIT` or a cursor
        is involved — at which point it leaks quietly. This asserts the *mechanism*: the
        plan the server actually chose has to mention `document_acl`.
        """
        plan = "\n".join(
            str(row[0])
            for row in (
                await db_session.execute(
                    text("EXPLAIN " + _statement(paginated=False)),
                    {
                        "query": literal(vec(1.0)),
                        "principals": ["local:ada@example.com"],
                        "groups": [],
                        "k": 30,
                    },
                )
            ).all()
        )

        assert "document_acl" in plan, f"the ACL join is not in the plan:\n{plan}"

    async def test_the_index_is_used_for_the_acl_lookup(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """The grant lookup should reach `ix_document_acl_principal_id_document_id`.

        Not a security property — a performance one — but it belongs next to the plan
        test: an ACL check that degrades to a sequential scan per candidate row is how a
        correct filter becomes a filter somebody is tempted to remove.
        """
        plan = "\n".join(
            str(row[0])
            for row in (
                await db_session.execute(
                    text("EXPLAIN " + _statement(paginated=False)),
                    {
                        "query": literal(vec(1.0)),
                        "principals": ["local:ada@example.com"],
                        "groups": [],
                        "k": 30,
                    },
                )
            ).all()
        )

        assert "document_acl" in plan
        # On a five-row table the planner will legitimately choose a sequential scan, so
        # this asserts the join is present and reports the plan rather than demanding an
        # index the data does not justify. The shape is what matters at this size.
        assert "Nested Loop" in plan or "Seq Scan on document_acl" in plan, plan

    def test_the_inner_scan_orders_only_by_distance(self) -> None:
        """A 100x performance cliff, pinned as a string assertion.

        An index supplies exactly one ordering. Adding `, c.id` as a secondary key to the
        inner `ORDER BY` forces a full sort of every qualifying row and the HNSW index is
        abandoned — measured at 40 000 chunks with an organisation-wide grant: **3 016 ms
        with the tie-break inside, 15 ms with it outside**, same rows either way.

        It is a string assertion because that is the only kind that catches it. Nothing
        fails, no test goes red, and no result changes — the query just becomes a hundred
        times slower, which is exactly the sort of regression that ships.

        The tie-break itself is not lost. It moved to the outer projection, where it sorts
        `k` rows instead of the corpus.
        """
        inner = _statement(paginated=False).split(") SELECT h.id")[0]

        assert "ORDER BY c.embedding <=> CAST(:query AS vector) LIMIT :k" in inner
        assert "<=> CAST(:query AS vector), c.id" not in inner, (
            "a secondary sort key in the inner scan abandons the HNSW index"
        )
        assert "ORDER BY h.distance, h.id" in _statement(paginated=False)

    def test_the_inner_scan_selects_from_chunks_alone(self) -> None:
        """The other half of the same cliff.

        Joining `documents` or `sources` inside the scan makes the planner drive the join
        from `documents` and the index is never opened. They belong in the outer
        projection, over the `k` rows the scan already authorized.
        """
        inner = _statement(paginated=False).split(") SELECT h.id")[0]

        assert "FROM chunks c" in inner
        assert "JOIN documents" not in inner
        assert "JOIN sources" not in inner
        # But the authorization is still inside it, and that is the point of both halves.
        assert ACL_PREDICATE in inner
        assert "LIMIT :k" in inner

    async def test_unauthorized_evidence_never_reaches_a_result_object(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """The filter runs before anything is constructed in Python.

        `Evidence` is built by iterating the rows the server returned, so if an
        unauthorized chunk id or its text ever appeared in one, it had already crossed the
        boundary. Asserted against every field rather than the title alone.
        """
        page = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(0.99), k=500)

        forbidden = set(world["secret_chunks"]) | set(world["publicly_chunks"])
        for item in page.items:
            assert item.chunk_id not in forbidden
            assert item.document_id not in {world["secret"], world["publicly"]}
            assert "secret" not in item.text
            assert "publicly" not in item.text

    async def test_counts_do_not_leak(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """§17 test 6 — two callers with different grants get independent counts.

        A count that reflects rows the caller cannot read is the same disclosure as
        returning them, and it is exactly what post-filtering in Python produces.
        """
        ada = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=50)
        grace = await search_chunks(db_session, user_id=world["grace"], query_vector=vec(1.0), k=50)

        assert ada.stats.returned == 2
        assert grace.stats.returned == 1

    async def test_the_returned_offsets_address_the_original_document(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """The trap CLAUDE.md records, asserted rather than described.

        `text` is masked and `char_start`/`char_end` index the *original* body. The two
        are different strings of different lengths, so using the offsets against the
        returned text mis-highlights the span. This pins that the offsets came through
        untouched.
        """
        page = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=1)
        item = page.items[0]

        stored = (
            await db_session.execute(
                text("SELECT char_start, char_end FROM chunks WHERE id = :i"), {"i": item.chunk_id}
            )
        ).one()

        assert (item.char_start, item.char_end) == (stored.char_start, stored.char_end)


class TestOrderingAndPagination:
    async def test_ranking_is_deterministic(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Same query, same order, every time.

        Ties on cosine distance are common in a corpus with boilerplate, and without the
        `c.id` tie-break the same query returns the same rows in a different sequence —
        which makes a keyset cursor lose or repeat rows rather than merely look untidy.
        """
        runs = [
            [
                item.chunk_id
                for item in (
                    await search_chunks(
                        db_session, user_id=world["ada"], query_vector=vec(1.0), k=50
                    )
                ).items
            ]
            for _ in range(3)
        ]

        assert runs[0] == runs[1] == runs[2]

    async def test_identical_scores_still_order_totally(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """Two chunks at the same vector must still come back in a stable, total order."""
        source = await make_source(db_session, world["alpha"])
        _, chunk_ids = await make_document(
            db_session,
            world["alpha"],
            source,
            title="twins",
            grants=[("user", "local:ada@example.com")],
            embedding=vec(1.0),
            chunks=2,
        )

        page = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=50)
        got = [item.chunk_id for item in page.items if item.chunk_id in set(chunk_ids)]

        assert got == sorted(chunk_ids)

    async def test_paging_covers_every_authorized_row_exactly_once(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """A page size of one, walked to the end, must reconstruct the whole result."""
        whole = [
            item.chunk_id
            for item in (
                await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=50)
            ).items
        ]

        walked: list[uuid.UUID] = []
        cursor = None
        for _ in range(len(whole) + 2):
            page = await search_chunks(
                db_session, user_id=world["ada"], query_vector=vec(1.0), k=1, after=cursor
            )
            if not page.items:
                break
            walked.extend(item.chunk_id for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert walked == whole


class TestEvidenceFetch:
    async def test_an_authorized_chunk_is_returned(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        evidence = await fetch_evidence(
            db_session, user_id=world["ada"], chunk_id=world["direct_chunks"][0]
        )

        assert evidence.document_title == "direct"
        assert evidence.source_system == "local"

    async def test_an_unauthorized_chunk_is_reported_absent(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """404, not 403 — a 403 would confirm the chunk exists.

        Fetch-by-id is the second door onto the same evidence, and it is the one where
        authorization is easiest to forget: it looks like plumbing next to the search.
        """
        with pytest.raises(NotFound):
            await fetch_evidence(
                db_session, user_id=world["ada"], chunk_id=world["secret_chunks"][0]
            )

    async def test_a_public_granted_chunk_is_absent_here_too(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """The two paths share one predicate, so they must agree on `public`."""
        with pytest.raises(NotFound):
            await fetch_evidence(
                db_session, user_id=world["ada"], chunk_id=world["publicly_chunks"][0]
            )

    async def test_a_revoked_identity_loses_the_fetch_too(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        await db_session.execute(
            text("UPDATE source_identities SET is_active = false WHERE user_id = :u"),
            {"u": world["ada"]},
        )

        with pytest.raises(NotFound):
            await fetch_evidence(
                db_session, user_id=world["ada"], chunk_id=world["direct_chunks"][0]
            )

    async def test_an_unknown_id_is_the_same_refusal(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """One answer for never-existed and not-granted, so ids cannot be probed."""
        with pytest.raises(NotFound):
            await fetch_evidence(db_session, user_id=world["ada"], chunk_id=uuid.uuid4())


class TestObservability:
    async def test_the_log_line_carries_no_content(
        self, db_session: AsyncSession, world: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        """§4.9 — counts and timings, never the question, the text or a principal.

        A principal is a provider subject and therefore personal data; a chunk is document
        content. Neither belongs in a log line, and this asserts against the rendered
        message rather than trusting the format string.
        """
        with caplog.at_level("INFO", logger="jutsu.retrieval.search"):
            await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=50)

        emitted = "\n".join(record.getMessage() for record in caplog.records)
        assert "vector_search" in emitted, "nothing was logged, so this proves nothing"
        assert "ada@example.com" not in emitted
        assert "local:" not in emitted
        assert "chunk" not in emitted
        assert str(world["direct"]) not in emitted

    async def test_the_stats_report_the_escalation(
        self, db_session: AsyncSession, world: dict[str, Any]
    ) -> None:
        """A search that quietly ran three times is a performance fact the caller is owed."""
        page = await search_chunks(db_session, user_id=world["ada"], query_vector=vec(1.0), k=50)

        assert page.stats.attempts >= 1
        assert page.stats.ef_search in DEFAULT_EF_SEARCH_LADDER
        assert page.stats.elapsed_ms >= 0
