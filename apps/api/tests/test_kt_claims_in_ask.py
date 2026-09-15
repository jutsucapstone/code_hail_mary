"""Ask KT reads a package's extracted claims beside its passages (ADR 0028).

The subject A has one claim of every type, each on a document inside the package. Around
them sit the claims that must never be read: one on A's archive outside the period, one on
a document a curator kept back, and one on the recipient's own document.

Every chunk and every question embed to one direction, so passages tie and cannot decide
anything. What these tests watch is the claim half: which claims arrive as numbered
evidence, what they cite, and what reaches the model.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.kt_search import CLAIM_LIMIT, claim_intents, query_terms
from jutsu_api.main import create_app
from jutsu_api.retrieval import get_query_embedder
from jutsu_api.routers.search import get_answer_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import configure_answers

REGISTRATION = {
    "full_name": "Ada Lovelace",
    "work_email": "ada@example.com",
    "company_name": "Example Analytical",
    "company_domain": "example.com",
    "job_title": "Head of Engineering",
    "org_size": "51-200",
    "terms_accepted": True,
}

OWNER = "ada@example.com"
SUBJECT = "a.leaver@example.com"
RECIPIENT = "c.recipient@example.com"

FULL_SCOPE = [
    "documents",
    "profile",
    "decisions",
    "people",
    "projects",
    "meetings",
    "responsibilities",
]
VECTOR = [1.0] + [0.0] * 767
VECTOR_LITERAL = "[" + ",".join(repr(value) for value in VECTOR) + "]"
K = 100

#: `(document key, claim type, claim name, summary)` for the claims a package holds.
INSIDE = (
    ("A_PROJECT", "project", "Orion migration", "A leads the Orion platform migration"),
    ("A_MEETING", "meeting", "Orion weekly sync", "Where A agreed the cutover plan"),
    ("A_PERSON", "person", "Priya Shah", "Vendor lead who worked with A on Orion"),
    ("A_OWNERSHIP", "responsibility", "Release ownership", "A owns Orion releases"),
    ("A_DECISION", "decision", "Move Orion to PostgreSQL", "The database decision A made"),
)
#: Claims that must never be read, and why.
NEVER = {
    "A_OLD decision": "outside the package period",
    "A_KEPT decision": "on a document a curator kept back",
    "C_OWN decision": "on the recipient's own document",
}


class AlignedEmbedder:
    async def embed(self, query: str) -> tuple[list[float], int]:
        return list(VECTOR), 5


class CitingAnswers:
    """Cites every numbered item the prompt carries."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        numbers = sorted({int(n) for n in re.findall(r"^\[(\d{1,3})\] ", prompt, flags=re.M)})
        if not numbers:
            return "INSUFFICIENT_EVIDENCE"
        return "Grounded " + "".join(f"[{n}]" for n in numbers) + "."


@dataclass
class World:
    http: AsyncClient
    answers: CitingAnswers
    db: AsyncSession
    mailbox: RecordingEmailSender


@dataclass
class Seeded:
    code: str
    package_id: str
    org_id: str
    subject_id: str
    documents: dict[str, str] = field(default_factory=dict)
    chunks: dict[str, str] = field(default_factory=dict)


@pytest.fixture
async def world(
    db_session: AsyncSession,
    settings: Settings,
    mailbox: RecordingEmailSender,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[World]:
    from jutsu_db.engine import dispose_engine

    await dispose_engine()
    monkeypatch.setenv("DATABASE_URL", database_url)
    configure_answers(monkeypatch)
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        yield db_session
        await db_session.commit()

    answers = CitingAnswers()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_email_sender] = lambda: mailbox
    app.dependency_overrides[get_query_embedder] = lambda: AlignedEmbedder()
    app.dependency_overrides[get_answer_transport] = lambda: answers

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as http:
        yield World(http=http, answers=answers, db=db_session, mailbox=mailbox)

    await dispose_engine()


# ---------------------------------------------------------------------------- helpers


def csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    return {CSRF_HEADER: token} if token else {}


