"""`POST /v1/search` — the retrieval surface the frontend will call (§12, §17).

`packages/retrieval/tests/test_search_acl.py` is the adversarial suite for the SQL, and
`test_evidence_retrieval.py` proves registration really feeds `Principal`. This file
tests the HTTP boundary that sits on top of both: request validation, the cursor codec,
the mapping from embedding failures onto status codes, and — the one that matters —
that none of it introduced a way to widen what a caller may see.

**No credential and no network.** The transport is a scripted fake implementing the
`EmbeddingTransport` Protocol, injected through `get_query_embedder`. That seam exists
in S6 precisely so a test can prove retry, budget and error behaviour without spending
anything, and it is the same seam here. A test in this file that reached Vertex would
be a defect in the test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.retrieval import decode_cursor, encode_cursor, get_query_embedder
from jutsu_api.routers.search import MAX_K
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_core.errors import RateLimited, ValidationFailed
from jutsu_retrieval import (
    EmbeddingSettings,
    PermanentEmbeddingError,
    TransientEmbeddingError,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

DIM = 768

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
    "terms_accepted": True,
}


def vec(*leading: float) -> list[float]:
    return [*leading, *([0.0] * (DIM - len(leading)))]


def literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def csrf_headers(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


# ------------------------------------------------------------------ the scripted provider


class ScriptedTransport:
    """An `EmbeddingTransport` that returns a fixed vector, or raises what it is told.

    Records every call so a test can assert the provider was *not* reached — which is
    how the "a malformed cursor costs nothing" claim is checked.
    """

    def __init__(self, vector: list[float] | None = None, error: Exception | None = None) -> None:
        self.vector = vector if vector is not None else vec(1.0)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def predict(
        self, *, instances: list[dict[str, Any]], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append({"instances": instances, "parameters": parameters})
        if self.error is not None:
            raise self.error
        return {
            "predictions": [
                {
                    "embeddings": {
                        "values": self.vector,
                        "statistics": {"token_count": 7, "truncated": False},
                    }
                }
            ]
        }


@pytest.fixture
async def client(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """The app, plus the two things the rate limiter forces this fixture to own.

    The limiter opens its **own** session through `jutsu_db.engine`, because its spend
    has to commit independently of the request transaction. That has two consequences
    here, both of which produced real failures before they were handled:

    *`DATABASE_URL` must point at the application role.* `db_session` sets it to the
    privileged migration URL so Alembic can run, and a limiter connecting as the owner
    would have row-level security silently inert — every isolation assertion in this
    file would pass while proving nothing.

    *The engine must be disposed around every test.* `jutsu_db.engine` caches one engine
    per process, and each test runs on its own event loop. Reusing the cache means a
    connection pool bound to a **closed** loop, which surfaces as `RuntimeError: Event
    loop is closed` in whichever unrelated test happens to run next — the CLAUDE.md trap
    about that cache, arriving as a lifetime problem rather than a schema one.
    """
    from jutsu_db.engine import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)

    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        http.app = app  # type: ignore[attr-defined]
        yield http

    await dispose_engine()


def use_provider(
    client: AsyncClient, *, vector: list[float] | None = None, error: Exception | None = None
) -> ScriptedTransport:
    """Point the route's embedder at a scripted transport. Returns it for assertions."""
    from jutsu_api.retrieval import QueryEmbedder

    scripted = ScriptedTransport(vector=vector, error=error)
    settings = EmbeddingSettings(project="test", location="asia-south1")
    app = client.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_query_embedder] = lambda: QueryEmbedder(scripted, settings)
    return scripted


async def register_owner(client: AsyncClient, mailbox: RecordingEmailSender) -> dict[str, Any]:
    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert verified.status_code == 200, verified.text
    me = (await client.get("/v1/me")).json()
    org = (await client.get("/v1/orgs/current")).json()
    return {"user_id": uuid.UUID(me["user_id"]), "org_id": uuid.UUID(org["id"])}


