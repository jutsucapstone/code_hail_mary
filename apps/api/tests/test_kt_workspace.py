"""The KT console's workspace over the wire: copilot, conversations, bookmarks, progress
and the computed overview — against real Postgres, RLS and the audit trail.

What is pinned here is the set of properties the design leans on: a turn is kept and its
citations are references; history reaches the model as context and never as a passage;
the window narrows and cannot widen; a saved item is re-checked under today's ACL; and
every one of these closes the moment the package does.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.retrieval import get_query_embedder
from jutsu_api.routers.search import get_answer_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
    "terms_accepted": True,
}

OWNER_EMAIL = "ada@example.com"
RECIPIENT_EMAIL = "newhire@example.com"
#: What invitation acceptance links for the recipient (ADR 0014): the address, in the
#: `local` namespace. Documents granted to it are the ones the recipient may read.
RECIPIENT_PRINCIPAL = "local:newhire@example.com"

#: A unit vector along one axis. The fake embedder returns the same, so every seeded
#: chunk is at cosine distance 0 from every question — relevance is not under test here.
UNIT = "[" + ",".join(["1.0"] + ["0.0"] * 767) + "]"


class FakeEmbedder:
    async def embed(self, query: str) -> tuple[list[float], int]:
        return [1.0] + [0.0] * 767, 7


class RecordingModel:
    """Answers from a script and records every prompt, so a test can see what the model
    was shown — the whole point of the history assertions."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append(prompt)
        return self.responses.pop(0) if self.responses else "INSUFFICIENT_EVIDENCE"


@pytest.fixture
def model() -> RecordingModel:
    return RecordingModel()


@pytest.fixture
async def client(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    model: RecordingModel,
) -> AsyncIterator[AsyncClient]:
    from jutsu_db.engine import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")

    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_query_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_answer_transport] = lambda: model

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as http:
        yield http

    await dispose_engine()


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


async def register_owner(client: AsyncClient, mailbox: RecordingEmailSender) -> None:
    await client.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    response = await client.post(
        "/v1/orgs/register/verify",
        json={"token": delivered["token"], "code": delivered["code"]},
    )
    assert response.status_code == 200, response.text


async def invite_and_accept(
    client: AsyncClient,
    mailbox: RecordingEmailSender,
    *,
    email: str,
    full_name: str = "Grace Hopper",
) -> None:
    invited = await client.post(
        "/v1/employees/invitations",
        json={"email": email, "role": "member"},
        headers=csrf(client),
    )
    assert invited.status_code == 202, invited.text
    token = mailbox.last.secrets["token"]
    accepted = await client.post(
        "/v1/invitations/accept", json={"token": token, "full_name": full_name}
    )
    assert accepted.status_code == 200, accepted.text


async def sign_in(client: AsyncClient, mailbox: RecordingEmailSender, *, email: str) -> None:
    await client.post("/v1/auth/request", json={"email": email})
    delivered = mailbox.last.secrets
    verified = await client.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def user_id_of(client: AsyncClient, email: str) -> str:
    page = (await client.get("/v1/employees", params={"q": email})).json()
    assert page["items"], f"no employee matching {email}"
    return str(page["items"][0]["id"])


async def scope(client: AsyncClient, session: AsyncSession) -> str:
    """Re-enter the caller's tenant on the test session; returns the org id."""
    org_id = str((await client.get("/v1/me")).json()["org_id"])
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id}
    )
    return org_id


