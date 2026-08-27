"""The production path, end to end, with nothing stubbed in the middle (§12, §15, §17).

```
a real registered user → their source identity → Principal resolution
    → ACL-filtered vector search → an authorized result
```

and the same chain with the identity revoked, which must yield nothing.

`packages/retrieval/tests/test_search_acl.py` is the adversarial suite for the query
itself and calls `search_chunks` directly. This file exists because that is not the same
claim. Every step above is individually tested somewhere; what is *not* otherwise tested
is that they are actually wired to each other — that registration's linking really feeds
`Principal`, and that `Principal` really feeds the filter. A slice can pass every unit
test in the repository with the last wire missing, and the symptom would be a caller who
is correctly authorized and retrieves nothing.

So the user here is created by POSTing to the real registration endpoint, and the
retrieval runs against a real Postgres with a real HNSW index.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.auth_service import scoped_acl_principals
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.routers import evidence as evidence_router
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER, declaration_of
from jutsu_core.rbac import ROLE_PERMISSIONS, Permission, Role
from jutsu_retrieval.search import search_chunks
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


def csrf_headers(client: AsyncClient) -> dict[str, str]:
    """The double-submit header a browser would send on a state-changing request."""
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


def vec(*leading: float) -> list[float]:
    return [*leading, *([0.0] * (DIM - len(leading)))]


def literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


@pytest.fixture
async def client(
    db_session: AsyncSession, settings: Settings, mailbox: RecordingEmailSender
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http


async def register_owner(client: AsyncClient, mailbox: RecordingEmailSender) -> dict[str, Any]:
    """Complete a real registration and return the resulting identity.

    Nothing is inserted by hand. The `local:ada@example.com` source identity this test
    depends on is created by S6.6's automatic linking, inside the registration
    transaction, from the address an OTP was actually redeemed against.
    """
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
    """One granted, embedded document. Returns its chunk id.

    Seeded directly because there is no ingestion route yet — S8 owns that. The grant is
    written in exactly the form S3's connector emits, `local:{address}`, so this is the
    shape a real ingested document has rather than a shape invented for the test.
    """
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


class TestTheProductionPath:
    async def test_a_registered_user_retrieves_the_document_granted_to_them(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The whole chain, with no step stubbed.

        The identity was created by registration. The principal set is resolved from it by
        the same code the request path uses. The filter is built from that. If any link
        were missing this returns nothing — which is why the assertion is on a document
        being *present*, not on one being absent.
        """
        who = await register_owner(client, mailbox)
        chunk_id = await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )

        principals, _ = await scoped_acl_principals(db_session, user_id=who["user_id"])
        assert principals == frozenset({"local:ada@example.com"}), "registration did not link"

        page = await search_chunks(db_session, user_id=who["user_id"], query_vector=vec(1.0), k=10)

        assert [item.chunk_id for item in page.items] == [chunk_id]
        assert page.items[0].document_title == "granted"
        assert page.items[0].source_system == "local"

    async def test_a_document_granted_to_somebody_else_is_not_retrieved(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Same tenant, nearer the query, granted to a principal this user does not hold."""
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

        page = await search_chunks(db_session, user_id=who["user_id"], query_vector=vec(1.0), k=50)

        assert {item.document_title for item in page.items} == {"granted"}

    async def test_revoking_the_identity_yields_zero_results(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The second half of the required path: revoked identity → nothing.

        The revocation is the S6.6 switch — `is_active = false`, the row kept. No cache to
        flush, no session to expire, no next login: the very next search resolves the
        principal set again and finds none.
        """
        who = await register_owner(client, mailbox)
        await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        before = await search_chunks(
            db_session, user_id=who["user_id"], query_vector=vec(1.0), k=10
        )
        assert before.stats.returned == 1, "nothing was retrievable, so this proves nothing"

        await db_session.execute(
            text(
                "UPDATE source_identities SET is_active = false, revoked_at = now() WHERE user_id = :u"
            ),
            {"u": who["user_id"]},
        )

        after = await search_chunks(db_session, user_id=who["user_id"], query_vector=vec(1.0), k=10)

        assert after.items == ()
        assert after.stats.returned == 0


class TestTheEvidenceRoute:
    async def test_an_authorized_chunk_comes_back_with_its_offsets(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The offsets index the **original** body, not the masked `text` returned here.

        Asserted rather than described: the seeded chunk spans characters 4 to 21 of the original, and
        those are the numbers a highlight must use. Applying them to `text` would land
        somewhere else entirely, which is the trap CLAUDE.md records.
        """
        who = await register_owner(client, mailbox)
        chunk_id = await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()

        response = await client.get(f"/v1/evidence/{chunk_id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["document_title"] == "granted"
        assert body["source_system"] == "local"
        assert (body["char_start"], body["char_end"]) == (4, 21)
        assert body["text"] == "granted body"

    async def test_an_unauthorized_chunk_is_a_404(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """404 and not 403, so the endpoint cannot be walked to enumerate documents.

        A 403 would confirm the chunk exists. Feed it ids, read the status codes, and the
        shape of a tenant's corpus falls out without ever being authorized to read a word
        of it.
        """
        who = await register_owner(client, mailbox)
        chunk_id = await seed_document(
            db_session,
            who["org_id"],
            title="forbidden",
            principal="local:eve@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()

        response = await client.get(f"/v1/evidence/{chunk_id}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert "forbidden" not in response.text, "the refusal leaked the document"

    async def test_an_unknown_chunk_is_refused_identically(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """One answer for never-existed and not-granted-to-you."""
        await register_owner(client, mailbox)
        await db_session.commit()

        response = await client.get(f"/v1/evidence/{uuid.uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_revoking_through_the_real_route_closes_this_one(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Two slices meeting: S6.6 revokes, S7 stops answering, over HTTP both ways.

        The revocation is not a hand-written `UPDATE` — it is `DELETE
        /v1/employees/{id}/identities/{sid}`, the endpoint an administrator actually uses,
        called here by the owner on their own identity (which S6.6 permits: removing your
        own access is not an escalation).

        Nothing is flushed, nothing expires, and the session cookie stays valid. The very
        next request resolves principals afresh, finds none, and the evidence is gone.
        """
        who = await register_owner(client, mailbox)
        chunk_id = await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()
        assert (await client.get(f"/v1/evidence/{chunk_id}")).status_code == 200

        listed = (await client.get("/v1/me/identities")).json()["items"]
        assert [item["subject"] for item in listed] == ["ada@example.com"]
        revoked = await client.delete(
            f"/v1/employees/{who['user_id']}/identities/{listed[0]['id']}",
            headers=csrf_headers(client),
        )
        assert revoked.status_code == 204, revoked.text

        assert (await client.get(f"/v1/evidence/{chunk_id}")).status_code == 404

    async def test_an_anonymous_caller_is_refused(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """401 before any authorization question is asked."""
        who = await register_owner(client, mailbox)
        chunk_id = await seed_document(
            db_session,
            who["org_id"],
            title="granted",
            principal="local:ada@example.com",
            embedding=vec(1.0),
        )
        await db_session.commit()
        client.cookies.clear()

        response = await client.get(f"/v1/evidence/{chunk_id}")

        assert response.status_code == 401


class TestTheRouteDeclaration:
    def test_the_route_declares_retrieval_query(self) -> None:
        declarations = {
            route.endpoint.__name__: declaration_of(route.endpoint)  # type: ignore[attr-defined]
            for route in evidence_router.router.routes
        }

        assert declarations["read_evidence"] is not None
        assert declarations["read_evidence"].permission is Permission.RETRIEVAL_QUERY

    def test_every_role_may_ask(self) -> None:
        """§17 — roles gate features, ACLs gate data.

        Retrieval is the feature every employee is hired to use; `document_acl` decides
        what comes back. Gating it on an administrative permission would invert that, and
        make a role decide what a person may read.
        """
        assert all(Permission.RETRIEVAL_QUERY in ROLE_PERMISSIONS[role] for role in Role)

    def test_holding_the_permission_grants_no_document(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Stated as a property of the catalogue rather than only in prose.

        Nothing in `Permission` names a document, a principal or an ACL. If one ever did,
        the §17 separation would be broken at the type level and this test is where it
        would be noticed.
        """
        forbidden = {"document", "chunk", "acl", "principal", "grant"}
        for permission in Permission:
            assert not (forbidden & set(permission.value.replace(":", "_").split("_"))), (
                f"{permission.value} names data, not a feature"
            )
