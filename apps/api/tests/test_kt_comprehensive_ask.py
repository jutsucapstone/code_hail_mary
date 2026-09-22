"""A handover question reads every category the package covers (ADR 0031).

The reported defect: C opens A's package, asks "what should I understand first?", and is
told the evidence does not answer it — while the Decisions, Projects, People and
Responsibilities tabs one click away list exactly what C asked for. Such a question names
no claim type and shares no claim's words, so the type-and-terms rule read **no claims at
all** and the answer stood on passages alone.

What these tests hold to:

* a question about the handover as a whole reads every category the package covers, and
  one crowded category cannot spend the whole allowance;
* a question about one category reads exactly what it always did, bounded as before;
* a category with no evidence is *named*, not turned into a refusal of the whole question;
* nothing is invented — the citation gate is untouched, and an uncited or mis-cited answer
  is still discarded;
* breadth changes ranking and nothing else: A's archive, C's own corpus, D's documents and
  another tenant's identical document stay out, and a revoked, expired or wrongly addressed
  package refuses a broad question exactly as it refuses a narrow one.

A's corpus is deliberately lopsided — twenty project claims, twenty decision claims, one
responsibility, one person, no meeting — because that is the shape that makes a single
global ordering look correct while quietly answering a five-part question with one part.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from jutsu_api.answers import _SYSTEM
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender
from jutsu_api.email import RecordingEmailSender
from jutsu_api.kt_search import CLAIM_LIMIT, COMPREHENSIVE_CLAIM_LIMIT, comprehensive
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
#: The subject: whose knowledge the package carries.
EMPLOYEE_A = "a.leaver@example.com"
#: The recipient: who opens it and asks.
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

#: How many claims one category holds, to make crowding visible. Comfortably above both
#: bounds, so a single ordering over everything would return this category and no other.
CROWD = 20

#: The question the reported defect was asked with, and its siblings.
HANDOVER_QUESTION = "What should I understand first?"
NARROW_QUESTION = "What decisions did A take?"


@dataclass(frozen=True)
class Doc:
    key: str
    title: str
    body: str
    #: Whose direct grant the document carries — an email, namespaced `local:` on the way in.
    owner: str
    #: `(type, name, summary)` per claim extracted from it.
    claims: tuple[tuple[str, str, str], ...] = ()
    age_days: float = 3.0
    #: A Knowledge Basket upload rather than a connector document.
    basket: bool = False


def _many(claim_type: str, name: str) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (claim_type, f"{name} {n}", f"Orion {claim_type} number {n}") for n in range(CROWD)
    )


#: A's package-authorized working life. Lopsided on purpose; no meeting anywhere.
A_CORPUS = (
    Doc("A_PROJECTS", "Orion programme board", "The Orion programme spans four teams.",
        EMPLOYEE_A, _many("project", "Orion workstream")),
    Doc("A_DECISIONS", "Orion decision log", "The Orion log records what was settled.",
        EMPLOYEE_A, _many("decision", "Orion ruling")),
    Doc("A_RESPONSIBILITY", "Orion release duties", "A owns the Orion release train.",
        EMPLOYEE_A, (("responsibility", "Orion release ownership", "A owns Orion releases"),)),
    Doc("A_PERSON", "Re: Orion launch window", "Priya Shah confirmed the Orion window.",
        EMPLOYEE_A, (("person", "Priya Shah", "Vendor lead alongside A on Orion"),)),
    Doc("A_PLAIN", "Orion design notes", "The Orion planner uses a task graph.", EMPLOYEE_A),
    Doc("A_BASKET", "Orion handover upload.txt", "The Orion runbook A uploaded on leaving.",
        EMPLOYEE_A, basket=True),
)  # fmt: skip

#: Everything that must never answer inside A's package, and why it is here.
OUTSIDE = (
    Doc("A_ARCHIVE", "Orion archive 2019", "An Orion note from before the period.", EMPLOYEE_A,
        (("project", "Orion archive", "A's own, outside the package period"),), age_days=400),
    Doc("C_OWN", "Zephyr launch notes", "Zephyr is the recipient's own launch.", EMPLOYEE_C,
        (("decision", "Zephyr cutover", "The recipient's own"),)),
    Doc("D_OWN", "Bystander private notes", "A third employee's private notes.", EMPLOYEE_D,
        (("decision", "Bystander ruling", "Nothing to do with the handover"),)),
)  # fmt: skip

RIVAL_TITLE = "Rival Orion clone"
RIVAL_CLAIM = "Rival Orion programme"

NEVER_IN_PACKAGE = (
    *(doc.title for doc in OUTSIDE),
    "Orion archive",
    "Zephyr cutover",
    "Bystander ruling",
    RIVAL_TITLE,
    RIVAL_CLAIM,
)


class ScriptedAnswers:
    """Records what it was asked and replies from a script, or cites everything it saw."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.replies: list[str] = []

    @property
    def prompts(self) -> list[str]:
        return [prompt for _, prompt in self.calls]

    @property
    def systems(self) -> list[str]:
        return [system for system, _ in self.calls]

    async def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self.replies:
            return self.replies.pop(0)
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
    answers: ScriptedAnswers
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

    answers = ScriptedAnswers()
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
    return await world.http.post(
        f"/v1/kt/{code}/ask", json={"question": question, "k": K}, headers=csrf(world.http)
    )