async def seed_document(
    session: AsyncSession,
    org_id: str,
    *,
    principal: str,
    title: str,
    quote: str,
    created_at: datetime | None = None,
    claim_type: str | None = "decision",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]:
    """A readable document with one embedded chunk, one finished extraction run whose
    stats name it, and (unless `claim_type` is None) one quote-gated claim.

    The session must already be scoped. Returns `(document_id, chunk_id, claim_id)`.
    """
    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:id, :org, 'local', '{}'::jsonb)"
        ),
        {"id": source_id, "org": org_id},
    )
    doc_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:id, :org, :src, :ext, :title, :ext, 'a', :q, :q, :created)"
        ),
        {
            "id": doc_id,
            "org": org_id,
            "src": source_id,
            "ext": str(doc_id),
            "title": title,
            "q": quote,
            "created": created_at or datetime.now(tz=UTC),
        },
    )
    chunk_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, char_end, "
            "token_count, embedding) VALUES (:id, :doc, :org, 0, :text, 0, :end, 5, "
            "CAST(:v AS vector))"
        ),
        {"id": chunk_id, "doc": doc_id, "org": org_id, "text": quote, "end": len(quote), "v": UNIT},
    )
    await session.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
            "permission) VALUES (:doc, 'user', :pid, :org, 'read')"
        ),
        {"doc": doc_id, "pid": principal, "org": org_id},
    )
    run_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, model, "
            "finished_at, stats_json) VALUES (:id, :org, 'v1', 'h', 'test', now(), "
            "cast(:stats AS jsonb))"
        ),
        {
            "id": run_id,
            "org": org_id,
            "stats": (
                '{"document_id": "' + str(doc_id) + '", "chunks_covered": 1, "chunks_total": 1}'
            ),
        },
    )
    claim_id: uuid.UUID | None = None
    if claim_type is not None:
        claim_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, claim_type, "
                "payload_json, confidence) VALUES (:id, :run, :chunk, :org, :type, "
                "cast(:payload AS jsonb), 0.9)"
            ),
            {
                "id": claim_id,
                "run": run_id,
                "chunk": chunk_id,
                "org": org_id,
                "type": claim_type,
                "payload": '{"summary": "' + title + '", "quote": "' + quote + '"}',
            },
        )
    return doc_id, chunk_id, claim_id


async def recipient_in_package(
    client: AsyncClient,
    mailbox: RecordingEmailSender,
    *,
    scope_: list[str] | None = None,
    period_days: int | None = None,
) -> tuple[str, str]:
    """Owner registered, a leaver invited, a package created, a new hire invited and the
    package claimed by them. Leaves the client signed in as the recipient. Returns
    `(kt_code, org_id)`."""
    await register_owner(client, mailbox)
    await invite_and_accept(client, mailbox, email="leaver@example.com")
    await sign_in(client, mailbox, email=OWNER_EMAIL)
    org_id = str((await client.get("/v1/me")).json()["org_id"])
    subject = await user_id_of(client, "leaver@example.com")
    payload: dict[str, object] = {
        "subject_user_id": subject,
        "scope": scope_ or ["documents", "profile", "decisions", "people"],
        "validity_days": 30,
    }
    if period_days is not None:
        payload["period_days"] = period_days
    created = await client.post("/v1/kt", json=payload, headers=csrf(client))
    assert created.status_code == 201, created.text
    code = str(created.json()["kt_code"])

    await invite_and_accept(client, mailbox, email=RECIPIENT_EMAIL, full_name="New Hire")
    opened = await client.post("/v1/kt/claim", json={"kt_code": code}, headers=csrf(client))
    assert opened.status_code == 200, opened.text
    return code, org_id


async def ask(client: AsyncClient, code: str, question: str, **extra: object) -> object:
    return await client.post(
        f"/v1/kt/{code}/ask", json={"question": question, **extra}, headers=csrf(client)
    )


# --------------------------------------------------------------------------------------