async def sign_in(world: World, email: str) -> None:
    await world.http.post("/v1/auth/request", json={"email": email})
    delivered = world.mailbox.last.secrets
    verified = await world.http.post(
        "/v1/auth/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def ask(world: World, code: str, question: str) -> Response:
    response = await world.http.post(
        f"/v1/kt/{code}/ask", json={"question": question, "k": K}, headers=csrf(world.http)
    )
    assert response.status_code == 200, response.text
    return response


def claims_in(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [source for source in body["sources"] if source["kind"] == "claim"]


async def _document(
    db: AsyncSession,
    seeded: Seeded,
    *,
    key: str,
    source_id: uuid.UUID,
    principal: str,
    age_days: float,
    claims: list[tuple[str, str, str]],
) -> None:
    document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    passage = f"{key}: " + "; ".join(summary for _, _, summary in claims)
    await db.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:id, :org, :src, :ext, :title, :ext, 'a', :body, :body, "
            "now() - CAST(:age AS double precision) * interval '1 day')"
        ),
        {
            "id": document_id,
            "org": seeded.org_id,
            "src": source_id,
            "ext": key.lower(),
            "title": f"{key} notes",
            "body": passage,
            "age": age_days,
        },
    )
    await db.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
            "permission) VALUES (:doc, 'user', :pid, :org, 'read')"
        ),
        {"doc": document_id, "pid": principal, "org": seeded.org_id},
    )
    await db.execute(
        text(
            "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, char_end, "
            "token_count, embedding) VALUES (:id, :doc, :org, 0, :text, 0, :end, 8, "
            "CAST(:vec AS vector))"
        ),
        {
            "id": chunk_id,
            "doc": document_id,
            "org": seeded.org_id,
            "text": passage,
            "end": len(passage),
            "vec": VECTOR_LITERAL,
        },
    )
    run_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, model, "
            "finished_at, stats_json) VALUES (:id, :org, 'v1', 'h', 'test', now(), "
            "cast(:stats AS jsonb))"
        ),
        {
            "id": run_id,
            "org": seeded.org_id,
            "stats": json.dumps({"document_id": str(document_id)}),
        },
    )
    for claim_type, name, summary in claims:
        await db.execute(
            text(
                "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, claim_type, "
                "payload_json, confidence) VALUES (gen_random_uuid(), :run, :chunk, :org, "
                ":type, cast(:payload AS jsonb), 0.9)"
            ),
            {
                "run": run_id,
                "chunk": chunk_id,
                "org": seeded.org_id,
                "type": claim_type,
                "payload": json.dumps({"name": name, "summary": summary, "quote": summary}),
            },
        )
    seeded.documents[key] = str(document_id)
    seeded.chunks[key] = str(chunk_id)


async def build(
    world: World,
    *,
    scope: list[str] | None = None,
    extra_decisions: int = 0,
) -> Seeded:
    http, mailbox = world.http, world.mailbox
    await http.post("/v1/orgs/register", json=REGISTRATION)
    delivered = mailbox.last.secrets
    verified = await http.post(
        "/v1/orgs/register/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text

    tokens: list[str] = []
    for email in (SUBJECT, RECIPIENT):
        invited = await http.post(
            "/v1/employees/invitations",
            json={"email": email, "role": "member"},
            headers=csrf(http),
        )
        assert invited.status_code == 202, invited.text
        tokens.append(mailbox.last.secrets["token"])
    for token in tokens:
        accepted = await http.post(
            "/v1/invitations/accept", json={"token": token, "full_name": "Employee"}
        )
        assert accepted.status_code == 200, accepted.text
    await sign_in(world, OWNER)

    page = (await http.get("/v1/employees", params={"q": SUBJECT})).json()
    subject_id = str(page["items"][0]["id"])
    org_id = str((await http.get("/v1/orgs/current")).json()["id"])
    created = await http.post(
        "/v1/kt",
        json={
            "subject_user_id": subject_id,
            "scope": scope or FULL_SCOPE,
            "validity_days": 30,
            "period_days": 90,
            "recipient_email": RECIPIENT,
        },
        headers=csrf(http),
    )
    assert created.status_code == 201, created.text
    package = created.json()
    seeded = Seeded(
        code=str(package["kt_code"]),
        package_id=str(package["id"]),
        org_id=org_id,
        subject_id=subject_id,
    )

    db = world.db
    await db.execute(text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id})
    source_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:id, :org, 'local', '{}'::jsonb)"
        ),
        {"id": source_id, "org": org_id},
    )
    subject, recipient = f"local:{SUBJECT}", f"local:{RECIPIENT}"
    for key, claim_type, name, summary in INSIDE:
        await _document(
            db,
            seeded,
            key=key,
            source_id=source_id,
            principal=subject,
            age_days=5,
            claims=[(claim_type, name, summary)],
        )
    for key, principal, age in (
        ("A_OLD", subject, 400),
        ("A_KEPT", subject, 4),
        ("C_OWN", recipient, 3),
    ):
        await _document(
            db,
            seeded,
            key=key,
            source_id=source_id,
            principal=principal,
            age_days=age,
            claims=[("decision", f"{key} decision", f"{key} decided to rewrite Orion")],
        )
    if extra_decisions:
        await _document(
            db,
            seeded,
            key="A_MANY",
            source_id=source_id,
            principal=subject,
            age_days=2,
            claims=[
                ("decision", f"Orion decision {n}", f"Orion decision number {n}")
                for n in range(extra_decisions)
            ],
        )
    await db.commit()

    kept = await http.post(
        f"/v1/kt/{seeded.package_id}/exclusions",
        json={"document_id": seeded.documents["A_KEPT"]},
        headers=csrf(http),
    )
    assert kept.status_code == 201, kept.text

    await sign_in(world, RECIPIENT)
    claimed = await http.post("/v1/kt/claim", json={"kt_code": seeded.code}, headers=csrf(http))
    assert claimed.status_code == 200, claimed.text
    return seeded


