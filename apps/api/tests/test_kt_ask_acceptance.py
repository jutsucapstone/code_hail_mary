"""The Ask KT acceptance flow: C opens A's package, asks about A, and is answered from A.

A end-to-end regression over the route the console calls, asserting the two halves the
console cannot show apart:

  * **evidence before the model.** Every question's `sources` are checked before anything
    about the answer, because "the evidence does not answer this" has two very different
    causes — nothing retrieved, and nothing groundable — and only the first is a retrieval
    defect (ADR 0025, ADR 0028).
  * **whose evidence.** Set equality against A's package, never containment: a leak is a
    document that is present, and a containment check passes with every one of them there.

Around A sit the four things that must never answer inside the package: the recipient's own
documents, A's own documents outside the package period, a third employee's, and another
tenant's granted to A's exact principal string.

The last class is the one this file exists for beyond the boundary: a package whose period
excludes a subject's documents retrieves almost nothing, and the same documents answer under
a whole-history package. That is what a thin package looks like from the inside, and it is
indistinguishable — from the console — from a broken search.
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
RIVAL_REGISTRATION = {
    "full_name": "Zed Rival",
    "work_email": "zed@rival.example",
    "company_name": "Rival Robotics",
    "company_domain": "rival.example",
    "job_title": "Head of Operations",
    "org_size": "51-200",
    "terms_accepted": True,
}

OWNER = "ada@example.com"
#: The subject of the package: the employee whose knowledge it carries.
EMPLOYEE_A = "a.leaver@example.com"
#: The recipient: the employee who opens it and asks.
EMPLOYEE_C = "c.recipient@example.com"
#: A third employee, in neither role.
EMPLOYEE_D = "d.bystander@example.com"

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
PERIOD_DAYS = 90


@dataclass(frozen=True)
class Doc:
    key: str
    title: str
    body: str
    #: Whose direct grant the document carries.
    owner: str
    #: A claim extracted from it: `(type, name, summary)`.
    claim: tuple[str, str, str] | None = None
    age_days: float = 3.0


#: A's package-authorized working life, one document per kind of thing a question asks about.
A_CORPUS = (
    Doc("A_PROJECT", "Orion migration plan", "Orion moves the ledger to PostgreSQL.", EMPLOYEE_A,
        ("project", "Orion migration", "A leads the Orion ledger migration")),
    Doc("A_DOCUMENT", "Orion design notes", "The Orion planner uses a task graph.", EMPLOYEE_A),
    Doc("A_MEETING", "Orion weekly sync transcript", "The weekly Orion sync reviewed cutover.",
        EMPLOYEE_A, ("meeting", "Orion weekly sync", "Where A reviewed the cutover plan")),
    Doc("A_RESPONSIBILITY", "Orion release duties", "A owns the Orion release train.", EMPLOYEE_A,
        ("responsibility", "Orion release ownership", "A owns Orion releases")),
    Doc("A_DECISION", "Orion database choice", "The team chose PostgreSQL for Orion.", EMPLOYEE_A,
        ("decision", "Move Orion to PostgreSQL", "The database decision A made")),
    Doc("A_EMAIL", "Re: Orion launch date", "Priya Shah confirmed the Orion launch window.",
        EMPLOYEE_A, ("person", "Priya Shah", "Vendor lead who worked with A on Orion")),
)  # fmt: skip

#: Everything that must never answer inside A's package.
OUTSIDE = (
    Doc("C_PROJECT", "Zephyr launch notes", "Zephyr is the recipient's own launch.", EMPLOYEE_C,
        ("project", "Zephyr", "The recipient's own project")),
    Doc("C_DOCUMENT", "Recipient onboarding", "The recipient's own onboarding notes.",
        EMPLOYEE_C),
    Doc("D_DOCUMENT", "Bystander private notes", "A third employee's private notes.", EMPLOYEE_D,
        ("decision", "Bystander decision", "Nothing to do with the handover")),
    # A's own, but older than the package period: inside the subject, outside the package.
    Doc("A_ARCHIVE", "Orion archive 2019", "An Orion note from before the period.", EMPLOYEE_A,
        ("project", "Orion archive", "A's own, outside the package period"), age_days=400),
)  # fmt: skip

RIVAL_TITLE = "Rival Orion clone"
RIVAL_CLAIM = "Rival Orion project"

IN_PACKAGE = {doc.title for doc in A_CORPUS}
NEVER_IN_PACKAGE = (
    *(doc.title for doc in OUTSIDE),
    "Zephyr",
    "Bystander decision",
    "Orion archive",
    RIVAL_TITLE,
    RIVAL_CLAIM,
)

#: §14's acceptance questions, and the claim each must find.
ACCEPTANCE = (
    ("What projects was A responsible for?", "Orion migration", "Orion migration plan"),
    ("What decisions did A make?", "Move Orion to PostgreSQL", "Orion database choice"),
    ("What meetings are relevant to A?", "Orion weekly sync", "Orion weekly sync transcript"),
    ("What were A's responsibilities?", "Orion release ownership", "Orion release duties"),
)


class CitingAnswers:
    """Cites every numbered item it is shown, and records the prompt it was shown."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        numbers = sorted({int(n) for n in re.findall(r"^\[(\d{1,3})\] ", prompt, flags=re.M)})
        if not numbers:
            return "INSUFFICIENT_EVIDENCE"
        return "Grounded " + "".join(f"[{n}]" for n in numbers) + "."


