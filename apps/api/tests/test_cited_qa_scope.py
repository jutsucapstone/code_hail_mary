"""Cited Q&A answers an employee from their own authorized memory, and from nothing else (ADR 0030).

Requester, subject and scope are one person: the signed-in employee. Nothing in the request
names a project, a source, a document, a date or another employee, and nothing a request could
carry widens what is read. The fixtures are one employee's working life and its neighbours:

    EMPLOYEE_D     projects on GitHub, a Drive design document, an email, a meeting transcript,
                   a Slack thread, Knowledge Basket files, extracted claims (a project, a
                   responsibility, a decision, a person) and two folders
    EMPLOYEE_A     a private project, document, email and meeting, and a private decision
    SHARED         a roadmap granted to a group D and A are both in, with its own claim
    OTHER TENANT   a project granted to D's exact principal strings, in another organisation

and the cases that must never answer: a superseded version, a stale extraction run, an extraction
still running, a revoked identity, a deleted basket file, an address a browser would execute.

Every chunk embeds to one direction, so passages tie and `k` retrieves every chunk the asker may
read — which is what makes "exactly these titles, and no others" an assertion about
authorization rather than about ranking. The model cites every numbered item it is shown, so
whatever reaches it reaches the response too.

A knowledge-transfer package for A, addressed to D, exists throughout: KT keeps reading A's
package through its own door, and Cited Q&A never reads it.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from jutsu_api.config import Settings, get_settings
from jutsu_api.deps import get_db, get_email_sender, get_object_store
from jutsu_api.email import RecordingEmailSender
from jutsu_api.main import create_app
from jutsu_api.retrieval import get_query_embedder
from jutsu_api.routers.search import get_answer_transport
from jutsu_api.security import CSRF_COOKIE, CSRF_HEADER
from jutsu_retrieval.claims import CLAIM_LIMIT, CLAIMS_STATEMENT, INTENT_WINDOW, search_claims
from jutsu_retrieval.folders import FOLDERS_STATEMENT
from jutsu_retrieval.search import (
    ACL_PREDICATE,
    KT_PACKAGE_PREDICATE,
    ORG_SCOPE_SQL,
    SUBJECT_PREDICATE,
)
from jutsu_retrieval.search import _statement as vector_statement
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import configure_answers

API_SRC = Path(__file__).resolve().parents[1] / "src" / "jutsu_api"

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
EMPLOYEE_D = "d.employee@example.com"
EMPLOYEE_A = "a.colleague@example.com"
#: Joined the organisation, holds nothing yet.
EMPLOYEE_E = "e.newcomer@example.com"

#: The provider subjects each employee has linked (ADR 0010). D's `local:` principal and A's
#: come from accepting the invitation.
D_SUBJECTS = {
    "github": "1001",
    "gmail": "d-google-sub",
    "zoom": "d-zoom-id",
    "slack": "U0DEMPLOYEE",
    "jira": "d-atlassian-id",
}
A_SUBJECTS = {"github": "2002", "gmail": "a-google-sub", "zoom": "a-zoom-id"}
SHARED_GROUP = "m365:group-astro-agent"

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


@dataclass(frozen=True)
class Doc:
    key: str
    system: str
    title: str
    body: str
    #: `(principal_type, principal key)`; a `user` key names an entry of `principals()`.
    grant: tuple[str, str]
    uri: str | None = None
    folder: str | None = None
    folder_uri: str | None = None
    age_days: float = 2.0


DOCS = (
    Doc("D_PROJECT_ALPHA", "github", "d-dev/astro-agent README",
        "Astro Agent is the autonomous research agent D maintains.", ("user", "D:github"),
        uri="https://github.com/d-dev/astro-agent", folder="d-dev/astro-agent",
        folder_uri="https://github.com/d-dev/astro-agent"),
    Doc("D_PROJECT_ALPHA_OLD", "github", "d-dev/astro-agent README (previous version)",
        "An earlier Astro Agent README that has since been replaced.", ("user", "D:github"),
        uri="https://github.com/d-dev/astro-agent", age_days=40),
    Doc("D_PROJECT_BETA", "github", "d-dev/orbit-cli issue 7",
        "Orbit CLI should gain a dry-run flag.", ("user", "D:github"),
        uri="https://github.com/d-dev/orbit-cli/issues/7"),
    Doc("D_DOCUMENT_ALPHA", "gmail", "Astro Agent design notes",
        "Design notes: the Astro Agent planner uses a task graph.", ("user", "D:gmail"),
        uri="https://docs.google.com/document/d/astro-agent-design",
        folder="My Drive/Projects/Astro Agent",
        folder_uri="https://drive.google.com/drive/folders/astro-agent"),
    Doc("D_EMAIL_ALPHA", "gmail", "Re: Astro Agent launch date",
        "Priya Shah confirmed the Astro Agent launch for October.", ("user", "D:gmail"),
        uri="https://mail.google.com/mail/u/0/#all/astro-launch"),
    Doc("D_MEETING_ALPHA", "zoom", "Astro Agent weekly sync transcript",
        "The weekly sync reviewed the Astro Agent evaluation results.", ("user", "D:zoom"),
        uri="https://zoom.us/rec/share/astro-agent-sync"),
    Doc("D_CONVERSATION_ALPHA", "slack", "astro-agent channel thread",
        "D wrote that the Astro Agent evaluation harness is green.", ("user", "D:slack")),
    Doc("D_BASKET_ALPHA", "basket", "astro-agent-on-call.txt",
        "On-call notes for Astro Agent incidents.", ("user", "basket:D")),
    Doc("D_BASKET_OLD", "basket", "old-astro-agent-notes.txt",
        "Old Astro Agent notes D later deleted from the basket.", ("user", "basket:D")),
    Doc("D_UNSAFE_LINK", "local", "Imported Astro Agent bookmark",
        "A bookmark imported from an old wiki.", ("user", "D:local"), uri="javascript:alert(1)"),
    Doc("D_REVOKED_ALPHA", "jira", "ASTRO-12 Astro Agent retry bug",
        "A retry bug in the Astro Agent scheduler.", ("user", "D:jira"),
        uri="https://example.atlassian.net/browse/ASTRO-12"),
    Doc("SHARED_PROJECT_DOCUMENT", "m365", "Astro Agent roadmap (shared)",
        "The Astro Agent roadmap shared with its working group.", ("group", SHARED_GROUP),
        uri="https://example.sharepoint.com/sites/astro/roadmap.docx"),
    Doc("A_PRIVATE_PROJECT", "github", "a-dev/secret-lander README",
        "A's private lander project.", ("user", "A:github"),
        uri="https://github.com/a-dev/secret-lander"),
    Doc("A_PRIVATE_DOCUMENT", "gmail", "A private Astro Agent critique",
        "A's private critique of Astro Agent.", ("user", "A:gmail"),
        folder="My Drive/Private/Astro Agent critiques"),
    Doc("A_PRIVATE_EMAIL", "gmail", "A personal leave request",
        "A asked for leave in November.", ("user", "A:gmail")),
    Doc("A_PRIVATE_MEETING", "zoom", "A one-to-one with their manager",
        "A discussed Astro Agent staffing privately.", ("user", "A:zoom")),
)  # fmt: skip
BY_KEY = {doc.key: doc for doc in DOCS}

#: `(document key, claim type, name, summary, on a chunk not yet embedded)` in the latest run.
CLAIMS = (
    ("D_PROJECT_ALPHA", "project", "Astro Agent", "The research agent D maintains", False),
    (
        "D_DOCUMENT_ALPHA",
        "responsibility",
        "Astro Agent release ownership",
        "D owns releases",
        False,
    ),
    (
        "D_MEETING_ALPHA",
        "decision",
        "Ship Astro Agent on Postgres",
        "Agreed in the weekly sync",
        True,
    ),
    ("D_EMAIL_ALPHA", "person", "Priya Shah", "Confirmed the Astro Agent launch date", False),
    (
        "D_REVOKED_ALPHA",
        "decision",
        "Revoked Astro Agent decision",
        "From a revoked account",
        False,
    ),
    (
        "SHARED_PROJECT_DOCUMENT",
        "project",
        "Astro Agent roadmap",
        "Shared with the working group",
        False,
    ),
    ("A_PRIVATE_PROJECT", "decision", "Cancel the secret lander", "A's private decision", False),
    (
        "D_PROJECT_ALPHA_OLD",
        "decision",
        "Superseded Astro Agent decision",
        "On a replaced version",
        False,
    ),
)
STALE_CLAIM = "Stale Astro Agent decision"
UNFINISHED_CLAIM = "Unfinished Astro Agent decision"
RIVAL_TITLE = "rival/astro-agent-clone README"
RIVAL_CLAIM = "Rival Astro Agent project"

#: What D's own reach holds, as embedded passages.
D_PASSAGES = {
    BY_KEY[key].title
    for key in (
        "D_PROJECT_ALPHA", "D_PROJECT_BETA", "D_DOCUMENT_ALPHA", "D_EMAIL_ALPHA", "D_MEETING_ALPHA",
        "D_CONVERSATION_ALPHA", "D_BASKET_ALPHA", "D_BASKET_OLD", "D_UNSAFE_LINK",
        "D_REVOKED_ALPHA", "SHARED_PROJECT_DOCUMENT",
    )
}  # fmt: skip
A_PRIVATE_TITLES = {BY_KEY[key].title for key in BY_KEY if key.startswith("A_PRIVATE")}

#: Never visible to D, in any form: a colleague's private items, another tenant's, a replaced
#: version, and claims that are not current.
NEVER_FOR_D = (
    *sorted(A_PRIVATE_TITLES),
    "Cancel the secret lander",
    "d-dev/astro-agent README (previous version)",
    "Superseded Astro Agent decision",
    STALE_CLAIM,
    UNFINISHED_CLAIM,
    RIVAL_TITLE,
    RIVAL_CLAIM,
)

GENERIC = "Summarise what I know about Astro Agent"
SCREENSHOT = "tell me about my project from github astro agent"


class CitingAnswers:
    """Cites every numbered item the prompt carries, or nothing at all when `uncited`."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.uncited = False

    async def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.uncited:
            return "Astro Agent is probably doing fine."
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
    rival_org_id: str = ""
    d_id: str = ""
    a_id: str = ""
    e_id: str = ""
    code: str = ""
    package_id: str = ""
    basket_file_id: str = ""
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
    app.dependency_overrides[get_object_store] = lambda: None

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