# ------------------------------------------------------------------ reading the question


class TestReadingTheQuestion:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("What projects was A responsible for?", ["project", "responsibility"]),
            ("What meetings are important?", ["meeting"]),
            ("Who worked with A?", ["person"]),
            ("What were A's responsibilities?", ["responsibility"]),
            ("What decisions did A make?", ["decision"]),
            ("Summarise the handover notes", []),
        ],
    )
    def test_the_claim_types_a_question_asks_about(
        self, question: str, expected: list[str]
    ) -> None:
        assert claim_intents(question) == expected

    def test_terms_are_letters_and_digits_and_never_tsquery_syntax(self) -> None:
        terms = query_terms("What did A decide about 'Orion' & (PostgreSQL) | !cutover:* <-> x")

        assert terms == ["decide", "orion", "postgresql", "cutover"]
        for term in terms:
            assert re.fullmatch(r"[a-z0-9]+", term)

    def test_terms_are_bounded(self) -> None:
        question = " ".join(f"word{n}" for n in range(40))
        assert len(query_terms(question)) == 12


# ------------------------------------------------------------------------ in the answer


class TestClaimsInAskKt:
    @pytest.mark.parametrize(
        ("question", "claim_type", "name"),
        [
            ("What projects was A responsible for?", "project", "Orion migration"),
            ("What meetings are important?", "meeting", "Orion weekly sync"),
            ("Who worked with A?", "person", "Priya Shah"),
            ("What were A's responsibilities?", "responsibility", "Release ownership"),
            ("What decisions did A make?", "decision", "Move Orion to PostgreSQL"),
        ],
    )
    async def test_each_claim_type_answers_the_question_that_asks_for_it(
        self, world: World, question: str, claim_type: str, name: str
    ) -> None:
        seeded = await build(world)

        body = (await ask(world, seeded.code, question)).json()
        claims = claims_in(body)

        assert any(c["claim_type"] == claim_type and name in c["text"] for c in claims), claims
        inside = {seeded.documents[key] for key, *_ in INSIDE}
        assert {c["document_id"] for c in claims} <= inside
        for leaked in NEVER:
            assert leaked not in json.dumps(body), f"{leaked} ({NEVER[leaked]}) was read"

    async def test_a_claim_is_cited_through_the_passage_it_was_extracted_from(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = (await ask(world, seeded.code, "What decisions did A make?")).json()
        claim_citations = [c for c in body["citations"] if c["kind"] == "claim"]

        assert claim_citations, "no claim was cited"
        for citation in claim_citations:
            span = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{citation['chunk_id']}")
            assert span.status_code == 200, span.text
            assert citation["document_id"] == span.json()["document_id"]

        replay = (
            await world.http.get(f"/v1/kt/{seeded.code}/conversations/{body['conversation_id']}")
        ).json()
        kept = [c for m in replay["messages"] for c in m["citations"] if c["kind"] == "claim"]
        assert len(kept) == len(claim_citations)
        assert all(c["available"] for c in kept)

    async def test_claims_outside_the_package_never_reach_the_model(self, world: World) -> None:
        seeded = await build(world)
        world.answers.prompts.clear()

        await ask(world, seeded.code, "What decisions did A make about Orion?")
        prompt = world.answers.prompts[-1]

        assert "Move Orion to PostgreSQL" in prompt
        for leaked, why in NEVER.items():
            assert leaked not in prompt, f"{leaked} ({why}) reached the model"

    async def test_a_claim_type_outside_the_packages_categories_is_not_read(
        self, world: World
    ) -> None:
        seeded = await build(world, scope=["documents", "projects", "decisions"])

        body = (await ask(world, seeded.code, "Who worked with A?")).json()

        assert not [c for c in claims_in(body) if c["claim_type"] == "person"]
        assert "Priya Shah" not in json.dumps(body["sources"])

    async def test_the_questions_words_find_a_claim_it_names_no_type_for(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = (await ask(world, seeded.code, "Tell me about PostgreSQL")).json()

        assert any("Move Orion to PostgreSQL" in c["text"] for c in claims_in(body))

    async def test_a_question_about_nothing_structured_reads_no_claims(self, world: World) -> None:
        seeded = await build(world)

        body = (await ask(world, seeded.code, "Summarise it briefly")).json()

        assert claims_in(body) == []
        assert body["sources"], "passages still answer it"

    async def test_one_question_reads_a_bounded_number_of_claims(self, world: World) -> None:
        seeded = await build(world, extra_decisions=CLAIM_LIMIT + 8)

        body = (await ask(world, seeded.code, "What decisions did A make?")).json()

        assert len(claims_in(body)) == CLAIM_LIMIT