class AlignedEmbedder:
    async def embed(self, query: str) -> tuple[list[float], int]:
        return list(VECTOR), 5


@dataclass
class World:
    http: AsyncClient
    answers: CitingAnswers
    db: AsyncSession
    mailbox: RecordingEmailSender


@dataclass
class Seeded:
    org_id: str = ""
    code: str = ""
    package_id: str = ""
    subject_id: str = ""
    recipient_id: str = ""
    documents: dict[str, str] = field(default_factory=dict)
    chunks: dict[str, str] = field(default_factory=dict)
    rival_chunk: str = ""


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


async def register(world: World, payload: dict[str, Any]) -> None:
    await world.http.post("/v1/orgs/register", json=payload)
    delivered = world.mailbox.last.secrets
    verified = await world.http.post(
        "/v1/orgs/register/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def current_org(world: World) -> str:
    return str((await world.http.get("/v1/orgs/current")).json()["id"])


async def employee_id(world: World, email: str) -> str:
    page = (await world.http.get("/v1/employees", params={"q": email})).json()
    return str(page["items"][0]["id"])


async def ask_kt(world: World, code: str, question: str) -> Response:
    response = await world.http.post(
        f"/v1/kt/{code}/ask", json={"question": question, "k": K}, headers=csrf(world.http)
    )
    assert response.status_code == 200, response.text
    return response


def titles(sources: list[dict[str, Any]]) -> set[str]:
    return {source["document_title"] for source in sources}


def evidence_text(body: dict[str, Any]) -> str:
    return "\n".join(source["text"] for source in body["sources"])


async def scope_to(db: AsyncSession, org_id: str) -> None:
    await db.execute(text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id})


async def insert_document(
    db: AsyncSession, *, org_id: str, source_id: str, doc: Doc, principal: str
) -> tuple[str, str]:
    document_id, chunk_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:id, :org, :src, :ext, :title, :ext, 'a', :body, :body, "
            "now() - CAST(:age AS double precision) * interval '1 day')"
        ),
        {
            "id": document_id,
            "org": org_id,
            "src": source_id,
            "ext": doc.key.lower(),
            "title": doc.title,
            "body": doc.body,
            "age": doc.age_days,
        },
    )
    await db.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
            "permission) VALUES (:doc, 'user', :pid, :org, 'read')"
        ),
        {"doc": document_id, "pid": f"local:{principal}", "org": org_id},
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
            "org": org_id,
            "text": doc.body,
            "end": len(doc.body),
            "vec": VECTOR_LITERAL,
        },
    )
    if doc.claim is not None:
        claim_type, name, summary = doc.claim
        run_id = str(uuid.uuid4())
        await db.execute(
            text(
                "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, model, "
                "finished_at, stats_json) VALUES (:id, :org, 'v1', 'h', 'test', now(), "
                "cast(:stats AS jsonb))"
            ),
            {"id": run_id, "org": org_id, "stats": json.dumps({"document_id": document_id})},
        )
        await db.execute(
            text(
                "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, claim_type, "
                "payload_json, confidence) VALUES (gen_random_uuid(), :run, :chunk, :org, :type, "
                "cast(:payload AS jsonb), 0.9)"
            ),
            {
                "run": run_id,
                "chunk": chunk_id,
                "org": org_id,
                "type": claim_type,
                "payload": json.dumps({"name": name, "summary": summary, "quote": doc.body[:24]}),
            },
        )
    return document_id, chunk_id