async def ask(world: World, question: str, *, k: int = K) -> dict[str, Any]:
    response = await world.http.post(
        "/v1/ask", json={"question": question, "k": k}, headers=csrf(world.http)
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def titles(body: dict[str, Any], kind: str) -> set[str]:
    return {source["document_title"] for source in body["sources"] if source["kind"] == kind}


def claim_texts(body: dict[str, Any]) -> str:
    return "\n".join(source["text"] for source in body["sources"] if source["kind"] == "claim")


def assert_nothing_outside_d(world: World, body: dict[str, Any]) -> None:
    prompt = world.answers.prompts[-1] if world.answers.prompts else ""
    response = json.dumps(body)
    for forbidden in NEVER_FOR_D:
        assert forbidden not in prompt, f"{forbidden!r} reached the model"
        assert forbidden not in response, f"{forbidden!r} reached the response"


async def scope(db: AsyncSession, org_id: str) -> None:
    await db.execute(text("SELECT set_config('app.current_org_id', :org, true)"), {"org": org_id})


async def link(db: AsyncSession, *, org_id: str, user_id: str, system: str, subject: str) -> None:
    await db.execute(
        text(
            "INSERT INTO source_identities (org_id, user_id, source_system, subject, linked_by) "
            "VALUES (:org, :user, CAST(:system AS source_system), :subject, 'test')"
        ),
        {"org": org_id, "user": user_id, "system": system, "subject": subject},
    )


async def source_for(db: AsyncSession, org_id: str, system: str, cache: dict[str, str]) -> str:
    if system not in cache:
        source_id = str(uuid.uuid4())
        await db.execute(
            text(
                "INSERT INTO sources (id, org_id, system, config_json) "
                "VALUES (:id, :org, CAST(:system AS source_system), '{}'::jsonb)"
            ),
            {"id": source_id, "org": org_id, "system": system},
        )
        cache[system] = source_id
    return cache[system]


async def insert_document(
    db: AsyncSession,
    *,
    org_id: str,
    source_id: str,
    doc: Doc,
    principal: tuple[str, str],
    external_id: str | None = None,
) -> tuple[str, str]:
    """One document with one embedded chunk, its grant, and its folder words."""
    from jutsu_retrieval.terms import folder_words

    document_id, chunk_id = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(
        text(
            "INSERT INTO documents (id, org_id, source_id, external_id, uri, title, content_hash, "
            "acl_hash, body_original, body_masked, created_at, folder_path, folder_uri) "
            "VALUES (:id, :org, :src, :ext, :uri, :title, :hash, 'a', :body, :body, "
            "now() - CAST(:age AS double precision) * interval '1 day', :folder, :folder_uri)"
        ),
        {
            "id": document_id,
            "org": org_id,
            "src": source_id,
            "ext": external_id or doc.key.lower(),
            "uri": doc.uri,
            "title": doc.title,
            "hash": doc.key.lower(),
            "body": doc.body,
            "age": doc.age_days,
            "folder": doc.folder,
            "folder_uri": doc.folder_uri,
        },
    )
    if doc.folder:
        await db.execute(
            text(
                "INSERT INTO document_folder_words (org_id, document_id, word) "
                "SELECT CAST(:org AS uuid), CAST(:doc AS uuid), unnest(CAST(:words AS text[]))"
            ),
            {"org": org_id, "doc": document_id, "words": folder_words(doc.folder)},
        )
    await db.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
            "permission) VALUES (:doc, :ptype, :pid, :org, 'read')"
        ),
        {"doc": document_id, "ptype": principal[0], "pid": principal[1], "org": org_id},
    )
    await insert_chunk(
        db, org_id=org_id, document_id=document_id, chunk_id=chunk_id, ordinal=0, body=doc.body
    )
    return document_id, chunk_id