async def answered(world: World, code: str, question: str) -> dict[str, Any]:
    response = await ask_kt(world, code, question)
    assert response.status_code == 200, response.text
    return dict(response.json())


def claims_of(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [source for source in body["sources"] if source["kind"] == "claim"]


def categories_of(body: dict[str, Any]) -> set[str]:
    return {claim["claim_type"] for claim in claims_of(body)}


async def scope_to(db: AsyncSession, org_id: str) -> None:
    await db.execute(text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id})


async def source_for(db: AsyncSession, org_id: str, system: str) -> str:
    source_id = str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:id, :org, CAST(:system AS source_system), '{}'::jsonb)"
        ),
        {"id": source_id, "org": org_id, "system": system},
    )
    return source_id


async def insert_document(
    db: AsyncSession, *, org_id: str, source_id: str, doc: Doc, principal: str, external_id: str
) -> tuple[str, str]:
    document_id, chunk_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at) "
            "VALUES (:id, :org, :src, :ext, :title, :hash, 'a', :body, :body, "
            "now() - CAST(:age AS double precision) * interval '1 day')"
        ),
        {
            "id": document_id,
            "org": org_id,
            "src": source_id,
            "ext": external_id,
            "hash": f"{doc.key.lower()}-hash",
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
        {"doc": document_id, "pid": principal, "org": org_id},
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
    if doc.claims:
        run_id = str(uuid.uuid4())
        await db.execute(
            text(
                "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, model, "
                "finished_at, stats_json) VALUES (:id, :org, 'v1', 'h', 'test', now(), "
                "cast(:stats AS jsonb))"
            ),
            {"id": run_id, "org": org_id, "stats": json.dumps({"document_id": document_id})},
        )
        for claim_type, name, summary in doc.claims:
            await db.execute(
                text(
                    "INSERT INTO extraction_claims (id, run_id, chunk_id, org_id, claim_type, "
                    "payload_json, confidence) VALUES (gen_random_uuid(), :run, :chunk, :org, "
                    ":type, cast(:payload AS jsonb), 0.9)"
                ),
                {
                    "run": run_id,
                    "chunk": chunk_id,
                    "org": org_id,
                    "type": claim_type,
                    "payload": json.dumps(
                        {"name": name, "summary": summary, "quote": doc.body[:24]}
                    ),
                },
            )
    return document_id, chunk_id


async def attach_basket_file(db: AsyncSession, seeded: Seeded, *, source_id: str, doc: Doc) -> None:
    """A's upload, ingested and attached to this package — inside it, and exempt from the
    period (ADR 0027). Its grant is the `basket:` principal an upload links for its owner."""
    file_id = str(uuid.uuid4())
    document_id, chunk_id = await insert_document(
        db,
        org_id=seeded.org_id,
        source_id=source_id,
        doc=doc,
        principal=f"basket:{seeded.subject_id}",
        external_id=file_id,
    )
    seeded.documents[doc.key], seeded.chunks[doc.key] = document_id, chunk_id
    await db.execute(
        text(
            "INSERT INTO basket_files (id, org_id, owner_user_id, object_key, original_filename, "
            "normalised_filename, declared_mime, size_bytes, state, document_id, extracted_chars) "
            "VALUES (:id, :org, :owner, :key, :name, :norm, 'text/plain', 64, 'ready', :doc, 64)"
        ),
        {
            "id": file_id,
            "org": seeded.org_id,
            "owner": seeded.subject_id,
            "key": f"org/{seeded.org_id}/basket/{file_id}",
            "name": doc.title,
            "norm": doc.title.lower(),
            "doc": document_id,
        },
    )
    await db.execute(
        text(
            "INSERT INTO kt_package_files (id, org_id, package_id, basket_file_id, attached_by) "
            "VALUES (:id, :org, :pkg, :file, :by)"
        ),
        {
            "id": str(uuid.uuid4()),
            "org": seeded.org_id,
            "pkg": seeded.package_id,
            "file": file_id,
            "by": seeded.subject_id,
        },
    )


async def build(world: World) -> Seeded:
    """The organisation, three employees, A's lopsided corpus, one package A → C, and a
    second tenant holding an identical document granted to A's exact principal string."""
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

    created = await http.post(
        "/v1/kt",
        json={
            "subject_user_id": seeded.subject_id,
            "scope": FULL_SCOPE,
            "validity_days": 30,
            "period_days": PERIOD_DAYS,
            "recipient_email": EMPLOYEE_C,
        },
        headers=csrf(http),
    )
    assert created.status_code == 201, created.text
    seeded.code, seeded.package_id = str(created.json()["kt_code"]), str(created.json()["id"])

    await scope_to(db, seeded.org_id)
    local = await source_for(db, seeded.org_id, "local")
    basket = await source_for(db, seeded.org_id, "basket")
    # The principal an upload links for its owner (`identities.link_basket_principal`).
    await db.execute(
        text(
            "INSERT INTO source_identities (org_id, user_id, source_system, subject, linked_by) "
            "VALUES (:org, :user, CAST('basket' AS source_system), :subject, 'basket_upload')"
        ),
        {"org": seeded.org_id, "user": seeded.subject_id, "subject": seeded.subject_id},
    )
    for doc in (*A_CORPUS, *OUTSIDE):
        if doc.basket:
            await attach_basket_file(db, seeded, source_id=basket, doc=doc)
            continue
        seeded.documents[doc.key], seeded.chunks[doc.key] = await insert_document(
            db,
            org_id=seeded.org_id,
            source_id=local,
            doc=doc,
            principal=f"local:{doc.owner}",
            external_id=doc.key.lower(),
        )
    await db.commit()

    # A second tenant, granting its document to A's exact principal string.
    await register(world, RIVAL_REGISTRATION)
    rival_org = await current_org(world)
    await scope_to(db, rival_org)
    rival_source = await source_for(db, rival_org, "local")
    rival = Doc(
        "RIVAL",
        RIVAL_TITLE,
        "A rival clone of Orion.",
        EMPLOYEE_A,
        (("project", RIVAL_CLAIM, "Another tenant's programme"),),
    )
    await insert_document(
        db,
        org_id=rival_org,
        source_id=rival_source,
        doc=rival,
        principal=f"local:{EMPLOYEE_A}",
        external_id="rival",
    )
    await db.commit()

    await sign_in(world, EMPLOYEE_C)
    claimed = await http.post("/v1/kt/claim", json={"kt_code": seeded.code}, headers=csrf(http))
    assert claimed.status_code == 200, claimed.text
    world.answers.calls.clear()
    return seeded


# ------------------------------------------------------------- reading a broad question


class TestWhatCountsAsAHandoverQuestion:
    @pytest.mark.parametrize(
        "question",
        [
            "What should I understand first?",
            "What should I know about A?",
            "Tell me everything about A's work",
            "Give me an overview of A's responsibilities",
            "Get me up to speed on A's work",
            "Can you walk me through the handover?",
            "What do I need to know?",
        ],
    )
    def test_a_question_about_the_handover_as_a_whole(self, question: str) -> None:
        assert comprehensive(question)

    @pytest.mark.parametrize(
        "question",
        [
            "What decisions did A take?",
            "Who is Priya Shah?",
            "Summarise it briefly",
            "Which database did the team choose?",
        ],
    )
    def test_a_question_about_one_thing(self, question: str) -> None:
        assert not comprehensive(question)


# ------------------------------------------------------------ what a broad question reads


class TestABroadQuestionReadsEveryCategory:
    async def test_it_reads_every_category_the_package_can_answer(self, world: World) -> None:
        seeded = await build(world)

        body = await answered(world, seeded.code, HANDOVER_QUESTION)

        # Four of the five: A has no meeting claim anywhere, which is the gap below.
        assert categories_of(body) == {"project", "decision", "responsibility", "person"}
        assert body["answer"], body
        assert not body["insufficient_evidence"]

    async def test_one_crowded_category_cannot_spend_the_whole_allowance(
        self, world: World
    ) -> None:
        seeded = await build(world)

        claims = claims_of(await answered(world, seeded.code, HANDOVER_QUESTION))

        counts = Counter(claim["claim_type"] for claim in claims)
        # Twenty projects and twenty decisions are available. Neither takes the lot, and
        # the two categories holding a single claim are still represented.
        assert counts["responsibility"] == 1, counts
        assert counts["person"] == 1, counts
        assert max(counts.values()) < CROWD, counts
        assert len(claims) <= COMPREHENSIVE_CLAIM_LIMIT, counts

    async def test_passages_claims_and_an_attached_upload_all_reach_the_model(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = await answered(world, seeded.code, HANDOVER_QUESTION)
        prompt = world.answers.prompts[-1]

        kinds = {source["kind"] for source in body["sources"]}
        assert "passage" in kinds and "claim" in kinds
        # The Knowledge Basket file A attached is inside the package and answers with it.
        assert "Orion handover upload.txt" in prompt
        assert "The Orion runbook A uploaded on leaving." in prompt

    async def test_the_categories_it_read_are_logged_as_counts(
        self, world: World, caplog: pytest.LogCaptureFixture
    ) -> None:
        seeded = await build(world)
        caplog.set_level("INFO", logger="jutsu.api.kt")

        await answered(world, seeded.code, HANDOVER_QUESTION)

        completed = [
            record.args
            for record in caplog.records
            if isinstance(record.args, dict) and record.args.get("event") == "kt_search_completed"
        ]
        assert completed, "no kt_search_completed line"
        assert completed[-1]["comprehensive"] is True
        assert completed[-1]["claim_types"] == 4


# ---------------------------------------------------------- what a narrow question reads


class TestANarrowQuestionIsUnchanged:
    async def test_it_reads_the_category_it_names(self, world: World) -> None:
        seeded = await build(world)

        body = await answered(world, seeded.code, NARROW_QUESTION)

        assert categories_of(body) == {"decision"}

    async def test_it_is_still_bounded_by_the_narrow_limit(self, world: World) -> None:
        seeded = await build(world)

        claims = claims_of(await answered(world, seeded.code, NARROW_QUESTION))

        assert len(claims) == CLAIM_LIMIT

    async def test_a_question_about_nothing_structured_still_reads_no_claims(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = await answered(world, seeded.code, "Summarise it briefly")

        assert claims_of(body) == []
        assert body["sources"], "passages still answer it"


# ------------------------------------------------------------------- a gap, not a refusal


class TestAGapIsNamedRatherThanRefused:
    async def test_the_model_is_asked_to_name_gaps_instead_of_refusing_everything(
        self, world: World
    ) -> None:
        seeded = await build(world)

        await answered(world, seeded.code, HANDOVER_QUESTION)

        system = world.answers.systems[-1]
        assert "Name the gaps instead of refusing everything" in system
        assert "only when NOT ONE part of the question is supported" in system
        # Added to the gate's own rules, never in place of them.
        assert system.startswith(_SYSTEM)

    async def test_an_answer_that_names_a_gap_is_an_answer(self, world: World) -> None:
        seeded = await build(world)
        world.answers.replies = [
            "Projects: A led the Orion programme [1].\n"
            "Meetings: no evidence in this package establishes this."
        ]

        body = await answered(world, seeded.code, HANDOVER_QUESTION)

        assert not body["insufficient_evidence"]
        assert body["attempts"] == 1, "a grounded partial answer must not cost a retry"
        assert "no evidence in this package establishes this" in body["answer"]
        assert [c["marker"] for c in body["citations"]] == [1]

    async def test_nothing_supported_is_still_a_refusal(self, world: World) -> None:
        seeded = await build(world)
        world.answers.replies = ["INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"]

        body = await answered(world, seeded.code, HANDOVER_QUESTION)

        assert body["insufficient_evidence"]
        assert body["answer"] is None
        assert body["citations"] == []


# ------------------------------------------------------------------ nothing is invented


class TestBreadthInventsNothing:
    async def test_an_uncited_answer_is_discarded(self, world: World) -> None:
        seeded = await build(world)
        fluent = "A ran the Orion programme, owned releases and chose PostgreSQL."
        world.answers.replies = [fluent, fluent]

        body = await answered(world, seeded.code, HANDOVER_QUESTION)

        assert body["insufficient_evidence"]
        assert body["answer"] is None

    async def test_a_citation_naming_evidence_that_was_never_retrieved_is_discarded(
        self, world: World
    ) -> None:
        seeded = await build(world)
        world.answers.replies = ["A owned everything [999].", "A owned everything [999]."]

        body = await answered(world, seeded.code, HANDOVER_QUESTION)

        assert body["insufficient_evidence"]
        assert body["answer"] is None

    async def test_every_citation_indexes_a_source_that_was_retrieved(self, world: World) -> None:
        seeded = await build(world)

        body = await answered(world, seeded.code, HANDOVER_QUESTION)

        sources = body["sources"]
        assert body["citations"], "nothing was cited"
        for citation in body["citations"]:
            source = sources[citation["marker"] - 1]
            assert citation["chunk_id"] == source["chunk_id"]
            assert citation["document_id"] == source["document_id"]


# -------------------------------------------------------- the boundary breadth cannot move


class TestTheBoundaryIsUnchangedByBreadth:
    async def test_a_broad_question_reads_nothing_outside_the_package(self, world: World) -> None:
        seeded = await build(world)

        body = await answered(world, seeded.code, HANDOVER_QUESTION)
        rendered = json.dumps(body) + "\n".join(world.answers.prompts)

        for leaked in NEVER_IN_PACKAGE:
            assert leaked not in rendered, f"{leaked} reached the recipient"
        inside = {doc.title for doc in A_CORPUS}
        assert {source["document_title"] for source in body["sources"]} <= inside

    async def test_every_citation_opens_through_the_packages_own_door(self, world: World) -> None:
        seeded = await build(world)

        body = await answered(world, seeded.code, HANDOVER_QUESTION)

        for citation in body["citations"]:
            opened = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{citation['chunk_id']}")
            assert opened.status_code == 200, opened.text
            assert opened.json()["document_id"] == citation["document_id"]

    async def test_another_employee_cannot_ask_a_broad_question_of_this_package(
        self, world: World
    ) -> None:
        seeded = await build(world)
        await sign_in(world, EMPLOYEE_D)

        refused = await ask_kt(world, seeded.code, HANDOVER_QUESTION)

        assert refused.status_code == 404, refused.text
        assert "Orion" not in refused.text

    async def test_a_revoked_package_refuses_a_broad_question(self, world: World) -> None:
        seeded = await build(world)
        await sign_in(world, OWNER)
        revoked = await world.http.post(
            f"/v1/kt/{seeded.package_id}/revoke", headers=csrf(world.http)
        )
        assert revoked.status_code == 200, revoked.text
        await sign_in(world, EMPLOYEE_C)

        refused = await ask_kt(world, seeded.code, HANDOVER_QUESTION)

        assert refused.status_code == 403, refused.text
        assert "Orion" not in refused.text

    async def test_an_expired_package_refuses_a_broad_question(self, world: World) -> None:
        seeded = await build(world)
        # The build left the session scoped to the second tenant; `kt_packages` is under
        # RLS, so an UPDATE from there would match nothing and quietly change no row.
        await scope_to(world.db, seeded.org_id)
        await world.db.execute(
            text("UPDATE kt_packages SET expires_at = now() - interval '1 day' WHERE id = :id"),
            {"id": seeded.package_id},
        )
        await world.db.commit()

        refused = await ask_kt(world, seeded.code, HANDOVER_QUESTION)

        assert refused.status_code == 403, refused.text
        assert "Orion" not in refused.text


# --------------------------------------------------------- the recipient's own Cited Q&A


class TestNormalCitedQaIsUnchanged:
    async def test_the_recipients_own_ask_reads_their_own_documents(self, world: World) -> None:
        await build(world)

        response = await world.http.post(
            "/v1/ask",
            json={"question": "What did I launch?", "k": K},
            headers=csrf(world.http),
        )

        assert response.status_code == 200, response.text
        titles = {source["document_title"] for source in response.json()["sources"]}
        assert titles == {"Zephyr launch notes"}, titles

    async def test_the_ask_prompt_carries_no_handover_rules(self, world: World) -> None:
        await build(world)
        world.answers.calls.clear()

        response = await world.http.post(
            "/v1/ask",
            json={"question": "What did I launch?", "k": K},
            headers=csrf(world.http),
        )

        assert response.status_code == 200, response.text
        assert world.answers.systems, "no model call was made"
        for system in world.answers.systems:
            assert system == _SYSTEM, "Cited Q&A composed a different system prompt"