async def seed_document(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    title: str,
    principal: str,
    embedding: list[float],
) -> uuid.UUID:
    """One granted, embedded document, in the shape S3's connector emits."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )
    source_id, doc_id, chunk_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:i,:o,'local','{}'::jsonb)"
        ),
        {"i": source_id, "o": org_id},
    )
    await session.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:i,:o,:s,:e,:t,'h','a','the original body','the masked body',now())"
        ),
        {"i": doc_id, "o": org_id, "s": source_id, "e": str(doc_id), "t": title},
    )
    await session.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id) "
            "VALUES (:d,'user',:p,:o)"
        ),
        {"d": doc_id, "p": principal, "o": org_id},
    )
    await session.execute(
        text(
            "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, char_end, "
            "token_count, embedding) VALUES (:i,:d,:o,0,:x,4,21,5,CAST(:v AS vector))"
        ),
        {"i": chunk_id, "d": doc_id, "o": org_id, "x": f"{title} body", "v": literal(embedding)},
    )
    return chunk_id


async def search(client: AsyncClient, **body: Any) -> Any:
    return await client.post("/v1/search", json=body, headers=csrf_headers(client))


# ------------------------------------------------------------------------------ the route


class TestTheHappyPath:
    async def test_an_authorized_document_comes_back(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        who = await register_owner(client, mailbox)
        chunk_id = await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()
        use_provider(client, vector=vec(1.0))

        response = await search(client, query="who owns billing?", k=10)

        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["chunk_id"] for item in body["items"]] == [str(chunk_id)]
        assert body["items"][0]["document_title"] == "granted"
        assert body["items"][0]["source_system"] == "local"
        assert body["stats"]["returned"] == 1

    async def test_the_offsets_are_the_originals_and_the_text_is_masked(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The trap CLAUDE.md records, asserted at the HTTP boundary.

        The chunk spans characters 4 to 21 of the *original* body; `text` is the masked
        string. A frontend highlighting `text` with those numbers lands somewhere else.
        """
        who = await register_owner(client, mailbox)
        await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()
        use_provider(client, vector=vec(1.0))

        item = (await search(client, query="q")).json()["items"][0]

        assert (item["char_start"], item["char_end"]) == (4, 21)
        assert item["text"] == "granted body"
        assert "the original body" not in item["text"]

    async def test_the_provider_token_count_is_reported(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The provider's own figure, never an estimate (§20)."""
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)

        body = (await search(client, query="q")).json()

        assert body["query_tokens"] == 7

    async def test_the_query_is_embedded_as_a_query_not_a_document(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Cosine 0.915 between the two task types — close enough to look like it works."""
        await register_owner(client, mailbox)
        await db_session.commit()
        scripted = use_provider(client)

        await search(client, query="who owns billing?")

        assert scripted.calls[0]["instances"][0]["task_type"] == "RETRIEVAL_QUERY"


class TestAuthorization:
    async def test_an_anonymous_caller_is_401(self, client: AsyncClient) -> None:
        use_provider(client)
        response = await client.post("/v1/search", json={"query": "q"})
        assert response.status_code == 401

    async def test_a_document_granted_to_somebody_else_is_not_returned(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Nearer the query, and still invisible. The filter is not a ranking tweak."""
        who = await register_owner(client, mailbox)
        await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(0.8, 0.2),
        )
        await seed_document(
            db_session,
            who["org_id"],
            title="forbidden",
            principal="local:eve@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()
        use_provider(client, vector=vec(1.0))

        titles = {
            i["document_title"] for i in (await search(client, query="q", k=50)).json()["items"]
        }

        assert titles == {"granted"}

    async def test_another_tenants_document_is_invisible(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Cross-org isolation, through the route rather than through the query."""
        who = await register_owner(client, mailbox)
        other_org = uuid.uuid4()
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(other_org)}
        )
        await db_session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, 'other')"), {"i": other_org}
        )
        await seed_document(
            db_session,
            other_org,
            title="other-tenant",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()
        use_provider(client, vector=vec(1.0))

        body = (await search(client, query="q", k=50)).json()

        assert body["items"] == []
        assert who["org_id"] != other_org

    async def test_a_caller_with_no_linked_identity_gets_an_empty_page_not_an_error(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """§17 test 1. Revoking the identity must not turn into a 403 or a 500."""
        who = await register_owner(client, mailbox)
        await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        await db_session.execute(
            text("UPDATE source_identities SET is_active = false, revoked_at = now()")
        )
        await db_session.commit()
        use_provider(client, vector=vec(1.0))

        response = await search(client, query="q")

        assert response.status_code == 200
        assert response.json()["items"] == []


class TestRequestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"query": ""},
            {"query": "   x" * 0},
            {"query": "x" * 4001},
            {"query": "q", "k": 0},
            {"query": "q", "k": MAX_K + 1},
            {"query": "q", "k": "many"},
        ],
    )
    async def test_a_bad_request_is_422(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        body: dict[str, Any],
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)

        response = await search(client, **body)

        assert response.status_code == 422, response.text

    async def test_a_malformed_cursor_is_422_and_costs_nothing(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Validated before the provider is called, so a bad token is a free rejection."""
        await register_owner(client, mailbox)
        await db_session.commit()
        scripted = use_provider(client)

        response = await search(client, query="q", cursor="not-a-cursor!!")

        assert response.status_code == 422, response.text
        assert scripted.calls == [], "a malformed cursor paid the provider"


class TestPagination:
    async def test_a_cursor_walks_the_ordering_without_repeating(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        who = await register_owner(client, mailbox)
        for index in range(3):
            await seed_document(
                db_session,
                who["org_id"],
                title=f"doc-{index}",
                principal="local:ada@example.com",
                embedding=vec(1.0 - index * 0.1, index * 0.1),
            )
        await db_session.commit()
        use_provider(client, vector=vec(1.0))

        first = (await search(client, query="q", k=2)).json()
        assert len(first["items"]) == 2
        assert first["next_cursor"]

        second = (await search(client, query="q", k=2, cursor=first["next_cursor"])).json()

        seen = [i["chunk_id"] for i in first["items"]]
        assert all(i["chunk_id"] not in seen for i in second["items"]), "a page repeated a row"

    async def test_the_cursor_round_trips_exactly(self) -> None:
        """`repr` on the float: a cursor that rounds skips or repeats the row it names."""
        chunk_id = uuid.uuid4()
        score = 0.8414709848078965
        assert decode_cursor(encode_cursor(score, chunk_id)) == (score, chunk_id)

    @pytest.mark.parametrize(
        "value", ["", "!!!!", "YWJj", encode_cursor(1.0, uuid.uuid4())[:-4] + "zzzz"]
    )
    def test_a_bad_cursor_raises_validation_failed(self, value: str) -> None:
        with pytest.raises(ValidationFailed):
            decode_cursor(value)

    def test_a_nan_cursor_is_refused(self) -> None:
        """NaN compares false against everything, so it would page for ever on nothing."""
        import base64

        raw = base64.urlsafe_b64encode(f"nan:{uuid.uuid4()}".encode()).decode().rstrip("=")
        with pytest.raises(ValidationFailed):
            decode_cursor(raw)


class TestProviderFailuresBecomeHttpErrors:
    async def test_a_transient_failure_is_503(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client, error=TransientEmbeddingError("429", status=429, retry_after=1.0))

        response = await search(client, query="q")

        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "service_unavailable"

    async def test_a_permanent_failure_is_422(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Not a 502: the input was rejected and will be rejected identically for ever."""
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client, error=PermanentEmbeddingError("400", status=400))

        response = await search(client, query="q")

        assert response.status_code == 422, response.text

    async def test_budget_exhaustion_is_429(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)
        monkeypatch.setenv("TOKEN_BUDGET_PER_REQUEST", "1")

        response = await search(client, query="q")

        assert response.status_code == 429, response.text
        assert response.json()["error"]["code"] == "rate_limited"

    async def test_no_failure_leaks_the_query_into_the_response(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """§4.9: the provider's body quotes the input, so nothing forwards it."""
        await register_owner(client, mailbox)
        await db_session.commit()
        secret = "the merger with Zephyr closes on Tuesday"
        use_provider(client, error=PermanentEmbeddingError("400", status=400))

        response = await search(client, query=secret)

        assert secret not in response.text


class TestTheBudgetIsPerRequest:
    async def test_spend_does_not_accumulate_across_requests(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The outage this design exists to prevent.

        A process-wide ledger would cross its ceiling some afternoon and refuse **every**
        subsequent search for the life of the process. Ten requests against a budget of
        ten tokens each, where one request costs seven: all ten must succeed. With a
        shared ledger the second one fails.
        """
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)
        monkeypatch.setenv("TOKEN_BUDGET_PER_REQUEST", "10")

        for attempt in range(10):
            response = await search(client, query=f"question {attempt}")
            assert response.status_code == 200, f"request {attempt} failed: {response.text}"
            assert response.json()["query_tokens"] == 7


class TestTheContractIsDeclared:
    def test_the_route_declares_its_permission(self) -> None:
        """`GuardedAPIRoute` refuses to build an undeclared route, so this is belt and
        braces — but it names the permission, which the startup check cannot."""
        from jutsu_api.routers.search import search as endpoint
        from jutsu_api.security import declaration_of
        from jutsu_core.rbac import Permission

        declaration = declaration_of(endpoint)
        assert declaration is not None, "the route declares no authorization at all"
        assert declaration.permission is Permission.RETRIEVAL_QUERY

    def test_the_openapi_document_carries_the_route(self) -> None:
        """Regenerating the client is part of the change, not a follow-up."""
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        document = json.loads((root / "apps/web/lib/openapi.json").read_text(encoding="utf-8"))
        assert "/v1/search" in document["paths"]
        assert "post" in document["paths"]["/v1/search"]


# ------------------------------------------------------------------------- rate limiting


async def budget_rows(session: AsyncSession, org_id: uuid.UUID) -> Any:
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )
    return (
        await session.execute(
            text("SELECT org_id, user_id, spent, window_start FROM search_budget")
        )
    ).all()


async def add_user(session: AsyncSession, org_id: uuid.UUID, email: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(org_id)}
    )
    await session.execute(
        text("INSERT INTO users (id, org_id, email, status) VALUES (:i,:o,:e,'active')"),
        {"i": user_id, "o": org_id, "e": email},
    )
    return user_id


class TestRateLimiting:
    async def test_requests_below_the_limit_succeed(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "5")

        for attempt in range(4):
            response = await search(client, query=f"q{attempt}")
            assert response.status_code == 200, f"request {attempt}: {response.text}"

    async def test_the_request_exactly_at_the_limit_still_succeeds(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Off-by-one: the limit is how many are allowed, not how many before refusal."""
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "3")

        codes = [(await search(client, query="q")).status_code for _ in range(3)]

        assert codes == [200, 200, 200], codes

    async def test_the_request_above_the_limit_is_429(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "2")

        for _ in range(2):
            assert (await search(client, query="q")).status_code == 200
        refused = await search(client, query="q")

        assert refused.status_code == 429, refused.text
        assert refused.json()["error"]["code"] == "rate_limited"
        assert refused.json()["error"]["details"]["limit"] == 2

    async def test_the_limiter_runs_before_the_paid_call(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refused caller must cost nothing. The whole reason the limiter exists."""
        await register_owner(client, mailbox)
        await db_session.commit()
        scripted = use_provider(client)
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "1")

        assert (await search(client, query="q")).status_code == 200
        before = len(scripted.calls)
        assert (await search(client, query="q")).status_code == 429

        assert len(scripted.calls) == before, "a refused request still called the provider"

    async def test_a_failed_embedding_still_consumes_quota(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The chosen policy, and the reason the spend commits on its own transaction.

        The request transaction rolls back on the 503. If the spend lived on it, the
        caller would retry for ever against a metered API behind a limiter that looked
        like it was working.
        """
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client, error=TransientEmbeddingError("429", status=429))
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "2")

        assert (await search(client, query="q")).status_code == 503
        assert (await search(client, query="q")).status_code == 503
        refused = await search(client, query="q")

        assert refused.status_code == 429, "a failed request did not consume its quota"

    async def test_concurrent_requests_cannot_all_pass_the_same_check(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A SELECT-then-UPDATE limiter passes this only by luck.

        Ten simultaneous spends against a limit of three: exactly three may be granted.
        The atomic INSERT ... ON CONFLICT ... RETURNING is what makes that true; split
        into a read and a write, all ten read `spent = 0` and all ten proceed.

        Driven through `spend_search_budget` rather than through ten HTTP requests on
        purpose — those would share this test's single `db_session` connection, and
        asyncpg refuses concurrent use of one connection whatever the limiter does. The
        claim under test is the statement's atomicity, and the limiter opens its own
        pooled session per call, so this exercises exactly it.
        """
        import asyncio

        from jutsu_api.rate_limit import spend_search_budget

        who = await register_owner(client, mailbox)
        await db_session.commit()
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "3")

        results = await asyncio.gather(
            *(spend_search_budget(org_id=who["org_id"], user_id=who["user_id"]) for _ in range(10)),
            return_exceptions=True,
        )
        granted = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, RateLimited)]

        assert len(granted) == 3, f"expected 3 grants, got {len(granted)}: {results}"
        assert len(refused) == 7, f"expected 7 refusals, got {len(refused)}: {results}"

    async def test_two_users_have_independent_budgets(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keyed on `(org_id, user_id)`, so one colleague cannot exhaust another."""
        from jutsu_api.rate_limit import spend_search_budget

        who = await register_owner(client, mailbox)
        colleague = await add_user(db_session, who["org_id"], "colleague@example.com")
        await db_session.commit()
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "1")

        await spend_search_budget(org_id=who["org_id"], user_id=who["user_id"])
        with pytest.raises(RateLimited):
            await spend_search_budget(org_id=who["org_id"], user_id=who["user_id"])

        remaining = await spend_search_budget(org_id=who["org_id"], user_id=colleague)

        assert remaining == 0, "a second user inherited the first user's spend"

    async def test_two_organisations_have_independent_budgets(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from jutsu_api.rate_limit import spend_search_budget

        who = await register_owner(client, mailbox)
        other_org = uuid.uuid4()
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(other_org)}
        )
        await db_session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:i, 'other')"), {"i": other_org}
        )
        other_user = await add_user(db_session, other_org, "x@other.example")
        await db_session.commit()
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "1")

        await spend_search_budget(org_id=who["org_id"], user_id=who["user_id"])
        remaining = await spend_search_budget(org_id=other_org, user_id=other_user)

        assert remaining == 0, "a second tenant inherited the first tenant's spend"

    async def test_the_window_resets(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Aged by moving `window_start`, not by sleeping.

        A test that waits sixty real seconds to prove a sixty-second window is a test
        nobody runs, and a limiter nobody tests.
        """
        from jutsu_api.rate_limit import spend_search_budget

        who = await register_owner(client, mailbox)
        await db_session.commit()
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "1")
        monkeypatch.setenv("SEARCH_RATE_WINDOW_S", "60")

        await spend_search_budget(org_id=who["org_id"], user_id=who["user_id"])
        with pytest.raises(RateLimited):
            await spend_search_budget(org_id=who["org_id"], user_id=who["user_id"])

        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(who["org_id"])}
        )
        await db_session.execute(
            text("UPDATE search_budget SET window_start = now() - interval '61 seconds'")
        )
        await db_session.commit()

        remaining = await spend_search_budget(org_id=who["org_id"], user_id=who["user_id"])

        assert remaining == 0, "the window did not roll"

    async def test_the_counter_lives_in_the_database_not_the_process(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cross-instance persistence.

        A second API instance shares no Python state with the first; what it shares is
        this row. Asserting the row is what distinguishes a real limiter from an
        in-memory one, which would multiply the effective limit by the instance count.
        """
        who = await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "5")

        await search(client, query="q")
        await search(client, query="q")

        rows = await budget_rows(db_session, who["org_id"])

        assert len(rows) == 1
        assert rows[0].spent == 2, "the spend was not durable outside the process"
        assert rows[0].org_id == who["org_id"]
        assert rows[0].user_id == who["user_id"]

    async def test_the_budget_row_is_invisible_to_another_tenant(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
    ) -> None:
        """Row-level security on the new table, asserted through the application role.

        The `client` fixture is what makes this meaningful: it repoints the limiter's
        engine at the application role. Connected as the migration owner the policy is
        inert and this passes vacuously.
        """
        who = await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)

        await search(client, query="q")

        assert len(await budget_rows(db_session, who["org_id"])) == 1
        assert await budget_rows(db_session, uuid.uuid4()) == []

    async def test_a_refusal_never_echoes_the_query(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)
        monkeypatch.setenv("SEARCH_RATE_LIMIT", "1")
        secret = "the merger with Zephyr closes on Tuesday"

        await search(client, query=secret)
        refused = await search(client, query=secret)

        assert refused.status_code == 429
        assert secret not in refused.text
        assert "Zephyr" not in refused.text