async def insert_chunk(
    db: AsyncSession,
    *,
    org_id: str,
    document_id: str,
    chunk_id: str,
    ordinal: int,
    body: str,
    embedded: bool = True,
) -> None:
    await db.execute(
        text(
            "INSERT INTO chunks (id, document_id, org_id, ordinal, text, char_start, char_end, "
            "token_count, embedding) VALUES (:id, :doc, :org, :ordinal, :text, 0, :end, 8, "
            "CAST(:vec AS vector))"
        ),
        {
            "id": chunk_id,
            "doc": document_id,
            "org": org_id,
            "ordinal": ordinal,
            "text": body,
            "end": len(body),
            "vec": VECTOR_LITERAL if embedded else None,
        },
    )


async def insert_run(
    db: AsyncSession, *, org_id: str, document_id: str, hours_ago: float, finished: bool = True
) -> str:
    run_id = str(uuid.uuid4())
    finished_at = "now()" if finished else "NULL"
    await db.execute(
        text(
            "INSERT INTO extraction_runs (id, org_id, extractor_version, prompt_hash, model, "  # noqa: S608
            "started_at, finished_at, stats_json) VALUES (:id, :org, 'v1', 'h', 'test', "
            f"now() - CAST(:ago AS double precision) * interval '1 hour', {finished_at}, "
            "cast(:stats AS jsonb))"
        ),
        {
            "id": run_id,
            "org": org_id,
            "ago": hours_ago,
            "stats": json.dumps({"document_id": document_id}),
        },
    )
    return run_id


