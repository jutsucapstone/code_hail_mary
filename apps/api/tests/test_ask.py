"""Answer synthesis: the grounding gate, tested against deliberately misbehaving models.

Non-negotiable 3 says answers come from retrieved evidence, never model memory, with
uncited assertions retried once and then refused. A live model cannot be made to
misbehave on demand, so the fakes here do it on purpose: cite nothing, cite passages
that were never retrieved, answer fluently from nowhere. The gate must catch every one.

The unit half exercises `synthesise_answer` directly; the wire half proves the endpoint
order (config gate before budget before the paid call) and the honest 503.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.answers import synthesise_answer
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.retrieval import get_query_embedder
from jutsu_api.routers.search import get_answer_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_retrieval.search import Evidence
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


def evidence(index: int, title: str = "Design doc") -> Evidence:
    return Evidence(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=f"{title} {index}",
        source_system="local",
        text=f"Passage {index} text about the architecture.",
        char_start=0,
        char_end=40,
        score=0.9,
        occurred_at=datetime.now(tz=UTC),
    )


class ScriptedModel:
    """Answers from a script, recording every call."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append(prompt)
        return self._responses.pop(0)


class TestGroundingGate:
    async def test_a_cited_answer_passes_with_its_citations_mapped(self) -> None:
        items = [evidence(1), evidence(2)]
        model = ScriptedModel("The system uses Postgres [1] and Neo4j [2].")

        outcome = await synthesise_answer(model, question="What stores?", evidence=items)

        assert outcome.insufficient_evidence is False
        assert outcome.answer == "The system uses Postgres [1] and Neo4j [2]."
        assert [c.marker for c in outcome.citations] == [1, 2]
        assert outcome.citations[0].chunk_id == str(items[0].chunk_id)
        assert outcome.attempts == 1

    async def test_an_uncited_fluent_answer_is_retried_then_refused(self) -> None:
        """The exact defect non-negotiable 3 names: plausible, fluent, groundless."""
        model = ScriptedModel(
            "The system almost certainly uses MongoDB and Kafka.",
            "It really does use MongoDB, trust me.",
        )

        outcome = await synthesise_answer(model, question="?", evidence=[evidence(1)])

        assert outcome.insufficient_evidence is True
        assert outcome.answer is None
        assert outcome.attempts == 2
        assert len(model.calls) == 2
        assert "rejected" in model.calls[1]

    async def test_a_citation_naming_unretrieved_evidence_is_refused(self) -> None:
        """[3] with two passages is an invented source, not a formatting slip."""
        model = ScriptedModel(
            "Postgres is used [3].",
            "Postgres is used [3].",
        )

        outcome = await synthesise_answer(model, question="?", evidence=[evidence(1), evidence(2)])

        assert outcome.insufficient_evidence is True
        assert outcome.citations == []

    async def test_a_failed_first_attempt_can_be_rescued_by_the_retry(self) -> None:
        model = ScriptedModel(
            "No citations here at all.",
            "Postgres is used [1].",
        )

        outcome = await synthesise_answer(model, question="?", evidence=[evidence(1)])

        assert outcome.insufficient_evidence is False
        assert outcome.answer == "Postgres is used [1]."
        assert outcome.attempts == 2

    async def test_the_models_own_refusal_is_honoured_without_retry_gaming(self) -> None:
        model = ScriptedModel("INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE")

        outcome = await synthesise_answer(model, question="?", evidence=[evidence(1)])

        assert outcome.insufficient_evidence is True

    async def test_no_evidence_refuses_for_free(self) -> None:
        """Nothing retrieved means nothing to ground on — and no paid call to confirm it."""
        model = ScriptedModel()

        outcome = await synthesise_answer(model, question="?", evidence=[])

        assert outcome.insufficient_evidence is True
        assert outcome.attempts == 0
        assert model.calls == []

    async def test_duplicate_markers_yield_one_citation_each(self) -> None:
        model = ScriptedModel("Postgres [1] stores rows [1], and Neo4j [2] stores edges.")

        outcome = await synthesise_answer(model, question="?", evidence=[evidence(1), evidence(2)])

        assert [c.marker for c in outcome.citations] == [1, 2]


# --------------------------------------------------------------------------------------
# The endpoint, over the wire.
# --------------------------------------------------------------------------------------


class FakeEmbedder:
    async def embed(self, query: str) -> tuple[list[float], int]:
        return [0.0] * 768, 7


@pytest.fixture
async def client(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """Disposed around every test — /v1/ask spends the search budget on the cached
    global engine, and each test runs on its own event loop. The same trap
    test_search_api.py documents; the same cure."""
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
    app.dependency_overrides[get_query_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_answer_transport] = lambda: ScriptedModel("Never reached [1].")

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


class TestAskEndpoint:
    async def test_unconfigured_answers_refuse_before_spending_anything(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """503 with the honest sentence, and the search budget untouched."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        await register_owner(client, mailbox)

        response = await client.post(
            "/v1/ask", json={"question": "What is the plan?"}, headers=csrf(client)
        )
        assert response.status_code == 503
        assert "not configured" in response.json()["error"]["message"]

        # The budget gate sits AFTER the config gate: a refused ask costs no quota, so
        # search still works at full budget.
        search = await client.post("/v1/search", json={"query": "plan"}, headers=csrf(client))
        assert search.status_code == 200

    async def test_a_configured_ask_with_no_evidence_refuses_honestly(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty corpus yields insufficient_evidence, never a fluent guess."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
        await register_owner(client, mailbox)

        response = await client.post(
            "/v1/ask", json={"question": "What is the plan?"}, headers=csrf(client)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["insufficient_evidence"] is True
        assert body["answer"] is None
        assert body["citations"] == []
        assert body["sources"] == []
        # Zero model calls for zero evidence: attempts says so.
        assert body["attempts"] == 0

    async def test_ask_requires_a_session(self, client: AsyncClient) -> None:
        response = await client.post("/v1/ask", json={"question": "hi"})
        assert response.status_code == 401

    async def test_unknown_fields_are_rejected(
        self,
        client: AsyncClient,
        mailbox: RecordingEmailSender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No model override from a browser: the server chooses the model (§28)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
        await register_owner(client, mailbox)

        response = await client.post(
            "/v1/ask",
            json={"question": "hi", "model": "gpt-4"},
            headers=csrf(client),
        )
        assert response.status_code == 422