class TestRateLimitConfiguration:
    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_a_non_positive_limit_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Reading 0 as unlimited is how a guardrail disappears with nobody removing it."""
        from jutsu_api.rate_limit import search_rate_limit_settings

        monkeypatch.setenv("SEARCH_RATE_LIMIT", value)
        with pytest.raises(RuntimeError, match="not a limit"):
            search_rate_limit_settings()

    def test_an_unparseable_window_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jutsu_api.rate_limit import search_rate_limit_settings

        monkeypatch.setenv("SEARCH_RATE_WINDOW_S", "a minute")
        with pytest.raises(RuntimeError, match="positive integer"):
            search_rate_limit_settings()

    def test_the_documented_defaults_are_what_ships(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jutsu_api.rate_limit import search_rate_limit_settings

        monkeypatch.delenv("SEARCH_RATE_LIMIT", raising=False)
        monkeypatch.delenv("SEARCH_RATE_WINDOW_S", raising=False)
        settings = search_rate_limit_settings()

        assert (settings.limit, settings.window_seconds) == (60, 60)


class TestTheLimiterIsSubjectToRowLevelSecurity:
    """The assertion the rest of this file's isolation claims rest on.

    A superuser bypasses RLS unconditionally and `FORCE` does not change that — FORCE
    covers the table *owner* only. So "another tenant cannot see this row" is a vacuous
    claim unless the connection making it is the restricted role. `db_session` sets
    `DATABASE_URL` to the privileged **migration** URL so Alembic can run, and the
    limiter opens its own session from that variable; without the `client` fixture
    repointing it, every isolation test here would pass while proving nothing.

    `packages/db` already guards this for the request path with
    `test_app_role_cannot_bypass_rls`. These are the same guard for the limiter's
    separate connection, which that test cannot see.
    """

    async def test_the_limiter_connects_as_the_restricted_role(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Named explicitly: `jutsu_app`, NOSUPERUSER and NOBYPASSRLS.

        Asserted on the connection `spend_search_budget` actually uses, not on a
        connection this test opened to look like it.
        """
        from jutsu_db.engine import org_session

        who = await register_owner(client, mailbox)
        await db_session.commit()

        async with org_session(who["org_id"]) as session:
            role = (await session.execute(text("SELECT current_user"))).scalar_one()
            bypass = (
                await session.execute(
                    text("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).scalar_one()
            superuser = (
                await session.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                )
            ).scalar_one()

        assert role == "jutsu_app", f"the limiter connected as {role!r}, so RLS is inert"
        assert bypass is False, "the limiter's role can bypass RLS"
        assert superuser is False, "the limiter's role is a superuser"

    async def test_the_policy_is_enabled_and_forced_on_search_budget(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """ENABLE alone exempts the owner; FORCE is what closes that.

        Checked against the catalogue rather than inferred from behaviour, because the
        behavioural test passes either way as long as the caller is not the owner.
        """
        await register_owner(client, mailbox)
        await db_session.commit()

        row = (
            await db_session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE oid = 'public.search_budget'::regclass"
                )
            )
        ).one()
        policies = (
            await db_session.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = 'search_budget'")
            )
        ).scalar_one()

        assert row.relrowsecurity is True, "row-level security is not enabled"
        assert row.relforcerowsecurity is True, "row-level security is not FORCED"
        assert policies == 1, f"expected exactly one policy on search_budget, found {policies}"

    async def test_a_spend_is_written_under_the_callers_tenant(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """`org_id` is taken from the GUC inside the statement, never from a parameter.

        The INSERT reads `current_setting('app.current_org_id')` for `org_id`, so the
        row lands under whichever tenant the session is scoped to and the WITH CHECK
        clause refuses anything else. A call site cannot write a budget row into another
        organisation even by passing one.
        """
        from jutsu_api.rate_limit import spend_search_budget

        who = await register_owner(client, mailbox)
        await db_session.commit()

        await spend_search_budget(org_id=who["org_id"], user_id=who["user_id"])

        rows = await budget_rows(db_session, who["org_id"])
        assert [r.org_id for r in rows] == [who["org_id"]]

    async def test_another_tenant_cannot_read_or_delete_the_row(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Invisible, and therefore also un-deletable — RLS filters writes as well.

        A limiter whose counter another tenant could delete is not a limiter.
        """
        who = await register_owner(client, mailbox)
        await db_session.commit()
        use_provider(client)

        await search(client, query="q")
        assert len(await budget_rows(db_session, who["org_id"])) == 1

        stranger = uuid.uuid4()
        await db_session.execute(
            text("SELECT set_config('app.current_org_id', :o, true)"), {"o": str(stranger)}
        )
        deleted = (
            await db_session.execute(text("DELETE FROM search_budget RETURNING org_id"))
        ).all()

        assert deleted == [], "another tenant deleted this tenant's budget row"
        assert len(await budget_rows(db_session, who["org_id"])) == 1