async def insert_claim(
    db: AsyncSession,
    *,
    org_id: str,
    run_id: str,
    chunk_id: str,
    claim_type: str,
    name: str,
    summary: str,
    quote: str,
) -> None:
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
            "payload": json.dumps({"name": name, "summary": summary, "quote": quote}),
        },
    )


async def build(world: World) -> Seeded:
    http, mailbox, db = world.http, world.mailbox, world.db
    seeded = Seeded()

    await register(world, REGISTRATION)
    tokens: list[str] = []
    for email in (EMPLOYEE_D, EMPLOYEE_A, EMPLOYEE_E):
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
    seeded.d_id = await employee_id(world, EMPLOYEE_D)
    seeded.a_id = await employee_id(world, EMPLOYEE_A)
    seeded.e_id = await employee_id(world, EMPLOYEE_E)

    org = seeded.org_id
    await scope(db, org)
    for system, subject in D_SUBJECTS.items():
        await link(db, org_id=org, user_id=seeded.d_id, system=system, subject=subject)
    await link(db, org_id=org, user_id=seeded.d_id, system="basket", subject=seeded.d_id)
    for system, subject in A_SUBJECTS.items():
        await link(db, org_id=org, user_id=seeded.a_id, system=system, subject=subject)
    for user in (seeded.d_id, seeded.a_id):
        await db.execute(
            text(
                "INSERT INTO user_groups (user_id, group_external_id, org_id) VALUES (:u, :g, :o)"
            ),
            {"u": user, "g": SHARED_GROUP, "o": org},
        )

    principals = {
        "D:local": f"local:{EMPLOYEE_D}",
        "basket:D": f"basket:{seeded.d_id}",
        **{f"D:{system}": f"{system}:{subject}" for system, subject in D_SUBJECTS.items()},
        **{f"A:{system}": f"{system}:{subject}" for system, subject in A_SUBJECTS.items()},
    }
    sources: dict[str, str] = {}
    for doc in DOCS:
        principal = (
            doc.grant[0],
            principals[doc.grant[1]] if doc.grant[0] == "user" else doc.grant[1],
        )
        external_id = None
        if doc.key == "D_BASKET_OLD":
            seeded.basket_file_id = str(uuid.uuid4())
            external_id = seeded.basket_file_id
        seeded.documents[doc.key], seeded.chunks[doc.key] = await insert_document(
            db,
            org_id=org,
            source_id=await source_for(db, org, doc.system, sources),
            doc=doc,
            principal=principal,
            external_id=external_id,
        )
    await db.execute(
        text("UPDATE documents SET superseded_by = :new WHERE id = :old"),
        {
            "new": seeded.documents["D_PROJECT_ALPHA"],
            "old": seeded.documents["D_PROJECT_ALPHA_OLD"],
        },
    )
    await db.execute(
        text(
            "INSERT INTO basket_files (id, org_id, owner_user_id, object_key, original_filename, "
            "normalised_filename, declared_mime, size_bytes, state, document_id, extracted_chars) "
            "VALUES (:id, :org, :owner, :key, :name, :name, 'text/plain', 64, 'ready', :doc, 64)"
        ),
        {
            "id": seeded.basket_file_id,
            "org": org,
            "owner": seeded.d_id,
            "key": f"org/{org}/basket/{seeded.basket_file_id}",
            "name": BY_KEY["D_BASKET_OLD"].title,
            "doc": seeded.documents["D_BASKET_OLD"],
        },
    )

    # The decision D's meeting recorded sits in a later passage that is not embedded yet: the
    # intent arm, not the passage ranking, is what can find it.
    meeting_decision_chunk = str(uuid.uuid4())
    await insert_chunk(
        db,
        org_id=org,
        document_id=seeded.documents["D_MEETING_ALPHA"],
        chunk_id=meeting_decision_chunk,
        ordinal=1,
        body="Decision: ship Astro Agent on Postgres.",
        embedded=False,
    )
    seeded.chunks["D_MEETING_ALPHA_DECISION"] = meeting_decision_chunk

    runs: dict[str, str] = {}
    for key, claim_type, name, summary, unembedded in CLAIMS:
        if key not in runs:
            runs[key] = await insert_run(
                db, org_id=org, document_id=seeded.documents[key], hours_ago=1
            )
        chunk = meeting_decision_chunk if unembedded else seeded.chunks[key]
        quote = "ship Astro Agent on Postgres" if unembedded else BY_KEY[key].body[:24]
        await insert_claim(
            db, org_id=org, run_id=runs[key], chunk_id=chunk, claim_type=claim_type,
            name=name, summary=summary, quote=quote,
        )  # fmt: skip
    # An older finished run on D's design notes (superseded by the latest one), and a run on
    # the same document that has not finished.
    stale = await insert_run(
        db, org_id=org, document_id=seeded.documents["D_DOCUMENT_ALPHA"], hours_ago=24 * 30
    )
    await insert_claim(
        db, org_id=org, run_id=stale, chunk_id=seeded.chunks["D_DOCUMENT_ALPHA"],
        claim_type="decision", name=STALE_CLAIM, summary="From a run replaced since", quote="Design",
    )  # fmt: skip
    running = await insert_run(
        db, org_id=org, document_id=seeded.documents["D_DOCUMENT_ALPHA"], hours_ago=0.1,
        finished=False,
    )  # fmt: skip
    await insert_claim(
        db, org_id=org, run_id=running, chunk_id=seeded.chunks["D_DOCUMENT_ALPHA"],
        claim_type="decision", name=UNFINISHED_CLAIM, summary="From a run still going", quote="Design",
    )  # fmt: skip
    await db.commit()

    # A knowledge-transfer package: A's knowledge, for D.
    created = await http.post(
        "/v1/kt",
        json={
            "subject_user_id": seeded.a_id,
            "scope": FULL_SCOPE,
            "validity_days": 30,
            "period_days": 90,
            "recipient_email": EMPLOYEE_D,
        },
        headers=csrf(http),
    )
    assert created.status_code == 201, created.text
    seeded.code, seeded.package_id = str(created.json()["kt_code"]), str(created.json()["id"])

    # Another organisation, granting a project to D's exact principal strings.
    await register(world, RIVAL_REGISTRATION)
    seeded.rival_org_id = await current_org(world)
    assert seeded.rival_org_id != org
    await scope(db, seeded.rival_org_id)
    rival_sources: dict[str, str] = {}
    rival = Doc("RIVAL", "github", RIVAL_TITLE, "A rival clone of Astro Agent.", ("user", ""))
    rival_doc, seeded.rival_chunk = await insert_document(
        db,
        org_id=seeded.rival_org_id,
        source_id=await source_for(db, seeded.rival_org_id, "github", rival_sources),
        doc=rival,
        principal=("user", f"github:{D_SUBJECTS['github']}"),
    )
    await db.execute(
        text(
            "INSERT INTO document_acl (document_id, principal_type, principal_id, org_id, "
            "permission) VALUES (:doc, 'user', :pid, :org, 'read')"
        ),
        {"doc": rival_doc, "pid": f"local:{EMPLOYEE_D}", "org": seeded.rival_org_id},
    )
    rival_run = await insert_run(db, org_id=seeded.rival_org_id, document_id=rival_doc, hours_ago=1)
    await insert_claim(
        db, org_id=seeded.rival_org_id, run_id=rival_run, chunk_id=seeded.rival_chunk,
        claim_type="project", name=RIVAL_CLAIM, summary="Another tenant's project", quote="A rival",
    )  # fmt: skip
    await db.commit()

    await sign_in(world, EMPLOYEE_D)
    claimed = await http.post("/v1/kt/claim", json={"kt_code": seeded.code}, headers=csrf(http))
    assert claimed.status_code == 200, claimed.text
    world.answers.prompts.clear()
    return seeded