async def source_for(db: AsyncSession, org_id: str) -> str:
    source_id = str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:id, :org, 'local', '{}'::jsonb)"
        ),
        {"id": source_id, "org": org_id},
    )
    return source_id


async def build(world: World, *, whole_history: bool = False) -> Seeded:
    """The organisation, its three employees, A's corpus, and one package A → C."""
    http, mailbox, db = world.http, world.mailbox, world.db
    seeded = Seeded()

    await register(world, REGISTRATION)
    tokens: list[str] = []
    for email in (EMPLOYEE_A, EMPLOYEE_C, EMPLOYEE_D):
        invited = await http.post(
            "/v1/employees/invitations", json={"email": email, "role": "member"}, headers=csrf(http)
        )
        assert invited.status_code == 202, invited.text
        tokens.append(mailbox.last.secrets["token"])
    for token in tokens:
        accepted = await http.post(
            "/v1/invitations/accept", json={"token": token, "full_name": "Employee"}
        )
        assert accepted.status_code == 200, accepted.text
    await sign_in(world, OWNER)
    seeded.org_id = await current_org(world)
    seeded.subject_id = await employee_id(world, EMPLOYEE_A)
    seeded.recipient_id = await employee_id(world, EMPLOYEE_C)

    await scope_to(db, seeded.org_id)
    source_id = await source_for(db, seeded.org_id)
    for doc in (*A_CORPUS, *OUTSIDE):
        seeded.documents[doc.key], seeded.chunks[doc.key] = await insert_document(
            db, org_id=seeded.org_id, source_id=source_id, doc=doc, principal=doc.owner
        )
    await db.commit()

    period: dict[str, Any] = (
        {"whole_history": True} if whole_history else {"period_days": PERIOD_DAYS}
    )
    created = await http.post(
        "/v1/kt",
        json={
            "subject_user_id": seeded.subject_id,
            "scope": FULL_SCOPE,
            "validity_days": 30,
            "recipient_email": EMPLOYEE_C,
            **period,
        },
        headers=csrf(http),
    )
    assert created.status_code == 201, created.text
    seeded.code, seeded.package_id = str(created.json()["kt_code"]), str(created.json()["id"])

    # Another tenant, granting a document to A's exact principal string.
    await register(world, RIVAL_REGISTRATION)
    rival_org = await current_org(world)
    await scope_to(db, rival_org)
    rival_source = await source_for(db, rival_org)
    rival = Doc("RIVAL", RIVAL_TITLE, "A rival clone of Orion.", EMPLOYEE_A,
                ("project", RIVAL_CLAIM, "Another tenant's project"))  # fmt: skip
    _rival_doc, seeded.rival_chunk = await insert_document(
        db, org_id=rival_org, source_id=rival_source, doc=rival, principal=EMPLOYEE_A
    )
    await db.commit()

    await sign_in(world, EMPLOYEE_C)
    claimed = await http.post("/v1/kt/claim", json={"kt_code": seeded.code}, headers=csrf(http))
    assert claimed.status_code == 200, claimed.text
    world.answers.prompts.clear()
    return seeded


# ------------------------------------------------------------------- the acceptance flow