class TestCopilot:
    async def test_unconfigured_answers_refuse_for_free(
        self, client: AsyncClient, mailbox: RecordingEmailSender, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        response = await ask(client, code, "What was decided?")

        assert response.status_code == 503  # type: ignore[attr-defined]
        assert "not configured" in response.json()["error"]["message"]  # type: ignore[attr-defined]
        assert (await client.get(f"/v1/kt/{code}/conversations")).json()["items"] == []

    async def test_no_visible_evidence_refuses_and_still_keeps_the_turn(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        model: RecordingModel,
    ) -> None:
        """An honest refusal is still a turn the recipient asked; it is kept as such, and
        the trail records counts — never the question."""
        code, _ = await recipient_in_package(client, mailbox)

        response = await ask(client, code, "What was decided about storage?")
        assert response.status_code == 200, response.text  # type: ignore[attr-defined]
        body = response.json()  # type: ignore[attr-defined]
        assert body["insufficient_evidence"] is True
        assert body["answer"] is None
        assert body["sources"] == []
        assert body["attempts"] == 0
        assert model.calls == [], "no evidence means no model call"

        conversations = (await client.get(f"/v1/kt/{code}/conversations")).json()
        assert len(conversations["items"]) == 1
        assert conversations["items"][0]["title"] == "What was decided about storage?"
        assert conversations["items"][0]["message_count"] == 2

        detail = (await client.get(f"/v1/kt/{code}/conversations/{body['conversation_id']}")).json()
        roles = [m["role"] for m in detail["messages"]]
        assert roles == ["user", "assistant"]
        assert detail["messages"][1]["insufficient_evidence"] is True
        assert "does not answer" in detail["messages"][1]["content"]

        await scope(client, db_session)
        trail = (
            await db_session.execute(
                text(
                    "SELECT meta_json, correlation_id FROM audit_log "
                    "WHERE action = 'kt.copilot_asked'"
                )
            )
        ).all()
        assert len(trail) == 1
        meta = trail[0].meta_json
        assert meta["citations"] == 0 and meta["sources"] == 0
        assert "storage" not in str(meta), "the question must never reach the trail"
        assert trail[0].correlation_id == response.headers["x-request-id"]  # type: ignore[attr-defined]

    async def test_an_answer_cites_visible_evidence_and_is_kept_as_references(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        model: RecordingModel,
    ) -> None:
        code, org_id = await recipient_in_package(client, mailbox)
        await scope(client, db_session)
        doc_id, _, _ = await seed_document(
            db_session,
            org_id,
            principal=RECIPIENT_PRINCIPAL,
            title="Storage decision",
            quote="we chose PostgreSQL",
        )
        await db_session.commit()
        model.responses = ["PostgreSQL was chosen [1]."]

        response = await ask(client, code, "What did we choose for storage?")
        body = response.json()  # type: ignore[attr-defined]
        assert response.status_code == 200, response.text  # type: ignore[attr-defined]
        assert body["answer"] == "PostgreSQL was chosen [1]."
        assert [c["marker"] for c in body["citations"]] == [1]
        assert body["citations"][0]["document_id"] == str(doc_id)
        assert body["citations"][0]["available"] is True
        assert len(body["sources"]) == 1
        assert body["sources"][0]["text"] == "we chose PostgreSQL"

        detail = (await client.get(f"/v1/kt/{code}/conversations/{body['conversation_id']}")).json()
        kept = detail["messages"][1]
        assert kept["content"] == "PostgreSQL was chosen [1]."
        assert kept["citations"][0]["document_id"] == str(doc_id)
        assert kept["citations"][0]["available"] is True
        # References only: the passage text is nowhere in the stored turn.
        assert "we chose PostgreSQL" not in str(kept["citations"])

    async def test_a_follow_up_carries_the_conversation_as_context_not_evidence(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        model: RecordingModel,
    ) -> None:
        code, org_id = await recipient_in_package(client, mailbox)
        await scope(client, db_session)
        await seed_document(
            db_session,
            org_id,
            principal=RECIPIENT_PRINCIPAL,
            title="Storage decision",
            quote="we chose PostgreSQL because of the team's experience",
        )
        await db_session.commit()
        model.responses = ["PostgreSQL was chosen [1].", "Because of the team's experience [1]."]

        first = (await ask(client, code, "What did we choose for storage?")).json()  # type: ignore[attr-defined]
        second = await ask(client, code, "Why that one?", conversation_id=first["conversation_id"])
        assert second.status_code == 200, second.text  # type: ignore[attr-defined]

        prompt = model.calls[1]
        assert prompt.startswith("Conversation so far (context only")
        assert "Recipient: What did we choose for storage?" in prompt
        assert "JUTSU: PostgreSQL was chosen [1]." in prompt
        # The numbered list still starts at [1] after the preamble.
        assert "[1] Storage decision" in prompt
        assert "Question: Why that one?" in prompt

        detail = (
            await client.get(f"/v1/kt/{code}/conversations/{first['conversation_id']}")
        ).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"] * 2

    async def test_the_window_leaves_out_documents_before_the_period(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        db_session: AsyncSession,
        model: RecordingModel,
    ) -> None:
        """A readable document from before the package's window is not evidence for the
        copilot — the window narrows, and the answer says so honestly."""
        code, org_id = await recipient_in_package(client, mailbox, period_days=30)
        await scope(client, db_session)
        await seed_document(
            db_session,
            org_id,
            principal=RECIPIENT_PRINCIPAL,
            title="Ancient decision",
            quote="we chose punch cards",
            created_at=datetime.now(tz=UTC) - timedelta(days=400),
        )
        await db_session.commit()
        model.responses = ["Punch cards [1]."]

        body = (await ask(client, code, "What did we choose?")).json()  # type: ignore[attr-defined]

        assert body["sources"] == []
        assert body["insufficient_evidence"] is True
        assert model.calls == []

    async def test_another_recipient_cannot_read_my_conversation(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        mine = (await ask(client, code, "Mine")).json()  # type: ignore[attr-defined]

        # A second package for the same leaver, claimed by a second person.
        await sign_in(client, mailbox, email=OWNER_EMAIL)
        subject = await user_id_of(client, "leaver@example.com")
        other_package = (
            await client.post(
                "/v1/kt",
                json={"subject_user_id": subject, "scope": ["documents"], "validity_days": 30},
                headers=csrf(client),
            )
        ).json()
        await invite_and_accept(client, mailbox, email="other@example.com", full_name="Other")
        other_code = str(other_package["kt_code"])
        assert (
            await client.post("/v1/kt/claim", json={"kt_code": other_code}, headers=csrf(client))
        ).status_code == 200

        stolen = await client.get(f"/v1/kt/{other_code}/conversations/{mine['conversation_id']}")
        assert stolen.status_code == 404
        assert (await client.get(f"/v1/kt/{other_code}/conversations")).json()["items"] == []

    async def test_revocation_closes_the_history(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        await ask(client, code, "Before")

        await sign_in(client, mailbox, email=OWNER_EMAIL)
        packages = (await client.get("/v1/kt")).json()["items"]
        package_id = next(p["id"] for p in packages if p["kt_code"] == code)
        assert (
            await client.post(f"/v1/kt/{package_id}/revoke", headers=csrf(client))
        ).status_code == 200

        await sign_in(client, mailbox, email=RECIPIENT_EMAIL)
        closed = await client.get(f"/v1/kt/{code}/conversations")
        assert closed.status_code == 403
        assert (
            closed.json()["error"]["message"] == "This Knowledge Transfer package has been revoked."
        )
        assert (await client.get(f"/v1/kt/{code}/workspace")).status_code == 403
        assert (await client.get(f"/v1/kt/{code}/bookmarks")).status_code == 403


class TestConversations:
    async def test_search_is_a_post_and_finds_by_content(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        await ask(client, code, "Who owns the deployment pipeline?")
        await ask(client, code, "What are the open risks?")

        found = await client.post(
            f"/v1/kt/{code}/conversations/search",
            json={"q": "deployment"},
            headers=csrf(client),
        )
        assert found.status_code == 200, found.text
        titles = [c["title"] for c in found.json()["items"]]
        assert titles == ["Who owns the deployment pipeline?"]

    async def test_archive_hides_a_conversation_and_then_it_is_gone(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        conversation_id = (await ask(client, code, "Temporary")).json()["conversation_id"]  # type: ignore[attr-defined]

        archived = await client.post(
            f"/v1/kt/{code}/conversations/{conversation_id}/archive", headers=csrf(client)
        )
        assert archived.status_code == 204
        assert (await client.get(f"/v1/kt/{code}/conversations")).json()["items"] == []
        assert (
            await client.get(f"/v1/kt/{code}/conversations/{conversation_id}")
        ).status_code == 404
        again = await client.post(
            f"/v1/kt/{code}/conversations/{conversation_id}/archive", headers=csrf(client)
        )
        assert again.status_code == 404


class TestBookmarks:
    async def test_a_visible_claim_is_saved_once_and_its_note_updates(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        code, org_id = await recipient_in_package(client, mailbox)
        await scope(client, db_session)
        _, _, claim_id = await seed_document(
            db_session,
            org_id,
            principal=RECIPIENT_PRINCIPAL,
            title="Storage decision",
            quote="we chose PostgreSQL",
        )
        await db_session.commit()

        saved = await client.post(
            f"/v1/kt/{code}/bookmarks",
            json={"kind": "claim", "ref_id": str(claim_id), "note": "ask about this"},
            headers=csrf(client),
        )
        assert saved.status_code == 201, saved.text
        body = saved.json()
        assert body["available"] is True
        assert body["tab"] == "decisions"
        assert "Storage decision" in body["label"]
        assert body["note"] == "ask about this"

        again = await client.post(
            f"/v1/kt/{code}/bookmarks",
            json={"kind": "claim", "ref_id": str(claim_id), "note": "resolved"},
            headers=csrf(client),
        )
        assert again.status_code == 201
        assert again.json()["id"] == body["id"], "a second save updates, never duplicates"
        items = (await client.get(f"/v1/kt/{code}/bookmarks")).json()["items"]
        assert len(items) == 1 and items[0]["note"] == "resolved"

    async def test_an_invisible_claim_is_the_same_404_as_no_claim(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """Saving must not become an existence oracle."""
        code, org_id = await recipient_in_package(client, mailbox)
        await scope(client, db_session)
        _, _, hidden = await seed_document(
            db_session,
            org_id,
            principal="local:somebody-else",
            title="Hidden decision",
            quote="we chose MongoDB",
        )
        await db_session.commit()

        invisible = await client.post(
            f"/v1/kt/{code}/bookmarks",
            json={"kind": "claim", "ref_id": str(hidden)},
            headers=csrf(client),
        )
        nonexistent = await client.post(
            f"/v1/kt/{code}/bookmarks",
            json={"kind": "claim", "ref_id": str(uuid.uuid4())},
            headers=csrf(client),
        )
        assert invisible.status_code == 404 and nonexistent.status_code == 404
        assert invisible.json()["error"]["message"] == nonexistent.json()["error"]["message"]

    async def test_shapes_are_validated(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        headers = csrf(client)

        question = await client.post(
            f"/v1/kt/{code}/bookmarks",
            json={"kind": "question", "note": "Who approves deploys?"},
            headers=headers,
        )
        assert question.status_code == 201
        assert question.json()["label"] == "Who approves deploys?"

        for payload in (
            {"kind": "question"},
            {"kind": "claim"},
            {"kind": "sticker", "ref_id": str(uuid.uuid4())},
        ):
            refused = await client.post(f"/v1/kt/{code}/bookmarks", json=payload, headers=headers)
            assert refused.status_code == 422, payload

    async def test_access_removed_later_renders_unavailable_not_broken(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        """The bookmark is the recipient's; the document it names is not. When the grant
        goes, the list says so instead of linking to a 404."""
        code, org_id = await recipient_in_package(client, mailbox)
        await scope(client, db_session)
        doc_id, _, _ = await seed_document(
            db_session,
            org_id,
            principal=RECIPIENT_PRINCIPAL,
            title="Runbook",
            quote="rotate the keys quarterly",
            claim_type=None,
        )
        await db_session.commit()

        saved = await client.post(
            f"/v1/kt/{code}/bookmarks",
            json={"kind": "document", "ref_id": str(doc_id)},
            headers=csrf(client),
        )
        assert saved.status_code == 201 and saved.json()["label"] == "Runbook"

        await scope(client, db_session)
        await db_session.execute(
            text("DELETE FROM document_acl WHERE document_id = :d"), {"d": doc_id}
        )
        await db_session.commit()

        items = (await client.get(f"/v1/kt/{code}/bookmarks")).json()["items"]
        assert items[0]["available"] is False
        assert items[0]["label"] == "No longer available to you"

    async def test_delete_is_final(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        saved = (
            await client.post(
                f"/v1/kt/{code}/bookmarks",
                json={"kind": "question", "note": "x"},
                headers=csrf(client),
            )
        ).json()
        gone = await client.delete(f"/v1/kt/{code}/bookmarks/{saved['id']}", headers=csrf(client))
        assert gone.status_code == 204
        again = await client.delete(f"/v1/kt/{code}/bookmarks/{saved['id']}", headers=csrf(client))
        assert again.status_code == 404


class TestProgress:
    async def test_states_and_keys_are_validated_and_the_marker_round_trips(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        key = f"claim:{uuid.uuid4()}"
        headers = csrf(client)

        done = await client.put(
            f"/v1/kt/{code}/progress/{key}", json={"state": "done"}, headers=headers
        )
        assert done.status_code == 200, done.text
        assert done.json() == {**done.json(), "item_key": key, "state": "done"}

        bogus = await client.put(
            f"/v1/kt/{code}/progress/{key}", json={"state": "finished"}, headers=headers
        )
        assert bogus.status_code == 422
        odd = await client.put(
            f"/v1/kt/{code}/progress/sticker:abc!", json={"state": "seen"}, headers=headers
        )
        assert odd.status_code == 422

        listed = (await client.get(f"/v1/kt/{code}/progress")).json()["items"]
        assert [(i["item_key"], i["state"]) for i in listed] == [(key, "done")]

        cleared = await client.delete(f"/v1/kt/{code}/progress/{key}", headers=headers)
        assert cleared.status_code == 204
        assert (await client.get(f"/v1/kt/{code}/progress")).json()["items"] == []


class TestWorkspace:
    async def test_without_readable_documents_coverage_is_honestly_unreliable(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox, scope_=["decisions", "documents"])

        workspace = (await client.get(f"/v1/kt/{code}/workspace")).json()

        coverage = workspace["coverage"]
        assert coverage["reliable"] is False
        assert coverage["extraction_ratio"] is None
        assert coverage["documents_visible"] == 0
        assert "cannot be calculated" in coverage["reason"]
        assert workspace["learning_path"] == []
        assert workspace["recommendations"] == []
        gap_keys = {g["key"] for g in workspace["gaps"]}
        assert gap_keys == {"category:decisions", "category:documents"}
        assert all(g["source"] == "evidence" for g in workspace["gaps"])
        assert workspace["resume"]["path_total"] == 0

    async def test_with_evidence_the_path_the_recommendations_and_the_gaps_follow_it(
        self, client: AsyncClient, mailbox: RecordingEmailSender, db_session: AsyncSession
    ) -> None:
        code, org_id = await recipient_in_package(
            client, mailbox, scope_=["decisions", "people", "documents"]
        )
        await scope(client, db_session)
        doc_id, _, claim_id = await seed_document(
            db_session,
            org_id,
            principal=RECIPIENT_PRINCIPAL,
            title="Storage decision",
            quote="we chose PostgreSQL",
        )
        await db_session.commit()

        workspace = (await client.get(f"/v1/kt/{code}/workspace")).json()

        coverage = workspace["coverage"]
        assert coverage["reliable"] is True
        assert coverage["extraction_ratio"] == 1.0
        assert coverage["documents_visible"] == 1 and coverage["documents_extracted"] == 1
        by_category = {c["category"]: c["claims_visible"] for c in coverage["categories"]}
        assert by_category == {"decisions": 1, "people": 0}

        stages = {s["day"]: s for s in workspace["learning_path"]}
        assert set(stages) == {3, 30}
        decision_item = stages[3]["items"][0]
        assert decision_item["key"] == f"claim:{claim_id}"
        assert decision_item["tab"] == "decisions"
        assert decision_item["state"] is None
        assert "Storage decision" in decision_item["why"]
        assert stages[30]["items"][0]["key"] == f"document:{doc_id}"

        first = workspace["recommendations"][0]
        assert first["key"] == f"claim:{claim_id}" and first["kind"] == "path"
        assert first["why"].startswith("Where your learning path starts")

        # People is in scope with nothing visible: an evidence gap, with the reason.
        people_gap = next(g for g in workspace["gaps"] if g["key"] == "category:people")
        assert people_gap["source"] == "evidence"
        assert "readable" in people_gap["why"]
        assert workspace["resume"] == {
            **workspace["resume"],
            "path_done": 0,
            "path_total": 2,
            "unclear": 0,
            "bookmarks": 0,
        }

        # Progress moves the path and the recommendation; "unclear" becomes a gap of yours.
        headers = csrf(client)
        await client.put(
            f"/v1/kt/{code}/progress/claim:{claim_id}", json={"state": "done"}, headers=headers
        )
        await client.put(
            f"/v1/kt/{code}/progress/document:{doc_id}",
            json={"state": "unclear"},
            headers=headers,
        )
        moved = (await client.get(f"/v1/kt/{code}/workspace")).json()
        assert moved["resume"]["path_done"] == 1 and moved["resume"]["unclear"] == 1
        kinds = [r["kind"] for r in moved["recommendations"]]
        assert kinds[0] == "path" and moved["recommendations"][0]["key"] == f"document:{doc_id}"
        assert "unclear" in kinds
        yours = [g for g in moved["gaps"] if g["source"] == "you"]
        assert yours and yours[0]["key"] == f"document:{doc_id}"

    async def test_resume_names_the_last_conversation_and_counts_bookmarks(
        self, client: AsyncClient, mailbox: RecordingEmailSender
    ) -> None:
        code, _ = await recipient_in_package(client, mailbox)
        await ask(client, code, "Where do I start?")
        await client.post(
            f"/v1/kt/{code}/bookmarks",
            json={"kind": "question", "note": "Who signs off releases?"},
            headers=csrf(client),
        )

        resume = (await client.get(f"/v1/kt/{code}/workspace")).json()["resume"]

        assert resume["last_conversation"]["title"] == "Where do I start?"
        assert resume["last_conversation"]["message_count"] == 2
        assert resume["bookmarks"] == 1
        assert resume["last_activity_at"] is not None