# ------------------------------------------------------------------ identity is the scope


class TestTheSignedInEmployeeIsTheScope:
    @pytest.mark.parametrize(
        "extra",
        [
            {"org_id": "00000000-0000-4000-8000-000000000000"},
            {"user_id": "00000000-0000-4000-8000-000000000000"},
            {"principals": ["github:2002"]},
            {"kt_code": "KT-ANYTHING"},
            {"subject_user_id": "00000000-0000-4000-8000-000000000000"},
            {"project": "Astro Agent"},
            {"source": "github"},
            {"document_ids": ["00000000-0000-4000-8000-000000000000"]},
        ],
    )
    async def test_a_request_cannot_name_a_scope(self, world: World, extra: dict[str, Any]) -> None:
        await build(world)

        response = await world.http.post(
            "/v1/ask", json={"question": GENERIC, **extra}, headers=csrf(world.http)
        )

        assert response.status_code == 422, response.text

    async def test_d_reads_every_kind_of_their_own_memory_and_nothing_else(
        self, world: World
    ) -> None:
        await build(world)

        body = await ask(world, GENERIC)

        # Documents, files, email, a meeting, a conversation, basket files, a shared document:
        # exactly D's reach, whatever its source.
        assert titles(body, "passage") == D_PASSAGES
        systems = {s["source_system"] for s in body["sources"] if s["kind"] == "passage"}
        assert systems == {"github", "gmail", "zoom", "slack", "basket", "local", "jira", "m365"}
        assert body["insufficient_evidence"] is False
        assert body["refusal_reason"] is None
        assert_nothing_outside_d(world, body)

    async def test_the_screenshot_question_is_answered_from_ds_github_project(
        self, world: World
    ) -> None:
        await build(world)

        body = await ask(world, SCREENSHOT)

        assert body["insufficient_evidence"] is False
        assert body["answer"]
        claims = [s for s in body["sources"] if s["kind"] == "claim"]
        project = next(s for s in claims if "Extracted project: Astro Agent" in s["text"])
        assert project["claim_type"] == "project"
        assert project["document_title"] == "d-dev/astro-agent README"
        assert project["source_uri"] == "https://github.com/d-dev/astro-agent"
        cited = {c["chunk_id"]: c for c in body["citations"]}
        assert cited[project["chunk_id"]]["source_uri"] == "https://github.com/d-dev/astro-agent"
        assert_nothing_outside_d(world, body)

    async def test_a_colleague_reads_their_own_reach_not_ds(self, world: World) -> None:
        await build(world)
        await sign_in(world, EMPLOYEE_A)

        body = await ask(world, GENERIC)

        passages = titles(body, "passage")
        assert A_PRIVATE_TITLES <= passages
        assert "Astro Agent roadmap (shared)" in passages
        assert passages.isdisjoint(D_PASSAGES - {"Astro Agent roadmap (shared)"})