class TestTheRecipientAsksAboutTheSubject:
    @pytest.mark.parametrize(("question", "claim", "document"), ACCEPTANCE)
    async def test_each_acceptance_question_is_answered_from_as_package(
        self, world: World, question: str, claim: str, document: str
    ) -> None:
        seeded = await build(world)

        body = (await ask_kt(world, seeded.code, question)).json()

        # Evidence FIRST, before anything about the answer: retrieval is what this proves.
        assert body["sources"], "no evidence was retrieved inside the package"
        assert claim in evidence_text(body), f"{claim!r} did not reach the evidence"
        assert document in titles(body["sources"])
        assert titles(body["sources"]) <= IN_PACKAGE, "evidence came from outside the package"
        # Then the answer the console renders, and its citations.
        assert body["insufficient_evidence"] is False
        assert body["answer"]
        assert body["citations"], "an answer with no citation"
        cited = {c["document_id"] for c in body["citations"]}
        assert cited <= set(seeded.documents[doc.key] for doc in A_CORPUS)

    async def test_the_whole_package_answers_and_nothing_outside_it_does(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = (await ask_kt(world, seeded.code, "Summarise everything about Orion")).json()

        # Set equality: a leak is a document that is present.
        assert titles(body["sources"]) == IN_PACKAGE
        prompt, response = world.answers.prompts[-1], json.dumps(body)
        for forbidden in NEVER_IN_PACKAGE:
            assert forbidden not in prompt, f"{forbidden!r} reached the model"
            assert forbidden not in response, f"{forbidden!r} reached the recipient"
        assert seeded.chunks["A_ARCHIVE"] not in response

    async def test_every_citation_opens_through_the_packages_own_door(self, world: World) -> None:
        seeded = await build(world)

        body = (await ask_kt(world, seeded.code, "What decisions did A make?")).json()

        assert body["citations"]
        for citation in body["citations"]:
            inside = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{citation['chunk_id']}")
            assert inside.status_code == 200, inside.text
            # The recipient's own door does not open a subject chunk: the package is the
            # only authorization for it, and it is not a grant to C.
            outside = await world.http.get(f"/v1/evidence/{citation['chunk_id']}")
            assert outside.status_code == 404

    async def test_a_chunk_outside_the_package_is_absent_from_both_doors(
        self, world: World
    ) -> None:
        seeded = await build(world)

        for key in ("C_PROJECT", "D_DOCUMENT", "A_ARCHIVE"):
            through_package = await world.http.get(
                f"/v1/kt/{seeded.code}/evidence/{seeded.chunks[key]}"
            )
            assert through_package.status_code == 404, key
        rival = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{seeded.rival_chunk}")
        assert rival.status_code == 404


class TestNormalCitedQaIsUnchanged:
    async def test_the_recipients_own_ask_reads_their_own_documents_not_the_subjects(
        self, world: World
    ) -> None:
        await build(world)

        own = await world.http.post(
            "/v1/ask", json={"question": "Summarise everything about Orion", "k": K},
            headers=csrf(world.http),
        )  # fmt: skip

        assert own.status_code == 200, own.text
        body = own.json()
        assert titles(body["sources"]) == {"Zephyr launch notes", "Recipient onboarding"}
        assert titles(body["sources"]).isdisjoint(IN_PACKAGE)


class TestWhatAThinPackageLooksLike:
    """The production shape of "Ask KT knows nothing": retrieval is correct and the package
    is nearly empty, because the subject's documents fall outside its period.

    Measured in production on 2026-09-20/21 for one package: `vector_search
    authorization=subject returned=6 … exhausted=True` while another package of the same
    tenant returned a full page of 30. A period is the difference between them, and the
    console cannot show it, which is why it is pinned here.
    """

    async def test_a_document_outside_the_period_is_not_retrieved(self, world: World) -> None:
        seeded = await build(world)

        body = (await ask_kt(world, seeded.code, "What projects was A responsible for?")).json()

        assert "Orion archive" not in evidence_text(body)
        assert "Orion archive 2019" not in titles(body["sources"])

    async def test_whole_history_retrieves_the_same_document(self, world: World) -> None:
        seeded = await build(world, whole_history=True)

        body = (await ask_kt(world, seeded.code, "What projects was A responsible for?")).json()

        assert "Orion archive 2019" in titles(body["sources"])
        assert "Orion archive" in evidence_text(body)
        # Still A's own: the wider period widens the subject's own documents and nothing else.
        assert titles(body["sources"]) <= IN_PACKAGE | {"Orion archive 2019"}
        assert "Zephyr" not in json.dumps(body)
