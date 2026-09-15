"""Folders are searchable exactly where the documents in them already are (ADR 0029).

One question — where are the Astro Agent documents kept? — and six folders whose names all
match it:

    Projects/Astro Agent              A's, inside A's knowledge-transfer package
    Archive/Astro Agent 2019          A's, outside the package period
    Projects/Astro Agent Contracts    A's, kept back by the curator
    Recipient/Astro Agent Onboarding  C's own
    Finance/Astro Agent Budget        a colleague's, shared with neither A nor C
    Rival/Astro Agent Secret          another tenant's, granted there to C's principal string

Ask JUTSU answers C from C's own reach and Ask KT from A's package. No folder reaches either
through a document the asker could not already open, and no folder carries a document's text.
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
from jutsu_retrieval.folders import FOLDERS_STATEMENT, SUBJECT_FOLDERS_STATEMENT
from jutsu_retrieval.terms import folder_words
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

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
SUBJECT = "a.leaver@example.com"
RECIPIENT = "c.recipient@example.com"
COLLEAGUE = "colleague@example.com"

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

#: `document key -> (folder, whose document, age in days)`, in the asker's organisation.
DOCUMENTS = {
    "A_ASTRO": ("Projects/Astro Agent", SUBJECT, 5),
    "A_OLD": ("Archive/Astro Agent 2019", SUBJECT, 400),
    "A_KEPT": ("Projects/Astro Agent Contracts", SUBJECT, 4),
    "C_OWN": ("Recipient/Astro Agent Onboarding", RECIPIENT, 3),
    "COLLEAGUE": ("Finance/Astro Agent Budget", COLLEAGUE, 2),
}
#: In another organisation, granted to the same principal string C holds in this one.
RIVAL = ("Rival/Astro Agent Secret", RECIPIENT, 1)

EVERY_FOLDER = {folder for folder, _, _ in DOCUMENTS.values()} | {RIVAL[0]}
FOR_ASK_JUTSU = {"Recipient/Astro Agent Onboarding"}
FOR_ASK_KT = {"Projects/Astro Agent"}

QUESTION = "Where are the Astro Agent documents stored?"
KT_QUESTION = "Where are A's Astro Agent documents stored?"


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
    documents: dict[str, str] = field(default_factory=dict)


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
    """A new organisation, signed in as its owner."""
    await world.http.post("/v1/orgs/register", json=payload)
    delivered = world.mailbox.last.secrets
    verified = await world.http.post(
        "/v1/orgs/register/verify", json={"token": delivered["token"], "code": delivered["code"]}
    )
    assert verified.status_code == 200, verified.text


async def current_org(world: World) -> str:
    return str((await world.http.get("/v1/orgs/current")).json()["id"])


async def ask_jutsu(world: World, question: str) -> Response:
    response = await world.http.post(
        "/v1/ask", json={"question": question, "k": K}, headers=csrf(world.http)
    )
    assert response.status_code == 200, response.text
    return response


async def ask_kt(world: World, code: str, question: str) -> Response:
    response = await world.http.post(
        f"/v1/kt/{code}/ask", json={"question": question, "k": K}, headers=csrf(world.http)
    )
    assert response.status_code == 200, response.text
    return response


def folders_in(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [source for source in body["sources"] if source["kind"] == "folder"]


async def _source(db: AsyncSession, org_id: str) -> uuid.UUID:
    await db.execute(text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id})
    source_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO sources (id, org_id, system, config_json) "
            "VALUES (:id, :org, 'local', '{}'::jsonb)"
        ),
        {"id": source_id, "org": org_id},
    )
    return source_id


async def _document(
    db: AsyncSession,
    *,
    org_id: str,
    source_id: uuid.UUID,
    key: str,
    folder: str,
    owner: str,
    age_days: float,
) -> str:
    """One document kept in `folder`, readable by `owner` alone, with one chunk and the
    folder words the pipeline would have recorded for it."""
    document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    body = f"{key} passage: the confidential {key.lower()} body"
    await db.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at, folder_path) "
            "VALUES (:id, :org, :src, :ext, :title, :ext, 'a', :body, :body, "
            "now() - CAST(:age AS double precision) * interval '1 day', :folder)"
        ),
        {
            "id": document_id,
            "org": org_id,
            "src": source_id,
            "ext": key.lower(),
            "title": f"{key} notes",
            "body": body,
            "age": age_days,
            "folder": folder,
        },
    )
    await db.execute(
        text(
            "INSERT INTO document_folder_words (org_id, document_id, word) "
            "SELECT CAST(:org AS uuid), CAST(:doc AS uuid), unnest(CAST(:words AS text[]))"
        ),
        {"org": org_id, "doc": str(document_id), "words": folder_words(folder)},
    )
    await db.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
            "permission) VALUES (:doc, 'user', :pid, :org, 'read')"
        ),
        {"doc": document_id, "pid": f"local:{owner}", "org": org_id},
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
            "text": body,
            "end": len(body),
            "vec": VECTOR_LITERAL,
        },
    )
    return str(document_id)


async def build(world: World) -> Seeded:
    http, mailbox, db = world.http, world.mailbox, world.db
    await register(world, REGISTRATION)

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
    org_id = await current_org(world)
    created = await http.post(
        "/v1/kt",
        json={
            "subject_user_id": subject_id,
            "scope": FULL_SCOPE,
            "validity_days": 30,
            "period_days": 90,
            "recipient_email": RECIPIENT,
        },
        headers=csrf(http),
    )
    assert created.status_code == 201, created.text
    package = created.json()
    seeded = Seeded(code=str(package["kt_code"]), package_id=str(package["id"]), org_id=org_id)

    source_id = await _source(db, org_id)
    for key, (folder, owner, age) in DOCUMENTS.items():
        seeded.documents[key] = await _document(
            db,
            org_id=org_id,
            source_id=source_id,
            key=key,
            folder=folder,
            owner=owner,
            age_days=age,
        )
    await db.commit()

    kept = await http.post(
        f"/v1/kt/{seeded.package_id}/exclusions",
        json={"document_id": seeded.documents["A_KEPT"]},
        headers=csrf(http),
    )
    assert kept.status_code == 201, kept.text

    await register(world, RIVAL_REGISTRATION)
    rival_org = await current_org(world)
    assert rival_org != org_id
    rival_source = await _source(db, rival_org)
    folder, owner, age = RIVAL
    await _document(
        db,
        org_id=rival_org,
        source_id=rival_source,
        key="RIVAL",
        folder=folder,
        owner=owner,
        age_days=age,
    )
    await db.commit()

    await sign_in(world, RECIPIENT)
    claimed = await http.post("/v1/kt/claim", json={"kt_code": seeded.code}, headers=csrf(http))
    assert claimed.status_code == 200, claimed.text
    return seeded


# ------------------------------------------------------------------------- Ask JUTSU


class TestAskJutsuReadsTheAskersOwnFolders:
    async def test_the_askers_folder_answers_where_their_documents_are_kept(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = (await ask_jutsu(world, QUESTION)).json()
        folders = folders_in(body)

        assert [f["folder_path"] for f in folders] == ["Recipient/Astro Agent Onboarding"]
        assert folders[0]["document_id"] == seeded.documents["C_OWN"]
        cited = [c for c in body["citations"] if c["kind"] == "folder"]
        assert [c["folder_path"] for c in cited] == ["Recipient/Astro Agent Onboarding"]
        span = await world.http.get(f"/v1/evidence/{cited[0]['chunk_id']}")
        assert span.status_code == 200, span.text
        assert span.json()["folder_path"] == "Recipient/Astro Agent Onboarding"

    async def test_no_other_folder_reaches_the_model_or_the_response(self, world: World) -> None:
        await build(world)
        world.answers.prompts.clear()

        body = (await ask_jutsu(world, QUESTION)).json()
        prompt = world.answers.prompts[-1]

        assert "Recipient/Astro Agent Onboarding" in prompt
        for folder in sorted(EVERY_FOLDER - FOR_ASK_JUTSU):
            assert folder not in prompt, f"{folder} reached the model"
            assert folder not in json.dumps(body), f"{folder} reached the response"

    async def test_a_question_that_does_not_ask_where_reads_no_folder(self, world: World) -> None:
        await build(world)

        body = (await ask_jutsu(world, "Summarise the Astro Agent onboarding")).json()

        assert folders_in(body) == []
        assert body["sources"], "passages still answer it"


# ---------------------------------------------------------------------------- Ask KT


class TestAskKtReadsThePackagesFolders:
    async def test_the_subjects_folder_answers_where_their_documents_are_kept(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = (await ask_kt(world, seeded.code, KT_QUESTION)).json()
        folders = folders_in(body)

        assert [f["folder_path"] for f in folders] == ["Projects/Astro Agent"]
        assert folders[0]["document_id"] == seeded.documents["A_ASTRO"]
        cited = [c for c in body["citations"] if c["kind"] == "folder"]
        assert cited, "the folder was not cited"
        span = await world.http.get(f"/v1/kt/{seeded.code}/evidence/{cited[0]['chunk_id']}")
        assert span.status_code == 200, span.text
        assert span.json()["folder_path"] == "Projects/Astro Agent"

        replay = (
            await world.http.get(f"/v1/kt/{seeded.code}/conversations/{body['conversation_id']}")
        ).json()
        kept = [c for m in replay["messages"] for c in m["citations"] if c["kind"] == "folder"]
        assert kept, "the folder citation was not stored"
        assert all(c["available"] for c in kept)

    async def test_no_folder_outside_the_package_reaches_the_model_or_the_response(
        self, world: World
    ) -> None:
        seeded = await build(world)
        world.answers.prompts.clear()

        body = (await ask_kt(world, seeded.code, KT_QUESTION)).json()
        prompt = world.answers.prompts[-1]

        assert "Projects/Astro Agent" in prompt
        for folder in sorted(EVERY_FOLDER - FOR_ASK_KT):
            assert folder not in prompt, f"{folder} reached the model"
            assert folder not in json.dumps(body), f"{folder} reached the response"

    async def test_a_folder_names_its_documents_and_never_their_text(self, world: World) -> None:
        seeded = await build(world)

        body = (await ask_kt(world, seeded.code, KT_QUESTION)).json()
        (folder,) = folders_in(body)

        assert folder["text"] == "Folder: Projects/Astro Agent\nDocuments kept in it: A_ASTRO notes"

    async def test_the_documents_tab_says_where_each_document_is_kept(self, world: World) -> None:
        seeded = await build(world)

        listing = await world.http.get(f"/v1/kt/{seeded.code}/documents")
        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        assert {item["title"]: item["folder_path"] for item in items} == {
            "A_ASTRO notes": "Projects/Astro Agent"
        }
        detail = await world.http.get(
            f"/v1/kt/{seeded.code}/documents/{seeded.documents['A_ASTRO']}"
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["folder_path"] == "Projects/Astro Agent"

    async def test_the_curators_review_shows_each_documents_folder(self, world: World) -> None:
        seeded = await build(world)
        await sign_in(world, OWNER)

        review = await world.http.get(f"/v1/kt/{seeded.package_id}/contents")

        assert review.status_code == 200, review.text
        shown = {i["title"]: (i["folder_path"], i["excluded"]) for i in review.json()["items"]}
        assert shown == {
            "A_ASTRO notes": ("Projects/Astro Agent", False),
            "A_KEPT notes": ("Projects/Astro Agent Contracts", True),
        }


# ------------------------------------------------------------------------- mechanism

#: Documents in unrelated folders: enough that reading them all is never the cheap plan.
SCALE_ROWS = 5000


class TestTheFolderIndexAtScale:
    """Folder search stays an index lookup for the application role in a large tenant.

    Every test above runs on a handful of rows, where scanning is the cheapest plan and an
    index nobody can use looks exactly as fast. This seeds one organisation with thousands of
    documents in unrelated folders, every one readable by the asker, gives the planner real
    statistics, and reads the plan of the two statements production runs — as `jutsu_app`,
    under row-level security, because the owner bypasses RLS and would be shown a plan the
    application role never gets (ADR 0029).
    """

    @pytest.mark.parametrize("subject", [False, True], ids=["ask-jutsu", "ask-kt"])
    async def test_the_folder_statement_reads_the_word_index(
        self, db_session: AsyncSession, migration_url: str, subject: bool
    ) -> None:
        org_id = str(uuid.uuid4())
        scope = text("SELECT set_config('app.current_org_id', :org, true)")
        await db_session.execute(scope, {"org": org_id})
        await db_session.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'Scale')"), {"id": org_id}
        )
        source_id = await _source(db_session, org_id)
        await db_session.execute(
            text(
                "INSERT INTO documents (id, org_id, source_id, external_id, title, content_hash, "
                "acl_hash, body_original, body_masked, created_at, folder_path) "
                "SELECT gen_random_uuid(), CAST(:org AS uuid), CAST(:src AS uuid), "
                "'doc-' || n, 'Note ' || n, 'h' || n, 'a', 'x', 'x', now(), "
                "CASE WHEN n % 2500 = 0 THEN 'Projects/Astro Agent' "
                "ELSE 'Team ' || (n % 97) || '/Weekly notes' END "
                "FROM generate_series(1, :rows) AS n"
            ),
            {"org": org_id, "src": str(source_id), "rows": SCALE_ROWS},
        )
        # The words `folder_words` records, spelled in SQL for five thousand rows at once.
        await db_session.execute(
            text(
                "INSERT INTO document_folder_words (org_id, document_id, word) "
                "SELECT DISTINCT d.org_id, d.id, w FROM documents d, "
                "unnest(regexp_split_to_array(lower(d.folder_path), '[^a-z0-9]+')) AS w "
                "WHERE d.org_id = CAST(:org AS uuid) AND length(w) >= 3"
            ),
            {"org": org_id},
        )
        # C's own reach for Ask JUTSU, A's corpus for Ask KT: each asker reads all of them.
        for principal in (f"local:{RECIPIENT}", f"local:{SUBJECT}"):
            await db_session.execute(
                text(
                    "INSERT INTO document_acl (document_id, principal_type, principal_id, "
                    "org_id, permission) SELECT id, 'user', :pid, org_id, 'read' "
                    "FROM documents WHERE org_id = CAST(:org AS uuid)"
                ),
                {"pid": principal, "org": org_id},
            )
        await db_session.commit()

        owner = create_async_engine(migration_url, isolation_level="AUTOCOMMIT")
        async with owner.connect() as connection:
            for table in ("documents", "document_acl", "document_folder_words"):
                await connection.execute(text(f"ANALYZE {table}"))
        await owner.dispose()

        await db_session.execute(scope, {"org": org_id})
        params: dict[str, object] = {"words": ["astro", "agent"], "candidates": 50}
        if subject:
            statement = SUBJECT_FOLDERS_STATEMENT
            params |= {
                "subject_principals": [f"local:{SUBJECT}"],
                "package_id": str(uuid.uuid4()),
                "window_start": None,
                "window_end": None,
            }
        else:
            statement = FOLDERS_STATEMENT
            params |= {"principals": [f"local:{RECIPIENT}"], "groups": []}
        plan = (await db_session.execute(text(f"EXPLAIN {statement}"), params)).scalars().all()

        assert any("ix_document_folder_words_word" in line for line in plan), "\n".join(plan)