# ------------------------------------------------------------------------------ claims


class TestDsExtractedClaims:
    async def test_decisions_come_from_the_intent_arm_and_only_current_ones(
        self, world: World
    ) -> None:
        seeded = await build(world)

        body = await ask(world, "Which decisions did I make about Astro Agent?")
        text_of_claims = claim_texts(body)

        assert "Ship Astro Agent on Postgres" in text_of_claims
        decision = next(s for s in body["sources"] if "Ship Astro Agent on Postgres" in s["text"])
        # Cited through the passage it came from, which D can open.
        assert decision["chunk_id"] == seeded.chunks["D_MEETING_ALPHA_DECISION"]
        assert decision["document_id"] == seeded.documents["D_MEETING_ALPHA"]
        opened = await world.http.get(f"/v1/evidence/{decision['chunk_id']}")
        assert opened.status_code == 200, opened.text
        assert opened.json()["source_uri"] == "https://zoom.us/rec/share/astro-agent-sync"
        assert_nothing_outside_d(world, body)

    async def test_responsibilities_and_people_are_found_by_what_the_question_asks(
        self, world: World
    ) -> None:
        await build(world)

        responsible = await ask(world, "What am I responsible for?")
        people = await ask(world, "Who have I worked with on Astro Agent?")

        assert "Astro Agent release ownership" in claim_texts(responsible)
        assert "Priya Shah" in claim_texts(people)
        assert_nothing_outside_d(world, people)

    async def test_claims_are_bounded(self, world: World) -> None:
        seeded = await build(world)
        await scope(world.db, seeded.org_id)
        run = await insert_run(
            world.db,
            org_id=seeded.org_id,
            document_id=seeded.documents["D_PROJECT_BETA"],
            hours_ago=1,
        )
        for number in range(3 * CLAIM_LIMIT):
            await insert_claim(
                world.db, org_id=seeded.org_id, run_id=run, chunk_id=seeded.chunks["D_PROJECT_BETA"],
                claim_type="decision", name=f"Orbit decision {number}", summary="Bulk", quote="Orbit",
            )  # fmt: skip
        await world.db.commit()

        body = await ask(world, "Which decisions have I made?")

        assert 0 < sum(1 for s in body["sources"] if s["kind"] == "claim") <= CLAIM_LIMIT

    async def test_a_shared_documents_claim_is_not_blocked(self, world: World) -> None:
        await build(world)

        body = await ask(world, "Which projects am I part of?")

        assert "Astro Agent roadmap" in claim_texts(body)


# ----------------------------------------------------------------------------- folders


class TestDsFolders:
    async def test_where_questions_read_ds_folders_through_ds_documents(self, world: World) -> None:
        await build(world)

        body = await ask(world, "Where are my Astro Agent documents stored?")
        folders = {s["folder_path"]: s for s in body["sources"] if s["kind"] == "folder"}

        assert set(folders) == {"My Drive/Projects/Astro Agent", "d-dev/astro-agent"}
        drive = folders["My Drive/Projects/Astro Agent"]
        assert drive["text"] == (
            "Folder: My Drive/Projects/Astro Agent\nDocuments kept in it: Astro Agent design notes"
        )
        assert drive["source_uri"] == "https://drive.google.com/drive/folders/astro-agent"
        assert_nothing_outside_d(world, body)


# ---------------------------------------------------------------------------- security


class TestNothingOutsideDsReach:
    @pytest.mark.parametrize(
        "question",
        [
            GENERIC,
            SCREENSHOT,
            "Which decisions did A make about the secret lander?",
            "What did A discuss with their manager about Astro Agent staffing?",
            "Tell me about the rival astro agent clone",
        ],
    )
    async def test_no_colleague_or_other_tenant_evidence_reaches_the_model_or_response(
        self, world: World, question: str
    ) -> None:
        await build(world)

        body = await ask(world, question)

        assert_nothing_outside_d(world, body)

    async def test_the_citation_door_is_404_for_everything_outside_ds_reach(
        self, world: World
    ) -> None:
        seeded = await build(world)

        for key in ("A_PRIVATE_PROJECT", "A_PRIVATE_DOCUMENT", "A_PRIVATE_EMAIL", "A_PRIVATE_MEETING",
                    "D_PROJECT_ALPHA_OLD"):  # fmt: skip
            response = await world.http.get(f"/v1/evidence/{seeded.chunks[key]}")
            assert response.status_code == 404, key
        assert (await world.http.get(f"/v1/evidence/{seeded.rival_chunk}")).status_code == 404
        own = await world.http.get(f"/v1/evidence/{seeded.chunks['D_PROJECT_ALPHA']}")
        assert own.status_code == 200

    async def test_a_revoked_identity_stops_contributing_on_the_next_question(
        self, world: World
    ) -> None:
        seeded = await build(world)
        before = await ask(world, "Which decisions did I make about Astro Agent?")
        assert "ASTRO-12 Astro Agent retry bug" in titles(before, "passage")
        assert "Revoked Astro Agent decision" in claim_texts(before)

        await scope(world.db, seeded.org_id)
        await world.db.execute(
            text(
                "UPDATE source_identities SET is_active = false, revoked_at = now() "
                "WHERE user_id = :user AND source_system = 'jira'"
            ),
            {"user": seeded.d_id},
        )
        await world.db.commit()
        after = await ask(world, "Which decisions did I make about Astro Agent?")

        assert "ASTRO-12 Astro Agent retry bug" not in titles(after, "passage")
        assert "Revoked Astro Agent decision" not in json.dumps(after)
        assert "Revoked Astro Agent decision" not in world.answers.prompts[-1]

    async def test_a_deleted_basket_file_is_not_retrievable(self, world: World) -> None:
        seeded = await build(world)
        assert "old-astro-agent-notes.txt" in titles(await ask(world, GENERIC), "passage")

        removed = await world.http.delete(
            f"/v1/basket/files/{seeded.basket_file_id}", headers=csrf(world.http)
        )
        assert removed.status_code == 204, removed.text
        body = await ask(world, GENERIC)

        assert "old-astro-agent-notes.txt" not in json.dumps(body)
        assert "Old Astro Agent notes" not in world.answers.prompts[-1]
        assert "astro-agent-on-call.txt" in titles(body, "passage")

    async def test_a_newcomer_with_nothing_gets_nothing(self, world: World) -> None:
        await build(world)
        await sign_in(world, EMPLOYEE_E)
        world.answers.prompts.clear()

        body = await ask(world, SCREENSHOT)

        assert body["sources"] == []
        assert body["citations"] == []
        assert body["insufficient_evidence"] is True
        assert body["refusal_reason"] == "no_authorized_evidence"
        assert body["attempts"] == 0
        assert world.answers.prompts == []


# --------------------------------------------------------------- citations and grounding


class TestCitationsAndGrounding:
    async def test_every_citation_is_authorized_evidence_with_its_sources_own_link(
        self, world: World
    ) -> None:
        await build(world)

        body = await ask(world, GENERIC)
        sources = body["sources"]

        assert body["citations"], "the model cited nothing"
        for citation in body["citations"]:
            source = sources[citation["marker"] - 1]
            assert citation["chunk_id"] == source["chunk_id"]
            assert citation["source_uri"] == source["source_uri"]
            assert citation["occurred_at"]
            opened = await world.http.get(f"/v1/evidence/{citation['chunk_id']}")
            assert opened.status_code == 200, citation
            if source["kind"] == "passage":
                assert opened.json()["source_uri"] == source["source_uri"]

        links = {s["document_title"]: s["source_uri"] for s in sources if s["kind"] == "passage"}
        assert links["d-dev/astro-agent README"] == "https://github.com/d-dev/astro-agent"
        assert (
            links["Re: Astro Agent launch date"]
            == "https://mail.google.com/mail/u/0/#all/astro-launch"
        )
        # No address a browser would execute, and none made up where the source gave none.
        assert links["Imported Astro Agent bookmark"] is None
        assert links["astro-agent channel thread"] is None
        assert links["astro-agent-on-call.txt"] is None

    async def test_an_answer_that_cannot_be_grounded_is_refused_as_not_answered(
        self, world: World
    ) -> None:
        await build(world)
        world.answers.uncited = True

        body = await ask(world, GENERIC)

        assert body["answer"] is None
        assert body["insufficient_evidence"] is True
        assert body["refusal_reason"] == "evidence_does_not_answer"
        assert body["attempts"] == 2
        assert body["citations"] == []


# ------------------------------------------------------------ knowledge transfer stays apart


class TestKnowledgeTransferStaysSeparate:
    async def test_a_package_d_holds_changes_nothing_cited_qa_reads(self, world: World) -> None:
        seeded = await build(world)

        body = await ask(world, "What did A decide and discuss privately about Astro Agent?")
        assert_nothing_outside_d(world, body)

        # KT still reads A's package, through its own door.
        kt = await world.http.post(
            f"/v1/kt/{seeded.code}/ask",
            json={"question": "What did A discuss about Astro Agent?", "k": K},
            headers=csrf(world.http),
        )
        assert kt.status_code == 200, kt.text
        assert A_PRIVATE_TITLES & {s["document_title"] for s in kt.json()["sources"]}
        assert (
            await world.http.get(f"/v1/evidence/{seeded.chunks['A_PRIVATE_MEETING']}")
        ).status_code == 404

    def test_the_ask_route_imports_nothing_from_knowledge_transfer(self) -> None:
        source = (API_SRC / "routers" / "search.py").read_text(encoding="utf-8")

        assert not re.search(r"(from|import)\s+jutsu_api\.kt", source)
        for name in ("KtScope", "search_subject_chunks", "search_subject_folders",
                     "fetch_subject_evidence", "KT_PACKAGE_PREDICATE", "SUBJECT_PREDICATE",
                     "resolve_subject_principals", "kt_code"):  # fmt: skip
            assert name not in source, name

    @pytest.mark.parametrize(
        "statement",
        [CLAIMS_STATEMENT, FOLDERS_STATEMENT, vector_statement(paginated=False)],
        ids=["claims", "folders", "passages"],
    )
    def test_every_cited_qa_statement_is_the_callers_acl_in_the_callers_tenant(
        self, statement: str
    ) -> None:
        assert ACL_PREDICATE in statement
        assert KT_PACKAGE_PREDICATE not in statement
        assert SUBJECT_PREDICATE not in statement
        assert ":subject_principals" not in statement
        assert "kt_" not in statement
        assert statement.count(ORG_SCOPE_SQL) >= 2


# ---------------------------------------------------------------------------- mechanism


class TestTheClaimsStatement:
    def test_it_takes_no_principals_groups_or_tenant(self) -> None:
        parameters = set(inspect.signature(search_claims).parameters)

        assert not parameters & {"principals", "groups", "org_id", "subject_user_id", "package_id"}

    def test_its_filters_are_in_the_sql(self) -> None:
        assert f"cl.org_id = {ORG_SCOPE_SQL}" in CLAIMS_STATEMENT
        assert f"d.org_id = {ORG_SCOPE_SQL}" in CLAIMS_STATEMENT
        assert "d.superseded_by IS NULL" in CLAIMS_STATEMENT
        assert "finished_at IS NOT NULL" in CLAIMS_STATEMENT
        assert "LIMIT :limit" in CLAIMS_STATEMENT

    def test_the_two_surfaces_ask_about_the_same_kinds_of_claim(self) -> None:
        """Cited Q&A keeps its own vocabulary because KT keeps its own implementation. Two
        copies of a word list drift; this is what stops them drifting silently."""
        from jutsu_api.kt_search import _INTENTS
        from jutsu_retrieval.claims import CLAIM_INTENTS

        assert CLAIM_INTENTS == _INTENTS

    def test_it_does_not_correlate_the_latest_run_per_claim(self) -> None:
        """A 200x cliff, pinned as a string: a correlated latest-run lookup keyed on
        `stats_json->>'document_id'` cannot use its index beneath row-level security (the
        operator is not leakproof), and measured 27 s at 5,000 documents as the application
        role (ADR 0030)."""
        from jutsu_api.kt import _LATEST_RUN_JOIN

        assert _LATEST_RUN_JOIN not in CLAIMS_STATEMENT
        assert "LIMIT 1)" not in CLAIMS_STATEMENT

    async def test_the_plan_authorizes_in_sql(self, world: World) -> None:
        seeded = await build(world)
        await scope(world.db, seeded.org_id)

        plan = "\n".join(
            str(row[0])
            for row in (
                await world.db.execute(
                    text("EXPLAIN " + CLAIMS_STATEMENT),
                    {
                        "principals": [f"github:{D_SUBJECTS['github']}"],
                        "groups": [],
                        "anchors": [seeded.chunks["D_PROJECT_ALPHA"]],
                        "intent_types": ["project"],
                        "terms": "astro | agent",
                        "window": INTENT_WINDOW,
                        "limit": CLAIM_LIMIT,
                    },
                )
            ).all()
        )

        assert "document_acl" in plan, plan


# ------------------------------------------------------------------------ observability


class TestObservability:
    async def test_the_ask_line_carries_counts_and_timings_and_no_content(
        self, world: World, caplog: pytest.LogCaptureFixture
    ) -> None:
        await build(world)
        caplog.set_level(logging.INFO, logger="jutsu.api.ask")

        body = await ask(world, SCREENSHOT)

        lines = [r for r in caplog.records if r.name == "jutsu.api.ask"]
        completed = [
            r.args
            for r in lines
            if isinstance(r.args, dict) and r.args.get("event") == "ask_completed"
        ]
        assert len(completed) == 1
        event = completed[0]
        assert event["passages"] == len([s for s in body["sources"] if s["kind"] == "passage"])
        assert event["claims"] == len([s for s in body["sources"] if s["kind"] == "claim"])
        assert event["citations"] == len(body["citations"])
        for key in ("embed_ms", "retrieve_ms", "claims_ms", "folders_ms", "answer_ms", "total_ms"):
            assert isinstance(event[key], int), key
        rendered = "\n".join(r.getMessage() for r in lines)
        for private in (SCREENSHOT, "astro-agent README", "https://", "Astro Agent", EMPLOYEE_D):
            assert private not in rendered, private
